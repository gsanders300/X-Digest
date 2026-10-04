# AGENTS.md

## What this is

A single-file cron-style job: scrape posts since the last run from the X accounts in
`handles.txt` via an Apify actor, de-duplicate against remembered post IDs, render
`template.html` with Jinja2, and email the digest over Gmail SMTP. The scan window slides
automatically: run it every 6 hours and each run covers the 6 hours since the previous one.
No web service, no linter config. The single-file layout is final — do not re-introduce a
package structure.

This is the **public** code repo. `handles.txt` here is an example list. The owner's real
handle list, secrets, and live workflow live in a private repo that checks this repo out
into `app/` at a pinned release tag and runs `uv run app/digest.py` from its own root
(README "Keep your handle list private"). Never commit personal handles, email addresses,
dataset IDs, or secrets here.

## Commands

```bash
uv run --env-file .env digest.py                 # local full run (scrapes = costs Apify credits)
uv run --env-file .env digest.py --dataset-id ID # reuse an existing Apify dataset, no scrape
uv run --env-file .env digest.py --dataset-id ID --window-date 2026-09-19  # replay, pin the UTC day
uv run --with "apify-client>=1.8.0" --with "jinja2>=3.1.0" --with "tzdata>=2024.1" \
  python -m unittest discover -s tests           # run the test suite
```

- `--env-file .env` is required locally: `digest.py` reads `os.environ` only and has no
  dotenv loading. CI supplies the same vars from GitHub secrets instead.
- `digest.py` uses PEP 723 inline script metadata (`apify-client`, `jinja2`, `tzdata`). `uv run` builds
  an isolated env from that header — the project `.venv`, `pyproject.toml` deps and `uv.lock`
  are **not** used by it. Adding a dependency means editing the header block at the top of
  `digest.py`, not `pyproject.toml`.
- Prefer `--dataset-id` for any iteration on parsing/rendering/email. Every scrape hits a
  pay-per-result actor (`kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest`).
- `--dataset-id` **without** `--window-date` skips window filtering entirely (a replayed
  dataset may cover any period); dedup and the author allowlist still apply. Pass
  `--window-date YYYY-MM-DD` to force filtering to one UTC day.

## Env vars

`APIFY_TOKEN`, `GMAIL_USER`, `GMAIL_APP_PASS`, `RECIPIENT_EMAIL` (all required; validated up
front). Optional: `HANDLES_FILE`, `TEMPLATE_PATH`, `DISPLAY_TZ` (IANA name, default
`America/New_York`;
bad names fall back to UTC). GitHub secret names match exactly. `GMAIL_APP_PASS` is a Gmail
app password, not the account password. `HANDLES_FILE` resolves against the working
directory (user config); `TEMPLATE_PATH` defaults to `template.html` beside `digest.py`
(ships with the code). Keep that split: the private repo depends on it.

## State and de-duplication

All state lives in a **named Apify key-value store**, not in the repo: store
`x-digest-state`, keys `seen_post_ids` and `run_metadata` (`digest.py`). Consequences:

- `run_metadata` holds the last successful run's `window_start`/`window_end` and
  `job_started_at`/`job_completed_at` (ISO-8601 UTC). Each sliding run scans from the stored
  `window_end` minus a 10-minute overlap (`WINDOW_OVERLAP`, absorbed by dedup — it covers X
  search indexing lag at the boundary) up to its own start time (`resolve_scan_window`).
  Missing/malformed metadata = first run = a 24-hour lookback (`FIRST_RUN_LOOKBACK`).
- The metadata advances on **every clean sliding run, including zero-post runs** — otherwise
  the same interval would be re-scanned forever. It does **not** advance on scrape errors,
  failed sends, or replay/pinned runs (`--dataset-id`/`--window-date`), so a failed run's
  interval is automatically re-scanned by the next run.
- Re-running after a successful send produces a "No New Posts" email covering a short window
  — the second run is not a no-op bug. To force a resend of an interval, clear or edit the
  `run_metadata` record (and if needed `seen_post_ids`) in the Apify console.
- A **missing** record is a fresh start; an **API failure** while reading either record
  raises `StateStoreError` and the run fails fast *before* the scrape: an error-banner email
  goes out, exit 1, no Apify credits burned, and the stored state is never overwritten with a
  partial list. Do not "fix" this back to silently returning `[]`.
- Seen IDs are only persisted when `send_email` returned `True` **and** there was at least
  one new post, so a failed send does not burn the IDs. If the send succeeded but a state
  save failed, the run logs it and exits 1 (the next digest may repeat posts or re-scan the
  window — that is the accepted trade-off, duplicates over losses).
- The ID list preserves insertion order and is pruned to the newest 1000 IDs.

## Scraping details worth knowing

- Handles are batched 8-per-search-term into
  `from:a OR from:b ... -filter:replies since_time:<epoch> until_time:<epoch>` — unix-epoch
  operators, precise to the second. This is deliberate on a pay-per-result actor: date-only
  `since:`/`until:` would re-fetch (and re-bill) the same day's posts on every 6-hour run.
