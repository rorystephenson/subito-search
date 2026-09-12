# subito-search

Self-hosted alerts for [subito.it](https://www.subito.it) with an LLM filter.

Subito's own saved-search alerts are noisy — its search matches loosely, so you
get pinged about things you don't want. This runs your searches on a schedule,
asks an LLM whether each new listing actually matches a plain-English description
of what you're after, and sends only the survivors to your Telegram bot.

Per search you configure: **the query and filters**, **a prompt describing your
interest**, and **how often to run**.

## How it works

```
subito JSON API  →  cheap local filters  →  LLM interest check  →  Telegram
                    (price, keywords,        (Gemini free tier)
                     already-seen)
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

### 4. Edit `searches.yaml`

```yaml
schedule:
  timezone: Europe/Rome
  active_hours: "08:00-22:00"   # end exclusive: last run starts before 22:00
  # days: [mon, tue, wed, thu, fri]

defaults:
  interval_minutes: 60
  max_pages: 2

searches:
  - name: bici-da-corsa
    query: "bici da corsa"
    interval_minutes: 30
    filters:
      price_min: 200
      price_max: 600
      shippable: true
    exclude_keywords: [bambino, ricambi]
    prompt: >
      A complete road bike in frame size 54-56, Shimano 105 or better.
      Not interested in frames alone, spare parts, wheels, or mountain bikes.
```

### Scheduling

`searches.yaml` is the **single source of truth** for when the bot runs. The
`schedule` block sets the window in *local* time; each search's
`interval_minutes` sets how often it runs inside that window.

GitHub's cron is UTC-only and has no DST awareness, so the workflow's cron is
**generated** from this config rather than written by hand:

```bash
python -m subito_alerts.main --sync-schedule
```

Run that after changing `schedule` or any `interval_minutes`. A test fails if the
two drift apart, and `--check` reports it, so it can't silently rot.

Two mechanisms combine:

- The generated cron is a *superset* of your window — wide enough to cover both
  DST offsets (for Rome, `08:00-22:00` local becomes `*/15 6-20 * * *` in UTC).
- Each run then checks the **real local time** and exits immediately if it's
  outside the window. That's what makes the window actually correct year-round;
  the cron only decides when to wake up.

The cost is about four wasted wake-ups a day at the DST edges — the price of
cron having no timezone support.

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

`exclude_keywords` is a local reject list checked against title and description
before the LLM runs. Use it for words that are *always* wrong — it saves quota.

## Running it

```bash
# See what it would do, without sending anything. State is not saved.
python -m subito_alerts.main --dry-run --ignore-interval

# Tune the filters without spending LLM calls
python -m subito_alerts.main --dry-run --ignore-interval --no-classify

# For real
python -m subito_alerts.main
```

| Flag | |
|---|---|
| `--dry-run` | print instead of sending; don't save state |
| `--ignore-interval` | run every search regardless of when it last ran |
| `--search NAME` | run just one search (repeatable) |
| `--no-classify` | skip the LLM pass entirely |
| `--check` | verify config, schedule and credentials, then exit |
| `--sync-schedule` | regenerate the workflow cron from `searches.yaml` |
| `--ignore-schedule` | run even outside the configured active hours |
| `--verbose` | debug logging |

### Tuning your prompt

Run `--dry-run --ignore-interval` and read the verdict lines — each shows
`MATCH`/`reject` and the model's one-line reason:

```
MATCH  350 €   Bici da corsa Cinelli — carbon frame, 105 groupset
reject 230 €   Freni Campagnolo super record — brake parts, not a bike
```

If something is wrongly rejected, the reason usually tells you which clause of
your prompt to soften. Iterate here before letting it run unattended.

## Running on GitHub Actions

`.github/workflows/alerts.yml`'s cron is generated from `searches.yaml` — see
[Scheduling](#scheduling). Don't edit it by hand; run `--sync-schedule`.

1. Push this repo to GitHub. **Make it private** — `searches.yaml` says what
   you're hunting for and what you'll pay.
2. Settings → Secrets and variables → Actions → add `GEMINI_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
   Optionally add a repo *variable* `GEMINI_MODEL` to override the model.
3. Actions tab → **Subito alerts** → **Run workflow** to test it.

Running on Actions also means requests come from varied runner IPs rather than
all from your home connection.

### Cost

Actions is free and unlimited on **public** repositories. On **private** ones you
get 2,000 minutes/month on the Free plan (3,000 on Pro/Team), then $0.006/min.
Each run is rounded up to a whole minute, so cost tracks the number of runs, not
their length:

| Schedule | Runs/month | Private, Free plan |
|---|---|---|
| every 15m, 08:00–22:00 Rome | ~1,825 | free, ~9% headroom |
| every 15m, all day | ~2,880 | ~880 min over → ~$5.30/mo |
| every 30m, all day | ~1,440 | free |

Restricting the active window is what keeps a 15-minute interval inside the free
tier — round-the-clock at `*/15` would not fit. `--check` prints the generated
cron so you can sanity-check the run count before pushing.

Private is the recommended setup: free at this schedule, and your shopping list
stays your business. If you do make the repo public, note that **Actions logs are
public too** — they show every ad title, price and classifier verdict, i.e. what
you're hunting and what you'll pay. Secrets themselves stay safe: GitHub encrypts
them and masks them in logs, and this code additionally scrubs the bot token out
of Telegram error messages (Telegram puts the token in the URL path, so a bare
connection error would otherwise print it).

### State, and why duplicates are unlikely

Which ads have already been alerted on is kept in `state.json`, carried between
runs in the Actions cache. Caches are evicted after 7 days without use, so on a
cache miss the bot does **not** treat the whole first page as new — it only looks
back `interval_minutes × 2` (capped at 24h). Losing the cache on a 30-minute
search therefore costs you a duplicate alert or two, not a flood.

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
- **Ad pages themselves are bot-blocked** (HTTP 403), but the JSON search
  endpoint is not. Everything here goes through the latter.

## Tests

```bash
python -m unittest discover -s tests -t . -q
```

Fully offline — no network, no API keys.
