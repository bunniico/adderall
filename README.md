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

### Resource limits

The container is capped at **1 CPU and 512 MB of RAM** in `docker-compose.yml`.
A FastAPI app over a SQLite file never comes close to that, so the cap mostly
keeps a runaway from taking the machine with it. If you ever do hit it, raise
`deploy.resources.limits` in `docker-compose.yml`.

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
  it and everything in it. Drag a tab along the strip to put it where you
  want it — hold and slide on a touchscreen, or focus a tab and use
  **Shift+←** / **Shift+→** — and that order sticks, on every device;
  reordering never switches tabs, and **←** / **→** on their own just walk
  along the strip. Adding, braindumping, breaking down, ordering and
  focusing all act on the tab you're on and leave the others alone, and the
  app reopens on the tab you were last in. Send a task to another tab from
  its detail modal — its subtasks go with it. Deadline alarms are the one
  thing that deliberately ignores tabs: a cue you'd miss because its task is
  one tab over is exactly what this app exists to prevent, so those fire
  wherever you are and say which project they came from. Existing installs
  upgrade with everything in a first tab called "Tasks".
- **Calendar** — 📅 in the header swaps the list for a calendar with **Day**,
  **Week** and **Month** views. Unlike everything else in the app it spans
  *every* project at once, because "what is due this week" is a question about
  your whole life and not about whichever tab is open; a filter row narrows it
  to one project, to one corner of the impact/effort matrix, or hides the
  deadlines the app assigned itself. **Day** is a real time grid: each task is
  a block ending at its deadline and starting one buffered estimate earlier,
  with its **time tax drawn as a striped tail** so you can see that a "45m"
  job really occupies an hour, the free stretches between blocks labelled with
  how long they are, and a line across the current time, above a running
  count of how much of the day is booked against how much it holds. **Week**
  and **Month**
  are day-by-day lists ranked by **score** — deadline pressure, impact and how
  cheap the task is, folded into one 0–100 number — so the top of each day is
  what that day is actually about. Arrow keys page through, `T` goes to today,
  `D`/`W`/`M` switch view, and clicking any task opens it wherever it lives.
- **Nudge past-due work** — a deadline that has already gone by is the most
  demoralizing thing a list like this can show you, and re-typing a date for
  every item is exactly the friction that leaves it showing. So the overdue
  badge **is a button**: click the red `overdue · … ⏩` on any task, in the
  list or on the calendar, and pick "in 1 hour", "tonight", "tomorrow, same
  time", "in 3 days", "next week" or a moment of your own. The task moves
  **keeping its length** — the estimate never changes, so it takes up exactly
  as much of the new day as it did of the old one — and for anything with
  subtasks the whole plan slides by the same amount, so three days of steps
  stay three days of steps instead of collapsing onto the new date. The
  calendar additionally gathers everything overdue into one rail with a
  *Nudge all* on it, for when the pile is the problem rather than any one
  task in it.
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
- **Sort it however you need to read it** — a **Sort** control above the list
  reorders it by **score** (the app's one-number answer to "what deserves the
  next hour"), **deadline**, **number of subtasks** (open steps, counted all
  the way down — which of these is still a whole project?), or **created
  time**, each way round: soonest or furthest off, most steps or fewest,
  newest or oldest. It is a lens, not a change of plan — the steps nested
  inside a task keep the order the breakdown gave them, and ▶ Focus still
  hands you the most urgent thing, so reading your list by deadline for a
  minute never quietly changes what you are about to work on. Tasks the field
  says nothing about (no deadline, say) sink to the bottom whichever way the
  sort is pointing. **Smart** is the default — urgency first, quick wins
  ahead of slogs — and **Manual** is the list exactly as you dragged it.
- **Your own order** — drag any task by its ⠿ handle: onto the top or bottom
  edge of another task to sit above or below it, into the middle of one to
  nest inside it, or onto empty list space to pull it back out to the top
  level. Works with a mouse, on a touchscreen, and from the keyboard (focus a
  handle and use ↑ ↓ to move, → to nest under the task above, ← to pop out).
  The first move switches the list to **manual order**: it then stays exactly
  as you arranged it — including which task is "next up" — instead of being
  re-sorted. Whatever order you were looking at when you dragged is the order
  it freezes, so a list sorted by deadline keeps its deadline order and only
  the task you moved moves. Picking another **Sort** (or the switch in
  ⚙ Settings, which is the same switch) turns it back off, and everything else
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
  tasks — **nested**, so the steps that add up to one outcome land as
  subtasks under it (up to three levels deep) instead of a flat wall of
  items. Unrelated one-off things stay where they belong, at the top level.
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
- **Deadlines that land on a day that can hold them** — an auto-assigned
  deadline used to be a horizon and nothing else, so fifteen things
  braindumped in one minute came back as fifteen blocks stacked on the same
  afternoon, at the same instant, on a day that could never have held them —
  a plan you bounce off rather than start. Now the app keeps a book of what
  each day already holds — deadlines you set, work it has already placed,
  across *every* tab — and gives each new task the first slot that actually
  fits, laid out through your working day and rolling onto the next one when
  a day is full. It only ever looks forward from the day the horizon asked
  for, so nothing is quietly pulled earlier, and nothing already overdue is
  quietly rescheduled out of the red. A whole task tree is placed as one
  span, so a project's steps still tile the block its parent occupies.
  Everything about it is a preference: the length of a day, the hour it
  starts, and whether to spread work at all, all in ⚙ Settings.
- **A day cap that learns** — the calendar warns when a day is booked past
  what a day holds, and auto-deadlines pack up to that and no further. Eight
  hours is the default, but it is also the number most likely to be wrong for
  any particular person, so **Learn my real day** watches how often you
  actually reach it. Clear it every single day and it rises; never once reach
  it and it falls halfway toward the day you really have — counting only the
  days you finished something on, so a weekend off is not evidence. A goal
  you have never hit isn't a plan, it's a daily notification that you failed,
  which is the exact failure mode this app exists to avoid. The warning always
  says which number it is using and why.
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
   click and changes nothing but which list is on screen. The calendar is the
   same deal: one button swaps the middle of the page for a dated view of the
   same tasks and swaps it back, with no navigation, no URL and nothing to
   find your way home from.
3. **Time is visual and honest.** Buffered by default, analog + depleting
   color, elapsed and remaining both shown, staged alarms instead of one.
4. **Deterministic where possible, AI only where needed.** All scheduling,
   buffering, urgency, priority scores, day-capacity learning, rollups, focus
   traversal, matrix math and deadline-nudging is local code (`app/logic.py`, fully unit-tested). The AI only does language work
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
urgency, priority scores, backward scheduling, load-aware placement, the
learned day cap, subtree rollups, focus traversal, next-task ordering, list
sorting, deadline nudging);
`tests/test_api.py` covers the API with the AI stubbed out.

## Layout

```
app/
  main.py     FastAPI routes + static hosting
  db.py       SQLite persistence (schema + migrations, settings, project
              and task CRUD)
  logic.py    deterministic scheduling core — no AI, no I/O
  ai.py       Claude API broker (breakdown / annotate / compile)
  static/     single-page front end (vanilla JS, no build step)
                app.js is the task list, focus mode and settings;
                calendar.js is the day/week/month views and nudging
tests/        pytest suite
data/         SQLite database (created at runtime, gitignored)
```
