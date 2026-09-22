# Privacy design — nothing leaves the machine

English | [日本語](privacy.md)

The goal of this tool is to produce meeting minutes **without ever sending the
meeting's content (audio, video, transcript, or summary) to any external
service**.

## What is processed where

| Data | Processed | Sent externally |
| --- | --- | --- |
| Video file | Locally (ffmpeg) | No |
| Extracted audio (wav) | Locally (temp file, `output/<video name>/audio.wav`) | No |
| Transcript | Locally (mlx-whisper / faster-whisper; assumes the model was already **fetched during setup**. Runtime is offline-forced) | No (at runtime; the model is only fetched during setup) |
| Extracted frame images | Locally (ffmpeg) | No |
| Frame analysis / minutes generation | Sent to a **local LLM server you run yourself** (LM Studio, etc.) over `localhost` | No (stays on the machine) |
| Minutes output (`output/<video name>/minutes.docx` and `minutes.md`) | Locally (`.docx` is converted from `.md` with python-docx; no external binary used) | No |

- There is no code that sends an HTTP request to an external domain at
  runtime. The only endpoint is `config.toml`'s `[ai] base_url`, which
  defaults to `http://localhost:1234/v1`
- The only external communication happens **during setup, when the model is
  fetched**. `scripts/setup.sh` (internally,
  `python src/meeting_minutes/download_transcribe_model.py`) downloads the
  Whisper model weights from HuggingFace. No meeting audio, video, or text is
  sent

The app itself (`cli.py` / `gui.py`) does not communicate externally at
runtime.

- The entry points set `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE`, which stops
  the HuggingFace libraries from making network calls
- The transcription code explicitly checks whether the local cache already
  has what it needs
- If the model has not been fetched, it **stops with an error instead of
  auto-downloading** ("please run `bash scripts/setup.sh` first")
- In an air-gapped environment, point `HF_HOME` at an internal mirror, or
  bundle the cache with the distribution so the model is already in place by
  setup time

## Verifying it runs offline

Once the initial downloads are done, the tool runs to completion with the
network disconnected.

1. Run `bash scripts/setup.sh` and one normal pass to fetch the transcription model and the LM Studio model.
2. Turn off Wi-Fi (unplug Ethernet too, if connected).
3. With the LM Studio local server still running, run `python src/meeting_minutes/cli.py sample.mp4`.
4. If `output/sample/minutes.md` is generated, it completed without any external communication.

- If you want to double-check, tools like `nettop` or `Little Snitch` can
  confirm during the run that this process's outbound traffic never leaves
  `localhost`

## Other notes

- No telemetry or usage data is sent.
- No API key is required (the `api_key` in `config.toml` is a dummy value —
  the local server just expects the field to be present in the request
  format).
- Output is written only under `output/`, which is excluded from the repo via
  `.gitignore`. Keep it off shared drives and it never leaves your machine.
- It also works on an on-prem server or a closed network, as long as ffmpeg,
  the Python dependencies, and the models are brought along.

## Caveats (on your side of the operation)

- Depending on how the local LLM server itself is configured (for example, a
  non-LM-Studio implementation that proxies a cloud API), external
  transmission could still happen. Confirm that `base_url` points at a local
  server you actually control.
- The contents of `output/` are raw meeting data. Treat it with the same care
  as any other confidential document.
