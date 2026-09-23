# Repeating tasks

Some things are rhythms, not tasks: bins on Tuesday, rent on the first, the standup every weekday morning.

## How it works

Open a top-level task and set **🔁 Repeat** to **Daily**, **Weekly**, **Monthly** or **Yearly**. **Custom…** is those same four with more options: every 3 days, every other week on Mon & Thu, the last Friday of every second month, the 31st (which becomes the last day in shorter months).

You can also:

- give it a **time of day**, or leave it to land at the end of your working day;
- end it **after a number of times**, **on a date**, or **never**;
- tick **Count from when I finish it**, so "every 3 days" means three days after you actually do it.

The dialog shows the next four real dates as you type.

**One copy at a time.** Only one copy of a repeating task is ever on your list. Finish it and the next appears as soon as its day is close enough; if that is a while off, the app says when it will land.

**Editing this copy edits the series**: title, estimate, subtasks. A step you added only for this time can be marked so it does not carry forward.

**Discarding or deleting a copy** skips that one. **Repeat → Doesn't repeat** ends the series.

## Why it works this way

**One copy is the decision the whole feature is built around.** A daily chore ignored for a fortnight comes back as one row, not fourteen. That pile is exactly what this app exists to prevent, and it is also untrue: you are one bin-day behind, not fourteen. So when a date comes round while the last copy is still there, it steps forward instead of stacking.

**A day of lead means a day.** Finishing this morning's chore puts tomorrow's on the list before you close the laptop, whatever hour tomorrow's is due at.

**The preview dates come from the same code that schedules**, so the dialog, the badge and the schedule cannot tell three different stories about one rule. "27 Feb · 24 Apr · 26 Jun" explains "the last Friday of every second month" better than the rule does.

**The calendar shows copies ahead.** One copy at a time is right for a list and wrong for a calendar, so the calendar draws three months of future copies as outlined blocks, and the scheduler plans around them. See [Calendar](06-calendar.md).

New copies are made by a sweep that runs hourly rather than at midnight, because a laptop that is asleep at midnight would otherwise silently skip a day.
