"""Machine-readable health checks for doctor/status — detection only.

Every check is {flag, ok, detail, remediation}; the agent turns a red flag
into exactly one fix command (see the agent-file remediation table).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from .settings import SetupError

_DEPS = ("onnxruntime", "tokenizers", "fastembed", "huggingface_hub",
         "numpy", "mcp")


def _dep_ok() -> tuple[bool, list[str]]:
    import importlib.util
    missing = [m for m in _DEPS if importlib.util.find_spec(m) is None]
    return not missing, missing


def _models_checks(home: Path, cli: str) -> list[dict]:
    from .retrieval import models_complete
    out = []
    try:
        mc = models_complete(home)
    except SetupError as e:
        return [{"flag": "models_complete", "ok": False, "detail": str(e),
                 "remediation": f"{cli} prepare-models"}]
    for key, spec in mc.items():
        out.append({
            "flag": f"model_{key}", "ok": spec["ok"],
            "detail": ("missing: " + ", ".join(spec["missing"])
                       if spec["missing"] else "present"),
            "remediation": None if spec["ok"] else f"{cli} prepare-models"})
    from .retrieval import active_embed_spec, active_provider
    out.append({"flag": "onnx_provider", "ok": True,
                "detail": active_provider(), "remediation": None})
    out.append({"flag": "embed_model", "ok": True,
                "detail": active_embed_spec()["repo"],
                "remediation": None})
    return out


_SERVER_MARKERS = ("confluence_knowledge.server",
                   "local_knowledge.server_local",
                   "sharepoint_knowledge.server_sp")


def _server_process_count() -> int:
    """How many knowledge MCP servers are running — every IDE window
    spawns its own copy of each installed service (~2 GB each once the
    models load), so a count ≫ 3 explains memory pressure. -1 = unknown.
    psutil only — `wmic` is removed from modern Windows and AppLocker
    blocks it on managed devices; `ps` isn't on Windows at all."""
    try:
        import psutil
        n = 0
        for p in psutil.process_iter(["cmdline"]):
            try:
                cmd = p.cmdline() or []
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
            if any(m in " ".join(cmd) for m in _SERVER_MARKERS):
                n += 1
        return n
    except Exception:  # noqa: BLE001 — detection only, never fatal
        return -1


