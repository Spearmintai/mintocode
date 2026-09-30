---
description: Compile this repository into .micode/ (one-time; later runs are incremental)
argument-hint: "[--model opus|fable|sonnet] [--card-model sonnet] [--full]"
allowed-tools: Bash(python3:*)
---
Compile the current repository with mic. This calls a strong model many times, once per batch of files; it may take a
few minutes and costs model usage once. Every later session then reads the compiled result instead of the source.

Run it in the background with the Bash tool (`run_in_background: true`), from the repository root:

`python3 "${CLAUDE_PLUGIN_ROOT}/bin/mic" compile . $ARGUMENTS`

When it finishes, report the stats line (files, lines, Q/A items, verified citations, cost). Tell the user that `.micode/`
is plain text meant to be committed, so teammates and CI reuse the compile, and that `/mic:update` recompiles only changed files.
