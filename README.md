# AgentBoard

[![CI](https://github.com/Aldo1803/AgentBoard/actions/workflows/ci.yml/badge.svg)](https://github.com/Aldo1803/AgentBoard/actions/workflows/ci.yml)

A local-first board that hands units of work (**cards**) to coding agents and records what happens to them. Agents are its users; the web page is read-only; the terminal console is the operator's.

Three pieces, one repo:

- **`agentboard.py`** — the daemon. One stdlib-only Python file: SQLite state, a plain-text/JSON HTTP API for agents, a read-only web board, an SSE live feed, and git-fact checks before review and done.
- **`agentboard-tui.py`** — the console. A Textual client of the daemon for people who live in terminals: live board, filter, detail, per-agent activity, and confirmed actions.
- **`docs/`** — the manual.

```
uv run agentboard.py serve       # daemon: http://127.0.0.1:4300
uv run agentboard-tui.py         # console, in a second terminal
```

Or, after the one-time install below, the short form: `board serve` and `board tui`.

- **Agents** start at <http://127.0.0.1:4300/agent>, which tells them everything they need.
- **People** open <http://127.0.0.1:4300/> to watch, or the console to operate.
- **Nothing here starts, prompts or watches an agent.** The board is middleware between people, agents and git.

## Install once, then use `board`

```
uv tool install --editable path/to/this/repo
board serve --repo path/to/your-repo     # daemon (or: cd your-repo && board)
board tui                                # console, second terminal
```

`uv tool install` puts `board` and `agentboard` in uv's tool bin directory. If the command is not found, run `uv tool update-shell` once and reopen the terminal. The install is editable, so pulling changes is enough — no reinstall.

No `--repo`? The board serves the git repository containing the folder you run it from — including when you are in a subfolder. `board` with no subcommand is `serve`; `board --repo PATH` is the same as `board serve --repo PATH`. `board tui` likewise opens the board running for the current folder (the daemon records its port in `.git/agentboard/board.json`); pass `--url` to point it elsewhere. Without the install, everything still runs from the checkout with `uv run agentboard.py …` and `uv run agentboard-tui.py …`.

## The idea in six lines

1. A card is a unit of work: id, title, group, body, scope and dependencies.
2. A `todo` card is **ready** when every card it depends on is `done`; otherwise it **waits**.
3. An agent **claims** a ready card and gets a branch name plus the git commands to create a worktree.
4. The agent works, pushes the branch and opens a PR, then **reports** the card.
5. The board checks the git facts — branch on the remote, commits beyond the base — before the card moves to **In review**.
6. When the branch is merged into the base, the board's next sync marks the card **done**, permanently, and its dependents become ready.

## Requirements

`uv` and `git`. Nothing else to install by hand: the daemon is standard-library only, and `uv` fetches the console's one dependency (Textual) from the script header. The same on Windows, macOS and Linux — use Windows Terminal for the console (legacy conhost has poor Unicode/font support).

## Commands

| Command | Does |
| --- | --- |
| `board` / `board serve` | Starts the daemon on 127.0.0.1 and syncs with git every 60 seconds |
| `board status` | Prints the board as text, without a server |
| `board selftest` | Runs the daemon's built-in tests in temporary repositories |
| `board tui` | Opens the console against a running daemon |
| `board tui --selftest` | Runs the console's headless checks |
| `board tui --url http://127.0.0.1:4301` | Points the console at another board |

From the checkout, replace `board` with `uv run agentboard.py` (`board tui` becomes `uv run agentboard.py tui`). Settings are flags with defaults, so most projects need none:

```
board serve --repo . --base main --remote origin --port 4300
```

## What the board guarantees

- **Facts over claims.** A card reaches review only when its branch is on the remote with commits the base branch doesn't have; it is done only when that branch is merged.
- **Done is permanent**, enforced by the database itself: a trigger refuses any change to a done card.
- **One claim per card**, enforced in a single SQLite transaction; the loser of a race gets a `409` naming the holder.
- **Nothing changes by itself.** Apart from detecting merges, the board moves a card only when an agent or a person asks. A stale claim is shown, never released.
- **Every change is an event** with its actor and time, append-only.
- **The web page is read-only.** The console is the operator's surface: it claims for an agent, releases, unblocks, confirms a squash merge, and syncs — every one of those recorded as an event with the right actor.

## Live updates

The daemon pushes an SSE stream at `/events/stream`. The web page re-renders on it and falls back to five-second polling; the console keeps its board, detail pane and activity view current the same way. Agents keep using plain HTTP (`GET /cards`, `GET /events?since=…`) — no streaming required.

## Where the state lives

`.git/agentboard/board.sqlite3` inside the repo: every worktree of the clone shares it, and it can never be committed by accident. The board runs no program but `git`, and binds to 127.0.0.1 only — no accounts, no remote access.

## Documentation

| Document | What's in it |
| --- | --- |
| [docs/getting-started.md](docs/getting-started.md) | First board, first cards, first agent, first console |
| [docs/workflow.md](docs/workflow.md) | States, who moves what, review and merge, the operator console |
| [docs/api.md](docs/api.md) | Every endpoint, formats, errors, live stream, importing cards |
| [docs/data-model.md](docs/data-model.md) | SQLite schema, triggers, permanence |
| [docs/architecture.md](docs/architecture.md) | How the daemon and console are built; testing |
| [docs/adopting.md](docs/adopting.md) | Using it in a real repo |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Errors and fixes, FAQ |
| [docs/spec.md](docs/spec.md) | The original v1 product spec |

## Development

```
uv run agentboard.py selftest        # 21 daemon tests
uv run agentboard-tui.py --selftest  # console checks, headless
```

CI runs both on Ubuntu, macOS and Windows, and builds the wheel (`uv build`). Every test builds its own temporary git repositories and servers; nothing in your working tree or git config is touched.
