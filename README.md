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

### ClickUp sync

Pulls every open task assigned to you, across every ClickUp workspace your
token can see, into its own **ClickUp** tab — created the first time a sync
runs. Set a personal API token (from ClickUp → Settings → Apps) in ⚙
Settings → ClickUp sync, or via `CLICKUP_API_TOKEN` in the environment.

It's a one-way mirror, and deliberately simple about it: re-syncing refreshes
an imported task's title, description and due date from ClickUp, but nothing
else — its estimate, impact/effort, status, subtasks and which tab it lives
in are yours, and sync never touches them once it exists. Nothing is pushed
back to ClickUp, and a task that stops showing up as "assigned and open" in
ClickUp (finished, reassigned, deleted) is simply left alone rather than
auto-completed or removed here.

Runs on the same kind of sweep as the recurring-task job below — once at
startup, then every `ADDERALL_CLICKUP_INTERVAL` seconds (1800 by default; `0`
turns it off) — plus **Sync now** in Settings for an on-demand pass. No
token configured yet is not an error; the sweep just has nothing to do.

### Seeing what the AI is doing

Every Claude call writes what it sent and what came back to the container's
stdout, so `docker compose logs -f` (or `docker logs -f <container>`) shows the
prompt, the model's thinking summary when it thought, and the JSON it returned:

```
2026-08-30 17:22:41,903 INFO    app.ai | AI call → tier=deep model=claude-opus-5 max_tokens=16000 effort=high thinking=adaptive
2026-08-30 17:22:41,903 INFO    app.ai | AI input:
Turn this braindump into a list of discrete, actionable tasks. …
2026-08-30 17:23:04,118 INFO    app.ai | AI response ← model=claude-opus-5 stop_reason=end_turn input_tokens=612 output_tokens=1840
2026-08-30 17:23:04,118 INFO    app.ai | AI thinking:
The car items are three steps toward one outcome, so they should nest …
2026-08-30 17:23:04,119 INFO    app.ai | AI output:
{"tasks": [{"title": "Book the car in for its MOT", …
```

Two environment variables tune it, both already wired into
`docker-compose.yml`:

| Variable | Default | Effect |
| --- | --- | --- |
| `ADDERALL_LOG_LEVEL` | `INFO` | `DEBUG` also logs the (constant) system prompt; `WARNING` keeps only failures. |
| `ADDERALL_AI_LOG_CHARS` | `4000` | Characters of any one prompt or completion a log line carries. `0` means no limit. |

API keys are never logged.

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
  to one project, to one corner of the impact/effort matrix, hides the
  deadlines the app assigned itself, or hides the copies your repeating tasks
  have not made yet (see **Repeats ahead** below — they are on by default,
  because a week of standing commitments is most of what a week is). **Day** is a real time grid: each task is
  a block ending at its deadline and starting one buffered estimate earlier,
  with its **time tax drawn as a striped tail** so you can see that a "45m"
  job really occupies an hour, the free stretches between blocks labelled with
  how long they are, and a line across the current time, above a running
  count of how much of the day is booked against how much it holds. **Week**
  and **Month**
  are day-by-day lists ranked by **score** — deadline pressure, impact, effort
  and how long it takes, folded into one 0–100 number — so the top of each day
  is what that day is actually about. Arrow keys page through, `T` goes to today,
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
- **Delete, with the cascade spelled out** — every task carries a **✕** at
  the end of its row, finished ones included, and it deletes: not "discard",
  which keeps the task and marks it dropped, but gone from the database.
  Because the schema cascades, deleting a task deletes everything nested under
  it — and that is exactly the thing you can't see at the moment you click,
  since a container is one line by design and may well be folded shut. So a
  task with subtasks doesn't get a bare "are you sure": it is counted ("has 7
  subtasks nested under it, and they all go too"), the first few are named,
  and the total you are about to lose is on the button. A task with nothing
  under it just asks once and gets out of the way.
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
- **One score, four signals** — **urgency** (40%), **impact** (30%), **effort**
  (15%) and **time cost** (15%) fold into a single 0–100 number. Effort and
  time are counted separately because they are different costs: a form you
  dread for ten minutes is cheap on the clock and dear in effort, three hours
  of mindless data entry is the other way round. Time cost decays rather than
  scaling flat — ten minutes scores 8.6, an hour 5, a whole day about 1 —
  because shaving twenty minutes off a half-hour job changes whether you do it
  now, and shaving twenty off a six-hour one changes nothing. A task nobody has
  estimated or rated sits at neutral, never at zero.
