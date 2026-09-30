"""Compiler prompts. The reader of every artifact is a coding agent, not a human."""

SYSTEM = (
    "You are a code compiler. You read source code and emit dense, exact, machine-oriented notes that "
    "let another coding agent answer questions and make changes WITHOUT reading the source again. "
    "Never pad, never praise, never speculate. Prefer concrete names, literal values, conditions and "
    "call relationships over prose. Cite locations as `path::Symbol` (preferred) or `path:L12-40`. "
    "Output exactly the JSON or Markdown requested."
)

FILE_CARD = """Compile the source file(s) below into agent-facing cards.

For EACH file, emit one object:
{{
  "path": "<exact path>",
  "purpose": "<one sentence: why this file exists>",
  "summary": "<3-8 dense sentences: what it does, how control and data flow through it, key data structures, who calls it>",
  "symbols": [{{"name": "<exact name from the symbol list>", "does": "<behaviour: inputs, outputs, raises/errors, defaults, side effects, notable branches>"}}],
  "facts": ["<exact facts an agent may be asked: constants and their values, defaults, env vars, config keys, CLI flags, routes, error messages, file formats, thresholds, versions — each with L<line>>"],
  "gotchas": ["<non-obvious behaviour, invariants, ordering constraints, coupling to other files, footguns>"],
  "questions": ["<8-15 concrete questions a developer or agent would ask whose answer is in this file>"]
}}

Rules: cover every non-trivial symbol in the provided symbol list (skip trivial getters); keep names exactly as listed;
put literal values in facts; use line numbers from the left margin. For documentation/config files, "symbols" may be
the section names and "facts" the settings or procedures they state.
{part_note}
Return {{"files": [ ... ]}} only.

{body}"""

MODULE_CARD = """Compile the module `{module}` of repository `{repo}` into an agent-facing module card, using the file
cards below (already compiled from source) and the dependency facts.

Dependencies (from import analysis): this module imports {deps_out}; it is imported by {deps_in}.
Tests touching it: {tests}

Return JSON only:
{{
  "purpose": "<one sentence>",
  "overview": "<5-10 dense sentences: responsibilities, internal architecture, main data/control flow across its files, cite path::Symbol>",
  "key_files": [{{"path": "<path>", "role": "<one line>"}}],
  "interfaces": ["<public entry points other modules use, as path::Symbol — what for>"],
  "how_to": ["<'To <common change>, edit path::Symbol (and ...), then ...'>"],
  "gotchas": ["<cross-file invariants and traps>"],
  "questions": ["<10-20 concrete developer questions answered by this module>"]
}}

FILE CARDS:
{cards}"""

CORE = """You are writing the CORE CARD for repository `{repo}`. It is loaded into every coding-agent session, so
every token must earn its place. Hard limit: {words} words. Use exactly these Markdown sections:

## What this is
(2-4 sentences: product, language/stack, how it is used)
## Architecture
(the main components and how a request/job/data flows through them, citing path::Symbol)
## Module map
(one line per module: `path/` — responsibility. Every module listed below must appear.)
## Entry points
(CLIs, servers, public APIs, main functions — path::Symbol)
## Build, test, run
(exact commands from the manifests below; how to run a single test)
## Conventions
(patterns the code follows that a change must respect: error handling, naming, config, logging, typing, style)
## Where to change things
(8-15 lines: 'To <task> → path::Symbol, path2')
## Glossary
(project-specific terms → meaning, only if non-obvious)

Use only facts present in the material below. Do not invent commands.

MODULE CARDS:
{modules}

MANIFESTS AND TOP-LEVEL DOCS:
{manifests}"""

QA = """Below is part of repository `{repo}`: the module card, file cards and the line-numbered SOURCE of module
`{module}`. Anticipate what developers and coding agents will ask about this code and compile the answers now,
so later sessions can answer without reading the source.

Produce {n} question/answer items covering: where-is / how-does-X-work / what-happens-when / why / how-do-I-add-or-change /
what-calls-what / edge cases / defaults and config / error causes. Answers must be correct against the SOURCE, complete
enough to act on, 1-6 sentences, and cite precise locations (path::Symbol or path:L12-40). Prefer questions phrased the way
a person would actually type them. Include some questions that span into other modules when the source shows the link.

Return JSON only: {{"qa": [{{"q": "...", "a": "...", "refs": ["path::Symbol" or "path:L12-40", ...]}}]}}

MODULE CARD:
{module_card}

FILE CARDS:
{cards}

SOURCE:
{source}"""

FLOWS = """From the core card and module cards of repository `{repo}` below, write the {n} most important end-to-end
flows an agent needs (e.g. how a request is served, how a job runs, how data is loaded and saved, how the CLI dispatches,
startup and shutdown, error propagation). Each flow is an ordered list of steps citing path::Symbol. Only use symbols that
appear in the cards.

Return JSON only: {{"flows": [{{"name": "...", "q": "<question this flow answers>", "steps": ["path::Symbol — what happens", ...]}}]}}

CORE CARD:
{core}

MODULE CARDS:
{modules}"""

ALIASES = """For each code symbol below (from repository `{repo}`), write how developers would describe or ask about it
WITHOUT knowing its name: 2-3 short abstract descriptions in plain words (e.g. "the flag that records whether the
component was already registered", "decorator that blocks setup calls after first request"). Avoid reusing the
identifier's own words where a synonym exists; mention its role, owner class/module and behaviour.

Return JSON only: {{"<id>": ["...", "..."], ...}} using the ids given.

{items}"""
