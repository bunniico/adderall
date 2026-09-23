# Welcome

Adderall is a single-user task app for people with executive dysfunction (ADHD, autism, AuDHD). It is built to get tasks out of your head, decide what matters, and hand you one thing to do next, with as little effort on your part as possible.

## How it works

Everything happens on one page. Your tasks sit in a list, one list per [project tab](02-projects-and-tabs.md). The buttons in the header open the rest:

- **📅 Calendar** swaps the list for a [dated view](06-calendar.md) of every project.
- **🧠 Braindump** turns a messy paragraph into [tasks](09-braindump.md).
- **▶ Focus** shows you [one task at a time](20-focus-mode.md) with a visual timer.
- **⚙** opens Settings.
- **❓** opens this Help area.

Type a task into the box at the top and press Enter. The app estimates how long it takes, scores it for impact and effort, and gives it a deadline and a start time if you want it to. You can override any of it.

## Why it works this way

The app follows five rules, and most of the articles here come back to one of them.

1. **Reliability first.** One user, one process, one SQLite file, and every edit is saved the moment you make it. Nothing can go out of sync, because there is nothing to sync.
2. **One page, no swapping.** Projects are tabs over the same page, and the calendar is one button that swaps the middle of the page and swaps it back. There is nowhere to get lost and nothing to find your way home from.
3. **Time is visual and honest.** Estimates are padded by default, timers show elapsed and remaining time together, and alarms come in stages instead of all at once.
4. **Deterministic where possible, AI only where needed.** Scheduling, scores, XP, repeats and deadlines are all plain local code. The AI only does language work: breaking a task into steps, estimating it, reading a braindump. It never picks a date; it says "this feels like an evening thing" and the app works out which evening.
5. **Low friction.** No accounts, one command to start, nothing you have to configure before it is useful.
