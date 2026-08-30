# ⏳ adderall

> Note: This application requires a Claude API key and a workspace ID if using an identity-based API key. See Anthropic's API site for more details.

goblin.tools is cool, but it also sucks.

Here's what I created to fix it:

A single-user, locally hosted web app for people with executive dysfunction
(ADHD, autism, AuDHD) designed to help us prioritize and organize tasks with minimal effort.

It ports the four most useful goblin.tools ideas:
- **Magic ToDo**, 
- **Taskmaster**, 
- **Estimator**,
- **Compiler**

The app automatically sets deadline and priority using a mix of the Eisenhower matrix and Action-Impact matrix. More importantly, it fixes the horrible UI goblin.tools has that never works.

## Quick start

```bash
git clone https://github.com/bunniico/adderall
docker compose up
```

Then open <http://localhost:8000>.

Your tasks live in a SQLite file under `./data/` and survive restarts,
tab closes, and crashes. 

This also means that you can access this app on any device on the network, so if its a shared network I would probably change the configuration a bit and/or add a password/login.

Without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### Updating

The Docker image bakes in the app code, so a plain `docker compose up` after
pulling keeps running the **old** build. Always rebuild:

```bash
git pull
docker compose up --build
```

Your tasks are unaffected — they live in the mounted `./data/` volume, not in
the image.

To not have to remember any of that, start it with the included script, which
pulls and rebuilds every time:

```bash
./start.sh          # foreground, Ctrl-C to stop
./start.sh -d       # background
```

If the pull fails (local edits, diverged history) it says so and starts the
version you already have, rather than refusing to launch.

### Running it at boot

To bring the app up automatically when the machine starts, install a systemd
service (Linux). Replace the path and user with your own:

```ini
# /etc/systemd/system/adderall.service
[Unit]
Description=adderall
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/YOU/adderall
ExecStart=/home/YOU/adderall/start.sh -d
ExecStop=/usr/bin/docker compose down
User=YOU

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now adderall
```

`start.sh -d` pulls, rebuilds, and starts detached, so each boot picks up the
latest code. Note that this means the machine deploys whatever is on `main`
unreviewed — fine for a personal app, worth thinking about if that changes.
Drop the `git pull` from the script if you'd rather update deliberately.

Alternatively, `restart: unless-stopped` is already set in
`docker-compose.yml`, so if the Docker daemon starts at boot the container
comes back on its own — just without pulling updates.

AI features (breakdown, estimates, braindump compiling) need an Anthropic API
key: set `ANTHROPIC_API_KEY` in the environment, or paste one into
⚙ Settings in the app. Everything else works without a key.

If your key is **identity-linked**, the API also requires the workspace each
request acts in, and calls fail with `anthropic-workspace-id is required`
until you provide it. Put the workspace ID in ⚙ Settings → Workspace ID, or
set `ANTHROPIC_WORKSPACE_ID`. You can find the ID in the Claude Console URL:
`platform.claude.com/workspaces/<id>`. Ordinary keys don't need this.

## What it does

- **Projects in tabs** — a row of tabs across the top, one list of tasks
  each: work, house, that side thing. Click a tab to switch (or **Alt+1…9**),
  **＋** to start a new one, click the open tab to rename it, **✕** to delete
  it and everything in it. Adding, braindumping, breaking down, ordering and
  focusing all act on the tab you're on and leave the others alone, and the
  app reopens on the tab you were last in. Send a task to another tab from
  its detail modal — its subtasks go with it. Deadline alarms are the one
  thing that deliberately ignores tabs: a cue you'd miss because its task is
  one tab over is exactly what this app exists to prevent, so those fire
  wherever you are and say which project they came from. Existing installs
  upgrade with everything in a first tab called "Tasks".
- **Magic ToDo** — type a task, hit ⚡, get concrete subtasks. A 🌶
  granularity slider controls how fine the breakdown is; any subtask can be
  broken down further, recursively. The **+** on every task adds a subtask by
  hand when you already know the step, at any depth, and the box stays open so
  you can reel off several in a row.
