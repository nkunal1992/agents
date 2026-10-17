---
name: local-refresh
description: Re-index a local file or folder to pick up changes
argument-hint: File or folder path to refresh
agent: local-knowledge
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "local-knowledge" — also copy agents/local-knowledge.agent.md into .github/agents/.
-->


Refresh the local index for:

${input:root:File or folder path}

Connect, then run a full refresh crawl, and report: files added/changed/
removed, files skipped (with reasons), and the new as-of timestamp.
