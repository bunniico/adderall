# AI and your budget

What the AI does, which model does it, and how to cap what it costs.

## How it works

**Setting up.** AI features need an Anthropic API key. Paste one into ⚙ Settings, or set `ANTHROPIC_API_KEY` in the environment. If your key is identity-linked, also fill in *Workspace ID* (from `platform.claude.com/workspaces/<id>`). Everything else in the app works without a key.

**Which model does what.**

| Job | Model tier | Why |
|---|---|---|
| Estimates, impact/effort, start times | Fast | Runs on every new task, in batches |
| Breaking a task down | Balanced | You are waiting for it |
| Compiling a braindump | Deep, with thinking | The hardest grouping job, once per dump |

**Daily AI budget.** Set a figure in ⚙ Settings and the app answers with cheaper models as the day's spend climbs:

| Spent today | Estimates | Breakdown | Braindump |
|---|---|---|---|
| under half | fast | balanced | deep |
| half | fast | balanced | balanced |
| three quarters | fast | fast | balanced |
| all of it | fast | fast | fast |

A badge appears in the header only while the budget is actually lowering the models. A budget of 0 (the default) turns it off.

## Why it works this way

**AI only does language work.** It breaks tasks down, estimates them, and reads braindumps. Dates, scores, scheduling and XP are plain local code that can be tested and trusted.

**The budget never refuses a call.** An app that stops working at 4pm is worse than one that gets a little blunter, so the fast model is the floor and everything keeps running on it.

**The day is your local day**, so the total resets at your midnight.

**The figure is an estimate, not an invoice.** Token counts come from the API, but prices come from a table in the app that will drift. A model the table has never heard of is costed at the highest known rate, because a budget that guesses low does not hold.
