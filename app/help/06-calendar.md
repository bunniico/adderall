# Calendar

**📅 Calendar** in the header swaps the task list for a calendar. **☰ List** swaps it back.

## How it works

There are three views:

- **Day** is a real time grid. Each task is a block ending at its deadline and starting one padded estimate earlier. The [time tax](12-estimates-and-time-tax.md) is drawn as a striped tail, free stretches between blocks are labelled with their length, and a line marks the current time. A running count shows how much of the day is booked against how much it holds.
- **Week** and **Month** are day-by-day lists ranked by [score](14-the-score.md), so the top of each day is what that day is actually about.

The filter row narrows it to one **project** or one **category**, and can show **completed** tasks. **⚙** next to it opens calendar settings, where you can hide **auto-scheduled** deadlines (ones the app assigned) and **repeats ahead**.

**Repeats ahead** are outlined blocks: the future copies a [repeating task](19-repeating-tasks.md) will make, drawn three months out. They count toward how full a day is, and clicking one opens the copy that is on your list.

**↻ Replan** asks the app to work out again where everything goes. The note beside it says when it last did.

**Keys:** ← / → page back and forward, **T** goes to today, **D** / **W** / **M** switch view. Clicking any task opens it, whichever project it lives in.

## Why it works this way

The calendar is the one view that ignores the open tab, because "what is due this week" is a question about your whole life, not one list. It hides the tab strip while open and uses its own project filter instead, since two project pickers on screen would only disagree.

Week and Month sort by score rather than time because a list of twelve things on a Tuesday is more useful ordered by importance.

The striped tail exists so you can see that a "45m" job really occupies an hour.

Repeats ahead are on by default because a week of standing commitments is most of what a week is. Without them, a fortnight of eight-hour workdays would look like a fortnight of free afternoons.
