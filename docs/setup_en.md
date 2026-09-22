# Setup instructions

English | [日本語](setup_ja.md)

Supported OS: **macOS / Windows / Linux**. The transcription backend is picked automatically by OS
(`config.toml`'s `[transcribe] backend` defaults to `auto`):

- Apple Silicon Mac — GPU-accelerated mlx-whisper
- Everything else (Windows / Linux / Intel Mac) — CPU-based faster-whisper

> Development and hands-on verification is mostly done on macOS. Windows / Linux status:
>
> - Dependencies: `requirements.txt`'s environment markers auto-skip mlx
> - CI: all tests pass across the `ubuntu-latest` / `windows-latest` / `macos-latest` matrix
> - The full pipeline has not yet been hands-on verified on those OSes

## 1. ffmpeg

| OS | Install |
| --- | --- |
| macOS | `brew install ffmpeg` |
| Windows | `winget install ffmpeg` (`choco install ffmpeg` / `scoop install ffmpeg` also work; for a manual zip extract, add `bin/` to PATH) |
| Linux | `sudo apt install ffmpeg` (Debian / Ubuntu; other distros use their own package manager) |

After installing, confirm it's on the PATH with `ffmpeg -version`.

## 2. Python environment + fetching the model

Python 3.11 or newer (verified on the 3.14 series).

**macOS / Linux** is one command:

```bash
cd meeting-minutes
bash scripts/setup.sh
```

**Windows**: under WSL (a Linux environment), the macOS / Linux steps above work as-is.
On native Windows, `scripts/setup.sh` doesn't work (it hardcodes POSIX paths like
`./.venv/bin/pip` internally, which don't match the `.venv\Scripts\` layout that
`python -m venv` creates on Windows), so use the Windows (PowerShell) steps under
"### Doing it by hand" below instead.

What `scripts/setup.sh` (and the manual steps) do:

1. Create the `.venv` virtual environment
2. `pip install -r requirements.txt`
   - On Apple Silicon Macs, the environment markers also pull in
     **mlx-whisper** (the GPU-accelerated transcription backend). On
     Windows / Linux / Intel Mac, it's automatically skipped and only
     faster-whisper is installed.
3. **Fetches the transcription model** with `python src/meeting_minutes/download_transcribe_model.py`
   (about 1.6GB into `~/.cache/huggingface/hub/`, once only; offline after that).
   **This fetch is required** — if it fails, `scripts/setup.sh` exits with an error. At runtime
   (`cli.py` / `gui.py`), `HF_HUB_OFFLINE` blocks external communication, so a missing model is
   never auto-downloaded — transcription just stops with an error instead.

- `config.toml`'s `[transcribe] backend` defaults to `auto`: mlx on Apple Silicon Mac,
  **faster-whisper on Windows / Linux / Intel Mac (CPU only, slower — `medium` /
  `large-v3-turbo` recommended)**
- It can also be pinned to `mlx` / `faster-whisper`. The default model is `large-v3-turbo`

### Doing it by hand (without setup.sh)

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/meeting_minutes/download_transcribe_model.py   # fetches the model (required; never auto-downloaded at runtime)
```

**Windows (PowerShell; no need to activate the venv — call `.venv\Scripts\python` directly):**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python src\meeting_minutes\download_transcribe_model.py
```

