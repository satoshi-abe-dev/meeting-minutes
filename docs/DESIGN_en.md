# Design decisions and rationale

English | [日本語](DESIGN.md)

This document records "why it was built this way." For how it's implemented, see
[`architecture_en.md`](architecture_en.md).

---

## 1. Why "fully local"

- **Decision**: Process audio, video, transcription, and summarization
  entirely on the machine. No code connects to an external domain (the
  LLM/VLM are only sent to a `localhost` local server)
- **Reason**: The target user is a workplace where meetings contain customer
  data, unreleased information, or personal information, and recordings
  can't be sent to a cloud STT or generative-AI service. "It doesn't leave
  the machine, even if accuracy drops a little" is itself a functional
  requirement here
- **Trade-offs**:
  - A local LLM's Japanese summarization quality is behind the best cloud models
  - `large-v3` transcription is slow on a Mac when run on CPU
  - The deploying side needs a fair amount of RAM

  These are accepted in exchange for "nothing leaves the machine."

---

## 2. The pipeline's single seam: `pipeline.run()` + `Deps`

- **Decision**: Every stage is consolidated into one function,
  `pipeline.run(video_path, config, on_progress, *, deps)`. Each stage's
  implementation is injected as a function field on a `Deps` dataclass; the
  default is the real implementation, and tests pass in dummies
- **Reason**:
  - The GUI, CLI, and tests can share the same entry point, so stage
    ordering and how output paths are built live in exactly one place
  - "Are stages called in the right order," "what's in the progress
    callback," and "how `output/<video name>/` is assembled" can all be
    verified (`tests/test_pipeline.py`) without setting up ffmpeg or an LLM
    server
  - Because a dataclass's `__init__` assigns each function to an instance
    attribute, plain functions can be used directly as defaults without
    turning into bound methods (descriptors), so swapping them stays
    straightforward
- **Rejected alternatives**:
  - Mock-free integration tests only: this would make ffmpeg and a local LLM
    required, which is hard to run in CI and hard to isolate failures in
  - Swapping each stage via an abstract base class + subclasses: a single
    function per stage is enough for now; a class hierarchy would be
    overkill. A dataclass of function fields is sufficient

---

## 3. Progress notifications: `on_progress(stage, current, total, message)`

- **Decision**: Every stage calls back with the same signature. It carries no UI knowledge
- **Reason**: The CLI turns this into text lines; the GUI puts it on a
  `queue.Queue` and hands it to the UI thread — the receiver can be swapped
  either way. Since tkinter can't touch widgets directly from a worker
  thread, this separation — "progress travels as data, the UI thread is the
  one that reflects it" — was necessary
- **Trade-off**: `total` is an estimate, not an exact value (e.g. the number
  of transcription segments is estimated as `video length ÷ 4 seconds`). The
  progress bar shows "roughly" where things stand. Not appearing to freeze
  was prioritized over exactness

---

## 4. Abstracting the LLM/VLM behind an OpenAI-compatible API

- **Decision**: `llm_client.py` calls the OpenAI-compatible
  `/v1/chat/completions` directly with `httpx`. There is exactly one place
  the endpoint is configured: `config.ai.base_url`
- **Reason**:
  - LM Studio, Ollama, and other OpenAI-compatible servers can all be
    swapped in just by changing `base_url`. Nothing is locked to a specific
    GUI app
  - Two methods are enough — `chat` (text) and `describe_image` (sends an
    image as a data URL) — and the response is read from
    `choices[0].message.content`. There's no complexity here that would
    justify adding the `openai` package; thinner dependencies are better
- **Rejected alternative**: going through the `openai` package. It would
  work, but for this use case it only adds a dependency and version
  constraints without buying much

**A timeout has to distinguish "the server is dead" from "the response is just taking a while":**

- Bug: the final merge step of minutes generation (which produces more
  output tokens than a partial summary) would fail once it exceeded the
  default 180-second timeout
- Cause: this wasn't a server problem — a larger model's generation simply
  took a while
- Fix: `httpx.TimeoutException` (a kind of `RequestError`) is now caught
  first, and instead of "please start the server," a dedicated message says
  "please increase `timeout`." The default `timeout` was also raised from
  180 to 600 seconds

**Reasoning models are excluded from minutes generation (a prerequisite):**

- Bug: with `qwen/qwen3.8-27b` (a Qwen3-family reasoning model), chunk
  summaries were saved with only the `### Part N` heading and an empty body
