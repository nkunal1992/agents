---
name: confluence-ask
description: Ask a question grounded in a Confluence page subtree (root page + all descendants)
argument-hint: Root page URL — and your question
agent: confluence-knowledge
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "confluence-knowledge" — also copy agents/confluence-knowledge.agent.md into .github/agents/.
-->


Answer this question using only the Confluence subtree rooted at:

${input:root:Root Confluence page URL or pageId}

Question: ${input:question:What do you want to know?}

Follow your standard flow: check status → connect → freshness nudge or crawl →
search → verify citations → cited answer with coverage line. If setup is
missing, explain how to run it instead of guessing.
