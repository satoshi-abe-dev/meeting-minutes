---
name: review-worker-pr
description: meeting-minutesプロジェクトでworkerセッションが発行したPRを、manager役として標準の観点（機密混入チェック・差分範囲・独自pytest実行・Codex独立レビュー）でレビューし、問題なければmainにマージしてworkerに報告する。worker報告のPR番号を引数に渡して呼び出す（例: /review-worker-pr 40）。
---

# worker PRレビュー手順

引数で渡されたPR番号（`$ARGUMENTS`）を、以下の手順でレビュー・マージする。
この手順は `doc/ai-workflow/SESSION_RULES.md` の「manager によるPRレビュー・マージ」
節に基づく。

## 1. メタデータと機密チェック

- `gh pr view <PR番号> --json title,files,additions,deletions --jq '{additions,deletions,files:[.files[].path]}'`
- `git fetch origin <ブランチ名>`
- 機密混入チェック：`output/` 配下に実会議のフォルダ（実名を含む）が残っていれば、
  その固有名詞（会議名・参加者名・場所名等の単語）で
  `git grep -ilE '<語1>|<語2>|...' origin/<ブランチ名> -- .` を実行し、追跡ファイルに
  混入していないか確認する（対象の語は毎回 `output/` の実際のフォルダ名から拾う。
  固定のキーワードリストを使い回さない）

## 2. 差分の中身を読む

`git --no-pager diff main FETCH_HEAD` または該当コミットを `git --no-pager show <SHA>`
で確認し、想定した範囲・内容と一致しているか確認する。

## 3. 独自にテストを実行する

```
git checkout -q FETCH_HEAD
source .venv/bin/activate 2>/dev/null
python -m pytest -q
git checkout -q main
```

worker報告のpassed数と一致するか確認する。

## 4. Codexで独立レビューする

```
codex exec review --base main --title "PR#<番号>: <概要>" -o /tmp/codex_review_pr<番号>.md
```

（単一コミットだけを見たい場合は `--commit <SHA>`）

## 5. 問題があれば差し戻す

Codexまたは自分の確認で問題が見つかったら、`SendMessage`でworkerに具体的な指摘内容
（該当箇所・再現条件・対処方針）を送り、マージを保留する。修正コミットが来たら
2〜4を再度実行する。

## 6. マージする

すべてクリアなら：

```
gh pr merge <PR番号> --merge --delete-branch
git pull --ff-only
git log --oneline -3
```

## 7. worker に報告する

`SendMessage` で `meeting-minutes-worker` 宛に、レビュー結果（確認した観点・
テスト件数・Codexの判定）とマージ済みコミットハッシュを報告する。

## 8. 関連Issueがあればクローズする

PRがGitHub Issueに対応する場合、`gh issue close <Issue番号> --comment "..."` で
対応内容を要約してクローズする。
