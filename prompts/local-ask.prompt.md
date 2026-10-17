---
name: local-ask
description: Ask a question grounded in a local file or folder (docs, pdfs, spreadsheets)
argument-hint: File or folder path — and your question
agent: local-knowledge
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "local-knowledge" — also copy agents/local-knowledge.agent.md into .github/agents/.
-->


Answer this question using only the documents under:

${input:root:File or folder path}

Question: ${input:question:What do you want to know?}

Follow your standard flow: if the local-knowledge MCP tools aren't callable,
run your bootstrap first. Then status → connect → freshness nudge or crawl →
search → verify citations → cited answer with coverage line.
