# Modules

Turn off the parts of the app you don't use.

## How it works

Open ⚙ Settings → **🧩 Modules** and untick anything you don't want, then save. Everything is on to begin with.

- **Integrations**: [ClickUp sync](24-clickup-sync.md) and the [MCP server](26-other-apps-and-agents.md).
- **AI**: [Magic ToDo](08-magic-todo.md), the [Braindump](09-braindump.md) compiler, and AI estimates (the estimate, [impact and effort](13-impact-and-effort.md) and start time a new task is given).
- **Views**: the [Calendar](06-calendar.md), the [Overview](04-overview.md), [Habits](05-habits.md) and this Help area.
- **Extras**: [repeating tasks](19-repeating-tasks.md), [focus mode](20-focus-mode.md), [transition alarms](21-transition-alarms.md), [XP and levels](15-xp-and-levels.md) and [sounds](22-sounds.md).

The task list, projects, scores and the way the app [plans your days](17-planning-your-days.md) are the app itself, so they are always on.

A module that is off is off everywhere, not just hidden:

- its buttons, tabs and settings leave the page;
- the server stops answering for it, so other apps and agents can't use it either;
- its background work (the ClickUp sync, new copies of repeating tasks, alarms) stops;
- anything it had queued while the [connection was down](25-when-the-connection-drops.md) waits until it is back on.

**Nothing is deleted.** Your routines, repeat rules, XP and imported ClickUp tasks stay where they are. Turn the module back on and they are all there.

Two things to know about repeating tasks. While the module is off, the copy on your list is an ordinary task: finishing it doesn't make the next one. When you turn it back on, each rhythm picks up where it stopped. The first beat it skipped can show up as overdue for a while, until the next hourly check marks it and the other skipped beats missed and puts the current one on your list.

## Why it works this way

A screen full of features you never touch is a screen you have to read past every time you look at it. Switching them off makes the app smaller rather than just quieter.

Off means really off, because a hidden feature that is still running in the background still costs something: API calls, notifications, a webhook firing at a Discord channel. If you turned it off, none of that should happen.

Your data stays because turning a module off is a choice about today. It shouldn't cost you a year of habit squares to find out you missed them.
