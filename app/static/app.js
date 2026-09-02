/* Adderall front end.
 * One page, no navigation: the task list is the workspace, everything else
 * is a modal or the Taskmaster overlay. All state lives on the server; every
 * mutation round-trips immediately so nothing is ever lost on tab close. */

"use strict";

const $ = (id) => document.getElementById(id);

let state = { tasks: [], next_task_id: null, projects: [], active_project_id: null,
              alarm_tasks: [], xp: null };
let settings = null;
let detailTaskId = null;
let renamingProject = null;   // project whose tab is currently an input box
let renameFocusPending = false;
const thanklessQueue = [];
let thanklessShowing = null;
let subtaskDraftFor = null;   // task whose inline "add a subtask" box is open
let subtaskDraftText = "";
let handleFocusFor = null;    // drag handle to re-focus after a keyboard move
let tabFocusFor = null;       // project tab to re-focus after a keyboard move
let toggleFocusFor = null;    // collapse toggle to re-focus after a fold/unfold

/* ---------------- API ---------------- */

async function api(path, options = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function applyState(newState) {
  const prevQuads = {};
  for (const t of flatten(state.tasks)) prevQuads[t.id] = t.quadrant;
  state = newState;
  for (const t of flatten(state.tasks)) {
    if (t.quadrant === "thankless" && !t.ack_thankless &&
        (t.status === "todo" || t.status === "in_progress") &&
        prevQuads[t.id] !== "thankless" &&
        !thanklessQueue.includes(t.id) && thanklessShowing !== t.id) {
      thanklessQueue.push(t.id);
    }
  }
  renderTabs();
  render();
  renderXp();
  renderThrottle();
  maybeShowThankless();
  scheduleTransitionAlarms();
  syncFocusWithState();
  // Every mutation lands here, and every mutation can move something on the
  // calendar — including tasks in tabs this state doesn't even carry.
  refreshCalendar();
}

function flatten(tasks) {
  const out = [];
  const walk = (list) => list.forEach((t) => { out.push(t); walk(t.subtasks || []); });
  walk(tasks || []);
  return out;
}

function findTask(id, list = state.tasks) {
  for (const t of list) {
    if (t.id === id) return t;
    const found = findTask(id, t.subtasks || []);
    if (found) return found;
  }
  return null;
}

/* The calendar spans every project, so a task opened from it is often not in
 * the list currently on screen. Its calendar event carries the same fields
 * the detail modal needs, so it stands in. */
function findAnyTask(id) {
  return findTask(id) || calendarTask(id);
}

/* ---------------- projects (tabs) ----------------
 * Each project is its own list of tasks, one open at a time. The server
 * remembers which tab you are on, so a reload — or the same app opened on
 * your phone — comes back to the project you were actually working in.
 * Everything else on the page (adding, braindumping, focusing, ordering)
 * acts on the open tab and nothing else. */

function renderTabs() {
  const strip = $("tab-strip");
  strip.replaceChildren();
  const projects = state.projects || [];
  for (const project of projects) {
    strip.appendChild(
      renamingProject === project.id ? renameTab(project) : projectTab(project));
  }
  // Only worth a strip once there is something to switch between; a single
  // project is just "the list", and a lone tab is noise.
  $("project-tabs").classList.toggle("solo", projects.length < 2);
  if (renameFocusPending) {
    renameFocusPending = false;
    const input = strip.querySelector(".tab-rename input");
    if (input) { input.focus(); input.select(); }
  }
  // A tab moved with the keyboard is redrawn somewhere else in the strip;
  // focus follows it, so the next Shift+arrow keeps moving the same tab.
  if (tabFocusFor) {
    const moved = strip.querySelector(
      `.tab[data-project-id="${cssEscape(tabFocusFor)}"] > .tab-btn`);
    tabFocusFor = null;
    if (moved) {
      moved.focus();
      moved.scrollIntoView({ block: "nearest", inline: "nearest" });
      return;
    }
  }
  // On a narrow screen the strip scrolls: the tab you are on has to be the
  // one you can see, however far along the row it sits.
  strip.querySelector(".tab.active")
    ?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function projectTab(project) {
  const active = project.id === state.active_project_id;
  // A lone tab has nothing to be reordered against, so it stays a plain tab.
  const reorderable = (state.projects || []).length > 1;
  const tab = document.createElement("div");
  tab.className = "tab" + (active ? " active" : "");
  tab.setAttribute("role", "presentation");  // the button inside is the tab
  tab.dataset.projectId = project.id;

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "tab-btn";
  btn.setAttribute("role", "tab");
  btn.setAttribute("aria-selected", String(active));
  btn.title = (active ? "Click to rename" : `Switch to ${project.name}`) +
    (reorderable ? " — drag to reorder, or Shift+← / Shift+→" : "");
  const name = document.createElement("span");
  name.className = "tab-name";
  name.textContent = project.name;
  btn.appendChild(name);
  if (project.open_tasks) {
    const count = document.createElement("span");
    count.className = "tab-count";
    count.textContent = project.open_tasks;
    count.title = `${project.open_tasks} unfinished task` +
      (project.open_tasks === 1 ? "" : "s");
    btn.appendChild(count);
  }
  btn.addEventListener("click", () => {
    // A drag that ended on this tab is not also a click on it.
    if (tabClickSuppressed) return;
    if (active) startRename(project.id);
    else switchProject(project.id);
  });
  btn.addEventListener("dblclick", () => startRename(project.id));
  btn.addEventListener("keydown", (e) => wireTabKeys(e, project));
  tab.appendChild(btn);

  if (active) {
    const del = document.createElement("button");
    del.type = "button";
    del.className = "tab-close";
    del.textContent = "✕";
    del.title = `Delete ${project.name} and everything in it`;
    del.setAttribute("aria-label", "Delete project " + project.name);
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteProject(project.id);
    });
    tab.appendChild(del);
  }
  if (reorderable) wireTabDrag(tab, project);
  return tab;
}

/* Renaming happens in place, in the tab itself — no modal for something this
 * small. Enter or clicking away keeps it, Escape puts the old name back. */
function renameTab(project) {
  const form = document.createElement("form");
  form.className = "tab tab-rename active";
  form.setAttribute("role", "presentation");
  form.autocomplete = "off";

  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 80;
  input.value = project.name;
  input.setAttribute("aria-label", "Project name");

  let settled = false;
  const commit = () => {
    if (settled) return;
    settled = true;
    const name = input.value.trim();
    renamingProject = null;
    if (name && name !== project.name) renameProject(project.id, name);
    else renderTabs();
  };
  const cancel = () => {
    if (settled) return;
    settled = true;
    renamingProject = null;
    renderTabs();
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); cancel(); }
  });
  input.addEventListener("blur", commit);
  form.addEventListener("submit", (e) => { e.preventDefault(); commit(); });
  form.appendChild(input);
  return form;
}

function startRename(projectId) {
  renamingProject = projectId;
  renameFocusPending = true;
  renderTabs();
}

async function switchProject(projectId) {
  // A different list is a different set of things; letting it cascade in is
  // what makes switching tabs feel like moving rather than like a repaint.
  drawn.clear();
  renamingProject = null;
  subtaskDraftFor = null;
  try {
    applyState(await api(`/projects/${projectId}/activate`, { method: "POST" }));
  } catch (e) { toast(e.message, true); }
}

async function addProject() {
  try {
    applyState(await api("/projects", {
      method: "POST", body: JSON.stringify({ name: "New project" }),
    }));
  } catch (e) { toast(e.message, true); return; }
  // Lands you on the new tab with its name selected: name it and start typing
  // tasks, rather than hunting for a rename control afterwards.
  startRename(state.active_project_id);
}

async function renameProject(projectId, name) {
  try {
    applyState(await api(`/projects/${projectId}`, {
      method: "PATCH", body: JSON.stringify({ name }),
    }));
  } catch (e) { toast(e.message, true); }
}

async function deleteProject(projectId) {
  const project = (state.projects || []).find((p) => p.id === projectId);
  if (!project) return;
  if ((state.projects || []).length < 2) {
    toast("This is your only project — rename it instead of deleting it.");
    return;
  }
  const n = project.open_tasks;
  const what = n ? ` and its ${n} unfinished task${n === 1 ? "" : "s"}` : "";
  if (!confirm(`Delete “${project.name}”${what}? Everything in it goes too, ` +
               `and this can't be undone.`)) return;
  try {
    applyState(await api(`/projects/${projectId}`, { method: "DELETE" }));
    toast(`Deleted “${project.name}”.`);
  } catch (e) { toast(e.message, true); }
}

function switchToProjectAt(index) {
  const project = (state.projects || [])[index];
  if (project && project.id !== state.active_project_id) switchProject(project.id);
}

/* ---------------- tab order ----------------
 * The strip is yours to arrange: drag a tab along it and drop it where you
 * want it, hold and slide on a phone, or Shift+← / Shift+→ it once it has
 * focus — because drag-and-drop is unusable for plenty of the people this
 * app is for. Plain ← / → walk the strip without moving anything.
 *
 * Like a task move, the server is told "before/after that tab" rather than
 * an index, so a drop means what it looked like on screen. Reordering never
 * switches tabs: rearranging the row is not the same as going somewhere. */

let dragProjectId = null;    // tab being dragged
let pendingTabDrop = null;   // move body for where it would land right now
let tabClickSuppressed = false;

async function moveProject(projectId, body) {
  try {
    applyState(await api(`/projects/${projectId}/move`, {
      method: "POST", body: JSON.stringify(body),
    }));
  } catch (e) { toast(e.message, true); }
}

function wireTabKeys(e, project) {
  const dir = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
  if (!dir || e.altKey || e.ctrlKey || e.metaKey) return;
  e.preventDefault();
  const projects = state.projects || [];
  const i = projects.findIndex((p) => p.id === project.id);
  if (i < 0) return;
  if (e.shiftKey) nudgeProject(project, dir);
  else focusTabAt(i + dir);
}

function nudgeProject(project, dir) {
  const projects = state.projects || [];
  const i = projects.findIndex((p) => p.id === project.id);
  const neighbour = projects[i + dir];
  if (!neighbour) return;  // already at that end of the strip
  tabFocusFor = project.id;
  moveProject(project.id,
    { target_id: neighbour.id, mode: dir < 0 ? "before" : "after" });
}

function focusTabAt(index) {
  const tabs = $("tab-strip").querySelectorAll(".tab-btn");
  const btn = tabs[Math.max(0, Math.min(index, tabs.length - 1))];
  if (btn) { btn.focus(); btn.scrollIntoView({ block: "nearest", inline: "nearest" }); }
}

/* Where a tab held at this point would land: next to whichever tab the
 * pointer is over — its own half of that tab decides which side — or at the
 * end of the row when the pointer is past the last tab. */
function tabDropAt(x, y) {
  const strip = $("tab-strip");
  const under = document.elementFromPoint(x, y);
  const target = under && under.closest(".tab");
  if (target && strip.contains(target) && target.dataset.projectId &&
      target.dataset.projectId !== dragProjectId) {
    const r = target.getBoundingClientRect();
    return { el: target,
             body: { target_id: target.dataset.projectId,
                     mode: x < r.left + r.width / 2 ? "before" : "after" } };
  }
  // Past the last tab — the bare row, or the strip's own edge.
  if (under === strip || under === $("project-tabs"))
    return { el: strip, body: { position: null } };
  return null;
}

function markTabDrop(x, y) {
  clearTabDropMarks();
  const drop = tabDropAt(x, y);
  pendingTabDrop = drop && drop.body;
  if (!drop) return;
  drop.el.classList.add(
    drop.body.target_id ? "drop-" + drop.body.mode : "drop-end");
}

function clearTabDropMarks() {
  document.querySelectorAll(
    "#tab-strip .drop-before, #tab-strip .drop-after, #tab-strip.drop-end")
    .forEach((el) => el.classList.remove("drop-before", "drop-after", "drop-end"));
}

function endTabDrag(tab) {
  tab.classList.remove("dragging");
  dragProjectId = null;
  pendingTabDrop = null;
  clearTabDropMarks();
}

function wireTabDrag(tab, project) {
  tab.draggable = true;
  tab.addEventListener("dragstart", (e) => {
    // Renaming owns the tab while it is an input box; nothing to drag.
    if (renamingProject) { e.preventDefault(); return; }
    dragProjectId = project.id;
    pendingTabDrop = null;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", project.name);
    tab.classList.add("dragging");
  });
  tab.addEventListener("dragover", (e) => {
    if (!dragProjectId || dragProjectId === project.id) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "move";
    markTabDrop(e.clientX, e.clientY);
  });
  tab.addEventListener("drop", (e) => {
    if (!dragProjectId || !pendingTabDrop) return;
    e.preventDefault();
    e.stopPropagation();
    const id = dragProjectId, body = pendingTabDrop;
    endTabDrag(tab);
    moveProject(id, body);
  });
  tab.addEventListener("dragend", () => endTabDrag(tab));
  wireTabTouchDrag(tab, project);
}

/* Phones don't get HTML5 drag-and-drop, and the strip itself scrolls
 * sideways, so a swipe has to stay a swipe: a tab only comes loose once you
 * have held it still for a moment. */
function wireTabTouchDrag(tab, project) {
  let timer = null;
  let start = null;
  let moved = false;

  const cancelHold = () => {
    if (timer) { clearTimeout(timer); timer = null; }
  };

  tab.addEventListener("touchstart", (e) => {
    // A second finger means a pinch or a two-handed scroll, not a drag.
    if (renamingProject || e.touches.length > 1) { cancelHold(); return; }
    start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    moved = false;
    cancelHold();
    timer = setTimeout(() => {
      timer = null;
      dragProjectId = project.id;
      pendingTabDrop = null;
      tab.classList.add("dragging");
      navigator.vibrate?.(10);
    }, 300);
  }, { passive: true });

  tab.addEventListener("touchmove", (e) => {
    const touch = e.touches[0];
    if (timer) {
      // Still deciding: a finger that travels is scrolling the strip.
      if (Math.abs(touch.clientX - start.x) > 8 ||
          Math.abs(touch.clientY - start.y) > 8) cancelHold();
      return;
    }
    if (dragProjectId !== project.id) return;
    e.preventDefault();  // the finger is carrying a tab, not scrolling
    moved = true;
    markTabDrop(touch.clientX, touch.clientY);
  }, { passive: false });

  const finish = () => {
    cancelHold();
    if (dragProjectId !== project.id) return;
    const id = dragProjectId, body = pendingTabDrop;
    endTabDrag(tab);
    if (!moved) return;   // held and let go without going anywhere
    // A drag ends where the finger left it, not on a tap: the click the
    // browser may still fire has to be ignored.
    tabClickSuppressed = true;
    setTimeout(() => { tabClickSuppressed = false; }, 400);
    if (body) moveProject(id, body);
  };
  tab.addEventListener("touchend", finish);
  tab.addEventListener("touchcancel", finish);
}