- Cause: the reasoning model writes its invisible "thinking" into
  `message.reasoning_content`, uses up `max_tokens` there, and returns with
  the visible `message.content` still empty and `finish_reason=length` (HTTP
  status is 200, so this is easy to miss)
- Measured: for a chunk of about 5000 characters, 4129 characters of
  thinking, and 433 seconds per chunk even on success — impractical
- Response:
  - `llm_client` now detects "`content` empty but `reasoning_content`
    present" and raises an explicit error: "max_tokens was used up by
    thinking; increase it or lower reasoning"
  - Chunk summarization's `max_tokens` changed from a fixed 1500 to
    `config.ai.max_tokens`
  - `minutes._load_partials` now invalidates entries with an empty body
    (a corrupted `work/minutes_partials.json` is automatically rebuilt on
    the next run)
  - The default `max_tokens` was raised from 4096 to 8192
- Since the root cause is "using a heavy reasoning model for minutes
  generation," **using a non-reasoning Instruct-style model was documented
  as a prerequisite** (`docs/models.md`). Auto-appending `/no_think` was
  considered, but that's Qwen3-specific syntax that doesn't work on other
  vendors' reasoning models (each model turns reasoning off differently), so
  it wasn't added to the code

---

## 5. Map-reduce for long transcripts (`minutes.py`)

- **Decision**: When the transcript text exceeds a threshold
  (`_CHUNK_TRIGGER_CHARS`), switch to a two-stage approach: split the
  segments into chunks → summarize each chunk into bullet points → generate
  the final minutes from that set of summaries plus the frame notes. Below
  the threshold, one call is enough
- **Reason**: The transcript of a one-hour meeting often doesn't fit in a
  local LLM's practical context length. Staged summarization loses fewer
  decisions than handing over the whole thing and letting it get truncated
- **Trade-off**: more LLM calls, more time. Chunk summarization can also lose
  some detail. The threshold is an empirical value aimed at "one call
  whenever it fits," and it may need adjusting for a different model

**Threshold update (2026-09):** in real use, `_CHUNK_TRIGGER_CHARS=12000` was
too small — even an ordinary meeting well under 1–2 hours got split, and the
minutes ended up as a "summary of a summary," losing a lot of specificity.
The defaults were raised to 40000/15000, tuned for running the LLM at a 32k
context, with the policy now being **one-shot generation whenever it fits**,
falling back to map-reduce only for very long meetings.

