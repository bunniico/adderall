/* Motion and sound.
 *
 * Two jobs that look unrelated and are not: both exist to answer "did that
 * land?" without the app having to say so in words. A row that squashes and a
 * click that thumps are the same feedback delivered to two senses, and they
 * are timed against each other on purpose — the click fires at the moment the
 * row is at the bottom of its anticipation dip, not when the pointer goes
 * down, so the sound reads as the row hitting rather than as a UI beep.
 *
 * Everything in here is decoration over information. The app is fully usable
 * with sound off and `prefers-reduced-motion` on; nothing below is load-bearing.
 */

const Motion = (() => {
  const reduceQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const reduced = () => reduceQuery.matches;

  /* ---------------- sound ----------------
   * The built-in set is synthesized, not sampled: four short sounds is a lot
   * of bytes to ship and cache for something most people turn off, and an
   * oscillator is always ready the instant it is wanted. Anything the user
   * picks in settings is decoded once and played as a buffer instead. */

  let ctx = null;
  let master = null;
  let enabled = true;
  let volume = 0.55;
  const custom = new Map();   // slot -> { name, buffer: AudioBuffer }

  /* Browsers refuse an AudioContext until the user has actually done
   * something, so it is built on the first sound and resumed on every one —
   * a context suspended by a background tab is otherwise silent forever. */
  function audio() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!ctx) {
      ctx = new AC();
      master = ctx.createGain();
      master.gain.value = volume;
      master.connect(ctx.destination);
    }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  }

  function env(gain, t, peak, attack, decay) {
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.0002, peak), t + attack);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
  }

  /* A muted retro button click. The pitch drop is what makes it read as a
   * physical switch rather than as a beep, and the lowpass is what keeps it
   * from being a physical switch you would like to throw across the room on
   * the two hundredth press. */
  function click(c, at, { pitch = 420, peak = 0.16, cutoff = 1100 } = {}) {
    const osc = c.createOscillator();
    const gain = c.createGain();
    const lp = c.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = cutoff;
    osc.type = "triangle";
    osc.frequency.setValueAtTime(pitch, at);
    osc.frequency.exponentialRampToValueAtTime(pitch * 0.42, at + 0.035);
    env(gain, at, peak, 0.004, 0.042);
    osc.connect(gain).connect(lp).connect(master);
    osc.start(at);
    osc.stop(at + 0.09);
  }

  /* One struck note with two quiet partials above it. Sine only, long decay,
   * nothing percussive — the brief for finishing a task was "satisfying
   * without being sensory overload", and a bell is about as far as that goes
   * before it becomes a slot machine. */
  function chime(c, at, root, { peak = 0.16, decay = 0.5 } = {}) {
    [[1, peak], [2, peak * 0.28], [3, peak * 0.1]].forEach(([mult, p]) => {
      const osc = c.createOscillator();
      const gain = c.createGain();
      osc.type = "sine";
      osc.frequency.value = root * mult;
      env(gain, at, p, 0.008, decay);
      osc.connect(gain).connect(master);
      osc.start(at);
      osc.stop(at + decay + 0.1);
    });
  }

  const BUILTIN = {
    click:    (c, t) => click(c, t),
    // A softer, lower click for anything destructive or dismissive: the same
    // switch, thrown the other way.
    back:     (c, t) => click(c, t, { pitch: 260, peak: 0.12, cutoff: 700 }),
    // Two notes a fifth apart, the second landing while the first still rings.
    complete: (c, t) => { chime(c, t, 659.25); chime(c, t + 0.085, 987.77, { peak: 0.13, decay: 0.62 }); },
    levelup:  (c, t) => [523.25, 659.25, 783.99, 1046.5]
                          .forEach((f, i) => chime(c, t + i * 0.075, f, { peak: 0.14, decay: 0.55 })),
    // Deliberately not a chime: an alarm has to survive being ignored.
    alarm:    (c, t) => [0, 0.22, 0.44].forEach((d) => chime(c, t + d, 880, { peak: 0.2, decay: 0.18 })),
    // The same switch as `click`, just barely touched: high and quiet enough
    // to read as a hint rather than a press, so it never competes with it.
    hover:    (c, t) => click(c, t, { pitch: 900, peak: 0.045, cutoff: 2400 }),
  };

  const SLOTS = [
    { id: "click",    label: "Button press",  hint: "every press, everywhere" },
    { id: "hover",    label: "Hover",         hint: "pointing at a button" },
    { id: "complete", label: "Task finished", hint: "checking something off" },
    { id: "levelup",  label: "Level up",      hint: "passing a level" },
    { id: "alarm",    label: "Alarm",         hint: "a deadline transition" },
  ];

  function play(name) {
    if (!enabled) return;
    const c = audio();
    if (!c) return;
    try {
      const own = custom.get(name);
      if (own) {
        const src = c.createBufferSource();
        src.buffer = own.buffer;
        src.connect(master);
        src.start();
        return;
      }
      (BUILTIN[name] || BUILTIN.click)(c, c.currentTime);
    } catch { /* a browser that will not make noise is not an error */ }
  }

  /* ---------------- custom sounds, in this browser only ----------------
   * IndexedDB rather than the server: these are a few hundred KB of personal
   * preference with no reason to leave the machine, and shipping them through
   * the API would mean an upload endpoint, a quota, and a migration for
   * something that is genuinely per-device taste. The cost is that they do not
   * follow you to another browser, which the settings panel says out loud. */

  const DB_NAME = "adderall-sounds";
  const STORE = "sounds";
  const MAX_BYTES = 2 * 1024 * 1024;

  function db() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  function tx(mode, fn) {
    return db().then((d) => new Promise((resolve, reject) => {
      const t = d.transaction(STORE, mode);
      const req = fn(t.objectStore(STORE));
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    }));
  }

  async function decodeInto(slot, rec) {
    const c = audio();
    if (!c) return;
    // decodeAudioData consumes the buffer it is handed, so it gets a copy —
    // otherwise re-decoding after a settings reopen finds an empty array.
    const buffer = await c.decodeAudioData(rec.bytes.slice(0));
    custom.set(slot, { name: rec.name, buffer });
  }

  async function loadCustom() {
    if (!window.indexedDB) return;
    try {
      for (const { id } of SLOTS) {
        const rec = await tx("readonly", (s) => s.get(id));
        if (rec) await decodeInto(id, rec);
      }
    } catch { /* a browser with no storage still gets the built-in set */ }
  }

  async function setCustom(slot, file) {
    if (file.size > MAX_BYTES) throw new Error("That file is over 2 MB.");
    const bytes = await file.arrayBuffer();
    const rec = { name: file.name, bytes };
    // Decode before storing, so a file this browser cannot play is rejected
    // here rather than silently going quiet later.
    await decodeInto(slot, rec);
    await tx("readwrite", (s) => s.put(rec, slot));
  }

  async function clearCustom(slot) {
    custom.delete(slot);
    try { await tx("readwrite", (s) => s.delete(slot)); } catch {}
  }

  const customName = (slot) => custom.get(slot)?.name || null;

  function setEnabled(on) { enabled = !!on; }
  function setVolume(v) {
    volume = Math.max(0, Math.min(1, v));
    if (master) master.gain.value = volume;
  }

  /* ---------------- animation helpers ---------------- */

  /* Restarts an animation that may already be running. Removing the class and
   * reading offsetWidth is the only reliable way to force the reflow that
   * makes the browser treat the re-add as a new animation. */
  function replay(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
  }

  /* Marks rows the list has never drawn before so they animate in, and leaves
   * every other row alone. The whole list is rebuilt on every state change, so
   * without this the entire page would re-cascade each time you ticked a box.
   *
   * The stagger is capped: past a dozen rows the cascade has already read as a
   * cascade, and the only thing more delay buys is a longer wait for row
   * forty. */
  const STAGGER_MS = 42;
  const STAGGER_MAX = 12;

  function enterNew(root, known, selector = ".task") {
    if (reduced()) {
      for (const el of root.querySelectorAll(selector))
        if (el.dataset.id) known.add(el.dataset.id);
      return;
    }
    let i = 0;
    for (const el of root.querySelectorAll(selector)) {
      const id = el.dataset.id;
      if (!id || known.has(id)) continue;
      known.add(id);
      el.style.setProperty("--stagger", Math.min(i++, STAGGER_MAX) * STAGGER_MS + "ms");
      el.classList.add("enter");
      // Taken off once it has played. A one-shot class left on the element
      // keeps overriding whatever resting animation the row is meant to have
      // — which for the next-up task is the only thing marking it as next.
      el.addEventListener("animationend", () => el.classList.remove("enter"),
                          { once: true });
    }
  }

  /* Steps arriving out of a container that was just unfolded. Same idea, its
   * own stagger, and only for the fold it belongs to. */
  function unfold(wrap) {
    if (reduced()) return;
    wrap.classList.add("unfolding");
    let i = 0;
    for (const el of wrap.children) {
      if (!el.classList.contains("task")) continue;
      el.style.setProperty("--stagger", Math.min(i++, 8) * 36 + "ms");
    }
  }

  /* Checking something off.
   *
   * `commit` is the network call, and it deliberately does not run first. The
   * row is replaced the moment the server answers, so firing immediately would
   * mean the animation is cut off by its own success on a fast connection and
   * plays in full on a slow one — the reward would arrive least reliably for
   * the people waiting longest. Instead the leap plays, the request goes out
   * at the top of it, and the re-render lands during the settle where it is
   * invisible either way. */
  const CHECK_COMMIT_MS = 200;

  function checkOff(row, commit) {
    play("complete");
    if (reduced() || !row) { commit(); return; }
    replay(row, "checking");
    row.addEventListener("animationend", () => row.classList.remove("checking"),
                         { once: true });
    setTimeout(commit, CHECK_COMMIT_MS);
  }

  /* Deleting. Here the animation genuinely does gate the work, because a row
   * that shrinks away after it has already gone from the list has nothing to
   * shrink. */
  function leave(el, done) {
    play("back");
    if (reduced() || !el) { done(); return; }
    el.classList.add("leaving");
    let fired = false;
    const go = () => { if (!fired) { fired = true; done(); } };
    el.addEventListener("animationend", go, { once: true });
    setTimeout(go, 320);   // animationend never fires on a hidden tab
  }

  /* Dialogs close instantly by default; this gives the panel its exit before
   * the browser takes it off the screen. */
  function closeDialog(dlg) {
    play("back");
    if (reduced()) { dlg.close(); return; }
    dlg.classList.add("closing");
    setTimeout(() => { dlg.classList.remove("closing"); dlg.close(); }, 130);
  }

  function kick(el, cls) {
    if (reduced() || !el) return;
    replay(el, cls);
    setTimeout(() => el.classList.remove(cls), 400);
  }

  const INTERACTIVE_SELECTOR = "button, .weekday-chip, summary";

  /* Every button in the app clicks, without every button in the app having to
   * ask. Pointerdown rather than click, because the sound belongs to the press
   * — waiting for the release puts it a hundred milliseconds behind the finger
   * and the whole thing stops feeling connected.
   *
   * `.no-click` opts a control out, for the handful that play their own sound. */
  function wireClicks() {
    document.addEventListener("pointerdown", (e) => {
      const el = e.target.closest(INTERACTIVE_SELECTOR);
      if (!el || el.disabled || el.classList.contains("no-click")) return;
      play(el.classList.contains("danger") || el.classList.contains("bad") ||
           el.classList.contains("close") ? "back" : "click");
    }, { passive: true });
  }

  /* A hint of sound on the way to a press, not just on it. Mouse only — a
   * touchscreen has no hover, and firing this off the synthetic pointerover
   * a tap sends first would double it up with the click a moment later.
   *
   * `lastHoverEl` stops the same element re-triggering itself as the pointer
   * crosses its children (pointerover bubbles); the timestamp gap on top of
   * it stops a fast sweep across a whole row of buttons from playing all of
   * them in a burst — a wash of clicks reads as noise, not as five hints. */
  let lastHoverEl = null;
  let lastHoverAt = 0;
  const HOVER_MIN_GAP_MS = 45;

  function wireHover() {
    document.addEventListener("pointerover", (e) => {
      if (e.pointerType && e.pointerType !== "mouse") return;
      const el = e.target.closest(INTERACTIVE_SELECTOR);
      if (!el || el.disabled || el.classList.contains("no-click") || el === lastHoverEl) return;
      lastHoverEl = el;
      const now = performance.now();
      if (now - lastHoverAt < HOVER_MIN_GAP_MS) return;
      lastHoverAt = now;
      play("hover");
    }, { passive: true });
  }

  return {
    reduced, play, setEnabled, setVolume, loadCustom, setCustom, clearCustom,
    customName, SLOTS, enterNew, unfold, checkOff, leave, closeDialog, kick,
    replay, wireClicks, wireHover,
  };
})();
