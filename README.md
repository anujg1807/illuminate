# Illuminate

Illuminate finds Product Manager jobs posted on LinkedIn across India and shows
them in one table, newest first. It checks LinkedIn every 15 minutes and the
table updates itself.

## Run it

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (it
installs Python and everything else for you). Then, in this folder:

```bash
uv run illuminate
```

The page opens at http://localhost:8000. Leave the terminal window open; press
Ctrl+C in it to stop. Checks only happen while the app is running and the
computer is awake. When it starts again it searches back far enough to cover
the time it was off (up to seven days, by keyword search).

The job list is stored in `data/illuminate.db`. Delete that file to start over.

## What it does

1. **Keyword searches.** It searches LinkedIn's public, logged-out job search
   for each phrase in `config.toml` ("product manager", "product management",
   "product owner", "head of product") and merges the results.
2. **Full scan.** LinkedIn's keyword search skips some jobs whose title plainly
   says "Product Manager". So every check also reads the title of every job
   newly posted in India (about 300 an hour on a quiet evening) and keeps the
   product roles.
3. **Sorting titles.** Each title gets one of three labels:
   - *Product Manager*: the title clearly says product management, including
     indirect forms such as "Head of Product" or "Senior Manager, Product",
     and "Product Owner" titles.
   - *Possibly*: ambiguous titles such as "APM", "PM", or a pharma
     "Product Manager" (usually brand marketing).
   - *Other roles*: project managers, product designers, engineers and so on.
     Hidden by default; shown with the "Other roles" button.
4. **Posting time.** LinkedIn never publishes an exact posting time, only
   "49 minutes ago". Illuminate works one out: for a job under an hour old the
   label is accurate to the minute. For an older job it asks LinkedIn whether
   the job appears in "the last N seconds" for different N, which brackets the
   posting time to the minute in about eight requests. Until that is done the
   time is shown with "≈".

## Settings

Edit `config.toml` and restart. You can change the location, the search
phrases, how often it checks, how far back the first run reaches, and how fast
it sends requests.

## Limits to know about

- It uses only pages LinkedIn shows to visitors who are not signed in. No
  LinkedIn account is used. Automated reading is still against LinkedIn's terms
  of use, and LinkedIn may throttle or block the computer's internet address if
  it sends too many requests. The app paces itself and backs off when told to.
- The full scan only reaches back one hour, so on the first run (and after the
  app has been off for more than an hour) older jobs come from keyword search
  alone and a few may be missed.
- LinkedIn returns at most 1,000 results per search. If a single check ever
  finds more new jobs than that across India, the page says so.

## Develop

```bash
uv run pytest
```

| File | What it holds |
|---|---|
| `illuminate/linkedin.py` | Reading LinkedIn's public job search, pacing, back-off |
| `illuminate/classifier.py` | The title rules |
| `illuminate/timing.py` | Working out posting times |
| `illuminate/store.py` | The SQLite database |
| `illuminate/service.py` | The 15-minute cycle |
| `illuminate/web.py`, `illuminate/static/index.html` | The page |

The LinkedIn approach follows [JobSpy](https://github.com/speedyapply/JobSpy) (MIT licence).
