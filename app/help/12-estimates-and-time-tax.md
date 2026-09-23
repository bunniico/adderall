# Estimates and the time tax

Every task gets a time estimate, and every estimate gets padded.

## How it works

**The estimate** is seeded by the AI and always yours to change: open the task and edit *Raw estimate (minutes)*.

**The time tax** is a buffer added on top of every estimate: 30% by default, adjustable from 25% to 50% in ⚙ Settings. A 45-minute task is planned as about an hour. On the calendar's Day view the tax is drawn as a striped tail on each block.

**Adaptive buffer** (in ⚙ Settings) learns from how long your finished tasks actually took compared to their estimates. It only ever raises the buffer, never lowers it.

**Rolled-up totals.** A task with subtasks shows the padded total of everything under it, and the furthest deadline anywhere inside it. A "~20m" parent with 46 minutes of steps under it reads ~46m. Under it, a **progress bar measured in time** shows how many minutes are done and how many are left.

## Why it works this way

Plans take longer than they feel like they will. This is the planning fallacy, and it is worse, not better, when time is hard to feel. Raw estimates are reliably too short, so the app pads them for you rather than asking you to remember to.

The adaptive buffer only rises because a buffer that shrinks after one fast week sets you up for the next slow one.

Progress is shown in time rather than ticked boxes because ten ticks of five-minute jobs and one tick of a three-hour job are not the same amount of progress.
