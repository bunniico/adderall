/* The Habits tab.
 *
 * Every other tab is about work that ends. This one is about work that
 * doesn't: the routines you keep, in four parts of a life — life, health,
 * exercise, mentality — and a year of squares per routine saying which days
 * you actually kept them.
 *
 * The squares are the point. A list of routines tells you what you meant to
 * do; a calendar of the days you did them is the only display that has ever
 * made a habit feel like something you are already in the middle of rather
 * than something you are behind on. Which is also why a gap is drawn quietly
 * and a run is drawn brightly: the graph is allowed to celebrate, never to
 * scold.
 *
 * Nothing here touches the planner. A routine has no estimate and no slot, so
 * it can be neither late nor in the way — see habits.py for the whole reason
 * this is its own thing rather than a repeating task.
 *
 * Like the calendar and the Overview, the payload is fetched only while the
 * tab is up, and every mutation answers with the whole of it: ticking one box
 * changes a streak, a rate and a square, and a reply carrying only the box
 * would leave this file to work the rest out and disagree with the server. */

"use strict";

const hab = { data: null, loaded: false, stale: true, loading: false,
              editing: null };

function onHabits() {
  return !cal.mode && !!state.habits;
}

async function loadHabits() {
  // One fetch at a time, like the calendar's and the Overview's: two in
  // flight can land out of order, and the older one wins.
  if (hab.loading) return;
  hab.loading = true;
  try {
    hab.data = await api("/habits");
    hab.loaded = true;
    hab.stale = false;
  } catch (e) {
    toast("Could not load your routines: " + e.message, true);
  } finally {
    hab.loading = false;
  }
}

/* Off screen, a change is only noted. `force` tells a mutation apart from a
 * return: coming back to the tab, squares that were current when you left
 * still are — nothing but this tab can fill one in. */
async function refreshHabits(force = false) {
  if (!onHabits()) { hab.stale = true; return; }
  if (force || !hab.loaded || hab.stale) await loadHabits();
  renderHabits();
}

/* ---------------- the rules, as the page reads them ----------------
 * A second copy of `habits.is_due`, deliberately: the server decides what a
 * streak is, but every square in a year of them would be a field on the wire
 * if the page could not answer "was this day owed" by itself. The three rule
 * shapes are small enough that the duplication is cheaper than the payload. */

const HAB_EMPTY = "—";

function habDue(rule, day) {
  if (rule?.type === "weekdays") return (rule.days || []).includes(day.getDay());
  return true;
}

/* A weekly target owes no particular day, so a day without a tick is simply a
 * day — not a miss. Drawing it as one would paint four failures a week onto
 * "run three times a week", which is the exact habit that rule exists for. */
function habDayState(habit, day, iso, ticked, today) {
  if (ticked.has(iso)) return "done";
  if (day > today) return "future";
  if (habit.rule?.type === "weekly") return "idle";
  return habDue(habit.rule, day) ? "missed" : "off";
}

function habEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function habDate(iso) {
  // Noon, so a day is never dragged across its own boundary by a DST shift.
  return new Date(iso + "T12:00:00");
}

