# Experiment: a CrewAI engine for Auto-mode structure generation

[日本語](crewai_experiment_ja.md) | English

## Summary

**We do not recommend it to users and do not adopt it.**
It needed countermeasures for side effects, and it was not better than the single call.
The code is kept as a record of the experiment and is reachable only through a developer flag.

### What we tried

- We added a second engine for Auto-mode structure generation, built on CrewAI (three agents: classify, research, design) (Issue #192).
- We wired it into the real pipeline behind a developer flag.

### What we found

Running and reading the real package, not the fake used in the tests, turned up things the fake could not show.

1. Each task's output, which includes the whole material given to the agents, is saved in a database under the home directory, that is, outside `output/`.
2. Merely importing CrewAI creates files in the home directory.
3. `max_execution_time` does not stop a running agent, and its error quotes the whole material.
4. On a context overflow it summarizes with extra LLM calls outside the step limit, so a call cap that is only computed from the settings is not enforced.
5. When the agents share one LLM object, its usage metrics are counted more than once (3 requests reported as 9).
6. A bug in our own cleanup code (it sent Ctrl+C on Windows) was caught by CI on the third operating system.

### How we closed them

- A **harness**: code that controls and limits the agents from the outside (not a test harness).
- Reviews by a different vendor (Codex) and by the manager session. They found a redirect that failed open and the two caps that were not enforced (3 and 4).
- CI on three operating systems. It found the Windows bug (6).

Each fix has a test, some against the real CrewAI.

### What we compared

On two meetings and one local model, the CrewAI engine showed the following.

- Time: about the same as the single call.
- LLM calls: four times as many.
- Structure: coarser.

It was not clearly better in any respect that held across both meetings.

### What we decided

- Not recommended to users, and not adopted.
- Off by default, reachable only through a developer flag.
- The code is kept as a record of the experiment.

### What we did not do

- The issue asked for a run of the whole pipeline with Wi-Fi turned off. Since this is no longer a feature for users, we did not do it.
- Instead we checked that, with the real CrewAI against a fake local server, only loopback connections were made.

The rest is for developers who want to reproduce it or read the details.
The default behavior of the tool is unchanged, and nothing here is needed to use it.

## What it is

The structure (the set of headings) in Auto mode is normally produced by **one LLM call** (`_generate_structure` in `minutes.py`).
The experiment splits the work across three [CrewAI](https://docs.crewai.com/) agents.

- **Classifier** decides the meeting type from the material.
- **Researcher** looks things up in the material through a search tool (numbers, deadlines, owners, action items?).
- **Designer** writes the structure from the two results, with the same rules as the single call (`prompts/structure_ja.txt`).

The aim was to try CrewAI the way it is meant to be used (agents that decide what to look at) and to keep a record of how it compares with the single call on the same input.
A result where the single call is as good or better is a fine outcome.

## What the harness limits

The agents decide how to go about the task; the harness decides what they cannot do.
It is the same idea as the harness engineering described under "Development process" in the README, applied to the agents instead of to the development work.
Each item has a test. The implementation is in `src/meeting_minutes/model/structure_crewai.py`.

### Keeps the work local

- Telemetry and tracing are switched off before CrewAI is imported.
  `CREWAI_DISABLE_TELEMETRY`, `OTEL_SDK_DISABLED` and `CREWAI_DISABLE_TRACKING` are set to true, and `CREWAI_TRACING_ENABLED` to false.
- The agents talk only to the `[ai] base_url` server, with the configured key and model.
- There is no environment-variable fallback to a hosted endpoint.

### Does not leave the material behind

CrewAI saves a copy of the material in a SQLite file.

- That location is redirected to a private temporary folder (mode 0700 on macOS and Linux; on Windows it inherits the permissions of `%TEMP%`).
- The folder is deleted as soon as the run ends, also on an error or an interrupt.
- Leftovers of a killed process are removed at the next start.
- If the redirect cannot be set up or verified, the engine **refuses to run**.

### Limits steps and calls

- `max_iter`: 2 for the Classifier, 4 for the Researcher, 2 for the Designer.
- Search tool: 6 calls in total.
- LLM calls: **22 in total, enforced.** One counter shared by the three agents counts every call, including any summarizing (typically 4 are made).
- The 23rd call is refused and the pipeline falls back to the built-in structure.
- CrewAI's automatic summarizing on a context overflow is switched off. An overflow fails instead of making extra calls.

### Treats the result like the single call's

- The same checks (required placeholders, size).
- The same fallback (the built-in structure).
- Error messages are shown shortened, because some quote the material.

### What the harness does not do

- **There is no wall-clock cap.** Each call is bound only by `[ai] timeout` (default 600 s).
- The OpenAI client may retry a failed HTTP request up to twice inside one call. The counter does not see that.
- CrewAI's own `max_execution_time` is not used, because it does not work.
- The Stop button does not interrupt the agents mid-run. It takes effect when they finish (about a minute or two in the comparisons).
- A force-quit during a run (SIGKILL, a crash, power loss) can leave the temporary folder until the next start or the OS's temp cleanup (`$TMPDIR` / `%TEMP%`, under your own account).
- Importing CrewAI creates `~/.config/crewai` and similar files. They hold no meeting content.

## Reproducing it (for developers)

CrewAI supports **Python 3.10 to 3.13 only** (not 3.14). Install it in a separate virtual environment (`.venv*/` is git-ignored).

### 1. Create the environment

```sh
python3.13 -m venv .venv-py313
.venv-py313/bin/python -m pip install -r requirements-dev.txt -r requirements-agent.txt
```

### 2. Start the GUI

```sh
.venv-py313/bin/python src/meeting_minutes/gui.py --structure-engine crewai
```

- Choose "Auto" for the minutes format.
- Without the flag, behavior is unchanged.
- With the flag but without CrewAI installed, it stops at start-up with a message saying how to install it (no stack trace).

### 3. Compare the two engines

```sh
.venv-py313/bin/python scripts/compare_structure_engines.py output/<meeting folder>
```

- Use a folder that holds `transcript.json`.
- Both engines get the **same material**, built by the existing logic. The run stops right after the structure step (no minutes are generated).
- Results go to `output/_compare/` (git-ignored): a table, a machine-readable copy, the exact material, and the structures.
- There is no automatic score.
- Run **one comparison at a time** against a local server (see below).

## What was checked about CrewAI (2026-10-03)

| Question | Finding |
| --- | --- |
| Maintenance | 1.15.23 released 2026-09-28; frequent releases ([PyPI](https://pypi.org/project/crewai/)) |
| Python | `>=3.10,<3.14`; **cannot be installed on 3.14**, which this project recommends |
| Telemetry | On by default (anonymous statistics, not prompts or outputs). Off with the variables above ([docs](https://docs.crewai.com/en/telemetry)) |
| Local server | `LLM(model="openai/<name>", base_url=..., api_key=...)` ([docs](https://docs.crewai.com/en/concepts/llms)) |

## Results of the comparison

Conditions:

- Two meetings (A and B), one local model (`qwen2.5-coder-32b-instruct-mlx` through LM Studio).
- Minutes language set to English. One successful run per engine and meeting.
- Both engines got the same material (checked by hash).
- Nothing from the meetings is written here.

| | A: single | A: CrewAI | B: single | B: CrewAI |
| --- | --- | --- | --- | --- |
| Material | chunk summaries, about 7,100 chars | same | full transcript, about 22,700 chars | same |
| Passed the checks | yes | yes | yes | yes |
| Wall-clock | 49 s | 50 s | 90 s | 92 s |
| LLM calls | 1 | 4 | 1 | 4 |
| Search-tool calls (cap 6) | 0 | 4 | 0 | 4 |
| Sections | 12 | 7 | 12 | 6 |

Differences:

- **Granularity.** The single call made about twice as many sections and split topics finely. CrewAI merged topics, and in both meetings some content got no section of its own.
- **Specifics (no consistent direction).** The aim is a structure that can be reused for another recording. In A, CrewAI's instructions named the event and its year; in B, the single call's instructions named a website and a product. Neither is reliably the more reusable.
- **Time and calls.** Within a few seconds of each other, with four times the LLM calls for CrewAI. The Researcher used its tool 4 times, inside the cap of 6.

What this shows:

- With two meetings, one model and one run each (sampling is not deterministic), there is no winner to declare.
- The simpler single call stays the default.
- More meetings would be needed to say anything about quality.

### Run one comparison at a time

- Two comparisons were started by mistake against the same LM Studio model. While their requests overlapped, the MLX backend crashed.
- The two overlapping requests failed at the same moment (requests that ran alone succeeded).
- The CrewAI engine reported the failure and the pipeline would fall back to the built-in structure.
- The numbers above come only from requests that ran alone. For B, the two engines' results therefore come from two separate runs.
