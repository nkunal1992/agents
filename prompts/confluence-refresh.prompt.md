---
name: confluence-refresh
description: Re-crawl the cached Confluence subtree to pick up page updates
argument-hint: Root page URL or pageId to refresh
agent: confluence-knowledge
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "confluence-knowledge" — also copy agents/confluence-knowledge.agent.md into .github/agents/.
-->


Refresh the Confluence subtree rooted at:

${input:root:Root Confluence page URL or pageId}

Connect, then run a full refresh crawl (reconcile deleted/moved pages), and
report: pages added/changed/removed, inaccessible pages, and the new as-of
timestamp.