/* The empty stretch of row past the last tab means "put it at the end" —
 * the strip itself is only as wide as its tabs, so the row is where there is
 * actually somewhere to let go. */
function wireTabStripDropTarget() {
  const row = $("project-tabs");
  const strip = $("tab-strip");
  // Only the bare row counts: a drop that bubbled up from a tab has already
  // been dealt with, and the ＋ button is not a place to put a tab.
  const onBackground = (e) => dragProjectId && (e.target === row || e.target === strip);
  row.addEventListener("dragover", (e) => {
    if (!onBackground(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearTabDropMarks();
    pendingTabDrop = { position: null };
    strip.classList.add("drop-end");
  });
  row.addEventListener("dragleave", (e) => {
    if (onBackground(e)) strip.classList.remove("drop-end");
  });
  row.addEventListener("drop", (e) => {
    if (!onBackground(e) || !pendingTabDrop) return;
    e.preventDefault();
    const id = dragProjectId, body = pendingTabDrop;
    dragProjectId = null;
    pendingTabDrop = null;
    clearTabDropMarks();
    moveProject(id, body);
  });
}

/* ---------------- rendering ---------------- */

function fmtMinutes(min) {
  if (min == null) return "?";
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

/* Four steps of deadline pressure, and none of them red.
 *
 * The one that matters is the last. A date that has gone by is not new
 * information — you already know, and you already feel bad about it — so
 * shouting "OVERDUE" in high-contrast red buys nothing and costs the whole
 * list: a page that makes you flinch is a page you stop opening, and a task
 * app you have stopped opening has failed at the only thing it does. So a
 * missed deadline states the fact quietly, in lavender, and the loudest thing
 * on the badge is the offer to fix it. */
function fmtDeadline(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  const now = new Date();
  const diffMin = (d - now) / 60000;
  const opts = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  const label = d.toLocaleString(undefined, opts);
  if (diffMin < 0)
    return { label: `was due ${label}`, cls: "gentle", overdue: true };
  if (diffMin < 180) return { label: `due ${label}`, cls: "urgent" };
  if (diffMin < 60 * 24) return { label: `due ${label}`, cls: "soon" };
  return { label: `due ${label}`, cls: "" };
}

const QUAD_LABEL = {
  quick_win: "⚡ quick win",
  major_project: "🏔 major project",
  fill_in: "· fill-in",
  thankless: "😮‍💨 thankless",
};

/* ---------------- sorting ----------------
 * The sorter is a lens, not a plan: it decides the order the top-level tasks
 * are read in and nothing else. Steps nested inside a task stay in the order
 * the breakdown put them in — a plan whose steps rearrange themselves by score
 * is not a plan — and ▶ Focus still picks by urgency (or by your manual order),
 * so glancing at the list by deadline never quietly changes what you are about
 * to work on.
 *
 * The server does the actual comparing: every task arrives with a
 * `list_sort_key` computed from the stored setting, so the page only has to
 * put the keys in order and draw the control that chose them. */

const SORT_FIELDS = ["smart", "manual", "score", "deadline", "subtasks", "created"];
const DEFAULT_SORT_DIR = {
  smart: "desc", manual: "asc", score: "desc",
  deadline: "asc", subtasks: "desc", created: "desc",
};
/* What each direction actually means, in the words of the field — "descending
 * subtasks" is a sentence nobody thinks in. */
const SORT_DIR_LABEL = {
  score: { desc: "↓ highest first", asc: "↑ lowest first" },
  deadline: { asc: "↑ soonest first", desc: "↓ furthest off first" },
  subtasks: { desc: "↓ most steps first", asc: "↑ fewest steps first" },
  created: { desc: "↓ newest first", asc: "↑ oldest first" },
};

/* Mirrors logic.sort_mode on the server: the old manual-order flag still means
 * a hand-arranged list, so a list arranged before the sorter existed keeps the
 * shape it was left in. */
function sortMode() {
  let field = settings?.sort_field || "smart";
  if (!SORT_FIELDS.includes(field)) field = "smart";
  if (field === "smart" && settings?.manual_order) field = "manual";
  let dir = settings?.sort_dir;
  if (dir !== "asc" && dir !== "desc") dir = DEFAULT_SORT_DIR[field];
  return { field, dir };
}

function renderSortBar() {
  if (!settings) return;
  const { field, dir } = sortMode();
  $("sort-field").value = field;
  const btn = $("sort-dir");
  const labels = SORT_DIR_LABEL[field];
  // Smart and manual carry their own direction — "urgency, ascending" would
  // just be the same list upside down — so the flip button steps aside.
  btn.hidden = !labels;
  if (!labels) return;
  btn.textContent = labels[dir];
  btn.title = `Sorting ${labels[dir].slice(2)} — click to flip`;
  btn.setAttribute("aria-label", "Reverse the sort order");
}

async function setSort(field, dir) {
  try {
    settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({ sort_field: field, sort_dir: dir }),
    });
    applyState(await api("/state"));  // the keys are computed server-side
  } catch (e) { toast(e.message, true); }
  renderSortBar();
}

function sortActive(tasks) {
  const key = (t) => t.list_sort_key || t.sort_key;
  return [...tasks].sort((a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) {
      if (ka[i] < kb[i]) return -1;
      if (ka[i] > kb[i]) return 1;
    }
    return 0;
  });
}

/* Progress, in time rather than in item counts: how much of a task's
 * buffered subtree estimate is already finished, and how much is still
 * ahead. Shown under any task that contains subtasks. */
function progressBar(task) {
  const total = task.rollup_estimate || 0;
  const done = Math.min(task.rollup_done || 0, total);
  const left = Math.max(0, total - done);
  const pct = total ? Math.round((done / total) * 100) : 0;

  const wrap = document.createElement("div");
  wrap.className = "progress-wrap";
  wrap.title = `${fmtMinutes(done)} of ${fmtMinutes(total)} of subtask time done`;

  const bar = document.createElement("div");
  bar.className = "progress";
  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = pct + "%";
  if (pct >= 100) fill.classList.add("complete");
  bar.appendChild(fill);

  const legend = document.createElement("div");
  legend.className = "progress-legend";
  const left_ = document.createElement("span");
  left_.textContent = `${fmtMinutes(done)} done`;
  const right_ = document.createElement("span");
  right_.textContent = `${fmtMinutes(left)} left · ${pct}%`;
  legend.append(left_, right_);

  wrap.append(bar, legend);
  return wrap;
}

/* ---------------- folding ----------------
 * A task with a long tail of subtasks is exactly the task you most need to
 * stop staring at. Folding one hides its steps and leaves the container —
 * its rolled-up time, deadline and progress bar — sitting there as one line.
 * The fold is stored on the task, so the shape you left the list in is the
 * shape it comes back in, on this device and any other. */

/* The one task whose steps have just been unfolded, so that render() animates
 * that nest and leaves every other one alone. */
let unfoldedNode = null;

function twisty(task, count, collapsed) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "twisty" + (collapsed ? " folded" : "");
  // One glyph that rotates, rather than two that swap: the turn says which
  // direction the state went, which a substitution cannot.
  btn.textContent = "▾";
  const label = `${count} subtask${count === 1 ? "" : "s"}`;
  btn.title = collapsed ? `Show ${label}` : `Hide ${label}`;
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.setAttribute("aria-label",
    (collapsed ? "Show " : "Hide ") + label + " of " + task.title);
  btn.addEventListener("click", () => toggleCollapsed(task.id));
  return btn;
}

/* Keeps the checkboxes of leaves and containers in one column. */
function spacer() {
  const span = document.createElement("span");
  span.className = "twisty-spacer";
  span.setAttribute("aria-hidden", "true");
  return span;
}

function holdsNextTask(task) {
  return !!state.next_task_id &&
    flatten(task.subtasks || []).some((t) => t.id === state.next_task_id);
}

/* Optimistic on purpose: folding is a view change, and waiting on a round
 * trip to see a list close is the kind of lag that loses you the thought. */
async function toggleCollapsed(id) {
  const task = findTask(id);
  if (!task) return;
  const collapsed = !task.collapsed;
  task.collapsed = collapsed;
  toggleFocusFor = id;
  if (!collapsed) unfoldedNode = id;
  render();
  // The reply rebuilds the list a second time, throwing away the nodes the
  // first render just animated — so both cues are armed twice, for the same
  // reason and in the same place.
  toggleFocusFor = id;
  if (!collapsed) unfoldedNode = id;
  await patchTask(id, { collapsed });
}

function taskNode(task, isSub) {
  const el = document.createElement("div");
  const active = task.status === "todo" || task.status === "in_progress";
  const subs = (task.subtasks || []).filter(
    (s) => s.status === "todo" || s.status === "in_progress");
  const composing = active && subtaskDraftFor === task.id;
  // Folding is only ever about subtasks you can actually see: a stale flag on
  // a task whose subtasks are all finished must not hide anything.
  const collapsed = !!task.collapsed && subs.length > 0;
  el.className = "task" + (active ? "" : " done-task") +
    (task.id === state.next_task_id ? " next" : "") +
    // Drawn as a container rather than a row: it gets the lid, the heavier
    // check-off, and the nest underneath.
    (subs.length && !collapsed ? " has-kids" : "");
  el.dataset.id = task.id;

  const row = document.createElement("div");
  row.className = "task-row";

  if (active) {
    row.appendChild(dragHandle(task, el));
    wireDropTarget(el, task);
  }

  // The fold sits in its own column so that parents and leaves line up: a
  // task with nothing under it gets the empty space instead of the arrow.
  row.appendChild(subs.length ? twisty(task, subs.length, collapsed)
                             : spacer());

  const check = document.createElement("input");
  check.type = "checkbox";
  check.checked = task.status === "done";
  check.title = "Mark done";
  check.addEventListener("change", () => {
    // Only finishing something is worth celebrating. Un-ticking is a
    // correction, and a correction that threw confetti would be insufferable.
    if (check.checked) Motion.checkOff(el, () => completeTask(task.id, null));
    else completeTask(task.id, null);
  });
  row.appendChild(check);

  const title = document.createElement("span");
  title.className = "task-title";
  title.textContent = task.title;
  title.addEventListener("click", () => openDetail(task.id));
  row.appendChild(title);

  if (active) {
    const addSub = document.createElement("button");
    addSub.className = "task-btn ghost";
    addSub.textContent = "+";
    addSub.title = "Add a subtask yourself";
    addSub.setAttribute("aria-label", "Add a subtask to " + task.title);
    addSub.addEventListener("click", () => openSubtaskComposer(task.id));
    row.appendChild(addSub);

    const bd = document.createElement("button");
    bd.className = "task-btn ghost";
    bd.textContent = "⚡";
    bd.title = "Break down into subtasks";
    bd.addEventListener("click", () => breakdown(task.id, bd));
    row.appendChild(bd);

    const focus = document.createElement("button");
    focus.className = "task-btn ghost";
    focus.textContent = "▶";
    focus.title = "Focus on this now";
    focus.addEventListener("click", () => startFocus(task.id));
    row.appendChild(focus);
  }

  // Last in the row, past everything you press all day: the one control here
  // that destroys work rather than doing it. Finished tasks get it too —
  // clearing out the done list is most of what it is for.
  row.appendChild(deleteBtn(task));
  el.appendChild(row);

  if (active) {
    const badges = document.createElement("div");
    badges.className = "task-badges";
    const add = (text, cls = "") => {
      const b = document.createElement("span");
      b.className = "badge " + cls;
      b.textContent = text;
      badges.appendChild(b);
      return b;
    };
    if (task.id === state.next_task_id) add("next up", "next-badge");
    // Folded away, but the thing you were told to do next is in there:
    // hiding that without a word is how a good list quietly stops working.
    if (collapsed && holdsNextTask(task)) add("next up inside", "next-badge");
    // The score, out in the open. It is the number the calendar sorts by, the
    // number "next up" is picked with, and — when gamification is on — the
    // number finishing the task pays you, so it belongs on the task itself
    // rather than buried in a dialog.
    if (task.score != null) {
      add(`★ ${Math.round(task.score)}`, "score-badge").title = scoreTitle(task);
    }
    // A task that contains subtasks is worth what it holds: the estimate is
    // the sum of everything underneath, the deadline the furthest one inside.
    const est = task.has_subtasks ? task.rollup_estimate : task.buffered_estimate;
    const dlIso = task.has_subtasks ? task.rollup_deadline : task.deadline;
    const dlSrc = task.has_subtasks ? task.rollup_deadline_source : task.deadline_source;
    if (est != null) {
      const b = add(`~${fmtMinutes(est)}`, "");
      if (task.has_subtasks) b.title = "Total of all subtasks";
    }
    // When it is due to begin, when that is still ahead of you. A deadline
    // says when it must be finished; on a list of things you have not started,
    // "starts 18:00" is usually the more useful half.
    const startBadge = fmtStart(task.start_at);
    if (startBadge) add(startBadge.label, "start-badge").title = startBadge.title;
    const dl = fmtDeadline(dlIso);
    // A deadline that has gone by is the one badge worth making clickable:
    // the thing you want the second you read it is to move it, and the
    // calendar is a detour for that. Same dialog, same "keeps its length"
    // promise — just reachable from where the task actually lives.
    if (dl && dl.overdue) badges.appendChild(nudgeBadge(task, dl, dlSrc));
    else if (dl) add(dl.label + (dlSrc === "auto" ? " (auto)" : ""), dl.cls);
    if (task.quadrant) add(QUAD_LABEL[task.quadrant], "quad-" + task.quadrant);
    // A task you will see again next week reads differently from a one-off
    // with the same date on it, so the rhythm is on the task, in words.
    if (task.recurrence) {
      const rb = add("🔁 " + task.recurrence.summary,
                     task.recurrence.active ? "repeat-badge" : "repeat-badge over");
      rb.title = task.recurrence.active
        ? `Repeats ${task.recurrence.summary}. Only one copy is on your list ` +
          `at a time — finish this one and the next turns up when it's due.`
        : `This was the last one — the repeat has run its course.`;
    }
    if (collapsed)
      add(`${subs.length} subtask${subs.length === 1 ? "" : "s"} hidden`, "folded");
    if (badges.children.length) el.appendChild(badges);
    // The progress bar stays out in the open when a task is folded: a rolled
    // up "40m left · 60%" is the whole point of hiding the steps.
    if (task.has_subtasks) el.appendChild(progressBar(task));
  } else if ((task.xp_awarded && settings?.gamification) || task.recurrence) {
    // What it paid, kept next to it. The Done list is the only place the
    // XP total can be traced back to the work that earned it — and the one
    // place you can see a repeating job's history as a run of finished copies.
    const badges = document.createElement("div");
    badges.className = "task-badges";
    if (task.xp_awarded && settings?.gamification) {
      const b = document.createElement("span");
      b.className = "badge xp-badge";
      b.textContent = `+${task.xp_awarded} XP`;
      b.title = "What finishing this one paid out — its score at the time.";
      badges.appendChild(b);
    }
    if (task.recurrence) {
      const b = document.createElement("span");
      b.className = "badge repeat-badge";
      b.textContent = "🔁";
      b.title = `One of a repeating job — ${task.recurrence.summary}.`;
      badges.appendChild(b);
    }
    el.appendChild(badges);
  }

  if (!collapsed && (subs.length || composing)) {
    const wrap = document.createElement("div");
    wrap.className = "subtasks";
    for (const sub of subs) wrap.appendChild(taskNode(sub, true));
    if (composing) wrap.appendChild(subtaskComposer(task.id));
    el.appendChild(wrap);
  }
  return el;
}

