# Decisions

Append-only. One entry per choice that could reasonably have gone another way.
Future-you and future-agent will not remember why, and the guide's defaults are
starting points, not conclusions.

Format:

```
## YYYY-MM-DD — <the choice>
**Decided:** what was chosen
**Alternatives:** what else was considered
**Why:** the reasoning
**Revisit if:** the condition that would change this
```

<!-- Agent: append new decisions below. Do not edit existing entries. -->

## 2026-08-06 — Zero dependencies in phase 0
**Decided:** `pyproject.toml` ships with an empty dependency list. `run.py` and the logging helper use only the standard library. `config.yaml` exists but nothing parses it yet.
**Alternatives:** Add PyYAML now so run.py loads config at startup.
**Why:** The standing rules require asking before adding any package, and phase 0 has no code that needs config values. PyYAML (and python-dotenv) will be proposed in phase 1, the first phase that reads config and secrets.
**Revisit if:** never — phase 1 supersedes this.

## 2026-08-06 — Phase numbers in not-implemented messages
**Decided:** update→1, clean→3, train→5, project→8, evaluate→9, audit→3.
**Alternatives:** update→2 (odds), train→6 (rates), project→7 (simulation).
**Why:** Each command points at the first phase that delivers a usable version of it: stats ingestion makes `update` real (odds extends it in 2), the minutes model is the first thing `train` fits (rates extend it in 6), and `project` needs pricing (8) before its output means anything.
**Revisit if:** a phase spec assigns a command differently when it arrives.

## 2026-08-06 — Python 3.12 interpreter
**Decided:** `uv sync` resolved to the machine's existing CPython 3.12.1; `requires-python = ">=3.11"` per the spec.
**Alternatives:** Pin 3.11 or have uv download 3.13.
**Why:** 3.12 satisfies the constraint, is already installed, and avoids an extra download. All planned libraries support it.
**Revisit if:** a dependency in a later phase lacks a 3.12 wheel.
