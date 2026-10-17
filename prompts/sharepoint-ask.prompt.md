---
name: sharepoint-ask
description: Ask a question grounded in a SharePoint site, library, or folder
argument-hint: SharePoint site/library/folder URL — and your question
agent: sharepoint-knowledge
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "sharepoint-knowledge" — also copy agents/sharepoint-knowledge.agent.md into .github/agents/.
-->


Answer this question using only the SharePoint content at:

${input:root:SharePoint site, library, or folder URL}

Question: ${input:question:What do you want to know?}

Follow your standard flow: if the sharepoint-knowledge MCP tools aren't
callable, run your bootstrap first. Then status → connect → freshness nudge
or crawl → search → verify citations → cited answer with coverage line.
