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
- **CrewAI's own copy of the material is deleted when a run ends.** CrewAI saves
  each task's output in a SQLite file under
  `~/Library/Application Support/<folder name>/`, and that record includes the
  task description and the agent messages, that is, **the whole material given
  to the agents** (the transcript, or the chunk summaries). Left alone, it would
  sit outside `output/`. The engine points that directory at private temporary
  folders (mode 0700) and deletes them **as soon as the run ends**, including
  when it ends with an error or an interrupt, with a delete at exit as a
  backstop. Folders left by a process that was killed are removed at the next
  start. **What this cannot cover:** if the process is force-quit (SIGKILL,
  `kill`, a crash, power loss) *while the agents are running*, the folder stays
  in the temporary directory (`$TMPDIR`, readable only by you) until the next
  start of this tool or the OS's own temp cleanup. The Stop button and closing
  the window end the run normally and are covered. Importing CrewAI also
  creates `~/.config/crewai` and a random key file,
  `~/Library/Application Support/crewai/credentials/secret.key`; neither holds
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

One meeting (about 15,000 characters of transcript, Japanese-language meeting,
minutes language set to English), one local model
(`qwen2.5-coder-32b-instruct-mlx` through LM Studio), one run each. Only a
qualitative reading is recorded; nothing from the meeting itself is written
here, and the generated structures stay under `output/_compare/` (git-ignored).
Both engines were given the same material. The transcript did not fit the
structure budget at the loaded context length, so the material was the existing
chunk summaries (3 chunks, about 7,100 characters).

| | Single call | CrewAI (3 agents) |
| --- | --- | --- |
| Passed the checks (no fallback) | yes | yes |
| Wall-clock | 49 s | 50 s |
| LLM calls | 1 | 4 |
| Search-tool calls (cap 6) | 0 | 4 |
| Sections | 12 | 7 |

What differed:

1. **Granularity.** The single call split the content into many topic-specific
   sections and added decision and next-action sections. CrewAI produced fewer,
   coarser sections: several topics were merged, and some distinct topics did
   not get a section of their own.
2. **The instruction under each heading.** The single call mostly copied the
   generic placeholder sentence from the example in `prompts/structure_ja.txt`
   under every heading, so the headings carry the meeting-specific information
   and the instructions carry none. CrewAI wrote a separate instruction for each
   heading and repeated the "only what is explicitly mentioned" rule in it.
3. **Reusability.** CrewAI's instructions named the event and its year, which
   ties the structure to this one meeting (the stated aim is a structure that can
   be reused for another recording). The single call's headings also contained
   meeting-specific names, so both are affected, in different places.
4. **Language and polish.** The single call mixed English headings with Japanese
   fixed text. CrewAI was in English throughout, but one placeholder line was
   garbled.

What this does and does not show: with one meeting, one model and one run
(sampling is not deterministic), there is no winner to declare. CrewAI used four
times the LLM calls for about the same time and a different, not clearly better,
structure. The Researcher did use its tool (4 calls, inside the cap of 6), and
the caps held. The single call is the simpler engine and stays the default.
More meetings would be needed before saying anything about quality.