function habIso(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function habLongDate(iso) {
  return habDate(iso).toLocaleDateString(undefined,
    { weekday: "long", month: "long", day: "numeric" });
}

/* ---------------- the heatmap ----------------
 * Weeks as columns, days as rows, a year of them — the GitHub contribution
 * graph, which is the shape everyone can already read. Drawn as a grid of
 * buttons rather than as SVG because every square is a control: a day you
 * forgot to tick is one you can still tick, and a square you filled in by
 * mistake that you cannot clear turns the whole calendar into something you
 * stop trusting. */

const HAB_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function heatmap(habit) {
  const wrap = habEl("div", "hm-wrap");
  const scroller = habEl("div", "hm-scroll");
  const start = habDate(hab.data.window.start);
  const end = habDate(hab.data.window.end);
  const today = habDate(hab.data.today);
  const ticked = new Set(habit.checkins || []);

  const months = habEl("div", "hm-months");
  const grid = habEl("div", "hm-grid");
  grid.setAttribute("role", "grid");
  grid.setAttribute("aria-label",
    `${habit.name} — the last year, one square a day`);

  let lastMonth = -1;
  for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const day = new Date(d);
    const iso = habIso(day);
    const state = habDayState(habit, day, iso, ticked, today);
    // The window starts on a week boundary, so which column a day sits in is
    // simply how many whole weeks it is past the start.
    const column = Math.floor(Math.round((day - start) / 86400000) / 7);
    // A month's label goes over the column its first day falls in.
    if (day.getMonth() !== lastMonth) {
      lastMonth = day.getMonth();
      const label = habEl("span", "hm-month", HAB_MONTHS[lastMonth]);
      label.style.gridColumn = String(column + 1);
      months.appendChild(label);
    }
    if (state === "future") {
      // Kept as a cell rather than dropped, so the last column keeps its
      // shape and today does not jump up the grid as the week fills in.
      grid.appendChild(habEl("span", "hm-cell hm-future"));
    } else {
      const cell = habEl("button", "hm-cell hm-" + state);
      cell.type = "button";
      cell.dataset.day = iso;
      const done = state === "done";
      cell.setAttribute("aria-pressed", String(done));
      cell.title = habLongDate(iso) + " — " +
        (done ? "done" : state === "missed" ? "missed" :
         state === "off" ? "not one of its days" : "not done");
      if (iso === hab.data.today) cell.classList.add("hm-today");
      cell.addEventListener("click", () => checkHabit(habit.id, iso, !done));
      grid.appendChild(cell);
    }
  }

  scroller.append(months, grid);
  wrap.appendChild(scroller);
  const key = habEl("div", "hm-key");
  key.append(habEl("span", "hm-key-label", "less"),
             habEl("span", "hm-cell hm-off"),
             habEl("span", "hm-cell hm-missed"),
             habEl("span", "hm-cell hm-done"),
             habEl("span", "hm-key-label", "more"));
  wrap.appendChild(key);
  // The year ends today, and today is the end everyone wants to see first.
  requestAnimationFrame(() => { scroller.scrollLeft = scroller.scrollWidth; });
  return wrap;
}

/* ---------------- one routine ---------------- */

function streakText(stats) {
  const n = stats.current;
  if (!n) return "no streak yet";
  return `${n} ${stats.unit}${n === 1 ? "" : "s"}`;
}

function habitCard(habit) {
  const s = habit.stats;
  const card = habEl("article", "hab-card cat-" + habit.category);
  card.dataset.id = habit.id;
  if (s.done_today) card.classList.add("is-done");

  const head = habEl("div", "hab-head-row");

  // The one control this tab exists for. Big, first, and it toggles: a tick
  // you did not mean is undone by pressing the same thing again.
  const tick = habEl("button", "hab-tick");
  tick.type = "button";
  tick.setAttribute("aria-pressed", String(s.done_today));
  tick.textContent = s.done_today ? "✓" : "";
  tick.title = s.done_today
    ? `Done today — press to undo`
    : s.due_today ? "Mark today done" : "Not one of its days — mark it anyway";
  tick.setAttribute("aria-label",
    `${habit.name} — ${s.done_today ? "done today" : "not done today"}`);
  if (!s.due_today) tick.classList.add("is-rest");
  tick.addEventListener("click", () =>
    checkHabit(habit.id, hab.data.today, !s.done_today));
  head.appendChild(tick);

  const title = habEl("button", "hab-title");
  title.type = "button";
  title.title = "Edit this routine";
  title.append(habEl("span", "hab-name", habit.name),
               habEl("span", "hab-rule", habit.rule_label +
                 (s.due_today ? "" : " · not today")));
  title.addEventListener("click", () => openHabitEditor(habit));
  head.appendChild(title);

  const stats = habEl("div", "hab-stats");
  const stat = (value, label, cls) => {
    const box = habEl("div", "hab-stat" + (cls ? " " + cls : ""));
    box.append(habEl("b", null, value), habEl("span", null, label));
    stats.appendChild(box);
  };
  stat((s.current ? "🔥 " : "") + streakText(s), "current streak",
       s.current ? "is-hot" : "");
  stat(s.longest ? `${s.longest} ${s.unit}${s.longest === 1 ? "" : "s"}`
                 : HAB_EMPTY, "best run");
  // The number that keeps a long streak honest: a 40-day streak on a
  // three-days-a-week rule is seventeen runs, and this says so.
  stat(s.total ? Math.round(s.rate * 100) + "%" : HAB_EMPTY,
       `last ${s.rate_days} days`);
  if (s.target) {
    stat(`${s.this_week}/${s.target}`, "this week",
         s.this_week >= s.target ? "is-hot" : "");
  }
  head.appendChild(stats);
  card.appendChild(head);
  card.appendChild(heatmap(habit));
  return card;
}

