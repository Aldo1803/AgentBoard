# Troubleshooting and FAQ

Every refusal is a status code plus one plain sentence that says what to do next. This page collects the ones you are most likely to meet.

## Starting the board

**`agentboard needs a git repository: run it from inside one, or pass --repo PATH.`**
The board keeps its state in the repo's `.git` folder, so it must be pointed at a git repo. `cd` into the repo first, or pass `--repo path/to/repo`.

**`could not listen on 127.0.0.1:4300 (...); pass --port with a free port`**
Something else has the port. Run `--port 4301` (or any free port) and point agents at the same one: the address in `/agent` follows the flag.

**`initial sync failed (the board still serves): ...`**
The remote could not be fetched at startup — usually no network, a wrong `--remote`, or a branch named differently from `--base`. The board still serves, shows cached facts, and retries every 60 seconds. Fix with `--base`/`--remote` and restart if needed.

**`No board yet in .../.git/agentboard/board.sqlite3. Start it with: uv run agentboard.py serve`**
You ran `status` before ever serving in this repo. Start the board once; importing cards or starting `serve` creates the database.

## Claims

**`409 IDN-02 is already claimed by codex (3h 12m ago); ask them to release it or take another ready card.`**
Exactly one agent holds each card. Either coordinate with the holder, ask them to release it (`POST /cards/IDN-02/release`), or take another card. Nothing expires; a claim is released only by an agent or a person.

**`409 IDN-02 is waiting on IDN-01 (todo); it is not ready yet ...`**
Take the dependency first, or pick the ready card that unblocks it. `GET /cards?status=ready` lists what can start now.

**The card shows `STALE (no events for over 4 hours)` — did the board release it?**
No. Stale is a label only: after 4 hours without an event on a claimed card, the page and `/agent` mark it so a person notices. The claim, branch and status stay until an agent or person acts. In practice: go look, then either nudge the agent or release the card.

## Reports

**`409 Refused: card/IDN-02 is not on origin yet - push it (git push -u origin card/IDN-02) and report again.`**
The branch was never pushed. From the worktree: `git push -u origin card/IDN-02`.

**`409 Refused: card/IDN-02 has no commits beyond origin/main - commit and push, then report again.`**
The branch is on the remote but has nothing the base does not. Either the work is not committed yet, or the commits landed on the wrong branch. Check `git log origin/main..card/IDN-02` from the worktree.

**`502 Could not fetch origin/main: ... Check the network and the remote, then report again.`**
The board could not talk to the remote. The card is untouched; retry when the network or remote is back.

**`409 IDN-02 is held by codex, not claude-code; only the holder can report it.`**
If you passed an `agent` that does not match the holder, the board refuses. Report as the holder, or say nothing about `agent` (the holder is used).

## Done and merges

**The PR is merged, but the card still sits in review.**
Two possibilities:

1. *Time* — the board syncs every 60 seconds. Wait, or force it: `curl.exe -s -X POST http://127.0.0.1:4300/sync` (`Invoke-RestMethod -Method Post http://127.0.0.1:4300/sync`).
2. *Squash or rebase-and-merge* — the base branch has no trace of the branch's commits, so git cannot prove the merge. A person confirms it: `POST /cards/IDN-02/merged` with a note.

**Can I undo a done card?**
No — done is permanent, enforced by a database trigger. If the work was wrong, create a new card that fixes it. That is the point: the record of what was accepted stays true.

**Re-import lists my card under "refused because done". Is something wrong?**
No — that is the design. Imports never change done cards; they are listed so you know they were seen and skipped.

## The page

**The board does not refresh fast enough for me.**
It refreshes within 5 seconds. Without JavaScript, a `<noscript>` meta refresh does the same.

**Can I edit cards from the page?**
No. The UI is read-only by design: people look, agents act through the API. Telling an agent "block IDN-02 with reason X" is the intended path.

**I see `[agentboard]` lines on the console.**
Those are non-200 responses being logged to stderr. Normal traffic is silent.

## PowerShell and Windows

**`curl` does something weird in Windows PowerShell 5.1.**
In PS 5.1, `curl` is an alias for `Invoke-WebRequest`. Use `curl.exe` for the real thing — the `/agent` page shows both spellings — or use `Invoke-RestMethod`:

```
Invoke-RestMethod http://127.0.0.1:4300/agent
Invoke-RestMethod -Method Post http://127.0.0.1:4300/cards/IDN-02/claim -Body @{agent='claude-code'}
```

**How do I send JSON from PowerShell?**
`Invoke-RestMethod -ContentType 'application/json' -Body '[...]'` needs careful quoting; the easier paths are a form body (`-Body @{agent='codex'}`) or a file:

```
curl.exe -s -X PUT -H "Content-Type: application/json" --data-binary "@cards.json" http://127.0.0.1:4300/cards
```

## General questions

**Does every agent need the same GitHub account or tool?**
No. The board only cares that branches are pushed to the remote it knows. The PR link it records is just text — any URL. Agents can be mixed freely (Claude Code, Codex, OpenCode, anything with a shell).

**Can several people use one board?**
The board binds to 127.0.0.1: it serves one machine by design. In practice the board runs on the machine where agents run; the shared truth beyond it is the git remote and the PRs.

**Can I run several boards?**
Yes — one per repo, on different ports: `uv run agentboard.py serve --repo ../other-repo --port 4301`. Each repo keeps its own database inside its own `.git`.

**Does the board do anything while nobody asks?**
Two things, both about git: it syncs the base branch every 60 seconds (merge detection), and the page refreshes in the browser. It never releases claims, never restarts agents, never runs project commands.

**Where do I back up the board?**
Usually nowhere: the file lives in `.git/agentboard/`, is never committed, and the cards can be re-imported. If the board's history matters to you, copy the `.sqlite3` file while the server is stopped.

**How do I start over?**
Stop the board, delete `.git/agentboard/`, and serve again. Cards, claims and history are gone; branches and PRs are untouched. Re-import your cards — send finished work with `"done": true` again.
