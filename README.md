# AgentBoard

[![CI](https://github.com/Aldo1803/AgentBoard/actions/workflows/ci.yml/badge.svg)](https://github.com/Aldo1803/AgentBoard/actions/workflows/ci.yml)

A single-file local server that hands units of work (**cards**) to coding agents and records what happens to them. Agents are the users; the page people see is read-only.

One Python file, no third-party packages, no config file, no agent runtime. It keeps a shared list of cards that any agent can read, claim and report on, while each person keeps using the agent they trust — Claude Code, Codex, OpenCode — in their own terminal.

```
uv run agentboard.py serve        # http://127.0.0.1:4300
```

- **People** open <http://127.0.0.1:4300/> and watch.
- **Agents** start at <http://127.0.0.1:4300/agent>, which tells them everything they need.

## The idea in six lines

1. A card is a unit of work: id, title, group, body, scope and dependencies.
2. A `todo` card is **ready** when every card it depends on is `done`; otherwise it **waits**.
3. An agent **claims** a ready card and gets a branch name plus the git commands to create a worktree.
4. The agent works, pushes the branch and opens a PR, then **reports** the card.
5. The board checks the git facts — branch on the remote, commits beyond the base — before the card moves to **In review**.
6. When the branch is merged into the base, the board's next sync marks the card **done**, permanently, and its dependents become ready.

## Requirements

`uv` and `git`. Nothing else. The same on Windows, macOS and Linux.

## Commands

| Command | Does |
| --- | --- |
| `uv run agentboard.py serve` | Starts the board on 127.0.0.1 and syncs with git every 60 seconds |
| `uv run agentboard.py status` | Prints the board as text, without a server |
| `uv run agentboard.py selftest` | Runs the built-in tests in temporary repositories |

Settings are flags with defaults, so most projects need none:

```
uv run agentboard.py serve --repo . --base main --remote origin --port 4300
```

## What the board guarantees

- **Facts over claims.** A card reaches review only when its branch is on the remote with commits the base branch doesn't have; it is done only when that branch is merged.
- **Done is permanent**, enforced by the database itself: a trigger refuses any change to a done card.
- **One claim per card**, enforced in a single SQLite transaction; the loser of a race gets a `409` naming the holder.
- **Nothing changes by itself.** Apart from detecting merges, the board moves a card only when an agent or a person asks. A stale claim is shown, never released.
- **Every change is an event** with its actor and time, append-only.

## Where the state lives

`.git/agentboard/board.sqlite3` inside the repo: every worktree of the clone shares it, and it can never be committed by accident. The board runs no program but `git`, and binds to 127.0.0.1 only — no accounts, no remote access.

## Documentation

| Document | What's in it |
| --- | --- |
| [docs/getting-started.md](docs/getting-started.md) | First board, first cards, first agent |
| [docs/workflow.md](docs/workflow.md) | States, who moves what, review and merge |
| [docs/api.md](docs/api.md) | Every endpoint, formats, errors, importing cards |
| [docs/data-model.md](docs/data-model.md) | SQLite schema, triggers, permanence |
| [docs/architecture.md](docs/architecture.md) | How the single file is built; testing |
| [docs/adopting.md](docs/adopting.md) | Using it in a real repo |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Errors and fixes, FAQ |
| [docs/spec.md](docs/spec.md) | The product spec and its acceptance criteria |
