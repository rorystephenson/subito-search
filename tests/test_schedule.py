"""Schedule tests, including the invariant that the workflow cron matches config."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from subito_alerts.config import load_config
from subito_alerts.schedule import (
    ScheduleError,
    parse_schedule,
    read_workflow_cron,
    required_cron,
    write_workflow_cron,
)

ROME = ZoneInfo("Europe/Rome")
REPO = Path(__file__).resolve().parent.parent


def rome_window(**kw):
    return parse_schedule({"timezone": "Europe/Rome", "active_hours": "08:00-22:00", **kw})


class TestWindow(unittest.TestCase):
    def test_boundaries_are_start_inclusive_end_exclusive(self):
        s = rome_window()
        cases = [(7, 59, False), (8, 0, True), (21, 59, True), (22, 0, False), (3, 0, False)]
        for hour, minute, expected in cases:
            with self.subTest(f"{hour:02d}:{minute:02d}"):
                self.assertEqual(s.is_active(datetime(2026, 7, 15, hour, minute, tzinfo=ROME)), expected)

    def test_window_is_local_not_utc(self):
        s = rome_window()
        # 06:30 UTC is 08:30 Rome in summer (active) but 07:30 in winter (not).
        self.assertTrue(s.is_active(datetime(2026, 7, 15, 6, 30, tzinfo=timezone.utc)))
        self.assertFalse(s.is_active(datetime(2026, 1, 15, 6, 30, tzinfo=timezone.utc)))

    def test_default_is_always_active(self):
        s = parse_schedule({})
        self.assertTrue(s.always)
        self.assertTrue(s.is_active(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)))

    def test_day_restriction(self):
        s = rome_window(days=["mon", "tue"])
        monday = datetime(2026, 9, 14, 10, 0, tzinfo=ROME)
        self.assertTrue(s.is_active(monday))
        self.assertFalse(s.is_active(monday + timedelta(days=2)))

    def test_window_wrapping_midnight(self):
        s = parse_schedule({"timezone": "Europe/Rome", "active_hours": "22:00-06:00"})
        for hour, expected in [(23, True), (2, True), (5, True), (6, False), (12, False)]:
            with self.subTest(hour):
                self.assertEqual(s.is_active(datetime(2026, 7, 15, hour, tzinfo=ROME)), expected)

    def test_24_00_means_end_of_day(self):
        s = parse_schedule({"active_hours": "08:00-24:00"})
        self.assertTrue(s.is_active(datetime(2026, 7, 15, 23, 59, tzinfo=timezone.utc)))
        self.assertFalse(s.is_active(datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)))

    def test_bad_input(self):
        for raw in [{"timezone": "Mars/Olympus"}, {"active_hours": "morning"},
                    {"active_hours": "08:00"}, {"days": ["funday"]}, {"days": []}]:
            with self.subTest(str(raw)), self.assertRaises(ScheduleError):
                parse_schedule(raw)


class TestRequiredCron(unittest.TestCase):
    def test_rome_8_to_22_at_15_minutes(self):
        # Rome is UTC+1/+2, so the UTC window must span 06:00-20:59.
        self.assertEqual(required_cron(rome_window(), 15, year=2026), "*/15 6-20 * * *")

    def test_cron_covers_every_active_slot_all_year(self):
        """The real invariant: the cron must never miss an active local slot."""
        schedule, step = rome_window(), 15
        cron = required_cron(schedule, step, year=2026)
        minute_field, hour_field = cron.split()[0], cron.split()[1]
        hours = set()
        for part in hour_field.split(","):
            if "-" in part:
                lo, hi = map(int, part.split("-"))
                hours |= set(range(lo, hi + 1))
            else:
                hours.add(int(part))

        missed = []
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        while moment.year == 2026:
            fires = moment.hour in hours and moment.minute % step == 0
            if schedule.is_active(moment) and not fires:
                missed.append(moment)
            moment += timedelta(minutes=step)
        self.assertEqual(missed[:3], [], f"{len(missed)} active slots not covered")

    def test_always_schedule_spans_all_hours(self):
        self.assertEqual(required_cron(parse_schedule({}), 30, year=2026), "*/30 * * * *")

    def test_interval_snaps_to_a_clean_cron_step(self):
        # 45 doesn't divide 60, so */45 would leave an uneven gap each hour.
        self.assertTrue(required_cron(parse_schedule({}), 45, year=2026).startswith("*/30 "))
        self.assertTrue(required_cron(parse_schedule({}), 60, year=2026).startswith("0 "))

    def test_day_restriction_reaches_the_cron(self):
        cron = required_cron(rome_window(days=["sat", "sun"]), 30, year=2026)
        self.assertNotEqual(cron.split()[-1], "*")


class TestWorkflowSync(unittest.TestCase):
    WORKFLOW = REPO / ".github/workflows/alerts.yml"

    def test_committed_workflow_matches_committed_config(self):
        """Fails CI if searches.yaml and the workflow cron drift apart."""
        config = load_config(REPO / "searches.yaml")
        self.assertEqual(
            read_workflow_cron(self.WORKFLOW),
            required_cron(config.schedule, config.min_interval),
            "workflow cron is stale — run: python -m subito_alerts.main --sync-schedule",
        )

    def test_write_preserves_comments_and_reports_change(self):
        path = Path(tempfile.mkdtemp()) / "wf.yml"
        path.write_text('on:\n  schedule:\n    # keep me\n    - cron: "*/30 * * * *"\n\njobs: {}\n')
        self.assertTrue(write_workflow_cron(path, "*/15 6-20 * * *"))
        self.assertIn("# keep me", path.read_text())
        self.assertEqual(read_workflow_cron(path), "*/15 6-20 * * *")
        self.assertFalse(write_workflow_cron(path, "*/15 6-20 * * *"))

    def test_missing_cron_line_is_an_error(self):
        path = Path(tempfile.mkdtemp()) / "wf.yml"
        path.write_text("jobs: {}\n")
        with self.assertRaises(ScheduleError):
            read_workflow_cron(path)


if __name__ == "__main__":
    unittest.main()
