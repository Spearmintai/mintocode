---
name: mic
description: Use when working in a repository that has a .micode/ directory (a compiled understanding of the codebase) — to answer questions about the code, locate where something is implemented, plan a change, or find what calls what, with far fewer tokens than exploring with Grep/Glob/Read. Also use when the user asks to compile, update or check the compiled understanding.
---
# Working from a compiled repository

`.micode/` holds a compile of this repository made by a strong model: a core card (already in context at
session start), module cards, file cards, a precompiled bank of question/answer pairs, end-to-end flows, and a
deterministic symbol table. Every `path:Lstart-end` citation in it was checked against the source.

## Order of operations
1. Check the `<micode-context>` block injected with the user's prompt: the minimal linked paragraphs for it (the
   best-matching functions with their source, plus the definitions they use, their callers and tests). If it answers
   the question, answer from it and cite its spans.
2. Otherwise call the mic MCP tools before exploring:
   - `ask(question)` — precompiled answers plus the relevant files with exact spans
   - `where(symbol)` — definition sites
   - `card(path)` — a file's full map (purpose, every symbol with its span, facts, gotchas)
   - `module(path)` — responsibilities, interfaces, how-to recipes of a directory
   - `deps(path)` — what a file imports and what imports it (blast radius)
   Without MCP, the same commands exist as `python3 "${CLAUDE_PLUGIN_ROOT}/bin/mic" ask|where|card ...`.
3. Read only the cited spans (Read with offset/limit). Use Grep only for what the compile cannot know, such as
   runtime strings or new code.
4. Before editing, read the exact lines you change. Items marked ⚠stale changed after the compile.

## Pure questions
For a question that needs no edit, `/mic:ask <question>` answers from the linked paragraphs in one cheap model
call, without an exploration loop.

## Maintenance
- `/mic:compile` makes the first compile, `/mic:update` recompiles changed files incrementally, and
  `/mic:status` shows staleness and cost.
- Commit `.micode/` (its own .gitignore excludes the cache) so the whole team reuses one compile.
