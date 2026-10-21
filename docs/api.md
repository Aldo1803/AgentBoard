# API reference

Agents use the board over HTTP at `http://127.0.0.1:4300`. Everything is plain text by default; ask for JSON with `?format=json` or `Accept: application/json`. Request bodies may be JSON or form fields, so PowerShell's `-Body @{…}` works as it is.

## Conventions

| Thing | Rule |
| --- | --- |
| Base URL | `http://127.0.0.1:4300` (bound to 127.0.0.1 only) |
| Text format | One fact per line, ids first, meant to be read by an agent |
| JSON format | `?format=json`, or `Accept: application/json`; `?format=text` forces text (wins over the header) |
| Request bodies | JSON (`Content-Type: application/json`), or form fields (`agent=alice&pr=…`), or a JSON string in a form field named `cards` for imports |
| Encoding | UTF-8 (a leading BOM is ignored). A body that is not valid UTF-8 is read in its declared `charset`, else as Windows-1252, which is what Windows PowerShell 5.1 sends for a string body. Send files (`--data-binary @file`, `-InFile`) to keep every character |
| Errors | HTTP status + one plain sentence saying what to do next; JSON mode: `{"error": "…"}` |
| Card ids | Letters, digits and dashes, at most 32 characters |
| Timestamps | UTC ISO 8601, e.g. `2026-10-15T03:30:34Z` |
| Statuses | Stored: `todo`, `claimed`, `in_review`, `blocked`, `done`. Derived: `ready`, `waiting` |
| Side effects | A claim changes nothing on disk. Report fetches the base and the card's branch; sync fetches the base and every `card/*` branch. Both only update remote-tracking refs inside `.git`. Nothing else touches git or the filesystem |

## Endpoint cheat sheet

| Method and path | Who calls it | Does |
| --- | --- | --- |
| `GET /agent` | Any agent, first | The rules, ready cards, claimed cards, copy-paste examples |
| `GET /cards?status=…` | Agent | Cards one line each: id, title, group, state, holder, waits |
| `GET /cards/{id}` | Agent | The full card: body, scope, dependencies, branch, PR, recent events |
| `GET /board` | Console, agents | One snapshot: counts, sync facts and every card |
| `GET /events/stream` | Web page, console | Server-Sent Events: snapshot, then each change as it happens |
| `POST /cards/{id}/claim` | Agent | Records the claim; returns the branch and worktree commands |
| `POST /cards/{id}/report` | Agent | Checks git facts; moves the card to In review |
| `POST /cards/{id}/block` | Agent | Moves a claimed card to blocked with a reason |
| `POST /cards/{id}/release` | Agent or person | Gives a claimed card back to ready; the branch stays |
| `POST /cards/{id}/unblock` | Person, through an agent | Blocked card back to ready; the note is recorded |
| `POST /cards/{id}/merged` | Person, through an agent | Marks an in-review card done (squash merges) |
| `PUT /cards` | Agent importing | JSON list of cards; creates and updates, never touches done |
| `POST /sync` | Anyone | Fetches the base branch now and marks merged cards done |
| `GET /events?since={id}` | Anyone | The append-only change log after a given event |
| `GET /` , `GET /view/{id}` | People | The read-only board and card pages (HTML) |

A 404 for anything else reads: `No route {METHOD} {path}. Agents start at GET /agent; the board is at GET /.`

---

## GET /agent

The complete brief for an agent. First line is the shell-not-web-tool warning; then the rules, the endpoint list, the ready cards in import order, the claimed cards, two copy-paste example blocks — `Invoke-RestMethod` (Windows PowerShell 5.1) and `curl.exe`/`curl` — and how to load cards: the card shape and the `PUT /cards` commands.

JSON mode returns:

```json
{
  "board": "http://127.0.0.1:4300",
  "repo": "/path/to/repo",
  "base": "origin/main",
  "ready": [ { "id": "IDN-02", "state": "ready", … } ],
  "claimed": [ { "id": "IDN-05", "state": "claimed", "agent": "codex", "stale": false, … } ]
}
```

## GET /cards

Cards as one line each, in import order.

