---
name: sharepoint-refresh
description: Re-index a SharePoint site or folder to pick up changes
argument-hint: SharePoint site/library/folder URL to refresh
agent: sharepoint-knowledge
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "sharepoint-knowledge" — also copy agents/sharepoint-knowledge.agent.md into .github/agents/.
-->


Refresh the SharePoint index for:

${input:root:SharePoint site, library, or folder URL}

Connect, then run a full refresh crawl, and report: items added/changed/
removed, items skipped (with reasons), and the new as-of timestamp.
