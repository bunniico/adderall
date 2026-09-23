# ClickUp sync

Mirror every open ClickUp task assigned to you into its own tab.

## How it works

Put a personal API token (from ClickUp → Settings → Apps) into ⚙ Settings → **🔄 ClickUp sync**. The first sync creates a **ClickUp** tab and pulls in every open task assigned to you, across every workspace the token can see.

Sync runs at startup, then every 30 minutes, and whenever you press **Sync now**.

Re-syncing refreshes an imported task's **title, description and due date** from ClickUp. Everything else (estimate, impact, effort, status, subtasks, which tab it lives in) is yours and is never touched once the task exists.

**New assignment webhooks.** Add URLs, one per line, and each task newly assigned to you is POSTed to each of them as a Discord-style message with its title, due date and link. The first sync after connecting announces nothing, since everything it finds was already assigned.

## Why it works this way

**It is one-way on purpose.** Nothing is pushed back to ClickUp, so nothing you do here can surprise your team.

**A task that disappears from ClickUp is left alone.** If it is finished, reassigned or deleted there, it is not auto-completed or removed here. Silently deleting something from your list is worse than leaving it for you to tick off.

**Only three fields are refreshed** because those are the ones ClickUp owns. Your estimate and priority are your own reading of the work and should not be overwritten every half hour.

Mentions are not announced, because ClickUp's API has no way to list them.