/* The "starts …" badge, or nothing.
 *
 * Only ever shown while the start time is still ahead: once it has gone by,
 * the task is simply something you should be doing, which the urgency and the
 * deadline badge already say more usefully than a stale timestamp would. */
function fmtStart(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  const mins = Math.round((when - Date.now()) / 60000);
  if (mins <= 0) return null;
  const today = new Date().toDateString() === when.toDateString();
  const clock = when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return {
    label: mins < 60 ? `⏳ starts in ${fmtMinutes(mins)}`
         : today ? `⏳ starts ${clock}`
         : `⏳ starts ${when.toLocaleDateString([], { month: "short", day: "numeric" })}`,
    title: `Meant to begin ${when.toLocaleString()} — the scheduler places it ` +
           `from there, and it stays out of the way until then.`,
  };
}

/* What the score means, in the one place both the list badge and the detail
 * modal read it from. */
function scoreTitle(task) {
  const score = Math.round(task.score);
  const how = task.has_subtasks
    ? `Score ${score}/100, rolled up from the steps still left inside it`
    : `Score ${score}/100 — deadline pressure, impact, and how cheap it is ` +
      `in both effort and time, in one number`;
  if (!settings?.gamification) return how + ".";
  return how + (task.has_subtasks
    ? ". Each of those steps pays out its own score in XP when you finish it."
    : `. Finishing it earns ${Math.max(1, score)} XP.`);
}

/* The overdue badge, as a button. Clicking it opens the nudge dialog on this
 * one task; the calendar's rail is where you move the whole pile at once.
 *
 * The label carries the invitation rather than the verdict — "reschedule when
 * ready" is the same click as "OVERDUE" would have been, minus the implication
 * that you have done something wrong by not having done it yet. */
function nudgeBadge(task, dl, dlSrc) {
  const btn = document.createElement("button");
  btn.className = "badge gentle nudge-badge";
  btn.type = "button";
  btn.textContent = dl.label + (dlSrc === "auto" ? " (auto)" : "") +
    " · reschedule when ready";
  btn.title = `Move “${task.title}” to a new deadline — it keeps its ` +
    `length (${fmtMinutes(task.length_min)} of buffered work)` +
    (task.has_subtasks ? ", and its subtasks slide with it" : "");
  btn.setAttribute("aria-label", "Reschedule " + task.title);
  btn.addEventListener("click", () => openNudge([task]));
  return btn;
}

/* ---------------- deleting ----------------
 * The only thing in the list with nothing behind it. Completing a task keeps
 * it, discarding a task keeps it; deleting takes the row out of the database,
 * and the schema cascades, so everything nested underneath goes with it.
 *
 * That cascade is the part worth stopping for, because what you lose is
 * rarely the line you clicked on: a container is one line by design — folded
 * away, or just read past — and the eleven steps it holds are the actual
 * cost. So a task with subtasks is counted, listed and confirmed against by
 * name, and a leaf is asked about once and no more than once. Deleting is
 * meant to stay usable; the warning is for when it isn't what it looks like. */

const DELETE_PREVIEW = 5;  // subtasks named in the warning before "…and N more"

function deleteBtn(task) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "task-btn ghost danger";
  btn.textContent = "✕";
  const n = flatten(task.subtasks || []).length;
  btn.title = n
    ? `Delete this task and the ${n} subtask${n === 1 ? "" : "s"} under it`
    : "Delete this task";
  btn.setAttribute("aria-label", "Delete " + task.title);
  btn.addEventListener("click", () => deleteTask(task.id));
  return btn;
}

async function deleteTask(id) {
  const task = findTask(id);
  if (!task) return;
  // Everything under it, at every depth and whatever its status: the cascade
  // makes no distinction, so neither does the count you are shown.
  const doomed = flatten(task.subtasks || []);
  const n = doomed.length;
  let message = `Delete “${task.title}”?`;
  if (n) {
    const lines = doomed.slice(0, DELETE_PREVIEW).map((t) => `  • ${t.title}`);
    if (n > lines.length) lines.push(`  • …and ${n - lines.length} more`);
    message =
      `“${task.title}” has ${n} subtask${n === 1 ? "" : "s"} nested under it, ` +
      `and ${n === 1 ? "it goes" : "they all go"} too:\n\n` +
      lines.join("\n") + `\n\nDelete all ${n + 1} of them?`;
  }
  if (task.recurrence && task.recurrence.active) {
    // Deleting one copy of a repeating job deletes that copy. Saying so is
    // the difference between clearing today's chore and quietly discovering
    // next week that you cancelled it.
    message += `\n\n“${task.title}” repeats ${task.recurrence.summary}. ` +
      `Deleting this copy skips this one — the next still comes. ` +
      `Use Repeat → Doesn't repeat in the task to stop it for good.`;
  }
  if (!confirm(message + "\n\nThis can't be undone.")) return;
  // The row has to still exist to be animated out of the list, so this is the
  // one place the motion genuinely gates the work rather than riding alongside
  // it. ~260ms, and the request goes out the moment it lands.
  const node = document.querySelector(`.task[data-id="${cssEscape(id)}"]`);
  if (node) await new Promise((done) => Motion.leave(node, done));
  // An "add a subtask" box open on a task that is about to stop existing has
  // nowhere left to put what you type.
  if (subtaskDraftFor === id || doomed.some((t) => t.id === subtaskDraftFor))
    subtaskDraftFor = null;
  try {
    applyState(await api(`/tasks/${id}`, { method: "DELETE" }));
    toast(n ? `Deleted “${task.title}” and ${n} subtask${n === 1 ? "" : "s"}.`
            : `Deleted “${task.title}”.`);
  } catch (e) { toast(e.message, true); }
}

/* Which task ids the list has already drawn. The list is rebuilt wholesale on
 * every state change, so without this every row would re-animate every time
 * anything at all happened. Cleared when the open project changes, because
 * arriving at a different list should read as arriving. */
const drawn = new Set();

function render() {
  const list = $("task-list");
  list.replaceChildren();
  const active = state.tasks.filter(
    (t) => t.status === "todo" || t.status === "in_progress");
  for (const t of sortActive(active)) list.appendChild(taskNode(t, false));
  $("empty-hint").hidden = active.length > 0;
  $("list-hint").hidden = active.length === 0;
  // Nothing to sort is nothing to decide about: the bar arrives with the list.
  $("list-toolbar").hidden = active.length === 0;
  renderSortBar();
  const project = (state.projects || []).find(
    (p) => p.id === state.active_project_id);
  $("empty-project").textContent = project && (state.projects || []).length > 1
    ? `“${project.name}” is empty. ` : "Nothing here yet. ";

  const finished = state.tasks.filter(
    (t) => t.status === "done" || t.status === "discarded");
  $("done-section").hidden = finished.length === 0;
  const doneList = $("done-list");
  doneList.replaceChildren();
  for (const t of finished) doneList.appendChild(taskNode(t, false));

  Motion.enterNew(list, drawn);
  // Steps that have just been unfolded get their own little cascade out from
  // under the container's lid.
  if (unfoldedNode) {
    const wrap = list.querySelector(
      `.task[data-id="${cssEscape(unfoldedNode)}"] > .subtasks`);
    unfoldedNode = null;
    if (wrap) Motion.unfold(wrap);
  }
  renderRail();
  restoreListFocus();
}

/* The rail. Two bars, both of them counting something the app already knows —
 * there is no streak table and no weekly rollup on the server, and a number
 * invented on the page to fill a card is worse than an empty card. */
function renderRail() {
  const mine = state.tasks || [];
  const done = mine.filter((t) => t.status === "done").length;
  const total = mine.filter((t) => t.status !== "discarded").length;
  $("rail-streak").textContent = total ? `${done} / ${total}` : "nothing yet";
  $("rail-streak-fill").style.width = (total ? (done / total) * 100 : 0) + "%";

  const xp = state.xp;
  const on = !!(xp && settings?.gamification);
  $("rail-goal-label").textContent = on ? `Level ${xp.level}` : "Level";
  $("rail-goal").textContent = on ? `${xp.into_level} / ${xp.level_span}` : "off";
  $("rail-goal-fill").style.width = (on ? xp.progress * 100 : 0) + "%";
  $("rail-xp").textContent = on ? `level ${xp.level} · ${xp.total} XP earned` : "";

  // Open, top-level tasks only: what is actually still ahead of you in this
  // list, not what has already been finished or discarded.
  const open = mine.filter((t) => t.status === "todo" || t.status === "in_progress");
  const timeLeft = open.reduce((sum, t) =>
    sum + (t.has_subtasks ? (t.rollup_remaining || 0) : (t.buffered_estimate || 0)), 0);
  const pointsLeft = open.reduce((sum, t) => sum + (t.score || 0), 0);
  $("rail-time-total").textContent = open.length ? fmtMinutes(timeLeft) : "—";
  $("rail-points-total").textContent = open.length ? Math.round(pointsLeft) : "—";
}

/* The list is rebuilt wholesale on every state change, so anything the user
 * was typing in or steering with the keyboard has to be handed its focus
 * back afterwards. */
function restoreListFocus() {
  if (subtaskDraftFor) {
    const input = document.querySelector(
      `.task[data-id="${cssEscape(subtaskDraftFor)}"] .subtask-composer input`);
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      return;
    }
    subtaskDraftFor = null;  // parent vanished (completed, deleted, discarded)
  }
  if (handleFocusFor) {
    const handle = document.querySelector(
      `.task[data-id="${cssEscape(handleFocusFor)}"] > .task-row > .drag-handle`);
    handleFocusFor = null;
    if (handle) handle.focus();
  }
  if (toggleFocusFor) {
    const twist = document.querySelector(
      `.task[data-id="${cssEscape(toggleFocusFor)}"] > .task-row > .twisty`);
    toggleFocusFor = null;
    if (twist) twist.focus();
  }
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : value;
}

/* ---------------- manual subtasks ---------------- */

function openSubtaskComposer(parentId) {
  subtaskDraftFor = parentId;
  subtaskDraftText = "";
  // Typing into a box you cannot see is no use: opening the composer on a
  // folded task unfolds it, here and on the server.
  const task = findTask(parentId);
  if (task && task.collapsed) {
    task.collapsed = false;
    patchTask(parentId, { collapsed: false });
  }
  render();
}

function closeSubtaskComposer() {
  subtaskDraftFor = null;
  subtaskDraftText = "";
  render();
}

/* Stays open after each add: subtasks come out of your head in bursts, and
 * re-opening the box for every one of them is exactly the kind of friction
 * this app exists to remove. */
function subtaskComposer(parentId) {
  const form = document.createElement("form");
  form.className = "subtask-composer";
  form.autocomplete = "off";

  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 500;
  input.placeholder = "New subtask… (Enter adds, Esc closes)";
  input.value = subtaskDraftText;
  input.addEventListener("input", () => { subtaskDraftText = input.value; });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closeSubtaskComposer(); }
  });

  const add = document.createElement("button");
  add.type = "submit";
  add.className = "accent task-btn";
  add.textContent = "Add";

  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost task-btn";
  cancel.textContent = "✕";
  cancel.title = "Done adding subtasks";
  cancel.addEventListener("click", closeSubtaskComposer);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const title = input.value.trim();
    if (!title) { closeSubtaskComposer(); return; }
    subtaskDraftText = "";
    input.value = "";
    addTask(title, parentId);
  });

  form.append(input, add, cancel);
  return form;
}

/* ---------------- toasts & celebration ---------------- */

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function celebrate() {
  if (!settings?.gamification) return;
  const box = $("celebration");
  box.hidden = false;
  box.replaceChildren();
  // The theme's own colours, so the celebration belongs to the app rather
  // than arriving from a party-supplies shop.
  const colors = ["#67d8ea", "#8f7ce6", "#e8c46a", "#7fe0b0", "#eaf4ff"];
  for (let i = 0; i < 36; i++) {
    const c = document.createElement("div");
    c.className = "confetti";
    c.style.left = Math.random() * 100 + "vw";
    c.style.background = colors[i % colors.length];
    c.style.animationDelay = Math.random() * 0.4 + "s";
    // Every piece gets its own sideways drift and its own spin. Identical
    // arcs read as a machine firing; a spread of them reads as a handful of
    // paper thrown in the air, which is the whole idea.
    c.style.setProperty("--drift", (Math.random() * 240 - 120).toFixed(0) + "px");
    c.style.setProperty("--spin", (360 + Math.random() * 720).toFixed(0) + "deg");
    box.appendChild(c);
  }
  setTimeout(() => { box.hidden = true; box.replaceChildren(); }, 2200);
}

