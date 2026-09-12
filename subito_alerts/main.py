"""Entry point: fetch, pre-filter, classify, notify."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .classify import ClassifierError, GeminiClassifier, Verdict
from .config import Config, ConfigError, Search, load_config, load_dotenv
from .state import SearchState, State
from .subito import Ad, SubitoClient
from .schedule import (
    ScheduleError,
    read_workflow_cron,
    required_cron,
    write_workflow_cron,
)
from .telegram import TelegramError, TelegramNotifier

log = logging.getLogger("subito_alerts")


@dataclass
class Counts:
    fetched: int = 0
    skipped_seen: int = 0
    skipped_old: int = 0
    skipped_price: int = 0
    skipped_keyword: int = 0
    classified: int = 0
    matched: int = 0
    sent: int = 0


def prefilter(
    ad: Ad, search: Search, state: SearchState, cutoff: datetime, counts: Counts
) -> bool:
    """Cheap local checks, run before we spend any LLM quota on an ad."""
    if state.has_seen(ad.id):
        counts.skipped_seen += 1
        return False
    if ad.posted_at and ad.posted_at < cutoff:
        counts.skipped_old += 1
        return False

    # subito's price filter is server-side, but ads with no price slip through it.
    lo, hi = search.filters.get("price_min"), search.filters.get("price_max")
    if ad.price is not None:
        if (lo is not None and ad.price < lo) or (hi is not None and ad.price > hi):
            counts.skipped_price += 1
            return False

    if search.exclude_keywords:
        haystack = f"{ad.title} {ad.body}".lower()
        if any(word in haystack for word in search.exclude_keywords):
            counts.skipped_keyword += 1
            return False
    return True


def run_search(
    search: Search,
    state: State,
    client: SubitoClient,
    classifier: GeminiClassifier | None,
    notifier: TelegramNotifier | None,
    now: datetime,
    ignore_interval: bool,
) -> Counts:
    counts = Counts()
    search_state = state.for_search(search.name)

    if not ignore_interval and not search_state.is_due(search.interval_minutes, now):
        log.info(
            "[%s] not due yet (every %dm, last run %s)",
            search.name, search.interval_minutes, search_state.last_run,
        )
        return counts

    cutoff = search_state.cutoff(search.interval_minutes, now)
    log.info("[%s] searching %r for ads since %s", search.name, search.query, cutoff)

    candidates: list[Ad] = []
    for ad in client.search(
        search.query, search.filters, max_pages=search.max_pages, stop_before=cutoff
    ):
        counts.fetched += 1
        if prefilter(ad, search, search_state, cutoff, counts):
            candidates.append(ad)

    log.info(
        "[%s] %d fetched → %d new candidate(s) "
        "(seen %d, old %d, price %d, keyword %d)",
        search.name, counts.fetched, len(candidates),
        counts.skipped_seen, counts.skipped_old,
        counts.skipped_price, counts.skipped_keyword,
    )

    if candidates:
        if classifier is None:
            verdicts = [
                Verdict.unclassified(ad, "classification disabled") for ad in candidates
            ]
        else:
            verdicts = classifier.classify(candidates, search.prompt)
        counts.classified = len(verdicts)

        for verdict in verdicts:
            status = "MATCH " if verdict.interested else "reject"
            if verdict.failed:
                status = "UNCLS "
            log.info(
                "  %s %-7s %s — %s",
                status,
                verdict.ad.price_text or "n/a",
                verdict.ad.title[:50],
                verdict.reason,
            )

            if verdict.interested:
                counts.matched += 1
                if notifier is not None:
                    try:
                        notifier.send(verdict, search.name)
                        counts.sent += 1
                    except TelegramError as exc:
                        # Don't mark as seen — retry it on the next run.
                        log.error("failed to notify for ad %s: %s", verdict.ad.id, exc)
                        continue

            # Rejects are recorded too, so we never pay to classify them twice.
            search_state.mark_seen(verdict.ad.id)

    search_state.last_run = now
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subito-alerts",
        description="Run subito.it searches, filter them with an LLM, alert on Telegram.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("searches.yaml"),
        help="path to searches.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "-s", "--state", type=Path, default=Path("state.json"),
        help="path to the state file (default: %(default)s)",
    )
    parser.add_argument(
        "--search", action="append", dest="only", metavar="NAME",
        help="run only this search (repeatable)",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="print results instead of sending to Telegram; state is not saved",
    )
    parser.add_argument(
        "--ignore-interval", action="store_true",
        help="run every search regardless of when it last ran",
    )
    parser.add_argument(
        "--no-classify", action="store_true",
        help="skip the LLM pass; report every new ad (useful for tuning filters)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="verify config and credentials, then exit without searching",
    )
    parser.add_argument(
        "--sync-schedule", action="store_true",
        help="regenerate the workflow's cron from searches.yaml, then exit",
    )
    parser.add_argument(
        "--workflow", type=Path, default=Path(".github/workflows/alerts.yml"),
        help="workflow file to sync (default: %(default)s)",
    )
    parser.add_argument(
        "--install-launchd", action="store_true",
        help="install a launchd agent on this Mac that runs the bot on schedule",
    )
    parser.add_argument(
        "--ignore-schedule", action="store_true",
        help="run even outside the configured active hours",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def sync_schedule(config: Config, workflow: Path) -> int:
    """Rewrite the workflow cron from searches.yaml. The config always wins."""
    cron = required_cron(config.schedule, config.min_interval)
    try:
        current = read_workflow_cron(workflow)
    except ScheduleError as exc:
        log.error("%s", exc)
        return 2
    if write_workflow_cron(workflow, cron):
        print(f"updated {workflow}\n  {current}  ->  {cron}")
    else:
        print(f"{workflow} already up to date ({cron})")
    return 0


def describe_schedule(config: Config, workflow: Path) -> None:
    wanted = required_cron(config.schedule, config.min_interval)
    print(f"✓ schedule: {config.schedule.describe()}, every {config.min_interval}m")
    try:
        current = read_workflow_cron(workflow)
    except ScheduleError:
        print(f"  cron (not synced to a workflow): {wanted}")
        return
    if current == wanted:
        print(f"  workflow cron: {current} (in sync)")
    else:
        print(f"  ✗ workflow cron is {current!r}, config wants {wanted!r}")
        print("    fix with: python -m subito_alerts.main --sync-schedule")


def check(config: Config, workflow: Path, dry_run: bool) -> int:
    searches = config.searches
    print(f"✓ config: {len(searches)} search(es)")
    for s in searches:
        print(f"    {s.name}: {s.query!r} every {s.interval_minutes}m, filters={s.filters or '{}'}")
    describe_schedule(config, workflow)

    ok = True
    try:
        classifier = GeminiClassifier()
        shape = classifier.ping()
        print(f"✓ gemini: model {classifier.model!r} reachable via {shape}")
    except ClassifierError as exc:
        print(f"✗ gemini: {exc}")
        ok = False

    if dry_run:
        print("- telegram: skipped (--dry-run)")
    else:
        try:
            print(f"✓ telegram: connected as @{TelegramNotifier().check()}")
        except TelegramError as exc:
            print(f"✗ telegram: {exc}")
            ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.sync_schedule:
        return sync_schedule(config, args.workflow)

    if args.install_launchd:
        return install_launchd(config, Path.cwd())


    searches = config.searches

    if args.only:
        wanted = set(args.only)
        unknown = wanted - {s.name for s in searches}
        if unknown:
            log.error("no such search(es): %s", ", ".join(sorted(unknown)))
            return 2
        searches = [s for s in searches if s.name in wanted]

    if args.check:
        return check(config, args.workflow, args.dry_run)

    classifier = None
    if not args.no_classify:
        try:
            classifier = GeminiClassifier()
        except ClassifierError as exc:
            # Fail open: still report the ads, flagged as unclassified.
            log.error("%s — continuing without classification", exc)

    notifier = None
    if not args.dry_run:
        try:
            notifier = TelegramNotifier()
        except TelegramError as exc:
            log.error("%s", exc)
            return 2

    now = datetime.now(timezone.utc).astimezone()

    # The cron only wakes us up; local time decides whether we act. This is what
    # makes "08:00-22:00 Rome" correct across DST, which a UTC cron cannot be.
    if not args.ignore_schedule and not config.schedule.is_active(now):
        log.info(
            "outside active hours (%s) — nothing to do",
            config.schedule.describe(),
        )
        return 0

    state = State.load(args.state)
    client = SubitoClient()

    total = Counts()
    failed = False
    for search in searches:
        try:
            counts = run_search(
                search, state, client, classifier, notifier, now, args.ignore_interval
            )
        except Exception:
            # One broken search must not cost us the others, or the state file.
            log.exception("[%s] failed", search.name)
            failed = True
            continue
        for field_name in vars(total):
            setattr(total, field_name,
                    getattr(total, field_name) + getattr(counts, field_name))

    if args.dry_run:
        log.info("dry run — state not saved")
    else:
        state.save()

    log.info(
        "done: %d fetched, %d classified, %d matched, %d sent",
        total.fetched, total.classified, total.matched, total.sent,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
