/* Adderall calendar.
 *
 * Day, week and month over every project at once. The list view is one tab at
 * a time on purpose; the calendar is deliberately the opposite, because "what
 * is due this week" is a question about your whole life rather than about
 * whichever tab happens to be open. Filtering by project or category happens
 * here, over one payload.
 *
 * The server sends UTC instants and knows nothing about timezones. Everything
 * to do with *days* — which day a task lands on, where midnight is, what "this
 * week" means — is worked out here, in the browser's own timezone, because
 * this is the only side that has one.
 *
 * Loaded before app.js: app.js boots as it is parsed and that boot may open
 * straight into the calendar, so these functions have to exist by then. It
 * borrows helpers from app.js ($, api, toast, fmtMinutes, openDetail,
 * applyState) — all of which are only ever called after app.js has run.
 */

"use strict";

const CAL_KEY = "adderall.calendar.v1";
const CAL_PX_PER_MIN = 0.9;        // day-view scale: 24h ≈ 1300px
const CAL_MIN_BLOCK_PX = 22;       // a 5-minute task still has to be clickable
const CAL_GAP_MIN = 20;            // gaps at least this long are worth drawing
const CAL_MONTH_CHIPS = 3;         // per month cell before "+N more"
const CAL_OVERDUE_CHIPS = 8;

/* View state. Deliberately not on the server: which week you are looking at
 * is a viewport, not data — the same class of thing as a scroll position. The
 * view mode and filters do persist, per device, because coming back to the
 * month view you left is worth a localStorage line. */
const cal = {
  mode: false,          // calendar shown instead of the task list
  view: "week",         // day | week | month
  cursor: new Date(),   // the day the current view is anchored on
  events: [],
  loaded: false,
  loading: false,
  stale: false,         // something changed while the calendar was off screen
  dayScroll: null,      // {key, top} — where you had scrolled the day grid
  renderedDay: null,    // the day the grid on screen was drawn for
  capacity: null,       // the day cap and how it got there (see capacityNote)
  filters: { project: "", category: "", auto: true, done: false, repeats: true },
};

const CAL_QUAD_ICON = {
  quick_win: "⚡", major_project: "🏔", fill_in: "·", thankless: "😮‍💨",
};

/* ---------------- persistence ---------------- */

function saveCalendarPrefs() {
  try {
    localStorage.setItem(CAL_KEY, JSON.stringify({
      mode: cal.mode, view: cal.view, filters: cal.filters,
    }));
  } catch {}
}

function restoreCalendarPrefs() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(CAL_KEY) || "null"); } catch {}
  if (!saved) return;
  if (["day", "week", "month"].includes(saved.view)) cal.view = saved.view;
  if (saved.filters) Object.assign(cal.filters, saved.filters);
  cal.mode = !!saved.mode;
}

/* ---------------- local-time date maths ----------------
 * All of it deliberately local: a deadline at 23:30 belongs to the day it
 * reads as on your wall, not to whatever day it is in UTC. */