- `maxItems` scales with handle count **and** window length:
  `max(30, ceil(5 * handles * window_hours / 24))`, capped at 3 days' worth
  (`max_items_budget`) so a long outage gap cannot explode the cost ceiling. When a scrape
  we triggered returns a full cap of items, a yellow truncation warning is added to the
  email. The cap is also the worst-case cost ceiling per run.
- The actor call carries a 600 s platform-side run timeout and a 660 s client wait. The
  timeout kwargs differ by client version (`timeout_secs`/`wait_secs` in 1.x/2.x,
  `run_timeout`/`wait_duration` timedeltas in 3.x), so `trigger_apify_scrape` picks them by
  inspecting the installed `call` signature — keep that when touching the call.
- `handles.txt` is one handle per line; leading `@` and `#` comment lines are stripped.
- Actor items are shape-unstable, hence the chained `.get()` fallbacks for id/author/text/url.
  Keep that defensive style when editing `parse_post_items`.
- `parse_post_items` returns `(posts, stats)`; `stats` counts drops by reason. Unmonitored-
  author and bad-timestamp drops become email warnings via `drift_warnings` (they usually mean
  the actor's output shape changed); mock/outside-window drops stay stdout-only.
- Items whose text mentions `from kaitoeasyapi` / `mock data`, or authored by `kaitoeasyapi`,
  are dropped on purpose: they are the actor's billing/mock placeholders.
- Items whose text is nothing but emoji once URLs and whitespace are removed
  (`is_emoji_only`) are dropped as no-commentary forwards (stdout-only `emoji_only` stat).

## Email pipeline

- The job **always sends** an email: real digest, "no new posts" notice, or a red scraper-error
  banner. Scrape/fetch failures are caught in `main` and reported in the mail, not raised.
- `main` exits 1 when the send failed, a scrape error occurred, the state store was
  unreadable, or the post-send state save failed — so GitHub Actions turns red even though
  the mail went out.
- `send_email` returns `bool` and retries 3 times with exponential backoff, except on
  `SMTPAuthenticationError` (fail fast — a bad app password never fixes itself).
- The subject carries the scanned interval in `DISPLAY_TZ` (e.g.
  `X Digest - 4 New Posts (Sep 20 06:00-12:00 EDT)`), the header shows a
  `Posts scanned: Sep 20, 6:00 AM - 12:00 PM EDT (6.0 h)` line plus job
  `Started`/`Completed` times. Pinned-day runs label with the date, unfiltered replays with
  `replay` / "No window filter (replayed dataset)".
- Posts render grouped by author, author order shuffled every run, oldest post first inside a
  group. `group_posts_by_author` builds that structure; the template iterates `author_groups`,
  not `posts` (`posts` is still passed, but only for counts/empty checks).
- Template context: `posts`, `author_groups`, `today_str`, `today_display`, `scrape_error`,
  `warnings` (a **list** of strings — cap hit, shape drift, usage fetch failure, first-run
  notice), `usage`, `started_str`, `completed_str`, `window_str` (the "Posts scanned"
  interval). `build_plain_digest` mirrors the HTML — change both or the text part silently
  drifts.
- Apify cost footer comes from `client.user().limits()`: `current.monthly_usage_usd` and
  `limits.max_monthly_usage_usd`. apify-client 3.x returns pydantic models, older 1.x/2.x
  returns plain camelCase dicts, and the inline metadata allows `>=1.8.0` — hence the `_field`
  helper. A failure here is non-fatal: it becomes a yellow warning entry.
- Post timestamps come from unstable actor keys (`createdAt`/`created_at`/`date`/`timestamp`),
  so `parse_post_date` handles epoch numbers, ISO-8601, and the legacy
  `Fri Sep 19 09:15:00 +0000 2025` form, and returns `None` rather than raising.

## CI

`.github/workflows/digest.yml` triggers on `repository_dispatch: apify-digest-ready`
(payload `client_payload.dataset_id`) and manual `workflow_dispatch`. There is deliberately
no `schedule:` block — an **external tool** fires the cron; do not add one. The workflow has
a `concurrency` group (queued, not cancelled — overlapping runs race the shared state
store), a 20-minute job timeout, and passes the dataset ID via the `DATASET_ID` env var
rather than inline `${{ }}` interpolation in the script (shell-injection guard — keep it
that way).

`.github/workflows/tests.yml` runs `uv run digest.py --help` and the unittest suite on
Python 3.11 and 3.14 for each push to `main` and each pull request. It needs no secrets.

## Releases

Semantic versioning; `version` in `pyproject.toml` must equal the latest `vX.Y.Z` tag. Follow
the README "Releases" steps: bump the version, `uv lock`, push, wait for Tests to pass, then
`gh release create vX.Y.Z --generate-notes`. The private runner only picks up a release when
its `ref:` is bumped.

## Verification

- `uv run digest.py --help` must still resolve the script env after editing the inline
  metadata.
- Run the unittest suite (command above); `tests/test_digest.py` imports `digest` from the
  repo root, so run it from there.
- The realistic end-to-end check is a `--dataset-id` run against a known dataset and
  inspecting the sent email. No formatter or type checker is configured.
