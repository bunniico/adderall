/* The Overview tab.
 *
 * Every other tab answers "what is on this list". This one answers the
 * questions a list cannot be asked: how much is there altogether, what should
 * I do next whichever tab it lives in, what is about to land on me, and am I
 * actually getting anywhere. It spans every project, like the calendar, but
 * it stays inside the tab strip, because it is a way of reading your lists
 * rather than a different thing to look at.
 *
 * Nothing here is computed on the page. `/api/overview` derives it through
 * the same shared day book the list and the calendar are derived through, so
 * "4h 30m booked today" is the same four and a half hours the calendar draws
 * rather than a second opinion about them.
 *
 * The charts are hand-rolled SVG: the app ships no JavaScript dependencies
 * and no build step, and a dashboard that needs a CDN is a dashboard that is
 * blank on the train. */

"use strict";

const ov = { data: null, loaded: false, stale: true, loading: false };

function onOverview() {
  return !cal.mode && !!state.overview;
}

/* Four panes, one of them on screen. The calendar sits over the whole strip
 * — it has its own project filter, and two project pickers that disagree with
 * each other is worse than one — while the Overview and Habits are tabs like
 * any other, so the strip stays up and the list is one click away. */
function syncPanes() {
  const overview = onOverview();
  const habits = onHabits();
  $("calendar-view").hidden = !cal.mode;
  $("overview-view").hidden = !overview;
  $("habits-view").hidden = !habits;
  $("list-view").hidden = cal.mode || overview || habits;
}

async function loadOverview() {
  // One fetch at a time, like the calendar's: two in flight can land out of
  // order, and the older one wins.
  if (ov.loading) return;
  ov.loading = true;
  try {
    ov.data = await api("/overview");
    ov.loaded = true;
    ov.stale = false;
  } catch (e) {
    toast("Could not load the overview: " + e.message, true);
  } finally {
    ov.loading = false;
  }
}

/* Anything that changes a task changes these numbers. Off screen that is only
 * noted: the task list has no business paying for an overview fetch on every
 * keystroke's worth of work.
 *
 * `force` is what tells a mutation apart from a return. Coming back from the
 * calendar, numbers that were current when you left still are; finishing a
 * task is exactly the moment they stopped being. */
async function refreshOverview(force = true) {
  if (!onOverview()) { ov.stale = true; return; }
  if (force || !ov.loaded || ov.stale) await loadOverview();
  renderOverview();
}

/* The Overview spans every project, so a task listed on it is usually not in
 * the list view's state at all. Its row carries enough of the task for the
 * detail modal to work — the same arrangement the calendar runs on. */
function overviewTask(id) {
  if (!ov.data) return null;
  const row = [...ov.data.top, ...ov.data.upcoming].find((t) => t.id === id);
  return row ? { ...row, subtasks: [] } : null;
}

/* ---------------- drawing helpers ----------------
 * All three scripts share one global scope, so everything here carries the
 * `ov` prefix rather than claiming a name as ordinary as `el`. */

const OV_SVG_NS = "http://www.w3.org/2000/svg";