/* ---------------- daily budget ----------------
 * Nothing to see until the budget starts biting, and then one badge saying
 * so. The server has already decided which tier answers which call; this only
 * reports it, so the badge and the routing can never disagree. */

function money(usd) {
  return `$${Number(usd || 0).toFixed(2)}`;
}

function renderThrottle() {
  const badge = $("throttle-badge");
  const spend = state.spend;
  if (!spend || !spend.stage) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.textContent = `⚡ ${money(spend.today)} / ${money(spend.budget)}`;
  badge.title = `Running cheap: ${spend.note}. Braindumps use the ` +
    `${spend.tiers.deep} model, breakdowns the ${spend.tiers.balanced} one. ` +
    `Resets at midnight.`;
}

/* ---------------- XP & levels ----------------
 * The bar in the corner is the same score you can read on every task, added
 * up: finish something worth 62 and the bar moves 62. It only ever animates
 * on a gain — every other state read redraws it exactly where it already was,
 * because a bar that slides about on its own is noise, not feedback. */

let xpDrawn = null;      // what the meter is currently showing
let xpGainTimer = null;
const XP_FILL_MS = 600;  // must match the transition in style.css

function renderXp() {
  const meter = $("xp-meter");
  const xp = state.xp;
  if (!xp || !settings?.gamification) {
    meter.hidden = true;
    // Remembered even while hidden, so turning the setting back on shows the
    // bar where it belongs instead of replaying an animation you missed.
    xpDrawn = xp ? { level: xp.level, progress: xp.progress } : null;
    return;
  }
  const wasHidden = meter.hidden;
  meter.hidden = false;
  $("xp-level").textContent = xp.level;
  $("xp-count").textContent = `${xp.into_level} / ${xp.level_span}`;
  meter.title = `Level ${xp.level} — ${xp.total} XP earned, ${xp.to_next} to ` +
    `level ${xp.level + 1}. Finishing a task pays out its score.`;
  meter.setAttribute("aria-label",
    `Level ${xp.level}, ${xp.into_level} of ${xp.level_span} XP`);

  const gained = xp.gained || 0;
  const previous = xpDrawn;
  xpDrawn = { level: xp.level, progress: xp.progress };
  if (!gained || wasHidden || !previous) { xpFill(xp.progress, false); return; }

  xpFloat(gained);
  Motion.play("xpgain");
  // The focus overlay covers the header, so a bar sliding about underneath it
  // is a reward nobody sees. Finishing a task from inside a session says it in
  // the one thing that does show through.
  if (focus.open) toast(`+${gained} XP`);
  if (xp.level > previous.level) {
    // Run the old level out to the end before starting the new one: a bar
    // that jumps from nearly-full to nearly-empty with no fanfare reads as
    // losing your progress rather than as passing a milestone.
    xpFill(1, false);
    setTimeout(() => {
      xpFill(0, true);          // back to empty with no animation at all
      xpFill(xp.progress, false);
      meter.classList.remove("levelup");
      void meter.offsetWidth;   // restart the flash if two levels land at once
      meter.classList.add("levelup");
      Motion.play("levelup");
      toast(`Level ${xp.level}! ${xp.to_next} XP to the next one.`);
    }, XP_FILL_MS + 40);
  } else {
    xpFill(xp.progress, false);
  }
}

/* `snap` fills without animating — the one moment that needs it is the reset
 * to an empty bar on a level-up, which must not be seen sliding backwards. */
function xpFill(progress, snap) {
  const fill = $("xp-fill");
  const width = Math.max(0, Math.min(1, progress)) * 100 + "%";
  if (!snap) { fill.style.width = width; return; }
  fill.style.transition = "none";
  fill.style.width = width;
  void fill.offsetWidth;  // force the reflow, so the next width does animate
  fill.style.transition = "";
}

/* The "+62 XP" that rises off the bar. */
function xpFloat(amount) {
  const el = $("xp-gain");
  el.textContent = `+${amount} XP`;
  el.hidden = false;
  el.classList.remove("rise");
  void el.offsetWidth;
  el.classList.add("rise");
  clearTimeout(xpGainTimer);
  xpGainTimer = setTimeout(() => {
    el.hidden = true;
    el.classList.remove("rise");
  }, 1600);
}

/* ---------------- task actions ---------------- */

async function addTask(title, parentId = null) {
  // The box kicks as it lets go, so the eye is already travelling down to the
  // list by the time the new row arrives there. Staging, in one line.
  if (!parentId) Motion.kick($("add-form"), "sent");
  try {
    applyState(await api("/tasks", {
      method: "POST",
      body: JSON.stringify({ title, parent_id: parentId }),
    }));
  } catch (e) { toast(e.message, true); }
}

async function breakdown(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    applyState(await api(`/tasks/${id}/breakdown`, {
      method: "POST",
      body: JSON.stringify({ granularity: Number($("add-granularity").value) || null }),
    }));
  } catch (e) { toast(e.message, true); }
  if (btn) { btn.disabled = false; btn.textContent = "⚡"; }
}

async function completeTask(id, actualMinutes) {
  // Read the rhythm before the task is finished: what comes back after this
  // one is the question ticking off a repeating task always raises, and by
  // the time the reply lands this copy is on the Done list.
  const repeat = findAnyTask(id)?.recurrence;
  try {
    applyState(await api(`/tasks/${id}/complete`, {
      method: "POST",
      body: JSON.stringify({ actual_time: actualMinutes }),
    }));
    celebrate();
    if (repeat?.active) announceNextOccurrence(repeat.series_id, id);
  } catch (e) { toast(e.message, true); }
}

/* Ticking off a copy of a repeating job and seeing nothing take its place
 * reads as a feature that has quietly stopped working — and most of the time
 * nothing is wrong at all: the next one simply is not due yet, and "not yet"
 * is invisible. So the app says which of the three it was. */
function announceNextOccurrence(seriesId, doneId) {
  const fresh = (state.tasks || []).find(
    (t) => t.id !== doneId && t.recurrence?.series_id === seriesId &&
           (t.status === "todo" || t.status === "in_progress"));
  if (fresh) {
    toast(`🔁 The next one is on your list${fresh.deadline
      ? `, due ${new Date(fresh.deadline).toLocaleString()}` : ""}.`);
    return;
  }
  const after = findTask(doneId)?.recurrence;
  if (after && !after.active) {
    toast("🔁 That was the last one — this repeat has run its course.");
  } else if (after?.next_at) {
    toast(`🔁 The next one is due ${new Date(after.next_at).toLocaleString()}` +
          ` — it joins your list nearer the time, and it is already on the calendar.`);
  }
}

async function patchTask(id, fields) {
  try {
    applyState(await api(`/tasks/${id}`, {
      method: "PATCH", body: JSON.stringify(fields),
    }));
  } catch (e) { toast(e.message, true); }
}

/* ---------------- manual ordering & nesting ----------------
 * Drag a task by its ⠿ handle: dropping on the top or bottom edge of another
 * task places it above or below as a sibling, dropping on the middle nests it
 * inside, and dropping on empty list space pulls it back out to the top level.
 * The same moves are on the keyboard once the handle is focused, because
 * drag-and-drop is unusable for plenty of the people this app is for.
 *
 * The server is told "before/after/inside that task" rather than an index, so
 * a drop always means what it looked like on screen — the visible order and
 * the stored order aren't the same thing until manual ordering kicks in. */

let dragId = null;      // task being dragged
let pendingDrop = null; // {targetId, mode} under the pointer right now

async function moveTask(id, body) {
  const wasAuto = settings && sortMode().field !== "manual";
  try {
    applyState(await api(`/tasks/${id}/move`, {
      method: "POST", body: JSON.stringify(body),
    }));
  } catch (e) { toast(e.message, true); return; }
  // The first move takes the list off whatever it was sorted by; say so once,
  // then stop. The sorter itself has already flipped to Manual on the server.
  if (wasAuto) {
    try { settings = await api("/settings"); } catch {}
    renderSortBar();
    if (sortMode().field === "manual")
      toast("Manual order on — the list stays exactly as you arrange it. " +
            "Pick another Sort above to go back.");
  }
}

function dragHandle(task, el) {
  const handle = document.createElement("button");
  handle.type = "button";
  handle.className = "drag-handle";
  handle.textContent = "⠿";
  handle.title = "Drag to reorder or nest — or use the arrow keys";
  handle.setAttribute("aria-label", "Reorder " + task.title);
  // The task itself carries the drag, but only once you grab the handle:
  // otherwise selecting text or hitting a button turns into a drag.
  handle.addEventListener("mousedown", () => { el.draggable = true; });
  handle.addEventListener("mouseup", () => { el.draggable = false; });
  handle.addEventListener("keydown", (e) => {
    const dir = { ArrowUp: "up", ArrowDown: "down",
                  ArrowLeft: "out", ArrowRight: "in" }[e.key];
    if (!dir) return;
    e.preventDefault();
    nudgeTask(task, dir);
  });

  el.addEventListener("dragstart", (e) => {
    e.stopPropagation();
    dragId = task.id;
    pendingDrop = null;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", task.id);
    el.classList.add("dragging");
  });
  el.addEventListener("dragend", () => {
    el.draggable = false;
    el.classList.remove("dragging");
    dragId = null;
    pendingDrop = null;
    clearDropMarks();
  });

  wireTouchDrag(handle, task, el);
  return handle;
}

/* Phones don't get HTML5 drag-and-drop at all, and this app is meant to be
 * open on one. Same gesture, driven off the touch stream: hold the handle,
 * slide over the task you want to be next to or inside, let go. */
function wireTouchDrag(handle, task, el) {
  let moving = false;

  handle.addEventListener("touchstart", (e) => {
    e.preventDefault();  // the finger is dragging a task, not scrolling
    moving = true;
    dragId = task.id;
    pendingDrop = null;
    el.classList.add("dragging");
  }, { passive: false });

  handle.addEventListener("touchmove", (e) => {
    if (!moving) return;
    e.preventDefault();
    const touch = e.touches[0];
    const under = document.elementFromPoint(touch.clientX, touch.clientY);
    const targetEl = under && under.closest(".task");
    clearDropMarks();
    pendingDrop = null;
    // Only active tasks carry a handle, and only they can be dropped onto.
    if (targetEl && targetEl.dataset.id !== dragId &&
        targetEl.querySelector(":scope > .task-row > .drag-handle") &&
        !inSubtree(dragId, targetEl.dataset.id)) {
      const mode = dropMode(targetEl, touch.clientY);
      pendingDrop = { targetId: targetEl.dataset.id, mode };
      targetEl.classList.add("drop-" + mode);
    } else if (under === $("task-list")) {
      $("task-list").classList.add("drop-root");
    }
  }, { passive: false });

  const finish = () => {
    if (!moving) return;
    moving = false;
    el.classList.remove("dragging");
    const id = dragId, drop = pendingDrop;
    const toRoot = $("task-list").classList.contains("drop-root");
    dragId = null;
    pendingDrop = null;
    clearDropMarks();
    if (!id) return;
    if (drop) moveTask(id, { target_id: drop.targetId, mode: drop.mode });
    else if (toRoot) moveTask(id, { parent_id: null, position: null });
  };
  handle.addEventListener("touchend", finish);
  handle.addEventListener("touchcancel", finish);
}

function wireDropTarget(el, task) {
  el.addEventListener("dragover", (e) => {
    if (!dragId || dragId === task.id || inSubtree(dragId, task.id)) return;
    e.preventDefault();
    e.stopPropagation();  // the innermost task under the pointer wins
    e.dataTransfer.dropEffect = "move";
    const mode = dropMode(el, e.clientY);
    pendingDrop = { targetId: task.id, mode };
    clearDropMarks();
    el.classList.add("drop-" + mode);
  });
  el.addEventListener("drop", (e) => {
    if (!pendingDrop) return;
    e.preventDefault();
    e.stopPropagation();
    const id = dragId, drop = pendingDrop;
    clearDropMarks();
    if (id) moveTask(id, { target_id: drop.targetId, mode: drop.mode });
  });
}

/* Edges of the title row mean "next to this one"; anywhere else on the card —
 * middle, badges, the gap its subtasks live in — means "inside this one". */
function dropMode(el, y) {
  const row = el.querySelector(":scope > .task-row");
  if (!row) return "into";
  const r = row.getBoundingClientRect();
  const edge = Math.max(6, r.height * 0.35);
  if (y < r.top + edge) return "before";
  if (y <= r.bottom && y > r.bottom - edge) return "after";
  return "into";
}

function clearDropMarks() {
  document.querySelectorAll(".drop-before, .drop-after, .drop-into, .drop-root")
    .forEach((el) => el.classList.remove(
      "drop-before", "drop-after", "drop-into", "drop-root"));
}

function inSubtree(rootId, id) {
  const root = findTask(rootId);
  return !!root && flatten([root]).some((t) => t.id === id);
}

/* Siblings as they are actually drawn: top-level tasks go through the sort,
 * subtasks are already in their stored order, and finished ones aren't on
 * screen to move around. */
function renderedSiblings(task) {
  const parent = task.parent_id ? findTask(task.parent_id) : null;
  const list = parent ? (parent.subtasks || []) : state.tasks;
  const active = list.filter(
    (t) => t.status === "todo" || t.status === "in_progress");
  return parent ? active : sortActive(active);
}

function nudgeTask(task, dir) {
  const sibs = renderedSiblings(task);
  const i = sibs.findIndex((t) => t.id === task.id);
  if (i < 0) return;
  const prev = sibs[i - 1], next = sibs[i + 1];
  handleFocusFor = task.id;
  if (dir === "up" && prev) moveTask(task.id, { target_id: prev.id, mode: "before" });
  else if (dir === "down" && next) moveTask(task.id, { target_id: next.id, mode: "after" });
  else if (dir === "in" && prev) moveTask(task.id, { target_id: prev.id, mode: "into" });
  else if (dir === "out" && task.parent_id)
    moveTask(task.id, { target_id: task.parent_id, mode: "after" });
  else handleFocusFor = null;  // already as far that way as it goes
}

