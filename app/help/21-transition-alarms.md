# Transition alarms

Three staged cues before a task's deadline or its planned slot.

## How it works

Turn on *Transition alarms* in ⚙ Settings and set how many minutes before each cue fires:

1. **Stop**: stop what you are doing.
2. **Ready**: get ready.
3. **Go**: go.

Each has its own sound. Alarms show as a banner at the top of the page, on whichever tab you are on, and say which project the task came from.

**Alarm webhooks.** Add URLs in ⚙ Settings, one per line, and every alarm is also POSTed to each of them. A Discord webhook URL gets a Discord message; any other URL gets the alarm as JSON.

## Why it works this way

Switching tasks is hard. One alarm at the deadline is too late to stop cleanly, so there are three: one to start wrapping up, one to get ready, one to go.

**Alarms ignore tabs.** A cue you miss because its task is one tab over is exactly what this app exists to prevent.

**The server raises alarms**, not the page, so they fire whether or not a browser tab is open. That is also what lets them reach webhooks and other apps. See [Other apps and AI agents](26-other-apps-and-agents.md).
