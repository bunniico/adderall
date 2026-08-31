/* Adderall front end.
 * One page, no navigation: the task list is the workspace, everything else
 * is a modal or the Taskmaster overlay. All state lives on the server; every
 * mutation round-trips immediately so nothing is ever lost on tab close. */

"use strict";

const $ = (id) => document.getElementById(id);

let state = { tasks: [], next_task_id: null, projects: [], active_project_id: null,
              alarm_tasks: [] };
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

function fmtDeadline(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  const now = new Date();
  const diffMin = (d - now) / 60000;
  const opts = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  const label = d.toLocaleString(undefined, opts);
  if (diffMin < 0)
    return { label: `overdue · ${label}`, cls: "urgent", overdue: true };
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

function twisty(task, count, collapsed) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "twisty";
  btn.textContent = collapsed ? "▸" : "▾";
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
  render();
  toggleFocusFor = id;  // the reply rebuilds the list a second time
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
    (task.id === state.next_task_id ? " next" : "");
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
  check.addEventListener("change", () => completeTask(task.id, null));
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
    // A task that contains subtasks is worth what it holds: the estimate is
    // the sum of everything underneath, the deadline the furthest one inside.
    const est = task.has_subtasks ? task.rollup_estimate : task.buffered_estimate;
    const dlIso = task.has_subtasks ? task.rollup_deadline : task.deadline;
    const dlSrc = task.has_subtasks ? task.rollup_deadline_source : task.deadline_source;
    if (est != null) {
      const b = add(`~${fmtMinutes(est)}`, "");
      if (task.has_subtasks) b.title = "Total of all subtasks";
    }
    const dl = fmtDeadline(dlIso);
    // A deadline that has gone by is the one badge worth making clickable:
    // the thing you want the second you read it is to move it, and the
    // calendar is a detour for that. Same dialog, same "keeps its length"
    // promise — just reachable from where the task actually lives.
    if (dl && dl.overdue) badges.appendChild(nudgeBadge(task, dl, dlSrc));
    else if (dl) add(dl.label + (dlSrc === "auto" ? " (auto)" : ""), dl.cls);
    if (task.quadrant) add(QUAD_LABEL[task.quadrant], "quad-" + task.quadrant);
    if (collapsed)
      add(`${subs.length} subtask${subs.length === 1 ? "" : "s"} hidden`, "folded");
    if (badges.children.length) el.appendChild(badges);
    // The progress bar stays out in the open when a task is folded: a rolled
    // up "40m left · 60%" is the whole point of hiding the steps.
    if (task.has_subtasks) el.appendChild(progressBar(task));
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

/* The overdue badge, as a button. Clicking it opens the nudge dialog on this
 * one task; the calendar's rail is where you move the whole pile at once. */
function nudgeBadge(task, dl, dlSrc) {
  const btn = document.createElement("button");
  btn.className = "badge urgent nudge-badge";
  btn.type = "button";
  btn.textContent = dl.label + (dlSrc === "auto" ? " (auto)" : "") + " ⏩";
  btn.title = `Nudge “${task.title}” to a new deadline — it keeps its ` +
    `length (${fmtMinutes(task.length_min)} of buffered work)` +
    (task.has_subtasks ? ", and its subtasks slide with it" : "");
  btn.setAttribute("aria-label", "Nudge " + task.title + " to a new deadline");
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
  if (!confirm(message + "\n\nThis can't be undone.")) return;
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

  restoreListFocus();
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
  const colors = ["#5fd39a", "#7c9cff", "#ffc86b", "#ff9c5f", "#e8eaf0"];
  for (let i = 0; i < 36; i++) {
    const c = document.createElement("div");
    c.className = "confetti";
    c.style.left = Math.random() * 100 + "vw";
    c.style.background = colors[i % colors.length];
    c.style.animationDelay = Math.random() * 0.4 + "s";
    box.appendChild(c);
  }
  setTimeout(() => { box.hidden = true; box.replaceChildren(); }, 2200);
}

/* ---------------- task actions ---------------- */

async function addTask(title, parentId = null) {
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
  try {
    applyState(await api(`/tasks/${id}/complete`, {
      method: "POST",
      body: JSON.stringify({ actual_time: actualMinutes }),
    }));
    celebrate();
  } catch (e) { toast(e.message, true); }
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
  updateDetailDerived();
  $("modal-detail").showModal();
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
  const buf = task?.buffer_applied ?? (settings ? settings.buffer : 0.3);
  $("d-buffer-note").textContent = est
    ? `Buffered: ${est}m + ${Math.round(buf * 100)}% time tax = ${fmtMinutes(Math.max(1, Math.ceil(est * (1 + buf) - 1e-9)))} — the timer uses this.`
    : "No estimate yet — hit 🎲 Re-estimate or type one.";
}

async function saveDetail() {
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
  const task = findAnyTask(detailTaskId);
  const targetProject = $("d-project").value;
  const moving = !$("d-project-row").hidden && task &&
                 targetProject && targetProject !== task.project_id;
  const id = detailTaskId;
  $("modal-detail").close();
  await patchTask(id, fields);
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
  $("s-alarms").checked = settings.alarms.enabled;
  $("s-stop-lead").value = settings.alarms.stop_lead;
  $("s-ready-lead").value = settings.alarms.ready_lead;
  $("s-go-lead").value = settings.alarms.go_lead;
  $("s-timer-style").value = settings.timer_style;
  $("s-week-start").value = String(settings.week_start ?? 0);
  $("s-granularity").value = settings.granularity;
  $("s-granularity-val").textContent = settings.granularity;
  $("s-gamification").checked = settings.gamification;
  $("s-manual-order").checked = sortMode().field === "manual";
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
    workspace_id: $("s-workspace-id").value.trim(),
  };
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

/* ---------------- sounds (WebAudio, distinct per meaning) ---------------- */

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
const SOUNDS = {
  stop:  () => beepPattern([440, 440]),               // gentle double
  ready: () => beepPattern([554, 659, 554]),          // rising triple
  go:    () => beepPattern([880, 880, 880, 880], 0.3, 0.15), // insistent
  wrap:  () => beepPattern([494, 392]),               // falling pair
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
  $("d-impact").addEventListener("input", updateDetailDerived);
  $("d-effort").addEventListener("input", updateDetailDerived);
  $("d-estimate").addEventListener("input", updateDetailDerived);
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
    btn.addEventListener("click", () => btn.closest("dialog").close()));

  // Ask for notification permission on first interaction (needed for alarms).
  document.body.addEventListener("click", () => {
    if ("Notification" in window && Notification.permission === "default")
      Notification.requestPermission();
  }, { once: true });
}

async function boot() {
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
