"""Unit tests run the direct (non-proxy) path — the daemon is covered
by test_proxy_daemon.py and the IDEKILL stress scenario. Opt back in per
test via monkeypatch.setenv("LOCAL_KNOWLEDGE_DAEMON", "1") (#196)."""

import os

os.environ.setdefault("LOCAL_KNOWLEDGE_DAEMON", "0")