- **The score is on the task** — that number used to exist only inside the
  app's own head and on the calendar chips. Every active task now wears it as
  a `★ 62` badge, and the detail modal spells out what it is made of, so the
  order the list is in is an order you can check rather than one you have to
  take on trust.
- **XP and levels** — finishing a task pays out its score as XP: worth 62,
  earns 62. One number, not two — there is nothing extra to learn and nothing
  to farm, and the way to earn more is to do the work that was worth more. A
  level and a bar sit in the top-right corner, the bar slides as the XP lands
  and `+62 XP` floats up under it, and passing a level runs the bar out to
  full before starting the new one. Levels sit steadily further apart (100 XP
  to level 2, another 200 to level 3), the Done list keeps what each task
  paid, and XP already earned stays earned — a task that has paid once never
  pays again, however often it is reopened, and deleting it later costs you
  nothing. Containers pay through their steps, never twice for the same
  afternoon. The whole thing is one switch in ⚙ Settings, along with the
  confetti.
- **A parent is its parts** — a task with subtasks gets no score of its own.
  It inherits the combined score of the work still underneath it, each step
  weighing what it costs in minutes, all the way down the tree. Finished and
  discarded steps stop counting, so a project is worth what is left of it —
  and re-rating a container changes nothing, because the work is in the steps.
- **Deadlines & urgency** — set deadlines yourself or let the app auto-assign
  them (toggleable). Subtasks are backward-scheduled from their parent's
  deadline using buffered estimates. Urgency rises as remaining time shrinks
  relative to the (buffered) work left, and drives the "what next" ordering.
- **Start times — when a thing wants to *begin*** — a deadline says when work
  has to be finished, which for most of what people actually write down is the
  harder question and the less useful answer. *Eat dinner* is a six o'clock
  thing; it has no deadline in any meaningful sense, and a list that can only
  say "due Thursday" has nothing to do with it. So every task can also carry a
  **start time**, and that is what the scheduler places it from: the task lands
  in the first slot that fits from that hour, on that day — after office hours
  if that is when it happens, because the working window is where the app puts
  work it chose the hour for *itself*, not a rule about when you are allowed to
  eat. Set it in the task's dialog, where **Now · In an hour · This evening ·
  Tomorrow morning · Next week · Some day** are one click each, because a
  datetime picker is exactly the friction this app exists to remove.
  It cuts both ways, and the second way is the point. A start time a few hours
  out makes a task urgent on its own account — it climbs the list as its hour
  approaches, whatever its deadline says — while **Some day** parks something
  a month out where it stops competing for this afternoon. *Finish that game*
  sinks to a score of 20 and stays there until it is nearly time; the evening
  it was quietly taking is handed back to the work that needed it.
  That handing-back is literal: when two tasks want the same day and the day
  cannot hold both, the one that is worth more takes it and the other moves to
  the next day with room. Placement used to follow whatever order the list
  happened to be in, so the first thing you ever typed got first pick of every
  afternoon; now the day is handed out by score — deadline *and* start
  pressure, impact, effort, length — and a task with nothing to say about when
  it should start sits at neutral, so a list that never touches the feature
  schedules exactly as it did before. Nudging a task carries its start time
  along with its deadline, so a plan pushed to next week keeps its shape at
  both ends instead of claiming it should have begun last Tuesday.
- **And the AI fills it in for you** — the field is only worth having if you
  never have to touch it, so each new task gets a suggested start time from the
  same fast batched call that estimates it: the model is told the local date
  and hour and answers with an offset, which is what keeps every timezone and
  date-format trap out of the loop. *Eat dinner* comes back as this evening,
  *renew the passport* as a weekday soon, *finish that game* as weeks out —
  and it is asked to say so with a big number rather than a small one, because
  putting the things that do not matter near the front is how a list stops
  being usable. Steps inside a task never get one: a step is scheduled inside
  the slot its parent was given, which is what keeps a plan one readable block
  rather than a scatter. Override any of it by hand, or turn the whole thing
  off in ⚙ Settings.
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
  span, so a project's steps still tile the block its parent occupies. A task
  with a **start time** skips the horizon entirely — it already knows which
  day and which hour it wants — and the cap does not get a veto over it, the
  same way it does not over a deadline you set yourself: you still have to eat
  dinner on a day that is already full, and the calendar saying the day is
  overbooked is the honest answer rather than moving the meal.
  The book holds **work that comes back** as well as work that exists: a
  weekday that will be eight hours of the standing job is a full weekday, and
  the app plans around it rather than finding out on the day. When genuinely
  nothing has room — every day between here and the horizon already full —
  the overflow goes to the emptiest day there is, after what is already booked
  on it, rather than piling onto one afternoon at one instant.
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
- **Work that comes back** — some things are not tasks, they are rhythms: bins
  on Tuesday, rent on the first, the standup every weekday morning. Any
  top-level task can repeat **daily**, **weekly**, **monthly** or **yearly**,
  and **Custom…** is those same four with their knobs turned up — every 3 days,
  every other week on Mon & Thu, the last Friday of every second month, the
  31st (clamped, so a bill at the end of the month is at the end of February
  too). Give it a time of day or leave it to land at the end of your working
  day; end it after a number of times, on a date, or never; and tick **count
  from when I finish it** when "every 3 days" means three days after you
  actually do it rather than three days after it was due. The dialog shows the
  next four real dates as you type, because *27 Feb · 24 Apr · 26 Jun* says
  what "the last Friday of every second month" means far better than the rule
  does.
