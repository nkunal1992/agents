"""#253: check-then-spawn races — the daemon burst behind the wake-up
freeze. These tests pin the mutex contract, not real processes."""

import os
import threading
import time


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME", str(tmp_path))
    return tmp_path


def test_concurrent_ensure_daemon_spawns_once(tmp_path, monkeypatch):
    """8 queued tool calls at wake → exactly ONE daemon spawn (the rest
    queue on .spawn.lock, then see the heartbeat and return)."""
    _home(tmp_path, monkeypatch)
    from confluence_knowledge import rpc as rpc_mod
    monkeypatch.setattr("confluence_knowledge.settings.daemon_home",
                        lambda: tmp_path)
    state = {"alive": False, "spawns": 0}

    class FakeRpc:
        def daemon_alive(self):
            return state["alive"]

    def fake_spawn(rpc, workdir, env=None):
        state["spawns"] += 1
        time.sleep(0.05)                  # the cold-boot window
        state["alive"] = True

    monkeypatch.setattr(rpc_mod, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr(rpc_mod, "_await_daemon",
                        lambda rpc, workdir: None)
    threads = [threading.Thread(target=rpc_mod.ensure_daemon,
                                args=(FakeRpc(),)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert state["spawns"] == 1


def test_fresh_stop_file_still_spawns(tmp_path, monkeypatch):
    """The lazy-start contract: a NEW tool call is intent to run — the
    watchdog's own re-check before respawn already covers the shutdown
    race, so ensure_daemon must NOT read the stop file (a stop→tool-call
    sequence under 4s used to hang e2e for the full 20s timeout)."""
    _home(tmp_path, monkeypatch)
    from confluence_knowledge import rpc as rpc_mod
    monkeypatch.setattr("confluence_knowledge.settings.daemon_home",
                        lambda: tmp_path)
    state = {"spawns": 0}

    class DeadRpc:
        def daemon_alive(self):
            return False

    monkeypatch.setattr(rpc_mod, "_spawn_daemon",
                        lambda rpc, workdir, env=None:
                        state.__setitem__("spawns", state["spawns"] + 1))
    monkeypatch.setattr(rpc_mod, "_await_daemon",
                        lambda rpc, workdir: None)
    (tmp_path / ".watchdog.stop").write_text("1")
    rpc_mod.ensure_daemon(DeadRpc())
    assert state["spawns"] == 1


def test_watchdog_spawn_dedup(tmp_path, monkeypatch):
    """Racing spawn_watchdog callers land exactly one Popen."""
    _home(tmp_path, monkeypatch)
    from confluence_knowledge import watchdog as wd
    state = {"running": False, "popens": 0}

    class FakeProc:
        pid = os.getpid()

    monkeypatch.setattr(wd, "watchdog_running",
                        lambda home: state["running"])

    def fake_popen(argv, **kw):
        state["popens"] += 1
        time.sleep(0.02)
        state["running"] = True
        return FakeProc()

    monkeypatch.setattr("confluence_knowledge.rpc._popen_detached",
                        fake_popen)
    threads = [threading.Thread(target=wd.spawn_watchdog,
                                args=(tmp_path,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert state["popens"] == 1


def test_stray_reaper_kills_same_home_only(tmp_path, monkeypatch):
    """#253: a booting daemon SIGTERMs sibling daemon/watchdog processes
    bound to ITS workdir — pre-fix leaks; different homes untouched."""
    from confluence_knowledge.daemon import Daemon

    killed = []

    class P:
        def __init__(self, pid, argv):
            self.pid, self._argv = pid, argv

        def cmdline(self):
            return self._argv

        def terminate(self):
            killed.append(self.pid)

    home = str(tmp_path)
    fake = [
        P(101, ["py", "-m", "confluence_knowledge.daemon", home]),
        P(102, ["py", "-m", "confluence_knowledge.watchdog", home]),
        P(103, ["py", "-m", "confluence_knowledge.daemon",
                "/other/home"]),           # different home — legitimate
        P(104, ["py", "-m", "other.module", home]),
    ]
    import sys
    import types
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda *a, **kw: iter(fake),
        NoSuchProcess=Exception, AccessDenied=Exception,
        ZombieProcess=Exception)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    d = Daemon.__new__(Daemon)
    d.workdir = tmp_path
    import os as _os
    me = _os.getpid()
    # exclude self: swap our pid onto proc 101's slot? simpler — own pid
    # won't be in `fake`; add a self-pid entry to prove exclusion
    fake.append(P(me, ["py", "-m", "confluence_knowledge.daemon", home]))
    d._reap_strays()
    assert sorted(killed) == [101, 102], killed


def test_stop_escalates_sigkill(tmp_path, monkeypatch):
    """A daemon that survives SIGTERM + the full wait gets SIGKILL."""
    import signal

    from confluence_knowledge import watchdog as wd

    kills = []
    monkeypatch.setattr(wd, "_daemon_pid_file", lambda h: 5555)
    monkeypatch.setattr(wd, "_watchdog_pid", lambda h: None)
    monkeypatch.setattr(wd, "_cmdline_is", lambda p, n: True)

    class AliveRpc:
        def daemon_pid(self): return 5555
        def daemon_alive(self): return True     # never dies politely

    monkeypatch.setattr(wd, "RpcDb", lambda p: AliveRpc())
    monkeypatch.setattr(wd.os, "kill", lambda p, s: kills.append(s))
    out = wd.stop_daemon(tmp_path, timeout=0.2)
    assert out.get("daemon_killed")
    assert kills[-1] == (signal.SIGKILL
                         if hasattr(signal, "SIGKILL") else wd._TERM)


def test_watchdog_role_blocks_spawn(tmp_path, monkeypatch):
    """KNOWLEDGE_WATCHDOG_ROLE is now a real guard — a watchdog must
    never spawn a sibling."""
    from confluence_knowledge import watchdog as wd
    monkeypatch.setenv("KNOWLEDGE_WATCHDOG_ROLE", "1")
    called = []
    monkeypatch.setattr(wd, "watchdog_running", lambda h: False)
    monkeypatch.setattr(
        "confluence_knowledge.rpc._popen_detached",
        lambda *a, **k: called.append(a))
    wd.spawn_watchdog(tmp_path)
    assert not called