- **Fold anything away** — a task with subtasks gets a **▾** next to it;
  click it and the whole list underneath collapses into the one line that
  contains it, which still carries its rolled-up time, its deadline, its
  progress bar, and a note when the "next up" task is one of the ones now
  hidden. Folds are stored per task, so the shape you left the list in is the
  shape it comes back in, on every device. Anything that puts a task inside a
  folded one — the **+** box, a ⚡ breakdown, a drag — opens it back up, so
  nothing ever lands somewhere you can't see.
- **Your own order** — drag any task by its ⠿ handle: onto the top or bottom
  edge of another task to sit above or below it, into the middle of one to
  nest inside it, or onto empty list space to pull it back out to the top
  level. Works with a mouse, on a touchscreen, and from the keyboard (focus a
  handle and use ↑ ↓ to move, → to nest under the task above, ← to pop out).
  The first move switches the list to **manual order**: it then stays exactly
  as you arranged it — including which task is "next up" — instead of being
  re-sorted by urgency. ⚙ Settings turns that back off, and everything else
  (estimates, deadlines, rollups, alarms) carries on as before either way.
- **Estimator** — every task gets a time estimate (AI-seeded, always
  overridable), and every estimate gets an automatic **time tax**: a 25–50%
  buffer (default 30%), because the planning fallacy means raw estimates are
  systematically wrong. Optionally the buffer adapts to your own recorded
  actual-vs-estimated history (it only ever raises, never lowers).
- **Rolled-up totals** — a task that contains subtasks is worth what it
  holds: its badge shows the summed buffered estimate of the whole subtree
  (a "~20m" parent with 46m of steps under it reads ~46m) and the furthest
  deadline anywhere inside it. Under it sits a **progress bar in time** —
  how many minutes of the subtree are done and how many are still ahead,
  not how many checkboxes are ticked.
- **Compiler** — 🧠 Braindump: dump everything in your head into one box;
  a single deep-model call with extended thinking turns it into discrete
  tasks.
- **Taskmaster** — ▶ Focus mode shows *one task at a time* with a
  Time-Timer-style depleting dial (with a real analog clock in the middle),
  a shrinking color block, elapsed **and** remaining time, and staged
  transition cues (wrap up → find a stopping point → time) with distinct
  sounds. It **walks the task tree depth-first**: the subtasks of a task
  come before the task itself, so a session drills down to the smallest
  first step and surfaces back up, showing you where you are in the tree.
  Closing the overlay only **minimizes** the session — the timer keeps
  running in the background (duck out, add the task you just remembered,
  tap the ▶ pill in the header to come back to the same countdown), and it
  survives reloads. ↺ resets the timer when you'd rather start the block
  over; ⏹ ends the session for good.
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
   screen; everything deeper is a modal or overlay. Projects are tabs over
   that same page, not separate places to navigate to — switching is one
   click and changes nothing but which list is on screen.
3. **Time is visual and honest.** Buffered by default, analog + depleting
   color, elapsed and remaining both shown, staged alarms instead of one.
4. **Deterministic where possible, AI only where needed.** All scheduling,
   buffering, urgency, rollups, focus traversal, and matrix math is local
   code (`app/logic.py`, fully unit-tested). The AI only does language work
   and returns minimal structured JSON.
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
urgency, backward scheduling, subtree rollups, focus traversal, next-task
ordering); `tests/test_api.py` covers the API with the AI stubbed out.

## Layout

```
app/
  main.py     FastAPI routes + static hosting
  db.py       SQLite persistence (schema + migrations, settings, project
              and task CRUD)
  logic.py    deterministic scheduling core — no AI, no I/O
  ai.py       Claude API broker (breakdown / annotate / compile)
  static/     single-page front end (vanilla JS, no build step)
tests/        pytest suite
data/         SQLite database (created at runtime, gitignored)
```
