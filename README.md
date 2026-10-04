# X-Digest

[![Tests](https://github.com/gsanders300/X-Digest/actions/workflows/tests.yml/badge.svg)](https://github.com/gsanders300/X-Digest/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/gsanders300/X-Digest)](https://github.com/gsanders300/X-Digest/releases/latest)
[![License: MIT](https://img.shields.io/github/license/gsanders300/X-Digest)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

X-Digest sends one email with posts from a list of X accounts. The job collects posts
from a sliding window. The window starts where the previous run stopped and ends at the
start time of the current run. The job removes posts that it sent before. It groups the
new posts by author and sends the result through Gmail.

The email also shows the current monthly Apify usage when that data is available.

This document uses ASD-STE100 Simplified Technical English.

## Highlights

- **Sliding scan window.** Each run scans the time since the previous run. A missed run
  loses no posts, and the scheduler can use any interval.
- **Cost control.** The search queries have time bounds that are precise to the second,
  and an item budget limits each paid scrape.
- **Safe state.** If the program cannot read its state, it stops before the paid scrape.
  It saves post IDs only after Gmail accepts the email.
- **Visible failures.** Each run sends a digest, a no-posts notice, or an error report. A
  failed run exits with code 1, so the GitHub Actions run fails.
- **Tolerant parser.** The parser accepts many field names and timestamp formats from the
  scraper. It counts the items that it removes and warns about format changes.
- **Tests.** No-network unit tests run in GitHub Actions on Python 3.11 and 3.14.

## Current status

The program is a single Python script: `digest.py`. This single-file layout is final.

The program operates locally and in GitHub Actions. The GitHub Actions workflow does not
have a time schedule. An external scheduler starts the workflow. You can also start the
workflow manually or send a `repository_dispatch` event.

Each release has a version tag. A private repository can run a release of this code with
its own handle list. See [Keep your handle list private](#keep-your-handle-list-private).

The Apify actor charges for its results. Use an existing dataset when you test changes.

## Quick start

### Requirements

You must have these items:

- Python 3.11 or a later version
- [uv](https://docs.astral.sh/uv/)
- An Apify account and an API token
- A Gmail account with two-step verification
- A Gmail app password

A Gmail app password is not your Gmail account password. Create an app password in the
security settings of your Google account.

### Configuration

1. Create a file named `.env` in the repository root.
2. Add these values:

```dotenv
APIFY_TOKEN=your_apify_token
GMAIL_USER=you@gmail.com
GMAIL_APP_PASS=your_gmail_app_password
RECIPIENT_EMAIL=recipient@example.com
```

3. Replace the example handles in `handles.txt`. Put one X handle on each line.
4. Run the job:

```powershell
uv run --env-file .env digest.py
```

This command starts a paid Apify actor run. The command waits for the actor, reads the
result dataset, and sends an email. The first run scans the previous 24 hours. Each
subsequent run scans the time since the previous run.

## Safe test run

Use an existing dataset to prevent a new actor charge:

```powershell
uv run --env-file .env digest.py --dataset-id DATASET_ID
```

Replace `DATASET_ID` with the ID of a completed Apify dataset.

The program still sends an email. It also reads the shared state store and can add new
post IDs to it. A replay run does not apply a time filter and does not move the scan
window forward. Duplicate protection still applies, so an old dataset can produce a
`No New Posts` email.

Add `--window-date` to limit a replay to one UTC day:

```powershell
uv run --env-file .env digest.py --dataset-id DATASET_ID --window-date 2026-09-19
```

## How the program operates

The program does these steps:

1. It validates the four required environment variables.
2. It opens the Apify key-value store named `x-digest-state`.
3. It reads the IDs of posts that it sent before.
4. It reads the scan metadata of the previous run.
5. It calculates the scan window.
6. It reads the monitored handles and calculates the item budget.
7. It starts the Apify actor, or it uses the dataset from `--dataset-id`.
8. It reads all items from the dataset.
9. It removes invalid, unrelated, out-of-window, and duplicate items.
10. It reads the monthly Apify usage.
11. It creates a plain-text email part and an HTML email part.
12. It sends the email through Gmail SMTP.
13. It saves the new post IDs and the new scan metadata after Gmail accepts the email.

The program uses this actor:

```text
kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest
```

The actor runs synchronously with 1024 MB of memory. The actor run stops after 600
seconds. The program waits a maximum of 660 seconds for the actor.

## Scan window

The scan window uses UTC. The window has three modes.

### Sliding mode (default)

A run without command options uses the sliding mode. The window starts at the end of the
previous scan window, minus a 10-minute overlap. The window ends at the start time of the
current run. The overlap covers the X search index delay at the window boundary.
Duplicate protection removes the posts that the overlap collects again.

Run the job at any interval. Each run covers the time since the previous run. A run every
6 hours covers 6 hours. A missed run does not lose posts: the next run covers the full
gap.

When the state store has no scan metadata, the run is a first run. A first run scans the
previous 24 hours and adds a notice to the email.

The window moves forward only after a clean run. A clean run sent the email and had no
collection error. A run with zero new posts is a clean run and moves the window forward.
A failed run does not move the window, and the next run scans the same interval again.

### Pinned mode

The `--window-date` option sets the window to one UTC day:

```text
2026-09-19 00:00:00 UTC <= post time < 2026-09-20 00:00:00 UTC
```

A pinned run does not move the scan window forward.

### Replay mode

The `--dataset-id` option without `--window-date` applies no time filter. Duplicate
protection and the author filter still apply. A replay run does not move the scan window
forward.

### Search query bounds

The program adds Unix epoch bounds to each X search query:

```text
since_time:1789836236 until_time:1789922636
```

These bounds are precise to the second. A short window does not collect posts from
earlier in the same day again. This precision prevents repeated charges for the same
posts on the pay-per-result actor.

The program also applies the same bounds after it reads the dataset. This second check
prevents an actor result outside the window from entering the email.

The scan window does not use `DISPLAY_TZ`.

## Item budget

The program calculates a maximum item count for each scrape:

```text
maxItems = max(30, ceil(5 * handles * window_hours / 24))
```

The budget has an upper limit of three days of items (`15 * handles`). A long gap between
runs cannot make the cost ceiling too large. When a scrape returns the full budget, the
email shows a yellow truncation warning.

Example budgets for 12 handles:

| Window | Budget |
| --- | --- |
| 6 hours | 30 (minimum) |
| 24 hours | 60 |
| 10 days | 180 (three-day limit) |

## Display time

The default display time zone is `America/New_York`. The email shows EST or EDT as
applicable. The subject interval, the scanned window, the job times, and the post times
use this display time zone.

Set `DISPLAY_TZ` to another IANA time zone name if necessary. If the name is not valid,
the program uses UTC and writes a notice to the log.

## Post filters

The program sends an item only when all these conditions are true:

- The item has a post ID.
- The ID is not in the state store.
- The author is in `handles.txt`.
- The timestamp is valid.
- The timestamp is in the scan window. (Replay mode does not apply this condition.)
- The item is not an actor test or billing item.

The program removes items that contain `from kaitoeasyapi` or `mock data`. It also
removes items from the author `kaitoeasyapi`. The actor makes these placeholder items
when a query has no results.

The program counts the removed items by reason. Removals for an unknown author or an
invalid timestamp add a yellow warning to the email. These removals usually show a change
in the actor result format.

The actor result format is not stable. The program accepts multiple field names for the
post ID, author, text, URL, links, and timestamp. It accepts ISO 8601 timestamps, Unix
timestamps in seconds or milliseconds, and the legacy X timestamp format.

## Handle file

The default handle file is `handles.txt`.

Use this format:

```text
# AI researchers
sama
@karpathy
ylecun
```

The program does these actions:

- It removes white space at the start and end of each line.
- It removes a leading `@` character.
- It ignores empty lines.
- It ignores lines that start with `#`.
- It stops with an error if the file does not exist or has no handles.

The program puts a maximum of eight handles in one search term. It creates more search
terms when the file has more than eight handles. The item budget applies to all search
terms together.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `APIFY_TOKEN` | Yes | None | Authenticates with Apify. |
| `GMAIL_USER` | Yes | None | Sets the Gmail account and the email sender. |
| `GMAIL_APP_PASS` | Yes | None | Authenticates with Gmail SMTP. |
| `RECIPIENT_EMAIL` | Yes | None | Sets the email recipient. |
| `HANDLES_FILE` | No | `handles.txt` | Sets the path of the handle file. |
| `TEMPLATE_PATH` | No | `template.html` in the folder of `digest.py` | Sets the path of the Jinja2 HTML template. |
| `DISPLAY_TZ` | No | `America/New_York` | Sets the time zone for email dates and times. |

`digest.py` reads the process environment. It does not load `.env` directly. Use
`uv run --env-file .env` for a local run. GitHub Actions supplies the required values
from repository secrets.

Do not commit `.env`. Do not put an API token or a Gmail app password in a tracked file.

## Command reference

### Show help

```powershell
uv run digest.py --help
```

This command does not start the actor or send an email.

### Start a full run

```powershell
uv run --env-file .env digest.py
```

This command starts the paid actor, scans the sliding window, and sends an email.

### Use an existing dataset

```powershell
uv run --env-file .env digest.py --dataset-id DATASET_ID
```

This command does not start the actor. It applies no time filter. It sends an email and
can add post IDs to the state store. It does not move the scan window.

### Use an existing dataset with one UTC day

```powershell
uv run --env-file .env digest.py --dataset-id DATASET_ID --window-date 2026-09-19
```

This command limits the replay to posts from the given UTC day.

## Email content

The program sends an email on each normal run. It sends one of these results:

- A digest with new posts
- A `No New Posts` notice
- A scraper error report

The email contains these parts:

| Part | Condition |
| --- | --- |
| Blue header with date, job times, and scanned window | Always |
| Red scraper-error banner | A collection step failed |
| Yellow warning banner | One or more warnings exist |
| Grey no-post notice | There are no new posts and no scraper error |
| Author sections | There are new posts |
| Apify usage bar | Apify usage data is available |

The header shows the job start time, the job completion time, and a `Posts scanned` line
with the scanned interval and its length. Example:

```text
Posts scanned: Sep 20, 6:00 AM - 12:00 PM EDT (6.0 h)
```

The yellow warning banner can contain these entries:

- The scrape returned the full item budget. Posts can be missing.
- Items were removed for an unknown author or an invalid timestamp.
- The Apify usage data is not available.
- The run was a first run with a 24-hour window.

The program puts all posts from one author in one section. It puts the oldest post first
in each section. It changes the sequence of author sections on each run.

The usage bar has these colors:

| Color | Monthly usage |
| --- | --- |
| Blue | Less than 70 percent of the limit |
| Orange | From 70 percent to less than 90 percent of the limit |
| Red | 90 percent of the limit or more |

The subject shows the scanned interval in the display time zone:

```text
X Digest - 3 New Posts (Sep 20 06:00-12:00 EDT)
X Digest - No New Posts (Sep 20 12:33-12:45 EDT)
X Digest - Scraper Error (Sep 20 06:00-12:00 EDT)
```

A pinned run shows the date, for example `(2026-09-19)`. A replay run shows `(replay)`.

## Email delivery

The program connects to `smtp.gmail.com` on port 587. It starts TLS before it sends the
Gmail credentials. The connection timeout is 30 seconds.

The program makes a maximum of three send attempts for an SMTP or network error. It waits
2 seconds before the second attempt and 4 seconds before the third attempt.

The program does not retry an authentication error. A different attempt cannot correct
an invalid app password.

## State and duplicate posts

The program stores its state in Apify. It uses these values:

| Item | Value |
| --- | --- |
| Key-value store | `x-digest-state` |
| Record key 1 | `seen_post_ids` |
| Record value 1 | A list of post IDs, maximum 1000 |
| Record key 2 | `run_metadata` |
| Record value 2 | The last window and job times, ISO 8601 UTC |

The `run_metadata` record has these keys: `window_start`, `window_end`,
`job_started_at`, and `job_completed_at`.

The ID list keeps insertion order. When the list has more than 1000 IDs, the program
removes the oldest IDs.

The program saves new IDs only after Gmail accepts an email that has new posts. A failed
email does not use the IDs. The program saves new scan metadata after each clean sliding
run, including a run with zero new posts.

A missing record is a fresh start. A read failure from the Apify API is different: the
program stops before the scrape, sends an error email, and exits with code 1. This rule
prevents duplicate posts and prevents loss of the saved state.

If a state save fails after a sent email, the program writes an error and exits with
code 1. The next run can send some posts again or scan the same window again. Duplicates
are the accepted result; lost posts are not.

To send an interval again, delete or edit the `run_metadata` record in the Apify
console. Also delete the `seen_post_ids` record when the posts of that interval must
appear again.

## Errors and exit codes

The program catches an error in handle loading, actor execution, dataset loading, or post
parsing. It then sends an email with a red error banner.

The program treats an Apify usage error as a warning. It sends the email without the
usage bar and adds a yellow warning entry.

| Condition | Exit code |
| --- | --- |
| Email sent, no scraper error, state saved | `0` |
| Scraper error, including when the error email was sent | `1` |
| Email build or delivery failure | `1` |
| State store read failure | `1` |
| State save failure after a sent email | `1` |
| Missing required environment variable | Nonzero |

GitHub Actions marks a run as failed when the exit code is not zero.

## GitHub Actions

The workflow file is `.github/workflows/digest.yml`. It supplies these secrets:

- `APIFY_TOKEN`
- `GMAIL_USER`
- `GMAIL_APP_PASS`
- `RECIPIENT_EMAIL`

Add each secret in the repository settings before you run the workflow.

The workflow has these protections:

- A concurrency group. A new run waits for the active run. Two runs cannot use the state
  store at the same time.
- A 20-minute job timeout.
- The dataset ID goes to the script through the `DATASET_ID` environment variable. The
  workflow does not put the value in the shell command text.

The workflow has two triggers.

### Manual trigger

Start `Send Daily X Digest` from the Actions page. You can supply an optional dataset ID.
If you do not supply an ID, the workflow starts a paid actor run with the sliding window.

### Repository dispatch trigger

Send a `repository_dispatch` event with this type:

```text
apify-digest-ready
```

Use this payload when an existing actor run supplies a dataset:

```json
{
  "event_type": "apify-digest-ready",
  "client_payload": {
    "dataset_id": "DATASET_ID"
  }
}
```

The workflow does not have a `schedule` trigger, and this is intentional. An external
scheduler starts the workflow at the desired interval. The sliding window adapts to the
interval automatically. Do not add a `schedule` block.

## Keep your handle list private

If you fork this public repository, your `handles.txt` is public too. To keep the list
private, use two repositories:

- This public repository has the code.
- A private repository has your `handles.txt`, the workflow file, and the secrets.

The workflow in the private repository gets a release of the code from this repository
when it runs. The program reads `handles.txt` from the working directory, which is the
root of the private repository. It reads `template.html` from the folder of `digest.py`.

1. Make a private repository. Put your `handles.txt` in its root folder.
2. Copy `.github/workflows/digest.yml` into the private repository.
3. In the workflow, add a second checkout step after the first one. Then run the script
   from the `app` folder, as this example shows:

   ```yaml
   - name: Check out repository
     uses: actions/checkout@v4

   - name: Check out X-Digest code
     uses: actions/checkout@v4
     with:
       repository: gsanders300/X-Digest
       ref: v1.0.0
       path: app

   - name: Install uv
     uses: astral-sh/setup-uv@v5
     with:
       enable-cache: true

   - name: Run digest script
     run: uv run app/digest.py
   ```

   Also change `digest.py` to `app/digest.py` in the `--dataset-id` command.
4. Add the four secrets to the private repository (see [GitHub Actions](#github-actions)).

The `ref` value pins a release. A change to the `main` branch of this repository does not
change the private digest. To use a new release, change `ref` to the new tag.

To run the job on your computer, clone both repositories into the same parent folder. Put
your `.env` file in the private folder, and run the script from that folder:

```powershell
uv run --env-file .env ../X-Digest/digest.py
```

## Releases

The project uses semantic versioning. Each release has a tag, for example `v1.0.0`, and a
[GitHub release](https://github.com/gsanders300/X-Digest/releases). The version in
`pyproject.toml` is the same as the latest tag.

To make a release:

1. Change `version` in `pyproject.toml`.
2. Run `uv lock`. This command writes the new version to `uv.lock`.
3. Commit the two files and push to `main`. Make sure that the **Tests** workflow passes.
4. Run `gh release create vX.Y.Z --generate-notes`. This command makes the tag and the
   GitHub release. The release notes list the changes since the previous release.
5. In each private repository that runs this code, change `ref` to the new tag.

## Tests and verification

The test suite uses the Python standard `unittest` module. The tests do not call Apify or
Gmail.

The **Tests** workflow (`.github/workflows/tests.yml`) runs the tests and the metadata
check on each push to `main` and on each pull request. It uses Python 3.11 and 3.14. It
needs no secrets.

Run the tests from the repository root:

```powershell
uv run --with "apify-client>=1.8.0" --with "jinja2>=3.1.0" --with "tzdata>=2024.1" python -m unittest discover -s tests -v
```

The tests verify these functions:

- Scan window calculation: normal, first run, and future metadata
- Run metadata storage and validation
- Search query epoch bounds
- Item budget scaling and limits
- EST and EDT display
- Window interval and subject formatting
- Long email-header date formatting
- Eastern start and completion times
- Source-style HTML layout
- Template lookup from another working directory
- Millisecond timestamp parsing
- Post filtering and removal counts
- State order, pruning, and read-failure behavior
- Empty handle file validation

Run this metadata check after a dependency change:

```powershell
uv run digest.py --help
```

For an integration check, replay a known dataset. This check sends an email and can add
post IDs to the state store:

```powershell
uv run --env-file .env digest.py --dataset-id DATASET_ID
```

Inspect the subject, the scanned window, the post count, the author groups, the post
times, the warning banners, and the usage bar in the received email.

The repository does not have a formatter, a linter, or a type checker.

## HTML template

`template.html` is a Jinja2 template. The program supplies this context:

| Name | Type | Content |
| --- | --- | --- |
| `posts` | List | The accepted new posts |
| `author_groups` | List | The accepted posts grouped by author |
| `today_str` | String | The digest date in `YYYY-MM-DD` format |
| `today_display` | String | The digest date in long format |
| `scrape_error` | String or `None` | The collection error |
| `warnings` | List of strings | The nonfatal warnings |
| `usage` | Dictionary or `None` | The monthly Apify usage data |
| `started_str` | String or `None` | The display-zone job start time |
| `completed_str` | String or `None` | The display-zone email build time |
| `window_str` | String or `None` | The scanned interval with its length |

Each item in `posts` has these keys:

| Key | Content |
| --- | --- |
| `id` | The post ID |
| `author` | The X handle |
| `text` | The post text |
| `url` | The post URL |
| `links` | The unique links from the post |
| `created_at` | The parsed timestamp |
| `time_str` | The display-zone timestamp |

Each item in `author_groups` has `author`, `profile_url`, and `posts` keys.

The `usage` dictionary has `used_usd`, `limit_usd`, `percent`, `bar_pct`, and
`bar_color` keys.

The function `build_plain_digest` makes the plain-text email part. When you change the
HTML content, make the equivalent change in the plain-text content.

Jinja2 automatic escaping is active for the HTML template.

## Dependencies and project files

`digest.py` uses PEP 723 inline script metadata. This metadata contains the runtime
dependencies:

- `apify-client>=1.8.0`
- `jinja2>=3.1.0`
- `tzdata>=2024.1`

When you add a runtime dependency, add it to the metadata at the top of `digest.py`.
`uv run digest.py` does not use the dependency list in `pyproject.toml`. The
`pyproject.toml` file contains project metadata only.

The program supports apify-client versions 1.x through 3.x. The actor call and the field
access adapt to the installed version.

## File reference

| Path | Purpose |
| --- | --- |
| `digest.py` | Runs the complete digest job. |
| `handles.txt` | Lists the monitored X handles. The file has an example list. |
| `template.html` | Defines the HTML email. |
| `tests/test_digest.py` | Contains the no-network unit tests. |
| `.github/workflows/digest.yml` | Runs the job in GitHub Actions. |
| `.github/workflows/tests.yml` | Runs the tests in GitHub Actions. |
| `LICENSE` | Contains the MIT license. |
| `.env` | Contains local secrets. Git does not track this file. |
| `AGENTS.md` | Gives repository instructions to coding agents. |
| `pyproject.toml` | Contains project metadata only. |

## Troubleshooting

| Problem | Cause | Corrective action |
| --- | --- | --- |
| `Missing required environment variables` | One or more required values are empty. | Use `--env-file .env`, or add the GitHub secrets. |
| `Handles configuration file not found` | The handle file path is not correct. | Run the command from the folder that contains `handles.txt`, or set `HANDLES_FILE`. |
| `Handles configuration file is empty` | The file has no active handles. | Add at least one handle that is not a comment. |
| `Jinja2 template not found` | The template path is not correct. | Keep `template.html` in the folder of `digest.py`, or set `TEMPLATE_PATH`. |
| `State store unavailable` in the email | The Apify API rejected a state read. | Check the Apify token and the Apify status. The next clean run scans the missed interval. |
| `Apify actor completed without a default dataset ID` | The actor result has no dataset ID. | Examine the actor run in the Apify console. |
| The subject contains `No New Posts` with a short interval. | The run started soon after the previous run. | No action is necessary. The window moved forward. |
| `Gmail authentication failed` | Gmail rejected the user or app password. | Use the Gmail address and a valid app password. |
| `Email attempt 1/3 failed` | An SMTP or network error occurred. | Wait for the automatic retry. |
| The email has a red banner. | A collection step failed. | Read the banner and examine the actor or dataset. |
| The email has a yellow banner. | A warning occurred. | Read the warning entries. |
| The email warns about the item budget. | The scrape returned the full budget. | Posts can be missing. Increase the run frequency, or accept the limit. |
| The email warns about removed items. | The actor result format changed. | Examine the dataset items and update `parse_post_items`. |
| The log says `Ignored mock/billing item`. | The actor returned a placeholder item. | No action is necessary. |
| The log says `Ignored item outside the UTC digest window`. | The dataset contains a post outside the scan window. | No action is necessary for the overlap. Use `--window-date` for old datasets. |
| The actor log says `Failed to get user info, status code: 404`. | The actor could not read optional user information. | Check the final actor status. No action is necessary when the status is `SUCCEEDED` and a dataset exists. |
| The displayed time is not correct. | `DISPLAY_TZ` is missing or not valid. | Use an IANA time zone name such as `America/New_York`. |

## Known limits

- The item budget limits each run. The job can miss posts when the accounts publish more
  than the budget. The email shows a warning when this occurs.
- The job keeps only the newest 1000 sent IDs.
- The actor result format can change.
- The actor can charge for placeholder items when a query has no results.
- A post that enters the X search index more than 10 minutes late can fall outside all
  scan windows. Increase `WINDOW_OVERLAP` in `digest.py` when this occurs.
- The job sends to one `RECIPIENT_EMAIL` value.
- The workflow needs an external scheduler.
