# Agent instructions — agents repo

## Managed-laptop checklist (mandatory for process/fs/network code)

Managed Windows laptops (domain policy, AppLocker/WDAC, corporate
proxy + TLS inspection, OneDrive Known Folder Move, removed system
tools, restricted `OpenProcess`) make code that is correct on dev
machines **lie**. Two shipped incidents: #259 (`os.kill(pid,0)` watchdog
pile-up) and #260 (hardening epic). Any change touching the surfaces
below must satisfy these rules AND get a managed-simulation test:

- **Never probe liveness with `os.kill(pid, 0)`.** On Windows it is
  `CTRL_C_EVENT` — throws on detached siblings or TERMINATES them.
  Use `psutil.pid_exists` + `rpc._cmdline_is` (pid-reuse guard).
- **Never `urlopen`/`requests`/`httpx.get` a `127.0.0.1` URL without
  `rpc._local_urlopen`.** `urllib.getproxies()` reads env AND the
  Windows registry — a corp proxy hijacks loopback and self-ejects
  the daemon. External calls (PyPI, Confluence, HF) keep the proxy.
- **Never shell out to OS tools that may be absent or blocked** —
  `wmic` (removed from Win11, AppLocker-blocked), `ps`, `git` (probe
  only). Prefer `psutil`/stdlib; degrade gracefully when missing.
- **Check `st_file_attributes` before reading user files on Windows** —
  `OFFLINE`/`RECALL_ON_DATA_ACCESS`/`RECALL_ON_OPEN` = OneDrive
  placeholders; reading hydrates from the cloud. Skip + disclose,
  never hydrate (`crawl_local._cloud_only`).
- **Explicit `encoding="utf-8"` on state-file I/O** — `Path.read_text`/
  `write_text` default to `cp1252` on Western Windows → mojibake.
- **No `msvcrt.locking(LK_LOCK)`** — retries ~10s then RAISES under a
  stuck holder. Use the bounded `LK_NBLCK` loop in `rpc._spawn_lock`.
- **TLS**: external calls use the OS cert store (`client._os_trust_context`),
  never bare `certifi` pinning — corp MITM roots live in the OS store.
  `pip-system-certs`' `.pth` injects truststore globally — its context's
  `get_ca_certs()` raises `NotImplementedError`; don't probe it.
- **WDAC/AppLocker**: unsigned `.pyd`/`.dll` (`onnxruntime`, `tokenizers`,
  `sqlite-vec`) fail `WinError 4551` under an enforced CI policy. Route
  native-load failures through `is_app_control_block()` so remediation
  says "ask IT to allowlist", never "reinstall".
- **Job objects**: IDE shells wrap children in `KILL_ON_JOB_CLOSE` jobs —
  detached daemons die with the IDE unless spawn requests
  `CREATE_BREAKAWAY_FROM_JOB` (`rpc._popen_detached`) and tolerates a
  breakaway-denied retry.
- **EDR/WFP**: loopback listeners can be bound but unreachable. The
  `loopback_listener` doctor probe (bind + connect) is the detection —
  remediation is always an exclusion request, never a workaround.
- **Reproduce the restriction before fixing** — simulate the denied
  probe/dead proxy/offline file, show the failure, then fix. Verify
  Windows-specific fixes on the Windows QA VM.
- **`scripts/check_managed.py` enforces the banned patterns in CI** —
  a legit POSIX-only fallback gets `# managed-ok` + the reason inline.

Diagnostics: `scripts/win_managed_sim.py` reports which restrictions a
machine enforces; `docs/managed-laptop.md` documents the class.

## Testing

Per-package pytest suites under `mcp/<pkg>/tests` (run from repo root
with the package venv). `ruff check` + `python scripts/check_managed.py`
must be clean before pushing. e2e session tests need `KNOWLEDGE_E2E=1`.
