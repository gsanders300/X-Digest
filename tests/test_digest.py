import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import digest


class FakeKeyValueStore:
    def __init__(self, value=None):
        self.value = value
        self.saved = None

    def get_record(self, _key):
        return {"value": self.value} if self.value is not None else None

    def set_record(self, _key, value):
        self.saved = value


class DigestTests(unittest.TestCase):
    def test_previous_utc_day_uses_calendar_boundaries(self):
        now = datetime(2026, 9, 20, 14, 16, tzinfo=timezone.utc)

        start, end = digest.previous_utc_day(now)

        self.assertEqual(start, datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 20, tzinfo=timezone.utc))

    def test_search_terms_use_second_precision_epochs(self):
        start = datetime(2026, 9, 20, 6, 0, 12, tzinfo=timezone.utc)
        end = datetime(2026, 9, 20, 12, 0, 4, tzinfo=timezone.utc)

        terms = digest.build_search_terms(
            [f"user{i}" for i in range(9)], start, end
        )

        self.assertEqual(len(terms), 2)
        since = int(start.timestamp())
        until = int(end.timestamp())
        self.assertTrue(all(f"since_time:{since}" in term for term in terms))
        self.assertTrue(all(f"until_time:{until}" in term for term in terms))
        self.assertIn("from:user8", terms[1])

    def test_eastern_display_uses_est_and_edt(self):
        original = digest.DISPLAY_TZ
        digest.DISPLAY_TZ = "America/New_York"
        try:
            winter = digest.format_post_time(
                datetime(2026, 1, 15, 17, tzinfo=timezone.utc)
            )
            summer = digest.format_post_time(
                datetime(2026, 7, 15, 16, tzinfo=timezone.utc)
            )
        finally:
            digest.DISPLAY_TZ = original

        self.assertEqual(winter, "Jan 15, 2026 at 12:00 PM EST")
        self.assertEqual(summer, "Jul 15, 2026 at 12:00 PM EDT")

    def test_digest_date_uses_source_email_format(self):
        self.assertEqual(digest.format_digest_date("2026-09-20"), "September 20, 2026")

    def test_job_time_uses_eastern_display(self):
        original = digest.DISPLAY_TZ
        digest.DISPLAY_TZ = "America/New_York"
        try:
            result = digest.format_job_time(
                datetime(2026, 9, 20, 12, 5, 24, tzinfo=timezone.utc)
            )
        finally:
            digest.DISPLAY_TZ = original

        self.assertEqual(result, "8:05:24 AM EDT")

    def test_html_uses_source_email_layout(self):
        post = {
            "id": "1",
            "author": "sama",
            "text": "Post text",
            "url": "https://x.com/sama/status/1",
            "links": [],
            "created_at": datetime(2026, 9, 19, 12, tzinfo=timezone.utc),
            "time_str": "Sep 19, 2026 at 8:00 AM EDT",
        }
        groups = [{
            "author": "sama",
            "profile_url": "https://x.com/sama",
            "posts": [post],
        }]

        html = digest.render_html_template(
            [post],
            "2026-09-20",
            groups,
            started_str="8:05:24 AM EDT",
            completed_str="8:06:08 AM EDT",
        )

        self.assertIn("X Daily Digest", html)
        self.assertIn("September 20, 2026", html)
        self.assertIn("max-width: 700px", html)
        self.assertIn("background: #0077b5", html)
        self.assertIn("Read on X &rarr;", html)
        self.assertIn("Started: 8:05:24 AM EDT", html)
        self.assertIn("Completed: 8:06:08 AM EDT", html)

    def test_template_renders_from_another_working_directory(self):
        # A private config repo runs app/digest.py from its own root.
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                html = digest.render_html_template([], "2026-09-20", [])
            finally:
                os.chdir(original)

        self.assertIn("X Daily Digest", html)

    def test_parse_post_date_accepts_epoch_milliseconds(self):
        parsed = digest.parse_post_date(1_758_278_400_000)

        self.assertEqual(
            parsed, datetime(2025, 9, 19, 10, 40, tzinfo=timezone.utc)
        )

    def test_parser_keeps_only_monitored_posts_inside_window(self):
        start = datetime(2026, 9, 19, tzinfo=timezone.utc)
        end = datetime(2026, 9, 20, tzinfo=timezone.utc)
        items = [
            {
                "id": "valid",
                "author": "Sama",
                "text": "A real post",
                "createdAt": "2026-09-19T12:00:00Z",
                "entities": "unexpected shape",
            },
            {
                "id": "today",
                "author": {"userName": "sama"},
                "text": "Too new",
                "createdAt": "2026-09-20T00:00:00Z",
            },
            {
                "id": "unknown-date",
                "author": {"userName": "sama"},
                "text": "No usable date",
            },
            {
                "id": "other-author",
                "author": {"userName": "someone_else"},
                "text": "Not monitored",
                "createdAt": "2026-09-19T12:00:00Z",
            },
            {
                "id": "mock",
                "author": {"userName": "sama"},
                "text": "Mock data from kaitoeasyapi",
                "createdAt": "2026-09-19T12:00:00Z",
            },
            {
                "id": "emoji-only",
                "author": {"userName": "sama"},
                "text": "🔥🔥 https://t.co/abc123",
                "createdAt": "2026-09-19T12:00:00Z",
            },
        ]
        seen_ids = set()

        posts, stats = digest.parse_post_items(
            items,
            seen_ids,
            allowed_handles={"sama"},
            window_start=start,
            window_end=end,
        )

        self.assertEqual([post["id"] for post in posts], ["valid"])
        self.assertEqual(seen_ids, {"valid"})
        self.assertEqual(stats["mock"], 1)
        self.assertEqual(stats["unmonitored"], 1)
        self.assertEqual(stats["bad_timestamp"], 1)
        self.assertEqual(stats["outside_window"], 1)
        self.assertEqual(stats["emoji_only"], 1)

    def test_is_emoji_only(self):
        for text in ["🔥", "👀 🚀", "👍🏽", "❤️", "👨‍👩‍👧", "🇺🇸", "💯 https://t.co/x"]:
            self.assertTrue(digest.is_emoji_only(text), text)
        for text in ["", "https://t.co/x", "wow 🔥", "This 👇", "100", "^^", "!!"]:
            self.assertFalse(digest.is_emoji_only(text), text)

    def test_parser_skips_window_filter_when_no_window_given(self):
        items = [
            {
                "id": "undated",
                "author": {"userName": "sama"},
                "text": "No usable date",
            },
        ]

        posts, stats = digest.parse_post_items(
            items, set(), allowed_handles={"sama"}
        )

        self.assertEqual([post["id"] for post in posts], ["undated"])
        self.assertEqual(stats["bad_timestamp"], 0)

    def test_drift_warnings_only_for_shape_drift_reasons(self):
        warnings = digest.drift_warnings(
            {"mock": 3, "unmonitored": 2, "bad_timestamp": 1, "outside_window": 4}
        )

        self.assertEqual(len(warnings), 2)
        self.assertIn("2 scraped item(s)", warnings[0])
        self.assertIn("1 scraped item(s)", warnings[1])
        self.assertEqual(digest.drift_warnings({"mock": 5, "outside_window": 2}), [])

    def test_max_items_budget_scales_with_handles_and_window(self):
        self.assertEqual(digest.max_items_budget(3, 24.0), 30)  # floor
        self.assertEqual(digest.max_items_budget(11, 24.0), 55)  # per-day rate
        self.assertEqual(digest.max_items_budget(11, 6.0), 30)  # short window -> floor
        self.assertEqual(digest.max_items_budget(11, 24.0 * 10), 165)  # 3-day cap

    def test_scan_window_continues_from_last_run_with_overlap(self):
        job_start = datetime(2026, 9, 20, 12, 0, 4, tzinfo=timezone.utc)
        metadata = {"window_end": datetime(2026, 9, 20, 6, 0, 12, tzinfo=timezone.utc)}

        start, end, first_run = digest.resolve_scan_window(metadata, job_start)

        self.assertEqual(end, job_start)
        self.assertEqual(
            start, datetime(2026, 9, 20, 6, 0, 12, tzinfo=timezone.utc) - digest.WINDOW_OVERLAP
        )
        self.assertFalse(first_run)

    def test_scan_window_first_run_uses_lookback(self):
        job_start = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)

        start, end, first_run = digest.resolve_scan_window(None, job_start)

        self.assertEqual(end, job_start)
        self.assertEqual(start, job_start - digest.FIRST_RUN_LOOKBACK)
        self.assertTrue(first_run)

    def test_scan_window_clamps_future_metadata(self):
        job_start = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        metadata = {"window_end": datetime(2026, 9, 21, 3, tzinfo=timezone.utc)}

        start, end, first_run = digest.resolve_scan_window(metadata, job_start)

        self.assertEqual(end, job_start)
        self.assertEqual(start, job_start - digest.WINDOW_OVERLAP)
        self.assertFalse(first_run)

    def test_run_metadata_round_trip(self):
        store = FakeKeyValueStore()
        window_start = datetime(2026, 9, 20, 6, tzinfo=timezone.utc)
        window_end = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)

        digest.save_run_metadata(store, window_start, window_end, window_end, window_end)
        store.value = store.saved

        metadata = digest.load_run_metadata(store)
        self.assertEqual(metadata["window_end"], window_end)

    def test_run_metadata_missing_or_malformed_is_first_run(self):
        self.assertIsNone(digest.load_run_metadata(FakeKeyValueStore()))
        self.assertIsNone(digest.load_run_metadata(FakeKeyValueStore(value=["not", "a", "dict"])))
        self.assertIsNone(digest.load_run_metadata(FakeKeyValueStore(value={"window_end": "garbage"})))

    def test_run_metadata_api_failure_raises(self):
        class BrokenStore:
            def get_record(self, _key):
                raise ConnectionError("boom")

        with self.assertRaises(digest.StateStoreError):
            digest.load_run_metadata(BrokenStore())

    def test_window_range_formats_same_day_interval(self):
        original = digest.DISPLAY_TZ
        digest.DISPLAY_TZ = "America/New_York"
        try:
            result = digest.format_window_range(
                datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 20, 16, 0, 4, tzinfo=timezone.utc),
            )
            subject = digest.format_subject_window(
                datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 20, 16, 0, 4, tzinfo=timezone.utc),
            )
        finally:
            digest.DISPLAY_TZ = original

        self.assertEqual(result, "Sep 20, 6:00 AM - 12:00 PM EDT (6.0 h)")
        self.assertEqual(subject, "Sep 20 06:00-12:00 EDT")

    def test_window_range_formats_cross_day_interval(self):
        original = digest.DISPLAY_TZ
        digest.DISPLAY_TZ = "America/New_York"
        try:
            result = digest.format_window_range(
                datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            )
        finally:
            digest.DISPLAY_TZ = original

        self.assertEqual(result, "Sep 20, 6:00 PM - Sep 21, 12:00 AM EDT (6.0 h)")

    def test_state_store_api_failure_raises_instead_of_wiping_state(self):
        class BrokenStore:
            def get_record(self, _key):
                raise ConnectionError("boom")

        with self.assertRaises(digest.StateStoreError):
            digest.load_seen_ids(BrokenStore())

    def test_seen_ids_keep_order_and_prune_oldest(self):
        store = FakeKeyValueStore(["2", "1", "2"])
        self.assertEqual(digest.load_seen_ids(store), ["2", "1"])

        digest.save_seen_ids(store, (str(index) for index in range(1002)))

        self.assertEqual(len(store.saved), 1000)
        self.assertEqual(store.saved[0], "2")
        self.assertEqual(store.saved[-1], "1001")

    def test_empty_handles_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handles.txt"
            path.write_text("# comments only\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "is empty"):
                digest.load_handles(path)


if __name__ == "__main__":
    unittest.main()
