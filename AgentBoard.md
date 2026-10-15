# Agent Board: product spec

Oct 14, 2026 · @Aldo

Agent Board is a single-file local server that hands units of work (cards) to coding agents and records what happens to them. Agents are its users; the page people see only shows the board. It is a development aid, separate from the Prooflane product, and Prooflane is its first user.

## Purpose and principles

It replaces Cline Kanban with the one part we need: a shared list of cards that any agent can read, claim and report on, while each person keeps using the agent they trust (Claude Code, Codex, OpenCode) in their own terminals.

- **Agents are the users.** Every endpoint answers in plain text an agent can act on, with JSON on request. The `/agent` page tells an agent everything it needs, so a person only gives it the address and a card ID.
- **People look; they don't operate.** The UI is read-only. Cards change through the API, usually by asking an agent to do it.
- **No agent runtime.** The board never starts, prompts or watches an agent. Anything with a shell and HTTP can use it.
- **Delegation only, no project config.** The board hands out work in a structured, agent-readable form and records what happens. It never runs a project's commands (setup, tests, lint); agents follow the repo's own guardrails (`AGENTS.md`, hooks, CI).
- **Facts over claims.** A card reaches review only when its branch is on the remote with new commits, and is done only when that branch is merged. Done is permanent.
- **Nothing changes by itself.** Apart from detecting merges, the board changes a card only when an agent or person asks it to. A stale claim is shown, never released.
- **Small and portable.** One Python file run with `uv`, standard library only, the same on Windows, macOS and Linux. Card state lives in SQLite.

## How it is used

A person starts the board and the agents; the board keeps the cards, the branches and the record straight.

1. **Start the board** from the repo: `uv run agentboard.py serve`. It listens on `http://127.0.0.1:4300`.
2. **Load the cards.** Tell any agent: "Read `http://127.0.0.1:4300/agent`, then import the cards in `docs/modules/*.md`." It posts them to the board. Re-running it later updates cards that are not done.
3. **Start agents** in as many terminals as you want, each with: "Read `http://127.0.0.1:4300/agent` and take card IDN-02" (or "take the next ready card").
4. **The agent reads the card,** summarises its plan and waits for your go-ahead.
5. **The agent claims the card.** The board records the claim and replies with the branch name and the git commands to create its worktree. The agent runs them, then sets itself up as the repo's `AGENTS.md` says.
6. **The agent works, commits, pushes the branch and opens a PR**, then reports the card for review with the PR link.
7. **The board checks the git facts:** the branch is on the remote and has commits the base branch doesn't. If not, the report is refused with the reason. If so, the card moves to In review. The repo's CI checks the PR as usual.
8. **Review and merge.** Ask Claude to review the PR; you merge it.
9. **The board sees the merge** on its next sync and marks the card done for good. Cards that waited on it become ready.

At any point an agent can mark its card blocked with a reason; a person decides, and the card goes back to ready.

## Scope

Version 1 is the shared card list with claims, checks and a permanent record. Everything about running or judging agents stays outside it.

| In version 1 | Out, and where it lives instead |
| --- | --- |
| Cards with dependencies, scope and body text, held in SQLite | Writing the cards: agents import them from the project's markdown |
| Readiness from dependencies; one claim per card | Starting, prompting or supervising agents: people do it in terminals |
| A branch name per claim, and the git commands to create its worktree | Creating worktrees and setting up: the agent, following the repo's `AGENTS.md` |
| Git facts checked before review: branch pushed, new commits | Running the project's checks, tests and lint: the repo's hooks and CI |
| Done detected from the merged base branch, and permanent | Code review: Claude reviews the PR on request. Merging: people, on GitHub |
| Event history for every change | Editing cards in the UI: the UI is read-only |
| Plain-text and JSON API, read-only web page | Users, logins, remote access: it binds to 127.0.0.1 only. Hosting APIs: only `git` is used |

## Data model

State lives in one SQLite file inside the repo's git folder, `.git/agentboard/board.sqlite3`. Every worktree of the clone shares it, and it can never be committed by accident. There is no project config file; the few settings are command-line flags (see Packaging and running).

