# Recommended models and rough spec guidance

English | [日本語](models.md)

- Pick based on your environment (Mac RAM, Apple Silicon generation). The
  examples here are ones that were readily available and handle Japanese
  reasonably well as of 2026
- **The model name has to match the model ID your LLM server actually
  returns**, written into `config.toml` (for LM Studio, the model key shown
  in **Library**; more generally, the `id` from `curl <base_url>/models`)

## Transcription

- Transcription uses **OpenAI Whisper** (an open-source speech recognition model)
- `config.toml`'s `[transcribe] model` takes a Whisper **size name**:
  `tiny` / `base` / `small` / `medium` / `large-v3` / `large-v3-turbo`
  (`large-v3-turbo` is a faster version of `large-v3`)
- Each backend (below) resolves the size name and fetches it from HuggingFace. Resolution rules:

- `mlx` — rewrites the size name to `mlx-community/whisper-<size>`
- `faster-whisper` — passes the size name straight to the `faster_whisper`
  library, which resolves it to a CTranslate2-converted repo
- A full HF repo name containing `/`, or a local model directory path, is
  used as-is

| Model | Rough size | Use |
| --- | --- | --- |
| `large-v3-turbo` | ~1.6GB | **Default**. Near-`large-v3` accuracy, fast. Especially effective on mlx. |
| `large-v3` | ~1.5–3GB on disk | When accuracy is the top priority. |
| `medium` | ~1.5GB | Balance of speed and accuracy. |
| `small` / `base` | a few hundred MB | Drafts, smoke tests, underpowered machines. |
| 4-bit quantized (e.g. `mlx-community/whisper-large-v3-mlx-4bit`) | ~0.5GB | To keep the first download light. Japanese accuracy may drop slightly. |

There are two backends, chosen via `config.toml`'s `[transcribe] backend` (default `auto`).

| backend | runs on | speed | notes |
| --- | --- | --- | --- |
| `mlx` | Apple Silicon GPU | fast | Mac-only (`mlx-whisper`). The default `auto` picks this on Apple Silicon |
| `faster-whisper` | CPU (on a Mac) | slow | works on any OS; the `auto` fallback |

- For faster-whisper, `compute_type` is `int8` for CPU, and `device = "auto"` leaves the choice to the library
- Both settings are ignored by the `mlx` backend

### Fetching and storing the model

- Models are stored under `~/.cache/huggingface/hub/` (change with `HF_HOME`). They are not shipped in the repo.
- `scripts/setup.sh` (internally, `python src/meeting_minutes/download_transcribe_model.py`) fetches the default model during setup. If it's already there, this is a no-op (idempotent). The fetch is required — `setup.sh` exits with an error if it fails.
- `download_transcribe_model.py` reads `config.toml` (or `config.example.toml` if there is none) for `[transcribe] backend` / `model` and fetches the matching model. If you change these from the defaults, re-run it to fetch the new combination (→ [`setup_en.md`](setup_en.md) §4).
- At runtime (`cli.py` / `gui.py`), offline mode is forced, so a missing model is never auto-downloaded. Starting transcription without it stops with "the transcription model (…) was not found locally." Fetch it with `bash scripts/setup.sh` or `python src/meeting_minutes/download_transcribe_model.py` (using the venv's Python — → [`setup_en.md`](setup_en.md) §2).
- The download happens **once per machine per user**, at setup time.

## Frame analysis (VLM)

| Example | Memory needed (rough) | Notes |
| --- | --- | --- |
| `qwen2-vl-7b-instruct` | 10–16GB | Relatively strong on Japanese text in slides. |
| `qwen2-vl-2b-instruct` | 6–8GB | Lightweight; fine when only the gist is needed. |

If there's ever a need to skip the VLM and rely on OCR alone, that can be
added by swapping out `vision.describe_frames` (currently the VLM is assumed).

## Minutes generation (text LLM)

### Model choice prerequisite (important)

**Pick a model that does not reason (think), or that lets reasoning be
turned off.** Using a reasoning model as-is burns large amounts of tokens and
time on invisible "thinking," and can make things **extremely slow, or return
an empty body for the minutes** (measured: over 7 minutes per chunk with a
reasoning model, and the body came back empty).

- Qwen3 family (`qwen3-*`): fine if LM Studio's reasoning setting lets you
  turn reasoning OFF. Avoid it if that's not possible.
- DeepSeek-R1 family: reasoning cannot be disabled, so avoid it.
- **A plain Instruct model is recommended** (e.g. `qwen2.5-7b-instruct`).
- An empty response surfaces as an error saying "max_tokens was used up by
  reasoning," so at least you'll notice (increasing `max_tokens` in
  `config.toml` still leaves the speed problem).

