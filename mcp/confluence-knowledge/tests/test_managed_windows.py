"""#260 managed-laptop simulation suite — every restriction a domain-
managed Windows machine can impose, reproduced without one. These run
on EVERY CI leg: the simulations are monkeypatch/env-driven, so ubuntu
and macos legs guard the class too. Real-attribute cases (winreg,
st_file_attributes) additionally run on windows-latest.

The reference incident: #259 — os.kill(pid,0) is CTRL_C_EVENT on
Windows, threw on a detached watchdog, watchdog_running lied False,
+1 watchdog per daemon kick → 25-30 python processes on one install.
Reproduce the restriction first, then assert the fix holds — the same
protocol used on the QA VM.
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def _dead_proxy(monkeypatch):
    """A corporate proxy that answers nothing — env-only, like a
    managed laptop's system proxy. urllib.getproxies() reads env on
    every platform, so the simulation is honest everywhere."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.fixture
def _loopback_server():
    srv = HTTPServer(("127.0.0.1", 0), type(
        "H", (BaseHTTPRequestHandler,),
        {"do_GET": lambda s: (s.send_response(200), s.end_headers(),
                              s.wfile.write(b"{}")),
         "log_message": lambda *a: None}))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_port
    srv.shutdown()


def test_loopback_calls_bypass_proxy(_dead_proxy, _loopback_server,
                                     monkeypatch):
    """F1: a corporate proxy hijacking loopback self-ejects the daemon
    (_probe_http fails 2× → return 5). _local_urlopen must ignore the
    proxy even when the environment shouts it. POSIX urlopen bypasses
    loopback for env proxies natively — proxy_bypass→False reproduces
    the Windows-registry case (ProxyOverride not exempting 127.0.0.1)."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda h: False)
    from confluence_knowledge.rpc import _local_urlopen
    req = urllib.request.Request(f"http://127.0.0.1:{_loopback_server}/_services")
    with _local_urlopen(req, timeout=5) as r:
        assert r.status == 200
    # and prove the simulation itself was hostile — a default-style
    # opener built now really does die through the dead proxy (fresh
    # build_opener: urlopen's module-level _opener is cached on first
    # use and earlier tests built it proxy-free)
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.build_opener().open(req, timeout=3)


def test_restart_daemon_wait_never_kill0(tmp_path, monkeypatch):
    """F2: the post-SIGTERM wait in _restart_daemon probes via psutil —
    under a denied probe it must exit fast, not spin to the 8s
    deadline. (Our own code never issues os.kill(0); psutil.pid_exists
    uses it internally on POSIX — out of scope, harmless here since
    _cmdline_is rejects our pid anyway → break on first check.)"""
    import time
    monkeypatch.setenv("KNOWLEDGE_DAEMON_HOME", str(tmp_path))
    from confluence_knowledge import registration, rpc
    rpc.RpcDb(tmp_path / "rpc.db").db.execute(
        "INSERT INTO daemon_lease VALUES ('__daemon__','test',?,?)",
        (os.getpid(), 0)).connection.commit()
    kills = []

    def spy(pid, sig, *a, **k):
        if sig == 0:
            raise PermissionError("denied")
        kills.append(sig)

    monkeypatch.setattr(os, "kill", spy)
    t0 = time.monotonic()
    registration._restart_daemon()
    assert time.monotonic() - t0 < 4   # psutil path → immediate break
    assert kills                        # SIGTERM was sent first


def test_server_process_count_via_psutil(monkeypatch):
    """F5: wmic is gone on managed Windows — the count comes from
    psutil only. Simulated procs exercise the marker filter."""
    from confluence_knowledge import health
    fake = [SimpleNamespace(cmdline=lambda: ["py", "-m",
                                             "confluence_knowledge.server"]),
            SimpleNamespace(cmdline=lambda: ["py", "-m",
                                             "local_knowledge.server_local"]),
            SimpleNamespace(cmdline=lambda: ["py", "-m", "other.app"])]
    monkeypatch.setattr("psutil.process_iter",
                        lambda *a, **k: iter(fake))
    monkeypatch.setattr(health, "_proc_count_cache", None)
    assert health._server_process_count() == 2


def test_state_files_roundtrip_utf8(tmp_path):
    """F8: cp1252-default Windows used to mangle non-ASCII fields in
    JSON state files — every state write/read is explicit utf-8."""
    from confluence_knowledge.settings import SiteProfile
    prof = SiteProfile("datacenter", "https://conf.corp.example/wiki",
                       "josé.müller@corp.example", "ref")
    prof.save(tmp_path)
    assert SiteProfile.load(tmp_path).email == "josé.müller@corp.example"


def test_managed_windows_checks_flags(monkeypatch, tmp_path):
    """D1/D4/D6: doctor reports the managed restrictions — proxy config,
    Store python, protected folder — as flags, not silent behaviour."""
    from confluence_knowledge import health
    monkeypatch.setattr(health.sys, "platform", "win32")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(sys, "base_prefix",
                        r"C:\Program Files\WindowsApps\Python312")
    workdir = tmp_path / "Documents" / "wk"
    checks = health._managed_windows_checks(tmp_path, workdir)
    flags = {c["flag"] for c in checks}
    assert "proxy_loopback" in flags
    assert "store_python" in flags
    assert "av_exclusion" in flags
    assert "workdir_network" not in flags      # local path — no flag
    assert all(c["ok"] for c in checks)


def test_managed_windows_checks_unc_workdir(tmp_path, monkeypatch):
    """D2: a UNC workdir is a hard flag — SQLite WAL over SMB corrupts."""
    from confluence_knowledge import health
    monkeypatch.setattr(health.sys, "platform", "win32")
    checks = health._managed_windows_checks(tmp_path,
                                            Path("//fileserver/share/work"))
    net = next(c for c in checks if c["flag"] == "workdir_network")
    assert net["ok"] is False


def test_managed_windows_checks_long_paths(monkeypatch, tmp_path):
    """D3: LongPathsEnabled=0 produces an informational flag."""
    from confluence_knowledge import health
    monkeypatch.setattr(health.sys, "platform", "win32")
    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda *a, **k: object(),
        QueryValueEx=lambda *a, **k: (0, 4))
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    checks = health._managed_windows_checks(tmp_path, tmp_path)
    assert any(c["flag"] == "long_paths" for c in checks)


def test_os_trust_context_survives_truststore_injection():
    """pip-system-certs' .pth injects truststore into ssl —
    create_default_context() then returns a ctx whose get_ca_certs()
    raises NotImplementedError. The fallback probe must not trip it."""
    import ssl

    from confluence_knowledge.client import _os_trust_context
    real = ssl.create_default_context

    class FakeTruststoreCtx:
        def get_ca_certs(self):
            raise NotImplementedError()

    try:
        ssl.create_default_context = lambda **k: FakeTruststoreCtx()
        ctx = _os_trust_context()
        assert isinstance(ctx, FakeTruststoreCtx)  # used as-is, no crash
    finally:
        ssl.create_default_context = real
    assert isinstance(_os_trust_context(), ssl.SSLContext)


def _fake_winreg(values: dict, subkeys: int = 0):
    """winreg stub: QueryValueEx looks up (path-suffix, name) in values;
    QueryInfoKey reports `subkeys` CI policy blobs."""
    class _Key:
        def __init__(self, path):
            self.path = str(path)

    def open_key(root, path):
        return _Key(path)

    def query(root_key, name):
        for (suffix, n), v in values.items():
            if root_key.path.endswith(suffix) and n == name:
                return (v, 4)
        raise OSError("not found")

    return SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(), HKEY_CURRENT_USER=object(),
        OpenKey=open_key, QueryValueEx=lambda k, n: query(k, n),
        QueryInfoKey=lambda k: (subkeys, 0, 0))


def test_cfa_block_mode_plus_protected_dir_fails(monkeypatch, tmp_path):
    """D6: CFA enforcement=1 (block) + state under Documents → hard flag."""
    from confluence_knowledge import health
    docs = tmp_path / "Documents"; docs.mkdir()
    fake = _fake_winreg(
        {("Controlled Folder Access", "EnableControlledFolderAccess"): 1})
    monkeypatch.setattr(health.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(health.Path, "home", classmethod(lambda c: tmp_path))
    checks = health._managed_windows_checks(tmp_path, docs)
    cfa = next(c for c in checks if c["flag"] == "controlled_folder_access")
    assert cfa["ok"] is False


def test_pac_proxy_flagged(monkeypatch, tmp_path):
    """D7: AutoConfigURL means browser works but python sees no proxy."""
    from confluence_knowledge import health
    fake = _fake_winreg(
        {("Internet Settings", "AutoConfigURL"): "http://wpad/proxy.pac"})
    monkeypatch.setattr(health.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    checks = health._managed_windows_checks(tmp_path, tmp_path)
    assert any(c["flag"] == "pac_proxy" for c in checks)


def test_app_control_policy_blobs_flagged(monkeypatch, tmp_path):
    """D8: deployed CI policy → warn that unsigned DLLs may be blocked."""
    from confluence_knowledge import health
    fake = _fake_winreg({}, subkeys=3)
    monkeypatch.setattr(health.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    checks = health._managed_windows_checks(tmp_path, tmp_path)
    assert any(c["flag"] == "app_control_policy" for c in checks)


def test_app_control_block_detection():
    from confluence_knowledge.settings import is_app_control_block
    e = OSError("DLL load failed: An Application Control policy has "
                "blocked this file")
    e.winerror = 4551
    assert is_app_control_block(e)
    assert is_app_control_block(
        ImportError("blocked by a policy on the system"))
    assert not is_app_control_block(ImportError("No module named 'x'"))


def test_loopback_listener_probe():
    from confluence_knowledge import health
    c = health._loopback_listener_check()
    assert c["flag"] == "loopback_listener" and c["ok"] is True
