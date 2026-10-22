"""Unit tests run the direct (non-proxy) path — the daemon is covered by
dedicated daemon tests + the IDEKILL stress scenario. All three service
env vars are pinned because the confluence suite parametrizes over
local_knowledge/sharepoint_knowledge too (#196)."""

import os

for _v in ("CONF_KNOWLEDGE_DAEMON", "LOCAL_KNOWLEDGE_DAEMON",
           "SP_KNOWLEDGE_DAEMON"):
    os.environ.setdefault(_v, "0")


# Everything daemon-shaped anchors at daemon_home() — isolate it per test
# so a stray daemon-enabled test can never write into the developer's real
# ~/.knowledge/ (pollution + cross-test flakiness, #206).
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolated_shared_home(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME", str(tmp_path / "shared"))
