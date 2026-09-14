/* Which days the calendar files an event under.
 *
 * `groupByDay` is the one piece of calendar.js worth testing on its own: it
 * decides what the week and month views draw, its edge cases are all off-by-one
 * (a span ending at midnight, two spans on one day, no spans at all), and none
 * of them are visible in a screenshot.
 *
 * calendar.js is a browser script rather than a module — plain declarations,
 * no exports — so it is evaluated here with the handful of globals it closes
 * over stubbed, and the real function is pulled out. That is deliberately not
 * a copy of the logic: a second implementation would agree with itself and
 * prove nothing.
 *
 * Run by `tests/test_calendar_js.py`, so `pytest` covers it like everything else.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "app", "static", "calendar.js"), "utf8");

const { groupByDay } = new Function(`
  const settings = {};
  const $ = () => ({ addEventListener() {}, classList: { add() {} }, style: {} });
  const document = { createElement: () => ({
    classList: { add() {} }, style: {},
    append() {}, appendChild() {}, addEventListener() {}, setAttribute() {},
  }) };
  const fmtMinutes = (m) => m + "m";
  const api = async () => ({});
  const applyState = () => {};
  const toast = () => {};
  ${src}
  return { groupByDay };
`)();

const ev = (title, deadline, blocks) =>
  ({ id: title, title, deadline, blocks, status: "todo", parent_id: null, score: 50 });

const cases = [
  ["work laid across Sat and Sun, due Monday, is on Sat and Sun",
   ev("spread", "2026-09-14T22:00:00Z",
      [["2026-09-12T09:00:00Z", "2026-09-12T22:00:00Z"],
       ["2026-09-13T09:00:00Z", "2026-09-13T14:00:00Z"]]),
   ["2026-09-12", "2026-09-13"]],
  ["two spans on one day draw one chip, not two",
   ev("twice", "2026-09-14T22:00:00Z",
      [["2026-09-14T09:00:00Z", "2026-09-14T11:00:00Z"],
       ["2026-09-14T15:00:00Z", "2026-09-14T17:00:00Z"]]),
   ["2026-09-14"]],
  ["a span ending exactly at midnight does not touch the next day",
   ev("tonight", "2026-09-14T00:00:00Z",
      [["2026-09-13T20:00:00Z", "2026-09-14T00:00:00Z"]]),
   ["2026-09-13"]],
  ["work with nowhere to go keeps the day it is due",
   ev("homeless", "2026-09-15T17:00:00Z", []),
   ["2026-09-15"]],
];

let failed = 0;
for (const [name, event, want] of cases) {
  const got = [...groupByDay([event]).keys()].sort();
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) {
    failed++;
    console.error(`FAIL  ${name}\n      got  ${JSON.stringify(got)}` +
                  `\n      want ${JSON.stringify(want)}`);
  }
}
if (failed) process.exit(1);
console.log(`${cases.length} groupByDay cases pass`);