function ovSvg(tag, attrs = {}) {
  const node = document.createElementNS(OV_SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/* A native tooltip on a mark. `<title>` rather than a floating div of our
 * own: it is what screen readers read, it survives every browser, and the
 * rest of this app already says what things are in exactly this way. */
function ovTip(node, text) {
  const title = ovSvg("title");
  title.textContent = text;
  node.appendChild(title);
  return node;
}

function ovEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function ovEmpty(host, text) {
  host.replaceChildren(ovEl("p", "ov-empty", text));
}

/* ---------------- stat tiles ----------------
 * A handful of headline numbers, and every one of them is a number rather
 * than a chart: a single current value is a tile, never a one-bar bar chart. */

function renderStats() {
  const host = $("ov-stats");
  host.replaceChildren();
  const s = ov.data.stats;
  const cap = ov.data.capacity;
  const xp = ov.data.xp;

  const tiles = [
    { label: "Open tasks", value: s.open,
      note: s.projects > 1 ? `across ${s.projects} lists` : "" },
    // The one number here that is allowed to be a warning — and even then it
    // states the fact rather than shouting it. A date that has gone by is not
    // news to you.
    { label: "Overdue", value: s.overdue, tone: s.overdue ? "warn" : "",
      note: s.overdue ? "already past" : "nothing late" },
    { label: `Due in ${s.soon_days} days`, value: s.due_soon,
      note: "not counting what is already late" },
    { label: "Work left", value: fmtMinutes(s.minutes_left),
      note: "buffered, every list" },
    { label: "Booked today", value: fmtMinutes(s.booked_today),
      note: `of a ${fmtMinutes(cap.minutes)} day`,
      meter: cap.minutes ? s.booked_today / cap.minutes : 0,
      // A day that is already over its cap is the one thing on this row worth
      // catching your eye, because it is the one you can still do something
      // about.
      tone: cap.minutes && s.booked_today > cap.minutes ? "warn" : "" },
    { label: `Finished in ${s.trend_days} days`, value: s.finished,
      note: s.finished_minutes ? fmtMinutes(s.finished_minutes) + " of work" : "" },
  ];
  if (xp && settings?.gamification) {
    tiles.push({ label: "Level", value: xp.level,
                 note: `${xp.into_level} / ${xp.level_span} XP`,
                 meter: xp.progress });
    // The pace the level is moving at, which the level itself cannot say:
    // a bar four-fifths of the way along looks the same whether it took a
    // week or a year. Quiet days are in the divisor, so this is what the
    // month paid out per day rather than what a good day pays.
    // A zero is a real answer — a month with nothing in it earned nothing a
    // day — so the em dash is kept for the other case, nothing to divide,
    // exactly as the rail reads it. Rounding 0 to "0" and no-answer to "0"
    // would collapse the two.
    tiles.push({ label: "XP a day",
                 value: xp.daily != null ? Math.round(xp.daily) : "—",
                 note: `over the last ${xp.daily_days} days` });
  }

  for (const tile of tiles) {
    const card = ovEl("div", "ov-tile" + (tile.tone ? " tone-" + tile.tone : ""));
    card.appendChild(ovEl("span", "ov-tile-label", tile.label));
    card.appendChild(ovEl("b", "ov-tile-value", tile.value));
    if (tile.meter != null) {
      const track = ovEl("div", "ov-meter");
      const fill = ovEl("div", "ov-meter-fill");
      fill.style.width = Math.min(100, Math.max(0, tile.meter * 100)) + "%";
      track.appendChild(fill);
      card.appendChild(track);
    }
    if (tile.note) card.appendChild(ovEl("span", "ov-tile-note", tile.note));
    host.appendChild(card);
  }
}

/* ---------------- the trend ----------------
 * Two charts stacked, not one chart with two scales. Minutes and task counts
 * are different measures of different size, and pinning them to two y-axes on
 * one plot invents a correlation that is not in the data — so they share an x
 * axis and nothing else.
 *
 * Every day in the window is drawn, including the empty ones. A chart made
 * only of the days you finished something on quietly rewrites the week: four
 * bars in a row read as four days on the trot, and the three empty days
 * between them vanish. */

/* Charts are drawn at the width they will be shown at, so one unit is one
 * pixel. A viewBox scaled to fit takes the type with it: a 9px date label in a
 * 720-unit chart squeezed into a 340px phone card is a 4px date label, which
 * is no label at all. Re-measured on resize — see `wireOverview`. */
function ovChartWidth(host) {
  return Math.max(260, Math.round(host.clientWidth) || 640);
}

const TREND_MINUTES_H = 108;
const TREND_COUNT_H = 52;
const TREND_GAP = 26;         // room between the two plots for the date axis
const TREND_PAD_L = 6;
const TREND_PAD_R = 6;
// Headroom above the tallest column, for the one value this chart labels and
// for the day-cap line when the cap is the tallest thing on it. A label that
// does not fit does not get drawn on top of its own bar.
const TREND_HEAD = 15;
const OV_BAR_MAX = 18;           // marks stay thin however few days there are
const OV_BAR_GAP = 2;            // surface gap between touching columns

function renderTrend() {
  const host = $("ov-trend");
  const sub = $("ov-trend-sub");
  const days = ov.data.trend || [];
  const s = ov.data.stats;
  host.replaceChildren();

  if (!s.finished) {
    sub.textContent = `Nothing ticked off in the last ${s.trend_days} days.`;
    ovEmpty(host, "Finish something and it lands here — one bar per day, " +
                    "so the quiet days stay visible too.");
    return;
  }
  sub.textContent =
    `${s.finished} task${s.finished === 1 ? "" : "s"} and ` +
    `${fmtMinutes(s.finished_minutes)} of work in the last ${s.trend_days} days ` +
    `· ${fmtMinutes(Math.round(s.finished_minutes / s.trend_days))} a day on average.`;

  const width = ovChartWidth(host);
  const cap = ov.data.capacity?.minutes || 0;
  const maxMin = Math.max(cap, ...days.map((d) => d.minutes), 1);
  const maxCount = Math.max(...days.map((d) => d.finished), 1);
  const height = TREND_MINUTES_H + TREND_GAP + TREND_COUNT_H;
  const svg = ovSvg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    class: "ov-svg",
    role: "img",
    "aria-label":
      `Work finished per day over the last ${s.trend_days} days: ` +
      `${fmtMinutes(s.finished_minutes)} across ${s.finished} tasks.`,
  });

  const plotH = TREND_MINUTES_H - TREND_HEAD;
  const band = (width - TREND_PAD_L - TREND_PAD_R) / days.length;
  const barW = Math.max(2, Math.min(OV_BAR_MAX, band - OV_BAR_GAP));
  const x = (i) => TREND_PAD_L + i * band + (band - barW) / 2;

  // Baselines, hairline and recessive: the data is the only loud thing here.
  svg.appendChild(ovSvg("line", {
    class: "ov-axis", x1: TREND_PAD_L, x2: width - TREND_PAD_R,
    y1: TREND_MINUTES_H, y2: TREND_MINUTES_H,
  }));
  svg.appendChild(ovSvg("line", {
    class: "ov-axis", x1: TREND_PAD_L, x2: width - TREND_PAD_R,
    y1: height - 0.5, y2: height - 0.5,
  }));

  // The day cap, as a line to read the bars against. Without it "three hours"
  // is a number; with it, it is most of a day or a fifth of one.
  if (cap) {
    const y = TREND_MINUTES_H - (cap / maxMin) * plotH;
    svg.appendChild(ovTip(ovSvg("line", {
      class: "ov-rule", x1: TREND_PAD_L, x2: width - TREND_PAD_R, y1: y, y2: y,
    }), `Your day holds about ${fmtMinutes(cap)}`));
    const label = ovSvg("text", {
      class: "ov-rule-label", x: width - TREND_PAD_R, y: Math.max(9, y - 4),
      "text-anchor": "end",
    });
    label.textContent = fmtMinutes(cap) + " a day";
    svg.appendChild(label);
  }

  const busiest = days.reduce((best, d) => (d.minutes > best.minutes ? d : best),
                              days[0]);
  days.forEach((day, i) => {
    const when = new Date(day.day + "T12:00:00");
    const label = when.toLocaleDateString(undefined,
      { weekday: "short", month: "short", day: "numeric" });
    const tip = `${label} — ${day.finished} task${day.finished === 1 ? "" : "s"}` +
                (day.minutes ? ` · ${fmtMinutes(day.minutes)}` : "");
    if (day.minutes) {
      const h = Math.max(2, (day.minutes / maxMin) * plotH);
      svg.appendChild(ovTip(ovSvg("rect", {
        class: "ov-bar", x: x(i), y: TREND_MINUTES_H - h, width: barW, height: h,
        rx: Math.min(4, barW / 2),
      }), tip));
      // Square at the baseline: the rounded corners belong to the data end,
      // and a rect rounds both. A second rect over the foot of the bar puts
      // the flat edge back.
      svg.appendChild(ovSvg("rect", {
        class: "ov-bar", x: x(i), y: TREND_MINUTES_H - Math.min(h, 4),
        width: barW, height: Math.min(h, 4),
      }));
    }
    if (day.finished) {
      const top = TREND_MINUTES_H + TREND_GAP;
      const h = Math.max(2, (day.finished / maxCount) * TREND_COUNT_H);
      svg.appendChild(ovTip(ovSvg("rect", {
        class: "ov-bar ov-bar-soft", x: x(i), y: top + TREND_COUNT_H - h,
        width: barW, height: h, rx: Math.min(4, barW / 2),
      }), tip));
      svg.appendChild(ovSvg("rect", {
        class: "ov-bar ov-bar-soft", x: x(i),
        y: top + TREND_COUNT_H - Math.min(h, 4), width: barW,
        height: Math.min(h, 4),
      }));
    }
  });

  // Labelled sparingly and on purpose: the ends of the window, and the one
  // day the chart is actually about. A number on every bar is chaos, and it
  // goes unread.
  const axisY = TREND_MINUTES_H + 15;
  const dayLabel = (iso) => new Date(iso + "T12:00:00")
    .toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const tick = (i, anchor) => {
    const text = ovSvg("text", {
      class: "ov-tick", x: x(i) + barW / 2, y: axisY, "text-anchor": anchor,
    });
    text.textContent = dayLabel(days[i].day);
    svg.appendChild(text);
  };
  tick(0, "start");
  tick(days.length - 1, "end");
  if (busiest.minutes) {
    const i = days.indexOf(busiest);
    const y = TREND_MINUTES_H - (busiest.minutes / maxMin) * plotH - 5;
    const text = ovSvg("text", {
      class: "ov-value", x: x(i) + barW / 2, y: Math.max(9, y),
      "text-anchor": "middle",
    });
    text.textContent = fmtMinutes(busiest.minutes);
    svg.appendChild(text);
  }

  host.appendChild(svg);
  const key = ovEl("p", "ov-key");
  key.append(ovEl("span", "ov-key-item ov-key-bar", "minutes of work"),
             ovEl("span", "ov-key-item ov-key-soft", "tasks finished"));
  host.appendChild(key);
}

