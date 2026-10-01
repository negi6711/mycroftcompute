"""Read Claude Code usage, price it with the prompt cache counted, and print what is worth changing.

    mycroftcompute                         your own Claude Code history (~/.claude/projects)
    mycroftcompute --sample                an invented 24-developer team, to see the report
    mycroftcompute --grafana <url> --user <instance-id>
    mycroftcompute --datadog eu1

Credentials are read from the environment by default, never written anywhere, and used only to
query. Nothing is uploaded, and nothing is stored unless you pass --db.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import date, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from mycroftcompute import sample
from mycroftcompute.analysis.findings import analyse
from mycroftcompute.economics.prices import StalePriceTableError, verify_prices
from mycroftcompute.parsers.claude_code import parse_claude_code
from mycroftcompute.parsers.model import ParsedUsage, ParseError
from mycroftcompute.report.readiness import render_readiness
from mycroftcompute.report.text import render
from mycroftcompute.sources.base import SourceError
from mycroftcompute.sources.datadog import SITES, DatadogEndpoint, DatadogSource
from mycroftcompute.sources.grafana import GrafanaSource, PrometheusEndpoint
from mycroftcompute.store.history import (
    Comparison,
    compare,
    load_pull,
    open_store,
    save,
)

PROG = "mycroftcompute"


def _version() -> str:
    try:
        return version(PROG)
    except PackageNotFoundError:
        return "unknown"


def default_transcripts() -> Path:
    """Where Claude Code keeps its session files on this machine.

    `CLAUDE_CONFIG_DIR` moves the whole ~/.claude directory, so it moves the transcripts too.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "projects"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Where your Claude Code money went, priced with the prompt cache counted, and which "
            "changes would actually pay. With no arguments it reads your own Claude Code history."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help=(
            "a directory of Claude Code transcripts. Defaults to ~/.claude/projects "
            "(or $CLAUDE_CONFIG_DIR/projects)."
        ),
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="print the report for an invented 24-developer team, to see what the report shows",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {_version()}")
    metrics = parser.add_argument_group(
        "reading a team's metrics backend",
        "For a team that already sends Claude Code telemetry to Grafana or Datadog. Nothing is "
        "uploaded; the backend is queried read-only.",
    )
    metrics.add_argument(
        "--grafana",
        metavar="URL",
        help=(
            "Prometheus-compatible query endpoint - Grafana Cloud, Grafana OSS or plain "
            "Prometheus, e.g. https://prometheus-prod-01.grafana.net/api/prom"
        ),
    )
    metrics.add_argument(
        "--user",
        metavar="ID",
        help="Grafana Cloud metrics instance ID. Omit for an unauthenticated local Prometheus.",
    )
    metrics.add_argument(
        "--token",
        metavar="TOKEN",
        help=(
            "Cloud Access Policy token with the metrics:read scope. An ordinary Grafana API key "
            "or service account token will not work for querying metrics. "
            "Reads MYCROFT_METRICS_TOKEN if omitted, so it need not appear in shell history."
        ),
    )
    metrics.add_argument(
        "--datadog",
        metavar="SITE",
        nargs="?",
        const="us1",
        help=(
            "read from Datadog instead. Takes the site your org is on ("
            + ", ".join(sorted(SITES))
            + "; default us1). If you pick the wrong one we will tell you which it is. "
            "Needs --token as the API key and --app-key as the Application key."
        ),
    )
    metrics.add_argument(
        "--app-key",
        metavar="KEY",
        help=(
            "Datadog Application key, scoped to metrics_read and timeseries_query. Reads "
            "MYCROFT_DATADOG_APP_KEY if omitted. The API key goes in --token."
        ),
    )
    metrics.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="how many days back to read (default 30)",
    )
    metrics.add_argument(
        "--discover",
        action="store_true",
        help=(
            "check the connection and stop: what we can see, what we cannot, and what each "
            "blind spot costs. Run this first on a backend nobody has seen before."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=(
            "keep a history at this path and compare this read with the last one. Re-reading "
            "overlapping days corrects them rather than double-counting."
        ),
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=date.today(),
        help="override today's date (used to judge how old the price table is)",
    )
    args = parser.parse_args(argv)

    if args.grafana and args.datadog:
        parser.error("read one backend at a time: --grafana or --datadog, not both")

    if args.sample:
        return _sample()

    if args.grafana or args.datadog:
        return _from_metrics(args)

    path = args.path or default_transcripts()
    if not path.exists():
        print(
            f"{PROG}: no Claude Code transcripts at {path}. Claude Code keeps them in "
            "~/.claude/projects once you have used it; to read a team's Grafana or Datadog "
            "instead, see --help. To see a report without any data, run with --sample.",
            file=sys.stderr,
        )
        return 2

    try:
        parsed = parse_claude_code(path)
    except ParseError as error:
        print(f"{PROG}: {error.message}", file=sys.stderr)
        return 1
    return _emit(parsed, args.today, db=args.db)


def _sample() -> int:
    """The invented team's second month, with its comparison against the first."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.db"
        _first, second = sample.write_history(path)
        connection = open_store(path)
        try:
            parsed = load_pull(connection, second)
            since = compare(connection, second)
        finally:
            connection.close()
    print(render(parsed, analyse(parsed), today=sample.GENERATED_ON, since=since))
    print("\n(An invented team. The arithmetic is real.)")
    return 0


def _source(args: argparse.Namespace) -> GrafanaSource | DatadogSource:
    """Whichever backend was asked for.

    Credentials come from the environment by default. A key on the command line ends up in shell
    history and in `ps` output, where it outlives the session and is readable by other users, so
    the environment variable is the documented path and the flag is the convenience.
    """
    token = args.token or os.environ.get("MYCROFT_METRICS_TOKEN")
    if args.datadog:
        app_key = args.app_key or os.environ.get("MYCROFT_DATADOG_APP_KEY")
        if not token or not app_key:
            raise SourceError(
                "missing_credentials",
                "Datadog needs two keys: an API key (--token or MYCROFT_METRICS_TOKEN) and an "
                "Application key (--app-key or MYCROFT_DATADOG_APP_KEY). The Application key is "
                "the one that carries the scopes, and it needs metrics_read and "
                "timeseries_query.",
            )
        return DatadogSource(DatadogEndpoint(api_key=token, app_key=app_key, site=args.datadog))
    return GrafanaSource(PrometheusEndpoint(args.grafana, args.user, token))


def _from_metrics(args: argparse.Namespace) -> int:
    """Read from a metrics backend rather than local transcripts."""
    try:
        source = _source(args)

        if args.discover:
            where = f"Datadog ({args.datadog})" if args.datadog else args.grafana
            print(render_readiness(source.discover(), where=where))
            return 0

        end = args.today
        parsed = source.fetch(end - timedelta(days=args.days), end)
    except SourceError as error:
        print(f"{PROG}: {error.message}", file=sys.stderr)
        return 1

    return _emit(parsed, args.today, db=args.db, backend="datadog" if args.datadog else "grafana")


def _emit(
    parsed: ParsedUsage,
    today: date,
    *,
    db: Path | None = None,
    backend: str | None = None,
) -> int:
    """Render, warning first if the price table has aged.

    Prices are checked by hand against each vendor's page, and this is a personal project, so the
    table will age. An old table still gives the right shape and roughly the right money, so the
    report prints, but with a warning above it that says how old the prices are.
    """
    analysis = analyse(parsed)
    try:
        verify_prices(today)
    except StalePriceTableError as error:
        print(
            f"{PROG}: warning - the price table may be out of date, so dollar figures may be "
            f"off.\n{error}\n",
            file=sys.stderr,
        )

    since: Comparison | None = None
    if db is not None:
        connection = open_store(db)
        try:
            since = compare(connection, save(connection, parsed, analysis, backend=backend))
        finally:
            connection.close()

    print(render(parsed, analysis, today=today, since=since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
