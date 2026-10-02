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
  folders (mode 0700 on macOS and Linux; on Windows they inherit the permissions of `%TEMP%`, which sits under your user profile) and deletes them **as soon as the run ends**, including
  when it ends with an error or an interrupt, with a delete at exit as a
  backstop. Folders left by a process that was killed are removed at the next
  start. If the directory cannot be redirected (this relies on an internal hook
  of CrewAI 1.15.x), the engine **refuses to run** instead of running without
  it: it checks on the real CrewAI at start-up that the task-output database
  lands in its temporary folder, and stops with a message otherwise.
  **What this cannot cover:** if the process is force-quit (SIGKILL,
  `kill`, a crash, power loss) *while the agents are running*, the folder stays
  in the temporary directory (`$TMPDIR` on macOS and Linux, `%TEMP%` on Windows; both under your own account) until the next
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

**The Stop button does not interrupt the agents.** Cancellation is checked before
the agents start and again after the structure step, not in the middle of a run,
so a Stop pressed while they are working takes effect when they finish (about a
minute or two in the comparisons above; in the worst case it is bounded by the
wall-clock limits in the table). The single-call engine behaves the same way.

## What was checked about CrewAI (2026-10-03)

| Question | Finding |
| --- | --- |
| Maintenance | Version 1.15.23 released 2026-09-28; frequent releases through 2026. ([PyPI](https://pypi.org/project/crewai/)) |
| Python versions | `>=3.10,<3.14`. **Not installable on 3.14**, which this project's docs recommend. |
| Telemetry | On by default (anonymous: versions, agent/task counts, role and tool names, model name, timings, success/failure; not prompts or outputs). Off with `CREWAI_DISABLE_TELEMETRY` or `OTEL_SDK_DISABLED`. ([docs](https://docs.crewai.com/en/telemetry)) |
| Checked against the real package (1.15.23, Python 3.13) | The three telemetry switches are honored by CrewAI's own `Telemetry`; the Agent/Task/Crew/LLM arguments used here exist; with a fake local server, only loopback connections were made. One LLM object per agent is needed, otherwise CrewAI's usage metrics count each request once per agent (3 requests were reported as 9). |
| Local server | `LLM(model="openai/<name>", base_url=..., api_key=...)` uses the OpenAI SDK directly. ([docs](https://docs.crewai.com/en/concepts/llms)) |

## Results of the comparison

Two meetings (A and B), one local model (`qwen2.5-coder-32b-instruct-mlx` through
LM Studio), minutes language set to English, one successful run per engine and
meeting. Only a qualitative reading is recorded; nothing from the meetings
themselves is written here, and the generated structures stay under
`output/_compare/` (git-ignored). For each meeting both engines were given the
same material (checked by hash).

| | A: single | A: CrewAI | B: single | B: CrewAI |
| --- | --- | --- | --- | --- |
| Material | chunk summaries, about 7,100 chars | same | full transcript, about 22,700 chars | same |
| Passed the checks (no fallback) | yes | yes | yes | yes |
| Wall-clock | 49 s | 50 s | 90 s | 92 s |
| LLM calls | 1 | 4 | 1 | 4 |
| Search-tool calls (cap 6) | 0 | 4 | 0 | 4 |
| Sections | 12 | 7 | 12 | 6 |

What differed:

1. **Granularity (the same in both meetings).** The single call produced about
   twice as many sections as CrewAI. It split the content into many topic-specific
   sections; CrewAI merged topics into fewer, coarser ones, and each time left
   some of the content without a section of its own. For B, the single call also
   covered side topics (personal and closing remarks) that CrewAI left out.
2. **Specifics in the structure (no consistent direction).** The aim is a
   structure that could be reused for another recording. In A, CrewAI's
   instructions named the event and its year while the single call's instructions
   were generic copies of the example. In B it was the other way round: the single
   call's instructions named a website, a third-party product and a personal
   project, while CrewAI's headings and instructions contained no proper nouns.
   So neither engine is reliably the more reusable one on this evidence.
3. **Fixed text.** CrewAI was in English throughout and gave the decision and
   next-action sections an explicit "none" fallback sentence. The single call
   mixed English headings with Japanese fixed text in both meetings. One of
   CrewAI's placeholder lines in A was garbled.
4. **Time and calls.** Wall-clock was within a few seconds in both meetings, with
   four times the LLM calls for CrewAI. The Researcher used its tool 4 times each
   time, inside the cap of 6; the caps held.

What this does and does not show: two meetings, one model, one successful run
each (sampling is not deterministic), so there is no winner to declare. CrewAI
cost four times the calls for about the same time and a coarser structure; it was
not clearly better in any respect that held across both meetings, and the single
call, which is simpler, stays the default. More meetings would be needed before
saying anything about quality.

### A note on running the comparison

Run one comparison at a time against a local server. In one session two
comparisons were started by mistake against the same LM Studio model, and while
their requests overlapped the MLX backend crashed (a fatal scheduler error, then
"the model has crashed"): both overlapping requests failed at the same moment,
one from each run, while the requests that ran alone succeeded. The CrewAI
engine reported the failure and the pipeline would have fallen back to the
built-in structure, as designed. The numbers above are only from requests that
ran alone; for B the two engines' results come from two separate runs for this
reason.
