/* Adderall front end.
 * One page, no navigation: the task list is the workspace, everything else
 * is a modal or the Taskmaster overlay. All state lives on the server; every
 * mutation round-trips immediately so nothing is ever lost on tab close. */

"use strict";

const $ = (id) => document.getElementById(id);

let state = { tasks: [], next_task_id: null };
let settings = null;
let detailTaskId = null;
const thanklessQueue = [];
let thanklessShowing = null;

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
  render();
  maybeShowThankless();
  scheduleTransitionAlarms();
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
  if (diffMin < 0) return { label: `overdue · ${label}`, cls: "urgent" };
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

function sortActive(tasks) {
  return [...tasks].sort((a, b) => {
    for (let i = 0; i < a.sort_key.length; i++) {
      if (a.sort_key[i] < b.sort_key[i]) return -1;
      if (a.sort_key[i] > b.sort_key[i]) return 1;
    }
    return 0;
  });
}

function taskNode(task, isSub) {
  const el = document.createElement("div");
  const active = task.status === "todo" || task.status === "in_progress";
  el.className = "task" + (active ? "" : " done-task") +
    (task.id === state.next_task_id ? " next" : "");
  el.dataset.id = task.id;

  const row = document.createElement("div");
  row.className = "task-row";

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
  el.appendChild(row);

  if (active) {
    const badges = document.createElement("div");
    badges.className = "task-badges";
    const add = (text, cls = "") => {
      const b = document.createElement("span");
      b.className = "badge " + cls;
      b.textContent = text;
      badges.appendChild(b);
    };
    if (task.id === state.next_task_id) add("next up", "next-badge");
    if (task.buffered_estimate != null)
      add(`~${fmtMinutes(task.buffered_estimate)}`, "");
    const dl = fmtDeadline(task.deadline);
    if (dl) add(dl.label + (task.deadline_source === "auto" ? " (auto)" : ""), dl.cls);
    if (task.quadrant) add(QUAD_LABEL[task.quadrant], "quad-" + task.quadrant);
    if (badges.children.length) el.appendChild(badges);
  }

  const subs = (task.subtasks || []).filter(
    (s) => s.status === "todo" || s.status === "in_progress");
  if (subs.length) {
    const wrap = document.createElement("div");
    wrap.className = "subtasks";
    for (const sub of subs) wrap.appendChild(taskNode(sub, true));
    el.appendChild(wrap);
  }
  return el;
}