/* ---------------- the whole pane ---------------- */

function renderHabits() {
  if (!hab.data) return;
  fillHabitCategories();
  const body = $("hab-body");
  body.replaceChildren();
  const rows = hab.data.habits || [];
  $("hab-empty").hidden = rows.length > 0;
  $("hab-legend").hidden = rows.length === 0;

  const due = hab.data.today_due;
  const done = hab.data.today_done;
  const line = $("hab-today");
  if (!rows.length) {
    line.textContent = "";
  } else if (!due) {
    line.textContent = "Nothing is owed today. Tick one anyway if you did it.";
  } else if (hab.data.today_clear) {
    line.textContent = `All ${due} done today. That's the whole list.`;
  } else {
    line.textContent = `${done} of ${due} done today — ` +
      `${due - done} still open.`;
  }
  line.classList.toggle("is-clear", !!hab.data.today_clear);

  for (const category of hab.data.categories) {
    const mine = rows.filter((h) => h.category === category.id);
    if (!mine.length) continue;
    const section = habEl("section", "hab-group cat-" + category.id);
    const head = habEl("h3", "hab-group-head");
    head.append(habEl("span", "hab-group-emoji", category.emoji),
                habEl("span", "hab-group-name", category.label),
                habEl("span", "hab-group-note", category.note));
    section.appendChild(head);
    for (const habit of mine) section.appendChild(habitCard(habit));
    body.appendChild(section);
  }
  Motion.enterNew(body, habDrawn, ".hab-card");
}

const habDrawn = new Set();

/* ---------------- mutations ----------------
 * Every one of them answers with the whole payload, so there is one place
 * that decides what a streak is and it is not this file. */

