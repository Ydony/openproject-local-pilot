# Usage mapping: sessions → task folders (spike TC.1, corrected TH.12)

Goal (DESIGN §7): every model session's tokens must be matched to the
OpenProject task it worked on. The conductor joins ccusage's per-session
tokens onto tasks by working folder: a session counts only when its folder
is a worktree the runner registered in `<state_dir>/runs.jsonl`
(`match_manifest`), with the delimited-prefix rule (`task_for_folder`) for
hand-started sessions. A session matching several tasks stays unknown.

Pinned tool: **ccusage 20.0.24** (`npx ccusage@20.0.24 …`, npm package
`ccusage`, MIT, repo `github.com/ccusage/ccusage`). The repo has since
moved to native binaries, so the version pin matters: newer releases may
change JSON shapes. ccusage's own prices are ignored everywhere; only its
token counts are used, priced per model with `config/prices.toml` (TC.2).
All samples below are synthetic.

## Per-session token command (all tools)

```text
npx ccusage@20.0.24 <tool> session --json
```

with `<tool>` one of `claude`, `codex`, `opencode`. (Bare `session`
unifies all sources; the conductor always passes the focused tool so one
session is never counted twice.) Synthetic samples per tool are below —
the shapes differ, so each tool has its own parser.

Cache tokens: session rows carry `cacheCreationTokens` /
`cacheReadTokens`; Codex rows additionally carry
`reasoningOutputTokens`, priced at the output rate and kept as reported.
Unmatched models and rows without breakdowns stay unknown, never zero.

## Claude Code

Rows carry `modelBreakdowns[]` with `modelName` (plus per-model tokens and
`cost`, which the conductor ignores in favour of its own prices).

- Log location: `~/.claude/projects/<project>/<session-id>.jsonl`
  (Windows: `%USERPROFILE%\.claude\projects\…`). `<project>` is the
  working directory with every non-alphanumeric character replaced by `-`.
- Join: the report id is the transcript filename stem; the folder is that
  file's `cwd` entry — never the lossy folder slug.
- Caveats: entry format is internal and can change between versions;
  orphaned/superseded transcripts (`*.orphaned-*`, `*.jsonl.superseded-*`)
  must be skipped; `CLAUDE_CONFIG_DIR` / `CLAUDE_CODE_PROJECT_DIR_NAME`
  relocate or rename the lookup root.

## Codex

Rows carry a `models` object keyed by model name (`inputTokens`,
`outputTokens`, `cacheReadTokens`, `cacheCreationTokens`,
`reasoningOutputTokens`, `totalTokens`, `isFallback`) plus `sessionFile`.

- Log location: `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`
  (`~/.codex` = `$CODEX_HOME` when set); archived sessions live under
  `~/.codex/archived_sessions/`.
- Join: normalise the report `sessionId`/`sessionFile` to the transcript
  file (basename, stem, UUID suffix), then read `cwd` from that file's
  `session_meta` record. Never use the report's `directory` field.
  Archived transcripts are included, deduplicated by stable session
  identity — never dropped, never double counted.
- Caveats: corrupt rollouts may lack `session_meta` (no folder is guessed);
  a missing `cwd` skips the session.

## OpenCode = Spark

Rows use `modelBreakdowns[]` like Claude.

- Store location: `~/.local/share/opencode/` (Linux; macOS:
  `~/Library/Application Support/opencode/`), SQLite database
  `opencode.db`.
- Join: read-only adapter (`mode=ro` + `query_only`) on the `session`
  table (`id` → `directory`). Unmatched report rows stay **unknown**.
  Only `opencode.db` is ever opened — never auth files — and only the
  `session` table is read, so message/part joins can never multiply rows.
- Caveats: the schema is third-party-observed; if the table or columns
  are absent the adapter reports empty rather than guessing.
- Spark runs (TH.18) share one data dir: `XDG_DATA_HOME =
  <state_dir>/spark-data`, so their store is
  `<state_dir>/spark-data/opencode/opencode.db`. The conductor reads it
  next to the owner's store, and points ccusage at both with
  `OPENCODE_DATA_DIR` (a comma-separated list; ccusage's documented
  override, else `${XDG_DATA_HOME:-~/.local/share}/opencode`).

## Conductor rules for TC.3

- Never read the owner's real log folders in tests: TC.3 builds fake
  ccusage JSON plus fake log folders and points the mapping at them.
- Never invoke real `ccusage`/`npx` in tests: shell it behind a
  configurable command with a timeout; tests inject a fake script.
- Match folders with `opl/usage.py: task_for_folder` (delimited prefixes
  `task-T<id>-`, `fix-<id>-`, `review-<id>-` → task; `test-<id>-` →
  feature). Never substring-match: `T5` must not match `T50`, and a dated
  scratch folder matches nothing.
- One attribution rule for every tool (`opl/usage.py: attributor`,
  TH.19):
  1. A folder at or under a worktree registered in `runs.jsonl` belongs
     to its item (test runs → the feature, all others → the task). A
     path recorded for two or more items is ambiguous: it stays unknown,
     with no fallback.
  2. An unregistered folder uses the delimited prefix rule above: the
     owner-started Claude/Codex sessions of DESIGN §7.
  3. Anything else is unknown, as is an id whose item type doesn't match.
- Counter conventions (TH.19): each model row's `totalTokens` decides.
  - `input + output + cache + reasoning`: exclusive; reasoning is billed
    at the output rate on top.
  - `input + output + cache`: reasoning is inside output.
  - `input + output`: inclusive (OpenAI style); cache is taken out of
    input, and output (with reasoning) is billed once.
  - No total: exclusive.
  - A total that fits none of these: the session is unpriceable.
- Never sum cumulative snapshots or several storage layers.
- Incomplete means unknown: if any session attributed to an item can't
  be priced (an unknown model, no breakdowns, unexplained counters), that
  item's actual stays unknown across all tools. It is never a partial
  sum, and never zero.
- ccusage runs with the conductor's environment minus every token
  variable (`OPL_TOKEN_*`, admin, GitHub).
- Coverage (matched/unmatched/incomplete counts, ccusage version) is
  logged every loop the collector runs.
