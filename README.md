# ⏳ adderall (beta)

> Note: This application requires a Claude API key and a workspace ID if using an identity-based API key. See Anthropic's API site for more details.

goblin.tools is cool, but it also sucks sometimes.

Here's what I want to try to fix it:

I want to make a single-user, locally hosted web app for people with executive dysfunction
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

**New assignment notifications.** Add URLs under ⚙ Settings → ClickUp sync →
*New assignment webhooks* and each task newly assigned to you is POSTed to
every one as a Discord-style message (`{"content": "..."}`) with its title,
due date and link, ready to forward to Discord, Harmony, or any chat that
takes that shape. A new assignment is one a sync hasn't seen before, so it is
announced on the sync after it's assigned (within
`ADDERALL_CLICKUP_INTERVAL`, or right away with **Sync now**). The first sync
after connecting announces nothing, since everything it finds was already
assigned. Mentions aren't announced: ClickUp's API has no way to list them.

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

### When the connection drops

There are two different connections, and they fail differently.

**Your browser to this app.** If a request to the server never gets a
response — the container is down, the machine is asleep, the network
between you and it drops — the page shows a banner and a clear "Lost
connection to the server" message instead of a silent failure or a cryptic
browser error. Nothing here retries on its own; once the server is back,
the next thing you do just works.

**This app to the outside internet.** Breakdown, re-estimate, the braindump
compiler, and ClickUp sync all call out to an external API. If that call
fails because *this machine* has no route to the internet, the request is
queued instead of failing: you get a toast saying so, and it runs on its own
— retried every `ADDERALL_QUEUE_INTERVAL` seconds (60 by default; `0` turns
retrying off) — once the connection is back, no need to redo anything. A
queued item survives a restart, since it's kept in the same SQLite file as
everything else. This is only for the connection itself: a missing or
invalid API key, or the AI declining a request, still fails immediately —
retrying a request that was never going to work just delays the same
failure. Either way, every attempt is logged, so `docker logs` always shows
what actually happened.

### Using it from other apps and AI agents

Everything the page does goes through a JSON API; its reference is at
<http://localhost:8000/api/docs>. On top of that, two ways in:

**Transition alarms as events.** The stop / get ready / go alarms are raised
by the server (checked every `ADDERALL_ALARM_INTERVAL` seconds, 30 by
default), so they fire whether or not a tab is open, and go to three places:

- the page's banner, as before;
- `GET /api/events`, a server-sent event stream (`event: alarm`, the alarm as
  JSON in `data`) that any local program can listen to:
  `curl -N localhost:8000/api/events`;
- every URL under ⚙ Settings → *Alarm webhooks*. A Discord webhook URL
  (Server Settings → Integrations → Webhooks → Copy URL) gets the alarm as a
  Discord message; any other URL gets the alarm's JSON POSTed to it.

**An MCP server for AI agents** at `http://localhost:8000/mcp` (streamable
HTTP). Its tools list, add, edit, start, complete and delete tasks, pick the
next one, compile a braindump, check off habits, and read the latest alarms.
To connect Claude Code:

```bash
claude mcp add --transport http adderall http://localhost:8000/mcp
```

Nothing here asks who is calling, the same as the rest of the API. The MCP
server only answers requests addressed to `localhost`, but the event stream
and the API answer anyone who can reach the port.

## What it does

Each feature has its own article, explaining how it works and why it was
built that way. They live in [`app/help/`](app/help/) as Markdown and are
also in the app itself under **❓** in the header, so they ship and update
with the code.

- [Welcome](app/help/01-welcome.md)
- [Projects and tabs](app/help/02-projects-and-tabs.md)
- [The All tab](app/help/03-the-all-tab.md)
- [Overview](app/help/04-overview.md)
- [Habits](app/help/05-habits.md)
- [Calendar](app/help/06-calendar.md)
- [Adding tasks](app/help/07-adding-tasks.md)
- [Magic ToDo: breaking tasks down](app/help/08-magic-todo.md)
- [Braindump](app/help/09-braindump.md)
- [Sorting and ordering](app/help/10-sorting-and-ordering.md)
- [Folding and deleting](app/help/11-folding-and-deleting.md)
- [Estimates and the time tax](app/help/12-estimates-and-time-tax.md)
- [Impact and effort](app/help/13-impact-and-effort.md)
- [The score](app/help/14-the-score.md)
- [XP and levels](app/help/15-xp-and-levels.md)
- [Deadlines and start times](app/help/16-deadlines-and-start-times.md)
- [How the app plans your days](app/help/17-planning-your-days.md)
- [Nudging overdue work](app/help/18-nudging-overdue-work.md)
- [Repeating tasks](app/help/19-repeating-tasks.md)
- [Focus mode](app/help/20-focus-mode.md)
- [Transition alarms](app/help/21-transition-alarms.md)
- [Sounds](app/help/22-sounds.md)
- [AI and your budget](app/help/23-ai-and-your-budget.md)
- [ClickUp sync](app/help/24-clickup-sync.md)
- [When the connection drops](app/help/25-when-the-connection-drops.md)
- [Other apps and AI agents](app/help/26-other-apps-and-agents.md)

To add or change one, edit the Markdown file. The number at the front of
the filename sets its place in the list, the first `# ` line is its title,
and links to other articles are plain relative links such as
`[Calendar](06-calendar.md)`.

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
                XP, AI spend, project, task, series and habit CRUD)
  logic.py      deterministic scheduling core — no AI, no I/O
  habits.py     routines: the three frequency shapes, what a tick means, and
                the streaks and rates that come out of them. Pure — days in,
                numbers out, and nothing to do with the scheduler
  recurring.py  what the app does with a recurrence rule: templates, one open
                occurrence at a time, the sweep, and the forecast the calendar
                and the day book plan against
  scheduler.py  the background timer that runs that sweep
  events.py     transition alarms: the timer that raises them, the event
                stream the page listens to, and the webhooks
  mcp_server.py the MCP server AI agents connect to at /mcp
  ai.py         Claude API broker (breakdown / annotate / compile), model
                prices, and the budget throttle applied at the call site
  clickup.py    ClickUp sync: the API client, the one-way import into a
                dedicated project, and its own background timer
  static/       single-page front end (vanilla JS, no build step)
                  app.js is the task list, repeat controls, focus mode
                  and settings; calendar.js is the day/week/month views
                  and nudging; overview.js is the Overview tab's tiles,
                  shortlists and hand-rolled SVG charts; habits.js is the
                  Habits tab, its editor and the year-of-squares heatmap;
                  help.js is the Help view and its small Markdown renderer
  help/         the in-app help: one Markdown article per feature, served
                by /api/help
tests/          pytest suite
data/           SQLite database (created at runtime, gitignored)
```
