"""Entry point: fetch, pre-filter, classify, notify."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .classify import ClassifierError, GeminiClassifier, Verdict
from .config import Config, ConfigError, Search, load_config, load_dotenv
from .state import SearchState, State
from .subito import Ad, SubitoClient
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
) -> Counts:
    counts = Counts()
    search_state = state.for_search(search.name)


    cutoff = search_state.cutoff(search.cold_start_minutes, now)
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
        "--no-classify", action="store_true",
        help="skip the LLM pass; report every new ad (useful for tuning filters)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="verify config and credentials, then exit without searching",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def check(config: Config, dry_run: bool) -> int:
    searches = config.searches
    print(f"✓ config: {len(searches)} search(es)")
    for s in searches:
        print(f"    {s.name}: {s.query!r}, filters={s.filters or '{}'}")

    ok = True

    # Reachability is the thing most likely to be wrong on a new machine, and
    # it is the one check that says which transport actually carried the
    # request — direct, or through which borrowed IP.
    client = SubitoClient(impersonate=config.fetch.impersonate)
    try:
        ads = list(client.search("bici", max_pages=1))
        print(f"✓ subito: {len(ads)} ads (impersonating {client.impersonate})")
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




    searches = config.searches

    if args.only:
        wanted = set(args.only)
        unknown = wanted - {s.name for s in searches}
        if unknown:
            log.error("no such search(es): %s", ", ".join(sorted(unknown)))
            return 2
        searches = [s for s in searches if s.name in wanted]

    if args.check:
        return check(config, args.dry_run)

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


    state = State.load(args.state)
    client = SubitoClient(impersonate=config.fetch.impersonate)

    total = Counts()
    failed = False
    for search in searches:
        try:
            counts = run_search(
                search, state, client, classifier, notifier, now
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
