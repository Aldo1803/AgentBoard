# Architecture

AgentBoard is one file, `agentboard.py`, run with `uv`. Its PEP 723 header declares the Python version, so `uv` fetches the right interpreter and nothing else:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
```

The standard library is the whole dependency list: `http.server` (threaded), `sqlite3`, `subprocess` (for `git` alone), `json`, plus `argparse`, `datetime`, `pathlib`, `threading`, and friends. A machine needs `uv` and `git`; the board never runs anything else.

## The file, top to bottom

| Section | What lives there |
| --- | --- |
| Constants and time helpers | defaults, card-id pattern, column definitions, UTC ISO 8601 helpers, the 4-hour stale test |
| Git layer | `run_git` (argument lists, never a shell), `GitFacts` (fetch, remote checks, commit counts, ancestry, last commit), `worktree_commands` (tailors the commands a claim hands out) |
| Schema and `Store` | the SQLite schema, per-operation connections, `BEGIN IMMEDIATE` transactions, event and meta helpers, read helpers |
| Domain helpers | scope/definition normalization, cycle detection, `card_summary` / `card_detail` view models |
| `Board` | the operations: `claim`, `report`, `block`, `release`, `unblock`, `mark_merged`, `sync`, `import_cards`, `list_cards` |
| `App` / `build_app` | repo resolution, database path, wiring |
| Text renderers | the plain-text answers for every endpoint and for `status` |
| HTML | one page shell, inline CSS (light/dark via `prefers-color-scheme`), inline JS, board and card pages |
| `BoardHTTP` | the threaded HTTP server: routing, content negotiation, body parsing, error formatting |
| CLI | `serve`, `status`, `selftest` and the flags |
| Selftest | 18 tests, temp repositories, ephemeral ports |

## Request lifecycle

1. `ThreadingHTTPServer` (bound to 127.0.0.1) accepts the request on its own thread; each handler gets a fresh SQLite connection for the duration of the operation.
2. The router matches path and method. Bodies are decoded as UTF-8 (a BOM is dropped), then as the declared charset, then as Windows-1252, so a Windows PowerShell 5.1 string body keeps its accents. They parse as JSON, as form fields, or — for imports — as a JSON string in a `cards` field. Responses are plain text unless the request asks for JSON (`?format=json` or `Accept: application/json`).
3. Operations raise `APIError(status, sentence)`; one catch-all formats both text (`sentence`) and JSON (`{"error": sentence}`). Git failures raise `GitError`, which maps to `502` with a sentence that says what to do.
4. Writes run in `BEGIN IMMEDIATE` transactions, so simultaneous claims serialize on the database itself. Git reads that need the network (`ls-remote`, `fetch`) happen outside every transaction.

## The sync loop

`serve` fetches the base branch at startup, then every 60 seconds on a daemon thread, and `POST /sync` runs the same routine on demand (a lock keeps the two from overlapping). Sync fetches `origin/<base>` and, while any card is in review, every `card/*` branch (one wildcard fetch that never prunes, so a branch deleted on merge keeps its last head). Then it walks the in-review cards and asks git one question per card: is the card's remote branch head an ancestor of the base head? Those that are get marked done in one transaction, each with a `merged` event whose actor is `board`. The header's "last sync" and base-head facts are recorded in the `meta` table at the same time.

If the remote is unreachable, sync logs the failure, leaves everything unchanged, and tries again on the next tick; `POST /sync` answers `502` with the reason.

## What the board does to your repo (and what it doesn't)

| Touches git reads | Touches git refs | Touches the working tree |
| --- | --- | --- |
| `rev-parse`, `show-ref`, `ls-remote`, `log`, `rev-list`, `merge-base`, `status` reads | `fetch origin <base>`, `fetch origin <branch>` during `report`, and `fetch origin card/*` during `sync` (remote-tracking refs inside `.git` only) | never |

The board hands out worktree commands; it never runs them. It writes only inside `.git/agentboard/` (the SQLite file). That is the whole footprint.

Git is always invoked with an argument list — `subprocess.run(["git", …])`, never a shell — so paths and branch names behave the same in PowerShell, cmd and Bash. On Windows the calls are made with `CREATE_NO_WINDOW` so no console flashes. The commands the board hands to agents are plain `git` invocations using forward slashes, runnable unchanged in any of those shells and on Windows; paths with spaces are double-quoted.

## Security posture

- Binds `127.0.0.1` only. No accounts, no tokens, no remote access — by design; the board is a development aid on one machine.
- The pages fetch nothing from the internet, and contain no controls that change state.
- All card content is HTML-escaped at render time. Card texts are untrusted input.
- The board runs no program but `git`, and accepts no instructions from a card's contents — it is not an agent runtime.

## Testing: `uv run agentboard.py selftest`

The built-in tests are the acceptance criteria from [spec.md](spec.md), executable. They create their own throwaway world: a bare `origin.git`, a seed clone, a work clone, and a real HTTP server on an ephemeral port — so the HTTP layer, the transactions and the git plumbing are all exercised end to end. No network beyond local file paths, no user git config touched (`user.name` and such are passed with `-c` on each call), and `tempfile` cleans up everything.

| Test | Proves |
| --- | --- |
| `t01_schema_and_triggers` | direct SQL cannot update/delete a done card, and cannot touch events |
| `t02_import_create_update_unchanged_refused` | import semantics: create/update/unchanged/refused-done, unknown deps and cycles refused atomically, `done:true` on create only |
| `t03_claim_commands_and_worktree` | a claim changes nothing on disk; the returned commands really create the worktree and branch — including the re-claim path where the branch already exists |
| `t04_concurrent_claims` | of two simultaneous claims, exactly one wins; the loser's `409` names the holder |
| `t05_report_git_facts` | reports are refused (naming the missing fact) for an unpublished branch and for a branch with no new commits; accepted once pushed |
| `t06_merge_sync_dependents` | a merge commit is detected by `POST /sync`, the card is done, and its dependent becomes ready |
| `t07_squash_manual_merged` | a squash merge is *not* auto-detected; `/merged` marks it done and records the note |
| `t08_block_release_unblock` | block/release/unblock transitions, reasons and notes, branch kept |
| `t09_done_permanent_api` | every endpoint refuses a done card; re-import leaves it unchanged |
| `t10_events_and_since` | every change appears in the log, and `since=` filters it |
| `t11_agent_page_selfsufficient` | `/agent` alone contains the rules, the ready cards and working examples |
| `t12_html_readonly` | the page has no forms, buttons or inputs, and carries the 5-second refresh |
| `t13_errors_and_json` | 404/400 sentences, JSON errors, JSON listing |
| `t14_status_render` | the text `status` command covers every column |
| `t15_source_stdlib_and_packaging` | the PEP 723 header holds and every import is standard library |
| `t16_stale_marker` | a claim older than 4 hours is marked stale — shown only, nothing changes |
| `t17_body_encodings` | Windows-1252, ISO-8859-1 and BOM-prefixed UTF-8 bodies import without losing accents |
| `t18_merge_detected_after_rewrite_elsewhere` | a branch rewritten from another clone while in review, then merged and deleted, is still detected |

CI runs `uv run agentboard.py selftest` on Ubuntu, macOS and Windows (`.github/workflows/ci.yml`), which is where the "same on every OS" claim is kept honest. The suite takes on the order of a minute or two, mostly git worktree setup.

## Portability notes

- Python ≥ 3.12 via `uv run`, on Windows, macOS and Linux.
- `sys.stdout`/`stderr` are reconfigured to UTF-8 at startup so agent-facing text survives any console codepage.
- No shell semantics anywhere: no `shell=True`, no string-built commands, no `&&` inside the product.
- Worktrees are suggested *beside* the repo (`../<repo>-worktrees/<id>`), which keeps editor watchers out of them and Windows paths short.
