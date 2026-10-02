# Experiment: a CrewAI engine for Auto-mode structure generation

[日本語](crewai_experiment_ja.md) | English

This is a developer experiment (Issue #192), not a feature for everyday use. The
default behavior does not change, and nothing here is needed to use the tool.

## What it is

In Auto mode the minutes structure (the set of headings) is normally produced by
**one LLM call** (`_generate_structure` in `src/meeting_minutes/model/minutes.py`).
The experiment adds a second engine built on [CrewAI](https://docs.crewai.com/),
where three agents split the work:

1. **Classifier** decides the meeting type from the material.
2. **Researcher** looks things up in the material through a search tool (are there
   numbers, deadlines, owners, action items?).
3. **Designer** writes the structure from the two results, using the same rules
   (`prompts/structure_ja.txt`) as the single-call engine.

The goal is to try CrewAI the way it is meant to be used (agents that decide what
to look at) and to keep a record of how it compares with the single call on the
same input. A result where the single call is just as good, or better, is a
perfectly fine outcome.

The CrewAI output goes through **the same checks** (required placeholders, size)
and **the same fallback** (the built-in structure) as the single-call engine.

## Installing the optional extra

CrewAI supports **Python 3.10 to 3.13 only** (not 3.14). If your usual virtual
environment is on 3.14, make a separate one:

```sh
python3.13 -m venv .venv-py313
.venv-py313/bin/python -m pip install -r requirements-dev.txt -r requirements-agent.txt
```

`.venv*/` is git-ignored. A normal install (`requirements.txt`) pulls in nothing new.

## Using it

Start the GUI with the developer flag, and choose "Auto" for the minutes format:

```sh
.venv-py313/bin/python src/meeting_minutes/gui.py --structure-engine crewai
```

- Without the flag, behavior is unchanged (`--structure-engine single` is the default).
- With the flag but without CrewAI installed, it stops at start-up with a message
  saying how to install it (no stack trace).

## Comparing the two engines

```sh
.venv-py313/bin/python scripts/compare_structure_engines.py output/<meeting folder>
```

The folder must contain `transcript.json` (an earlier run's output folder works).
Both engines receive **exactly the same material**: the script goes through
`generate_minutes`, so the material is built by the existing logic (the full
transcript if it fits the structure budget, otherwise the existing chunk
summaries), and the run stops right after the structure step. No minutes are
generated. Results go to `output/_compare/<folder>-<timestamp>/` (git-ignored):

| File | Contents |
| --- | --- |
| `comparison.md` | A table (result, seconds, LLM calls, tool calls) and both structures |
| `comparison.json` | The same, machine-readable, with a hash of the material |
| `material.txt` | The exact material both engines received |
| `structure_single.txt` / `structure_crewai.txt` | The structures (only if the engine succeeded) |

There is no automatic score. Read the two structures and note what differs.

## How it stays local

- **Telemetry is off.** CrewAI sends anonymous usage statistics by default. The
  engine sets `CREWAI_DISABLE_TELEMETRY=true`, `OTEL_SDK_DISABLED=true`,
  `CREWAI_DISABLE_TRACKING=true` and `CREWAI_TRACING_ENABLED=false` when its
  module is imported, before CrewAI is imported, overriding any other value. `tests/test_offline_env.py` checks this in
  a separate process, and also checks that a normal GUI start does not touch
  these variables.
- **No meeting data is left in CrewAI's data directory.** CrewAI stores each
  run's task outputs (text derived from the meeting) in a SQLite file under
  `~/Library/Application Support/<folder name>/`, which would leave meeting
  content outside `output/`. The engine points that directory at a private
  temporary folder that is deleted when the process exits. Importing CrewAI
  still creates `~/.config/crewai` and a random key file,
  `~/Library/Application Support/crewai/credentials/secret.key`, which hold no
  meeting content.
- **Same local server as the rest of the tool.** The agents get
  `LLM(model="openai/<llm_model>", base_url=<[ai] base_url>, api_key=<[ai] api_key>)`
  explicitly; there is no environment-variable fallback to a hosted endpoint.
- **CI uses no real model and no CrewAI.** The tests use a fake `crewai` module and
  a fake LLM client.

## Hard caps

Small local models can misuse tools (in a sibling project, one issued about 50
searches in one response), so the limits are fixed in code
(`src/meeting_minutes/model/structure_crewai.py`):

| Limit | Value |
| --- | --- |
| `max_iter` (Classifier / Researcher / Designer) | 2 / 4 / 2 |
| Search-tool calls in total (counted by us, beyond the cap the tool refuses) | 6 |
| Wall-clock per agent | at most 900 s (and at most `[ai] timeout`) |
| Agents | 3, run in sequence, no delegation |

The number of LLM calls is read from CrewAI's usage metrics and shown in the
comparison table.

## What was checked about CrewAI (2026-10-03)

| Question | Finding |
| --- | --- |
| Maintenance | Version 1.15.23 released 2026-09-28; frequent releases through 2026. ([PyPI](https://pypi.org/project/crewai/)) |
| Python versions | `>=3.10,<3.14`. **Not installable on 3.14**, which this project's docs recommend. |
| Telemetry | On by default (anonymous: versions, agent/task counts, role and tool names, model name, timings, success/failure; not prompts or outputs). Off with `CREWAI_DISABLE_TELEMETRY` or `OTEL_SDK_DISABLED`. ([docs](https://docs.crewai.com/en/telemetry)) |
| Checked against the real package (1.15.23, Python 3.13) | The three telemetry switches are honored by CrewAI's own `Telemetry`; the Agent/Task/Crew/LLM arguments used here exist; with a fake local server, only loopback connections were made. One LLM object per agent is needed, otherwise CrewAI's usage metrics count each request once per agent (3 requests were reported as 9). |
| Local server | `LLM(model="openai/<name>", base_url=..., api_key=...)` uses the OpenAI SDK directly. ([docs](https://docs.crewai.com/en/concepts/llms)) |

## Results of the comparison

*Not run yet.* The comparison needs a Python 3.13 environment with CrewAI and a
local model, and will be added here once it has been run on one to three meetings
(including any case where CrewAI does worse). Only qualitative observations
(headings, missing sections, time, calls) will be recorded; no content of a real
meeting goes into this repository.