/* Empty space in the list is the way back out to the top level. */
function wireListDropTarget() {
  const list = $("task-list");
  // Only the bare list counts. Events that bubble up from a task the drag
  // can't legally land on (its own subtree) must stay refused, not quietly
  // turn into "yank it out to the top level".
  const onBackground = (e) => dragId && e.target === list;
  list.addEventListener("dragover", (e) => {
    if (!onBackground(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    pendingDrop = null;
    clearDropMarks();
    list.classList.add("drop-root");
  });
  list.addEventListener("dragleave", (e) => {
    if (e.target === list) list.classList.remove("drop-root");
  });
  list.addEventListener("drop", (e) => {
    if (!onBackground(e)) return;
    e.preventDefault();
    const id = dragId;
    clearDropMarks();
    if (id) moveTask(id, { parent_id: null, position: null });
  });
}

/* ---------------- detail modal ---------------- */

function isoToLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openDetail(id) {
  const task = findAnyTask(id);
  if (!task) return;
  detailTaskId = id;
  $("d-title").value = task.title;
  $("d-description").value = task.description || "";
  $("d-deadline").value = task.deadline_source === "user" ? isoToLocalInput(task.deadline) : "";
  $("d-deadline-source").textContent =
    task.deadline_source === "auto" ? `auto: ${new Date(task.deadline).toLocaleString()}` :
    task.deadline_source === "user" ? "set by you" : "none";
  $("d-start").value = isoToLocalInput(task.start_at);
  renderStartPresets();
  updateStartNote();
  $("d-estimate").value = task.estimated_time ?? "";
  $("d-impact").value = task.impact ?? 5;
  $("d-effort").value = task.effort ?? 5;
  // Moving between tabs only means anything once there is more than one.
  const projects = state.projects || [];
  const select = $("d-project");
  select.replaceChildren();
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    select.appendChild(opt);
  }
  select.value = task.project_id || state.active_project_id;
  $("d-project-row").hidden = projects.length < 2;
  // Only a top-level task repeats: a step that came back on its own schedule
  // while the thing containing it did not would be a plan nobody could read.
  $("d-repeat-block").hidden = !!task.parent_id;
  loadRepeat(task);
  updateDetailDerived();
  $("modal-detail").showModal();
}

/* ---------------- start times ----------------
 * When you would like to *begin* a task, which is a different question from
 * when it has to be finished by — and for most things the easier one to
 * answer. Dinner is a six o'clock thing; a game you will get round to
 * eventually is a next-month thing. The server schedules from it: a task with
 * a start time is placed at that hour, ahead of whatever wanted the same slot
 * and matters less, and one set weeks out stops competing for today.
 *
 * The presets exist because a datetime picker is exactly the friction this
 * app is built to remove — "tonight" is one click, not six.
 */

const START_PRESETS = [
  { label: "Now", hint: "right away", at: () => new Date() },
  { label: "In an hour", hint: "an hour from now", at: () => new Date(Date.now() + 3600e3) },
  {
    label: "This evening",
    hint: "6pm today, or tomorrow evening if that has gone",
    at: () => {
      const d = new Date();
      d.setHours(18, 0, 0, 0);
      return d > new Date() ? d : (d.setDate(d.getDate() + 1), d);
    },
  },
  {
    label: "Tomorrow morning",
    hint: "9am tomorrow",
    at: () => {
      const d = new Date();
      d.setDate(d.getDate() + 1);
      d.setHours(9, 0, 0, 0);
      return d;
    },
  },
  {
    label: "Next week",
    hint: "a week out, same time of day",
    at: () => new Date(Date.now() + 7 * 24 * 3600e3),
  },
  {
    // The "it genuinely does not matter when" end of the scale, and the whole
    // reason the field is worth having: something parked a month out stops
    // taking the good hours away from work that needed them.
    label: "Some day",
    hint: "a month out — parked, and out of the way until then",
    at: () => {
      const d = new Date();
      d.setMonth(d.getMonth() + 1);
      return d;
    },
  },
];

function renderStartPresets() {
  const row = $("d-start-presets");
  row.replaceChildren();
  for (const preset of START_PRESETS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost";
    btn.textContent = preset.label;
    btn.title = `Start ${preset.hint}`;
    btn.addEventListener("click", () => {
      $("d-start").value = isoToLocalInput(preset.at().toISOString());
      updateStartNote();
    });
    row.appendChild(btn);
  }
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "ghost";
  clear.textContent = "No start time";
  clear.title = "Let the app pick a day from the task's quadrant, as it did before";
  clear.addEventListener("click", () => { $("d-start").value = ""; updateStartNote(); });
  row.appendChild(clear);
}

/* What setting this actually does, in the dialog, while you set it. The two
 * cases read very differently and both are worth saying out loud. */
function updateStartNote() {
  const raw = $("d-start").value;
  const task = findAnyTask(detailTaskId);
  const tag = $("d-start-source");
  const note = $("d-start-note");
  if (!raw) {
    tag.textContent = "none";
    note.textContent = task?.parent_id
      ? "This step is scheduled inside its parent's slot."
      : "No preferred start — the app picks a day from the task's quadrant.";
    return;
  }
  const when = new Date(raw);
  const hours = (when - Date.now()) / 3600e3;
  tag.textContent = task?.start_at &&
    Math.abs(new Date(task.start_at) - when) < 60000 ? "set" : "unsaved";
  if (task?.parent_id) {
    // A step is placed inside the block its parent was given, so a start time
    // on one changes how loudly it asks for attention, not where it lands.
    note.textContent = "A step is scheduled inside its parent's slot, so this " +
      "raises how urgent the step reads rather than moving it.";
  } else if (hours <= 0) {
    note.textContent = "That has already gone by — the task reads as ready to " +
      "start now, and will keep saying so until you do it or move it.";
  } else if (hours <= 6) {
    note.textContent = `Starts in ${fmtMinutes(Math.round(hours * 60))} — the ` +
      `scheduler puts it in the first slot that fits from then, ahead of ` +
      `anything wanting the same hours that matters less.`;
  } else {
    note.textContent = `Starts ${when.toLocaleString()} — parked until then, ` +
      `and out of the running for today's hours.`;
  }
}

function updateDetailDerived() {
  const impact = Number($("d-impact").value);
  const effort = Number($("d-effort").value);
  $("d-impact-val").textContent = impact;
  $("d-effort-val").textContent = effort;
  const th = settings?.matrix_threshold ?? 5;
  const quad = impact >= th
    ? (effort >= th ? "major_project" : "quick_win")
    : (effort >= th ? "thankless" : "fill_in");
  const badge = $("d-quadrant");
  badge.textContent = QUAD_LABEL[quad];
  badge.style.color = `var(--${quad})`;
  const est = Number($("d-estimate").value);
  const task = findAnyTask(detailTaskId);
  // The score as the server last worked it out. Impact and effort feed it, so
  // dragging those sliders changes it — but only once saved, since urgency
  // and the rolled-up length are the server's to know.
  const scoreEl = $("d-score");
  const hasScore = task && task.score != null;
  scoreEl.textContent = hasScore ? `★ ${Math.round(task.score)}` : "–";
  scoreEl.title = hasScore ? scoreTitle(task) : "";
  $("d-score-note").textContent = hasScore
    ? scoreTitle(task) + " Saving new impact, effort or estimate values re-scores it."
    : "";
  const buf = task?.buffer_applied ?? (settings ? settings.buffer : 0.3);
  $("d-buffer-note").textContent = est
    ? `Buffered: ${est}m + ${Math.round(buf * 100)}% time tax = ${fmtMinutes(Math.max(1, Math.ceil(est * (1 + buf) - 1e-9)))} — the timer uses this.`
    : "No estimate yet — hit 🎲 Re-estimate or type one.";
}

async function saveDetail() {
  const task0 = findAnyTask(detailTaskId);
  const hadRule = task0?.recurrence?.active ? task0.recurrence.rule : null;
  const repeats = !$("d-repeat-block").hidden;
  const fields = {
    title: $("d-title").value.trim() || undefined,
    description: $("d-description").value,
    impact: Number($("d-impact").value),
    effort: Number($("d-effort").value),
  };
  const est = $("d-estimate").value;
  if (est) fields.estimated_time = Number(est);
  const dl = $("d-deadline").value;
  if (dl) fields.deadline = new Date(dl).toISOString();
  else fields.clear_deadline = true;
  const start = $("d-start").value;
  if (start) fields.start_at = new Date(start).toISOString();
  else fields.clear_start_at = true;
  const task = findAnyTask(detailTaskId);
  const targetProject = $("d-project").value;
  const moving = !$("d-project-row").hidden && task &&
                 targetProject && targetProject !== task.project_id;
  const id = detailTaskId;
  $("modal-detail").close();
  await patchTask(id, fields);
  if (repeats) {
    // After the field save, so the rhythm is timed off the deadline you just
    // set rather than the one you were replacing.
    try {
      const state = await saveRepeat(id, hadRule);
      if (state) applyState(state);
    } catch (e) { toast(e.message, true); }
  }
  if (moving) {
    const name = (state.projects || []).find((p) => p.id === targetProject)?.name;
    try {
      applyState(await api(`/tasks/${id}/project`, {
        method: "POST", body: JSON.stringify({ project_id: targetProject }),
      }));
      toast(`Moved to “${name}” — it's waiting in that tab.`);
    } catch (e) { toast(e.message, true); }
  }
}

/* ---------------- repeat ----------------
 * Four shapes cover what people actually repeat — daily, weekly, monthly,
 * yearly — so those are the presets, and "Custom…" is those same four with
 * their knobs turned up (every 3 weeks on Mon & Thu; the last Friday of every
 * second month). One rule format underneath, four ways in.
 *
 * There is deliberately no date arithmetic here. The dates under the controls
 * come from `/api/recurring/preview`, the badge's wording comes down with the
 * task, and both are the same server-side describer — so the dialog, the list
 * and the schedule can never end up telling three different stories about one
 * rule. */

const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const REPEAT_UNITS = { daily: "days", weekly: "weeks", monthly: "months",
                       yearly: "years" };
const NTH_LABELS = [[1, "1st"], [2, "2nd"], [3, "3rd"], [4, "4th"], [5, "5th"],
                    [-1, "last"]];
let repeatPreviewTimer = null;
let repeatPickedDays = [];   // weekday chips, kept while the panel is hidden

/* One-time fill of the selects that never change. */
function fillRepeatOptions() {
  const monthday = $("d-repeat-monthday");
  if (monthday.options.length) return;
  for (let day = 1; day <= 31; day++) {
    const opt = document.createElement("option");
    opt.value = String(day);
    opt.textContent = String(day);
    monthday.appendChild(opt);
  }
  // The last day, whatever length the month turns out to be — which is what
  // "the end of the month" means for a bill, February included.
  const last = document.createElement("option");
  last.value = "-1";
  last.textContent = "last day";
  monthday.appendChild(last);

  const nth = $("d-repeat-nth");
  for (const [value, label] of NTH_LABELS) {
    const opt = document.createElement("option");
    opt.value = String(value);
    opt.textContent = label;
    nth.appendChild(opt);
  }
  const weekday = $("d-repeat-weekday");
  WEEKDAY_LABELS.forEach((name, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = name;
    weekday.appendChild(opt);
  });

  const chips = $("d-repeat-weekdays");
  WEEKDAY_LABELS.forEach((name, i) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "weekday-chip";
    chip.dataset.day = String(i);
    chip.textContent = name;
    chip.setAttribute("aria-pressed", "false");
    chip.addEventListener("click", () => {
      chip.classList.toggle("on");
      chip.setAttribute("aria-pressed", chip.classList.contains("on") ? "true" : "false");
      onRepeatChanged();
    });
    chips.appendChild(chip);
  });
}

function repeatChipDays() {
  return [...$("d-repeat-weekdays").querySelectorAll(".weekday-chip.on")]
    .map((c) => Number(c.dataset.day));
}

