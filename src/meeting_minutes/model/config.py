"""設定の読み込み。

優先順位（強い順）:
    1. 環境変数（MM_ プレフィックス）
    2. TOML ファイル（既定は config.toml、無ければ config.example.toml）
    3. コード内のデフォルト値

TOML は標準ライブラリ tomllib（Python 3.11+）で読む。追加依存なし。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import cast

from meeting_minutes.i18n import normalize_language

# リポジトリのルート（このファイルは src/meeting_minutes/model/config.py なので 3 つ上）
REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts"


def _to_bool(raw: str) -> bool:
    """環境変数の真偽値。"1"/"true"/"yes"/"on"（大小無視）だけ True。"""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def load_prompt(name: str) -> str:
    """prompts/<name> を読み込んで文字列で返す。"""
    path = PROMPTS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"プロンプトファイルが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "local-no-key"
    model: str = "qwen2.5-7b-instruct"
    vlm_model: str = "qwen2-vl-7b-instruct"
    # ローカルの大きめモデルは1リクエストで数分かかることがある（特に議事録の
    # 最終統合は出力トークン数が多く時間がかかりやすい）ので長めにしてある。
    timeout: float = 600.0
    # Qwen3 系などの推論（reasoning）モデルは、可視の回答を書く前に見えない
    # "思考" にもこの上限からトークンを消費する。小さすぎると思考だけで使い切り
    # 本文が空で返ってくることがあるため、通常のモデルより多めにしてある。
    max_tokens: int = 8192
    # 議事録生成で「一発生成」する文字起こしの上限文字数。これを超えると
    # チャンク要約 → 統合の分割モードに切り替える。既定 40000 は LLM を 32k 前後の
    # コンテキストで動かす前提。LM Studio 側で Context Length を大きくできない場合は
    # 小さくする（例: 8000）。→ docs/models.md「コンテキスト長の設定」
    chunk_trigger_chars: int = 20000
    # 分割モードのときの 1 チャンクの文字数。
    chunk_size_chars: int = 12000
    # ロード中モデルの実コンテキスト長（トークン）。0 なら自動検出
    # （LM Studio の /api/v0/models）。検出できない基盤で、かつ 32k 以外を
    # 使っている場合はここに実値を書く。
    context_tokens: int = 0


@dataclass
class TranscribeConfig:
    # 文字起こしエンジン: "auto"（Apple Silicon なら mlx、他は faster-whisper）/
    # "mlx" / "faster-whisper"
    backend: str = "auto"
    # モデルのサイズ名（large-v3-turbo / large-v3 / medium / small ...）。
    # バックエンドごとに実体（HF リポジトリ名など）へ変換する。"/" を含む文字列は
    # フルリポジトリ名としてそのまま使う。
    # 既定の large-v3-turbo は large-v3 とほぼ同精度で推論が速く、faster-whisper /
    # mlx どちらのバックエンドでも使える。
    model: str = "large-v3-turbo"
    # 以下 2 つは faster-whisper のときだけ有効（mlx では無視）
    compute_type: str = "int8"
    device: str = "auto"
    language: str = "ja"  # 空文字なら自動判定


@dataclass
class FramesConfig:
    interval_sec: float = 15.0
    scene_threshold: float = 0.3
    max_frames: int = 60
    min_gap_sec: float = 4.0


@dataclass
class OutputConfig:
    dir: str = "output"
    # 議事録の「構造」を差し替えるカスタムテンプレートのパス。空なら内蔵テンプレート。
    # ここに設定しておくと毎回自動で使われる（GUI のドロップダウンからその回だけ上書きも可能）。
    # 存在しない・読めない・空の場合は内蔵にフォールバックし警告する。
    template_path: str = ""
    # 「おまかせ」モード: 動画の内容に合わせて議事録の見出し構成を LLM に自動生成させる。
    # True なら template_path より優先（優先順位: auto > file > builtin）。生成された構成は
    # output/<動画名>/work/structure_used.txt に保存され、気に入れば templates/ にコピーして
    # 固定テンプレートとして使い回せる。生成に失敗したら内蔵にフォールバックし警告する。
    auto_structure: bool = False


@dataclass
class GuiConfig:
    # GUI の表示言語。"ja" / "en"。それ以外は load_config で "ja" に丸める。
    # 起動時に gui.py の --lang で毎回上書きできる（優先順位: --lang > config/env > 既定）。
    # 影響範囲は GUI の画面文言のみ（文字起こし言語・LLM プロンプト・議事録内容は別）。
    language: str = "ja"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    frames: FramesConfig = field(default_factory=FramesConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)

    @property
    def output_root(self) -> Path:
        """出力ルートを絶対パスで返す（相対指定はリポジトリルート基準）。"""
        p = Path(self.output.dir).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)


# 環境変数 -> (セクション, キー, 変換関数) の対応表
_ENV_MAP: dict[str, tuple[str, str, Callable[[str], object]]] = {
    "MM_LLM_BASE_URL": ("llm", "base_url", str),
    "MM_LLM_API_KEY": ("llm", "api_key", str),
    "MM_LLM_MODEL": ("llm", "model", str),
    "MM_LLM_VLM_MODEL": ("llm", "vlm_model", str),
    "MM_LLM_TIMEOUT": ("llm", "timeout", float),
    "MM_LLM_MAX_TOKENS": ("llm", "max_tokens", int),
    "MM_LLM_CHUNK_TRIGGER_CHARS": ("llm", "chunk_trigger_chars", int),
    "MM_LLM_CHUNK_SIZE_CHARS": ("llm", "chunk_size_chars", int),
    "MM_LLM_CONTEXT_TOKENS": ("llm", "context_tokens", int),
    "MM_TRANSCRIBE_BACKEND": ("transcribe", "backend", str),
    "MM_TRANSCRIBE_MODEL": ("transcribe", "model", str),
    "MM_TRANSCRIBE_COMPUTE_TYPE": ("transcribe", "compute_type", str),
    "MM_TRANSCRIBE_DEVICE": ("transcribe", "device", str),
    "MM_TRANSCRIBE_LANGUAGE": ("transcribe", "language", str),
    "MM_FRAMES_INTERVAL_SEC": ("frames", "interval_sec", float),
    "MM_FRAMES_SCENE_THRESHOLD": ("frames", "scene_threshold", float),
    "MM_FRAMES_MAX_FRAMES": ("frames", "max_frames", int),
    "MM_FRAMES_MIN_GAP_SEC": ("frames", "min_gap_sec", float),
    "MM_OUTPUT_DIR": ("output", "dir", str),
    "MM_OUTPUT_TEMPLATE_PATH": ("output", "template_path", str),
    "MM_OUTPUT_AUTO_STRUCTURE": ("output", "auto_structure", _to_bool),
    "MM_GUI_LANGUAGE": ("gui", "language", str),
}

_SECTION_TYPES = {
    "llm": LLMConfig,
    "transcribe": TranscribeConfig,
    "frames": FramesConfig,
    "output": OutputConfig,
    "gui": GuiConfig,
}


def default_config_path() -> Path | None:
    """使う TOML を決める。config.toml > config.example.toml > なし。"""
    for name in ("config.toml", "config.example.toml"):
        p = REPO_ROOT / name
        if p.is_file():
            return p
    return None


def _build_section(section_cls: type, raw: dict) -> object:
    """辞書から dataclass セクションを作る。未知キーは無視し、型は緩く合わせる。"""
    known = {f.name: f for f in fields(section_cls)}
    kwargs = {}
    for key, value in raw.items():
        if key not in known:
            continue  # 知らないキーは黙って捨てる（前方互換）
        target_type = known[key].type
        try:
            if target_type in ("int", int):
                value = int(value)
            elif target_type in ("float", float):
                value = float(value)
            elif target_type in ("str", str):
                value = str(value)
        except (TypeError, ValueError):
            pass
        kwargs[key] = value
    return section_cls(**kwargs)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """設定を読み込む。

    path: TOML のパス。None なら default_config_path() を使う。
    """
    toml_path: Path | None
    if path is not None:
        toml_path = Path(path)
        if not toml_path.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {toml_path}")
    else:
        toml_path = default_config_path()

    data: dict = {}
    if toml_path is not None:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    sections: dict[str, object] = {}
    for name, cls in _SECTION_TYPES.items():
        sections[name] = _build_section(cls, data.get(name, {}) or {})

    # 環境変数で上書き
    for env_name, (section, key, caster) in _ENV_MAP.items():
        if env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        try:
            casted = caster(raw)
        except (TypeError, ValueError):
            casted = raw
        setattr(sections[section], key, casted)

    # GUI 言語は対応外の値なら既定（ja）に丸める（TOML・環境変数どちらの経路でも）。
    gui = cast(GuiConfig, sections["gui"])
    gui.language = normalize_language(getattr(gui, "language", None))

    return Config(**sections)  # type: ignore[arg-type]