/* ---------------- the breakdowns ----------------
 * Two single-series bar charts. Every bar is labelled with its own name and
 * its own value, so nobody has to decode a colour to read either of them —
 * which is also why the list chart paints every bar the same: colouring
 * nominal categories by size spends the one free channel re-encoding what bar
 * length already shows. */

const OV_ROW_H = 38;
const OV_LABEL_Y = 12;
const OV_BAR_Y = 20;
const OV_BAR_H = 8;

function breakdownChart(rows, width, aria) {
  const svg = ovSvg("svg", {
    viewBox: `0 0 ${width} ${rows.length * OV_ROW_H}`,
    class: "ov-svg", role: "img", "aria-label": aria,
  });
  const max = Math.max(...rows.map((r) => r.value), 1);
  rows.forEach((row, i) => {
    const top = i * OV_ROW_H;
    const name = ovSvg("text", { class: "ov-row-label", x: 0, y: top + OV_LABEL_Y });
    name.textContent = row.name;
    svg.appendChild(name);
    const value = ovSvg("text", {
      class: "ov-row-value", x: width, y: top + OV_LABEL_Y, "text-anchor": "end",
    });
    value.textContent = row.text;
    svg.appendChild(value);
    // The track says what the bar is a fraction of, so an empty category is
    // still a row rather than a gap in the chart.
    svg.appendChild(ovSvg("rect", {
      class: "ov-track", x: 0, y: top + OV_BAR_Y, width, height: OV_BAR_H,
      rx: OV_BAR_H / 2,
    }));
    if (row.value > 0) {
      const w = Math.max(OV_BAR_H, (row.value / max) * width);
      const bar = ovSvg("rect", {
        class: "ov-bar", x: 0, y: top + OV_BAR_Y, width: w, height: OV_BAR_H,
        rx: OV_BAR_H / 2,
      });
      if (row.color) bar.style.fill = row.color;
      svg.appendChild(ovTip(bar, `${row.name} — ${row.text}`));
    }
  });
  return svg;
}