```
IDN-01  Bootstrapping  [identity]  ready
IDN-02  OIDC sign-in and callback  [identity]  waiting on IDN-01 (claimed)
IDN-05  Rate limiting  [api]  claimed by codex 3h 12m ago
IDN-06  Webhooks  [api]  claimed by claude-code 5h 2m ago  STALE (no events for over 4 hours)
IDN-03  Session cookies  [identity]  in review  https://github.com/you/repo/pull/12
IDN-04  Key rotation  [identity]  blocked: waiting for API keys
IDN-00  Scaffolding  [core]  done 2026-10-14T09:00:00Z
```

`?status=` filters: `ready`, `waiting`, `todo` (every `todo`, ready and waiting), `claimed`, `in_review`, `blocked`, `done`. No parameter (or `all`) lists everything. An unknown value is refused:

```
400 Unknown status 'quickly'; use ready, waiting, todo, claimed, in_review, blocked or done.
```

JSON mode wraps the same summaries: `{"cards": [ … ]}`, one object per card with `id`, `title`, `group`, `status`, `state`, `agent`, `branch`, `pr`, `blocked_reason`, `stale`, `last_event_at`, `created_at`, `updated_at`, `done_at`, `source`, `depends_on` (all dependencies with their statuses) and `waits_on` (the unmet ones).

## GET /cards/{id}

Human- and agent-readable detail: the summary, the body, the scope, the dependency list with states, and the 50 most recent events, newest first.

```
IDN-02  OIDC sign-in and callback
state: claimed (claimed by claude-code 1h 4m ago)
group: identity  source: docs/modules/identity.md
scope: server/internal/modules/identity/**
branch: card/IDN-02
created: 2026-10-14T10:00:00Z  updated: 2026-10-15T02:26:10Z

Acceptance: AC-1, AC-2 …

depends on:
  IDN-01  done  Bootstrapping

events (newest first):
  #31 2026-10-15T02:26:10Z IDN-02 claude-code claimed  branch=card/IDN-02
```

JSON mode returns the detail object directly (summary fields plus `body`, `scope` as a list, and `events`).

Unknown cards: `404 No card IDN-99. GET /cards lists every card; check the id.`

## GET /board

One call for the whole board: the `status` summary line and sync facts, counts per column, and every card. The console refreshes from it, and it is the payload of the stream's first event.

```
AgentBoard - repo: /path/to/repo   base: origin/main
Waiting 3 | Ready 5 | Claimed 2 | In review 1 | Blocked 1 | Done 12
last sync 2m ago | origin/main @ ab12cd3 "Merge pull request #12"
...
```

JSON mode:

```json
{
  "repo": "/path/to/repo",
  "repo_name": "repo",
  "base": "origin/main",
  "last_sync_at": "2026-10-15T03:30:34Z",
  "base_head": { "sha": "ab12cd3", "when": "2026-10-15T03:00:00Z", "subject": "Merge pull request #12" },
  "counts": { "waiting": 3, "ready": 5, "claimed": 2, "in_review": 1, "blocked": 1, "done": 12 },
  "max_event_id": 97,
  "cards": [ { "id": "IDN-02", "state": "ready", … } ]
}
```

## POST /cards/{id}/claim

Body: `agent=<your name>` (required).

```
curl.exe -s -X POST -d agent=codex http://127.0.0.1:4300/cards/IDN-02/claim
Invoke-RestMethod -Method Post http://127.0.0.1:4300/cards/IDN-02/claim -Body @{agent='claude-code'}
```

Success replies with everything the agent needs:

```
IDN-02 is claimed by claude-code.
Branch: card/IDN-02
Worktree: ../your-repo-worktrees/IDN-02 (beside the repo)

Run these from the repo root (/path/to/your-repo):
  git fetch origin main
  git worktree add -b card/IDN-02 ../your-repo-worktrees/IDN-02 origin/main

Then work in the worktree and follow the repo's AGENTS.md (setup, checks, hooks, scope).
```

Reading the remote to tailor those commands is read-only (`git ls-remote`). If the branch `card/IDN-02` exists already (an earlier claim was released, for example), the commands fetch and continue that branch instead of creating it. If the remote cannot be read, the board still answers with the fresh-branch commands plus a `note` explaining it.

JSON mode: `{"id", "status", "agent", "branch", "worktree", "repo_root", "commands": [ … ], "note"?: "…"}`.

Refusals, all `409` unless noted:

