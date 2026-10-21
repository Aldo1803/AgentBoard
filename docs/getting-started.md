# Getting started

This walkthrough takes about ten minutes: one board, a few cards, one agent.

## 0. What you need

- `uv` — runs the board and fetches the right Python for it
- `git` — the only program the board calls

```
uv --version
git --version
```

Nothing else to install by hand. The daemon is standard-library only; the console's one dependency (Textual) is declared in its script header, and `uv` fetches it the first time you open the console.

## 1. Start the board in your repo

```
cd path/to/your-repo
uv run agentboard.py serve
```

You should see:

```
AgentBoard on http://127.0.0.1:4300
  repo: /path/to/your-repo
  base: origin/main
  db:   /path/to/your-repo/.git/agentboard/board.sqlite3
  initial sync: no new merges
  people: http://127.0.0.1:4300/
  agents: http://127.0.0.1:4300/agent
```

- Open <http://127.0.0.1:4300/> in a browser. The board is empty: no cards yet.
- The board creates its database inside `.git/agentboard/` — your working tree is untouched, and the file can never be committed by accident.
- While it runs, it fetches the base branch every 60 seconds to detect merges. `Ctrl-C` stops it.
- The base and remote are flags; `main` and `origin` are the defaults: `uv run agentboard.py serve --base develop --remote upstream`

## 2. Load the cards

Any agent can load cards; the board has no parser for your project's format. The simplest prompt:

> Read `http://127.0.0.1:4300/agent`, then import the cards in `docs/modules/*.md`.

If you want to see it move without writing files first, import one card by hand. Save this as `cards.json`:

```json
[
  {
    "id": "DEMO-01",
    "title": "Say hello in the README",
    "group": "demo",
    "scope": ["README.md"],
    "source": "docs/demo.md",
    "body": "Acceptance: README.md contains a line starting with 'Hello'."
  }
]
```

Then send the file from any shell. In Windows PowerShell, plain `curl` is a different command, so use `curl.exe` or `Invoke-RestMethod`:

```
curl.exe -s -X PUT -H "Content-Type: application/json" --data-binary "@cards.json" http://127.0.0.1:4300/cards
Invoke-RestMethod -Method Put -InFile cards.json -ContentType 'application/json; charset=utf-8' http://127.0.0.1:4300/cards
```

Send the file rather than a string: Windows PowerShell 5.1 re-encodes string bodies and can lose characters such as accents.

The reply:

```
Import: 1 created (DEMO-01), 0 updated, 0 unchanged, 0 refused because done.
```

