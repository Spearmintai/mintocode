---
description: Answer a codebase question from the minimal linked paragraphs, in one cheap model call
argument-hint: "<question>"
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/micode" ask "$ARGUMENTS"`. micode chooses the minimal set of source paragraphs
(best-matching functions plus the definitions, callers and tests they link to), and one model call answers from them.
Relay its answer to the user as it is, keeping the file:line citations. Do not re-investigate unless the user asks.