| Table | Holds | Key columns |
| --- | --- | --- |
| `cards` | One row per card | `id` (e.g. `IDN-02`), `seq` (import order), `title`, `grp` (module), `body` (markdown: acceptance criteria, notes), `scope` (paths it may change), `source` (file it came from), `status`, `agent`, `branch`, `pr_url`, `blocked_reason`, `created_at`, `updated_at`, `done_at` |
| `deps` | What each card waits on | `card_id`, `depends_on` |
| `events` | Every change, append-only | `id`, `card_id`, `at`, `actor` (agent name, `person` or `board`), `kind`, `detail` (JSON) |

**Stored status:** `todo`, `claimed`, `in_review`, `blocked`, `done`. **Waiting** and **Ready** are not stored: a `todo` card is ready when every card it depends on is `done`, otherwise waiting.

**Done is permanent, enforced by the database itself:**

- A trigger refuses any update to a `done` card's status or definition, and any delete of it.
- `events` has no update or delete path at all.
- A re-import that touches a done card leaves it unchanged and lists it in the reply.

Timestamps are UTC ISO 8601. Writes that must not race (claims, status changes) run in `BEGIN IMMEDIATE` transactions.

## Agent API

Agents use the board over HTTP at `http://127.0.0.1:4300`. Responses are plain text by default and JSON with `Accept: application/json` or `?format=json`. Request bodies may be JSON or form fields, so PowerShell's `-Body @{…}` works as is. Errors are a status code plus one plain sentence that says what to do next.

| Method and path | Who calls it | Does |
| --- | --- | --- |
| `GET /agent` | Any agent, first | The rules for using the board, the ready cards, and copy-paste examples for PowerShell and curl |
| `GET /cards?status=ready` | Agent | Cards by status, one line each: ID, title, what it waits on, who holds it |
| `GET /cards/{id}` | Agent | The full card: body, scope, dependencies and their states, claim, branch, PR, recent events |
| `POST /cards/{id}/claim` | Agent | Body `agent`. Records the claim; returns the branch name and the git commands for its worktree. `409` if already claimed or not ready |
| `POST /cards/{id}/report` | Agent | Body `pr` (URL) and optional `note`. Checks the git facts; `200` and In review, or `409` with what is missing |
| `POST /cards/{id}/block` | Agent | Body `reason`. Moves the card to blocked |
| `POST /cards/{id}/release` | Agent or person | Gives the card back to ready; the branch stays |
| `POST /cards/{id}/unblock` | Person, through an agent | Body `note`. Back to ready, the decision recorded |
| `POST /cards/{id}/merged` | Person, through an agent | Body `note`. Marks an in-review card done when its merge can't be seen in git (a squash merge) |
| `PUT /cards` | Agent importing | A JSON list of cards; creates new ones, updates those not done |
| `POST /sync` | Anyone | Fetches the base branch now and marks merged cards done (also runs every 60 s) |
| `GET /events?since={id}` | Anyone | The event log after a given event |

Two copy-paste examples appear on `/agent`. In Windows PowerShell 5.1, `curl` is a different command, so the board shows `Invoke-RestMethod` or `curl.exe`:

```text
Invoke-RestMethod http://127.0.0.1:4300/agent
Invoke-RestMethod -Method Post http://127.0.0.1:4300/cards/IDN-02/claim -Body @{agent='claude-code'}

curl.exe -s http://127.0.0.1:4300/agent
curl.exe -s -X POST -d agent=codex http://127.0.0.1:4300/cards/IDN-02/claim
```

