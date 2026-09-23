# When the connection drops

There are two connections, and they fail differently.

## How it works

**Your browser to the app.** If the server stops answering (it is down, the machine is asleep, the network dropped), a banner says so at the top of the page. Once the server is back, the next thing you do just works.

**The app to the internet.** Breakdown, re-estimate, the braindump compiler and ClickUp sync all call out to the internet. If a call fails because the machine has no connection, it is **queued** instead: a message tells you so, and it runs on its own once the connection is back. You do not need to redo anything, and a queued request survives a restart.

A missing or invalid API key, or the AI declining a request, still fails straight away.

## Why it works this way

A silent failure, or a cryptic browser error, leaves you unsure whether your work was saved. The banner makes it plain.

Queuing means a flaky connection does not cost you a braindump you just typed out.

Only connection failures are retried, because retrying a request that was never going to work just delays the same failure.

Your tasks themselves are never at risk: they live in a file on the machine running the app, and every edit is saved the moment you make it.