> If you'd rather activate the venv and use shorter commands, use `.venv\Scripts\Activate.ps1`.
> If execution policy blocks it, allow it for just that session:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`
> (from `cmd.exe`, use `.venv\Scripts\activate.bat` instead — it isn't affected by execution policy.)

> **Path convention for the rest of this guide**: wherever a step below writes `python …`,
> if you have **not** activated the venv, use the venv's own Python explicitly instead —
> `.venv\Scripts\python …` on Windows, `.venv/bin/python …` on macOS / Linux. A bare
> `python` uses the global environment and will fail with things like
> `ImportError: faster_whisper`.

### Model storage and distribution

Where the model is cached and how it's distributed:

- The model lives in a **shared cache under the user's home directory**
  (`~/.cache/huggingface/hub/`; on Windows, `%USERPROFILE%\.cache\huggingface\hub\`;
  overridable with `HF_HOME`)
- It's not shipped in the repo, so **each user downloads it once**, the first
  time they set up (`scripts/setup.sh` takes care of this)
- Since the app forces offline mode at runtime, this setup-time fetch is the only chance to download it
- In an air-gapped environment, point `HF_HOME` at an internal mirror or shared storage, or
  bundle the cache with the distribution image so the model is already in place by setup time

## 3. Local LLM server (LM Studio)

- This section assumes **LM Studio**. The tool is an OpenAI-compatible API
  client, so it also works with other compatible servers (Ollama, etc.) — in
  that case, adapt the startup steps to that server and point `config.toml`'s
  `[ai] base_url` at it (see "### Other OpenAI-compatible servers (Ollama,
  etc.)" at the end of this doc)
- The screen layout varies by LM Studio version. This guide assumes the newer
  UI ("Bionic"-style, with the sidebar split into Settings / Integrations /
  Devices / Local Models). On the older UI, the equivalent settings are under
  the "Developer" tab

1. Install and launch [LM Studio](https://lmstudio.ai/).
2. Get two models ready (for detailed guidance on choosing, see [`models_en.md`](models_en.md)):
   - A text LLM (for minutes generation)
   - A VLM (for frame analysis — a vision/image-capable model. In LM Studio's
     **Explore**, pick one tagged with the vision badge, or one with `-vl` /
     `vision` in its name)

   **If unsure**: on a 16GB Mac, use text LLM `qwen2.5-7b-instruct` + VLM
   `qwen2-vl-2b-instruct`; at 24GB or more, use VLM `qwen2-vl-7b-instruct`.
   See "Recommended setup by memory size" in [`models_en.md`](models_en.md) for the full table.

   Get them from the left sidebar **Local Models → Explore** (search and download).
   Already-downloaded models show up under **Local Models → Library**.
3. **Load the text LLM with Context Length set to 32768 or higher** (important):
   - The need for "Context Length ≥ 32768" is the same on any server (exceed
     it and you get HTTP 400). The steps below are LM Studio-specific; on
     other servers, set the equivalent yourself (Ollama uses `num_ctx`, for
     example). See the note at the end of this doc about auto-detection not
     working on non-LM-Studio servers.
   - LM Studio loads a model with a small default Context Length unless you
     specify one. Left as-is, minutes generation **fails with HTTP 400
     (context length too small)**.
   - Model loading settings → set **Context Length** to `32768` or higher,
     then load. If it's already loaded, Eject it first and reload with that
     setting.
   - If you use **Just-in-time model loading**, also set that model's
     loading settings (its default Context Length) to 32768 or higher (load
     it manually once to set it, or change the model's default settings).
   - For details, and what to do on the `config.toml` side if it still
     doesn't fit, see "Setting the context length (important)" in
     [`models_en.md`](models_en.md).
4. Start the local API server:
   - Open **Settings** → left sidebar **Local Models → Local Model API**
   - Turn **"Local API server"** ON (it shows **Running**)
   - The **Base URL** on the same screen is `http://localhost:1234/v1`. Use
     this for `config.toml`'s `base_url` (match it if you change the port).
   - Turning ON **"Just-in-time model loading"** makes LM Studio
     auto-load whichever model an API request names. No pre-loading needed,
     which pairs well with swapping the LLM and VLM in one at a time (to save
     memory).
   - Since nothing hits it from a browser, **CORS can stay OFF**.
5. Confirm the model IDs to write into `config.toml`:
   - Take the model key shown under **Local Models → Library** (e.g.
     `qwen2.5-7b-instruct`) and use it as-is for `llm_model` / `vlm_model`.
   - Or check the `id` returned by `curl <base_url>/models` (LM Studio's
     default is `http://localhost:1234/v1/models`).
   - If Just-in-time loading is OFF, load the models you'll use ahead of time
     under **Local Models → Loaded Instances**.

### Other OpenAI-compatible servers (Ollama, etc.)

Since the tool uses the OpenAI-compatible `chat/completions` API, it also works with servers other than LM Studio.

- Point `config.toml`'s `[ai] base_url` at that server. For Ollama, start
  `ollama serve` and use `http://localhost:11434/v1`.
- Confirm the model ID via `curl <base_url>/models`'s `id`, and write it into `llm_model` / `vlm_model`.
- **Automatic context-length detection is LM-Studio-only** (it reads the
  `/api/v0/models` extension). It doesn't work on other servers, so set
  `config.toml`'s `[ai] context_tokens` to the real value, or lower
  `chunk_trigger_chars` / `chunk_size_chars` to lean on split generation
  instead (→ "Setting the context length (important)" in [`models_en.md`](models_en.md)).
- How to set Context Length itself depends on the server (Ollama uses `num_ctx` / a Modelfile, etc.).

## 4. Config file

**macOS / Linux:**

```bash
cp config.example.toml config.toml
```

**Windows:**

```powershell
copy config.example.toml config.toml
```

Open `config.toml` and adjust at least these to your environment:

```toml
[ai]
base_url = "http://localhost:1234/v1"
vlm_model = "<the ID of the VLM used for frame analysis>"
llm_model = "<the ID of the text LLM used for minutes generation>"

[transcribe]
backend = "auto"          # mlx (GPU) on Apple Silicon; can also be pinned to "faster-whisper"
model = "large-v3-turbo"  # default. "large-v3" for accuracy, "medium" / "small" for lighter weight
```

> As covered in §3, confirm the text LLM is loaded with **Context Length ≥ 32768**
> (without it, minutes generation fails with HTTP 400).

