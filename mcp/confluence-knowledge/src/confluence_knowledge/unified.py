"""Unified `knowledge` MCP entry — one registration unions every
installed service (#265).

A single `knowledge` server whose tools/list is the LIVE union of all
installed knowledge services, namespaced `<stem>__<tool>` —
`confluence__search`, `local__connect`, `sharepoint__status`. Discovery
runs per list (entry points + invalidated import caches), so a service
pip-installed after this server started appears on the next tools/list:
register once, never again. A background watcher also emits
tools/list_changed for clients that re-list on the notification.

tools/call routes on the `__` prefix — the named service module's own
FastMCP handles the call exactly as if asked directly, so proxy/daemon
hops, scoping and security checks are identical to per-service entries.

Served as `python -m confluence_knowledge.unified` (stdio) and mounted
by the daemon at `/knowledge/mcp` (http). Register it with
`<cli> register --unified` — one entry covers every service, present
and future.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import logging
import os
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

log = logging.getLogger(__name__)

_SUFFIX = "-knowledge"
_SEP = "__"


def _stem(service: str) -> str:
    """Tool-name prefix for a mount name — 'confluence-knowledge' →
    'confluence'. Charset stays inside [a-z0-9_-] for strict clients."""
    return service.removesuffix(_SUFFIX).replace("-", "_")


def _poll_interval() -> float:
    try:
        return float(os.environ.get("KNOWLEDGE_UNIFIED_POLL_S", "15"))
    except ValueError:
        return 15.0


class UnifiedMCP(FastMCP):
    """FastMCP whose tools are the union of every installed service —
    live, namespaced, and announcing changes via tools/list_changed."""

    def __init__(self, poll_s: float | None = None):
        super().__init__("knowledge")
        self._session = None
        self._last_services: dict[str, object] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll_s = _poll_interval() if poll_s is None else poll_s
        self._watch_started = False

    # ------------------------------------------------------------------
    def _services(self) -> dict[str, object]:
        """Live discovery — a post-start pip install must be importable,
        so path-importer caches are invalidated before each scan.

        Only the request path may call this: service modules pull heavy
        native extensions (numpy's OpenBLAS inits inside DllMain) and two
        threads importing concurrently can wedge in the Windows loader
        lock — the watcher therefore compares entry-point names, never
        imports.

        A scan hiccup (e.g. a sys.path entry whose finder raises on
        invalidate) must never error a tools/list — fall back to the
        last-known set so the union stays up."""
        from .daemon import discover_services  # lazy — daemon imports us
        try:
            importlib.invalidate_caches()
            found = discover_services()
        except Exception:
            log.warning("service scan failed — serving last-known set",
                        exc_info=True)        # must never blank the union
            return self._last_services
        self._last_services = found
        return found

    def _installed_names(self) -> set[str]:
        """Entry-point names only — metadata scan, zero imports. Safe for
        the watcher thread to run while a request is mid-import."""
        importlib.invalidate_caches()
        try:
            return {ep.name for ep in importlib.metadata.entry_points(
                group="knowledge.services")}
        except Exception:                      # noqa: BLE001 — a scan
            return set()                        # miss means no change

    def _capture_session(self) -> None:
        """Stash the serving session+loop — the watcher thread needs both
        to push notifications; request_context only exists mid-request."""
        try:
            self._session = self._mcp_server.request_context.session
            self._loop = asyncio.get_running_loop()
        except (LookupError, AttributeError):
            pass                          # no active request context

    def _notify_tools_changed(self) -> None:
        sess, loop = self._session, self._loop
        if sess is None or loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                sess.send_tool_list_changed(), loop)
        except Exception:                   # noqa: BLE001 — a dead SSE
            self._session = None            # session just resets the hint

    def _watch_once(self, seen: set[str]) -> set[str]:
        """One watcher poll — notify on change, return the new set."""
        now = self._installed_names()
        if now != seen:
            log.info("knowledge: services changed +%s -%s",
                     sorted(now - seen), sorted(seen - now))
            self._notify_tools_changed()
        return now

    def _watcher(self) -> None:
        """Poll the installed-service set — tools/list_changed lets a
        client pick up a new service without an IDE reload. Best-effort:
        clients that ignore the notification still see the union on the
        next tools/list (new chat, reconnect)."""
        seen = self._installed_names()
        while True:
            time.sleep(self._poll_s)
            try:
                seen = self._watch_once(seen)
            except Exception:               # noqa: BLE001,S112 —
                continue                    # watcher must never die

    def _maybe_start_watcher(self) -> None:
        if self._watch_started or self._poll_s <= 0:
            return
        self._watch_started = True
        threading.Thread(target=self._watcher, daemon=True,
                         name="knowledge-watch").start()

    # ------------------------------------------------------------------
    async def list_tools(self):
        self._capture_session()
        self._maybe_start_watcher()
        out = []
        services = self._services()
        for name in sorted(services):
            mod = services[name]
            try:
                tools = await mod.mcp.list_tools()
            except Exception:
                log.warning("service %r list_tools failed", name,
                            exc_info=True)  # service must not hide the rest
                continue
            stem = _stem(name)
            for t in tools:
                out.append(t.model_copy(
                    update={"name": f"{stem}{_SEP}{t.name}"}))
        return out

    async def call_tool(self, name: str, arguments: dict):
        stem, sep, tool_name = name.partition(_SEP)
        if not sep or not tool_name:
            raise ToolError(
                f"unknown tool {name!r} — unified tools are named "
                "<service>__<tool>, e.g. confluence__search")
        services = self._services()
        stems: dict[str, str] = {}
        for svc_name in services:
            s = _stem(svc_name)
            if s in stems:
                log.warning("stem collision: %r and %r both map to %r",
                            stems[s], svc_name, s)
            stems[s] = svc_name
        target = stems.get(stem)
        if target is None:
            raise ToolError(
                f"no service for prefix {stem!r} — installed: "
                f"{sorted(stems) or 'none'}")
        return await services[target].mcp.call_tool(tool_name, arguments)


mcp = UnifiedMCP()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s knowledge %(message)s")
    # Warm discovery BEFORE the event loop starts: service modules pull
    # native extensions whose DllMain can deadlock when a load races an
    # anyio worker's own DLL load (Windows loader lock vs GIL). The
    # daemon imports services the same way — one thread, at boot.
    mcp._services()
    mcp.run()                               # stdio — one entry, all services


if __name__ == "__main__":
    main()