Refresh the board: the card sits in **Ready**. Re-running the same import later is safe — it updates definitions of cards that are not done and never touches claims, status or history. Imports use `PUT /cards`; see [api.md](api.md#importing-cards-put-cards) for the full card format and the rules around dependencies.

## 3. Start an agent

Open a terminal per agent. Example prompts:

> Read `http://127.0.0.1:4300/agent` and take card DEMO-01. Summarise your plan and wait for my go-ahead.

> Read `http://127.0.0.1:4300/agent` and take the next ready card.

The agent reads the card, tells you its plan, and — after your go-ahead — claims the card. The claim reply looks like this:

```
DEMO-01 is claimed by claude-code.
Branch: card/DEMO-01
Worktree: ../your-repo-worktrees/DEMO-01 (beside the repo)

Run these from the repo root (/path/to/your-repo):
  git fetch origin main
  git worktree add -b card/DEMO-01 ../your-repo-worktrees/DEMO-01 origin/main

Then work in the worktree and follow the repo's AGENTS.md (setup, checks, hooks, scope).
```

The board does not run those commands — the agent does. A claim changes nothing on disk; if the commands fail, the agent fixes them or releases the card.

Notes:

- The worktree sits **beside** the repo (`../<repo>-worktrees/<id>`), so editors and file watchers in your main checkout never scan it.
- Worktrees do not share the main checkout's ignored files (`node_modules`, `bin/`, `.env`). The agent runs the repo's setup command inside the new worktree, as `AGENTS.md` says.
- Any number of agents can run at once; the board gives each card to exactly one of them.

## 4. Work, push, open a PR

The agent works in the worktree, commits, pushes `card/DEMO-01` to the remote, and opens a PR. Then it reports the card:

```
curl.exe -s -X POST -d pr=https://github.com/you/repo/pull/12 http://127.0.0.1:4300/cards/DEMO-01/report
```

Before accepting a report the board checks two facts, and refuses with the missing one otherwise:

- the branch `card/DEMO-01` exists on the remote;
- it has at least one commit the base branch doesn't have.

The board never judges the code. If both facts hold, the card moves to **In review**:

```
DEMO-01 is in review: https://github.com/you/repo/pull/12
1 commit(s) on the branch beyond the base; the repo's CI and review take over.
```

## 5. Review, merge, done

Review the PR the way you usually do (for example, ask Claude to review it), then merge it.

- **Merge commit** (or a rebase that keeps the branch's commits): the board's next sync sees the branch head is contained in the base branch and marks the card **done** for good. You can also force it now with `curl.exe -s -X POST http://127.0.0.1:4300/sync`.
- **Squash merge** (or GitHub's "Rebase and merge", which rewrites commits): the trace is gone, so a person marks it done through the board: `curl.exe -s -X POST -d note="squashed, confirmed in GitHub" http://127.0.0.1:4300/cards/DEMO-01/merged`

When a card is done, cards that were waiting on it become ready, in import order.

## 6. Watch it all on one page

<http://127.0.0.1:4300/> shows six columns — Waiting, Ready, Claimed, In review, Blocked, Done — a per-agent strip, and a condensed recent-activity list. Changes arrive over an SSE stream within a second; if the stream cannot connect, the page falls back to five-second polling. Each card links to its own page (`/view/DEMO-01`) with the body, dependencies, scope, branch and the full event history. There are no buttons: the page reports, it never operates.

## 7. Operate from the console

In a second terminal, from the repo:

```
uv run agentboard-tui.py
```

The console is a client of the running daemon: a live board table beside the selected card's body, scope, dependencies, branch, PR and events, plus an Activity tab that lists agents and what they have been doing. It follows the same live stream as the page, so claims, blocks and merges appear without refreshing.

- `j`/`k` or arrows move; `/` or `f` filters; `g` cycles the state filter; `1`/`2` switch Board/Activity; `?` shows all keys.
- `a` opens the actions for the selected card: **claim for an agent** (you enter the agent's name and get the worktree commands to hand over), **release**, **unblock with a note**, **mark merged** (squash), and `s` **syncs** now.
- Every action is recorded by the board — as `person`, or under the agent's name for a claim — and is visible in the event log. The console never runs an agent and never touches git.

If the daemon is down, the status bar says offline and the console keeps retrying. Windows: run it in **Windows Terminal**, not legacy conhost.

## Try it without a project

Any git repo with a remote works. A two-minute sandbox:

```
mkdir agentboard-demo && cd agentboard-demo
git init -b main
echo "# demo" > README.md
git add . && git commit -m "init"
git init --bare ../agentboard-demo-remote.git
git remote add origin ../agentboard-demo-remote.git
git push -u origin main
uv run agentboard.py serve
```

Then import `cards.json` from step 2, claim the card, run the two git commands the board replies with, commit something in the worktree, `git push -u origin card/DEMO-01`, report it with a made-up PR URL, and in a second clone merge `card/DEMO-01` into `main` with a merge commit (`git merge --no-ff origin/card/DEMO-01`) and push. The next sync marks it done. The remote being a local folder makes no difference — it is still a git remote.

## Where to go next

- [workflow.md](workflow.md) — the states, who moves what, and the review loop in detail
- [api.md](api.md) — every endpoint with examples for PowerShell and curl
- [adopting.md](adopting.md) — wiring the board into a repo's `AGENTS.md` and habits
- [troubleshooting.md](troubleshooting.md) — when something refuses
