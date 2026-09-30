---
description: Incrementally recompile only the files that changed since the last micode compile
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/micode" update .` from the repository root with the Bash tool. It recompiles only
changed files and their modules. Report how many files changed and the cost.