function setRepeatChipDays(days) {
  for (const chip of $("d-repeat-weekdays").querySelectorAll(".weekday-chip")) {
    const on = days.includes(Number(chip.dataset.day));
    chip.classList.toggle("on", on);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

/* Which frequency the controls currently describe. "Custom…" defers to the
 * unit dropdown; every other choice is the frequency itself. */
function repeatFreq() {
  const picked = $("d-repeat-freq").value;
  if (!picked) return "";
  return picked === "custom" ? $("d-repeat-unit").value : picked;
}

/* The rule the controls are currently describing, in the shape the API takes.
 * Null when the task doesn't repeat. */
function repeatRule() {
  const freq = repeatFreq();
  if (!freq) return null;
  const custom = $("d-repeat-freq").value === "custom";
  const rule = {
    freq,
    interval: custom ? Math.max(1, Number($("d-repeat-interval").value) || 1) : 1,
    weekdays: freq === "weekly" ? repeatChipDays() : [],
    monthly_mode: document.querySelector("input[name=d-monthly-mode]:checked")?.value
                  || "day_of_month",
    month_day: null,
    nth: null,
    weekday: null,
    time: $("d-repeat-time").value || null,
    count: null,
    until: null,
    from_completion: $("d-repeat-from-completion").checked,
  };
  if (freq === "monthly" || freq === "yearly") {
    if (rule.monthly_mode === "nth_weekday") {
      rule.nth = Number($("d-repeat-nth").value);
      rule.weekday = Number($("d-repeat-weekday").value);
    } else {
      rule.month_day = Number($("d-repeat-monthday").value);
    }
  }
  const ends = $("d-repeat-end").value;
  if (ends === "count") rule.count = Math.max(1, Number($("d-repeat-count").value) || 1);
  if (ends === "until") rule.until = $("d-repeat-until").value || null;
  return rule;
}

/* Show only the controls this frequency has anything to say about. */
function syncRepeatControls() {
  const picked = $("d-repeat-freq").value;
  const freq = repeatFreq();
  $("d-repeat-detail").hidden = !freq;
  $("d-repeat-every-row").hidden = picked !== "custom";
  $("d-repeat-unit").value = freq || "daily";
  $("d-repeat-weekdays").hidden = freq !== "weekly";
  $("d-repeat-monthly").hidden = !(freq === "monthly" || freq === "yearly");
  const ends = $("d-repeat-end").value;
  $("d-repeat-count-row").hidden = ends !== "count";
  $("d-repeat-until-row").hidden = ends !== "until";
  if (freq === "weekly" && !repeatChipDays().length) {
    // Weekly with nothing ticked repeats the day the task already falls on,
    // which is what the server would do anyway — so show it rather than
    // leaving the row looking unanswered.
    setRepeatChipDays(repeatPickedDays.length ? repeatPickedDays : [defaultRepeatDay()]);
  }
}

/* What day a fresh rule should default to: the task's own deadline if it has
 * one, otherwise today. The first occurrence of a rule set on a dated task is
 * that date, so seeding from anywhere else would put "every month on the 31st"
 * on a task due on the 1st and make the dialog contradict itself. */
function repeatSeedDate() {
  const task = findAnyTask(detailTaskId);
  const iso = task?.deadline_source === "user" ? task.deadline : null;
  return new Date(iso || Date.now());
}

function defaultRepeatDay() {
  return repeatSeedDate().getDay();
}

function onRepeatChanged() {
  syncRepeatControls();
  const days = repeatChipDays();
  if (days.length) repeatPickedDays = days;
  clearTimeout(repeatPreviewTimer);
  const rule = repeatRule();
  if (!rule) {
    $("d-repeat-preview").textContent = "";
    return;
  }
  // Debounced: typing "12" in the interval box is two keystrokes, not two
  // schedules worth asking about.
  repeatPreviewTimer = setTimeout(() => previewRepeat(rule), 250);
}

async function previewRepeat(rule) {
  const forTask = detailTaskId;
  try {
    const body = await api("/recurring/preview", {
      method: "POST", body: JSON.stringify({ ...rule, task_id: forTask }),
    });
    if (detailTaskId !== forTask) return;   // the dialog moved on
    const dates = (body.occurrences || []).map((iso) =>
      new Date(iso).toLocaleString(undefined,
        { weekday: "short", month: "short", day: "numeric",
          hour: "2-digit", minute: "2-digit" }));
    $("d-repeat-preview").textContent = dates.length
      ? `${body.summary} — next: ${dates.join(" · ")}`
      : body.summary;
  } catch (e) {
    $("d-repeat-preview").textContent = e.message;
  }
}

/* Put a task's saved rule (if any) back into the controls. */
function loadRepeat(task) {
  fillRepeatOptions();
  const rule = task?.recurrence?.active ? task.recurrence.rule : null;
  repeatPickedDays = [];
  if (!rule) {
    $("d-repeat-freq").value = "";
    $("d-repeat-interval").value = 1;
    $("d-repeat-time").value = "";
    $("d-repeat-end").value = "";
    $("d-repeat-count").value = 10;
    $("d-repeat-until").value = "";
    $("d-repeat-from-completion").checked = false;
    const seed = repeatSeedDate();
    $("d-repeat-monthday").value = String(seed.getDate());
    $("d-repeat-nth").value = String(Math.floor((seed.getDate() - 1) / 7) + 1);
    $("d-repeat-weekday").value = String(seed.getDay());
    document.querySelector("input[name=d-monthly-mode][value=day_of_month]").checked = true;
    setRepeatChipDays([]);
    $("d-repeat-preview").textContent = "";
    syncRepeatControls();
    return;
  }
  // Anything with a knob turned up is shown as Custom, so the dialog never
  // presents a rule it couldn't reproduce: "Weekly" with an interval of 3
  // would be a lie about what is stored.
  const plain = rule.interval === 1;
  $("d-repeat-freq").value = plain ? rule.freq : "custom";
  $("d-repeat-unit").value = rule.freq;
  $("d-repeat-interval").value = rule.interval;
  setRepeatChipDays(rule.weekdays || []);
  repeatPickedDays = rule.weekdays || [];
  document.querySelector(
    `input[name=d-monthly-mode][value=${rule.monthly_mode || "day_of_month"}]`
  ).checked = true;
  $("d-repeat-monthday").value = String(rule.month_day ?? repeatSeedDate().getDate());
  $("d-repeat-nth").value = String(rule.nth ?? 1);
  $("d-repeat-weekday").value = String(rule.weekday ?? defaultRepeatDay());
  $("d-repeat-time").value = rule.time || "";
  $("d-repeat-end").value = rule.count ? "count" : (rule.until ? "until" : "");
  $("d-repeat-count").value = rule.count || 10;
  $("d-repeat-until").value = rule.until || "";
  $("d-repeat-from-completion").checked = !!rule.from_completion;
  syncRepeatControls();
  $("d-repeat-preview").textContent = task.recurrence.summary;
  onRepeatChanged();
}

/* Save whatever the repeat controls now say, if it differs from what the task
 * already had. Returns the state the server sent back, or null for "nothing to
 * do" — `saveDetail` needs to know which, because it applies whichever reply
 * came last. */
async function saveRepeat(taskId, hadRule) {
  const rule = repeatRule();
  if (!rule && !hadRule) return null;
  if (!rule) {
    const state = await api(`/tasks/${taskId}/repeat`, { method: "DELETE" });
    toast("This no longer repeats — the task itself is untouched.");
    return state;
  }
  if (hadRule && JSON.stringify(normalizedForCompare(hadRule)) ===
                 JSON.stringify(normalizedForCompare(rule))) {
    return null;   // the controls say exactly what is already stored
  }
  const state = await api(`/tasks/${taskId}/repeat`, {
    method: "PUT", body: JSON.stringify(rule),
  });
  return state;
}

/* Rules come back from the server carrying every field, including the ones
 * this frequency ignores; the controls only fill in the ones that matter. Line
 * them up before comparing so re-saving a dialog you only opened doesn't count
 * as a change and quietly re-time the series. */
function normalizedForCompare(rule) {
  const freq = rule.freq;
  const monthly = freq === "monthly" || freq === "yearly";
  const nth = monthly && rule.monthly_mode === "nth_weekday";
  return {
    freq,
    interval: rule.interval || 1,
    weekdays: freq === "weekly" ? [...(rule.weekdays || [])].sort() : [],
    monthly_mode: monthly ? (rule.monthly_mode || "day_of_month") : "day_of_month",
    month_day: monthly && !nth ? (rule.month_day ?? null) : null,
    nth: nth ? (rule.nth ?? null) : null,
    weekday: nth ? (rule.weekday ?? null) : null,
    time: rule.time || null,
    count: rule.count || null,
    until: rule.until || null,
    from_completion: !!rule.from_completion,
  };
}

/* ---------------- thankless modal ---------------- */

function maybeShowThankless() {
  if (thanklessShowing || !thanklessQueue.length) return;
  const id = thanklessQueue.shift();
  const task = findTask(id);
  if (!task || task.quadrant !== "thankless" || task.ack_thankless) {
    maybeShowThankless();
    return;
  }
  thanklessShowing = id;
  $("t-title").textContent = task.title;
  $("t-scores").textContent = `impact ${task.impact}/10, effort ${task.effort}/10`;
  $("modal-thankless").showModal();
}

/* ---------------- braindump ---------------- */

async function compileBraindump() {
  const text = $("b-text").value.trim();
  if (!text) return;
  const btn = $("b-compile");
  btn.disabled = true;
  $("b-status").textContent = "Compiling… (the deep model is thinking, this can take a moment)";
  try {
    applyState(await api("/compile", { method: "POST", body: JSON.stringify({ text }) }));
    $("b-text").value = "";
    $("modal-braindump").close();
    toast("Braindump compiled into tasks ✓");
  } catch (e) {
    $("b-status").textContent = "";
    toast(e.message, true);
  }
  btn.disabled = false;
  $("b-status").textContent = "";
}

/* ---------------- settings ---------------- */

/* The hours of the day, as options for "the working day starts at". Built
 * rather than written out so they read in the reader's own locale — 9 AM or
 * 09:00, whichever their clock uses. */
function fillDayStarts() {
  const select = $("s-day-start");
  if (select.options.length) return;
  for (let h = 0; h < 24; h++) {
    const opt = document.createElement("option");
    opt.value = String(h);
    opt.textContent = new Date(2000, 0, 1, h)
      .toLocaleTimeString(undefined, { hour: "numeric" });
    select.appendChild(opt);
  }
}

/* What the cap has actually learned to be, in a sentence. A setting that
 * quietly overrides itself is worse than no setting, so the dialog says what
 * the number has moved to and on what evidence. */
function capacityLearned() {
  const c = settings.capacity;
  if (!c || !c.adaptive) return "";
  if (c.days < 5) {
    return `Nothing learned yet — ${c.days} day${c.days === 1 ? "" : "s"} of ` +
      `finished work so far, and it wants five before it says anything.`;
  }
  const pct = Math.round((c.hit_rate ?? 0) * 100);
  if (!c.learned) {
    return `You reach ${fmtMinutes(c.base)} on ${pct}% of the ${c.days} days ` +
      `you finished anything on, which is often enough for it to mean ` +
      `something. Leaving it where you put it.`;
  }
  return `You reach ${fmtMinutes(c.base)} on ${pct}% of the ${c.days} days ` +
    `you finished anything on (a typical one is ${fmtMinutes(c.typical)}), so ` +
    `the app is working to ${fmtMinutes(c.minutes)} instead.`;
}

/* The server stores instants and has no timezone of its own, but "which day is
 * this due on" and "eight hours of one" are both local questions. The page is
 * the only side that knows the answer, so it says so once — and only when
 * nothing is stored, so a zone set by hand is never overwritten. */
async function reportTimezone() {
  try {
    if (settings.timezone) return;
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone) return;
    settings = await api("/settings",
      { method: "PUT", body: JSON.stringify({ timezone: zone }) });
  } catch { /* a plan an hour out of place beats no plan at all */ }
}

function openSettings() {
  $("s-auto-deadlines").checked = settings.auto_deadlines;
  $("s-buffer").value = Math.round(settings.buffer * 100);
  $("s-buffer-val").textContent = Math.round(settings.buffer * 100) + "%";
  $("s-adaptive").checked = settings.adaptive_buffer;
  fillDayStarts();
  $("s-capacity").value = (settings.day_capacity ?? 480) / 60;
  $("s-capacity-val").textContent = fmtMinutes(settings.day_capacity ?? 480);
  $("s-adaptive-capacity").checked = settings.adaptive_capacity !== false;
  $("s-day-start").value = String(settings.day_start ?? 9);
  $("s-spread").checked = settings.spread_tasks !== false;
  $("s-capacity-note").textContent = capacityLearned();
  $("s-threshold").value = settings.matrix_threshold;
  $("s-threshold-val").textContent = settings.matrix_threshold;
  $("s-ai-scoring").checked = settings.ai_scoring;
  $("s-ai-start-times").checked = settings.ai_start_times;
  $("s-title-parsing").checked = settings.title_parsing !== false;
  $("s-title-parsing-ai").checked = settings.title_parsing_ai !== false;
  $("s-alarms").checked = settings.alarms.enabled;
  $("s-stop-lead").value = settings.alarms.stop_lead;
  $("s-ready-lead").value = settings.alarms.ready_lead;
  $("s-go-lead").value = settings.alarms.go_lead;
  $("s-timer-style").value = settings.timer_style;
  $("s-week-start").value = String(settings.week_start ?? 0);
  $("s-granularity").value = settings.granularity;
  $("s-granularity-val").textContent = settings.granularity;
  $("s-gamification").checked = settings.gamification;
  $("s-sound").checked = soundOn;
  $("s-volume").value = Math.round(soundVolume * 100);
  $("s-volume-val").textContent = Math.round(soundVolume * 100) + "%";
  renderSoundSlots();
  $("s-manual-order").checked = sortMode().field === "manual";
  $("s-budget").value = settings.daily_budget_usd || "";
  const spend = settings.spend;
  $("s-spend-status").textContent = spend.budget
    ? `${money(spend.today)} of ${money(spend.budget)} today`
    : `${money(spend.today)} today`;
  $("s-api-key").value = "";
  $("s-key-status").textContent = settings.has_api_key ? "configured ✓" : "not set";
  // Not a secret, unlike the key — safe to show so it can be edited/cleared.
  $("s-workspace-id").value = settings.workspace_id || "";
  $("modal-settings").showModal();
}

async function saveSettings() {
  const body = {
    auto_deadlines: $("s-auto-deadlines").checked,
    buffer: Number($("s-buffer").value) / 100,
    adaptive_buffer: $("s-adaptive").checked,
    day_capacity: Math.round(Number($("s-capacity").value) * 60),
    adaptive_capacity: $("s-adaptive-capacity").checked,
    day_start: Number($("s-day-start").value),
    spread_tasks: $("s-spread").checked,
    matrix_threshold: Number($("s-threshold").value),
    ai_scoring: $("s-ai-scoring").checked,
    ai_start_times: $("s-ai-start-times").checked,
    title_parsing: $("s-title-parsing").checked,
    title_parsing_ai: $("s-title-parsing-ai").checked,
    alarms: {
      enabled: $("s-alarms").checked,
      stop_lead: Number($("s-stop-lead").value),
      ready_lead: Number($("s-ready-lead").value),
      go_lead: Number($("s-go-lead").value),
    },
    timer_style: $("s-timer-style").value,
    week_start: Number($("s-week-start").value),
    granularity: Number($("s-granularity").value),
    gamification: $("s-gamification").checked,
    manual_order: $("s-manual-order").checked,
    daily_budget_usd: Math.max(0, Number($("s-budget").value) || 0),
    workspace_id: $("s-workspace-id").value.trim(),
  };
  // Sound is device-local, so it is saved here and now rather than riding
  // along with the request — it must stick even if the server call fails.
  soundOn = $("s-sound").checked;
  soundVolume = Number($("s-volume").value) / 100;
  saveSoundPrefs();

  const key = $("s-api-key").value.trim();
  if (key) body.api_key = key;
  try {
    settings = await api("/settings", { method: "PUT", body: JSON.stringify(body) });
    $("modal-settings").close();
    $("add-granularity").value = settings.granularity;
    applyState(await api("/state"));
    toast("Settings saved ✓");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- sounds ----------------
 *
 * Whether sound is on, how loud it is, and which files stand in for the
 * built-in set all live in this browser rather than on the server. That is a
 * deliberate split from every other setting in the app: the audio files
 * themselves are held in IndexedDB because uploading a few hundred KB of
 * personal taste would mean an endpoint, a quota and a migration for something
 * nobody would want synced anyway — and once the files are per-device, having
 * the volume follow you to a machine that has none of them would be worse than
 * not syncing at all. So the whole sound config travels together, or not at all.
 */

const SOUND_KEY = "adderall.sound";

let soundOn = true;
let soundVolume = 0.55;

function loadSoundPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(SOUND_KEY) || "{}");
    if (typeof saved.on === "boolean") soundOn = saved.on;
    if (typeof saved.volume === "number") soundVolume = saved.volume;
  } catch { /* first run, or storage the browser will not give us */ }
  applySoundPrefs();
}