| Situation | Reply |
| --- | --- |
| Missing `agent` | `400 POST /cards/IDN-02/claim needs an agent name: pass agent=<your name> as a form field or JSON.` |
| Not a card id / unknown | `404 …` |
| Already claimed | `IDN-02 is already claimed by codex (3h 12m ago); ask them to release it or take another ready card.` |
| In review | `IDN-02 is in review (https://…); nothing to claim.` |
| Blocked | `IDN-02 is blocked: waiting for API keys. A person decides; unblock it with POST /cards/IDN-02/unblock and a note.` |
| Waiting on deps | `IDN-02 is waiting on IDN-01 (todo), IDN-07 (claimed); it is not ready yet - take the next ready card (GET /cards?status=ready).` |
| Done | `IDN-02 is done - done is permanent; take another card (GET /cards?status=ready).` |

## POST /cards/{id}/report

Body: `pr=<URL>` (required), `note=<optional>`, `agent=<your name, optional>`.

The board fetches `origin/main` and `card/IDN-02` from the remote and accepts only when both facts hold. Otherwise the card stays **Claimed** and the reply names the missing fact:

| Situation | Reply |
| --- | --- |
| Branch not on the remote | `409 Refused: card/IDN-02 is not on origin yet - push it (git push -u origin card/IDN-02) and report again.` |
| No new commits | `409 Refused: card/IDN-02 has no commits beyond origin/main - commit and push, then report again.` |
| Remote unreachable | `502 Could not fetch origin/main: … Check the network and the remote, then report again.` |
| Not claimed yet | `409 IDN-02 is not claimed yet; claim it first (POST /cards/IDN-02/claim).` |
| Already in review | `409 IDN-02 is already in review (https://…).` |
| Blocked | `409 IDN-02 is blocked: …; unblock it first (POST /cards/IDN-02/unblock).` |
| Done | `409 IDN-02 is done; no report needed.` |
| Another agent's card | `409 IDN-02 is held by codex, not claude-code; only the holder can report it.` |
| Missing `pr` | `400 POST /cards/IDN-02/report needs the PR link: pass pr=<URL>.` |

On success:

```
IDN-02 is in review: https://github.com/you/repo/pull/12
1 commit(s) on the branch beyond the base; the repo's CI and review take over.
```

JSON mode: `{"id", "status": "in_review", "pr", "commits", "note"}`.

## POST /cards/{id}/block

Body: `reason=<why>` (required), `agent=<optional>`.

```
IDN-02 is blocked: waiting for API keys
A person decides; to return it to ready, POST /cards/IDN-02/unblock with a note.
```

Only a claimed card can be blocked; the holder stays recorded. Refusals: missing reason (`400`), not claimed (`409`), done (`409`), another agent's card when `agent` mismatches (`409`). JSON mode: `{"id", "status": "blocked", "reason"}`.

## POST /cards/{id}/release

Body: `agent=<optional>` — a person may release without one.

```
IDN-02 is ready again (was held by codex). The branch stays.
```

Only a claimed card can be released. A blocked card answers: `409 IDN-02 is blocked: …; only POST /cards/IDN-02/unblock with a person's note returns it to ready.` JSON mode: `{"id", "status": "todo", "holder"}`.

## POST /cards/{id}/unblock

Body: `note=<the person's decision>` (required). Only from blocked. The card returns to ready; the reason and holder are cleared; the note is recorded in the event log, actor `person`.

```
IDN-02 is ready again. Note recorded: keys rotated, go ahead
```

JSON mode: `{"id", "status": "todo", "note"}`.

## POST /cards/{id}/merged

