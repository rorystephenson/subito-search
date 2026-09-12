"""When the bot is allowed to run.

GitHub Actions cron has no timezone support — it is always UTC — and Rome
alternates between UTC+1 and UTC+2. A fixed cron therefore cannot express
"08:00-22:00 Rome" on its own: the correct UTC window moves twice a year.

So the schedule is defined once, in searches.yaml, in local time. Two things
derive from it:

  * `Schedule.is_active()` gates each run against real local time, which is what
    actually makes the window correct.
  * `required_cron()` computes a UTC cron that is a superset of the window across
    the whole year. It only has to wake the job up often enough; the gate decides
    whether there is anything to do.

`--sync-schedule` writes that cron into the workflow and a test asserts the two
agree, so the schedule can never drift out of sync with the config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Cron's */N only spaces evenly when N divides the field's range.
CLEAN_MINUTE_STEPS = [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30]
CLEAN_HOUR_STEPS = [1, 2, 3, 4, 6, 8, 12]

CRON_LINE = re.compile(r'^(?P<indent>\s*-\s*cron:\s*)(?P<quote>["\']?)(?P<cron>[^"\'\n]+)(?P=quote)\s*$', re.M)


class ScheduleError(Exception):
    pass


@dataclass(frozen=True)
class Schedule:
    tz: ZoneInfo
    start: time
    end: time
    days: frozenset[int] | None = None  # 0 = Monday; None = every day

    # `end` is exclusive: "08:00-22:00" means the last run starts before 22:00.
    # start == end means no restriction at all.

    @property
    def always(self) -> bool:
        return self.start == self.end and self.days is None

    def is_active(self, moment: datetime) -> bool:
        """Is `moment` (any timezone) inside the configured local window?"""
        local = moment.astimezone(self.tz)
        if self.days is not None and local.weekday() not in self.days:
            return False
        now = local.time()
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= now < self.end
        # Window wraps past midnight, e.g. 22:00-06:00.
        return now >= self.start or now < self.end

    def describe(self) -> str:
        days = "every day" if self.days is None else ",".join(
            DAY_NAMES[d] for d in sorted(self.days)
        )
        window = (
            "all day" if self.start == self.end
            else f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"
        )
        return f"{window} {self.tz.key}, {days}"


def _parse_window(raw: str) -> tuple[time, time]:
    if "-" not in raw:
        raise ScheduleError(f"active_hours must look like '08:00-22:00', got {raw!r}")
    start_s, _, end_s = raw.partition("-")
    try:
        # "24:00" is a natural way to write end-of-day; time.fromisoformat
        # rejects it, and it means the same as "00:00" for an exclusive end.
        start = time.fromisoformat(start_s.strip())
        end_text = end_s.strip()
        end = time(0, 0) if end_text in ("24:00", "24") else time.fromisoformat(end_text)
    except ValueError as exc:
        raise ScheduleError(f"could not read active_hours {raw!r}: {exc}") from None
    return start, end


def _parse_days(raw: object) -> frozenset[int] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [d.strip() for d in raw.split(",")]
    days: set[int] = set()
    for item in raw:
        key = str(item).strip().lower()[:3]
        if key not in DAY_NAMES:
            raise ScheduleError(
                f"unknown day {item!r}; use any of {', '.join(DAY_NAMES)}"
            )
        days.add(DAY_NAMES.index(key))
    if not days:
        raise ScheduleError("'days' is empty — remove it to run every day")
    return frozenset(days)


def parse_schedule(raw: dict | None) -> Schedule:
    """Build a Schedule from the `schedule:` block of searches.yaml."""
    raw = raw or {}
    name = raw.get("timezone", "UTC")
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone {name!r}: {exc}") from None
    start, end = _parse_window(raw.get("active_hours", "00:00-00:00"))
    return Schedule(tz=tz, start=start, end=end, days=_parse_days(raw.get("days")))


def _step_for(interval_minutes: int) -> tuple[str, int]:
    """Cron minute field and its real spacing, for a given interval."""
    if interval_minutes < 60:
        step = max(s for s in CLEAN_MINUTE_STEPS if s <= interval_minutes)
        return f"*/{step}", step
    return "0", 60


def required_cron(schedule: Schedule, interval_minutes: int, year: int | None = None) -> str:
    """The narrowest UTC cron that still covers the whole local window.

    Computed by walking a full year rather than doing offset arithmetic, so it
    stays correct for any timezone, any DST rule, and windows that wrap midnight.
    """
    if interval_minutes < 1:
        raise ScheduleError("interval_minutes must be >= 1")
    minute_field, step = _step_for(interval_minutes)

    hours: set[int] = set()
    weekdays: set[int] = set()
    day = datetime(year or date.today().year, 1, 1, tzinfo=timezone.utc)
    end_of_year = day + timedelta(days=365)
    while day < end_of_year:
        if schedule.is_active(day):
            hours.add(day.hour)
            weekdays.add((day.weekday() + 1) % 7)  # cron: 0 = Sunday
        day += timedelta(minutes=step)

    if not hours:
        raise ScheduleError("schedule never becomes active — check active_hours/days")

    hour_field = "*" if len(hours) == 24 else _compact(sorted(hours))
    dow_field = "*" if len(weekdays) == 7 else _compact(sorted(weekdays))
    return f"{minute_field} {hour_field} * * {dow_field}"


def _compact(values: list[int]) -> str:
    """[6,7,8,9,20] -> '6-9,20'"""
    parts: list[str] = []
    run_start = previous = values[0]
    for value in values[1:] + [None]:  # type: ignore[list-item]
        if value == previous + 1:
            previous = value
            continue
        parts.append(str(run_start) if run_start == previous else f"{run_start}-{previous}")
        if value is None:
            break
        run_start = previous = value
    return ",".join(parts)


def read_workflow_cron(path: Path) -> str:
    match = CRON_LINE.search(path.read_text())
    if not match:
        raise ScheduleError(f"no '- cron:' line found in {path}")
    return match.group("cron").strip()


def write_workflow_cron(path: Path, cron: str) -> bool:
    """Rewrite the workflow's cron line. Returns True if it changed."""
    text = path.read_text()
    match = CRON_LINE.search(text)
    if not match:
        raise ScheduleError(f"no '- cron:' line found in {path}")
    if match.group("cron").strip() == cron:
        return False
    replacement = f'{match.group("indent")}"{cron}"'
    path.write_text(text[: match.start()] + replacement + text[match.end():])
    return True

