# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "apify-client>=1.8.0",
#     "jinja2>=3.1.0",
#     "tzdata>=2024.1",
# ]
# ///

import argparse
import functools
import inspect
import math
import os
import random
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from apify_client import ApifyClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

# Environment Configuration
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")
HANDLES_FILE = os.environ.get("HANDLES_FILE", "handles.txt")
# The template ships beside this script; handles.txt is user config read from
# the working directory, so a private repo can run this file from its own root.
TEMPLATE_PATH = os.environ.get("TEMPLATE_PATH", str(Path(__file__).with_name("template.html")))
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "America/New_York")

KVS_STORE_NAME = "x-digest-state"
KVS_RECORD_KEY = "seen_post_ids"
KVS_METADATA_KEY = "run_metadata"

# Sliding scan window: each run scans from the previous run's window end
# (minus a small overlap for X search indexing lag) up to its own start time.
WINDOW_OVERLAP = timedelta(minutes=10)
FIRST_RUN_LOOKBACK = timedelta(hours=24)

SMTP_TIMEOUT_SECONDS = 30
SMTP_MAX_ATTEMPTS = 3
SMTP_RETRY_BASE_DELAY = 2.0

# Result budget for the pay-per-result actor: worst-case cost ceiling per run.
# MAX_ITEMS_PER_HANDLE is a per-24-hours rate; the budget scales with the
# scanned window but is capped at MAX_ITEMS_WINDOW_CAP_DAYS' worth so a long
# outage gap cannot explode the ceiling.
MAX_ITEMS_PER_HANDLE = 5
MAX_ITEMS_FLOOR = 30
MAX_ITEMS_WINDOW_CAP_DAYS = 3

# Kill a hung actor run platform-side, and never let the client wait forever.
ACTOR_TIMEOUT_SECONDS = 600
ACTOR_WAIT_SECONDS = 660

# Sentinel so posts with a missing or unparseable date sort before dated ones.
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


class StateStoreError(RuntimeError):
    """Raised when the Apify key-value state store cannot be read."""


@functools.lru_cache(maxsize=8)
def _resolve_timezone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Notice: Unknown DISPLAY_TZ '{name}'. Using UTC.")
        return timezone.utc


def display_timezone():
    """Time zone used to show post times. Falls back to UTC on a bad name."""
    return _resolve_timezone(DISPLAY_TZ)


def parse_post_date(value):
    """Best-effort parse of the many date shapes the actor emits."""
    if isinstance(value, (int, float)):
        try:
            # Some actor payloads use milliseconds instead of seconds.
            if abs(value) >= 100_000_000_000:
                value /= 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    # Twitter's legacy format, e.g. "Fri Sep 19 09:15:00 +0000 2025"
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def format_post_time(dt):
    if dt is None:
        return ""
    return dt.astimezone(display_timezone()).strftime("%b %d, %Y at %I:%M %p %Z").replace(" 0", " ")


def format_digest_date(value: str) -> str:
    """Convert an ISO date to the long date used in the email header."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%B %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return value


def format_job_time(value: datetime) -> str:
    return value.astimezone(display_timezone()).strftime("%I:%M:%S %p %Z").lstrip("0")


def format_window_range(window_start: datetime, window_end: datetime) -> str:
    """Long form of the scanned interval in DISPLAY_TZ, with its duration."""
    tz = display_timezone()
    start_local = window_start.astimezone(tz)
    end_local = window_end.astimezone(tz)
    start_fmt = start_local.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    if start_local.date() == end_local.date():
        end_fmt = end_local.strftime("%I:%M %p %Z").lstrip("0")
    else:
        end_fmt = end_local.strftime("%b %d, %I:%M %p %Z").replace(" 0", " ")
    hours = (window_end - window_start).total_seconds() / 3600.0
    return f"{start_fmt} - {end_fmt} ({hours:.1f} h)"


def format_subject_window(window_start: datetime, window_end: datetime) -> str:
    """Compact interval used in the subject line, e.g. 'Sep 20 06:00-12:00 EDT'."""
    tz = display_timezone()
    start_local = window_start.astimezone(tz)
    end_local = window_end.astimezone(tz)
    tz_abbr = end_local.strftime("%Z")
    if start_local.date() == end_local.date():
        return (
            f"{start_local.strftime('%b %d %H:%M')}-{end_local.strftime('%H:%M')} {tz_abbr}"
        )
    return (
        f"{start_local.strftime('%b %d %H:%M')}-{end_local.strftime('%b %d %H:%M')} {tz_abbr}"
    )


def previous_utc_day(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the half-open UTC interval for the previous calendar day."""
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today


