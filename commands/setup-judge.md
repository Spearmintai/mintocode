---
description: Install micode's optional local paragraph judge (Laya, open-source, runs on your machine)
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/micode" setup-judge` with the Bash tool (it can take several minutes: it creates
a virtualenv with torch and laya; the first judge start downloads ~1.7 GB of weights). Add `--gpu` only if the user has
an NVIDIA GPU. Report when it finishes. The judge makes `/micode:ask` choose paragraphs more precisely.
