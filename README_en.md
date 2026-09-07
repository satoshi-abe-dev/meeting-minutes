# meeting-minutes

English | [日本語](README_ja.md)

**Turn a recording of a meeting into minutes (Markdown). Everything runs on your own PC; audio, transcription, and summarization are never sent anywhere external.**

<!-- badges: CI badges planned for phase 2 -->

> 🧭 **The requirements and process design decisions here are the author's.** The main ones:
>
> - **The all-local-processing constraint** — designed around the requirement that
>   confidential meeting content never leaves the machine
> - **The worker/manager two-session governance model** — separated implementation
>   from independent review/merge authority, and required an independent review
>   from a different vendor (Codex)
> - **Proposing "Auto" mode** — having the LLM suggest a heading structure per video,
>   which can be pinned as a template once you like it
> - **The overall GUI design**
>
> Implementation was carried out by multiple Claude Code sessions. The roles and
> review criteria are documented in
> [Development process](#development-process-ai-assisted-collaboration).

---

## The problem

Every meeting, someone rewatches the recording and writes the minutes by hand. People want to automate this, but there are settings where existing cloud transcription/summarization services are not an option:

- Meetings contain **customer information, non-public business information, HR information, or personal data**
- NDAs, data-protection law, GDPR, internal policy, or industry regulation mean the **recording cannot be sent to an external service**
- "Convenient, but the data leaves the building" tools do not pass procurement review in the first place

## What this is

A CLI / GUI tool that takes a single video file and runs the following **entirely locally** to generate minutes.

1. Extract audio from the video (ffmpeg)
2. Transcribe with timestamps (faster-whisper; local and offline)
3. Extract frames of screen shares / slides and summarize their content (local VLM)
4. Generate minutes from the transcript + frame notes (local LLM)

- There is no code that connects to an external domain. The only thing it talks to is a **local LLM server you run yourself** (e.g. [LM Studio](https://lmstudio.ai/)) over `localhost`
- Once the models are downloaded, it **runs to completion even with Wi-Fi turned off** (→ [`docs/privacy.md`](docs/privacy.md))

Output goes to `output/<video name>/`:

```
output/<video name>/
├─ transcript/
│   ├─ audio.wav              Extracted audio
│   ├─ transcript.json        Timestamped transcript (structured)
│   └─ transcript.txt         Timestamped transcript (readable text)
├─ frames/
│   ├─ *.jpg                  Extracted frames
│   ├─ frames.json            Index of extracted frames (used for resume)
│   └─ frame_notes.json       Per-frame analysis results (used for resume)
├─ minutes_partials.json      Chunk summaries of the minutes (for long transcripts; used for resume)
├─ structure_used.txt         Auto-generated heading structure from "Auto" mode (copy into templates/ if you like it; see below)
└─ minutes.md                 The minutes (organized into decisions / action items+owners / due dates, etc.)
```

This project was developed by running multiple Claude Code sessions (an implementer and a reviewer) in coordination, with a defined division of roles and review process (→ [Development process](#development-process-ai-assisted-collaboration)).

## Demo

<!-- TODO(phase 2): Process a non-confidential video, add a GUI screenshot and examples/<name>/minutes.md, and link them from here. Do not include information about real meetings that are being processed. -->

_Coming soon. Minutes and screenshots generated from a non-confidential sample video will be added shortly._

## Setup (macOS / Apple Silicon)

```bash
brew install ffmpeg          # required for audio / frame extraction
bash scripts/setup.sh        # creates the virtualenv, installs deps, and fetches the transcription model
cp config.example.toml config.toml
```

- `scripts/setup.sh` does the whole setup in one command: create the virtualenv (`.venv`), install dependencies, and fetch the transcription model from HuggingFace (first run only; offline afterwards)
- Separately, start **LM Studio** (Settings → Local Models → Local Model API: turn on "Local API server"; "Just-in-time model loading" is also recommended)
- For minutes generation, choose an **Instruct-style model that does not do reasoning (thinking), or can have it turned off** (reasoning models are extremely slow and can return empty bodies → [`docs/models.md`](docs/models.md))
- For detailed steps, model selection, and troubleshooting, see [`docs/setup-mac.md`](docs/setup-mac.md) and [`docs/models.md`](docs/models.md)

## Usage

```bash
python src/meeting_minutes/gui.py                 # GUI: pick a video and click "Create minutes". Shows a progress bar and log
python src/meeting_minutes/gui.py --lang en       # Show the GUI text in English (default is Japanese)
python src/meeting_minutes/cli.py meeting.mp4      # CLI: for smoke tests / automation
```

- The GUI display language can also be set via `[gui] language` in `config.toml` (`"ja"` / `"en"`, default `"ja"`); `--lang` overrides it just for that run
- Only the **GUI screen text** changes; the transcription language and the content of the generated minutes are separate (`[transcribe] language` / the LLM side)

(For developers, `python -m meeting_minutes.gui` / `-m meeting_minutes.cli` also work. In that case, either `cd src` or set `PYTHONPATH=src`.)

## Configuration

Adjust via `config.toml` (if you don't have one yet, copy `config.example.toml` to create it). Main keys:

- `[llm] base_url` — the local server endpoint (for Ollama, `http://localhost:11434/v1`)
- `[llm] model` / `[llm] vlm_model` — the loaded model names
- `[transcribe] backend` — `auto` / `mlx` (Apple GPU) / `faster-whisper`
- `[transcribe] model` — default `large-v3-turbo` (fast, high accuracy) / `large-v3` / `medium` / `small`
- `[frames] interval_sec` / `scene_threshold` / `max_frames` — the granularity and cap for frame extraction
- `[output] template_path` — a custom template that replaces the minutes format (heading structure) (see below)
- `[output] auto_structure` — `true` enables "Auto" mode (auto-generates the heading structure to match the video content; see below)

Each value can also be overridden by an environment variable (`MM_LLM_MODEL`, etc.).

### Choosing the minutes format (heading structure)

The **structure** of the minutes (headings and items) can be chosen in three ways. **Quality rules such as "do not fabricate" always apply regardless of which you choose** (the format is only about structure). The same format is used for both single-pass and split generation.

| Mode | Description |
| --- | --- |
| Built-in (default) | Always the same headings regardless of meeting content (decisions / action items / discussion highlights, etc.). Identical to `templates/minutes_template_example.txt` |
| Choose a file | Uses the structure of an external file you prepared, e.g. a client's prescribed format |
| Auto | Has the LLM propose the heading structure each time to match the video content (e.g. a group-tour briefing → "Schedule", "What to bring", "Meeting place / time", "Notes") |

- Set the default in `config.toml` (priority: **`auto_structure=true` > `template_path` > built-in**)
- The GUI's "Minutes format" radio buttons switch it **for that run only** (the three options are mutually exclusive; even with `auto_structure=true` in `config.toml`, choosing "Built-in" or "Choose a file" in the GUI wins)

#### Choose a file (custom template)

Use this to match a client's prescribed format.

- Writing a path in `[output] template_path` in `config.toml` makes that file be used every time
- In the GUI, "Choose a file" → "Choose..." to pick the file. Selecting "Built-in (default)" again reverts at any time
- If the file is missing, unreadable, or empty, it **falls back to built-in with a warning**

Placeholders you can use in a template:

| Placeholder | What is substituted |
| --- | --- |
| `{title}` | The video file name (without extension) |
| `{datetime_hint}` | A hint at the date/time ("（記載なし）" / "not stated" if unknown) |
| `{duration_hint}` | A hint at the recording length ("約 N 分" / "about N min", etc.) |
| `{transcript}` | The full transcript (**optional**. If you do not write it in the template, it is appended automatically at the end) |
| `{frames}` | The frame analysis result (with timestamps. **Optional**. Independently of `{transcript}`, only the one you omitted is filled in) |

The starter is [`templates/minutes_template_example.txt`](templates/minutes_template_example.txt) (identical to built-in). Copying it and rewriting the headings is the quick path.

#### Auto (auto-generate to match the video)

Set `[output] auto_structure = true` in `config.toml`, or choose "Auto" in the GUI.

- Using the transcript (chunk summaries for long meetings) as material, the LLM generates **only the heading structure that fits that meeting**, once. It does this within the token budget and **does not crowd the context used to generate the minutes body** (it reuses the existing split summaries and does not read the full text twice)
- The generated structure is saved to **`structure_used.txt`** in the output folder, with `{title}` and the like left as placeholders
- If generation fails (LLM error, missing placeholder, a structure too large to fit the merge, etc.), it **falls back to built-in with a warning** (`structure_used.txt` is not kept)

**Turn a structure you like into a fixed template** (faster by not having the LLM generate it every time, and the results do not drift):

```sh
cp output/<video name>/structure_used.txt templates/<client name>.txt
```

Then set `[output] template_path = "templates/<client name>.txt"` in `config.toml` (or "Choose a file" in the GUI) to pin that structure from then on.

## Known limitations and next steps

| Limitation | Note |
| --- | --- |
| When transcription is slow | With `backend=auto`, Apple Silicon uses GPU-backed mlx-whisper. Pinning `backend=faster-whisper` runs CPU-only on a Mac, where `large-v3` takes tens of minutes for a one-hour meeting. `medium` / `large-v3-turbo` cut that further |
| Progress does not move under mlx | mlx-whisper returns its result in one go, so the progress bar stays at 0 during transcription and then jumps at the end (faster-whisper updates incrementally) |
| Misrecognition of song / BGM sections | Endless repetition of the same phrase is handled with `condition_on_previous_text=False`. One-off mishearings are a general Whisper-family trait and can remain, so for meetings with a lot of singing or applause, eyeballing the transcript is recommended |
| No speaker diarization | There is no "who spoke" label (everything is written as `参加者` / "participant") |
| Reasoning models are ill-suited to minutes generation | Reasoning models such as Qwen3 / DeepSeek-R1 spend a lot of time and tokens on invisible "thinking" and are extremely slow / return empty bodies. **Use a non-reasoning Instruct model** (an empty response is surfaced as an error saying "used up max_tokens on thinking") |
| Resume from mid-way | Transcription, frame extraction, **frame analysis (per frame)**, and **chunk summaries (per chunk)** reuse intermediate files and only re-run what was redone (disable with `--fresh` / the GUI checkbox). Before processing starts, connectivity to the LLM server is checked so it fails early |
| Interruption is not fully immediate | The GUI "Stop" reacts at stage / frame / chunk boundaries. However, **during an mlx-whisper transcription call** and **during ffmpeg execution** it waits for that stage to finish |
| Settings cannot be changed from the GUI | Edit `config.toml` directly |

Automatic retry when the LLM times out or drops the connection is not implemented yet (re-run to recover for now).

> 💡 **If you just want to run it, you can stop here.** The rest explains the
> pipeline's internal design (the split via `pipeline.run` / `Deps`) and the
> AI-assisted collaborative development workflow.

## How it works

```
video ─▶ audio extract ─▶ transcribe ─▶ frame extract ─▶ frame analysis(VLM) ─▶ minutes generation(LLM) ─▶ minutes.md
        (ffmpeg)        (faster-whisper)  (ffmpeg)         (localhost)             (localhost)
```

- `pipeline.run()` owns this ordering, progress notifications, and output-directory management, and **the GUI, CLI, and tests all just call `run()`**
- Each stage's implementation is swappable via `pipeline.Deps` (see [`docs/architecture.md`](docs/architecture.md) for details)

## Design highlights

- **A single seam (`pipeline.run` + `Deps`)** — separates the UI from the real processing. You can test "stage ordering / progress / output paths" without calling ffmpeg or an LLM.
- **LLM/VLM abstracted behind an OpenAI-compatible API** — swappable between LM Studio / Ollama / others just by changing `base_url`. No dependency on the `openai` package; calls `httpx` directly.
- **Map-reduce for long transcripts** — a one-hour meeting does not fit in the context window, so it switches to a two-stage chunk-summarize → merge. Summaries are written incrementally to `minutes_partials.json`, so a re-run does not redo the finished parts.
- **Per-stage elapsed time shown in the log** — the completion message for audio extraction, transcription, frame extraction, frame analysis, chunk summaries, and the minutes merge each append "elapsed X".
- **Frame analysis is also checkpointed per frame** — `frames/frame_notes.json` is rewritten after each frame finishes, so a mid-way failure does not redo already-analyzed frames.
- **"Waiting for a response" shown during LLM calls** — for stages where the wait is noticeable other than transcription (frame analysis, minutes generation), it is shown together with the model name.
- **Swappable transcription backend** — with `backend=auto`, Apple Silicon uses GPU-backed mlx-whisper and everything else uses faster-whisper. You can also pin it in `config.toml`.
- **Layered configuration** — defaults < `config.toml` < environment variables. TOML is parsed with the standard-library `tomllib`, so there is zero dependency.

For the reasoning behind decisions and their trade-offs, see [`docs/DESIGN.md`](docs/DESIGN.md).

## Tests

```bash
pip install -r requirements-dev.txt
pytest        # 17 files. Includes integration tests that run ffmpeg (auto-skipped where ffmpeg / an LLM is unavailable)
```

## Development process (AI-assisted collaboration)

This project was implemented through collaborative development by multiple Claude Code sessions.

- **worker** — handles implementation, tests, and git operations
- **manager** — reviews the PRs the worker opens and handles merging to `main`. Merges only after checking for leaked confidential data (proper nouns from real meetings), the `.gitignore` exclusions, that the diff stays within the intended scope, and the absence of destructive operations
- The division of roles, the prohibitions, and the review criteria are written out in [`.claude/CLAUDE.md`](.claude/CLAUDE.md) (Claude Code loads it automatically at session start; the rest of `.claude/`, such as the operational session log, is private)
- **the manager does not take the worker's self-report at face value** — for every PR the manager re-runs pytest and the confidential-data grep itself and reviews the diff directly before merging
- **conversation context is not shared between sessions** — the manager does not see the worker's trial and error, and reviews only from the final diff and report
- **the permission boundary actually held** — for operations that need the owner's direct confirmation, such as deleting tracked files, the worker did not act on a relay through the manager alone and has, in practice, held work pending the owner's confirmation
- **an independent review by a model from a different vendor is also built in** — in addition to Claude's (the manager's) judgment, an independent code review by OpenAI Codex (`codex exec review`) was added to the pre-merge checks for every PR and is actually in use
- **loop engineering was put into practice** — rather than a one-shot review, the design repeats implement → independent verification (pytest, confidential-data grep, diff review, Codex review) → send-back → fix → re-verify until every check is clear
- send-backs are specific — each is returned with the exact location, reproduction conditions, and a fix approach
- Example: in Auto mode (Issue #34, PRs #19 / #35), more than six token-budget bugs were fixed over these round trips
- Example: the problem where the settings frame was invisible on a real screen was solved over the four-stage round trip of PRs #33 → #40 → #41 → #42
- **when a class of finding recurs, the operational rules themselves are updated** — not just the individual PRs; the loop is structured to improve itself (example: the old-path guard in `.gitignore` was dropped three times in a row across PRs #48 / #60 / #66, so "always keep the old-path ignore entry" was then written down as a rule)

Because this project handles real meeting data, the practice of grepping tracked files for leaked confidential data before every push / PR is strictly followed.

## License

MIT License. See [LICENSE](LICENSE) for the full text.
