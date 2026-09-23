# Other apps and AI agents

Everything the page does goes through a JSON API, so other programs can use the app too.

## How it works

**The API.** Its reference is at `/api/docs` on the machine running the app.

**Alarms as events.** [Transition alarms](21-transition-alarms.md) go to three places:

- the banner on the page;
- `GET /api/events`, a stream any local program can listen to (`curl -N localhost:8000/api/events`);
- every URL under ⚙ Settings → *Alarm webhooks*.

**An MCP server for AI agents** at `/mcp`. Its tools list, add, edit, start, complete and delete tasks, pick the next one, compile a braindump, check off habits, and read the latest alarms. To connect Claude Code:

```
claude mcp add --transport http adderall http://localhost:8000/mcp
```

## Why it works this way

A task app you can only reach through its own page is a task app you have to remember to open. Events and webhooks let alarms reach wherever you already are, and the MCP server lets an assistant add or check off tasks for you.

**Nothing asks who is calling**, the same as the rest of the app, which has no accounts. The MCP server only answers requests addressed to `localhost`, but the event stream and the API answer anyone who can reach the port. If the app is on a shared network, put it behind a password or keep it off that network.
