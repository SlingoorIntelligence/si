LATEST CALLOUTS (the newest call is featured over the agent video, the next two sit beside it)

Setup once: put callouts.yml in .github/workflows/ next to collect.yml.
It runs every 6 hours, and also right after you edit callouts.json.

To add a call: open callouts.json on GitHub > pencil icon > add a line at the TOP, newest first:
  { "url": "PASTE THE PUMP.FUN CALLOUT LINK" },
Commit. Within a couple of minutes the bot reads the link's preview (caller name, text, image) and the
site shows it. Only the newest 3 are used.

If a link's preview comes through empty or wrong, fill the fields yourself and the bot keeps them:
  { "url": "...", "who": "caller name", "text": "what they wrote", "img": "callouts/2.jpg" }
(upload a screenshot into the callouts folder for "img").

CHART TIMEFRAME
The chart defaults to Birdeye's widget on 15-second candles. To change it:
1. Open https://birdeye.so/tv-widget-builder/YOUR_CA?chain=solana
2. Pick the timeframe and look you want, copy the URL it gives you.
3. Paste it into config.json as "chart_embed_url": "..." and commit.
Leave chart_embed_url empty to keep the default.

MUSIC
The site plays SI 2 on loop in this order: normal -> slowed + reverb -> chopped & screwed -> sped up.
Files live in /audio. Browsers don't allow sound before a visitor interacts with the page, so the
music starts on the first tap, click or key press (or the ENABLE AUDIO button on the monitor).
