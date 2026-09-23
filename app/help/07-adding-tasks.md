# Adding tasks

Type into the box at the top of a list and press Enter.

## How it works

A new task lands in the project you are on. In the background, the AI estimates how long it takes, suggests impact and effort, and suggests a start time. Each of those can be turned off in ⚙ Settings, and each can be changed by hand.

**Dates in the title.** With *Read deadlines/repeats from the title* on, the app reads scheduling words straight off what you typed and tidies them out of the title:

- `pay rent tomorrow` sets a deadline of tomorrow.
- `call the bank by friday`, `this friday`, `next tuesday`, `in 3 days`, `in 2 weeks`, `today`, `tonight` all work.
- `at 5pm` or `at 17:30` sets the time of day.
- `water the plants every day`, `every other week`, `every 3 months`, `every year` set a [repeat](19-repeating-tasks.md).

A bare weekday name only counts when a word like *by*, *due* or *on* is in front of it, so "email Monday's notes" does not get a deadline.

*…and let the AI take a second look* adds an AI fallback for phrasing too loose for the built-in reader to catch.

The **🌶** slider next to the box sets how fine a [breakdown](08-magic-todo.md) will be.

Click any task to open its dialog, where you can edit the title, description, start time, deadline, estimate, impact, effort, repeat and project.

## Why it works this way

"Pay rent tomorrow" should not need a date picker afterwards; the words are already there. The built-in reader is deliberately cautious and leaves anything it does not recognise alone, because a wrong match that quietly reassigns a deadline is far worse than a phrase it fails to catch.

The AI fills in estimates and scores because a field you have to fill in before a task is useful is friction, and friction is what stops a list from being used.