async function habitCall(path, options) {
  try {
    hab.data = await api(path, options);
    hab.loaded = true;
    hab.stale = false;
    renderHabits();
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

async function checkHabit(habitId, day, done) {
  const ok = await habitCall(`/habits/${habitId}/check`, {
    method: "POST", body: JSON.stringify({ day, done }),
  });
  if (ok && done) Motion.play("complete");
}

/* ---------------- the editor ---------------- */

function habitRuleFromForm() {
  const type = $("h-freq").value;
  if (type === "weekdays") {
    return { type, days: [...$("h-weekdays").querySelectorAll(".weekday-chip.on")]
      .map((c) => Number(c.dataset.day)) };
  }
  if (type === "weekly") return { type, times: Number($("h-times").value) };
  return { type: "daily" };
}

function syncHabitForm() {
  const type = $("h-freq").value;
  $("h-weekdays").hidden = type !== "weekdays";
  $("h-times-row").hidden = type !== "weekly";
  const note = {
    daily: "Every day counts, and a day missed ends the streak.",
    weekdays: "Only the days you pick count. The others are days off, not " +
              "misses — nothing to keep up on a Sunday you never claimed.",
    weekly: "Any days you like, as long as you get there by the end of the " +
            "week. This one's streak is counted in weeks.",
  }[type];
  $("h-freq-note").textContent = note || "";
}

function openHabitEditor(habit) {
  fillHabitCategories();
  hab.editing = habit ? habit.id : null;
  $("h-heading").textContent = habit ? "Edit routine" : "New routine";
  $("h-name").value = habit ? habit.name : "";
  $("h-delete").hidden = !habit;

  const rule = habit ? habit.rule : { type: "daily" };
  $("h-freq").value = rule.type || "daily";
  $("h-times").value = rule.times || 3;
  const days = rule.days || [1, 2, 3, 4, 5];
  for (const chip of $("h-weekdays").querySelectorAll(".weekday-chip")) {
    const on = days.includes(Number(chip.dataset.day));
    chip.classList.toggle("on", on);
    chip.setAttribute("aria-pressed", String(on));
  }
  setHabitCategory(habit ? habit.category : hab.data?.categories?.[0]?.id);
  syncHabitForm();
  $("modal-habit").showModal();
  $("h-name").focus();
}

function setHabitCategory(id) {
  for (const chip of $("h-categories").querySelectorAll(".cat-chip")) {
    const on = chip.dataset.category === id;
    chip.classList.toggle("on", on);
    chip.setAttribute("aria-pressed", String(on));
  }
}

function habitCategory() {
  return $("h-categories").querySelector(".cat-chip.on")?.dataset.category
    || hab.data?.categories?.[0]?.id;
}

async function saveHabit() {
  const name = $("h-name").value.trim();
  if (!name) { toast("Give it a name first.", true); $("h-name").focus(); return; }
  const body = JSON.stringify({ name, category: habitCategory(),
                                rule: habitRuleFromForm() });
  const ok = hab.editing
    ? await habitCall(`/habits/${hab.editing}`, { method: "PATCH", body })
    : await habitCall("/habits", { method: "POST", body });
  if (ok) Motion.closeDialog($("modal-habit"));
}

async function deleteHabit() {
  const habit = (hab.data?.habits || []).find((h) => h.id === hab.editing);
  if (!habit) return;
  // Every tick goes with it, which is the part worth being sure about.
  const kept = habit.stats.total;
  if (!confirm(`Delete “${habit.name}”?` +
      (kept ? ` The ${kept} day${kept === 1 ? "" : "s"} you have ticked go too.`
            : ""))) return;
  if (await habitCall(`/habits/${hab.editing}`, { method: "DELETE" }))
    Motion.closeDialog($("modal-habit"));
}

function wireHabits() {
  const chips = $("h-weekdays");
  WEEKDAY_LABELS.forEach((name, i) => {
    const chip = habEl("button", "weekday-chip", name);
    chip.type = "button";
    chip.dataset.day = String(i);
    chip.setAttribute("aria-pressed", "false");
    chip.addEventListener("click", () => {
      chip.classList.toggle("on");
      chip.setAttribute("aria-pressed",
        String(chip.classList.contains("on")));
    });
    chips.appendChild(chip);
  });

  $("h-freq").addEventListener("change", syncHabitForm);
  $("h-save").addEventListener("click", saveHabit);
  $("h-delete").addEventListener("click", deleteHabit);
  $("hab-add").addEventListener("click", () => openHabitEditor(null));
  $("hab-empty-add").addEventListener("click", () => openHabitEditor(null));
}

/* The category chips are drawn from the server's own list, so the four parts
 * of a life are named in exactly one place. */
function fillHabitCategories() {
  const host = $("h-categories");
  if (host.childElementCount || !hab.data) return;
  for (const category of hab.data.categories) {
    const chip = habEl("button", "cat-chip cat-" + category.id);
    chip.type = "button";
    chip.dataset.category = category.id;
    chip.title = category.note;
    chip.setAttribute("aria-pressed", "false");
    chip.append(habEl("span", "cat-chip-emoji", category.emoji),
                habEl("span", null, category.label));
    chip.addEventListener("click", () => setHabitCategory(category.id));
    host.appendChild(chip);
  }
}