def _resources_check(cli: str) -> dict:
    from .resources import _phys_cores, _psutil, limits
    lim = limits()
    n = _server_process_count()
    mem = ""
    free_mb = -1
    if _psutil is not None:
        try:
            free_mb = int(_psutil.virtual_memory().available // 1048576)
        except Exception as e:  # noqa: BLE001 — reading memory is best-effort
            mem = f" free_mem_mb=? ({e})"
    if free_mb >= 0:
        mem = (f" free_mem_mb={free_mb}"
               f" min_free_mb={lim.min_free_mb}"
               + (" LOW" if free_mb < lim.min_free_mb else ""))
    detail = (f"threads={lim.threads} batch_tokens={lim.batch_tokens} "
              f"max_batch={lim.max_batch} "
              f"idle_unload_s={lim.idle_unload_s:.0f} "
              f"fetch_concurrency={lim.fetch_concurrency}"
              f" phys_cores={_phys_cores()}"
              f" guard={'on' if _psutil is not None else 'off (psutil missing)'}"
              f" battery_saver={'on' if lim.on_battery() else 'off'}{mem}"
              + (f"; {n} knowledge server process(es) running"
                 if n >= 0 else "; process count unavailable"))
    too_many = n > 4
    low_mem = _psutil is not None and lim.min_free_mb > 0 and \
        not lim.memory_ok()
    ok = not too_many and not low_mem
    return {"flag": "resources", "ok": ok,
            "detail": detail,
            "remediation": (
                "free RAM is under KNOWLEDGE_MIN_FREE_MB — crawls pause "
                "automatically; close apps to free memory" if low_mem else
                (None if not too_many else
                 "each IDE window starts its own knowledge servers — close "
                 "unused windows (idle servers unload their models after "
                 "~10 min anyway)"))}


def _index_checks(index_root: Path) -> list[dict]:
    """Per-scope index: integrity + stale-model marker. Detection only."""
    from .index import Index
    from .retrieval import MODEL_MARKER as EMBED_MODEL_ID
    if not index_root.exists():
        return []
    out = []
    for db in sorted(index_root.glob("*.db")):
        if db.name == "rpc.db":
            continue                    # daemon control plane, not a scope
        scope = db.stem[:24]
        idx = None
        unreadable = False
        try:
            # read-only + quick_check: status must never take a write
            # lock (a WAL publish by the daemon otherwise stalls this
            # call for seconds — #199) and integrity_check is a full
            # scan per call — quick_check catches real corruption
            idx = Index(db, readonly=True)
            ok = idx.quick_integrity()
            stale = idx.model_id() is not None and \
                idx.model_id() != EMBED_MODEL_ID
        except Exception as e:  # noqa: BLE001 — triage the reason
            # "can't read right now" is NOT corruption — a WAL write or
            # Windows file lock fails the open transiently. Reporting
            # corrupt here sent a field user recrawling a healthy index.
            # Structural damage ("not a database", "malformed") IS
            # corrupt regardless of how it surfaced.
            # walk the cause chain — Index() wraps sqlite errors in
            # SetupError("index unreadable…"); the real reason lives in
            # __cause__/__context__
            msgs = [str(e)]
            for linked in (e.__cause__, e.__context__):
                if linked is not None:
                    msgs.append(str(linked))
            msg = " | ".join(msgs).lower()
            structural = ("not a database" in msg or "malformed" in msg
                          or "corrupt" in msg)
            ok, stale = False, False
            unreadable = not structural
            if structural:
                ok = False
        finally:
            if idx is not None:
                idx.close()  # Windows locks the file while a conn is open
        if unreadable:
            detail = "couldn't read — index busy; retry in a moment"
            remediation = None
        elif not ok:
            detail = "corrupt — delete the index and re-crawl"
            remediation = "start_crawl (rebuilds the index)"
        elif stale:
            detail = "built with an older model — run start_crawl once"
            remediation = "start_crawl (rebuilds the index)"
        else:
            detail = "ok"
            remediation = None
        out.append({
            "flag": f"index:{scope}",
            "ok": ok and not stale and not unreadable,
            "detail": detail,
            "remediation": remediation})
    return out


_LOOPBACK_TTL_S = 600.0     # the probe does real socket work — on AV-
_loopback_probe_cache: tuple[float, dict] | None = None  # scanned hosts
# each bind+connect costs ~seconds; status() is a hot path, so the
# result caches process-wide (an EDR verdict can't change mid-session)


def _loopback_listener_check() -> dict:
    """Bind + connect a 127.0.0.1 listener — the daemon serves MCP over
    exactly this; an EDR/WFP callout (CrowdStrike, SentinelOne, AV
    'encrypted web scan') that intercepts loopback turns daemon mode
    into a crash-respawn loop. Cross-platform, cached."""
    global _loopback_probe_cache
    if (_loopback_probe_cache
            and time.monotonic() - _loopback_probe_cache[0]
            < _LOOPBACK_TTL_S):
        return _loopback_probe_cache[1]
    result = _loopback_probe()
    _loopback_probe_cache = (time.monotonic(), result)
    return result


def _loopback_probe() -> dict:
    import socket
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        c = socket.socket()
        c.settimeout(3)
        try:
            c.connect(("127.0.0.1", port))
            return {"flag": "loopback_listener", "ok": True,
                    "detail": "loopback bind+connect works",
                    "remediation": None}
        except OSError as e:
            return {"flag": "loopback_listener", "ok": False,
                    "detail": (f"bound 127.0.0.1:{port} but connect "
                               f"failed: {e} — an EDR/WFP filter is "
                               "intercepting loopback"),
                    "remediation": "ask IT for a loopback exclusion for "
                                   "python (127.0.0.1 inbound+outbound)"}
        finally:
            c.close()
    except OSError as e:
        return {"flag": "loopback_listener", "ok": False,
                "detail": f"cannot bind a loopback listener: {e}",
                "remediation": "ask IT for a loopback exclusion for "
                               "python; the daemon serves MCP over "
                               "127.0.0.1"}
    finally:
        srv.close()


def _managed_windows_checks(home: Path, workdir: Path) -> list[dict]:
    """Managed-laptop detection surface (#260): restrictions a domain-
    managed Windows machine imposes that our code can't fix — report
    them so a red flag names the real cause. Informational `ok=True`
    items only warn; hard blockers go `ok=False`."""
    if sys.platform != "win32":     # sys.platform, not os.name — tests
        return []                   # fake it; pathlib reads os.name
    out = []

    try:
        import winreg
    except ImportError:
        winreg = None              # tests fake sys.platform off-Windows

    def _reg_or_none(root, path, name):
        if winreg is None:
            return None
        try:
            return winreg.QueryValueEx(
                winreg.OpenKey(root, path), name)[0]
        except Exception:                       # noqa: BLE001 — absent
            return None                         # or unreadable

    # D1: proxy set — loopback is bypassed by _local_urlopen regardless,
    # but external calls NEED the proxy; flag when localhost isn't
    # excluded so a corp-proxy failure reads correctly
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        no_proxy = (os.environ.get("NO_PROXY")
                    or os.environ.get("no_proxy") or "")
        loopback_excluded = any(
            h in no_proxy for h in ("127.0.0.1", "localhost", "<local>"))
        out.append({"flag": "proxy_loopback", "ok": True,
                    "detail": (
                        f"corporate proxy set; NO_PROXY "
                        f"{'covers' if loopback_excluded else 'does NOT cover'} "
                        "127.0.0.1 — daemon loopback is bypassed "
                        "internally, external calls keep the proxy"),
                    "remediation": None})

    # D2: UNC/network-drive workdir — SQLite WAL over SMB corrupts/stalls
    unc = str(workdir).startswith(("\\\\", "//"))
    try:
        import ctypes
        drive = workdir.drive
        remote = bool(drive) and ctypes.windll.kernel32.GetDriveTypeW(
            drive + "\\") == 4          # DRIVE_REMOTE
    except Exception:                   # noqa: BLE001 — probe best-effort
        remote = False
    if unc or remote:
        out.append({"flag": "workdir_network", "ok": False,
                    "detail": (f"workdir is on a network path "
                               f"({workdir}) — SQLite WAL over SMB "
                               "corrupts/stalls the index"),
                    "remediation": "point the workdir env var at a local "
                                   "NTFS folder and re-crawl"})

    # D3: LongPathsEnabled — deep trees throw OSError storms in the
    # local crawler when the OS gate is off
    try:
        import winreg
        v = winreg.QueryValueEx(winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem"),
            "LongPathsEnabled")[0]
        if not v:
            out.append({"flag": "long_paths", "ok": True,
                        "detail": "LongPathsEnabled=0 — files deeper "
                                  "than 260 chars are skipped on errors",
                        "remediation": None})
    except Exception:                   # noqa: BLE001,S110 — winreg is
        pass                            # nt-only; unreadable → skip

    # D4: Microsoft Store python — packaged interpreter virtualizes
    # writes under some profile dirs
    try:
        if "WindowsApps" in str(getattr(sys, "base_prefix", "")):
            out.append({"flag": "store_python", "ok": True,
                        "detail": "Microsoft Store python — profile-dir "
                                  "writes may virtualize and packaged "
                                  "apps run under AppContainer network "
                                  "isolation (loopback needs an "
                                  "exemption); prefer a python.org "
                                  "install if paths/network misbehave",
                        "remediation": None})
    except Exception:                   # noqa: BLE001,S110 — probe
        pass                            # only, nothing to log

    # D5: Defender real-time scanning of the model/index dirs slows
    # first-boot + crawls — suggest an exclusion (needs admin, so it's
    # informational, never a flag)
    out.append({"flag": "av_exclusion", "ok": True,
                "detail": "real-time AV scanning the index/models dir "
                          "slows crawls on managed devices",
                "remediation": "ask IT to exclude "
                               f"{home} from Defender real-time scans "
                               "if crawls are slow"})

    hklm = getattr(winreg, "HKEY_LOCAL_MACHINE", None)
    hkcu = getattr(winreg, "HKEY_CURRENT_USER", None)

    # D6: Controlled Folder Access — read the real enforcement state,
    # then check whether our state dirs sit under a protected folder
    cfa = _reg_or_none(hklm,
        r"SOFTWARE\Microsoft\Windows Defender\Windows Defender "
        r"Exploit Guard\Controlled Folder Access",
        "EnableControlledFolderAccess")
    prof = Path.home().resolve()
    protected = {prof / d for d in ("Documents", "Desktop", "Pictures")}
    try:
        hit = next((d for d in protected if workdir.resolve()
                    .is_relative_to(d) or home.resolve()
                    .is_relative_to(d)), None)
    except OSError:
        hit = None
    if cfa == 1 and hit:    # block mode + state under a protected dir
        out.append({"flag": "controlled_folder_access", "ok": False,
                    "detail": (f"Controlled Folder Access is ON and "
                               f"state lives under {hit} — writes by "
                               "unapproved apps are denied outright"),
                    "remediation": "allowlist python in CFA or move "
                                   "workdir/home outside "
                                   "Documents/Desktop/Pictures"})
    elif hit:
        out.append({"flag": "protected_folder", "ok": True,
                    "detail": (f"state under {hit} — if Controlled "
                               "Folder Access is enabled it blocks "
                               "writes by unapproved apps"),
                    "remediation": "move the workdir/home outside "
                                   "Documents/Desktop or allowlist python"})

    # D7: PAC/WPAD-only proxy — the browser works but getproxies() is
    # empty, so external calls (PyPI, HF, Confluence) fail while web
    # browsing looks fine
    inet = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    pac = _reg_or_none(hkcu, inet, "AutoConfigURL")
    detect = _reg_or_none(hkcu, inet, "AutoDetect")
    if pac or detect:
        out.append({"flag": "pac_proxy", "ok": True,
                    "detail": (f"proxy via PAC/WPAD "
                               f"(AutoConfigURL={pac}, AutoDetect="
                               f"{detect}) — python can't evaluate PAC; "
                               "external calls need HTTP(S)_PROXY set"),
                    "remediation": "set HTTPS_PROXY to the explicit "
                                   "proxy host:port from IT"})

    # D8: WDAC/AppLocker CI policy deployed — unsigned .pyd/.dll
    # (onnxruntime, tokenizers, sqlite-vec) fail to load with WinError
    # 4551; reinstall can't fix, only an IT allowlist can
    if winreg is not None:
        try:
            k = winreg.OpenKey(hklm,
                               r"SYSTEM\CurrentControlSet\Control\CI\Policy")
            if winreg.QueryInfoKey(k)[0] > 0:
                out.append({"flag": "app_control_policy", "ok": True,
                            "detail": "CI policy blobs deployed — "
                                      "unsigned native extensions may "
                                      "be blocked (WinError 4551 on "
                                      "model load)",
                            "remediation": None})
        except Exception:               # noqa: BLE001,S110 — probe
            pass                        # best-effort, unreadable → skip

    # D9: temporary/mandatory profile — state wiped on logout means a
    # full re-crawl every login
    if any(t in prof.name.upper() for t in ("TEMP", ".BAK", "MANDATORY")):
        out.append({"flag": "temp_profile", "ok": False,
                    "detail": (f"profile {prof} looks temporary — its "
                               "contents are discarded on logout, taking "
                               "the index with it"),
                    "remediation": "point the workdir env var at a "
                                   "persistent local dir"})

    # D10: running inside a Job object — KILL_ON_JOB_CLOSE kills every
    # child when the IDE closes; spawn already requests BREAKAWAY but a
    # hardened job can deny it
    try:
        import ctypes
        in_job = ctypes.c_int(0)
        ctypes.windll.kernel32.IsProcessInJob(
            ctypes.windll.kernel32.GetCurrentProcess(), None,
            ctypes.byref(in_job))
        if in_job.value:
            out.append({"flag": "in_job_object", "ok": True,
                        "detail": "running inside a Windows Job — if "
                                  "it's KILL_ON_JOB_CLOSE without "
                                  "breakaway, the daemon dies with the "
                                  "IDE (spawns request BREAKAWAY)",
                        "remediation": None})
    except Exception:                   # noqa: BLE001,S110 — probe
        pass                            # only, ctypes absent → skip

    return out


def health_checks(home: Path, workdir: Path, server_id: str, cli: str,
                  index_root: Path | None = None) -> list[dict]:
    """All non-network checks — the machine-readable half of `status()`."""
    checks = []

    ok, missing = _dep_ok()
    # repair = reinstall the INSTALLED version — never -U (a repair must
    # not silently upgrade into an unverified release)
    inst = None
    if not ok:
        try:
            from importlib.metadata import version as _v
            dist = cli.replace("_", "-")
            inst = _v(dist)
        except Exception:  # noqa: BLE001 — can't pin what isn't installed
            inst = None
    checks.append({"flag": "deps", "ok": ok,
                   "detail": "missing: " + ", ".join(missing) if missing
                             else "all importable",
                   "remediation": None if ok else (
                       f'pip install "{cli}[models]=={inst}"' if inst
                       else f"pip install {cli}[models]")})

    checks += _models_checks(home, cli)

    try:
        from .registration import registered_paths
        regs = registered_paths(server_id=server_id, workspace=workdir)
        ok = bool(regs)
        checks.append({"flag": "registration", "ok": ok,
                       "detail": (", ".join(str(p) for p in regs) if regs
                                  else "no IDE config contains the server"),
                       "remediation": None if ok
                                    else f"{cli} register --ide auto"})
    except Exception as e:  # noqa: BLE001
        checks.append({"flag": "registration", "ok": False,
                       "detail": str(e), "remediation": f"{cli} register"})

    if index_root is not None:
        checks += _index_checks(index_root)

    anchor = home
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        free = shutil.disk_usage(anchor).free // (1024 * 1024)
    except OSError:
        free = -1  # can't measure — don't flag
    checks.append({"flag": "disk_free", "ok": free < 0 or free >= 2000,
                   "detail": f"{free} MB free",
                   "remediation": None if free >= 2000
                                else "free ~2 GB for the model download"})

    checks.append({"flag": "python", "ok": sys.version_info >= (3, 11),
                   "detail": sys.version.split()[0],
                   "remediation": None if sys.version_info >= (3, 11)
                                else "reinstall with Python >=3.11"})

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    checks.append({"flag": "proxy", "ok": True,
                   "detail": proxy or "none set",
                   "remediation": None})
    checks.append(_loopback_listener_check())
    checks += _managed_windows_checks(home, workdir)

    checks.append(_resources_check(cli))
    return checks
