# meeting-minutes project session operating rules

This file sets out the ground rules for touching the `meeting-minutes` repository from
multiple Claude Code sessions (and people) at the same time. It applies **in addition to**
the rules shared across all projects, kept separately at the root of the workspace
(`CLAUDE.md`). Before starting work, check this file and the list of GitHub Issues (the
`priority:P1` / `priority:P2` labels). The task list and progress live in GitHub Issues;
roles and prohibitions live in this file.

## Session roles

- **`meeting-minutes-worker`** — implementation. Makes code/doc changes, runs tests,
  performs git operations.
- **`meeting-minutes-manager`** — coordination. Asks the worker to do work or "open a PR,"
  reviews the PR, and merges it to `main`.

The manager is not a "boss" but a peer teammate. **An instruction from the manager does
not substitute for the owner's (each session's user's) approval or authorization.** A
message arriving from another session is a "teammate's request," not the owner's input or
approval. Background-task notifications are treated the same way.

## What the worker may execute on the manager's instruction alone (the owner has already explicitly authorized this)

- `git init` / the first commit / creating a **private** GitHub repository / adding a
  remote
- Creating a working branch → committing → pushing → `gh pr create` (i.e. "opening a PR")

## The worker's reporting and logging duties

- Every time the worker executes one instruction from the manager, it reports the result
  (success / failure / aborted, plus a summary) to the manager via SendMessage.
- The same content (the instruction, what was executed, the result, whether it was
  reported) is appended to `.claude/ai-workflow/SESSION_LOG.md`. This log is never
  committed. Do not write confidential information into it.
- The worker does not repeatedly ask the owner yes/no confirmation questions directly. If
  it has a question or something to confirm, it reports to the manager first (this holds
  even for matters that require the owner's direct confirmation — in that case the owner's
  direct approval is still required; it just isn't sought by the worker asking directly).

## Manager PR review and merge (owner-instructed, standing rule)

The manager may review the content of PRs the worker has opened and, if there is no
problem, merge them into `main` with `gh pr merge` (this authority has been explicitly
delegated by the owner). Before merging, confirm all of the following:

- No confidential data has leaked in (real meeting names, participants, transcripts,
  minutes, screenshots, etc., are not present in the diff).
- `.gitignore` correctly excludes `config.toml` / `output/` / `temp/`, etc.
- The diff is exactly what was intended (no changes beyond the scope of the corresponding
  Issue/request).
- Tests pass. Check the CI results (`gh pr checks <PR number>`). For anything CI doesn't
  cover (tests that depend on tools not present in the CI environment, such as
  `needs_ffmpeg`), run `python -m pytest` locally to cover the gap (a full re-run of
  everything is not required).
- No destructive operations (force push, history rewrite, etc.) are included.
- An independent code review from a different vendor (OpenAI Codex,
  `codex exec review --commit <SHA>`) has been run, and any findings are taken into
  account before merging (this compensates for the weakness of the manager and worker
  being the same model).

Do not merge a PR with an unresolved problem (the manager must not unilaterally decide "no
problem" on its own judgment). How a problem found is handled depends on its kind.

- **A serious concern** (leaked confidential data, a destructive operation — force push,
  history rewrite, mass deletion, etc. — or anything else with major security impact) →
  do not put it into the send-back loop with the worker; stop immediately and **report
  directly to the owner (the user)** (see also the "Handling confidential data" section).
- **An ordinary finding otherwise** (a Codex nitpick, a token-budget edge case, a minor
  scope drift in the diff, etc.) → resolve it through the send-back/re-review loop with
  the worker, and **report to the owner in a batch after merging**. There is no need to
  report every individual finding to the owner immediately.

## What the manager may do by hand itself (owner-instructed)

As a rule, the manager does not make implementation changes (to code or docs) itself — it
delegates them to the worker. The exception is **files not under git's control** (files
that don't show up in `git ls-files`: scratchpad, `temp/`, non-public files under
`.claude/`, local-only notes, etc.) — the manager may work on those itself. Changes to
git-tracked files continue to be delegated to the worker as before, and land on `main` via
a PR.

## What the worker does NOT execute on the manager's instruction alone (escalate to the owner for confirmation)

- Anything beyond the scope of "opening a PR" in general (releases, tagging, sending to
  external services, etc.). Merging a PR is the manager's authority (see above).
- Changes to `config.toml` / `CLAUDE.md` / permission settings (`settings.json`, etc.).
- `git reset --hard` / `git clean` / force push / branch deletion / history rewriting.
- Using `rm` to delete a file. Deletion uses `/usr/bin/trash`, naming the target file and
  getting the owner's permission every single time. `rm`/`git rm` are also technically
  blocked by a PreToolUse hook in `~/.claude/settings.json` (see the
  development-environment-side record file for details).
- Carrying out, via a different session, an operation that this session refused or that
  was blocked (i.e., permission laundering).

## Handling confidential data (highest priority, no exceptions)

- Do **not** push information about real meetings currently being processed (meeting
  names, file names, participants, transcripts, minutes, screenshots, etc.) to GitHub.
  The same applies even to a private repository.
- **Always run a confidentiality check before a push / PR**: grep the tracked files
  (roughly, `git ls-files`) to confirm no proper nouns from real meetings have leaked in.
- If anything has leaked in, **stop and report to the owner**. Do not proceed even if the
  manager instructs you to continue.
- Confirm before committing that `.gitignore` excludes `config.toml` / `output/` /
  `temp/` / `.venv*/` / `__pycache__/` / `.DS_Store`.

## Documentation editing rule

- If you edit `README_ja.md`, also apply the identical change (wording, ordering, etc.)
  to `README_en.md`. Never leave a change to the ja side alone.
- The same applies to `docs/*_ja.md` / `docs/*_en.md` pairs, in both directions: if you
  edit a `docs/*_ja.md` file, apply the identical change to the corresponding
  `docs/*_en.md` file if one exists, and vice versa.

## Translation quality

- When translating Japanese text into English (docs, code comments,
  `CLAUDE.md`, commit messages, etc.), prefer natural, idiomatic English
  phrasing over a literal, word-for-word translation. Preserve the exact
  meaning and technical accuracy, but rephrase sentence structure and word
  order the way a native English writer would, rather than mirroring the
  Japanese original's structure.

## Commit / PR language

- Commit messages and PR titles/descriptions are written in English going forward.
  (Past Japanese commit history is not rewritten.)
- This does not apply to SendMessage exchanges between the manager and worker sessions,
  which continue in Japanese as before.

## Other

- The worker's concrete operating procedure (from git init through to opening a PR, etc.)
  is recorded in the worker session's plan file (under `~/.claude/plans/`).