Agents read the board through their shell, not their web tool: some web tools refuse `localhost` addresses (Claude Code's does). `/agent` says so in its first line.

## Rules

The board, not the agent, decides when a card is ready, in review and done.

&#91;embedded content: card states · who or what moves a card\]

Agents move a card only by claiming, reporting, blocking or releasing it; the board decides the rest from git, except a squash merge, which a person confirms.

**Ready.** A `todo` card whose dependencies are all `done`. `/agent` lists ready cards in the order they were imported, so a project sets priority by import order; "take the next ready card" takes the first one.

**Claim.** One agent per card, enforced in a single transaction: of two simultaneous claims, one gets the card and the other gets `409` naming the holder. A claim changes nothing on disk. The board replies with:

1. the branch name, `card/{id}`;
2. the git commands to fetch the base branch and create a worktree for that branch beside the repo, `../<repo>-worktrees/{id}`;
3. a reminder to follow the repo's `AGENTS.md` from there.

The agent runs those commands itself. If they fail, it fixes them or releases the card; the board has nothing to undo.

**Report for review.** The board fetches from the remote and accepts the report only when both hold:

- the card's branch exists on the remote;
- it has at least one commit that is not on the base branch.

Otherwise the card stays **Claimed** and the reply says which fact is missing. When both hold, the card moves to **In review** with the PR link. Whether the code is right is for the repo's CI and the review, not the board.

**Done.** Every 60 seconds, and on `POST /sync`, the board fetches the base branch. An in-review card whose branch head is contained in the base branch has been merged: the board marks it `done`, records when, and never changes it again. A squash merge leaves no such trace, so a person marks that card done through `POST /cards/{id}/merged`, which is recorded with their note.

**Blocked and released.** An agent may block its card with a reason, or release it. Both keep the worktree and branch, so work is not lost. Only `unblock`, carrying a person's note, returns a blocked card to ready.

**Stale claims.** A claim with no event for 4 hours is marked stale in the UI and on `/agent`. Nothing else happens: the claim, branch and status stay until an agent or person acts.

## Loading cards

Agents load cards: the board has no parser for any one project's format. A person tells an agent to turn markdown specs into cards, and the agent sends them with `PUT /cards`. `/agent` documents the shape:

```text
[
  {
    "id": "IDN-02",
    "title": "OIDC sign-in and callback",
    "group": "identity",
    "depends_on": ["IDN-01"],
    "scope": ["server/internal/modules/identity/**"],
    "source": "docs/modules/identity.md",
    "body": "Acceptance: AC-1, AC-2 … (markdown)"
  }
]
```

- `id` and `title` are required. An `id` is letters, digits and `-`, at most 32 characters.
- A dependency on an unknown card, or a cycle, is refused with the cards involved; nothing in that request is saved.
- The reply lists the cards created, updated, unchanged, and refused because they are done.
- Re-importing is safe: it replaces definitions of cards that are not done and never touches claims, status or history.
- `done: true` on an imported card records it as done from the start, for work finished before the board existed (Prooflane's spec cards and FND-01).

## The page people see

`http://127.0.0.1:4300/` shows the board and nothing else: no buttons, forms or drag-and-drop. It refreshes every 5 seconds.

- **Columns:** Waiting, Ready, Claimed, In review, Blocked, Done (Done collapsed to a count, expandable).
- **Each card shows:** ID, title, group; the agent and how long ago it claimed; the PR link; the blocked reason; a stale marker after 4 hours without events.
- **A card's page** (`/view/{id}`) shows the body, dependencies with their states, scope, branch, and the card's events newest first.
- **A header line** counts cards per column and shows the time of the last sync and the base branch's last commit.
- **Built in:** one HTML page with inline CSS and plain JavaScript, served by the same file, light and dark following the system setting. Nothing is fetched from the internet.

## Packaging and running

The board is one file, `agentboard.py`, with no third-party dependencies, run with `uv`. Its header declares the Python version as PEP 723 inline metadata, so `uv` fetches the right Python and nothing else, the same on every OS:

```text
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
```

It uses only the standard library: `http.server` (threaded), `sqlite3`, `subprocess` (for `git` alone) and `json`. A machine needs `uv` and `git`; the board never runs anything else.

| Command | Does |
| --- | --- |
| `uv run agentboard.py serve` | Starts the server on 127.0.0.1 and the 60-second sync |
| `uv run agentboard.py status` | Prints the board as text, without a server |
| `uv run agentboard.py selftest` | Runs the built-in tests |

**Settings are flags with defaults**, so most projects need none. There is no config file to keep in step:

```text
uv run agentboard.py serve --repo . --base main --remote origin --port 4300
```

- **Git is called with argument lists, never through a shell,** so paths and branch names behave the same in PowerShell, cmd and Bash.
- **The git commands the board hands out** are plain `git` calls that run unchanged in any of those shells.
- **Worktrees it suggests sit outside the repo** (`../Prooflane-PM-worktrees/IDN-02`), so editors and file watchers in the main checkout never scan them, and Windows paths stay short.

## Using it with Prooflane

Prooflane swaps Cline Kanban for the board. The specs, guardrails and cards stay as they are; only how work is handed out changes.

| Area | Change |
| --- | --- |
| `AGENTS.md` section 1 | "Read `http://127.0.0.1:4300/agent`, take your card, wait for the person's go-ahead, then create the worktree with the commands the board gives you" replaces the card prompt. Setting the card and running setup stay as they are |
| `docs/workflow.md` | Rewritten around the board: start it, load cards, start agents, review, merge |
| `scripts/kanban.mjs`, `.cline/`, `.clinerules/` | Removed; Codex and Cline still read `AGENTS.md` |
| Guardrails | Unchanged: the edit hook, pre-commit check and CI still enforce each card's scope |
| Merges | PRs merged with a merge commit, as now, so the board sees them |
| First load | An agent imports the 102 cards from `docs/modules/*.md` in critical-path order; the 20 already done (19 spec cards and FND-01) are sent with `done: true` |

One behaviour differs from Kanban: plain git worktrees don't share the main checkout's ignored files (`node_modules`, `bin/`, `deploy/dev/.env`). Each agent runs `npm run setup` in its worktree, as `AGENTS.md` already says. That costs time and disk once per card, and it removes the link edge cases we had to guard against. A critical path and explicit parallel bundles for Prooflane can come later, as import order and card fields, without changing the board.

## Acceptance criteria

1. With only `uv` and `git` installed, `uv run agentboard.py serve` starts on `127.0.0.1:4300` and creates the database, on Windows (from PowerShell 5.1 and cmd), macOS and Linux.
2. `GET /agent` alone is enough for a fresh Claude Code, Codex or OpenCode session to claim a named card and create its worktree, without other instructions.
3. Importing Prooflane's cards gives the same dependencies as the specs, and the ready list matches `node scripts/kanban.mjs ready` today.
4. Of two claims sent at the same moment for one card, exactly one succeeds; the other gets `409` naming the holder.
5. A claim records the agent and returns `card/{id}` with working git commands, and changes nothing on disk.
6. A report is refused, with the missing fact named, when the branch is not on the remote or has no commits beyond the base branch; otherwise the card moves to in review.
7. After the branch is merged into the base branch with a merge commit, the next sync marks the card done and its dependents become ready. `POST /cards/{id}/merged` does the same for a squash merge, with the person's note recorded.
8. A done card cannot be changed by any API call, by a re-import, or by a direct `UPDATE` in SQLite (the trigger refuses it).
9. Every change appears in `GET /events` with its actor and time, and nothing changes without an agent, a person or a detected merge.
10. The page has no control that changes state, and shows a change within 5 seconds.
11. The board runs no program but `git`; no third-party packages; `uv run agentboard.py selftest` passes on all three operating systems in CI.

## Decisions (Aldo, 2026-10-14)

- [ ] **Where it lives:** its own repository (recommended, since it serves any project), or inside Prooflane under `tools/agentboard/`?
  - [ ] This will be it's own project, so no need to worry about where it lives
- [ ] **Merges:** done detection relies on merge commits or rebases, which keep the branch's commits. A squash merge would hide them. Keep squash merges off on Prooflane, or should a person mark a squash-merged card done?
  - [ ] We'll always hame some form of main agent availability or a system like Claude, so as long as we are pushing branches we can always backtrack as needed
- [ ] **Setup on claim:** run Prooflane's full setup in each new worktree (minutes for `npm ci` once the web app exists), or leave it to the agent?
  - [ ] This app should not mess with config, it just delegates in a structured, agentic native format. The coding agent at that point (OpenCode, Codex, etc) will work based on other guardrails (agents.md, etc)
- [ ] **Stale claims:** is showing a claim as stale after 4 hours, with no automatic release, the right default?
  - [ ] The app should not update things by itself just because it's "stale" - We can reconvine in a system like Claude and check what's going on
- [ ] **Card order:** within ready cards, is "unblocks the most others first" the right order, or should the critical path from the blueprint come first?
  - [ ] That's irrelevant for our product spec, but once we get to work on prooflane maybe we sure add a critical path and explicit parallel bundles
