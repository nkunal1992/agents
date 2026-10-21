"""Run sync MCP tools off the event loop.

FastMCP (mcp 1.30) calls synchronous tool functions inline on the asyncio
loop — one slow `search` (model warm-up, rerank) or a SQLite wait would
block *every* other request, including `crawl_step` polls, which the IDE
then reports as a hung server. `tool()` registers an async wrapper that
dispatches to a worker thread while leaving the module attribute a plain
sync function, so existing tests keep calling it directly.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

# Heavy tools (search/rerank, warm-up waits) run one at a time — two
# concurrent rerank batches would double the bounded memory peak; queueing
# is cheaper than allocating a second batch. Light tools share a wider
# limiter so a blocked search never stalls crawl_step/status/etc.
HEAVY = anyio.CapacityLimiter(1)
LIGHT = anyio.CapacityLimiter(8)

# Sync tools run on a worker thread but may still emit MCP progress
# notifications — the spec-correct keep-alive that resets the client's
# request timeout on long polls (wait_for_crawl). The worker pushes into
# a plain Queue; an async pump on the runner drains it via
# ctx.report_progress. No client progressToken → report_progress is a
# no-op, so heartbeat emission is always safe.
_hb_local = threading.local()
_HB_DRAIN_S = 0.4


def heartbeat(progress: float, total: float | None = None,
              message: str | None = None) -> None:
    """Emit a progress notification from inside a sync tool. No-op when
    the caller isn't a heartbeat-aware wrapper (tests call tools
    directly)."""
    q = getattr(_hb_local, "queue", None)
    if q is not None:
        q.put((progress, total, message))


def tool(mcp: FastMCP, *, heavy: bool = False) -> Callable:
    """Decorator replacing ``@mcp.tool()``: registers an async, thread-
    offloaded wrapper under the same name/schema and returns the original
    sync function unchanged for the module namespace."""

    def wrap(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        limiter = HEAVY if heavy else LIGHT

        async def runner(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call = functools.partial(fn, *bound.args, **bound.kwargs)
            ctx = bound.arguments.get("ctx")
            if ctx is None:
                return await anyio.to_thread.run_sync(call, limiter=limiter)
            q: queue.Queue = queue.Queue()
            done = asyncio.Event()

            def _call() -> Any:
                _hb_local.queue = q
                try:
                    return call()
                finally:
                    _hb_local.queue = None

            async def _pump() -> None:
                while not done.is_set() or not q.empty():
                    try:
                        p, t, m = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(_HB_DRAIN_S)
                        continue
                    try:
                        await ctx.report_progress(p, t, m)
                    except Exception:
                        logging.getLogger(__name__).debug(
                            "heartbeat dropped", exc_info=True)

            task = asyncio.create_task(_pump())
            try:
                return await anyio.to_thread.run_sync(
                    _call, limiter=limiter)
            finally:
                done.set()
                await task

        functools.update_wrapper(runner, fn)
        runner.__signature__ = sig
        runner.__annotations__ = dict(getattr(fn, "__annotations__", {}))
        mcp.add_tool(runner, name=fn.__name__,
                     description=inspect.getdoc(fn))
        return fn

    return wrap