- **One copy at a time** — the single decision the whole feature is built
  around. A daily chore you ignored for a fortnight comes back as **one** row,
  not fourteen: that pile is the thing this app exists to prevent, and it is
  also a lie — you are one bin-day behind, not fourteen. So a rhythm whose date
  came round again while the last copy was still sitting there quietly steps to
  the next one instead of stacking. Finish a copy and the next is timed from
  there; clearing a month-old backlog hands you the next one, never last
  month's. Editing this week's copy — a better title, a truer estimate, one more
  step — is how you edit the series, subtasks and all, and a step you dropped
  stays dropped. Discarding or deleting one copy skips that one; **Repeat →
  Doesn't repeat** is how you end the job itself, and the delete confirmation
  says so rather than letting you find out next week. Finish a copy and the
  next one appears as soon as its day is close enough to be worth seeing — a
  day of lead means a day, so ticking off this morning's chore puts tomorrow's
  on the list before you close the laptop, whatever hour tomorrow's is due at.
  When the next one is further off than that, the app says when it lands
  rather than leaving you looking at an empty list wondering if the repeat
  broke.
- **Repeats ahead, on the calendar** — one copy at a time is the right answer
  for a list and the wrong one for a calendar: a fortnight in which you will
  work eight hours a day looked like a fortnight of free afternoons, because
  thirteen of those days had no task on them yet. So the calendar draws the
  copies each rhythm still owes — three months of them — as outlined blocks:
  real dates, real lengths, nothing to tick off, and a click opens the copy
  that *is* on your list. They count toward how full a day is, so the day cap
  warns against the day you are actually going to have, and **the scheduler
  books around them**: a new task is given a day that is not already eight
  hours of the same job. Turn them off with **Repeats ahead** in the calendar
  filters if you would rather see only what exists.
