# Architecture

[日本語](architecture_ja.md) | English

## Overall flow

```
video file
  │
  ├─(0) client.preflight           tiny request to the LLM/VLM models; aborts here if they fail
  │
  ├─(1) audio.extract_audio        builds a 16kHz mono wav with ffmpeg
  │        └─ transcript/audio.wav (kept in the same folder as the transcript)
  │
  ├─(2) transcribe.transcribe_wav  timestamped transcription (mlx-whisper / faster-whisper)
  │        ├─ save_transcript → transcript/transcript.json / transcript.txt
  │        └─ if reuse=True and transcript.json exists, reused via load_transcript
  │
  ├─(3) frames.extract_frames      ffmpeg extracts frames on scene changes plus a fixed interval
  │        ├─ save_frame_index → frames/frames.json
  │        └─ if reuse=True and frames/frames.json exists, reused via load_frames
  │
  ├─(4) vision.describe_frames     the local VLM summarizes each frame
  │        ├─ save_frame_notes after each frame → incrementally written to frames/frame_notes.json
  │        └─ if reuse=True, only the remaining frames are analyzed (resumable per frame)
  │
  └─(5) minutes.generate_minutes   transcript + frame notes → minutes Markdown
           ├─ for long transcripts, each chunk summary is saved incrementally to work/minutes_partials.json
           │    → if reuse=True, already-summarized chunks are not redone (resumable per chunk)
           ├─ save_minutes → minutes.md
           └─ save_minutes_docx → minutes.docx (Word version of the same content as .md; a failure here only warns)
```

- This ordering, the progress notifications, and management of the output
  directory (`output/<video name>/`) are all consolidated inside
  `pipeline.run()`; the GUI, CLI, and tests each call `run()` to use it
- `run(..., reuse=True)` (the default) reuses the previous run's intermediate
  artifacts (transcript/transcript.json / frames/frames.json /
  frames/frame_notes.json / work/minutes_partials.json) and does not redo
  what already finished (disable with `--fresh` or the GUI checkbox)
- The progress `stage` sequence is
  `preflight → audio → transcribe → frames → vision → minutes → done`
- Each stage's, chunk's, and frame's completion message carries the measured
  "elapsed X" time
- The start message for a stage that calls the LLM/VLM includes the model
  name in use and a "waiting for a response" note
  (`pipeline._format_elapsed` / `minutes._format_elapsed` /
  `vision._format_elapsed` — small functions duplicated across modules rather
  than shared, since sharing wasn't worth the coupling)

## Module responsibilities

The code is split by role:

- The real processing (Model) lives under `src/meeting_minutes/model/`
- The GUI is `src/meeting_minutes/view/` + `src/meeting_minutes/presenter/`
- The entry points are `gui.py` / `cli.py` / `download_transcribe_model.py`
  directly under `src/meeting_minutes/`
- All 10 modules in the table below other than `download_transcribe_model.py`
  live under `model/` (e.g. `model/pipeline.py` ⇔
  `meeting_minutes.model.pipeline`)

| Module | Role | External dependency |
| --- | --- | --- |
| `model/config.py` | Builds `Config` from TOML + environment variables. Loads prompts. | None (stdlib `tomllib`) |
| `model/cancel.py` | Shared cooperative-cancellation building blocks (`PipelineCancelled` / `check_cancel`) | None |
| `model/ffmpeg_utils.py` | Checks for ffmpeg / ffprobe, runs them, reads video duration | ffmpeg (system) |
| `model/audio.py` | video → wav | ffmpeg |
| `model/transcribe.py` | wav → array of `Segment`, saving, text formatting. `backend` switches between mlx / faster-whisper | mlx-whisper / faster-whisper |
| `model/frames.py` | video → array of `Frame` (timestamped images) | ffmpeg |
| `model/llm_client.py` | `chat` / `describe_image` against an OpenAI-compatible server | httpx, the local LLM server |
| `model/vision.py` | `Frame` → `FrameNote` (summary text) | `llm_client` |
| `model/minutes.py` | `Segment` + `FrameNote` → minutes Markdown. Long transcripts go through chunk-summarize-then-merge | `llm_client` |
| `model/pipeline.py` | Orchestrates every stage, progress, and swapping via `Deps` | all of the above |
| `i18n.py` | Catalog of GUI display text (`{key: {"ja", "en"}}`) and `t(key, language, **kwargs)`. Shared by `view/` and `presenter/` (→ `DESIGN_en.md` §8.5) | None |
| `download_transcribe_model.py` | Fetches the Whisper model for the resolved backend (called from `scripts/setup.sh`) | huggingface_hub / faster-whisper |

## Entry points

`gui.py` / `cli.py` / `download_transcribe_model.py` under `src/meeting_minutes/` are the entry points.

- The GUI is a thin wrapper that assembles `view/` + `presenter/` and starts
  it (→ `DESIGN_en.md` §8.5)
- The CLI / model-fetch script is argparse plus calls into
  `meeting_minutes.model.*` (the fetch script itself sits outside `model/`)
- Both have a `__package__` bootstrap at the top, so they work whether
  launched by file path (`python src/meeting_minutes/gui.py`) or as a module
  (`python -m meeting_minutes.gui`, which needs `cd src` or
  `PYTHONPATH=src`) (→ `DESIGN_en.md` §8.5)

`gui.py` accepts `--lang {ja,en}`.

- If given, it overrides the display language for that run only
- If omitted, it follows `config.toml`'s `[gui] language` (default `ja`;
  invalid values are treated as `ja`)
- Only the GUI's on-screen text is affected (→ `DESIGN_en.md` §8.5)

## Progress notifications

`on_progress(stage, current, total, message)` is used the same way by every stage.

- `stage`: `"preflight" | "audio" | "transcribe" | "frames" | "vision" | "minutes" | "done"`
- `current` / `total`: progress within that stage (`total=0` means unknown)
- The CLI turns this into text lines; the GUI receives it over a `queue` and
  reflects it in the progress bar and log.

## Swapping for tests (`Deps`)

- `pipeline.Deps` holds a function reference for each stage
- Tests pass a `Deps` built from dummy functions into `run(..., deps=fake_deps)`
  to verify stage ordering, the contents of the progress callback, and how
  output paths are built — without calling ffmpeg or an LLM

## Data structures

- `transcribe.Segment(start: float, end: float, text: str)`
- `frames.Frame(timestamp: float, path: Path)`
- `vision.FrameNote(timestamp: float, path: str, description: str)`
- `minutes.MinutesMeta(title, datetime_hint, duration_hint)`
- `pipeline.PipelineResult(...)` — the full set of output paths, counts, and warnings
