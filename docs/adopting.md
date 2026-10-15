# Adopting AgentBoard in a repo

The board asks nothing of your project: no config file, no scripts, no hooks, no CI changes. Adoption is a habit change plus a few paragraphs in `AGENTS.md`.

## 1. Put the file where you can run it

Keep `agentboard.py` anywhere and point it at the repo, or copy it into the repo. It is one file with no dependencies:

```
uv run path/to/agentboard.py serve --repo path/to/your-repo
```

Cards come from wherever your work is already described — specs, an issue list, a planning doc. An agent turns them into the card format and imports them (step 5).

## 2. Teach `AGENTS.md` about the board

Add a section that replaces "here is your card" with "here is how you get your card". Wording that matches the board's actual replies:

```markdown
## Cards and the board

- The board runs at http://127.0.0.1:4300 (a person starts it). Read
  http://127.0.0.1:4300/agent at the start of every session.
- Take the card you were given, or the first ready card. Summarise your
  plan and wait for the person's go-ahead before claiming.
- Claim the card with your agent name; the board replies with the branch
  name and the git commands to create your worktree. Run them from the
  repo root and work in that worktree.
- Setup, checks, lint and hooks: follow this file exactly as before, but
  run them inside the worktree (ignored files like node_modules are not
  shared with the main checkout).
- Commit, push the branch, open a PR, then report the card with the PR
  link. Only the board decides when the card is in review or done.
- If you are blocked, POST a reason; a person unblocks. Do not change a
  card the board did not hand you.
```

Everything else in `AGENTS.md` — setup commands, test commands, code style, scope rules — keeps working unchanged, because the board never runs any of it; agents do, exactly as before.

## 3. Decide the merge policy

Done detection relies on the branch's commits surviving into the base branch.

| Merge style | The board sees it? | What to do |
| --- | --- | --- |
| Merge commit (`--no-ff`) | yes, on the next sync | nothing |
| Rebase that keeps the branch's commits | yes | nothing |
| Squash merge | no — the trace is gone | mark the card done through `POST /cards/{id}/merged` with a note |
| GitHub "Rebase and merge" | no — commits are rewritten | same as squash |

If you want zero manual steps, merge PRs with a merge commit.

## 4. Write cards that agents can act on

Conventions that pay off (all optional; the board only requires `id` and `title`):

- **Id scheme**: `MOD-01`, where `MOD` is the area — `IDN-02`, `API-14`. Two digits keep sorting readable; the board does not care.
- **Group**: the module or area; it shows as a tag and groups the page.
- **Scope**: the path globs the card may touch. This is the same information your edit hooks and CI use to enforce boundaries — keep them in step.
- **Source**: the spec file the card came from, so anyone can trace it back.
- **Body**: the acceptance criteria, verbatim from the spec. That is what the agent reads first and what the review checks against.
- **Depends on**: the real prerequisites. Readiness is automatic; a wrong dependency stalls a card, a missing one lets two agents collide on the same code.

Import order is priority order: `/agent` lists ready cards in import order, and "take the next ready card" takes the first. When loading a large backlog, import in critical-path order.

## 5. Load the backlog

One prompt to any agent:

> Read `http://127.0.0.1:4300/agent`, then import the cards in `docs/modules/*.md`, in file order.

For work that predates the board, send those cards with `"done": true` so dependencies resolve from day one. Re-running the import after the specs change updates the not-done cards and leaves the history alone.

## 6. Keep guardrails where they are

The board deliberately knows nothing about your tests, lint or setup. The edit hook, the pre-commit check, the CI pipeline and the per-card scope rules stay in the repo and keep doing their jobs; the only difference is that agents now run them inside a fresh worktree. One consequence to expect: plain git worktrees do not share ignored files (`node_modules`, `bin/`, `deploy/dev/.env`), so each agent runs the repo's setup command once per card. That costs time and disk, and it removes a whole class of link edge cases.

## 7. A team routine that works

1. A person keeps the board running (it can be restarted any time; state is in `.git/agentboard/`).
2. Agents are started per card or per lane — Claude Code, Codex, OpenCode, in as many terminals as wanted.
3. Card plans are approved by a person before claiming; the board is the shared source of truth after that.
4. PRs are reviewed and merged as usual; the board notices.
5. Any agent can import an updated spec at any time — re-import is safe by design.
6. When something is stuck, look at the board first: a stale claim, a blocked reason, or a waiting card usually explains why.
