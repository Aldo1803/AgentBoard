# Data model

State lives in one SQLite file inside the repo's git folder: `.git/agentboard/board.sqlite3` (plus the standard `-wal` and `-shm` companions while the board runs). Every worktree of the clone shares it — the path comes from `git rev-parse --git-common-dir` — and because it sits inside `.git`, it can never be committed by accident.

There is no project config file. The few settings are command-line flags: `--repo`, `--base`, `--remote`, `--port`.

## Tables

### cards — one row per card

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | `IDN-02`; letters, digits, dashes, ≤ 32 chars |
| `seq` | INTEGER | import order; set on first import, never changed after |
| `title` | TEXT | non-empty |
| `grp` | TEXT, null | module/group, shown as a tag |
| `body` | TEXT, null | markdown: acceptance criteria, notes |
| `scope` | TEXT, null | JSON array of path globs |
| `source` | TEXT, null | the file the card came from |
| `status` | TEXT | `todo`, `claimed`, `in_review`, `blocked`, `done` (CHECK-constrained) |
| `agent` | TEXT, null | current holder |
| `branch` | TEXT, null | `card/{id}`; stays after release |
| `pr_url` | TEXT, null | recorded at report time |
| `blocked_reason` | TEXT, null | why the holder stopped |
| `created_at` | TEXT | UTC ISO 8601, e.g. `2026-10-15T03:30:34Z` |
| `updated_at` | TEXT | last API change |
| `done_at` | TEXT, null | when the card became done |

### deps — what each card waits on

| Column | Type | Notes |
| --- | --- | --- |
| `card_id` | TEXT FK → cards(id) | the dependent card |
| `depends_on` | TEXT FK → cards(id) | the card it waits on |

Primary key: `(card_id, depends_on)`. Dependencies are part of a card's definition: a re-import replaces the rows for cards it updates, except done cards, which imports never touch.

### events — every change, append-only

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | also the cursor for `GET /events?since=` |
| `card_id` | TEXT, null | null is reserved for board-level events |
| `at` | TEXT | UTC ISO 8601 |
| `actor` | TEXT | agent name, `person`, `board`, or the import actor |
| `kind` | TEXT | `imported`, `updated`, `claimed`, `reported`, `blocked`, `released`, `unblocked`, `merged` |
| `detail` | TEXT, null | JSON object: branch, pr, reason, note, commits, … |

There is no update or delete path for events, in code or in SQL — enforced by triggers, not convention.

### meta — bookkeeping for the header

| Key | Value |
| --- | --- |
| `last_sync_at` | when a sync last succeeded |
| `base_head` | JSON `{"sha", "when", "subject"}` of the base branch head at that sync |

## Derived state: ready and waiting

`Waiting` and `Ready` are not stored. A `todo` card is ready when every card it depends on is `done`, otherwise it is waiting. The board answers the question fresh every time, ordered by `seq`:

```sql
SELECT c.id
FROM cards c
WHERE c.status = 'todo'
  AND NOT EXISTS (
    SELECT 1
    FROM deps d JOIN cards dep ON dep.id = d.depends_on
    WHERE d.card_id = c.id AND dep.status <> 'done'
  )
ORDER BY c.seq;
```

This is why dependencies never need scheduling and why a merge can ripple: the moment a card turns `done` (sets its `done_at`, fires its event), its dependents answer as ready with no further writes.

## Done is permanent, enforced by the database itself

Four triggers ship with the schema. They protect the guarantees even if someone opens the file with a SQLite client:

```sql
CREATE TRIGGER trg_done_no_update
BEFORE UPDATE ON cards
WHEN OLD.status = 'done'
BEGIN SELECT RAISE(ABORT, 'done cards are permanent: refusing to change ' || OLD.id); END;

CREATE TRIGGER trg_done_no_delete
BEFORE DELETE ON cards
WHEN OLD.status = 'done'
BEGIN SELECT RAISE(ABORT, 'done cards are permanent: refusing to delete ' || OLD.id); END;

CREATE TRIGGER trg_events_no_update
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only: refusing to change event ' || OLD.id); END;

CREATE TRIGGER trg_events_no_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only: refusing to delete event ' || OLD.id); END;
```

The transition into done is the one update the first trigger allows — its `OLD.status` is `in_review` — and it writes `done_at` in the same statement. After that, the row is frozen. A re-import that touches a done card is skipped before it can even reach the trigger, and the card is listed in the reply under "refused because done".

There is no API that deletes a card, either. The board's only deletions are dependency rows being replaced during an update of a non-done card.

## Concurrency

- Each HTTP request and the sync thread use their own SQLite connection (`check_same_thread` never has to be relaxed).
- The database runs in WAL mode with a 30-second busy timeout, so readers never block the sync or each other.
- Every write that must not race — claims, reports, blocks, releases, unblocks, manual done, merges by sync, imports — runs inside `BEGIN IMMEDIATE`, taking the write lock up front. Two simultaneous claims serialize; the second one re-reads the card inside its transaction and loses with a `409` naming the holder.
- Board-side git reads (the remote checks that tailor worktree commands) happen outside any transaction and outside any lock, so no network call ever holds the database.

## Timestamps

All stored timestamps are UTC ISO 8601 with a `Z` suffix (`2026-10-15T03:30:34Z`). The UI turns them into "3h 12m ago" at render time and keeps them fresh client-side; the API always returns the raw UTC values.

## Inspecting by hand

The file is ordinary SQLite, so `sqlite3 .git/agentboard/board.sqlite3` works for reading:

```
sqlite> SELECT id, status, agent, updated_at FROM cards ORDER BY seq;
sqlite> SELECT * FROM events ORDER BY id DESC LIMIT 20;
```

Writing by hand is possible but unsupported: the triggers will refuse the dangerous cases (done cards, events), and anything else you change will not produce an event — leaving the log inconsistent with the cards. Use the API; it is one `curl`.

## Resetting a board

Stop the server and delete `.git/agentboard/`. The next `serve` creates an empty board. Cards, claims, PR links and the whole event history are gone — the git branches and PRs they referred to are not. Re-import the cards to start again; cards sent with `"done": true` come back as done.
