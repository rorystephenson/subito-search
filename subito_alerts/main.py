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
from .proxies import ProxyPool
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


def build_client(config: Config, state: State) -> SubitoClient:
    """A search client, with a proxy pool when the config allows one."""
    pool = None
    if config.proxies.enabled:
        pool = ProxyPool.from_json(
            state.proxies,
            sources=config.proxies.sources,
            min_pool=config.proxies.min_pool,
            probe_concurrency=config.proxies.probe_concurrency,
            probe_batch=config.proxies.probe_batch,
        )
    return SubitoClient(
        pool=pool,
        allow_direct=config.proxies.allow_direct,
        max_proxy_attempts=config.proxies.max_attempts,
        impersonate=config.proxies.impersonate,
    )


def refresh_proxies(config: Config, state_path: Path) -> int:
    """Probe public lists for working proxies and record them in the state."""
    if not config.proxies.enabled:
        print("proxies are disabled (proxies.mode: never)")
        return 0
    state = State.load(state_path)
    client = build_client(config, state)
    pool = client.pool
    assert pool is not None
    before = len(pool.proven_hosts)
    pool.refresh(client.prober())
    state.proxies = pool.to_json()
    state.save()
    print(f"working proxy hosts: {before} -> {len(pool.proven_hosts)} "
          f"({len(pool.proven)} entries, {len(pool.records)} known)")
    for record in sorted(pool.proven, key=lambda r: -r.successes)[:10]:
        print(f"    {record.url:34} ok={record.successes} strikes={record.strikes}")
    return 0


LAUNCHD_LABEL = "com.subito-alerts.agent"

LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>subito_alerts.main</string>
  </array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <!-- Fires this often; the schedule block in searches.yaml decides whether
       there is anything to do, so runs outside active hours exit immediately.
       launchd also runs a missed job once after the Mac wakes. -->
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def install_launchd(config: Config, project: Path) -> int:
    """Install a launchd agent that runs the bot on this Mac.

    Scheduled runners are blocked by subito, so the practical place to run this
    is a machine on a residential connection. The interval comes from
    searches.yaml, same as the workflow cron does.
    """
    import subprocess

    python = project / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable)

    logs = project / "logs"
    logs.mkdir(exist_ok=True)
    plist_path = Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    plist_path.write_text(LAUNCHD_PLIST.format(
        label=LAUNCHD_LABEL,
        python=python,
        workdir=project,
        interval=config.min_interval * 60,
        log=logs / "subito-alerts.log",
    ))

    # bootout first so a reinstall picks up changes rather than silently keeping
    # the old definition; it fails harmlessly when nothing is loaded yet.
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                   capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"✗ launchctl bootstrap failed: {result.stderr.strip()}")
        return 1

    print(f"✓ installed {plist_path}")
    print(f"  runs every {config.min_interval}m, active {config.schedule.describe()}")
    print(f"  logs: {logs / 'subito-alerts.log'}")
    print(f"\n  status:    launchctl list | grep {LAUNCHD_LABEL}")
    print(f"  run now:   launchctl kickstart {domain}/{LAUNCHD_LABEL}")
    print(f"  uninstall: launchctl bootout {domain}/{LAUNCHD_LABEL}")
    return 0


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
        "--refresh-proxies", action="store_true",
        help="probe public proxy lists for working proxies, then exit",
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

    # Reachability is the thing most likely to be wrong on a new machine, and
    # it is the one check that says which transport actually carried the
    # request — direct, or through which borrowed IP.
    client = build_client(config, State.load(Path("state.json")))
    try:
        ads = list(client.search("bici", max_pages=1))
        transport = client._preferred or "direct connection"
        print(f"✓ subito: {len(ads)} ads via {transport} (impersonating {client.impersonate})")
    except Exception as exc:
        print(f"✗ subito: {exc}")
        ok = False

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

    if args.refresh_proxies:
        return refresh_proxies(config, args.state)

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
    client = build_client(config, state)

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

    if client.pool is not None:
        client.pool.prune()
        state.proxies = client.pool.to_json()

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
