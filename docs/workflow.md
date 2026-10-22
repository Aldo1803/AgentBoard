# Workflow

The board owns the card states. Agents move a card only by claiming, reporting, blocking or releasing it; the board decides the rest from git, except a squash merge, which a person confirms.

## States

**Stored:** `todo`, `claimed`, `in_review`, `blocked`, `done`.

**Derived, not stored:** a `todo` card is **ready** when every card it depends on is `done`; otherwise it **waits**. Nothing to schedule, nothing to recompute — the board answers from the dependency list every time it is asked.

| Column | Meaning |
| --- | --- |
| Waiting | `todo` with at least one dependency that is not `done` |
| Ready | `todo` with every dependency `done` |
| Claimed | one agent holds it; a branch name is recorded |
| In review | branch pushed with commits beyond the base; a PR link is recorded |
| Blocked | the holder stopped with a reason; only a person's unblock returns it |
| Done | merged into the base (or confirmed by a person); permanent |

## Who moves a card

```
todo (waiting) --> todo (ready)          derived: the last dependency reached done
todo (ready)   --> claimed               agent, or a person claiming for an agent:
                                         POST /cards/{id}/claim (console)
claimed        --> in_review             agent: POST /cards/{id}/report   (board checks git first)
in_review      --> done                  board: merge commit detected on sync
in_review      --> done                  person: POST /cards/{id}/merged  (squash merges)
claimed        --> blocked               agent: POST /cards/{id}/block
blocked        --> todo (ready)          person: POST /cards/{id}/unblock (with a note)
claimed        --> todo (ready)          agent or person: POST /cards/{id}/release
```

The console performs the person's routes as `person` (release, unblock, merged) or with the agent name the person enters (claim). `done` is terminal. The database itself refuses to change or delete a done card, whatever route asks — API, re-import, or a direct `UPDATE` in SQLite.

## The rules behind the transitions

**Ready.** A `todo` card whose dependencies are all `done`. `/agent` lists ready cards in import order, so a project sets its priority by import order; "take the next ready card" takes the first one. A card's position never changes on re-import — the original `seq` is kept.

**Claim.** One agent per card, decided in a single SQLite transaction (`BEGIN IMMEDIATE`). Of two simultaneous claims, one gets the card and the other gets `409` naming the holder. A claim changes nothing on disk: no branch, no worktree. The reply gives the branch name `card/{id}`, the worktree path `../<repo>-worktrees/{id}` and the git commands to create it. If the branch already exists — from a released earlier claim, for example — the board tailors the commands to continue on it, never to reset it.

**Report for review.** The board fetches from the remote and accepts only when both facts hold:

- the card's branch exists on the remote;
- it has at least one commit that is not on the base branch.

Otherwise the card stays **Claimed** and the reply names the missing fact (and the command that fixes it). When both hold, the card moves to **In review** with the PR link. Whether the code is right is for the repo's CI and the review — not the board.

**Done.** Every 60 seconds while `serve` runs, and on `POST /sync`, the board fetches the base branch and the `card/*` branches. An in-review card whose branch head is contained in the base branch has been merged: the board marks it `done`, records when, and never changes it again. The event's actor is `board`.

**Squash merges.** A squash merge leaves no trace of the branch's commits in the base branch, so git cannot prove the merge. A person marks that card done through `POST /cards/{id}/merged` with a note; the event records them and the note. GitHub's "Rebase and merge" rewrites the commits the same way, so it is treated like a squash: use `/merged`.

**Blocked and released.** An agent may block its card with a reason, or release it. Both keep the worktree and branch, so work is not lost. Only `unblock`, carrying a person's note, returns a blocked card to ready. Releasing clears the holder; the branch name stays on the card and the branch stays in git.

**Stale claims.** A claim with no event for 4 hours is marked *stale* on the page, on `/agent` and in the console. Nothing else happens: no release, no reassignment, no notification. When you notice one, talk to the person or agent, release the card from the console, or ask an agent to release it. The stale marker is a signpost, not a timeout.

## The console

`uv run agentboard-tui.py` (or `board tui`, after the shortcut install; a second terminal, with the daemon running) is the operator's surface. It shows the board as a live table beside the selected card's body, scope, dependencies, branch, PR and events, with an **Activity** tab listing agents and recent events. The daemon pushes updates over SSE, so the view follows claims, blocks and merges without refreshing.

Keys: `j`/`k` or arrows to move, `a`/`d` for previous/next tab (Board, Activity; `1`/`2` jump directly), `Enter` to open the selected card's actions, `f` or `/` to filter, `g` to cycle the state filter, `t` to show/hide done cards (hidden by default), `h` for help, `q` to quit.

Actions are deliberately narrow, and each one is a normal API call with a confirmation or a note:

| Action | From | Recorded as |
| --- | --- | --- |
| Claim for an agent | Ready | the agent name you enter; the worktree commands are shown for you to hand over |
| Release | Claimed | `person` (or the agent name, if you pass one) |
| Unblock | Blocked | `person`, with your note |
| Mark merged | In review | `person`, with your note |
| Sync now | anywhere (`s`) | the board's merge detector |

The web page stays read-only. The console never runs an agent and never touches git: it is the same middleware, with a keyboard.

## A day with the board

1. **Start the board** in the repo: `uv run agentboard.py serve`. It detects merges from now on.
2. **Load or refresh the cards.** Tell an agent: "Read `http://127.0.0.1:4300/agent`, then import the cards in `docs/modules/*.md`." Re-running the import after specs change is safe and expected.
3. **Start as many agents as you want**, each with the address and a card: "take card IDN-02" or "take the next ready card".
4. **Let each agent plan first.** The go-ahead step is social, not technical — the board does not care — but it is where you set the card, split it, or redirect it.
5. **Watch, from the browser or the console.** Claimed cards show who holds them and for how long; blocked cards show why; the Activity tab says who has been doing what.
6. **Review and merge.** Ask your reviewer-of-choice for the PR review; you merge on GitHub.
7. **The board closes the loop.** The next sync marks merged cards done; waiting cards become ready, in the order they were imported.

## Why it is built this way

- **Agents are the users.** Every endpoint answers in plain text an agent can act on, with JSON on request. The `/agent` page is a complete brief: a person only hands over the address and a card id.
- **The web page reports; the console operates.** The browser view is read-only by design. The terminal console is where a person acts on the board's terms — claim for a named agent, release, unblock with a note, confirm a squash merge, sync — and every one of those is an event with the right actor. Nothing is ever done silently.
- **No agent runtime.** The board never starts, prompts or watches an agent. Anything with a shell and HTTP can use it.
- **Delegation only, no project config.** The board hands out a structured, agent-readable card and records what happens. It never runs a project's commands — agents follow the repo's own guardrails (`AGENTS.md`, hooks, CI).
- **Nothing changes by itself.** Apart from detecting merges and syncing the base branch, every change traces to an agent, a person, or the merge detector — and every one of those is an event with an actor and a time.
