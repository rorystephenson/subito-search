# subito-search

Self-hosted alerts for [subito.it](https://www.subito.it) with an LLM filter.

Subito's own saved-search alerts are noisy — its search matches loosely, so you
get pinged about things you don't want. This runs your searches on a schedule,
asks an LLM whether each new listing actually matches a plain-English description
of what you're after, and sends only the survivors to your Telegram bot.

Per search you choose **the query**, **a prompt describing your interest**, and
optionally **filters**. The list lives with the trigger on the VPS, not in this
repo.

## How it works

```
subito JSON API  →  cheap local filters  →  LLM interest check  →  Telegram
                    (price, already-seen)    (Gemini free tier)
```

The local filters run first so the LLM only ever sees genuinely new ads — that's
what keeps usage inside the free tier.

It reads subito's own JSON search endpoint (`hades.subito.it/v1/search/items`)
rather than scraping HTML, so results come back structured and it doesn't break
when the site is restyled.

## Setup

### 1. Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 2. Get credentials

| Variable | Where from |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free tier is plenty |
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | Send your bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id` |

Put them in a `.env` file in the project root — it's gitignored, and the bot
loads it automatically:

```
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Real environment variables take precedence, so this never shadows the secrets in
GitHub Actions.

### Finding your chat ID

Bots can't start conversations, so message yours first: search its `@username`
in Telegram and tap **Start**. Then:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
  | python3 -c "import json,sys; print({u['message']['chat']['id'] for u in json.load(sys.stdin)['result'] if 'message' in u})"
```

An empty `result` means the bot hasn't been messaged, or it was more than ~24h
ago — `getUpdates` doesn't keep history longer than that. For a group chat, add
the bot to the group and send `/start` *there*; the ID will be negative.

### 3. Check everything is wired up

```bash
.venv/bin/python -m subito_alerts.main --check
```

### 4. Add a search

Searches are not in this repo. The trigger on the VPS keeps them in
`/opt/projects/subito-trigger/data/searches.json` and sends the whole list with
every dispatch. Adding or removing one means editing that file; the next tick
picks it up. See the infra repo's Subito Trigger section.

```json
[
  {
    "query": "bici da corsa",
    "filters": {"price_min": 200, "price_max": 600, "shippable": true},
    "prompt": "A complete road bike in frame size 54-56, Shimano 105 or better. Not interested in frames alone, spare parts, wheels, or mountain bikes."
  }
]
```

**A search is identified by its query alone** — lowercased, with runs of
whitespace collapsed, then hashed into the state key. So:

- **Edit the `prompt` or `filters` freely.** The search keeps what it has
  already seen and carries on from its last run.
- **Changing the query makes it a new search.** It cold-starts, which is right:
  a different query returns different ads.
- **Removing a search forgets it.** The next run drops its state, so adding it
  back later cold-starts instead of alerting on the whole backlog. As a
  backstop, no search ever looks back further than 24h.

An empty list means the trigger doesn't dispatch at all, so a pause between
hunts costs no Actions minutes.

**`prompt` is the important part.** Be specific about what you *don't* want as
well as what you do — that's what kills the noise. The classifier is told to lean
towards including ambiguous or terse listings, on the grounds that a missed
bargain costs more than one extra notification.

`filters` are applied by subito itself:

| Filter | Meaning |
|---|---|
| `price_min` / `price_max` | price range in € |
| `shippable` | only ads the seller will ship |
| `title_only` | match the query against the title only |
| `category` | subito category id (e.g. `41` = Biciclette) |
| `region` / `town` | subito region / town id |

To find a `category` or `region` id, run the search on subito.it and read the ids
out of the URL's query string.

### Scheduling

There is none in here either. The same VPS trigger decides when runs happen.

`searches.yaml` holds only how to fetch and the defaults every search gets.
`cold_start_minutes` is not a schedule: it bounds how far back a search looks
the very first time it runs, or after its state is lost, so a cold start alerts
on recent listings rather than a whole page of old ones.

## Running it

Locally, pass the list as a file (the workflow receives it in
`$SUBITO_SEARCHES` instead):

```bash
# See what it would do, without sending anything. State is not saved.
python -m subito_alerts.main --searches searches.json --dry-run

# Tune the filters without spending LLM calls
python -m subito_alerts.main --searches searches.json --dry-run --no-classify
```

With no searches supplied, a run logs that and exits cleanly.

| Flag | |
|---|---|
| `--searches FILE` | JSON list of searches to run |
| `--dry-run` | print instead of sending; don't save state |
| `--search QUERY` | run just the search with this query (repeatable) |
| `--no-classify` | skip the LLM pass entirely |
| `--check` | verify config, searches and credentials, then exit |
| `--verbose` | debug logging |

### Tuning your prompt

Run `--dry-run` and read the verdict lines — each shows
`MATCH`/`reject` and the model's one-line reason:

```
MATCH  350 €   Bici da corsa Cinelli — carbon frame, 105 groupset
reject 230 €   Freni Campagnolo super record — brake parts, not a bike
```

If something is wrongly rejected, the reason usually tells you which clause of
your prompt to soften. Iterate here before letting it run unattended.

## Running on GitHub Actions

`.github/workflows/alerts.yml` has no `schedule:` trigger — GitHub's cron
never fired here. It is started by the VPS trigger, or by hand from the Actions
tab.

1. Push this repo to GitHub. **Make it private**: each run's inputs, and so
   what you're hunting for and what you'll pay, are visible in the Actions tab.
2. Settings → Secrets and variables → Actions → add `GEMINI_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
   Optionally add a repo *variable* `GEMINI_MODEL` to override the model.
3. Actions tab → **Subito alerts** → **Run workflow** to test it. Paste a JSON
   list into **searches**; left empty, the run does nothing.

Running on Actions also means requests come from varied runner IPs rather than
all from your home connection.

### Cost

Actions is free and unlimited on public repositories. On a private one you get
2,000 minutes/month on the Free plan, and each run is billed as a whole minute —
so cost tracks the number of dispatches. The schedule in the VPS crontab
(08:00-22:00 Rome, every 15 minutes) is about 1,700 runs a month.


### State, and why duplicates are unlikely

Which ads have already been alerted on is kept in `state.json`, carried between
runs in the Actions cache. Caches are evicted after 7 days without use, so on a
cache miss the bot does **not** treat the whole first page as new — it only looks
back `cold_start_minutes` (capped at 24h). Losing the cache therefore costs you
a duplicate alert or two, not a flood.

### Gotchas

- **Scheduled workflows are disabled after 60 days of repository inactivity.**
  State lives in the cache, not in commits, so nothing here pushes to the repo.
  If it goes quiet, check the Actions tab — GitHub will have paused the schedule.
- **Scheduled runs are delayed under load.** `*/15` is a best effort, not a
  guarantee; runs can slip by several minutes at busy times.

## Behaviour when things break

The bot fails *open*: if Gemini is down, rate-limited, or returns something
unparseable, the affected ads are sent anyway, marked `⚠️ unclassified`. You get
some noise instead of silently missing a listing. Similarly, an ad whose Telegram
send fails is not recorded as seen, so it's retried on the next run.

## Notes on subito's API

Two things that aren't obvious and will silently degrade the bot if changed:

- **Image URLs need a rendition.** The API returns a bare `cdn_base_url` that
  serves HTTP 400 on its own. It needs `?rule=<rendition>` appended —
  `subito_alerts/subito.py` uses `gallery-desktop-2x-jpeg`. The `-auto`
  renditions serve AVIF for some source images, which Telegram's fetcher
  rejects, so alerts quietly fall back to text-only.
- **Subito scores the client's TLS and HTTP/2 fingerprint.** A plain HTTP
  library is refused with 403 from a GitHub runner; `curl_cffi` reproducing a
  real Chrome handshake is accepted from the same machine. This is why the
  client uses `curl_cffi` and why `impersonate` is configurable — roll it
  forward as Chrome versions age.
- **Do not override the User-Agent.** `curl_cffi` supplies a User-Agent,
  `Sec-Ch-Ua` and `Accept-Encoding` matching the handshake it negotiates.
  Replacing the User-Agent contradicts the TLS profile, which is a louder
  signal than sending nothing. Only the XHR headers the subito app itself adds
  are layered on top.


## Tests

```bash
python -m unittest discover -s tests -t . -q
```

Fully offline — no network, no API keys.