> `python src/meeting_minutes/download_transcribe_model.py` reads `config.toml` (or
> `config.example.toml` if there is none) for `[transcribe] backend` / `model` and fetches the
> matching Whisper model.
>
> - §2's `setup.sh` runs before §4, so the default (`large-v3-turbo`) is already fetched
> - **If you change `model` / `backend` from the default in §4, re-run
>   `python src/meeting_minutes/download_transcribe_model.py`** to fetch the new one
>   (the app never auto-downloads at runtime)
> - If the venv isn't activated, follow §2's path convention: `.venv\Scripts\python …`
>   (`.venv/bin/python …` on macOS / Linux)

## 5. Smoke test

Try it on a short video (a minute or two, with some slides shown).

**macOS / Linux** (venv activated; if not, use `.venv/bin/python` explicitly):

```bash
python src/meeting_minutes/cli.py sample.mp4   # CLI
python src/meeting_minutes/gui.py              # GUI
```

**Windows** (if the venv isn't activated, use `.venv\Scripts\python` explicitly):

```powershell
.venv\Scripts\python src\meeting_minutes\cli.py sample.mp4
.venv\Scripts\python src\meeting_minutes\gui.py
```

- Success looks like `output/sample/minutes.md` being generated
- As long as the venv is activated, or you use the venv's own Python
  explicitly as above, the same command structure works on any OS (Python
  accepts either `/` or `\` as a path separator; calling a bare `python`
  without the venv active uses the global environment and gives an
  ImportError)

> For developers, `python -m meeting_minutes.cli sample.mp4` / `-m meeting_minutes.gui`
> also work (in that case, `cd src` first, or set `PYTHONPATH=src`).

## When it doesn't work

| Symptom | Fix |
| --- | --- |
| `ffmpeg not found` | See §1's per-OS install (`brew` / `winget` / `apt`, etc.). PATH is needed even outside the venv. |
| `Cannot connect to the local LLM server` | Check that Settings → Local Model API's "Local API server" is **Running**, and that `base_url` matches (menu names are LM Studio's; for other servers, check the running state and `base_url` your own way). |
| `model_not_found` / model not loaded | Turn on "Just-in-time model loading," or load the model under Loaded Instances. Confirm `config.toml`'s name matches the Library's model key (for LM Studio; on other servers, check the model-loading method and ID your own way). |
| `Details: timed out` (fails partway through minutes generation) | The server is up; generation is just taking a while. The final merge step in particular produces long output and is prone to timing out. Increase `config.toml`'s `[ai] timeout` (default 600s; larger models need more). |
| `HTTP 400` (context length too small) right after minutes generation starts | The text LLM's Context Length is too small. Reload it in LM Studio with **≥ 32768** (→ §3, "Setting the context length (important)" in [`models_en.md`](models_en.md)). A different symptom from a `timeout` or a reasoning model's empty response (for LM Studio; on other servers, do it your own way — auto-detection is LM Studio-only, so set `[ai] context_tokens` manually). |
| Minutes generation is extremely slow / a "max_tokens used up by reasoning" error / a partial summary comes back empty | The LLM in use is likely a reasoning (thinking) model. In LM Studio, turn reasoning OFF, or switch to a non-reasoning **Instruct-style model** (→ [`models_en.md`](models_en.md)). Increasing `max_tokens` doesn't fix the speed problem (turning reasoning OFF applies to LM Studio; on other servers, use that server's reasoning setting, or switch models). |
| The minutes come out in English | Set `config.toml`'s `[transcribe] language = "ja"`. Also use an LLM that handles Japanese well. |
| Transcription is slow | On Apple Silicon, use `[transcribe] backend = "auto"` (or `"mlx"`) to use the GPU. Confirm `mlx-whisper` is installed (`pip show mlx-whisper`). Also try `model = "large-v3-turbo"` / `"medium"`. |
| The progress bar doesn't move on mlx | Expected. mlx-whisper returns its result all at once, so it stays at 0 until it finishes. If the GUI log shows "transcribing with mlx-whisper," it's working. |
| The model download fails | Check the network connection and `HF_HOME`, then re-run `python src/meeting_minutes/download_transcribe_model.py` (if the venv isn't activated, follow §2's path convention — `.venv\Scripts\python …` / `.venv/bin/python …`). The app never auto-downloads at runtime, so this has to succeed here. |
| At runtime: "the transcription model (…) was not found locally" | It hasn't been pre-fetched yet. Run `bash scripts/setup.sh` (macOS / Linux) or `python src/meeting_minutes/download_transcribe_model.py` (following §2's path convention if the venv isn't activated). If you changed `config.toml`'s `[transcribe] model` / `backend` along the way, that combination needs to be re-fetched too. |
| Too many / too few frames | Adjust `[frames] interval_sec`, `scene_threshold`, `max_frames`. |