function applySoundPrefs() {
  Motion.setEnabled(soundOn);
  Motion.setVolume(soundVolume);
}

function saveSoundPrefs() {
  applySoundPrefs();
  try {
    localStorage.setItem(SOUND_KEY, JSON.stringify({ on: soundOn, volume: soundVolume }));
  } catch {}
}

/* One row per sound: what it is for, what is loaded, and the controls that
 * change it. Rebuilt rather than patched, because picking a file, clearing one
 * and failing to decode one all change the same three things. */
function renderSoundSlots() {
  const box = $("sound-slots");
  box.replaceChildren();
  for (const slot of Motion.SLOTS) {
    const row = document.createElement("div");
    row.className = "sound-slot";

    const name = document.createElement("div");
    name.className = "sound-slot-name";
    name.innerHTML = "";
    name.append(slot.label, Object.assign(document.createElement("small"),
                                          { textContent: slot.hint }));

    const own = Motion.customName(slot.id);
    const file = document.createElement("span");
    file.className = "sound-slot-file" + (own ? "" : " default");
    file.textContent = own || "built-in";
    file.title = own || "The synthesized default";

    // A hidden input behind a real button: the native file control cannot be
    // styled and reads as a foreign object dropped into the panel.
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = "audio/*";
    picker.addEventListener("change", async () => {
      const chosen = picker.files?.[0];
      if (!chosen) return;
      try {
        await Motion.setCustom(slot.id, chosen);
        Motion.play(slot.id);
        renderSoundSlots();
      } catch (err) {
        toast(`Couldn't use that file: ${err.message || "this browser can't play it."}`, true);
      }
    });

    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "ghost";
    choose.textContent = own ? "Change…" : "Choose…";
    choose.addEventListener("click", () => picker.click());

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "ghost no-click";   // it plays the slot, not the click
    preview.textContent = "▶";
    preview.title = "Hear it";
    preview.addEventListener("click", () => Motion.play(slot.id));

    row.append(name, file, picker, choose, preview);
    if (own) {
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "ghost";
      reset.textContent = "✕";
      reset.title = "Back to the built-in sound";
      reset.addEventListener("click", async () => {
        await Motion.clearCustom(slot.id);
        renderSoundSlots();
      });
      row.appendChild(reset);
    }
    box.appendChild(row);
  }
}

let audioCtx = null;
function beepPattern(freqs, dur = 0.18, gap = 0.12) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    let t = audioCtx.currentTime;
    for (const f of freqs) {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = f;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t); osc.stop(t + dur + 0.02);
      t += dur + gap;
    }
  } catch {}
}
/* The three transition cues stay distinct from each other — the whole point
 * of "stop / ready / go" is that you can tell which one fired without looking
 * — so a custom alarm file replaces all three rather than one of them. One
 * chosen sound that means "a deadline moved" beats three that are the same
 * sound and therefore mean nothing. */
const alarmCue = (pattern) => () => {
  if (!soundOn) return;
  if (Motion.customName("alarm")) Motion.play("alarm");
  else pattern();
};
const SOUNDS = {
  stop:  alarmCue(() => beepPattern([440, 440])),               // gentle double
  ready: alarmCue(() => beepPattern([554, 659, 554])),          // rising triple
  go:    alarmCue(() => beepPattern([880, 880, 880, 880], 0.3, 0.15)), // insistent
  wrap:  alarmCue(() => beepPattern([494, 392])),               // falling pair
};

/* ---------------- deadline transition alarms ----------------
 * Staged cues for tasks with deadlines: stop current activity → get ready →
 * go. Checked every 30s while the page is open; each cue fires once. */

const firedAlarms = new Set();

function scheduleTransitionAlarms() { /* recomputed on every check tick */ }

function checkTransitionAlarms() {
  if (!settings?.alarms?.enabled) return;
  const now = Date.now();
  const stages = [
    { key: "stop", lead: settings.alarms.stop_lead, text: (t) => `⏸ Stop what you're doing — “${t.title}” is coming up` },
    { key: "ready", lead: settings.alarms.ready_lead, text: (t) => `🧦 Get ready: “${t.title}”` },
    { key: "go", lead: settings.alarms.go_lead, text: (t) => `🚀 Time for “${t.title}” — go now` },
  ];
  // Deadlines come from every project, not just the open tab: a cue you
  // miss because its task is one tab over is the whole failure mode this
  // app is built to avoid. The banner says which project when it isn't
  // the one on screen.
  for (const t of state.alarm_tasks || []) {
    if (!t.deadline) continue;
    const due = new Date(t.deadline).getTime();
    for (const stage of stages) {
      const fireAt = due - stage.lead * 60000;
      const id = `${t.id}:${stage.key}`;
      if (firedAlarms.has(id)) continue;
      // Fire if we're within the window (up to 2 min late) — not for
      // deadlines that were already long past when the page opened.
      if (now >= fireAt && now - fireAt < 2 * 60000) {
        firedAlarms.add(id);
        const where = t.project_id && t.project_id !== state.active_project_id
          ? ` · in ${t.project_name}` : "";
        SOUNDS[stage.key]();
        showAlarmBanner(stage.text(t) + where);
        notify(stage.text(t) + where);
      } else if (now - fireAt >= 2 * 60000) {
        firedAlarms.add(id); // silently expire stale cues
      }
    }
  }
}

function showAlarmBanner(text) {
  const banner = $("alarm-banner");
  banner.replaceChildren();
  const span = document.createElement("span");
  span.textContent = text;
  const btn = document.createElement("button");
  btn.textContent = "OK";
  btn.addEventListener("click", () => { banner.hidden = true; });
  banner.append(span, btn);
  banner.hidden = false;
}

function notify(text) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") new Notification("adderall", { body: text });
}

setInterval(checkTransitionAlarms, 30000);

/* ---------------- Taskmaster focus mode ----------------
 * A focus session outlives the overlay. Closing the overlay only minimizes
 * it: the timer keeps running in the background, so you can duck out to add
 * a task and come back to the same countdown — or reset it deliberately.
 * The session is mirrored into localStorage, so a reload or a crash resumes
 * it too. Remaining time is derived from wall-clock stamps rather than a
 * decrementing counter, so a throttled background tab cannot lose minutes.
 *
 * Within a session the task tree is walked depth-first (see
 * logic.focus_queue): the subtasks of a task come before the task itself,
 * so a session drills down to the smallest first step and surfaces back up.
 */

const FOCUS_KEY = "adderall.focus.v1";
const FOCUS_PERSIST = [
  "active", "projectId", "rootId", "rootTitle", "taskId", "taskTitle",
  "taskDescription", "path", "stepsLeft",
  "estimateMin", "totalSec", "startedAt", "pausedAccum", "pauseStart", "paused",
  "skipped", "cuesFired",
];

const focus = {
  active: false,      // a session exists — running even while the overlay is closed
  open: false,        // overlay visible
  advancing: false,   // guards against re-entrant queue walks
  projectId: null,    // the tab this session belongs to
  rootId: null,
  rootTitle: "",
  taskId: null,
  taskTitle: "",
  taskDescription: "",
  path: [],
  stepsLeft: 0,
  nextTitle: "",
  estimateMin: null,
  totalSec: 0,
  startedAt: 0,       // epoch ms this task's timer started
  pausedAccum: 0,     // ms spent paused
  pauseStart: 0,
  paused: false,
  skipped: [],
  cuesFired: [],
  timer: null,
};

function saveFocus() {
  try {
    if (!focus.active) localStorage.removeItem(FOCUS_KEY);
    else localStorage.setItem(FOCUS_KEY, JSON.stringify(
      Object.fromEntries(FOCUS_PERSIST.map((k) => [k, focus[k]]))));
  } catch {}
}

function restoreFocus() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(FOCUS_KEY) || "null"); } catch {}
  if (!saved || !saved.active || !saved.taskId) return;
  Object.assign(focus, saved);
  focus.open = false;
  focus.advancing = false;
  ensureFocusTimer();
  renderFocusTask();
  renderFocusPill();
}

/* Elapsed/remaining come from timestamps, never from a tick counter, so the
 * clock stays honest across minimized overlays, hidden tabs and reloads. */
function focusElapsedSec() {
  if (!focus.startedAt) return 0;
  let paused = focus.pausedAccum;
  if (focus.paused && focus.pauseStart) paused += Date.now() - focus.pauseStart;
  return Math.max(0, (Date.now() - focus.startedAt - paused) / 1000);
}

function focusRemainingSec() {
  return focus.totalSec - focusElapsedSec();
}

function ensureFocusTimer() {
  if (!focus.timer) focus.timer = setInterval(focusTick, 1000);
}

/* ---- session lifecycle ---- */

async function startFocus(taskId) {
  let data;
  try {
    data = await api("/focus" + (taskId ? `?root=${encodeURIComponent(taskId)}` : ""));
  } catch (e) { toast(e.message, true); return; }
  if (!data.queue.length) { toast("Nothing to focus on — add a task first."); return; }
  focus.active = true;
  focus.projectId = data.project_id || state.active_project_id;
  focus.rootId = data.root_id;
  focus.rootTitle = data.root_title || "";
  focus.skipped = [];
  focusOn(data.queue[0], data, true);
  openFocusOverlay();
}

/* Move to the next step of the walk. The queue is re-fetched every time, so
 * subtasks added mid-session slot straight into the traversal. */
async function advanceFocus() {
  if (focus.advancing) return false;
  focus.advancing = true;
  try {
    let data = focus.rootId
      ? await api(`/focus?root=${encodeURIComponent(focus.rootId)}`).catch(() => null)
      : null;
    let item = pickFromQueue(data);
    if (!item) {
      // This tree is finished (or entirely skipped) — roll into the next one.
      data = await api("/focus").catch(() => null);
      if (data && data.root_id && data.root_id !== focus.rootId) {
        focus.rootId = data.root_id;
        focus.rootTitle = data.root_title || "";
        focus.projectId = data.project_id || focus.projectId;
        focus.skipped = [];
      }
      item = pickFromQueue(data);
    }
    if (!item) {
      const wasOpen = focus.open;
      endFocus();
      if (wasOpen) toast("Nothing left to focus on — that's the lot ✓");
      return false;
    }
    focusOn(item, data, true);
    return true;
  } finally {
    focus.advancing = false;
  }
}

function pickFromQueue(data) {
  if (!data || !data.queue || !data.queue.length) return null;
  return data.queue.find((t) => !focus.skipped.includes(t.id)) || null;
}

function focusOn(item, data, resetTimer) {
  const queue = (data && data.queue) || [];
  const idx = queue.findIndex((t) => t.id === item.id);
  focus.taskId = item.id;
  focus.taskTitle = item.title;
  focus.taskDescription = item.description || "";
  focus.projectId = (data && data.project_id) || item.project_id || focus.projectId;
  focus.path = item.path || [];
  focus.stepsLeft = queue.length - (idx < 0 ? 0 : idx);
  focus.nextTitle = idx >= 0 && queue[idx + 1] ? queue[idx + 1].title : "";
  focus.estimateMin = item.buffered_estimate ?? null;
  if (resetTimer) resetFocusTimer();
  renderFocusTask();
  ensureFocusTimer();
  saveFocus();
  api(`/tasks/${item.id}/start`, { method: "POST" }).then(applyState).catch(() => {});
}

function resetFocusTimer() {
  focus.totalSec = Math.max(60, (focus.estimateMin ?? 25) * 60);
  focus.startedAt = Date.now();
  focus.pausedAccum = 0;
  focus.pauseStart = 0;
  focus.paused = false;
  focus.cuesFired = [];
  $("f-pause").textContent = "⏸ Pause";
  $("f-stage").textContent = "";
  drawFocusTimers();
  renderFocusPill();
  saveFocus();
}

function openFocusOverlay() {
  if (!focus.active) return;
  focus.open = true;
  const style = settings?.timer_style || "both";
  $("f-dial").style.display = style === "block" ? "none" : "";
  $("f-block").parentElement.style.display = style === "analog" ? "none" : "";
  $("f-pause").textContent = focus.paused ? "▶ Resume" : "⏸ Pause";
  $("focus-overlay").hidden = false;
  renderFocusTask();
  drawFocusTimers();
  renderFocusPill();
}

/* Close the overlay, keep the session: the whole point is that stepping out
 * to jot down a task doesn't cost you the timer. */
function minimizeFocus() {
  if (!focus.active) return;
  focus.open = false;
  $("focus-overlay").hidden = true;
  renderFocusPill();
  saveFocus();
  toast("Still running in the background — tap the ▶ pill to come back.");
}

function endFocus() {
  clearInterval(focus.timer);
  focus.timer = null;
  focus.active = false;
  focus.open = false;
  focus.taskId = null;
  focus.rootId = null;
  focus.projectId = null;
  focus.skipped = [];
  $("focus-overlay").hidden = true;
  renderFocusPill();
  saveFocus();
}

/* If the current task gets completed or deleted from the list while the
 * overlay is closed, the session walks on by itself. */
function syncFocusWithState() {
  if (!focus.active || focus.advancing) return;
  // A session running in another tab is not missing, it is simply off
  // screen — the list we just rendered says nothing about it either way.
  if (focus.projectId && state.active_project_id &&
      focus.projectId !== state.active_project_id) {
    renderFocusPill();
    return;
  }
  const task = focus.taskId ? findTask(focus.taskId) : null;
  if (!task || !(task.status === "todo" || task.status === "in_progress")) {
    advanceFocus();
    return;
  }
  if (task.title !== focus.taskTitle) {
    focus.taskTitle = task.title;
    renderFocusTask();
  }
  renderFocusPill();
}

/* ---- rendering ---- */

function renderFocusTask() {
  const task = focus.taskId ? findTask(focus.taskId) : null;
  $("f-title").textContent = focus.taskTitle || "";
  $("f-description").textContent = task?.description || focus.taskDescription || "";
  $("f-path").textContent = focus.path.length ? focus.path.join(" › ") : "";
  const scope = focus.rootTitle && focus.path.length ? ` in “${focus.rootTitle}”` : "";
  $("f-progress").textContent = focus.stepsLeft > 1
    ? `${focus.stepsLeft} steps left${scope}`
    : focus.stepsLeft === 1 ? `last step${scope}` : "";
  $("f-next-hint").textContent = focus.nextTitle ? `after this: ${focus.nextTitle}` : "";
}

