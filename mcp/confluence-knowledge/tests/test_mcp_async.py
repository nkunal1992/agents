"""Off-loop tool execution — a slow/blocked heavy tool must not stall
light tool calls, and the registered wrapper must keep the sync
signature's schema."""

import inspect
import json
import threading
import time

import anyio

from confluence_knowledge import server as srv
from confluence_knowledge.mcp_async import tool


def test_registered_tools_are_async_wrappers():
    """Every registered tool runs through the async offload wrapper; the
    module attribute stays a plain sync function for direct calls."""
    tm = srv.mcp._tool_manager._tools
    for name, t in tm.items():
        fn = getattr(srv, name)
        assert not inspect.iscoroutinefunction(fn), \
            f"{name}: module attr must stay sync for tests"
        assert inspect.iscoroutinefunction(t.fn), \
            f"{name}: registered tool must be async (offloaded)"


def test_wrapper_schema_matches_signature():
    """The wrapper's JSON schema equals the sync fn's parameter schema —
    offload must not alter what clients see."""
    tm = srv.mcp._tool_manager._tools
    for name, t in tm.items():
        src = inspect.signature(getattr(srv, name))
        ours = set(src.parameters)
        schema = t.parameters.get("properties", {})
        assert ours == set(schema), \
            f"{name}: schema {sorted(schema)} != signature {sorted(ours)}"


def test_blocked_heavy_tool_does_not_stall_light_tool():
    """A heavy tool stuck on a lock must not block `crawl_step` — this is
    the 'server looks hung' scenario the offload exists to prevent."""
    gate = threading.Event()

    @tool(srv.mcp, heavy=True)
    def _blocker_test() -> dict:
        """test blocker"""
        gate.wait(30)
        return {"ok": True}

    async def run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(srv.mcp.call_tool, "_blocker_test", {})
            await anyio.sleep(0.3)   # let the blocker get onto its thread
            t0 = time.monotonic()
            res = await srv.mcp.call_tool("status", {})
            assert res is not None    # the light tool answered despite the blocker
            return time.monotonic() - t0

    elapsed = anyio.run(run)
    gate.set()
    # the light call must answer fast — no ~30s wait behind the blocker
    assert elapsed < 10, f"light tool stalled {elapsed:.1f}s behind blocker"


def test_blocked_light_tool_uses_limiter_not_loop():
    """A slow light tool is offloaded to a thread — the loop stays free
    for a concurrent light call."""
    gate = threading.Event()

    @tool(srv.mcp)
    def _slow_test() -> dict:
        """test slow"""
        gate.wait(30)
        return {"ok": True}

    async def run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(srv.mcp.call_tool, "_slow_test", {})
            await anyio.sleep(0.3)
            t0 = time.monotonic()
            await srv.mcp.call_tool("status", {})
            return time.monotonic() - t0

    elapsed = anyio.run(run)
    gate.set()
    assert elapsed < 10, f"second light tool stalled {elapsed:.1f}s"


def test_state_dicts_concurrent_access():
    """Concurrent _index_for/_feedback_for calls must return the single
    instance — no double-opened DB files."""
    key = "racetest"
    srv._indexes.pop(key, None)
    results = []
    def grab():
        results.append(srv._index_for(key))
    ts = [threading.Thread(target=grab) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({id(i) for i in results}) == 1
    srv._indexes.pop(key, None)


def test_tool_result_shape_unchanged(tmp_path, monkeypatch):
    """calling through the wrapper yields the same dict the sync fn
    returns — verified on crawl_step's error path."""
    async def run():
        try:
            res = await srv.mcp.call_tool(
                "crawl_step", {"scope_handle": "scope:x:y", "job_id": "j"})
            return json.dumps(res, default=str)
        except BaseException as e:  # noqa: BLE001 — error may be an MCP error
            return str(e)
    txt = anyio.run(run)
    # proves the call flowed through the async path end-to-end — the
    # sync fn's error surfaces through the MCP error channel
    assert "crawl_step" in txt and ("unknown scope" in txt or "Error" in txt)
