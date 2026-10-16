---
name: confluence-setup
description: One-time setup for the local Confluence knowledge service (credentials, models, MCP registration)
argument-hint: Say "start" — or describe your OS/IDE if you hit an error
agent: confluence-setup
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "confluence-setup" — also copy agents/confluence-setup.agent.md into .github/agents/.
-->


Run the confluence-knowledge setup flow: detect OS/IDE → preview and install
the local service → walk me through secure credential setup (I enter the token
in my own terminal) → prepare the local models → register the MCP server →
verify with doctor. Show every command before running it and wait for my
approval.