**This assumption needed to be documented and made configurable (2026-09,
Issue #16):** "assumes a 32k context" was only a code comment — there was no
setup procedure in `docs/models.md`, so users who hadn't raised the Context
Length in LM Studio hit HTTP 400 (context length too small) on one-shot
generation. Response:

- `docs/models.md` now documents setting Context Length to 32768 or higher
- `llm_client` detects this 400 and turns it into a hint that explains the cause
- The threshold became adjustable via `config.toml`'s `[ai] chunk_trigger_chars` /
  `chunk_size_chars`, so a model with a smaller context can be lowered to lean on
  split mode (`minutes.py`'s `_CHUNK_TRIGGER_CHARS` / `_CHUNK_SIZE_CHARS` are just the defaults)

**The character-count threshold itself was still too loose (2026-09, Issue
#18):** even with Context Length correctly set to 32768, leaving
`_CHUNK_TRIGGER_CHARS=40000` at its default meant one-shot generation could
still exceed the limit and hit HTTP 400 again. Root-causing this:

- For the Qwen tokenizer family, Japanese measures at about **0.74
  tokens/character** (measured; the code comment's "1–1.5" was an
  overestimate). 40000 characters ≈ 30k tokens
- On top of that, **`frames_text` (the concatenated frame analysis) turned
  out to dominate** — a real case with 60 frames ran to about 18k tokens,
  larger than the transcript itself
- `max_tokens=8192` (the default meant to counter reasoning models) was
  being used directly as the response reservation

Response (`minutes.py` / `llm_client.py` / `pipeline.py`):

- `pipeline` now reads the **loaded model's actual context length**
  (`loaded_context_length`) from LM Studio's `/api/v0/models` and passes it
  to `generate_minutes(context_tokens=...)`
- When the real context length is known, `generate_minutes` decides
  one-shot vs. split with
  `estimated prompt tokens + response reservation + margin <= context_tokens`
  (the character-count threshold is demoted to a fallback for when this
  isn't available; defaults 20000/12000)
- `frames_text` is truncated to roughly 1/3 of the context (with a 6000-token floor)
- The response reservation for the minutes body is capped at
  `_MINUTES_RESPONSE_TOKENS` (5000) rather than `max_tokens` (plenty for a
  template-filling task; reasoning models aren't a target here)
- When the real context length can't be read, `[ai] context_tokens` can
  still be set directly

Tests based on these measurements were added to `test_minutes.py` / `test_llm_client.py`.

**Each chunk summary is saved to `work/minutes_partials.json` as soon as it finishes:**

- Bug: there were cases where only the final merge (after both chunk
  summaries had already finished, and which produces more output tokens)
  would time out
- Problem: even though the chunk summaries themselves had finished fine,
  `generate_minutes()` was a single call, so any failure meant redoing
  everything
- Fix: `_save_partials()` now writes completed work to disk as it goes, and
  on a re-run with `reuse=True` (the default), `_load_partials()` reads it
  back so finished chunks aren't re-summarized
- A call that doesn't pass `out_dir` (e.g. in tests) simply disables this
  persistence — behavior is otherwise unchanged

**The saved file carries a signature for the chunk boundaries (2026-09, from a Codex review finding):**

- `work/minutes_partials.json` also stores `chunk_size_chars` and the total
  segment count of the split input (`format: 2`)
- On resume, if these don't match the current settings, the cache is
  discarded and summarization starts over from scratch
- Without this, lowering `chunk_size_chars` to work around a context-length
  issue and then resuming would apply old-boundary partial summaries to the
  new chunk sequence purely by count, silently duplicating or dropping
  content in the minutes with no error
- The old format with no metadata (a plain JSON array) can't have its
  boundaries validated either, so it isn't used anymore

**The minutes "structure" can be swapped via an external template (2026-09, Issue #21):**

- To support a customer's own required format, `_MINUTES_TEMPLATE` was split
  into a "structure" (`_MINUTES_STRUCTURE`) and "input sections"
  (`_INPUT_TRANSCRIPT` / `_INPUT_FRAMES`)
- `config.toml`'s `[output] template_path` swaps the structure
  (`pipeline` passes it from config into `generate_minutes(template_path=...)`)
- The path for a one-off runtime override was first added as a GUI button
  plus a CLI `--template` flag, then removed, then finally brought back as
  the GUI's "minutes format" dropdown (`ttk.Combobox`)
- The fixed first item is the `config.toml` setting (either "Built-in
  (default)" or the file name); "Choose a file..." swaps it for that run
  only; reselecting the first item reverts it. The CLI's one-off override
  was not brought back (`config.toml` is sufficient)
- User-facing terminology is standardized on "Built-in (the default
  format)," with a tooltip explaining it's "a fixed structure that does not
  change dynamically with the meeting content"
- **The system prompt (the no-fabrication rule etc. in
  `prompts/minutes_ja.txt`) always applies regardless of the template.** The
  template only covers structure — quality rules never get pulled into a
  customer's format
- Substitution is neither sequential `.replace` calls nor `.format` — it's a
  single pass over the template with one bulk substitution
  (`re.sub` + a callback, `_fill_minutes_template`). Substituted values (like
  the transcript) are not re-scanned, so a string that happens to look like
  `{frames}` inside the transcript text is never caught up in it. A bare
  `{ }` that isn't a known placeholder name (e.g. inside a JSON example) is
  left untouched
- If the template doesn't include `{transcript}` / `{frames}`, **only
  whichever is missing** is appended at the end (writing `{transcript}` but
  forgetting `{frames}` doesn't make the frame information disappear). A
  customer only needs to write their output format
- Missing, unreadable, or empty file → falls back to the built-in structure
  without stopping on an error, and warns via `on_progress`
  (`load_minutes_structure`)
- The same `structure` is used in both the one-shot path and the merge step of the split path

**The GUI's format picker became 3 exclusive radio buttons (2026-09, Issue #34 PR-B):**

- Problem: the `ttk.Combobox` above had issues — "the selected item stays
  highlighted and is awkward to reselect," "the width doesn't match the
  content"
- Fix: replaced it with 3 mutually-exclusive `ttk.Radiobutton`s (Built-in
  (default) / Choose a file / Auto)
- `_start()` sets `output.auto_structure` and `output.template_path` **every
  time** from the radio state (even if `config.toml` has
  `auto_structure=true`, the GUI's explicit selection wins — the 3 radio
  buttons represent exactly one state)

**"Auto" mode (2026-09, Issue #34):** a third option that has the LLM
propose the heading structure itself, matched to the video's content.
`config.toml`'s `[output] auto_structure` (priority order: `auto_structure`
> `template_path` > built-in).

- **Doesn't reopen the token-budget problem** (avoiding a repeat of the
  Issue #16/#18/#19 kind of issue). Structure generation happens only once.
  If the full transcript fits in a "light budget for structure generation"
  (the response reservation is the same `minutes_max_tokens` as the minutes
  body), it's used directly as material; if not, the **existing map-reduce
  chunk summaries are reused** (`_summarize_chunks` runs at most once — no
  new full-transcript-reading path was added). Long material is truncated to
  the structure-generation budget by `_fit_structure_material`
- **The generated output leaves placeholders as literal text** (this is
  explicit in the prompt). It's saved to
  `output/<video name>/work/structure_used.txt`, which can be copied
  straight into `templates/` to become a fixed template
- **The fallback is always the built-in structure** (`_MINUTES_STRUCTURE`,
  even when a file was specified). Failure conditions: an LLM exception, an
  empty response, a missing required placeholder (`{title}`
  `{datetime_hint}` `{duration_hint}`), or the structure alone exceeding the
  merge budget (`_structure_fits_minutes_skeleton`; rejected outright since
  switching to split mode wouldn't save it). On failure,
  `work/structure_used.txt` is not left behind (leftovers from a previous
  run are also removed)
- After swapping in the structure, the budget (`one_pass` / `frames_text` /
  `size_chars`) is recomputed by `_minutes_budget`. Right before the merge
  step, the actual budget — including the real `merged_transcript` plus
  frames — is checked again, and anything over is truncated from the end by
  `_fit_merged_transcript`
- A `check_cancel` was also added right after structure generation (a heavy LLM call)

**Minutes are always output as both `minutes.md` and `minutes.docx`
(2026-09, Issue #13):** driven by requests to open it in Word or distribute
it as-is. No toggle was added to the GUI — both are always produced (so
users don't have to think about it). Conversion lives in a new module,
`model/docx_export.py`.

- **No external binary like `pandoc` is used.** Only `python-docx` (pure
  Python) was added as a dependency. The libraries it pulls in (`lxml`,
  `typing_extensions`) all ship prebuilt wheels for every OS, so CI stays a
  plain `pip install` on all 3 OSes with no new system dependency
- **No general-purpose Markdown parser was added.** Only the Markdown subset
  the minutes actually use (ATX headings / `- ` or `* ` bullet lists
  (nested) / GFM pipe tables / fences / `**bold**` / `` `code` ``) is
  converted by a small line-based state machine. **Unknown lines fall
  through as plain paragraphs**, so a different structure from "Auto" mode
  or a custom template doesn't break the conversion. The conversion itself
  never raises
- **`.md` is the primary artifact.** `pipeline.py` calls `save_minutes_docx`
  right after `save_minutes`, wrapped in `try/except` — if the conversion or
  write fails, it just warns (`pmsg.warn_docx_failed`) and continues.
  `.docx` goes to `output/<video name>/minutes.docx` (at the root, not under
  `minutes/`, since it isn't an intermediate artifact)

---

## 6. Transcription backends (faster-whisper / mlx)

- **Decision**:
  - `transcribe.py` has two implementations, chosen via `config.backend`
  - `transcribe_wav()` is a thin dispatcher that calls
    `_transcribe_faster_whisper()` or `_transcribe_mlx()` based on
    `resolve_backend()`'s result
  - The signature stays the same, so `pipeline.Deps` and the existing tests needed no changes

The two implementations, compared:

- **faster-whisper (CTranslate2)** — works on any OS. Japanese accuracy is
  practical, and `vad_filter` is available. However, it **can't use Apple
  Silicon's GPU and runs on CPU on a Mac** — tens of minutes to an hour for a
  one-hour meeting with `large-v3`
- **mlx-whisper** — uses the Apple GPU and is much faster on a Mac. It's in
  `requirements.txt` behind an environment marker, so pip skips it on
  anything other than Apple Silicon

**Resolution rule for `backend = "auto"` (the default)**: if
`sys.platform == "darwin"` and `platform.machine() == "arm64"` and
`mlx_whisper` can be imported, use `"mlx"`; otherwise use `"faster-whisper"`.
An explicit choice (`"mlx"` / `"faster-whisper"`) is always honored as-is.

**How `model` is handled:**

- The user specifies only a size name (`large-v3` / `large-v3-turbo` /
  `medium` / `small`)
- Each backend converts it to the real thing (faster-whisper uses it as-is;
  mlx maps it through `_MLX_MODEL_MAP` to `mlx-community/whisper-<size>`)
- A string containing `/` is passed through as-is, treated as a full HF repo name
- Unlike the LLM, where `model` / `vlm_model` are two separate keys, this
  uses "one logical name + a lookup table" instead (to reduce the number of
  choices the user has to make)

**Deliberate limitations on mlx:**

- mlx-whisper returns its result all at once (it's not a generator), so no
  incremental progress can be shown while transcribing
- An explanatory message is shown once at the start, and `on_progress` is
  driven while converting segments after completion (the progress bar jumps
  from 0 to 100)
- Incremental display would require chunking the audio ourselves, which is out of scope here

**The model must be pre-fetched; runtime forces offline mode:**

- The Whisper model (default `large-v3-turbo`, about 1.6GB) is not shipped
  in the repo — it goes into HuggingFace's shared cache
  (`~/.cache/huggingface/hub/`). It's fetched **only once, during setup**
- `scripts/setup.sh` calls
  `python src/meeting_minutes/download_transcribe_model.py` right after
  creating the venv and installing dependencies
- This fetch script only downloads the model matching
  `resolve_backend()`'s result, and is a no-op if it's already there (idempotent)
- **The fetch is required — a failure fails the whole setup via `set -e`**
  (previously, a failure was silently swallowed with guidance to
  "auto-download on first run" — that's no longer how it works)

The app never communicates externally at runtime (`cli.py` / `gui.py`). Two layers of defense:

1. **Environment variables** — the entry points `setdefault` `HF_HUB_OFFLINE`
   / `TRANSFORMERS_OFFLINE` before any HuggingFace library is imported. This
   lives in `cli.py` / `gui.py`, not `__init__.py` (since the fetch script
   itself imports the same package — putting it in `__init__.py` would make
   the fetch script itself offline too, and it wouldn't be able to fetch the
   model). Because it's `setdefault`, a user who explicitly set `0` has
   their intent respected
2. **A runtime guard independent of environment variables** — each backend
   explicitly checks whether the cache has the model before running, and if
   it hasn't been fetched, stops with `ModelNotAvailableError` (an i18n'd
   message) instead of auto-downloading. Both the CLI and GUI catch this
   exception and show it as a single line, so no stack trace reaches the
   screen. Note that `config.model` can also be a **local model directory
   path**, not just a size name or HF repo ID (for air-gapped
   distribution) — both guards skip HF resolution when it's an existing
   directory

LM Studio's LLM/VLM are managed separately, so neither the model-fetch script nor offline enforcement applies to them.

**Handling repetition loops from mishearing:**

- Bug: in stretches with singing or background music, the shared default
  `condition_on_previous_text=True` (both faster-whisper and mlx-whisper
  feed the previous window's output back in as context) backfired, causing a
  hallucinated phrase to repeat for dozens of lines
- Fix: both backends now explicitly set `condition_on_previous_text=False`
  (no longer dragged along by a previous window's mistake)

**Remaining limitations:**

- A single-line hallucination (a Whisper-family classic, like "thanks for
  watching") can still occasionally slip in during full silence or singing
- This is a known quirk of Whisper models in general that the default
  `no_speech_threshold` / `logprob_threshold` can't fully prevent — it stops
  "the same line repeating endlessly" but can't get "an occasional one weird
  line" to zero
- For a meeting with a lot of singing, applause, or background music, reviewing the transcript by eye is recommended

---

## 7. Layered configuration (`config.py`)

- **Decision**: priority order is default values < `config.toml` (or
  `config.example.toml` if there is none) < environment variables (the `MM_`
  prefix). Unknown TOML keys are silently ignored
- **Reason**:
  - This lets a team distribution be edited per-user via `config.toml`,
    while CI or a one-off override uses environment variables
  - Ignoring unknown keys keeps a config file forward-compatible with future versions
  - TOML can be read with the standard library's `tomllib` (Python 3.11+), so no dependency was added

---

## 8. Why tkinter

- **Decision**: the GUI is built with tkinter
- **Reason**: it ships with CPython, so there's zero extra dependency — easy
  to hand to a non-engineer as "clone the repo, then
  `python src/meeting_minutes/gui.py`." It's also the same stack as another
  project in the same workspace, sharing the learning cost
- **Rejected alternative**: a web UI (Flask + browser, etc.). That means
  managing a server process and a port, and more browser-dependent
  explanation — it doesn't fit the "fully local, single self-contained
  distribution" policy

---

## 8.5 Main refactoring history (2026-09)

Internal cleanup as part of moving toward the MVP structure. Behavior was not changed (refactor only).

- **Model-View-Presenter split** (Issue #49) — split the widget
  construction, screen logic, and processing kickoff that had all been
  mixed into the single `gui.py` class into `view/` (the Tkinter
  implementation), `presenter/` (screen logic, tkinter-independent), and
  `model/` (the actual processing)
- **Moved entry points under src/** (Issue #51) — moved `gui.py` etc. from
  the repo root into `src/meeting_minutes/`. Both launching by file path and
  by `-m` are supported (prioritizing the "clone and one command" simplicity
  for non-engineers, so the primary guidance still uses the file-path form)
- **Consolidated the Model layer into model/** (Issue #53) — moved the 10
  processing modules into a symmetric `view/` + `presenter/` + `model/`
  layout
- **GUI display language ja/en support** (Issue #55) — made only the GUI's
  on-screen text switchable (transcription language, the LLM prompt, and the
  minutes content are unaffected). Chosen once, at startup

---

## 9. Test strategy

- **Pure logic gets ordinary unit tests** — frame thinning (`_thin_by_gap` /
  `_cap_count`), chunk splitting (`_split_segments`), and config resolution
  (`load_config`) are all verified with no external dependency
- **External-process dependencies can be skipped** — tests that actually
  call ffmpeg use the `needs_ffmpeg` marker plus `skipif`, and are skipped
  in an environment without ffmpeg
- **LLMs are stubbed in** — the `minutes` / `pipeline` tests pass in a fake
  client or `Deps`, needing neither the network nor an actual model
- **GUI logic is tested with an injected FakeView** — the `MainPresenter`
  tests (`test_gui_presenter.py`) pass in a fake implementation of
  `MainView`, verifying progress-percentage calculation, resolving the
  3-way format choice, and button state transitions without ever starting
  tkinter (Issue #49, §8.5)
- In an environment that has ffmpeg, integration tests that actually extract
  frames from a generated test clip also run (this is also where the
  `-vsync` → `-fps_mode` regression is caught)

---

## 9.5 Preflight checks and mid-run resume

**What prompted this:**

- After spending several minutes transcribing a one-hour meeting, LM Studio
  returned "No models loaded" at the frame-analysis stage, and the whole run
  was wasted
- For a long-running process, "failing late" and "starting over from zero on
  failure" both hurt

**Preflight check (the `"preflight"` stage of `pipeline.run`):**

- Before extracting audio, it calls
  `client.preflight([config.ai.llm_model, config.ai.vlm_model])`
- This sends a tiny `max_tokens=1` request to each model, and **aborts
  immediately** if even one fails — so "go load the model" is known before
  waiting several minutes
- As a side effect, this also triggers LM Studio's Just-in-time loading early
- If `llm_client` finds `No models loaded` / `model_not_found` /
  `"param": "model"` in a 400 response body, it adds a hint: "turn on JIT /
  load it under Loaded Instances / make the model name match the Library"

**Mid-run resume (`run(..., reuse=True)`, ON by default):** all intermediate
files are reused under the same `reuse` flag, **skipping whatever already
finished and running only what's left**.

- If `transcript.json` exists, it's read back via `load_transcript` and transcription is not redone
- If `frames/frames.json` exists, it's read back via `load_frames` and frame extraction is not redone
- If `frames/frame_notes.json` exists, frames already analyzed up to that
  point are skipped **one frame at a time**
  (`vision.describe_frames` reads it back on startup and rewrites it after
  each frame finishes). The original design didn't reuse this — the idea
  was to redo everything right after a VLM failure — but redoing all 60
  frames every time turned out more costly, so it was brought in line with
  the other intermediate files' "persist per unit, resume from there"
  approach
- If `work/minutes_partials.json` exists (chunk summaries, see §5), chunks
  that already finished are not re-summarized

To start over from scratch, use the CLI's `--fresh` or the GUI's checkbox to
set `reuse=False` (this ignores every intermediate file and rewrites
everything from the beginning).

**Elapsed time is shown for each stage, chunk, and frame:**

- Knowing "how much longer" matters for a long-running process
- `pipeline.py` / `minutes.py` / `vision.py` each have a `_format_elapsed()`
  that embeds a `time.monotonic()`-measured duration into the completion
  message (e.g. "Transcription complete (444 segments, took 12m34s)")
- The time is folded into the message string itself, so the log display
  side (GUI/CLI) needed no changes, and the shape of the progress event
  (stage, current, total, message) wasn't changed either

**The model name in use and "waiting for a response" are embedded in the same message:**

- Every message right before calling the LLM/VLM (preflight, transcription
  start, frame analysis in progress, partial summaries, minutes merge) now
  includes the model name in use plus "waiting for a response"
- Two goals here: being able to tell which model is currently running
  without looking at the GUI's settings panel, and making sure the screen
  doesn't look frozen during a blocking HTTP call (a few seconds to a few
  minutes; no streaming is used)
- For the same reason as the elapsed-time display, this is also achieved by
  folding it into the message string — the shape of the progress event is unchanged

---

## 9.6 Cancellation (interrupt) design

- **Decision**: added a "Stop" button to the GUI. The implementation is
  **cooperative cancellation** — a shared `threading.Event` is checked at
  points along the long-running process, and when it's set, a
  `PipelineCancelled` is raised and unwound
- **Reason**: a Python thread can't be force-stopped from outside
  (`Thread.kill()` doesn't exist). The only natively safe approach is "the
  processing code exits on its own, at a point it knows is safe." Threading
  a `cancel_event` through, the same way `on_progress` is already threaded
  through every function, made this addable without disturbing the existing
  structure

**Why `cancel.py` is its own module:**

- Putting `PipelineCancelled` in `pipeline.py` would create a circular
  import, since `transcribe.py` / `vision.py` / `minutes.py` — all imported
  by `pipeline.py` — would need to import back from `pipeline` to use it
- A dependency-free `cancel.py` was split out instead, so everyone imports from there

**Where the check is placed (and how fast it responds):**

| Location | Response speed |
| --- | --- |
| Before each stage starts | Nearly immediate |
| The frame-analysis loop (between VLM calls) | Within one frame — the one that feels most responsive in practice (up to 60 checks) |
| The minutes chunk-summarization loop | Within one chunk |
| faster-whisper's segment-generation loop | Within one segment (it's a lazy generator, so cutting it off partway also stops later generation) |
| During an mlx-whisper transcription call | **Does not respond** (one blocking call that returns the whole result at once; only checked before the call) |
| While an ffmpeg subprocess is running | **Does not respond** (`subprocess.run` just waits; switching to `Popen` would make this cancellable, but that was left for later) |

**What's left behind on cancellation:**

- The LLM client is always closed, in a `finally`
- Beyond `transcript/transcript.json` / `frames/frames.json`, **the frame
  analysis's progress so far (`frames/frame_notes.json`) is also saved per
  frame**, so the next run (via §9.5's resume mechanism) doesn't redo frames
  already analyzed
- Likewise, chunk summaries for the minutes (`work/minutes_partials.json`) resume from wherever they left off

---

## 10. Known weaknesses (an honest inventory)

| Weakness | Current state | Next step if pursued |
| --- | --- | --- |
| ~~Can't resume~~ (addressed) | Transcription, frame extraction, frame analysis (per frame), and chunk summarization (per chunk) all skip recomputation (`reuse=True` by default). `client.preflight()` checks LLM connectivity before processing starts, for early failure | Automatic retry when the LLM goes down |
| No speaker separation | Everyone is labeled "participant" | Add speaker separation (e.g. pyannote) as an optional stage |
| Frame analysis assumes a VLM | No lightweight OCR-only path | Make `vision.describe_frames` swappable and add an OCR backend |
| The progress total is an estimate | Looks "roughly" right | Add a light pre-analysis pass to get closer to the real count |
| No settings from the GUI | Edit `config.toml` directly | Add a settings panel to the GUI |
| Thin error recovery | An LLM going down just aborts | Retry, or an explicit resume guide |
