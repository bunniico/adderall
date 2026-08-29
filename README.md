# ⏳ adderall

A single-user, locally hosted web app for people with executive dysfunction
(ADHD, autism, AuDHD). It ports the four most useful goblin.tools ideas —
**Magic ToDo**, **Taskmaster**, **Estimator**, and **Compiler** — into one
reliable single-page workspace, and fixes the gaps: nothing is ever lost,
time is visual and honest, and the app helps you *do* tasks, not just plan
them.

## Quick start

```bash
docker compose up
```

Then open <http://localhost:8000>. That's it — no accounts, no onboarding.
Your tasks live in a SQLite file under `./data/` and survive restarts,
tab closes, and crashes.

Without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

AI features (breakdown, estimates, braindump compiling) need an Anthropic API
key: set `ANTHROPIC_API_KEY` in the environment, or paste one into
⚙ Settings in the app. Everything else works without a key.

If your key is **identity-linked**, the API also requires the workspace each
request acts in, and calls fail with `anthropic-workspace-id is required`
until you provide it. Put the workspace ID in ⚙ Settings → Workspace ID, or
set `ANTHROPIC_WORKSPACE_ID`. You can find the ID in the Claude Console URL:
`platform.claude.com/workspaces/<id>`. Ordinary keys don't need this.

## What it does

- **Magic ToDo** — type a task, hit ⚡, get concrete subtasks. A 🌶
  granularity slider controls how fine the breakdown is; any subtask can be
  broken down further, recursively.
- **Estimator** — every task gets a time estimate (AI-seeded, always
  overridable), and every estimate gets an automatic **time tax**: a 25–50%
  buffer (default 30%), because the planning fallacy means raw estimates are
  systematically wrong. Optionally the buffer adapts to your own recorded
  actual-vs-estimated history (it only ever raises, never lowers).
- **Compiler** — 🧠 Braindump: dump everything in your head into one box;
  a single deep-model call with extended thinking turns it into discrete
  tasks.
- **Taskmaster** — ▶ Focus mode shows *one task at a time* with a
  Time-Timer-style depleting dial (with a real analog clock in the middle),
  a shrinking color block, elapsed **and** remaining time, and staged
  transition cues (wrap up → find a stopping point → time) with distinct
  sounds. When you finish, it rolls straight into the next task.
- **Prioritization** — every task carries impact/effort scores (0–10) placed
  on an action-priority matrix: ⚡ quick wins, 🏔 major projects, fill-ins,
  and 😮‍💨 **thankless tasks** — which trigger a gentle "this is high effort,
  low impact; consider dropping it" suggestion.
- **Deadlines & urgency** — set deadlines yourself or let the app auto-assign
  them (toggleable). Subtasks are backward-scheduled from their parent's
  deadline using buffered estimates. Urgency rises as remaining time shrinks
  relative to the (buffered) work left, and drives the "what next" ordering.
- **Transition alarms** — three staged cues before any deadline (stop what
  you're doing → get ready → go), with different sounds and configurable
  lead times.

## Design principles

1. **Reliability first.** One user, one process, one SQLite file
   (WAL, `synchronous=FULL`), every edit persisted immediately. The entire
   class of sync-conflict/data-loss bugs can't happen.
2. **One page, no swapping.** All four tools share one task list on one
   screen; everything deeper is a modal or overlay.
3. **Time is visual and honest.** Buffered by default, analog + depleting
   color, elapsed and remaining both shown, staged alarms instead of one.
4. **Deterministic where possible, AI only where needed.** All scheduling,
   buffering, urgency, and matrix math is local code (`app/logic.py`,
   fully unit-tested). The AI only does language work and returns minimal
   structured JSON.
5. **Low friction.** No accounts, one command, zero configuration required.

## AI model routing

Claude API only, routed by cost/quality (configurable in the DB settings):

| Call | Default model | Thinking | Batched |
|---|---|---|---|
| Estimates + impact/effort scores | `claude-haiku-4-5` | off | yes — one call per batch of tasks |
| Task breakdown (Magic ToDo) | `claude-sonnet-5` | low effort | no (interactive) |
| Braindump compiler | `claude-opus-5` | adaptive | yes — one call |

Every call uses structured JSON output (`output_config.format`), so responses
are compact and parse deterministically. The system prompt is cache-marked.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_logic.py` covers the deterministic core (buffering, quadrants,
urgency, backward scheduling, next-task ordering); `tests/test_api.py` covers
the API with the AI stubbed out.

## Layout

```
app/
  main.py     FastAPI routes + static hosting
  db.py       SQLite persistence (schema, settings, task CRUD)
  logic.py    deterministic scheduling core — no AI, no I/O
  ai.py       Claude API broker (breakdown / annotate / compile)
  static/     single-page front end (vanilla JS, no build step)
tests/        pytest suite
data/         SQLite database (created at runtime, gitignored)
```
