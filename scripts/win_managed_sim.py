#!/usr/bin/env python3
"""Managed-laptop self-diagnostic (#260).

Run on ANY machine (designed for corporate Windows laptops):
    python win_managed_sim.py

Probes which managed restrictions the machine actually enforces and
prints a profile. Read-only: spawns one short-lived child, opens no
files outside temp, makes no network calls (the dead-proxy check is
a local socket, not a real proxy connection).
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def check(name):
    def deco(fn):
        def run():
            try:
                return fn()
            except Exception as e:                      # noqa: BLE001
                return f"PROBE FAILED: {e}"
        run.__name__ = name
        return run
    return deco


@check("os.kill(pid,0) on a detached child")
def kill_zero_detached():
    """Does the 'liveness probe' survive — or throw, or kill the child?
    Windows CTRL_C_EVENT, not a POSIX probe (#259)."""
    kw = {"creationflags": 0x00000008 | 0x08000000 | 0x01000000,
          "close_fds": True} if os.name == "nt" else {
        "start_new_session": True}
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **kw)
    try:
        time.sleep(1)
        try:
            os.kill(p.pid, 0)
        except Exception as e:                          # noqa: BLE001
            return f"THREW {type(e).__name__}: {e} — probes denied here"
        alive = p.poll() is None
        return ("no-op (live child survived) — POSIX-style semantics"
                if alive else
                "KILLED the child — os.kill(0) is destructive here")
    finally:
        with contextlib.suppress(Exception):
            p.kill()


@check("proxy configuration")
def proxy_env():
    import urllib.request
    px = urllib.request.getproxies()
    if not px:
        return "none set — direct connections"
    noproxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    covered = any(h in noproxy for h in ("127.0.0.1", "localhost", "<local>"))
    return (f"{px}; NO_PROXY {'covers' if covered else 'does NOT cover'} "
            f"127.0.0.1{' — corp proxy would hijack loopback without _local_urlopen' if not covered else ''}")


@check("loopback TCP connect")
def loopback():
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 1))                     # refused = fine
        return "connectable (unexpected)"
    except ConnectionRefusedError:
        return "reachable (refused as expected) — loopback works"
    except Exception as e:                              # noqa: BLE001
        return f"BLOCKED: {e} — an EDR/firewall may deny loopback"


@check("wmic presence")
def wmic():
    import shutil
    w = shutil.which("wmic")
    return f"{w or 'ABSENT (removed/blocked — do not shell out to it)'}"


@check("git presence")
def git():
    import shutil
    return f"{shutil.which('git') or 'absent — git-optional paths must degrade'}"


@check("LongPathsEnabled")
def long_paths():
    if os.name != "nt":
        return "n/a (not Windows)"
    try:
        import winreg
        v = winreg.QueryValueEx(winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem"),
            "LongPathsEnabled")[0]
        return str(v) + (" — deep trees OK" if v else
                         " — >260-char paths will OSError")
    except OSError as e:
        return f"unreadable ({e}) — assume disabled"


@check("Store-Python virtualization")
def store_python():
    bp = str(getattr(sys, "base_prefix", sys.executable))
    return ("Microsoft Store python (WindowsApps) — profile-dir writes may "
            "virtualize" if "WindowsApps" in bp else f"regular install: {bp}")


@check("home/workdir locality")
def locality():
    home = Path.home()
    unc = str(home).startswith(("\\\\", "//"))
    try:
        import ctypes
        drive = home.drive
        remote = bool(drive) and ctypes.windll.kernel32.GetDriveTypeW(
            drive + "\\") == 4
    except Exception:                                   # noqa: BLE001
        remote = False
    tag = "NETWORK — SQLite WAL over SMB will corrupt/stall" if (
        unc or remote) else "local"
    return f"{home} — {tag}"


@check("workdir write probe")
def writable():
    d = Path(tempfile.mkdtemp(prefix="knowledge_probe_"))
    try:
        (d / "w").write_text("1", encoding="utf-8")
        (d / "w").unlink()
        return f"writable ({d})"
    except OSError as e:
        return f"WRITE BLOCKED: {e} — Controlled Folder Access may be on"


