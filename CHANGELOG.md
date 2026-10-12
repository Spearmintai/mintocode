# Changelog

## 1.1.0

Built for large, real-world codebases, starting with the C++ CFD world (thanks to
[@FoamScience](https://github.com/FoamScience) for [#2](https://github.com/Spearmintai/mintocode/issues/2)).

**Custom file types**
- `mic compile --ext .H,.C`, `--ext .inl=cpp`, `MIC_EXTENSIONS`, and `"extensions"` in `.mic.json` (per repo) or
  `~/.config/mic/config.json` (per user). Later sources win: user, repo, environment, command line.
- Extensions match case-sensitively, so `.C` can be C++ while `.c` stays C; uppercase `.C`/`.H` infer C++.
- Command-line extensions are recorded in the manifest and reapplied by `mic update`.

**Ignore and scope**
- `.micignore` with gitignore syntax (`dir/`, `/anchored`, `**`, `!re-include`); `.micodeignore` still works.
- `"ignore"` and `"include"` in `.mic.json`, to skip generated trees or compile only part of a monorepo.

**C++ (tested on OpenFOAM-dev core: `src/OpenFOAM` + `src/finiteVolume`, 3,273 files, 546k lines)**
- Templated, multi-line definitions in OpenFOAM style (`template<class Type>` / `void Foam::fvMatrix<Type>::relax` /
  `(` on the next line) are indexed; `fvMatrix.C` goes from 0 to 159 symbols. Operators are indexed too.
- Out-of-class definitions at column 0, including constructors and `Foam::scalar Foam::Field::rms()`.
- In-class member declarations are indexed, so header citations resolve.
- `::` qualifiers resolve everywhere: `mic where rms`, `Field::rms` and `Field.rms` find the same method.
- Calls inside function bodies are no longer mistaken for definitions; namespaces are no longer indexed as symbols.
- A header declaration chosen for a pack is swapped for its definition's code (`.H` declares, `.C` defines), and the
  declaration stays as a one-line link.

**Scale**
- Include and import resolution use name indexes: 25,000 files with 30 includes each resolve in 0.2 s
  (about 500× faster than 1.0 on lnInclude-style trees).
- Module size adapts to repository size (about 150 modules at most): OpenFOAM's core goes from 558 module calls to 65.
- On OpenFOAM's core: structure compile 1.7 s, `mic where` 0.07 s, a link pack 0.23 s, the per-prompt hook 183 ms.

**Spend control**
- `--structure-only`: no model calls at all. Symbol table, link graph, `where`/`deps`/packs from live source and the
  hooks work immediately and for free; a later normal compile adds the compiled notes.
- `--no-qa` skips the precompiled Q&A stage; with `--card-model sonnet` it halves the cost of a large first compile.

**Cost preview**
- `mic compile --dry-run` shows files, lines per language, modules, model calls and an estimated token and dollar
  cost, without calling a model. Every compile prints the estimate first.

**Other**
- `mic --version`; version 1.1.0 across the CLI, plugin and MCP server.

## 1.0.0

First release: compiler, link packs, Claude Code plugin, `mic install` for Codex, OpenCode, ZCode, Cursor, VS Code,
Claude Desktop, Windsurf and Gemini CLI, and the MCP server.