### Rough sizing

| Class | Examples | Memory needed (4-bit quantized, rough) |
| --- | --- | --- |
| 7–8B | `qwen2.5-7b-instruct`, `llm-jp-3-7.2b-instruct`, the `elyza-japanese-llama` family | 8–12GB |
| ~14B | `qwen2.5-14b-instruct` | 12–20GB |
| ~32B | `qwen2.5-32b-instruct` | 24GB+ |

- Minutes generation is "fill in a fixed template," so 7–8B is already practical
- Consider 14B or larger if missed decisions or inconsistent formatting become a concern

### Setting the context length (important)

([`setup_en.md`](setup_en.md) §3 tells you to do this; this is the detailed version.)

- The instructions below assume **LM Studio**. Auto-detecting the loaded
  model's context length (`/api/v0/models`) is also LM-Studio-specific. On
  other OpenAI-compatible servers, use the manual `chunk_*` / `context_tokens`
  settings described under "If it still doesn't fit / on a non-LM-Studio
  backend" below
- Minutes generation tries to pass the full transcript plus the frame notes
  to the LLM in **a single request** wherever possible (splitting turns it
  into a "summary of a summary," which loses specificity)
- LM Studio loads a model with a **small default Context Length unless you
  explicitly set it**, so without that, minutes generation fails with:

```
LLM server returned an error (HTTP 400):
{"error":"The number of tokens to keep from the initial prompt is greater than
the context length. Try to load the model with a larger context length,
or provide a shorter input"}
```

**Fix: when loading this LLM in LM Studio, set Context Length to 32768 or higher.**

1. In the model-loading bar → the target model → set Context Length to 32768 or higher, then load
2. If it's already loaded, Eject it first and reload with that setting
   (Qwen2.5-7B and 14B both support 32k+)

**Automatic adjustment built into the tool:**

- It reads the loaded model's actual context length from LM Studio's `/api/v0/models`
- If the transcript + frame notes + response headroom is estimated not to
  fit, it automatically switches to split generation (chunk-summarize, then
  merge)
- If the frame analysis results are too large, they're truncated to roughly
  1/3 of the context
- Token counts are estimated from measured Japanese-text behavior of the
  Qwen tokenizer family (**~0.75 tokens/character**, rounded up to 0.8 to
  stay on the safe side)

**If it still doesn't fit / on a non-LM-Studio backend:**

- Lower `config.toml`'s `[ai] chunk_trigger_chars` and `chunk_size_chars`
  (defaults 20000 / 12000; e.g. `chunk_trigger_chars = 8000` /
  `chunk_size_chars = 6000`)
- On a backend where the real context length can't be auto-detected, setting
  `[ai] context_tokens` to the actual value (e.g. 32768) enables
  token-based decisions

## Recommended setup by memory size

| Mac RAM | Transcription (mlx if backend=auto) | VLM | LLM |
| --- | --- | --- | --- |
| 16GB | `medium` or `large-v3-turbo` | 2B VLM (or skip the VLM) | 7–8B |
| 24GB | `large-v3-turbo` or `large-v3` | `qwen2-vl-7b` | 7–8B |
| 32GB+ | `large-v3` | `qwen2-vl-7b` | 14B |

- Keeping both the LLM and VLM **loaded at the same time** makes switching
  faster, at the cost of more memory. Loading them one at a time lets you get
  by on less memory than the table above
- Turning on **"Just-in-time model loading"** (an LM Studio feature, under
  Settings → Local Model API) makes it auto-load/swap the model named in
  `config.toml` on each API call, so no manual per-stage loading is needed.
  On memory-constrained setups, this is the easiest way to swap the VLM and
  LLM in only when each is needed