@check("process listing visibility")
def proc_view():
    try:
        import psutil
        n, denied = 0, 0
        for p in psutil.process_iter(["cmdline"]):
            n += 1
            try:
                p.cmdline()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                denied += 1
        return f"{n} processes visible, {denied} access-denied" + (
            " — cmdline reads restricted (EDR)" if denied > n // 3 else "")
    except ImportError:
        return "psutil absent"


@check("WDAC/AppLocker code-integrity policy")
def wdac():
    r"""Unsigned .pyd/.dll (onnxruntime, tokenizers, sqlite-vec) die with
    WinError 4551 under an enforced policy. Registry CI\Policy presence
    is the cheap signal; the functional proof is an ImportError naming
    'Application Control policy'."""
    if os.name != "nt":
        return "n/a (not Windows)"
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\CI\Policy")
        n = winreg.QueryInfoKey(k)[0]
        return (f"{n} CI policy blob(s) deployed — unsigned native "
                "extensions may be blocked (verify via an import error "
                "naming 'Application Control policy')" if n else
                "no CI policy blobs — unlikely to block unsigned DLLs")
    except OSError as e:
        return f"unreadable ({e})"


@check("job-object breakaway (daemon survives IDE)")
def job_breakaway():
    """IDEs run children inside KILL_ON_JOB_CLOSE jobs — a detached
    daemon still dies with the IDE unless CREATE_BREAKAWAY_FROM_JOB is
    honoured; hardened jobs can deny it (WinError 5)."""
    if os.name != "nt":
        return "n/a (not Windows)"
    import ctypes
    in_job = ctypes.c_int(0)
    ctypes.windll.kernel32.IsProcessInJob(
        ctypes.windll.kernel32.GetCurrentProcess(), None,
        ctypes.byref(in_job))
    try:
        p = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            creationflags=0x00000008 | 0x08000000 | 0x01000000)
        p.wait(timeout=10)
        brk = "breakaway OK"
    except OSError as e:
        brk = (f"BREAKAWAY DENIED (winerror {e.winerror}) — daemons "
               "spawned from a job-managed shell die with the parent")
    return f"in-job={bool(in_job.value)}; {brk}"


@check("PAC/WPAD-only proxy (urllib can't evaluate)")
def pac_proxy():
    """AutoConfigURL/AutoDetect mean the browser works but python's
    getproxies() sees NOTHING — external calls fail while browsing
    looks fine."""
    if os.name != "nt":
        return "n/a (not Windows)"
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        pac = winreg.QueryValueEx(k, "AutoConfigURL")[0]
        detect = winreg.QueryValueEx(k, "AutoDetect")[0]
        enabled = winreg.QueryValueEx(k, "ProxyEnable")[0]
        if pac or detect:
            return (f"PAC/WPAD in use (AutoConfigURL={pac!r}, "
                    f"AutoDetect={detect}) — python can't evaluate it; "
                    "external calls need an explicit HTTP(S)_PROXY")
        return f"no PAC (ProxyEnable={enabled})"
    except OSError as e:
        return f"unreadable ({e})"


@check("loopback listener (EDR/WFP interception)")
def loopback_listener():
    """A blocked CONNECT to a dead port is normal; a blocked LISTEN or a
    dead connect to a live listener means the daemon's 127.0.0.1 service
    will be intercepted by security tooling."""
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        c = socket.socket()
        c.settimeout(3)
        try:
            c.connect(("127.0.0.1", port))
            return "bind+connect OK — loopback listeners work"
        except Exception as e:                          # noqa: BLE001
            return (f"LISTENER UNREACHABLE: {e} — an EDR/WFP callout is "
                    "intercepting loopback; daemon mode will fail")
        finally:
            c.close()
    except Exception as e:                              # noqa: BLE001
        return f"BIND BLOCKED: {e} — daemon cannot serve loopback"
    finally:
        srv.close()


@check("Controlled Folder Access (registry)")
def cfa():
    if os.name != "nt":
        return "n/a (not Windows)"
    try:
        import winreg
        v = winreg.QueryValueEx(winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows Defender\Windows Defender "
            r"Exploit Guard\Controlled Folder Access"),
            "EnableControlledFolderAccess")[0]
        return {0: "off", 1: "BLOCK MODE — unapproved apps can't write "
                            "to Documents/Desktop/Pictures",
                2: "audit mode"}.get(v, f"unknown ({v})")
    except OSError as e:
        return f"unreadable/off ({e})"


@check("DNS filtering probe (huggingface/pypi)")
def dns_filter():
    out = []
    for host in ("huggingface.co", "pypi.org"):
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET)
            out.append(f"{host}→{infos[0][4][0]}")
        except socket.gaierror:
            out.append(f"{host}=NXDOMAIN/filtered")
    return "; ".join(out) + (" — a sinkholed domain means model "
                             "downloads can't work"
                             if "filtered" in " ".join(out) else "")


@check("temporary/mandatory profile")
def temp_profile():
    home = Path.home()
    suspicious = any(t in home.name.upper()
                     for t in ("TEMP", ".BAK", "MANDATORY"))
    state = ("TEMPORARY — state is lost on logout; the index re-crawls "
             "every login" if suspicious else "persistent")
    return f"{home} — {state}"


def main():
    print("managed-laptop restriction profile\n" + "=" * 40)
    for fn in (kill_zero_detached, proxy_env, loopback, loopback_listener,
               wmic, git, long_paths, store_python, locality, writable,
               proc_view, wdac, job_breakaway, pac_proxy, cfa,
               dns_filter, temp_profile):
        try:
            print(f"{fn.__name__:38.38} {fn()}")
        except Exception as e:                          # noqa: BLE001
            print(f"{fn.__name__:38.38} ERROR: {e}")


if __name__ == "__main__":
    main()