function render() {
  const list = $("task-list");
  list.replaceChildren();
  const active = state.tasks.filter(
    (t) => t.status === "todo" || t.status === "in_progress");
  for (const t of sortActive(active)) list.appendChild(taskNode(t, false));
  $("empty-hint").hidden = active.length > 0;

  const finished = state.tasks.filter(
    (t) => t.status === "done" || t.status === "discarded");
  $("done-section").hidden = finished.length === 0;
  const doneList = $("done-list");
  doneList.replaceChildren();
  for (const t of finished) doneList.appendChild(taskNode(t, false));
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

async function addTask(title) {
  try {
    applyState(await api("/tasks", {
      method: "POST",
      body: JSON.stringify({ title }),
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

/* ---------------- detail modal ---------------- */

function isoToLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openDetail(id) {
  const task = findTask(id);
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
  const task = findTask(detailTaskId);
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
  $("modal-detail").close();
  await patchTask(detailTaskId, fields);
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

function openSettings() {
  $("s-auto-deadlines").checked = settings.auto_deadlines;
  $("s-buffer").value = Math.round(settings.buffer * 100);
  $("s-buffer-val").textContent = Math.round(settings.buffer * 100) + "%";
  $("s-adaptive").checked = settings.adaptive_buffer;
  $("s-threshold").value = settings.matrix_threshold;
  $("s-threshold-val").textContent = settings.matrix_threshold;
  $("s-ai-scoring").checked = settings.ai_scoring;
  $("s-alarms").checked = settings.alarms.enabled;
  $("s-stop-lead").value = settings.alarms.stop_lead;
  $("s-ready-lead").value = settings.alarms.ready_lead;
  $("s-go-lead").value = settings.alarms.go_lead;
  $("s-timer-style").value = settings.timer_style;
  $("s-granularity").value = settings.granularity;
  $("s-granularity-val").textContent = settings.granularity;
  $("s-gamification").checked = settings.gamification;
  $("s-api-key").value = "";
  $("s-key-status").textContent = settings.has_api_key ? "configured ✓" : "not set";
  $("modal-settings").showModal();
}

async function saveSettings() {
  const body = {
    auto_deadlines: $("s-auto-deadlines").checked,
    buffer: Number($("s-buffer").value) / 100,
    adaptive_buffer: $("s-adaptive").checked,
    matrix_threshold: Number($("s-threshold").value),
    ai_scoring: $("s-ai-scoring").checked,
    alarms: {
      enabled: $("s-alarms").checked,
      stop_lead: Number($("s-stop-lead").value),
      ready_lead: Number($("s-ready-lead").value),
      go_lead: Number($("s-go-lead").value),
    },
    timer_style: $("s-timer-style").value,
    granularity: Number($("s-granularity").value),
    gamification: $("s-gamification").checked,
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
  for (const t of flatten(state.tasks)) {
    if (!(t.status === "todo" || t.status === "in_progress") || !t.deadline) continue;
    const due = new Date(t.deadline).getTime();
    for (const stage of stages) {
      const fireAt = due - stage.lead * 60000;
      const id = `${t.id}:${stage.key}`;
      if (firedAlarms.has(id)) continue;
      // Fire if we're within the window (up to 2 min late) — not for
      // deadlines that were already long past when the page opened.
      if (now >= fireAt && now - fireAt < 2 * 60000) {
        firedAlarms.add(id);
        SOUNDS[stage.key]();
        showAlarmBanner(stage.text(t));
        notify(stage.text(t));
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

/* ---------------- Taskmaster focus mode ---------------- */

const focus = {
  taskId: null,
  totalSec: 0,
  remainingSec: 0,
  paused: false,
  timer: null,
  startedAt: null,
  pausedAccum: 0,
  cuesFired: new Set(),
};

async function startFocus(taskId) {
  let task = taskId ? findTask(taskId) : null;
  if (!task) {
    try {
      const res = await api("/next");
      task = res.task;
    } catch (e) { toast(e.message, true); return; }
  }
  if (!task) { toast("Nothing to focus on — add a task first."); return; }

  focus.taskId = task.id;
  focus.totalSec = Math.max(60, (task.buffered_estimate ?? 25) * 60);
  focus.remainingSec = focus.totalSec;
  focus.paused = false;
  focus.startedAt = Date.now();
  focus.pausedAccum = 0;
  focus.cuesFired = new Set();

  $("f-title").textContent = task.title;
  $("f-description").textContent = task.description || "";
  $("f-stage").textContent = "";
  $("f-pause").textContent = "⏸ Pause";
  const style = settings?.timer_style || "both";
  $("f-dial").style.display = style === "block" ? "none" : "";
  $("f-block").parentElement.style.display = style === "analog" ? "none" : "";
  $("focus-overlay").hidden = false;

  api(`/tasks/${task.id}/start`, { method: "POST" }).then(applyState).catch(() => {});
  updateNextHint();

  clearInterval(focus.timer);
  focus.timer = setInterval(focusTick, 1000);
  focusTick();
}

async function updateNextHint() {
  try {
    const res = await api("/next");
    $("f-next-hint").textContent =
      res.task && res.task.id !== focus.taskId ? `after this: ${res.task.title}` : "";
  } catch { $("f-next-hint").textContent = ""; }
}

function focusTick() {
  if (focus.paused) return;
  focus.remainingSec = Math.max(-3600, focus.remainingSec - 1);
  drawFocusTimers();
  fireFocusCues();
}

function drawFocusTimers() {
  const total = focus.totalSec;
  const remaining = Math.max(0, focus.remainingSec);
  const elapsed = total - Math.max(0, focus.remainingSec);
  const frac = remaining / total;

  // Depleting color block
  const fill = $("f-block-fill");
  fill.style.width = (frac * 100).toFixed(2) + "%";
  fill.style.background = frac > 0.4 ? "var(--good)" : frac > 0.15 ? "var(--warn)" : "var(--bad)";

  $("f-elapsed").textContent = fmtMinutes(Math.floor(elapsed / 60));
  $("f-remaining").textContent = focus.remainingSec < 0
    ? "-" + fmtMinutes(Math.ceil(-focus.remainingSec / 60))
    : fmtMinutes(Math.ceil(remaining / 60));

  drawDial(frac, remaining);
}

/* Analog dial: outer Time-Timer-style depleting wedge + a real analog clock
 * with moving hands in the center, so time reads as position and distance. */
function drawDial(frac, remainingSec) {
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
  ctx.fillText(focus.remainingSec < 0 ? "over time" : `${mins} min left`, cx, cy + R * 0.72);
}

/* Staged cues inside a focus session: wrap-up near the end, then time-up. */
function fireFocusCues() {
  const alarms = settings?.alarms || {};
  if (alarms.enabled === false) return;
  const remainingMin = focus.remainingSec / 60;
  const cues = [
    { key: "wrap", at: Math.min(alarms.stop_lead ?? 30, focus.totalSec / 60 * 0.2),
      label: "🌗 Start wrapping up", sound: SOUNDS.wrap },
    { key: "ready", at: Math.min(alarms.ready_lead ?? 10, focus.totalSec / 60 * 0.08),
      label: "🌘 Almost there — find a stopping point", sound: SOUNDS.ready },
    { key: "go", at: 0, label: "⏰ Time — transition now", sound: SOUNDS.go },
  ];
  for (const cue of cues) {
    if (remainingMin <= cue.at && !focus.cuesFired.has(cue.key)) {
      focus.cuesFired.add(cue.key);
      cue.sound();
      $("f-stage").textContent = cue.label;
      notify(cue.label + " — " + $("f-title").textContent);
    }
  }
}

function closeFocus() {
  clearInterval(focus.timer);
  focus.timer = null;
  focus.taskId = null;
  $("focus-overlay").hidden = true;
}

async function finishFocus() {
  const id = focus.taskId;
  const actual = Math.max(1, Math.round((Date.now() - focus.startedAt) / 60000) - Math.round(focus.pausedAccum / 60000));
  closeFocus();
  await completeTask(id, actual);
  // Roll straight into the next task if there is one — keep the momentum.
  try {
    const res = await api("/next");
    if (res.task) {
      $("f-next-hint").textContent = "";
      startFocus(res.task.id);
    }
  } catch {}
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

  $("btn-braindump").addEventListener("click", () => $("modal-braindump").showModal());
  $("b-compile").addEventListener("click", compileBraindump);

  $("btn-focus").addEventListener("click", () => startFocus(null));
  $("btn-settings").addEventListener("click", openSettings);
  $("s-save").addEventListener("click", saveSettings);
  $("s-buffer").addEventListener("input", () =>
    $("s-buffer-val").textContent = $("s-buffer").value + "%");
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

  // focus overlay
  $("f-close").addEventListener("click", closeFocus);
  $("f-done").addEventListener("click", finishFocus);
  $("f-skip").addEventListener("click", async () => {
    const skipped = focus.taskId;
    closeFocus();
    if (skipped) await patchTask(skipped, { status: "todo" });
    const res = await api("/next").catch(() => null);
    if (res?.task && res.task.id !== skipped) startFocus(res.task.id);
    else toast("That was the only task left.");
  });
  $("f-pause").addEventListener("click", () => {
    focus.paused = !focus.paused;
    $("f-pause").textContent = focus.paused ? "▶ Resume" : "⏸ Pause";
    if (focus.paused) focus.pauseStart = Date.now();
    else focus.pausedAccum += Date.now() - focus.pauseStart;
  });
  $("f-extend").addEventListener("click", () => {
    focus.remainingSec += 300;
    focus.totalSec += 300;
    focus.cuesFired.delete("go");
    focus.cuesFired.delete("ready");
    $("f-stage").textContent = "";
    drawFocusTimers();
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
  try {
    settings = await api("/settings");
    $("add-granularity").value = settings.granularity;
    applyState(await api("/state"));
    if (!settings.has_api_key)
      toast("No Anthropic API key set — AI features are off. Add one in ⚙ Settings.");
  } catch (e) {
    toast("Could not load tasks: " + e.message, true);
  }
}

boot();
