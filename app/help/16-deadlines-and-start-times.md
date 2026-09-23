# Deadlines and start times

A deadline says when something has to be finished. A start time says when it wants to begin.

## How it works

Both are in the task dialog.

**Deadlines.** Set one yourself, or turn on *Auto-deadlines* in ⚙ Settings and the app assigns one. Subtasks are scheduled backwards from their parent's deadline using padded estimates. **Urgency** rises as the time left shrinks relative to the work left, and it drives the "what next" order.

**Start times.** One click sets the common ones: **Now · In an hour · This evening · Tomorrow morning · Next week · Some day**. The scheduler places the task from that hour, on that day, in the first slot that fits.

**The AI fills start times in.** With *AI start times* on in ⚙ Settings, each new task gets a suggested start: *eat dinner* comes back as this evening, *renew the passport* as a weekday soon, *finish that game* as weeks out. Override any of it by hand.

Steps inside a task never get their own start time. They are scheduled inside the slot their parent was given.

The small tag next to the deadline says whether you set it or the app assigned it (and when), and the note under the start time explains what that start time will do.

## Why it works this way

For most of what people write down, "when does this start" is the easier question and the more useful answer. *Eat dinner* is a six o'clock thing with no deadline in any meaningful sense.

**Start times cut both ways.** A start time a few hours away makes a task climb the list as its hour approaches. **Some day** parks something a month out, where it stops competing for this afternoon; *finish that game* sinks to a score around 20 and stays there until it is nearly time.

A start time can put work outside your working hours, because the working window is where the app puts work it chose the hour for *itself*. It is not a rule about when you are allowed to eat.

The AI never returns a date. It answers with an offset from a local time the app tells it, which keeps timezones and date formats out of the model's hands entirely. It is asked to use big offsets for things that do not matter, because putting unimportant things near the front is how a list stops being usable.