function renderFocusPill() {
  const pill = $("focus-pill");
  if (!pill) return;
  if (!focus.active || focus.open) { pill.hidden = true; return; }
  const rem = focusRemainingSec();
  $("focus-pill-time").textContent = focus.paused
    ? "paused"
    : rem < 0 ? "+" + fmtMinutes(Math.ceil(-rem / 60)) + " over"
              : fmtMinutes(Math.ceil(rem / 60)) + " left";
  $("focus-pill-title").textContent = focus.taskTitle || "";
  pill.classList.toggle("over", rem < 0 && !focus.paused);
  pill.hidden = false;
}

function focusTick() {
  if (!focus.active) return;
  if (!focus.paused) fireFocusCues();
  if (focus.open) drawFocusTimers();
  else renderFocusPill();
}

function drawFocusTimers() {
  const total = focus.totalSec || 1;
  const remainingSec = focusRemainingSec();
  const remaining = Math.max(0, remainingSec);
  const elapsed = Math.min(total, focusElapsedSec());
  const frac = Math.max(0, Math.min(1, remaining / total));

  // Depleting color block
  const fill = $("f-block-fill");
  fill.style.width = (frac * 100).toFixed(2) + "%";
  fill.style.background = frac > 0.4 ? "var(--good)" : frac > 0.15 ? "var(--warn)" : "var(--bad)";

  $("f-elapsed").textContent = fmtMinutes(Math.floor(elapsed / 60));
  $("f-remaining").textContent = remainingSec < 0
    ? "-" + fmtMinutes(Math.ceil(-remainingSec / 60))
    : fmtMinutes(Math.ceil(remaining / 60));

  drawDial(frac, remaining, remainingSec < 0);
  renderFocusPill();
}

/* Analog dial: outer Time-Timer-style depleting wedge + a real analog clock
 * with moving hands in the center, so time reads as position and distance. */
function drawDial(frac, remainingSec, overTime) {
  const canvas = $("f-dial");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2;
  const R = W / 2 - 6;
  ctx.clearRect(0, 0, W, H);

  const css = getComputedStyle(document.documentElement);
  const col = (name) => css.getPropertyValue(name).trim();

  // dial background
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = col("--panel");
  ctx.fill();
  ctx.strokeStyle = col("--border");
  ctx.lineWidth = 2;
  ctx.stroke();

  // depleting wedge (from 12 o'clock, clockwise), color shifts as it shrinks
  if (frac > 0) {
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    const start = -Math.PI / 2;
    ctx.arc(cx, cy, R - 4, start, start + Math.PI * 2 * frac, false);
    ctx.closePath();
    ctx.fillStyle = frac > 0.4 ? col("--good") : frac > 0.15 ? col("--warn") : col("--bad");
    ctx.globalAlpha = 0.75;
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  // minute ticks
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * Math.PI * 2 - Math.PI / 2;
    const len = i % 5 === 0 ? 10 : 4;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (R - len), cy + Math.sin(a) * (R - len));
    ctx.lineTo(cx + Math.cos(a) * (R - 1), cy + Math.sin(a) * (R - 1));
    ctx.strokeStyle = col("--muted");
    ctx.lineWidth = i % 5 === 0 ? 2 : 1;
    ctx.stroke();
  }

  // real analog clock in the middle
  const now = new Date();
  const rInner = R * 0.45;
  ctx.beginPath();
  ctx.arc(cx, cy, rInner, 0, Math.PI * 2);
  ctx.fillStyle = col("--bg");
  ctx.fill();
  ctx.strokeStyle = col("--border");
  ctx.stroke();
  const hourA = ((now.getHours() % 12) + now.getMinutes() / 60) / 12 * Math.PI * 2 - Math.PI / 2;
  const minA = (now.getMinutes() + now.getSeconds() / 60) / 60 * Math.PI * 2 - Math.PI / 2;
  const hand = (angle, len, width, color) => {
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(angle) * len, cy + Math.sin(angle) * len);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.stroke();
  };
  hand(hourA, rInner * 0.55, 4, col("--text"));
  hand(minA, rInner * 0.85, 2.5, col("--accent"));
  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fillStyle = col("--text");
  ctx.fill();

  // remaining time, big, under the inner clock
  ctx.fillStyle = col("--text");
  ctx.font = "600 15px system-ui";
  ctx.textAlign = "center";
  const mins = Math.ceil(Math.max(0, remainingSec) / 60);
  ctx.fillText(overTime ? "over time" : `${mins} min left`, cx, cy + R * 0.72);
}

/* Staged cues inside a focus session: wrap-up near the end, then time-up. */
function fireFocusCues() {
  const alarms = settings?.alarms || {};
  if (alarms.enabled === false) return;
  const remainingMin = focusRemainingSec() / 60;
  const cues = [
    { key: "wrap", at: Math.min(alarms.stop_lead ?? 30, focus.totalSec / 60 * 0.2),
      label: "🌗 Start wrapping up", sound: SOUNDS.wrap },
    { key: "ready", at: Math.min(alarms.ready_lead ?? 10, focus.totalSec / 60 * 0.08),
      label: "🌘 Almost there — find a stopping point", sound: SOUNDS.ready },
    { key: "go", at: 0, label: "⏰ Time — transition now", sound: SOUNDS.go },
  ];
  for (const cue of cues) {
    if (remainingMin <= cue.at && !focus.cuesFired.includes(cue.key)) {
      focus.cuesFired.push(cue.key);
      saveFocus();
      cue.sound();
      $("f-stage").textContent = cue.label;
      notify(cue.label + " — " + focus.taskTitle);
    }
  }
}

async function finishFocus() {
  const id = focus.taskId;
  if (!id) return;
  const actual = Math.max(1, Math.round(focusElapsedSec() / 60));
  focus.advancing = true;          // completing re-renders state; don't double-walk
  try { await completeTask(id, actual); } finally { focus.advancing = false; }
  // Roll straight into the next step of the walk — keep the momentum.
  await advanceFocus();
}

async function skipFocus() {
  const skipped = focus.taskId;
  if (skipped) {
    focus.skipped.push(skipped);
    await patchTask(skipped, { status: "todo" });
  }
  if (!(await advanceFocus()) && skipped) toast("That was the only task left.");
}

/* ---------------- wiring ---------------- */

function wire() {
  $("add-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const title = $("add-title").value.trim();
    if (!title) return;
    $("add-title").value = "";
    addTask(title);
  });

  wireListDropTarget();
  wireTabStripDropTarget();
  wireCalendar();

  $("btn-add-project").addEventListener("click", addProject);

  // The sorter. Switching field takes that field's natural direction with it —
  // picking "Deadline" and being shown the furthest-off task first would be an
  // answer to a question nobody asked — and the button flips it from there.
  $("sort-field").addEventListener("change", () => {
    const field = $("sort-field").value;
    setSort(field, DEFAULT_SORT_DIR[field] || "desc");
  });
  $("sort-dir").addEventListener("click", () => {
    const { field, dir } = sortMode();
    setSort(field, dir === "asc" ? "desc" : "asc");
  });

  // Alt+1…9 jumps straight to a tab. Ctrl/Cmd+number belongs to the browser's
  // own tabs, so it stays out of the way. Read off the physical key, because
  // Alt+1 types a symbol rather than a digit on several layouts — and for
  // that same reason it is left alone while you are typing in a field.
  document.addEventListener("keydown", (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target?.closest?.("input, textarea, select")) return;
    const digit = /^Digit([1-9])$/.exec(e.code || "");
    const n = digit ? Number(digit[1]) : Number(e.key);
    if (!Number.isInteger(n) || n < 1 || n > 9) return;
    if (!(state.projects || [])[n - 1]) return;
    e.preventDefault();
    switchToProjectAt(n - 1);
  });

  $("btn-braindump").addEventListener("click", () => $("modal-braindump").showModal());
  $("b-compile").addEventListener("click", compileBraindump);

  $("btn-focus").addEventListener("click", () => startFocus(null));
  $("btn-settings").addEventListener("click", openSettings);
  $("s-save").addEventListener("click", saveSettings);
  $("s-buffer").addEventListener("input", () =>
    $("s-buffer-val").textContent = $("s-buffer").value + "%");
  $("s-capacity").addEventListener("input", () =>
    $("s-capacity-val").textContent =
      fmtMinutes(Math.round(Number($("s-capacity").value) * 60)));
  $("s-threshold").addEventListener("input", () =>
    $("s-threshold-val").textContent = $("s-threshold").value);
  $("s-granularity").addEventListener("input", () =>
    $("s-granularity-val").textContent = $("s-granularity").value);

  // detail modal
  fillRepeatOptions();
  for (const id of ["d-repeat-freq", "d-repeat-unit", "d-repeat-interval",
                    "d-repeat-time", "d-repeat-end", "d-repeat-count",
                    "d-repeat-until", "d-repeat-monthday", "d-repeat-nth",
                    "d-repeat-weekday", "d-repeat-from-completion"]) {
    // `input` covers all of them — selects, checkboxes and text alike — so
    // one listener per control rather than a change/input pair that would
    // fire the same preview twice.
    $(id).addEventListener("input", onRepeatChanged);
  }
  for (const radio of document.querySelectorAll("input[name=d-monthly-mode]"))
    radio.addEventListener("change", onRepeatChanged);

  $("d-impact").addEventListener("input", updateDetailDerived);
  $("d-effort").addEventListener("input", updateDetailDerived);
  $("d-estimate").addEventListener("input", updateDetailDerived);
  $("d-start").addEventListener("input", updateStartNote);
  $("d-save").addEventListener("click", saveDetail);
  $("d-breakdown").addEventListener("click", async () => {
    $("modal-detail").close();
    await breakdown(detailTaskId, null);
  });
  $("d-annotate").addEventListener("click", async () => {
    const btn = $("d-annotate");
    btn.disabled = true;
    try {
      applyState(await api(`/tasks/${detailTaskId}/annotate`, { method: "POST" }));
      openDetail(detailTaskId);
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
  });
  $("d-focus").addEventListener("click", () => {
    $("modal-detail").close();
    startFocus(detailTaskId);
  });
  $("d-done").addEventListener("click", () => {
    $("modal-detail").close();
    completeTask(detailTaskId, null);
  });
  $("d-discard").addEventListener("click", () => {
    $("modal-detail").close();
    patchTask(detailTaskId, { status: "discarded" });
  });

  // thankless modal
  const closeThankless = () => { $("modal-thankless").close(); thanklessShowing = null; maybeShowThankless(); };
  $("t-discard").addEventListener("click", () => {
    patchTask(thanklessShowing, { status: "discarded", ack_thankless: true });
    closeThankless();
  });
  $("t-keep").addEventListener("click", () => {
    patchTask(thanklessShowing, { ack_thankless: true });
    closeThankless();
  });
  $("t-rescore").addEventListener("click", () => {
    const id = thanklessShowing;
    patchTask(id, { ack_thankless: true });
    closeThankless();
    openDetail(id);
  });

  // focus overlay — ✕ only minimizes; the session keeps running
  $("f-min").addEventListener("click", minimizeFocus);
  $("f-close").addEventListener("click", minimizeFocus);
  $("f-end").addEventListener("click", () => {
    endFocus();
    toast("Focus session ended.");
  });
  $("f-done").addEventListener("click", finishFocus);
  $("f-skip").addEventListener("click", skipFocus);
  $("f-pause").addEventListener("click", () => {
    focus.paused = !focus.paused;
    $("f-pause").textContent = focus.paused ? "▶ Resume" : "⏸ Pause";
    if (focus.paused) focus.pauseStart = Date.now();
    else { focus.pausedAccum += Date.now() - focus.pauseStart; focus.pauseStart = 0; }
    drawFocusTimers();
    saveFocus();
  });
  $("f-reset").addEventListener("click", () => {
    resetFocusTimer();
    toast("Timer reset to the full estimate.");
  });
  $("f-extend").addEventListener("click", () => {
    focus.totalSec += 300;
    focus.cuesFired = focus.cuesFired.filter((k) => k !== "go" && k !== "ready");
    $("f-stage").textContent = "";
    drawFocusTimers();
    saveFocus();
  });

  // minimized session pill
  $("focus-pill").addEventListener("click", openFocusOverlay);
  $("focus-pill-end").addEventListener("click", (e) => {
    e.stopPropagation();
    endFocus();
    toast("Focus session ended.");
  });

  // Escape out of the overlay leaves the timer running, like ✕ does.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && focus.open) { e.preventDefault(); minimizeFocus(); }
  });

  document.querySelectorAll("[data-close]").forEach((btn) =>
    btn.addEventListener("click", () => Motion.closeDialog(btn.closest("dialog"))));

  // Live, because a volume slider you cannot hear while dragging is a volume
  // slider you set by trial and error.
  $("s-volume").addEventListener("input", () => {
    const v = Number($("s-volume").value);
    $("s-volume-val").textContent = v + "%";
    Motion.setVolume(v / 100);
  });
  $("s-volume").addEventListener("change", () => Motion.play("click"));
  $("s-sound").addEventListener("change", () => {
    Motion.setEnabled($("s-sound").checked);
    if ($("s-sound").checked) Motion.play("complete");
  });

  // Ask for notification permission on first interaction (needed for alarms).
  document.body.addEventListener("click", () => {
    if ("Notification" in window && Notification.permission === "default")
      Notification.requestPermission();
  }, { once: true });
}

async function boot() {
  loadSoundPrefs();
  Motion.wireClicks();
  Motion.wireHover();
  // Decoding needs an AudioContext, which browsers withhold until the user has
  // done something. Not awaited: the built-in set covers every sound until the
  // custom ones are ready, so nothing is silent and nothing is blocked.
  Motion.loadCustom();
  wire();
  restoreFocus();  // a session survives reloads — pick it back up
  try {
    settings = await api("/settings");
    // Before the first state read: which day a deadline lands on, and how full
    // that day is, are worked out in this zone.
    await reportTimezone();
    $("add-granularity").value = settings.granularity;
    applyState(await api("/state"));
    // wireCalendar() has already read back which view you were last in.
    if (cal.mode) setCalendarMode(true);
    if (!settings.has_api_key)
      toast("No Anthropic API key set — AI features are off. Add one in ⚙ Settings.");
  } catch (e) {
    toast("Could not load tasks: " + e.message, true);
  }
}

boot();
