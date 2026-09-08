"""エントリポイント（cli.py / gui.py）が、HuggingFace 系ライブラリの import より
前に実行時オフライン（HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE）を強制することの検証。

モジュールを import するだけ（main() は呼ばない）で環境変数が立つ ＝ モジュール
レベルで設定されている、を別プロセスで確認する。download_transcribe_model は
対象外なので、そちら経由では立たないことも確認する。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")

# gui.py はモジュールレベルで tkinter を import する（view 層経由）。CI ランナーに
# tkinter が無くてもこのテストが成立するよう、サブプロセス側で軽量スタブを入れる。
# 検証したいのは「meeting_minutes を import する前に環境変数を立てているか」だけ。
_TK_STUB = (
    "import sys, types\n"
    "tk = types.ModuleType('tkinter')\n"
    "for _s in ('filedialog', 'messagebox', 'ttk'):\n"
    "    _m = types.ModuleType('tkinter.' + _s)\n"
    "    setattr(tk, _s, _m); sys.modules['tkinter.' + _s] = _m\n"
    "sys.modules['tkinter'] = tk\n"
)

_REPORT = (
    "import os, importlib\n"
    "importlib.import_module({mod!r})\n"
    "print(os.environ.get('HF_HUB_OFFLINE'), os.environ.get('TRANSFORMERS_OFFLINE'))\n"
)


def _import_and_report(
    module: str, *, extra_env: dict | None = None, stub_tk: bool = False
) -> str:
    env = dict(os.environ)
    # サブプロセスにパッケージを見せる（このリポジトリは src レイアウト・未インストール）。
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("HF_HUB_OFFLINE", None)
    env.pop("TRANSFORMERS_OFFLINE", None)
    if extra_env:
        env.update(extra_env)
    code = (_TK_STUB if stub_tk else "") + _REPORT.format(mod=module)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def test_cli_import_forces_offline():
    assert _import_and_report("meeting_minutes.cli") == "1 1"


def test_gui_import_forces_offline():
    assert _import_and_report("meeting_minutes.gui", stub_tk=True) == "1 1"


def test_download_transcribe_model_import_does_not_force_offline():
    # download_transcribe_model はモデルを取得する側。オフラインは立てない。
    assert (
        _import_and_report("meeting_minutes.download_transcribe_model")
        == "None None"
    )


def test_entrypoint_respects_explicit_offline_opt_out():
    # setdefault なので、利用者が明示的に "0" を指定していれば尊重する
    # （社内ミラー等でオンラインにしたいケース）。
    out = _import_and_report(
        "meeting_minutes.cli",
        extra_env={"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0"},
    )
    assert out == "0 0"
