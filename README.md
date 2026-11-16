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

## Resume Overlap

Click **Upload resume** and choose a PDF, Word (.docx) or text file. Each
product role then gets a percentage showing how well its job description
matches your resume. Click a percentage to see the breakdown. Your resume is
stored only in `data/illuminate.db` on this computer; its text is sent to
Anthropic's API for scoring, and nowhere else.

Scoring uses Claude, so it needs an Anthropic API key (this is separate from a
Claude subscription and is billed per use):

1. Create a key at https://console.anthropic.com/settings/keys and add credit.
2. In this folder, create a file named `.env` containing one line:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. Restart the app. The `.env` file is ignored by git, so the key is never pushed.

How it works:

- A new job appears in the table straight away with "In progress"; its
  percentage follows a few seconds later.
- Each job is scored once per resume and the result is stored. Uploading a new
  resume clears the scores and scores every job again.
- Claude rates six things from 0 to 100 and the percentage is their weighted
  average: role and responsibilities (25%), experience and seniority (20%),
  skills and tools (20%), domain and industry (15%), product and company
  context (10%), and stated requirements such as education or location (10%).
  Anything the posting does not mention is left out rather than counted as
  zero. If the posting states a hard requirement your resume clearly fails,
  the percentage is capped at 55.
- The model is set in `config.toml` under `[scoring]`. The default,
  `claude-opus-5-5`, costs roughly 3 to 5 US cents per job; the file lists
  cheaper options.

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
| `illuminate/service.py` | The 15-minute cycle and the scoring worker |
| `illuminate/scoring.py` | The Resume Overlap rubric and the call to Claude |
| `illuminate/resume.py` | Reading text from an uploaded resume |
| `illuminate/web.py`, `illuminate/static/index.html` | The page |

The LinkedIn approach follows [JobSpy](https://github.com/speedyapply/JobSpy) (MIT licence).
