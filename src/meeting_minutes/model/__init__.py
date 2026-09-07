"""model — 実処理（Model）層。フォルダ ＝ この名前空間。

動画 → 音声 → 文字起こし → フレーム → VLM → 議事録 のパイプラインと、その各工程
（`audio` / `transcribe` / `frames` / `vision` / `minutes` / `pipeline`）、共通部品
（`config` / `cancel` / `ffmpeg_utils` / `llm_client`）を持つ。tkinter を知らず、
GUI（`view/` + `presenter/`）や CLI からは `pipeline.run` を呼ぶだけ。
"""