function startOfDay(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

function addDays(d, n) {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

function addMonths(d, n) {
  const x = new Date(d);
  x.setDate(1);
  x.setMonth(x.getMonth() + n);
  return x;
}

function weekStartDay() {
  return Number(settings?.week_start ?? 0) === 1 ? 1 : 0;
}

function startOfWeek(d) {
  const start = weekStartDay();
  const x = startOfDay(d);
  return addDays(x, -((x.getDay() - start + 7) % 7));
}

function startOfMonth(d) {
  const x = startOfDay(d);
  x.setDate(1);
  return x;
}

function dayKey(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function sameDay(a, b) {
  return dayKey(a) === dayKey(b);
}

function minutesIntoDay(date, day) {
  return (date - startOfDay(day)) / 60000;
}

function fmtClock(d) {
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/* ---------------- data ---------------- */

async function loadCalendar() {
  if (cal.loading) return;
  cal.loading = true;
  try {
    const data = await api("/calendar");
    cal.events = data.events || [];
    cal.capacity = data.capacity || null;
    cal.loaded = true;
    cal.stale = false;
  } catch (e) {
    toast("Could not load the calendar: " + e.message, true);
  } finally {
    cal.loading = false;
  }
}

/* Anything that changes a task changes the calendar, and the calendar is the
 * thing on screen — so it refetches rather than trying to patch itself. Off
 * screen it is only marked stale: the task list has no business paying for a
 * calendar fetch on every keystroke's worth of work. */
async function refreshCalendar() {
  if (!cal.mode) { cal.stale = true; return; }
  await loadCalendar();
  renderCalendar();
}

/* The calendar spans every project, so a task opened from it is often not in
 * the list view's state at all. Its event carries enough of the task for the
 * detail modal to work. */
function calendarTask(id) {
  const e = cal.events.find((ev) => ev.id === id);
  // A forecast block is not a task: it has no row in the database and nothing
  // the detail modal could save. Clicking one opens the copy that *is* on
  // your list instead — see openProjected.
  return e && !e.projected ? { ...e, subtasks: [] } : null;
}

/* Clicking a block that does not exist yet. The job it is a forecast of does
 * exist — one copy of a repeating task is always on your list — so that is
 * what opens: the place where changing the estimate, the steps or the rule
 * changes every outline after it too. When there is no copy on the list (you
 * finished this week's and the next is not due yet) there is genuinely
 * nothing to open, so it says when it lands instead. */
function openProjected(e) {
  if (e.source_task_id && findAnyTask(e.source_task_id)) {
    openDetail(e.source_task_id);
    return;
  }
  const when = new Date(e.deadline);
  toast(`“${e.title}” repeats ${e.recurrence?.summary || ""} — this one lands ` +
        `${when.toLocaleString()}. It joins your list nearer the time.`);
}

function openEvent(e) {
  if (e.projected) openProjected(e);
  else openDetail(e.id);
}

function calEvents() {
  const f = cal.filters;
  return cal.events.filter((e) =>
    (!f.project || e.project_id === f.project) &&
    (!f.category ||
      (f.category === "__none" ? !e.quadrant : e.quadrant === f.category)) &&
    (f.auto || e.deadline_source !== "auto") &&
    (f.repeats || !e.projected) &&
    (f.done || e.status !== "done"));
}

function eventEnd(e) { return new Date(e.deadline); }
function eventStart(e) {
  return new Date(new Date(e.deadline).getTime() - e.length_min * 60000);
}

function byScore(a, b) {
  return (b.score ?? 0) - (a.score ?? 0) ||
         new Date(a.deadline) - new Date(b.deadline);
}

function byTime(a, b) {
  return new Date(a.deadline) - new Date(b.deadline);
}

function groupByDay(events) {
  const map = new Map();
  for (const e of events) {
    const key = dayKey(new Date(e.deadline));
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(e);
  }
  return map;
}

function isOverdue(e) {
  return e.status !== "done" && new Date(e.deadline) < new Date();
}

/* ---------------- how full a day is ----------------
 * How much a day costs, and what it is allowed to cost. */

/* Leaves only. A task with subtasks is drawn as a block spanning the work its
 * steps add up to, and its steps are drawn inside it, so adding every block on
 * screen together would charge the day twice for the same afternoon. */
function dayLoad(events, field = "length_min") {
  return events.reduce((n, e) => n + (e.has_subtasks ? 0 : e[field]), 0);
}

function capacityMinutes() {
  return cal.capacity?.minutes || Number(settings?.day_capacity) || 8 * 60;
}

/* Why the warning uses the number it uses. A cap that quietly disagrees with
 * the one in Settings is worse than no cap at all, so it says so out loud. */
function capacityNote() {
  const c = cal.capacity;
  if (!c) return "";
  if (!c.learned) {
    return `Your day cap is ${fmtMinutes(c.base)}. Change it in ⚙ Settings.`;
  }
  return `You set ${fmtMinutes(c.base)}, but you reach that on ` +
    `${Math.round((c.hit_rate ?? 0) * 100)}% of the ${c.days} days you ` +
    `finished anything on — a typical one is ${fmtMinutes(c.typical)}. So the ` +
    `warning has moved to ${fmtMinutes(c.minutes)}, and new deadlines are ` +
    `packed to that. Turn this off with “learn my real day” in ⚙ Settings.`;
}

/* ---------------- mode switching ---------------- */

function setCalendarMode(on) {
  cal.mode = on;
  document.body.classList.toggle("calendar-mode", on);
  $("calendar-view").hidden = !on;
  $("list-view").hidden = on;
  // The calendar has its own project filter; two project pickers on screen at
  // once would just disagree with each other.
  $("project-tabs").hidden = on;
  $("btn-view").textContent = on ? "☰ List" : "📅 Calendar";
  $("btn-view").title = on
    ? "Back to the task list"
    : "See everything with a date on it, across every project";
  saveCalendarPrefs();
  if (!on) return;
  if (!cal.loaded || cal.stale) loadCalendar().then(renderCalendar);
  else renderCalendar();
}

function toggleCalendarMode() {
  setCalendarMode(!cal.mode);
}

function setCalendarView(view) {
  cal.view = view;
  saveCalendarPrefs();
  renderCalendar();
}

function calendarStep(dir) {
  if (cal.view === "day") cal.cursor = addDays(cal.cursor, dir);
  else if (cal.view === "week") cal.cursor = addDays(cal.cursor, 7 * dir);
  else cal.cursor = addMonths(cal.cursor, dir);
  renderCalendar();
}

function calendarToday() {
  cal.cursor = new Date();
  renderCalendar();
}

function openCalendarDay(day) {
  cal.cursor = new Date(day);
  cal.view = "day";
  saveCalendarPrefs();
  renderCalendar();
}

/* ---------------- chrome ---------------- */

function rangeLabel() {
  const d = cal.cursor;
  if (cal.view === "day") {
    return d.toLocaleDateString(undefined,
      { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  }
  if (cal.view === "week") {
    const a = startOfWeek(d), b = addDays(a, 6);
    const sameMonth = a.getMonth() === b.getMonth();
    const left = a.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const right = b.toLocaleDateString(undefined,
      sameMonth ? { day: "numeric" } : { month: "short", day: "numeric" });
    return `${left} – ${right}, ${b.getFullYear()}`;
  }
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function renderCalendarChrome() {
  $("cal-range").textContent = rangeLabel();
  for (const btn of document.querySelectorAll(".cal-view-btn")) {
    const on = btn.dataset.view === cal.view;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", String(on));
  }

  const select = $("cal-project");
  const projects = state.projects || [];
  const wanted = cal.filters.project;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All projects";
  select.appendChild(all);
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    select.appendChild(opt);
  }
  // A project deleted in another tab must not leave the filter pointing at
  // nothing and the calendar looking empty.
  select.value = projects.some((p) => p.id === wanted) ? wanted : "";
  cal.filters.project = select.value;

  $("cal-category").value = cal.filters.category;
  $("cal-auto").checked = cal.filters.auto;
  $("cal-repeats").checked = cal.filters.repeats;
  $("cal-done").checked = cal.filters.done;
}

/* ---------------- render ---------------- */

function renderCalendar() {
  if (!cal.mode) return;
  renderCalendarChrome();
  const body = $("cal-body");
  // The day grid is rebuilt every minute to keep the now-line honest; having
  // it scroll back to the top each time would make the evening unreachable.
  const grid = body.querySelector(".cal-grid");
  // Keyed by the day that grid was drawn for, not by the cursor — paging to
  // the next day must not inherit yesterday's scroll position.
  if (grid) cal.dayScroll = { key: cal.renderedDay, top: grid.scrollTop };
  body.replaceChildren();
  body.className = "cal-body cal-" + cal.view;

  const events = calEvents();
  const ahead = events.filter((e) => e.projected).length;
  const real = events.length - ahead;
  $("cal-count").textContent = cal.loaded
    ? `${real} scheduled task${real === 1 ? "" : "s"}` +
      (ahead ? ` · ${ahead} repeat${ahead === 1 ? "" : "s"} ahead` : "")
    : "loading…";

  renderOverdueRail(events);

  if (cal.view === "day") renderDayView(body, events);
  else if (cal.view === "week") renderWeekView(body, events);
  else renderMonthView(body, events);
}

/* ---- the overdue rail ----
 * A pile of deadlines that have already gone by is the single most
 * demoralizing thing a list like this can show you, and re-typing a date for
 * every one of them is exactly the friction that leaves it showing. So the
 * pile gets its own row, above every view, with one button that clears it. */

function renderOverdueRail(events) {
  const rail = $("cal-overdue");
  rail.replaceChildren();
  const overdue = events.filter(isOverdue).sort(byScore);
  rail.hidden = overdue.length === 0;
  if (!overdue.length) return;

  const head = document.createElement("div");
  head.className = "cal-overdue-head";
  const label = document.createElement("span");
  label.textContent = `⏰ ${overdue.length} past due`;
  const nudgeAll = document.createElement("button");
  nudgeAll.className = "accent";
  nudgeAll.textContent = overdue.length === 1 ? "Nudge it…" : "Nudge all…";
  nudgeAll.title = "Move these onto new deadlines, each keeping its length";
  nudgeAll.addEventListener("click", () => openNudge(overdue));
  head.append(label, nudgeAll);

  const strip = document.createElement("div");
  strip.className = "cal-overdue-strip";
  for (const e of overdue.slice(0, CAL_OVERDUE_CHIPS)) {
    strip.appendChild(eventChip(e, { showTime: true, showDate: true, nudge: true }));
  }
  if (overdue.length > CAL_OVERDUE_CHIPS) {
    const more = document.createElement("span");
    more.className = "cal-more muted";
    more.textContent = `+${overdue.length - CAL_OVERDUE_CHIPS} more`;
    strip.appendChild(more);
  }
  rail.append(head, strip);
}

/* ---- shared chip ---- */

/* One task, anywhere outside the day grid. `stacked` puts the time above the
 * title instead of beside it: in a seven-column week, or a month cell, a
 * one-line chip spends most of its width on the clock and leaves the title
 * as "Ship the qu…", which is no use to anyone. */
function eventChip(e, opts = {}) {
  const chip = document.createElement("div");
  chip.className = "cal-chip quad-" + (e.quadrant || "none") +
    (opts.stacked ? " stacked" : "") + (e.projected ? " projected" : "") +
    (e.status === "done" ? " done" : "") + (isOverdue(e) ? " overdue" : "");

  const open = document.createElement("button");
  open.className = "cal-chip-open";
  open.title = chipTooltip(e);

  if (opts.showTime) {
    const time = document.createElement("span");
    time.className = "cal-chip-time";
    const when = new Date(e.deadline);
    time.textContent = opts.showDate
      ? when.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
        " " + fmtClock(when)
      : fmtClock(when);
    open.appendChild(time);
  }

  const title = document.createElement("span");
  title.className = "cal-chip-title";
  // A block you will see again next week reads differently from a one-off on
  // the same date, and on a chip there is only room to say so with a mark.
  title.textContent = (e.recurrence?.active ? "🔁 " : "") + e.title;
  open.appendChild(title);

  const score = document.createElement("span");
  score.className = "cal-score";
  score.textContent = Math.round(e.score ?? 0);
  score.title = `Score ${Math.round(e.score ?? 0)}/100 — urgency, impact, ` +
    `effort and time cost combined. Higher comes first.`;
  open.appendChild(score);

  open.addEventListener("click", () => openEvent(e));
  chip.appendChild(open);

  if (opts.nudge && isOverdue(e)) {
    const btn = document.createElement("button");
    btn.className = "cal-chip-nudge";
    btn.textContent = "⏩";
    btn.title = "Nudge this to a new deadline, same length";
    btn.setAttribute("aria-label", "Nudge " + e.title);
    btn.addEventListener("click", (ev) => { ev.stopPropagation(); openNudge([e]); });
    chip.appendChild(btn);
  }
  return chip;
}

function chipTooltip(e) {
  const bits = [e.title];
  if (e.path?.length) bits.push("in " + e.path.join(" › "));
  bits.push(`${e.project_name} · ${fmtMinutes(e.length_min)}`);
  // Said first, because it changes what every other line here means: this is
  // time the app has already spent on your behalf, not a task you can tick.
  if (e.projected) {
    bits.push("not on your list yet — a copy this repeat will make");
    if (e.subtask_count) bits.push(`${e.subtask_count} step${e.subtask_count === 1 ? "" : "s"}`);
  }
  if (e.quadrant) bits.push(CAL_QUAD_ICON[e.quadrant] + " " + e.quadrant.replace("_", " "));
  bits.push(`score ${Math.round(e.score ?? 0)}`);
  if (e.recurrence) bits.push("repeats " + e.recurrence.summary);
  // Where the block sits, said out loud: for anything the app scheduled
  // itself the start time *is* the left edge of this block, and for a task
  // with a deadline you set it is the intention the deadline overrode.
  if (e.start_at) bits.push("starts " + new Date(e.start_at).toLocaleString());
  if (e.deadline_source === "auto") bits.push("deadline assigned by the app");
  if (e.has_subtasks) bits.push(`${e.subtask_count} subtask${e.subtask_count === 1 ? "" : "s"}`);
  return bits.join(" · ");
}

/* ---- day view ----
 * A real time grid, because "today" is the one view where *when* matters more
 * than *what*. Each task is a block ending at its deadline and starting one
 * buffered estimate earlier, so the block is the shape of the commitment. The
 * striped tail is the time-tax buffer, and the bands between blocks are the
 * slack you actually have — the two things a plain list hides. */

function renderDayView(root, events) {
  const day = cal.cursor;
  const today = events
    .filter((e) => sameDay(new Date(e.deadline), day))
    .sort(byTime);

  root.appendChild(dayScheduleSummary(today, day));

  if (!today.length) {
    const empty = document.createElement("p");
    empty.className = "cal-empty muted";
    empty.textContent = sameDay(day, new Date())
      ? "Nothing due today. Genuinely nothing — enjoy it."
      : "Nothing due on this day.";
    root.appendChild(empty);
    return;
  }

  const grid = document.createElement("div");
  grid.className = "cal-grid";
  cal.renderedDay = dayKey(day);
  const height = 24 * 60 * CAL_PX_PER_MIN;

  const hours = document.createElement("div");
  hours.className = "cal-hours";
  hours.style.height = height + "px";
  for (let h = 0; h < 24; h++) {
    const row = document.createElement("div");
    row.className = "cal-hour";
    row.style.top = h * 60 * CAL_PX_PER_MIN + "px";
    row.style.height = 60 * CAL_PX_PER_MIN + "px";
    const label = document.createElement("span");
    label.className = "cal-hour-label";
    label.textContent = new Date(2000, 0, 1, h)
      .toLocaleTimeString(undefined, { hour: "numeric" });
    row.appendChild(label);
    hours.appendChild(row);
  }

  const canvas = document.createElement("div");
  canvas.className = "cal-canvas";
  canvas.style.height = height + "px";

  const placed = today.map((e) => {
    const end = eventEnd(e);
    const startMin = Math.max(0, minutesIntoDay(eventStart(e), day));
    const endMin = Math.min(24 * 60, minutesIntoDay(end, day));
    return { e, startMin, endMin: Math.max(endMin, startMin + 1) };
  });

  for (const band of freeBands(placed)) canvas.appendChild(gapBand(band));
  for (const item of layoutColumns(placed)) canvas.appendChild(dayBlock(item, day));

  if (sameDay(day, new Date())) canvas.appendChild(nowLine());

  grid.append(hours, canvas);
  root.appendChild(grid);

  if (cal.dayScroll && cal.dayScroll.key === dayKey(day)) {
    grid.scrollTop = cal.dayScroll.top;   // you scrolled here; stay here
  } else {
    // Open on the part of the day you are actually in — an empty 3am is
    // nobody's idea of a useful first impression.
    const anchor = sameDay(day, new Date())
      ? minutesIntoDay(new Date(), day)
      : placed[0].startMin;
    grid.scrollTop = Math.max(0, (anchor - 45) * CAL_PX_PER_MIN);
  }
}

function dayScheduleSummary(events, day) {
  const wrap = document.createElement("div");
  wrap.className = "cal-summary";
  const open = events.filter((e) => e.status !== "done");
  const total = dayLoad(open);
  const raw = dayLoad(open, "raw_length_min");
  const buffer = Math.max(0, total - raw);
  const cap = capacityMinutes();

  const bits = [
    `${events.length} task${events.length === 1 ? "" : "s"}`,
    `${fmtMinutes(total)} booked of ${fmtMinutes(cap)}`,
  ];
  if (buffer > 0) bits.push(`${fmtMinutes(buffer)} of that is buffer`);
  // Work that comes back is counted like any other commitment — that is the
  // whole point of drawing it — but it is worth saying how much of the day is
  // already spoken for by a rhythm rather than by anything you chose today.
  const repeating = dayLoad(open.filter((e) => e.projected));
  if (repeating > 0) bits.push(`${fmtMinutes(repeating)} of it repeating work`);
  const done = events.length - open.length;
  if (done) bits.push(`${done} done`);

  const line = document.createElement("span");
  line.textContent = bits.join(" · ");
  wrap.appendChild(line);

  line.title = capacityNote();

  // Over-committing a day is the thing that makes a plan collapse, so say it
  // out loud rather than leaving you to add the blocks up yourself. The number
  // it measures against is the day you actually have, not a round eight hours
  // nobody has ever hit — see capacityNote().
  if (total > cap) {
    const warn = document.createElement("span");
    warn.className = "cal-warn";
    warn.textContent = cal.capacity?.learned
      ? `⚠ ${fmtMinutes(total - cap)} over — and ${fmtMinutes(cap)} is what ` +
        `your days actually hold, not the ${fmtMinutes(cal.capacity.base)} you set`
      : `⚠ that is ${fmtMinutes(total - cap)} more than ${fmtMinutes(cap)} ` +
        `of work in one day`;
    warn.title = capacityNote();
    wrap.appendChild(warn);
  }
  return wrap;
}

/* Google-Calendar-style side-by-side packing: everything that overlaps in
 * time shares the width of the column instead of hiding behind it. */
function layoutColumns(items) {
  const sorted = [...items].sort(
    (a, b) => a.startMin - b.startMin || b.endMin - a.endMin);
  const out = [];
  let cluster = [], clusterEnd = -Infinity;

  const flush = () => {
    if (!cluster.length) return;
    const columns = [];       // end time of the last item in each column
    for (const item of cluster) {
      let col = columns.findIndex((end) => end <= item.startMin);
      if (col < 0) { col = columns.length; columns.push(0); }
      columns[col] = item.endMin;
      item.col = col;
    }
    for (const item of cluster) item.cols = columns.length;
    out.push(...cluster);
    cluster = [];
    clusterEnd = -Infinity;
  };

  for (const item of sorted) {
    if (item.startMin >= clusterEnd) flush();
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.endMin);
  }
  flush();
  return out;
}

function dayBlock(item, day) {
  const { e, startMin, endMin } = item;
  const el = document.createElement("div");
  el.className = "cal-block quad-" + (e.quadrant || "none") +
    (e.projected ? " projected" : "") +
    (e.status === "done" ? " done" : "") + (isOverdue(e) ? " overdue" : "");
  const height = Math.max(CAL_MIN_BLOCK_PX, (endMin - startMin) * CAL_PX_PER_MIN);
  el.style.top = startMin * CAL_PX_PER_MIN + "px";
  el.style.height = height + "px";
  el.style.left = `calc(${(item.col / item.cols) * 100}% + 2px)`;
  el.style.width = `calc(${(1 / item.cols) * 100}% - 6px)`;
  if (height < 34) el.classList.add("tiny");

  // The buffer, drawn where it actually sits: the last stretch before the
  // deadline, protecting it. Seeing that a "45m" task really occupies an
  // hour is the entire point of the time tax.
  const bufferMin = Math.max(0, e.length_min - e.raw_length_min);
  if (bufferMin > 0) {
    const buf = document.createElement("div");
    buf.className = "cal-block-buffer";
    buf.style.height = (bufferMin / e.length_min) * 100 + "%";
    buf.title = `${fmtMinutes(bufferMin)} time-tax buffer before the deadline`;
    el.appendChild(buf);
  }

  const open = document.createElement("button");
  open.className = "cal-block-open";
  open.title = chipTooltip(e);
  const time = document.createElement("span");
  time.className = "cal-block-time";
  time.textContent =
    `${fmtClock(eventStart(e))} – ${fmtClock(eventEnd(e))}`;
  const title = document.createElement("span");
  title.className = "cal-block-title";
  title.textContent = (e.projected ? "🔁 " : "") + e.title;
  const meta = document.createElement("span");
  meta.className = "cal-block-meta";
  meta.textContent = [
    `${fmtMinutes(e.length_min)}`,
    e.projected ? "repeats — not on your list yet" : null,
    bufferMin ? `incl. ${fmtMinutes(bufferMin)} buffer` : null,
    e.project_name && !cal.filters.project ? e.project_name : null,
  ].filter(Boolean).join(" · ");
  open.append(time, title, meta);
  open.addEventListener("click", () => openEvent(e));
  el.appendChild(open);

  const score = document.createElement("span");
  score.className = "cal-score cal-block-score";
  score.textContent = Math.round(e.score ?? 0);
  score.title = `Score ${Math.round(e.score ?? 0)}/100`;
  el.appendChild(score);

  if (isOverdue(e)) {
    const btn = document.createElement("button");
    btn.className = "cal-chip-nudge cal-block-nudge";
    btn.textContent = "⏩";
    btn.title = "Nudge this to a new deadline, same length";
    btn.setAttribute("aria-label", "Nudge " + e.title);
    btn.addEventListener("click", (ev) => { ev.stopPropagation(); openNudge([e]); });
    el.appendChild(btn);
  }
  return el;
}

/* Slack between commitments, named. A day that looks full is usually a day
 * with three hours of gaps in it, and seeing them is what makes the next
 * thing feel possible. */
function freeBands(placed) {
  if (placed.length < 2) return [];
  const merged = [];
  for (const item of [...placed].sort((a, b) => a.startMin - b.startMin)) {
    const last = merged[merged.length - 1];
    if (last && item.startMin <= last.end) last.end = Math.max(last.end, item.endMin);
    else merged.push({ start: item.startMin, end: item.endMin });
  }
  const bands = [];
  for (let i = 1; i < merged.length; i++) {
    const start = merged[i - 1].end, end = merged[i].start;
    if (end - start >= CAL_GAP_MIN) bands.push({ start, end });
  }
  return bands;
}

function gapBand(band) {
  const el = document.createElement("div");
  el.className = "cal-gap";
  el.style.top = band.start * CAL_PX_PER_MIN + "px";
  el.style.height = (band.end - band.start) * CAL_PX_PER_MIN + "px";
  const label = document.createElement("span");
  label.textContent = `${fmtMinutes(band.end - band.start)} free`;
  el.appendChild(label);
  return el;
}

function nowLine() {
  const el = document.createElement("div");
  el.className = "cal-now";
  el.style.top = minutesIntoDay(new Date(), new Date()) * CAL_PX_PER_MIN + "px";
  el.title = "now";
  return el;
}

/* ---- week view ----
 * Seven day-by-day lists rather than a second time grid: across a week the
 * question is "what is coming", and the answer is more readable as a ranked
 * list than as blocks two pixels tall. Ranked by score, so the top of each
 * day is what that day is actually about. */

function renderWeekView(root, events) {
  const start = startOfWeek(cal.cursor);
  const byDay = groupByDay(events);
  const grid = document.createElement("div");
  grid.className = "cal-week-grid";

  for (let i = 0; i < 7; i++) {
    const day = addDays(start, i);
    const list = (byDay.get(dayKey(day)) || []).sort(byScore);
    grid.appendChild(dayColumn(day, list));
  }
  root.appendChild(grid);
}

function dayColumn(day, list) {
  const col = document.createElement("div");
  col.className = "cal-col" + (sameDay(day, new Date()) ? " today" : "");

  const head = document.createElement("button");
  head.className = "cal-col-head";
  head.title = "Open this day";
  const name = document.createElement("span");
  name.className = "cal-col-dow";
  name.textContent = day.toLocaleDateString(undefined, { weekday: "short" });
  const num = document.createElement("b");
  num.textContent = day.getDate();
  head.append(name, num);
  head.addEventListener("click", () => openCalendarDay(day));
  col.appendChild(head);

  const body = document.createElement("div");
  body.className = "cal-col-body";
  for (const e of list) {
    body.appendChild(eventChip(e, { showTime: true, stacked: true, nudge: true }));
  }
  if (!list.length) {
    const none = document.createElement("span");
    none.className = "cal-col-empty muted";
    none.textContent = "—";
    body.appendChild(none);
  }
  col.appendChild(body);

  const open = list.filter((e) => e.status !== "done");
  if (open.length) {
    const total = dayLoad(open);
    const cap = capacityMinutes();
    const over = total > cap;
    const foot = document.createElement("div");
    foot.className = "cal-col-foot" + (over ? " over" : " muted");
    foot.textContent = (over ? "⚠ " : "") + fmtMinutes(total);
    foot.title = over
      ? `${fmtMinutes(total)} of buffered work — ${fmtMinutes(total - cap)} ` +
        `more than this day holds. ${capacityNote()}`
      : `${fmtMinutes(total)} of buffered work, of ${fmtMinutes(cap)}. ` +
        capacityNote();
    col.appendChild(foot);
  }
  return col;
}

/* ---- month view ---- */

function renderMonthView(root, events) {
  const first = startOfMonth(cal.cursor);
  const start = startOfWeek(first);
  const byDay = groupByDay(events);
  const month = first.getMonth();

  const dow = document.createElement("div");
  dow.className = "cal-dow";
  for (let i = 0; i < 7; i++) {
    const cell = document.createElement("span");
    cell.textContent = addDays(start, i)
      .toLocaleDateString(undefined, { weekday: "short" });
    dow.appendChild(cell);
  }

  const grid = document.createElement("div");
  grid.className = "cal-month-grid";
  // Six rows always: a month grid that changes height as you page through it
  // makes everything below it jump around.
  for (let i = 0; i < 42; i++) {
    const day = addDays(start, i);
    const list = (byDay.get(dayKey(day)) || []).sort(byScore);
    grid.appendChild(monthCell(day, list, month));
  }
  root.append(dow, grid);
}

function monthCell(day, list, month) {
  const cell = document.createElement("div");
  cell.className = "cal-cell" +
    (day.getMonth() === month ? "" : " outside") +
    (sameDay(day, new Date()) ? " today" : "");

  const head = document.createElement("button");
  head.className = "cal-cell-date";
  head.textContent = day.getDate();
  head.title = "Open this day";
  head.addEventListener("click", () => openCalendarDay(day));
  cell.appendChild(head);

  for (const e of list.slice(0, CAL_MONTH_CHIPS)) {
    cell.appendChild(eventChip(e, { showTime: true, stacked: true }));
  }
  if (list.length > CAL_MONTH_CHIPS) {
    const more = document.createElement("button");
    more.className = "cal-more";
    more.textContent = `+${list.length - CAL_MONTH_CHIPS} more`;
    more.addEventListener("click", () => openCalendarDay(day));
    cell.appendChild(more);
  }
  return cell;
}

/* ---------------- nudging ----------------
 * "Same length" is the whole promise: the estimate never changes, so the block
 * is exactly as big on its new day as it was on the old one. For a task with
 * subtasks the promise is bigger — everything nested under it that has its own
 * deadline slides by the same amount, so a plan spread over three days stays
 * spread over three days instead of collapsing onto the new date. That part
 * happens on the server (logic.nudge_plan); this side just picks the moment. */

let nudgeTargets = [];

const NUDGE_PRESETS = [
  {
    label: "In 1 hour",
    hint: "an hour from now",
    at: () => new Date(Date.now() + 60 * 60000),
  },
  {
    label: "Tonight, 6pm",
    hint: "6pm today, or tomorrow if that has gone",
    at: () => {
      const d = startOfDay(new Date());
      d.setHours(18);
      return d > new Date() ? d : (d.setDate(d.getDate() + 1), d);
    },
  },
  {
    // The one that usually wants picking: same slot in the day, one day later.
    label: "Tomorrow, same time",
    hint: "tomorrow, at the time it was already set for",
    at: (e) => atSameClockTime(e, addDays(startOfDay(new Date()), 1)),
  },
  {
    label: "In 3 days",
    hint: "three days out, same time of day",
    at: (e) => atSameClockTime(e, addDays(startOfDay(new Date()), 3)),
  },
  {
    label: "Next week",
    hint: "a week out, same time of day",
    at: (e) => atSameClockTime(e, addDays(startOfDay(new Date()), 7)),
  },
];

function atSameClockTime(e, day) {
  const old = new Date(e.deadline);
  const d = startOfDay(day);
  d.setHours(old.getHours(), old.getMinutes(), 0, 0);
  return d;
}

function openNudge(events) {
  nudgeTargets = events.filter(Boolean);
  if (!nudgeTargets.length) return;
  const one = nudgeTargets.length === 1 ? nudgeTargets[0] : null;

  $("n-summary").textContent = one
    ? `“${one.title}” was due ${new Date(one.deadline).toLocaleString()}.`
    : `${nudgeTargets.length} past-due tasks.`;
  const total = nudgeTargets.reduce((n, e) => n + e.length_min, 0);
  $("n-length").textContent = one
    ? `It keeps its length: ${fmtMinutes(one.length_min)} of buffered work ` +
      `ending at the new deadline.` +
      (one.has_subtasks
        ? ` Its subtasks slide by the same amount, so the plan keeps its shape.`
        : "")
    : `Each keeps its own length — ${fmtMinutes(total)} of work in total — and ` +
      `anything nested under them slides by the same amount.`;

  const presets = $("n-presets");
  presets.replaceChildren();
  for (const preset of NUDGE_PRESETS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost";
    btn.textContent = preset.label;
    btn.title = nudgeTargets.length > 1
      ? `Move all ${nudgeTargets.length} to ${preset.hint}`
      : `Move to ${preset.hint}`;
    btn.addEventListener("click", () => applyNudge(
      nudgeTargets.map((e) => ({ task_id: e.id, deadline: preset.at(e).toISOString() }))));
    presets.appendChild(btn);
  }

  // The custom picker defaults to the first preset that everything can share.
  $("n-when").value = isoToLocalInput(
    (one ? atSameClockTime(one, addDays(startOfDay(new Date()), 1))
         : new Date(Date.now() + 24 * 60 * 60000)).toISOString());
  $("modal-nudge").showModal();
}

async function applyNudge(nudges) {
  $("modal-nudge").close();
  try {
    applyState(await api("/nudge", {
      method: "POST", body: JSON.stringify({ nudges }),
    }));
    const when = new Date(nudges[0].deadline);
    toast(nudges.length === 1
      ? `Nudged to ${when.toLocaleString()} — same length.`
      : `Nudged ${nudges.length} tasks forward, each keeping its length.`);
  } catch (e) { toast(e.message, true); }
}

/* ---------------- wiring ---------------- */

function wireCalendar() {
  restoreCalendarPrefs();
  $("btn-view").addEventListener("click", toggleCalendarMode);
  $("cal-prev").addEventListener("click", () => calendarStep(-1));
  $("cal-next").addEventListener("click", () => calendarStep(1));
  $("cal-today").addEventListener("click", calendarToday);
  for (const btn of document.querySelectorAll(".cal-view-btn")) {
    btn.addEventListener("click", () => setCalendarView(btn.dataset.view));
  }
  const filter = (id, key, prop = "value") => {
    $(id).addEventListener("change", () => {
      cal.filters[key] = $(id)[prop];
      saveCalendarPrefs();
      renderCalendar();
    });
  };
  filter("cal-project", "project");
  filter("cal-category", "category");
  filter("cal-auto", "auto", "checked");
  filter("cal-repeats", "repeats", "checked");
  filter("cal-done", "done", "checked");

  $("n-save").addEventListener("click", () => {
    const when = $("n-when").value;
    if (!when) { toast("Pick a date and time first."); return; }
    const iso = new Date(when).toISOString();
    applyNudge(nudgeTargets.map((e) => ({ task_id: e.id, deadline: iso })));
  });

  // Calendar keys, borrowed from Google Calendar's: arrows page, T is today,
  // D/W/M switch view. Only while the calendar is the thing on screen, and
  // never while a dialog or a text field has the keystroke.
  document.addEventListener("keydown", (e) => {
    if (!cal.mode || e.altKey || e.ctrlKey || e.metaKey) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.target?.closest?.("input, textarea, select")) return;
    const key = e.key.toLowerCase();
    if (e.key === "ArrowLeft") calendarStep(-1);
    else if (e.key === "ArrowRight") calendarStep(1);
    else if (key === "t") calendarToday();
    else if (key === "d") setCalendarView("day");
    else if (key === "w") setCalendarView("week");
    else if (key === "m") setCalendarView("month");
    else return;
    e.preventDefault();
  });

  // The now-line and "past due" styling go stale on their own; a minute is
  // fine-grained enough for a calendar and cheap enough to be free.
  setInterval(() => { if (cal.mode) renderCalendar(); }, 60000);
}