- **Transition alarms** — three staged cues before any deadline (stop what
  you're doing → get ready → go), with different sounds and configurable
  lead times.
- **ClickUp sync** — a **ClickUp** tab that mirrors every open task assigned
  to you, pulled in on a background timer or on demand with **Sync now** in
  ⚙ Settings. One-way: ClickUp owns an imported task's title, description
  and due date, everything else is yours. See **ClickUp sync** above.

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
   buffering, urgency, priority scores, the XP curve, day-capacity learning,
   rollups, focus traversal, matrix math, recurrence dates and deadline-nudging
   is local code (`app/logic.py`, fully unit-tested), start-time placement
   included: the model says a task feels like an evening thing, and the app
   works out which evening, which hour of it, and what moves. The AI only does
   language work and returns minimal structured JSON. The front end has no date
   arithmetic at all: even the "next four dates" preview in the repeat dialog
   is the server's answer, so the dialog, the badge on the task and the
   schedule itself cannot drift into telling three different stories about one
   rule.
5. **Low friction.** No accounts, one command, zero configuration required.

## AI model routing

Claude API only, routed by cost/quality (configurable in the DB settings):

| Call | Default model | Thinking | Batched |
|---|---|---|---|
| Estimates + impact/effort scores + start times | `claude-haiku-4-5` | off | yes — one call per batch of tasks |
| Task breakdown (Magic ToDo) | `claude-sonnet-5` | low effort | no (interactive) |
| Braindump compiler | `claude-opus-5` | adaptive | yes — one call |

Every call uses structured JSON output (`output_config.format`), so responses
are compact and parse deterministically. The system prompt is cache-marked.

The model never returns a date. Start times come back as `start_in_minutes`,
an offset from a local clock the prompt states outright, which keeps every
timezone, format and end-of-month question on the app's side of the line —
the same rule the rest of the app follows, where the AI makes the language
judgment and the scheduling arithmetic all happens locally.

### Throttling against a daily budget

Set a **daily AI budget** in Settings and the routing above becomes a ceiling
rather than a fixture. Every response is costed from the tokens it reports
against a price table in `app/ai.py` and booked in the `spend` table, and the
day's running total pulls the routing down a rung at a time:

| Spent today | Estimates + scores | Task breakdown | Braindump compiler |
|---|---|---|---|
| under half | fast | balanced | deep |
| half | fast | balanced | **balanced** |
| three quarters | fast | **fast** | balanced |
| all of it | fast | fast | **fast** |

Each step substitutes the tier a call asked for, applied once rather than
cascaded, which is why the middle rows differ: at three quarters a braindump
is still worth the balanced model while an interactive breakdown is not. A
throttled call also loses its thinking and its effort setting, because those
are what the dearer tiers are for and the fast tier's model may reject them
outright.

The budget never refuses a call. An app that stops working at 4pm is worse
than one that gets a little blunter, and the fast tier is the floor — spend
past the budget and everything simply keeps running on it. The day is a local
one, so the total rolls over at your midnight, not UTC's; the badge in the
header appears only while a budget is actually biting. A budget of 0, the
default, turns the whole mechanism off and costs not even a query.

The figure is an estimate, not an invoice: the token counts are the API's own,
but the prices are a hardcoded table that will drift, and a model the table has
never heard of is costed at the dearest rate known — a budget that guesses low
is a budget that does not hold.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_logic.py` covers the deterministic core (buffering, quadrants,
urgency, priority scores, XP and levels, backward scheduling, load-aware
placement, the learned day cap, subtree rollups, focus traversal, next-task
ordering, list sorting, deadline nudging, the budget throttle's ladder);
`tests/test_ai_client.py` covers the broker: client construction, logging, what
a call costs and which model the budget lets it have;
`tests/test_recurrence.py` covers recurrence end to end — the date arithmetic
(interval phase, weekday sets, month-length clamping, nth and last weekdays,
DST, end conditions) and then the app around it, with the sweep driven by hand
so the tests own the clock;
`tests/test_api.py` covers the API with the AI stubbed out;
`tests/test_clickup.py` covers the ClickUp client against mocked HTTP (no
network) and what a sync creates, updates and deliberately leaves alone.

### The recurring-task sweep

The scheduled job runs inside the app process: once at startup, then every
`ADDERALL_RECUR_INTERVAL` seconds (3600 by default). It is a sweep on a timer
rather than a job pinned to midnight on purpose — this runs on a laptop that
sleeps and a box that gets unplugged, and a job that only works if the process
happens to be awake at 00:00 is a job that silently stops working. The pass is
idempotent, so whether it last ran an hour ago or three weeks ago it brings
every rhythm to the same place.

A copy is made once its occurrence is within the rule's lead, which is counted
in whole local days: one day of lead means "tomorrow's copy is welcome today",
not "welcome 24 hours before it is due". The difference is the whole feature —
measured in hours, a chore due at six tomorrow evening is out of reach until
six tonight, so finishing this morning's leaves an empty list. Between sweeps,
finishing, discarding or deleting a copy runs the same step immediately.

Set `ADDERALL_RECUR_INTERVAL=0` to turn the background job off and drive
`POST /api/recurring/run` from a real cron instead; that route runs exactly the
same pass and hands back the page.

## Layout

```
app/
  main.py       FastAPI routes + static hosting
  db.py         SQLite persistence (schema + migrations, settings, lifetime
                XP, AI spend, project, task and series CRUD)
  logic.py      deterministic scheduling core — no AI, no I/O
  recurring.py  what the app does with a recurrence rule: templates, one open
                occurrence at a time, the sweep, and the forecast the calendar
                and the day book plan against
  scheduler.py  the background timer that runs that sweep
  ai.py         Claude API broker (breakdown / annotate / compile), model
                prices, and the budget throttle applied at the call site
  clickup.py    ClickUp sync: the API client, the one-way import into a
                dedicated project, and its own background timer
  static/       single-page front end (vanilla JS, no build step)
                  app.js is the task list, repeat controls, focus mode
                  and settings; calendar.js is the day/week/month views
                  and nudging
tests/          pytest suite
data/           SQLite database (created at runtime, gitignored)
```
