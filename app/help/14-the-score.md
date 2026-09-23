# The score

Every active task wears a **★** number from 0 to 100: the app's answer to "what deserves the next hour".

## How it works

Four signals fold into one number:

| Signal | Weight | What it measures |
|---|---|---|
| Urgency | 40% | How close the deadline or start time is, against the padded work left |
| Impact | 30% | How much it matters |
| Effort | 15% | How much it takes out of you (lower effort scores higher) |
| Time cost | 15% | How long it takes (shorter scores higher) |

Open a task to see what its score is made of.

**A parent is its parts.** A task with subtasks has no score of its own. It takes the combined score of the work still underneath it, each step weighted by its length in minutes. Finished and discarded steps stop counting, so a project is worth what is left of it.

A task nobody has estimated or rated sits at neutral, never at zero.

## Why it works this way

**Effort and time are counted separately** because they are different costs. A form you dread for ten minutes is cheap on the clock and expensive in effort. Three hours of mindless data entry is the other way round.

**Time cost decays rather than scaling flat.** Ten minutes scores 8.6 out of 10, an hour 5, a whole day about 1. Shaving twenty minutes off a half-hour job changes whether you do it now; shaving twenty off a six-hour one changes nothing.

**The score is shown on the task** so the order of your list is something you can check rather than something you have to take on trust.

Re-rating a container changes nothing, because the work is in the steps. The score also decides who gets a crowded day when the app [plans your days](17-planning-your-days.md), and it is what a finished task pays out as [XP](15-xp-and-levels.md).