/* Both breakdowns stand down below two bars. One bar is not a comparison —
 * it is the "Work left" tile again, drawn wide — and a chart of it says
 * nothing the row above it has not already said. */
function renderByProject() {
  const host = $("ov-projects");
  const rows = (ov.data.by_project || []).filter((p) => p.open > 0);
  $("ov-projects-card").hidden = rows.length < 2;
  if (rows.length < 2) return;
  const width = ovChartWidth(host);
  host.replaceChildren(breakdownChart(
    rows.map((p) => ({
      name: p.name,
      value: p.minutes,
      text: p.minutes
        ? `${fmtMinutes(p.minutes)} · ${p.open} open`
        : `${p.open} open · no estimates yet`,
    })),
    width, "Work left per list, in buffered minutes"));
}

// The categories keep the colours they wear on every badge and every calendar
// chip: a quick win is the same cyan wherever you meet it. Each bar carries
// its own icon and its own name, so the colour is an echo of something
// already spelled out rather than the only way to tell the bars apart —
// which is the only reason a reserved four-colour set is allowed on a chart
// at all. (The trend above takes the other route: one hue at two steps, told
// apart by which band a bar is in.)
const QUAD_UNSET = "· not scored yet";

function renderByQuadrant() {
  const host = $("ov-quadrants");
  const rows = (ov.data.by_quadrant || []).filter((q) => q.tasks > 0);
  $("ov-quadrants-card").hidden = rows.length < 2;
  if (rows.length < 2) return;
  const style = getComputedStyle(document.documentElement);
  const width = ovChartWidth(host);
  host.replaceChildren(breakdownChart(
    rows.map((q) => ({
      name: q.quadrant === "none" ? QUAD_UNSET : QUAD_LABEL[q.quadrant],
      value: q.minutes,
      text: q.minutes
        ? `${fmtMinutes(q.minutes)} · ${q.tasks} task${q.tasks === 1 ? "" : "s"}`
        : `${q.tasks} task${q.tasks === 1 ? "" : "s"} · no estimates yet`,
      color: q.quadrant === "none"
        ? null : style.getPropertyValue("--" + q.quadrant).trim(),
    })),
    width, "Work left per impact/effort category, in buffered minutes"));
}