Body: `note=<who confirmed the merge>` (required). Only from in review. Marks the card done for a merge git cannot see (a squash merge, or GitHub's rebase-and-merge, which rewrites the commits).

```
IDN-02 is done (confirmed merge). Done is permanent.
```

JSON mode: `{"id", "status": "done", "note"}`. The event records actor `person`, the note, and `"manual": true`.

## POST /sync

Fetches the base branch now (and, while any card is in review, every `card/*` branch) and marks merged in-review cards done. Runs automatically every 60 seconds while `serve` is running; this endpoint forces it. Refreshing the card branches means a review fix pushed from another machine is seen before the merge, and a branch deleted on merge keeps its last head.

```
Sync: marked done IDN-01.        (or: Sync: no new merges.)
```

JSON mode: `{"newly_done": ["IDN-01"], "base_head": {"sha", "when", "subject"}, "at"}`. If the remote cannot be reached: `502 Sync failed: … Nothing changed; check the remote and the network, then retry.`

## GET /events

The append-only log, oldest first.

```
#12 2026-10-15T03:30:34Z IDN-01 tester claimed  branch=card/IDN-01
#13 2026-10-15T03:31:02Z IDN-01 board merged  branch=card/IDN-01 into=origin/main
```

`?since=12` returns only events with a higher id — useful for polling. JSON mode: `{"events": [{"id", "card_id", "at", "actor", "kind", "detail"}]}` where `detail` is an object.

## GET /events/stream

The live feed, as Server-Sent Events. The web page and the console subscribe; agents keep using `GET /events?since=`.

- It opens with `event: snapshot`, whose data is the `GET /board` payload — everything a fresh client needs in one message.
- Then one `event: event` per new event, data identical to a `GET /events` entry.
- `retry: 2000` is sent at the start; a `: ping` comment every 15 seconds keeps the connection warm; `?since=<id>` asks for a cursor.
- The connection stays open until the client leaves. Nothing is written by this route.

```
curl.exe -s -N http://127.0.0.1:4300/events/stream
```

Event kinds and their actors:

| Kind | Actor | When |
| --- | --- | --- |
| `imported` | the import actor (`?actor=`, default `agent`) | a card was created |
| `updated` | the import actor | a re-import changed the definition |
| `claimed` | the agent's name | a claim succeeded |
| `reported` | the holder | a report was accepted |
| `blocked` | the holder | an agent blocked the card |
| `released` | the agent or `person` | a card went back to ready |
| `unblocked` | `person` | a person returned a blocked card to ready |
| `merged` | `board` (detected) or `person` (manual) | the card became done |

## Importing cards: PUT /cards

Agents load cards; the board has no parser for any project's format. A person tells an agent to turn markdown specs into cards, and the agent sends them here.

```json
[
  {
    "id": "IDN-02",
    "title": "OIDC sign-in and callback",
    "group": "identity",
    "depends_on": ["IDN-01"],
    "scope": ["server/internal/modules/identity/**"],
    "source": "docs/modules/identity.md",
    "body": "Acceptance: AC-1, AC-2 … (markdown)",
    "done": false
  }
]
```

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | letters, digits, dashes; at most 32 characters |
| `title` | yes | non-empty |
| `group` | no | module or area; shown as a tag on the board |
| `depends_on` | no | list of card ids; they must all exist (here or in this request) |
| `scope` | no | list of path globs the work may touch, or one string |
| `source` | no | the file this card came from |
| `body` | no | markdown; put the acceptance criteria here |
| `done` | no | `true` records it as done from the start (work finished before the board existed) |

`PUT /cards` is atomic per request: if anything is wrong — an unknown dependency, a cycle, a bad id, a duplicate in the payload — **nothing in that request is saved** and the reply names the cards involved.

```
400 Card IDN-02 depends on IDN-99, which does not exist anywhere; import IDN-99 first or fix the dependency. Nothing was saved.
400 Dependency cycle refused: CYC-1 -> CYC-2 -> CYC-1. Nothing was saved.
```

The reply lists what happened:

```
Import: 3 created (IDN-01, IDN-02, IDN-03), 1 updated (IDN-04), 2 unchanged (IDN-05, IDN-06), 2 refused because done (IDN-00, FND-01).
```

- **created** — new cards; they get the next import-order numbers.
- **updated** — the payload's definition differed from the stored one; title, group, body, scope, source and dependencies are replaced. Claims, status, branch, PR and history are never touched. A missing optional field means empty.
- **unchanged** — identical definition; nothing written, no event.
- **refused because done** — done cards are permanent; the import leaves them untouched and lists them.

`done: true` is honoured when the card is created. On an existing card it cannot change the status — re-import never touches status — the definition (if any) still updates.

The actor on the events comes from `?actor=<name>` or an `actor` form field; the default is `agent`.

Send the list as a UTF-8 file, so no shell re-encodes it:

```
curl.exe -s -X PUT -H "Content-Type: application/json" --data-binary "@cards.json" "http://127.0.0.1:4300/cards?actor=codex"
Invoke-RestMethod -Method Put -InFile cards.json -ContentType 'application/json; charset=utf-8' "http://127.0.0.1:4300/cards?actor=claude-code"
```

Import via a form body is also supported: a field named `cards` holding the JSON list as a string.

## HTML pages

`GET /` and `GET /view/{id}` return the read-only pages described in [workflow.md](workflow.md). They contain no forms, buttons or anything that changes state, and fetch nothing from the internet. Both refresh within five seconds.
