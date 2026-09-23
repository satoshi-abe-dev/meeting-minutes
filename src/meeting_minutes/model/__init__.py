"""model — the actual-processing (Model) layer. This folder = this namespace.

Holds the video -> audio -> transcription -> frames -> VLM -> minutes
pipeline, each of its stages (`audio` / `transcribe` / `frames` / `vision` /
`minutes` / `pipeline`), and shared components (`config` / `cancel` /
`ffmpeg_utils` / `llm_client`). Knows nothing about tkinter; the GUI
(`view/` + `presenter/`) and CLI just call `pipeline.run`.
"""
