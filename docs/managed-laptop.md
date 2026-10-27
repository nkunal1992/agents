# Managed laptops

Corporate Windows laptops impose restrictions that consumer machines
do not. This page documents the restriction class, the symptoms each
restriction produces, and how to diagnose which of them your machine
enforces.

## Diagnose your machine

Run the self-diagnostic (read-only, ~10s):

```
python scripts\win_managed_sim.py
```

It reports a one-line verdict per restriction: whether `os.kill(pid,0)`
throws or kills, whether a corporate proxy would hijack loopback
daemon calls, whether `wmic` exists, LongPathsEnabled, Store-Python
virtualisation, network/roaming profile dirs, Controlled Folder
Access, and how much of the process table is access-denied.

The daemon also runs a subset automatically — `knowledge doctor`
surfaces the flags below under **Windows managed-environment checks**.

## The restrictions and their symptoms

| Restriction | Symptom without the fix | Guard |
|---|---|---|
| `os.kill(pid,0)` ≠ POSIX probe on Windows (`CTRL_C_EVENT`/throws) | watchdog process pile-up — tens of `watchdog` siblings per daemon home (#259) | `psutil.pid_exists` + pid-reuse cmdline check; watchdog self-ejects when the pid file names another owner |
| `urllib` honours env **and registry** proxy settings | localhost daemon calls routed through the corporate proxy → self-probe fails → daemon self-ejects → watchdog crash-respawn loop | all loopback calls go through `rpc._local_urlopen` (a `ProxyHandler({})` opener); external calls keep honouring the proxy |
| `wmic` removed from modern Windows / AppLocker-blocked | doctor process-count raised `FileNotFoundError` | `psutil.process_iter` |
| `msvcrt` `LK_LOCK` retries ~10s then raises | two installs racing for the spawn lock → one spawn hard-fails | bounded `LK_NBLCK` retry loop in `rpc._spawn_lock` |
| OneDrive files-on-demand placeholders aren't symlinks | `read_bytes` hydrates from the cloud → a folder of placeholders = hydrate-storm, crawl looks frozen | `st_file_attributes` OFFLINE/RECALL check → skip + disclosure warning |
| TLS inspection (corporate MITM root) | `certifi` lacks the corporate root → every Confluence call and model download fails `CERTIFICATE_VERIFY_FAILED` | OS trust store via `ssl.create_default_context()` (+ `pip-system-certs`); doctor reports whether inspection is active |
| `Path.read_text()` defaults to `cp1252` | non-ASCII JSON state mojibake | explicit `encoding="utf-8"` on all state I/O |
| `sqlite-vec` has no Windows ARM64 wheel | `pip install` fails outright on Copilot+ laptops | dependency marked `platform_machine != 'ARM64'`; runtime falls back to the O(n) scan automatically |
| `cryptography` has no Windows ARM64 wheel above 46.0.3 | `pip install` fails outright (transitive: `mcp`→`PyJWT[crypto]`, `msal`) | pinned `<46.0.4` under the same ARM64 marker; amd64/macOS resolve unpinned |
| `py_rust_stemmers` (fastembed dep) has no Windows ARM64 wheel at any version | `pip install pkg[models]` fails outright | `fastembed` marker-skipped on ARM64; sparse leg degrades to dense+rerank (`SparseUnavailable`), doctor reports `platform-degraded`, `prepare-models` skips the sparse download |
| WDAC/AppLocker code-integrity policy (enforced) | unsigned `.pyd`/`.dll` — `onnxruntime`, `tokenizers`, `sqlite-vec` — fail `ImportError`/`WinError 4551` "Application Control policy has blocked this file" | `is_app_control_block()` detection → remediation tells the user to ask IT for an allowlist/managed-installer rule, not to reinstall; doctor flags deployed CI policy blobs |
| EDR/WFP loopback interception (CrowdStrike, SentinelOne, AV "encrypted web scan") | daemon's `127.0.0.1` listener bound but unreachable → connect fails → same crash-respawn loop as the proxy case | doctor `loopback_listener` probe (bind + connect, all platforms); remediation = loopback exclusion for python |
| Windows Job objects (`KILL_ON_JOB_CLOSE` — VS Code/JetBrains terminals, Copilot CLI) | "detached" daemon still dies when the IDE closes | spawns request `CREATE_BREAKAWAY_FROM_JOB` with a retry-without fallback (#220); doctor `in_job_object` flag warns when running inside a job |
| PAC/WPAD-only proxy (`AutoConfigURL`) | `urllib.getproxies()` sees nothing — external calls (PyPI, HF, Confluence) fail while the browser works fine | doctor `pac_proxy` flag → set explicit `HTTPS_PROXY` |
| Controlled Folder Access, block mode | writes to Documents/Desktop/Pictures denied for unapproved apps | doctor reads `EnableControlledFolderAccess` and hard-fails when state lives under a protected folder |
| Temporary/mandatory profile | profile dir discarded on logout → full re-crawl every login | doctor `temp_profile` flag → point workdir at a persistent local dir |
| `subprocess.run(timeout=…)` post-kill re-`communicate()` | tool shim (Git-for-Windows under ARM64 emulation) times out, gets killed, then a surviving grandchild keeps the pipe handles open → the post-kill wait blocks **forever** — `register_token` hung 90s+ in `ensure_gitignore` (#322) | `_git_root()` walks parents for `.git` instead of `git rev-parse`; unavoidable child calls go through `_run_bounded` — `communicate(timeout)` → `kill` → `wait(timeout)` → abandon, `stdin=DEVNULL`, and `capture=False` opens no pipes when only the exit code matters |
| DNS filtering (Zscaler/Umbrella sinkholes) | `huggingface.co`/`pypi.org` NXDOMAIN → model downloads and updates fail | `win_managed_sim.py` DNS probe surfaces it; install-time failure message names the domain |

## Doctor flags

`loopback_listener` runs on **all** platforms — it binds and connects a
real `127.0.0.1` listener; a failure means security tooling intercepts
loopback and daemon mode can't work without an exclusion.

On `sys.platform == "win32"` the managed checks add:

- `proxy_managed` — a proxy is configured; warns if `NO_PROXY` doesn't
  cover `127.0.0.1` (the loopback helper bypasses it anyway).
- `workdir_network` — workdir/profile is UNC/network → SQLite WAL over
  SMB corrupts/stalls (hard flag).
- `long_paths` — `LongPathsEnabled=0` → deep trees hit 260-char limits.
- `store_python` — Store Python → profile-dir writes may virtualise and
  packaged apps sit under AppContainer network isolation.
- `av_exclusion` — real-time AV scanning `~\.knowledge` slows crawls;
  suggests a Defender exclusion.
- `controlled_folder_access` — CFA in **block mode** + state under a
  protected folder → hard flag.
- `protected_folder` — state under Documents/Desktop/Pictures with CFA
  unverified → informational.
- `pac_proxy` — `AutoConfigURL`/`AutoDetect` set → python can't evaluate
  PAC; external calls need explicit `HTTPS_PROXY`.
- `app_control_policy` — CI policy blobs deployed → unsigned native
  extensions may be blocked on load.
- `temp_profile` — profile looks temporary → state lost on logout
  (hard flag).
- `in_job_object` — running inside a Windows Job → daemon may die with
  the IDE unless the job allows breakaway.

## Engineering rule

Any new code touching process liveness, filesystem reads, loopback
networking, or OS tooling must satisfy the checklist in `AGENTS.md`;
`scripts/check_managed.py` runs in CI and rejects the banned patterns
(`os.kill(pid,0)`, loopback `urlopen` outside the helper, `wmic`, raw
`LK_LOCK`). Reproduce the restriction before fixing it.
