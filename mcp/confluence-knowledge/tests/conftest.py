"""Unit tests run the direct (non-proxy) path — the daemon is covered by
dedicated daemon tests + the IDEKILL stress scenario. All three service
env vars are pinned because the confluence suite parametrizes over
local_knowledge/sharepoint_knowledge too (#196)."""

import os

for _v in ("CONF_KNOWLEDGE_DAEMON", "LOCAL_KNOWLEDGE_DAEMON",
           "SP_KNOWLEDGE_DAEMON"):
    os.environ.setdefault(_v, "0")