def validate_environment():
    required = ["APIFY_TOKEN", "GMAIL_USER", "GMAIL_APP_PASS", "RECIPIENT_EMAIL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


def load_handles(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Handles configuration file not found: {filepath}")

    handles = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            clean = line.strip().lstrip("@")
            if clean and not clean.startswith("#"):
                handles.append(clean)
    if not handles:
        raise ValueError(f"Handles configuration file is empty: {filepath}")
    return handles


def chunk_handles(handles, chunk_size=8):
    for i in range(0, len(handles), chunk_size):
        yield handles[i : i + chunk_size]


def get_state_store_client(client: ApifyClient):
    """Retrieve or create the named Key-Value store and return its KeyValueStoreClient."""
    store_info = client.key_value_stores().get_or_create(name=KVS_STORE_NAME)
    store_id = store_info.id if hasattr(store_info, "id") else store_info["id"]
    return client.key_value_store(store_id)


def load_seen_ids(kvs_client) -> list[str]:
    """Return remembered post IDs.

    A missing or malformed record means a fresh start; an API failure raises
    StateStoreError instead, so a transient outage never silently wipes the
    de-duplication state (and is never overwritten with a partial ID list).
    """
    try:
        record = kvs_client.get_record(KVS_RECORD_KEY)
    except Exception as e:
        raise StateStoreError(
            f"Could not read record '{KVS_RECORD_KEY}' from store '{KVS_STORE_NAME}': {e}"
        ) from e

    if record is None:
        print("Notice: No existing state record. Starting fresh.")
        return []
    value = _field(record, "value", "value")
    if isinstance(value, list):
        return list(dict.fromkeys(str(post_id) for post_id in value))
    print("Notice: State record is malformed. Starting fresh.")
    return []


def save_seen_ids(kvs_client, seen_ids):
    pruned_list = list(dict.fromkeys(seen_ids))[-1000:]
    kvs_client.set_record(KVS_RECORD_KEY, pruned_list)


def load_run_metadata(kvs_client) -> dict | None:
    """Return the previous run's scan metadata, or None on a fresh start.

    Same contract as load_seen_ids: a missing or malformed record is a fresh
    start (first run); an API failure raises StateStoreError.
    """
    try:
        record = kvs_client.get_record(KVS_METADATA_KEY)
    except Exception as e:
        raise StateStoreError(
            f"Could not read record '{KVS_METADATA_KEY}' from store '{KVS_STORE_NAME}': {e}"
        ) from e

    if record is None:
        return None
    value = _field(record, "value", "value")
    if not isinstance(value, dict):
        print("Notice: Run metadata record is malformed. Treating as a first run.")
        return None
    window_end = parse_post_date(value.get("window_end"))
    if window_end is None:
        print("Notice: Run metadata has no usable window_end. Treating as a first run.")
        return None
    return {"window_end": window_end.astimezone(timezone.utc)}


def save_run_metadata(
    kvs_client,
    window_start: datetime,
    window_end: datetime,
    job_started_at: datetime,
    job_completed_at: datetime,
):
    kvs_client.set_record(
        KVS_METADATA_KEY,
        {
            "window_start": window_start.astimezone(timezone.utc).isoformat(),
            "window_end": window_end.astimezone(timezone.utc).isoformat(),
            "job_started_at": job_started_at.astimezone(timezone.utc).isoformat(),
            "job_completed_at": job_completed_at.astimezone(timezone.utc).isoformat(),
        },
    )


def resolve_scan_window(
    metadata: dict | None, job_start_utc: datetime
) -> tuple[datetime, datetime, bool]:
    """Return (window_start, window_end, first_run) for a sliding-window run.

    The window runs from the previous run's window end (minus WINDOW_OVERLAP,
    absorbed by dedup) up to this job's start. Without metadata, fall back to
    FIRST_RUN_LOOKBACK. The interval a failed run should have scanned is
    re-scanned automatically because failures never advance the metadata.
    """
    window_end = job_start_utc.astimezone(timezone.utc)
    if metadata is None:
        return window_end - FIRST_RUN_LOOKBACK, window_end, True
    window_start = metadata["window_end"] - WINDOW_OVERLAP
    if window_start >= window_end:
        # Clock skew or overlapping runs; still scan at least the overlap.
        window_start = window_end - WINDOW_OVERLAP
    return window_start, window_end, False


def build_search_terms(
    handles: list[str], window_start: datetime, window_end: datetime
) -> list[str]:
    # since_time/until_time take unix epochs and are precise to the second, so
    # short scan windows never re-fetch (and re-pay for) earlier same-day posts.
    since = int(window_start.timestamp())
    until = int(window_end.timestamp())
    search_terms = []
    for chunk in chunk_handles(handles, chunk_size=8):
        sub_query = " OR ".join(f"from:{h}" for h in chunk)
        search_terms.append(
            f"({sub_query}) -filter:replies since_time:{since} until_time:{until}"
        )
    return search_terms


def max_items_budget(handle_count: int, window_hours: float = 24.0) -> int:
    """Result cap scaled by handle count and scanned-window length."""
    scaled = math.ceil(MAX_ITEMS_PER_HANDLE * handle_count * window_hours / 24.0)
    cap = MAX_ITEMS_PER_HANDLE * handle_count * MAX_ITEMS_WINDOW_CAP_DAYS
    return max(MAX_ITEMS_FLOOR, min(scaled, cap))


def trigger_apify_scrape(
    client: ApifyClient,
    handles: list[str],
    window_start: datetime,
    window_end: datetime,
    max_items: int,
) -> str:
    search_terms = build_search_terms(handles, window_start, window_end)

    actor_input = {
        "searchTerms": search_terms,
        "maxItems": max_items,
        "sort": "Latest",
        "proxyConfig": {"useApifyProxy": True},
    }

    print(
        "Triggering synchronous run on Apify "
        f"({window_start.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC through "
        f"{window_end.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC, "
        f"maxItems={max_items})..."
    )
    actor_client = client.actor(
        "kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest"
    )
    # Timeout parameter names differ between apify-client 1.x/2.x (ints) and
    # 3.x (timedeltas), so pick whichever this installed version accepts.
    call_params = inspect.signature(actor_client.call).parameters
    call_kwargs = {"run_input": actor_input, "memory_mbytes": 1024}
    if "run_timeout" in call_params:  # apify-client 3.x
        call_kwargs["run_timeout"] = timedelta(seconds=ACTOR_TIMEOUT_SECONDS)
        call_kwargs["wait_duration"] = timedelta(seconds=ACTOR_WAIT_SECONDS)
    else:  # apify-client 1.x / 2.x
        call_kwargs["timeout_secs"] = ACTOR_TIMEOUT_SECONDS
        call_kwargs["wait_secs"] = ACTOR_WAIT_SECONDS
    run = actor_client.call(**call_kwargs)

    if isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    else:
        dataset_id = getattr(run, "default_dataset_id", None) or getattr(
            run, "defaultDatasetId", None
        )
    if not dataset_id:
        raise RuntimeError("Apify actor completed without a default dataset ID")
    return dataset_id


def fetch_dataset_items(client: ApifyClient, dataset_id: str):
    print(f"Fetching dataset items from ID: {dataset_id}")
    dataset = client.dataset(dataset_id)
    return dataset.list_items().items


_URL_RE = re.compile(r"https?://\S+")
# Emoji sequences are symbols (So) glued together by skin-tone modifiers,
# variation selectors, keycaps and zero-width joiners (Sk/Mn/Me/Cf).
_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Me", "Cf"}


def is_emoji_only(text: str) -> bool:
    """True when the text, ignoring URLs and whitespace, is nothing but emoji -
    a forward with no commentary of its own."""
    chars = [c for c in _URL_RE.sub("", text) if not c.isspace()]
    return any(unicodedata.category(c) == "So" for c in chars) and all(
        unicodedata.category(c) in _EMOJI_CATEGORIES for c in chars
    )


def parse_post_items(
    raw_items: list,
    seen_ids: set,
    allowed_handles: set[str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
):
    """Parse actor items into posts.

    Returns (new_posts, stats). stats counts dropped items by reason so the
    email can surface actor output-shape drift instead of hiding it in logs.
    """
    new_posts = []
    stats = {
        "mock": 0,
        "unmonitored": 0,
        "bad_timestamp": 0,
        "outside_window": 0,
        "emoji_only": 0,
    }

    for item in raw_items:
        pid = str(
            item.get("id")
            or item.get("id_str")
            or item.get("tweet_id")
            or item.get("conversation_id")
            or ""
        )
        if not pid or pid in seen_ids:
            continue

        author_data = item.get("author")
        author_fields = author_data if isinstance(author_data, dict) else {}
        user_data = item.get("user")
        user_fields = user_data if isinstance(user_data, dict) else {}
        author = (
            author_fields.get("userName")
            or author_fields.get("username")
            or (author_data if isinstance(author_data, str) else None)
            or user_fields.get("screen_name")
            or item.get("user_screen_name")
            or item.get("username")
            or "User"
        )
        text = (item.get("text") or item.get("full_text") or "").strip()

        # Filter out developer synthetic/mock messages and billing notices
        text_lower = text.lower()
        if (
            "from kaitoeasyapi" in text_lower
            or "mock data" in text_lower
            or author.lower() == "kaitoeasyapi"
        ):
            stats["mock"] += 1
            print(f"Ignored mock/billing item (ID: {pid})")
            continue
        if allowed_handles is not None and author.lower() not in allowed_handles:
            stats["unmonitored"] += 1
            print(f"Ignored item from unmonitored author @{author} (ID: {pid})")
            continue
        if is_emoji_only(text):
            stats["emoji_only"] += 1
            print(f"Ignored emoji-only item (ID: {pid})")
            continue

        url = item.get("url") or item.get("tweet_url") or f"https://x.com/{author}/status/{pid}"

        entities = item.get("entities")
        entity_urls = entities.get("urls", []) if isinstance(entities, dict) else []
        urls = entity_urls or item.get("urls", [])
        extracted_links = []
        for u in urls:
            if isinstance(u, dict):
                extracted_links.append(u.get("expanded_url") or u.get("url"))
            elif isinstance(u, str):
                extracted_links.append(u)

        valid_links = [l for l in extracted_links if l]

        created_at = parse_post_date(
            item.get("createdAt")
            or item.get("created_at")
            or item.get("date")
            or item.get("timestamp")
        )
        if window_start is not None and window_end is not None:
            if created_at is None:
                stats["bad_timestamp"] += 1
                print(f"Ignored item with missing or invalid timestamp (ID: {pid})")
                continue
            if not window_start <= created_at.astimezone(timezone.utc) < window_end:
                stats["outside_window"] += 1
                print(f"Ignored item outside the UTC digest window (ID: {pid})")
                continue

        new_posts.append({
            "id": pid,
            "author": author,
            "text": text,
            "url": url,
            "links": list(dict.fromkeys(valid_links)),
            "created_at": created_at,
            "time_str": format_post_time(created_at),
        })
        seen_ids.add(pid)

    return new_posts, stats


def drift_warnings(stats: dict) -> list[str]:
    """Warnings for drop reasons that usually mean the actor's output shape
    changed. Mock/billing and outside-window drops are normal and stay in logs."""
    warnings = []
    if stats.get("unmonitored"):
        warnings.append(
            f"{stats['unmonitored']} scraped item(s) were dropped because the author "
            "was missing or not in handles.txt - the actor's output shape may have changed."
        )
    if stats.get("bad_timestamp"):
        warnings.append(
            f"{stats['bad_timestamp']} scraped item(s) were dropped due to a missing "
            "or unparseable timestamp - the actor's output shape may have changed."
        )
    return warnings


def group_posts_by_author(posts: list) -> list:
    """Group posts by author. Author order is random on every run; posts inside
    a group are oldest first."""
    groups: dict[str, list] = {}
    for post in posts:
        groups.setdefault(post["author"].lower(), []).append(post)

    author_groups = list(groups.values())
    random.shuffle(author_groups)

    result = []
    for group in author_groups:
        group.sort(key=lambda p: p.get("created_at") or _MIN_DT)
        author = group[0]["author"]
        result.append({
            "author": author,
            "profile_url": f"https://x.com/{author}",
            "posts": group,
        })
    return result


def _field(obj, snake: str, camel: str):
    """Read a field from an apify-client model (3.x) or a plain dict (1.x/2.x)."""
    if isinstance(obj, dict):
        return obj.get(snake, obj.get(camel))
    return getattr(obj, snake, None) or getattr(obj, camel, None)


def fetch_apify_usage(client: ApifyClient):
    """Return (usage, warning). usage is None when the account data is unavailable."""
    try:
        account_limits = client.user().limits()
        current = _field(account_limits, "current", "current")
        limits = _field(account_limits, "limits", "limits")
        used = float(_field(current, "monthly_usage_usd", "monthlyUsageUsd") or 0.0)
        limit = float(_field(limits, "max_monthly_usage_usd", "maxMonthlyUsageUsd") or 0.0)
    except Exception as e:
        return None, f"Apify usage data is unavailable: {e}"

    percent = (used / limit * 100.0) if limit else 0.0

    if percent >= 90:
        bar_color = "#f4212e"
    elif percent >= 70:
        bar_color = "#e65100"
    else:
        bar_color = "#1d9bf0"

    return {
        "used_usd": used,
        "limit_usd": limit,
        "percent": percent,
        # Clamp the bar so it never overflows its container.
        "bar_pct": min(percent, 100.0),
        "bar_color": bar_color,
    }, None


def render_html_template(
    posts: list,
    today_str: str,
    author_groups: list,
    scrape_error: str | None = None,
    warnings: list[str] | None = None,
    usage: dict | None = None,
    started_str: str | None = None,
    completed_str: str | None = None,
    window_str: str | None = None,
) -> str:
    template_file = Path(TEMPLATE_PATH)
    if not template_file.exists():
        raise FileNotFoundError(f"Jinja2 template not found: {TEMPLATE_PATH}")

    env = Environment(
        loader=FileSystemLoader(template_file.parent),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template(template_file.name)
    return template.render(
        posts=posts,
        today_str=today_str,
        today_display=format_digest_date(today_str),
        author_groups=author_groups,
        scrape_error=scrape_error,
        warnings=warnings or [],
        usage=usage,
        started_str=started_str,
        completed_str=completed_str,
        window_str=window_str,
    )


def build_plain_digest(
    posts: list,
    today_str: str,
    author_groups: list,
    scrape_error: str | None = None,
    warnings: list[str] | None = None,
    usage: dict | None = None,
    started_str: str | None = None,
    completed_str: str | None = None,
    window_str: str | None = None,
) -> str:
    lines = ["X Daily Digest", format_digest_date(today_str), ""]

    if started_str:
        lines.append(f"Started: {started_str}")
    if completed_str:
        lines.append(f"Completed: {completed_str}")
    if window_str:
        lines.append(f"Posts scanned: {window_str}")
    if started_str or completed_str or window_str:
        lines.append("")

    if scrape_error:
        lines += ["SCRAPER ERROR", scrape_error, ""]

    if warnings:
        lines.append("WARNINGS" if len(warnings) > 1 else "WARNING")
        lines += warnings
        lines.append("")

    if posts:
        for group in author_groups:
            lines.append(f"-- @{group['author']} --")
            for post in group["posts"]:
                lines.append(f"  {post['text']}")
                if post["time_str"]:
                    lines.append(f"  {post['time_str']}")
                if post["links"]:
                    lines.append("  Links:")
                    lines += [f"    - {link}" for link in post["links"]]
                lines.append(f"  {post['url']}")
                lines.append("")
    elif not scrape_error:
        lines += [
            "No new posts from the monitored X accounts in the scanned window.",
            "",
        ]

    if usage:
        lines += [
            f"Apify usage this month: ${usage['used_usd']:.2f} of "
            f"${usage['limit_usd']:.2f} ({usage['percent']:.1f}%)",
            "",
        ]

    return "\n".join(lines)


def subject_line(posts: list, scrape_error: str | None, window_label: str) -> str:
    if scrape_error:
        return f"X Digest - Scraper Error ({window_label})"
    if not posts:
        return f"X Digest - No New Posts ({window_label})"
    count = len(posts)
    return f"X Digest - {count} New Post{'s' if count != 1 else ''} ({window_label})"


def send_email(
    posts: list,
    today_str: str | None = None,
    subject_label: str | None = None,
    window_str: str | None = None,
    scrape_error: str | None = None,
    warnings: list[str] | None = None,
    usage: dict | None = None,
    job_start: datetime | None = None,
) -> bool:
    """Send the digest. Returns True only when Gmail accepted the message.

    today_str is the digest date shown in the header (defaults to the send
    date), subject_label is the interval or date in the subject line, and
    window_str is the human "Posts scanned" interval.
    """
    if not today_str:
        today_str = datetime.now(display_timezone()).strftime("%Y-%m-%d")
    if not subject_label:
        subject_label = today_str
    author_groups = group_posts_by_author(posts)
    completed_at = datetime.now(display_timezone())
    started_str = format_job_time(job_start) if job_start else None
    completed_str = format_job_time(completed_at)

    try:
        msg = EmailMessage()
        msg["Subject"] = subject_line(posts, scrape_error, subject_label)
        msg["From"] = GMAIL_USER
        msg["To"] = RECIPIENT_EMAIL

        # Plain text first; the HTML alternative is the preferred part.
        msg.set_content(
            build_plain_digest(
                posts,
                today_str,
                author_groups,
                scrape_error,
                warnings,
                usage,
                started_str,
                completed_str,
                window_str,
            )
        )
        msg.add_alternative(
            render_html_template(
                posts,
                today_str,
                author_groups,
                scrape_error,
                warnings,
                usage,
                started_str,
                completed_str,
                window_str,
            ),
            subtype="html",
        )
    except Exception as e:
        print(f"Error: Failed to build digest email: {e}")
        return False

    for attempt in range(1, SMTP_MAX_ATTEMPTS + 1):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASS)
                server.send_message(msg)
            print(f"Email sent: {msg['Subject']}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"Error: Gmail authentication failed: {e}")
            return False
        except (smtplib.SMTPException, OSError) as e:
            if attempt >= SMTP_MAX_ATTEMPTS:
                print(f"Error: Failed to send digest email after {attempt} attempts: {e}")
                return False
            delay = SMTP_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(
                f"Warning: Email attempt {attempt}/{SMTP_MAX_ATTEMPTS} failed; "
                f"retrying in {delay:.1f}s. Error: {e}"
            )
            time.sleep(delay)
        except Exception as e:
            print(f"Error: Failed to send digest email: {e}")
            return False

    return False


def main():
    job_start = datetime.now(display_timezone())
    parser = argparse.ArgumentParser(description="Send X Digest Email")
    parser.add_argument(
        "--dataset-id",
        dest="dataset_id",
        help="Apify Dataset ID from a completed run (skips running the scraper)",
        default=None,
    )
    parser.add_argument(
        "--window-date",
        dest="window_date",
        help="UTC day (YYYY-MM-DD) to keep posts from, instead of the sliding "
        "window since the last run. With --dataset-id and no --window-date, "
        "window filtering is skipped entirely (de-duplication still applies).",
        default=None,
    )
    args = parser.parse_args()

    validate_environment()
    client = ApifyClient(APIFY_TOKEN)
    job_start_utc = job_start.astimezone(timezone.utc)

    # Run mode: "sliding" scans since the last run (the normal cron path),
    # "pinned" filters to one explicit UTC day, "replay" reuses a dataset with
    # no window filter. Only sliding runs advance the stored scan metadata.
    if args.window_date:
        mode = "pinned"
        try:
            window_start = datetime.strptime(args.window_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            parser.error(f"--window-date must be YYYY-MM-DD, got {args.window_date!r}")
        window_end = window_start + timedelta(days=1)
    elif args.dataset_id:
        mode = "replay"
        window_start = window_end = None
    else:
        mode = "sliding"
        window_start = window_end = None  # resolved after the state load

    warnings: list[str] = []

    # 1. State store. Fail fast on API errors: scraping without dedup state
    # burns credits, risks duplicate emails, and a later save would overwrite
    # the remembered IDs with a partial list.
    try:
        kvs_client = get_state_store_client(client)
        stored_seen_ids = load_seen_ids(kvs_client)
        run_metadata = load_run_metadata(kvs_client) if mode == "sliding" else None
    except Exception as e:
        error = f"State store unavailable: {type(e).__name__}: {e}"
        print(f"Error: {error}")
        usage, usage_warning = fetch_apify_usage(client)
        if usage_warning:
            warnings.append(usage_warning)
        send_email(
            [],
            scrape_error=error,
            warnings=warnings,
            usage=usage,
            job_start=job_start,
        )
        sys.exit(1)

    seen_ids = set(stored_seen_ids)

    if mode == "sliding":
        window_start, window_end, first_run = resolve_scan_window(
            run_metadata, job_start_utc
        )
        if first_run:
            warnings.append(
                "First run (no previous scan metadata): scanned the last "
                f"{int(FIRST_RUN_LOOKBACK.total_seconds() // 3600)} hours."
            )

    # Labels for the subject line and the "Posts scanned" display.
    if mode == "sliding":
        digest_date_str = window_end.astimezone(timezone.utc).strftime("%Y-%m-%d")
        subject_label = format_subject_window(window_start, window_end)
        window_str = format_window_range(window_start, window_end)
    elif mode == "pinned":
        digest_date_str = window_start.strftime("%Y-%m-%d")
        subject_label = digest_date_str
        window_str = f"{format_digest_date(digest_date_str)} (full UTC day)"
    else:  # replay
        digest_date_str = previous_utc_day()[0].strftime("%Y-%m-%d")
        subject_label = "replay"
        window_str = "No window filter (replayed dataset)"

    # 2. Scrape or Fetch, then Parse & Filter.
    # A failure here still produces an email that carries the error banner.
    new_posts = []
    scrape_error = None
    try:
        handles = load_handles(HANDLES_FILE)
        allowed_handles = {handle.lower() for handle in handles}
        if window_start is not None and window_end is not None:
            window_hours = (window_end - window_start).total_seconds() / 3600.0
        else:
            window_hours = 24.0
        max_items = max_items_budget(len(handles), window_hours)
        dataset_id = args.dataset_id
        if not dataset_id:
            dataset_id = trigger_apify_scrape(
                client, handles, window_start, window_end, max_items
            )

        raw_posts = fetch_dataset_items(client, dataset_id)
        if not args.dataset_id and len(raw_posts) >= max_items:
            warnings.append(
                f"The scrape returned the full result cap of {max_items} items; "
                "some posts may be missing from this digest."
            )
        new_posts, parse_stats = parse_post_items(
            raw_posts,
            seen_ids,
            allowed_handles=allowed_handles,
            window_start=window_start,
            window_end=window_end,
        )
        warnings.extend(drift_warnings(parse_stats))
    except Exception as e:
        scrape_error = f"{type(e).__name__}: {e}"
        print(f"Error: Scrape or fetch failed: {scrape_error}")

    # 3. Account usage for the email footer
    usage, usage_warning = fetch_apify_usage(client)
    if usage_warning:
        print(f"Warning: {usage_warning}")
        warnings.append(usage_warning)

    # 4. Deliver & Persist. The digest is sent on every run, even with no posts,
    # so the Apify usage footer stays visible.
    sent = send_email(
        new_posts,
        today_str=digest_date_str,
        subject_label=subject_label,
        window_str=window_str,
        scrape_error=scrape_error,
        warnings=warnings,
        usage=usage,
        job_start=job_start,
    )

    # Persist. Seen IDs advance only when there are new posts; the scan-window
    # metadata advances on every clean sliding run - including zero-post runs -
    # or the same interval would be re-scanned forever. Failed sends and scrape
    # errors advance neither, so the missed interval is re-scanned next run.
    save_failed = False
    if sent:
        try:
            if new_posts:
                save_seen_ids(
                    kvs_client,
                    [*stored_seen_ids, *(post["id"] for post in new_posts)],
                )
            else:
                print("No new posts.")
            if mode == "sliding" and not scrape_error:
                save_run_metadata(
                    kvs_client,
                    window_start,
                    window_end,
                    job_start_utc,
                    datetime.now(timezone.utc),
                )
        except Exception as e:
            save_failed = True
            print(
                "Error: Digest was sent but saving state failed; the next run "
                f"may repeat posts or re-scan this window: {e}"
            )

    if not sent or scrape_error or save_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