/* ---------------- the two shortlists ---------------- */

function overviewRow(task, kind) {
  const row = ovEl("button", "ov-row");
  row.type = "button";
  const head = ovEl("div", "ov-row-head");
  head.appendChild(ovEl("span", "ov-row-title", task.title));
  head.appendChild(ovEl("span", "ov-row-project", task.project_name));
  row.appendChild(head);

  const meta = ovEl("div", "ov-row-meta");
  // Which tree a step came from. "Ask her sister" says nothing on its own.
  if (task.path?.length) meta.appendChild(ovEl("span", "ov-crumb", task.path.join(" › ")));
  // The same badges the task list wears, in the same colours and the same
  // order — this is the same task, so it should read like it.
  const chip = (text, cls = "") => meta.appendChild(ovEl("span", "badge " + cls, text));
  if (kind === "top" && task.score != null) chip("★ " + Math.round(task.score), "score-badge");
  if (task.quadrant) chip(QUAD_LABEL[task.quadrant], "quad-" + task.quadrant);
  const minutes = task.has_subtasks ? task.rollup_remaining : task.buffered_estimate;
  if (minutes) chip(fmtMinutes(minutes));
  const due = fmtDeadline(task.deadline);
  if (due) chip(due.label, due.cls);
  row.appendChild(meta);

  row.title = "Open this task";
  row.addEventListener("click", () => openDetail(task.id));
  return row;
}

function renderShortlist(hostId, rows, kind, empty) {
  const host = $(hostId);
  if (!rows.length) {
    ovEmpty(host, empty);
    return;
  }
  host.replaceChildren(...rows.map((task) => overviewRow(task, kind)));
}

/* ---------------- the whole pane ---------------- */

/* The charts are drawn to a measured width, so a window that changes width
 * has to redraw them. Coalesced to one redraw a frame: a drag across a desktop
 * fires resize dozens of times a second, and this redraws two SVGs. */
let overviewResizePending = false;

function wireOverview() {
  window.addEventListener("resize", () => {
    if (overviewResizePending || !onOverview() || !ov.data) return;
    overviewResizePending = true;
    requestAnimationFrame(() => {
      overviewResizePending = false;
      if (onOverview()) renderOverview();
    });
  });
}

function renderOverview() {
  if (!ov.data) return;
  renderStats();
  renderTrend();
  renderShortlist("ov-top", ov.data.top, "top",
                  "Nothing to do. Add a task on one of your lists.");
  renderShortlist("ov-upcoming", ov.data.upcoming, "upcoming",
                  "Nothing has a date on it yet.");
  renderByProject();
  renderByQuadrant();
}
