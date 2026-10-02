# 実験: おまかせフォーマット生成の CrewAI 版

日本語 | [English](crewai_experiment_en.md)

開発者向けの実験（Issue #192）で、普段の利用に必要な機能ではない。既定の動作は変わらず、
ここに書いたものは何も入れなくてもツールは使える。

## 何をするものか

おまかせモードでは、議事録の型（見出し構成）を通常 **LLM の 1 回の呼び出し**
（`src/meeting_minutes/model/minutes.py` の `_generate_structure`）で作っている。
この実験では [CrewAI](https://docs.crewai.com/) を使った 2 つ目のエンジンを追加し、
3 つのエージェントで分担する。

1. **判定役（Classifier）**: 資料から会議の種類を判定する。
2. **調査役（Researcher）**: 検索ツールで資料を調べる（数値・期限・担当者・宿題があるか）。
3. **設計役（Designer）**: 2 つの結果から型を作る。ルールは単発版と同じ
   （`prompts/structure_ja.txt`）。

目的は、CrewAI を本来の形（エージェントが見る場所を判断する）で試し、同じ入力での
単発版との比較を記録に残すこと。単発版と同等か、単発版のほうがよいという結果でも、
それは十分な成果として扱う。

CrewAI 版の出力にも、単発版と **同じチェック**（必須プレースホルダー・サイズ）と
**同じフォールバック**（内蔵の型）を通す。

## 任意の追加依存のインストール

CrewAI が対応するのは **Python 3.10〜3.13 のみ**（3.14 は非対応）。普段の仮想環境が
3.14 系なら、別の仮想環境を作る。

```sh
python3.13 -m venv .venv-py313
.venv-py313/bin/python -m pip install -r requirements-dev.txt -r requirements-agent.txt
```

`.venv*/` は git の管理対象外。通常のインストール（`requirements.txt`）では何も増えない。

## 使い方

開発者用の引数を付けて GUI を起動し、議事録フォーマットで「おまかせ」を選ぶ。

```sh
.venv-py313/bin/python src/meeting_minutes/gui.py --structure-engine crewai
```

- 引数なしなら動作は変わらない（`--structure-engine single` が既定）。
- 引数を付けて CrewAI が未インストールの場合は、起動時にインストール方法を示すメッセージを
  出して終了する（スタックトレースは出さない）。

## 2 つのエンジンの比較

```sh
.venv-py313/bin/python scripts/compare_structure_engines.py output/<会議のフォルダー>
```

フォルダーに `transcript.json` があればよい（過去の実行の出力フォルダーが使える）。
両エンジンには **まったく同じ材料** を渡す。スクリプトは `generate_minutes` を通して
動くので、材料は既存の処理で作られ（型の予算に収まれば文字起こし全文、収まらなければ
既存のチャンク要約）、型の生成が終わった直後に止まる。議事録は生成しない。
結果は `output/_compare/<フォルダー名>-<日時>/`（git 管理外）に保存される。

| ファイル | 内容 |
| --- | --- |
| `comparison.md` | 表（結果・秒数・LLM 呼び出し回数・ツール呼び出し回数）と両方の型 |
| `comparison.json` | 同じ内容の機械可読版。材料のハッシュ付き |
| `material.txt` | 両エンジンが受け取った材料そのもの |
| `structure_single.txt` / `structure_crewai.txt` | 型（そのエンジンが成功した場合のみ） |

自動の採点はない。2 つの型を読んで、違いを書き留める。

## ローカル完結を保つ仕組み

- **テレメトリは切る。** CrewAI は既定で匿名の利用統計を送る。エンジンのモジュールを
  import した時点で、CrewAI を import する前に `CREWAI_DISABLE_TELEMETRY=true`・
  `OTEL_SDK_DISABLED=true`・`CREWAI_DISABLE_TRACKING=true`・
  `CREWAI_TRACING_ENABLED=false` を設定する（ほかの値が入っていても上書きする）。`tests/test_offline_env.py` が別プロセスでこれを確認し、
  通常の GUI 起動ではこれらの変数に触れないことも確認する。
- **CrewAI が持つ材料の写しは、実行が終わったら消す。** CrewAI はタスクごとの出力を
  `~/Library/Application Support/<フォルダー名>/` 下の SQLite に保存し、その記録には
  タスクの指示文とエージェントのメッセージ、つまり **エージェントに渡した材料の全体**
  （文字起こし、またはチャンク要約）が含まれる。放っておくと `output/` の外に残る。
  そこでエンジンは保存先を専用の一時フォルダー（権限 0700）へ向け、**実行が終わった
  直後に削除**する（エラーや割り込みで終わった場合も同じ）。念のため終了時にも削除し、
  強制終了されたプロセスが残したフォルダーは、次回の起動時に削除する。
  **これで防げない場合:** エージェントの実行中にプロセスが強制終了されると（SIGKILL・
  `kill`・クラッシュ・停電）、そのフォルダーは一時ディレクトリ（`$TMPDIR`。あなただけが
  読める）に、このツールの次回起動か OS の一時ファイル掃除まで残る。「中止」ボタンや
  ウィンドウを閉じる操作は、実行が通常どおり終わるので対象になる。CrewAI を import すると
  `~/.config/crewai` と、ランダムな鍵ファイル
  `~/Library/Application Support/crewai/credentials/secret.key` も作られるが、どちらも
  会議の内容は含まない。
- **ほかの処理と同じローカルサーバーを使う。** エージェントには
  `LLM(model="openai/<llm_model>", base_url=<[ai] base_url>, api_key=<[ai] api_key>)` を
  明示的に渡す。環境変数経由でクラウドの接続先に落ちることはない。
- **CI では実モデルも CrewAI も使わない。** テストは偽の `crewai` モジュールと偽の
  LLM クライアントで動く。

## 手順数・呼び出し回数の上限

小さなローカルモデルはツールの使い方を誤ることがある（姉妹プロジェクトでは、1 回の応答で
約 50 回の検索を出した例があった）ため、上限はコードに固定してある
（`src/meeting_minutes/model/structure_crewai.py`）。

| 上限 | 値 |
| --- | --- |
| `max_iter`（判定役 / 調査役 / 設計役） | 2 / 4 / 2 |
| 検索ツールの呼び出し合計（こちらで数え、上限を超えるとツールが断る） | 6 |
| エージェント 1 つあたりの実時間 | 最大 900 秒（かつ `[ai] timeout` 以下） |
| エージェント | 3 つ、順番に実行、委任なし |

LLM の呼び出し回数は CrewAI の利用状況メトリクスから読み、比較表に出す。

## CrewAI について確認したこと（2026-10-03 時点）

| 項目 | 結果 |
| --- | --- |
| 保守状況 | 1.15.23 が 2026-09-28 にリリース。2026 年中、頻繁にリリースされている（[PyPI](https://pypi.org/project/crewai/)） |
| 対応 Python | `>=3.10,<3.14`。このプロジェクトの文書が推奨する **3.14 には入らない** |
| テレメトリ | 既定で有効（匿名。版・エージェント/タスク数・role 名・tool 名・モデル名・所要時間・成否。プロンプトや出力は含まない）。`CREWAI_DISABLE_TELEMETRY` または `OTEL_SDK_DISABLED` で無効化（[公式](https://docs.crewai.com/en/telemetry)） |
| 実物での確認（1.15.23・Python 3.13） | 3 つのテレメトリ設定は CrewAI 自身の `Telemetry` が参照している。ここで使う Agent/Task/Crew/LLM の引数は存在する。偽のローカルサーバーとの実行では、loopback への接続だけだった。LLM はエージェントごとに 1 つ作る必要がある（共有すると利用量がエージェント数の分だけ重複して数えられ、3 リクエストが 9 と報告された） |
| ローカルサーバー | `LLM(model="openai/<名前>", base_url=..., api_key=...)` で OpenAI SDK を直接使う（[公式](https://docs.crewai.com/en/concepts/llms)） |

## 比較の結果

*まだ実施していない。* 比較には CrewAI を入れた Python 3.13 環境とローカルモデルが必要で、
会議 1〜3 件で実施したあと（CrewAI のほうが劣った場合も含めて）ここに追記する。
記録するのは定性的な観察（見出し・欠けた節・時間・呼び出し回数）だけで、実際の会議の
内容はこのリポジトリに入れない。
