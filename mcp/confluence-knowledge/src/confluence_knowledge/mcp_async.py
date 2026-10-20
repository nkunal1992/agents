"""Run sync MCP tools off the event loop.

FastMCP (mcp 1.30) calls synchronous tool functions inline on the asyncio
loop — one slow `search` (model warm-up, rerank) or a SQLite wait would
block *every* other request, including `crawl_step` polls, which the IDE
then reports as a hung server. `tool()` registers an async wrapper that
dispatches to a worker thread while leaving the module attribute a plain
sync function, so existing tests keep calling it directly.
"""

from __future__ import annotations

import functools
import inspect
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
            return await anyio.to_thread.run_sync(call, limiter=limiter)

        functools.update_wrapper(runner, fn)
        runner.__signature__ = sig
        runner.__annotations__ = dict(getattr(fn, "__annotations__", {}))
        mcp.add_tool(runner, name=fn.__name__,
                     description=inspect.getdoc(fn))
        return fn

    return wrap
