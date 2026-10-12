---
description: Set mic's file types, ignores and scope (e.g. ext .H .C, ignore tutorials/, include src/finiteVolume, show)
argument-hint: "show | ext .H .C [--user] | ignore PATTERN... | include PATH... | auto off"
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/mic" config $ARGUMENTS` from the repository root and report what changed. If file
types, ignores or scope changed, offer to run `/mic:update` so the compile picks them up.
