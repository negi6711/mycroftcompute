"""Read Claude Code's own transcripts as a usage source.

Anyone on a Pro or Max subscription has no API bill to export - they pay a flat fee - so the
console usage CSV does not exist for them. But Claude Code writes a JSONL transcript per session
to `~/.claude/projects/`, and every assistant message in it carries a full `usage` object. That
is a richer source than any console export: it reports the five-minute and one-hour cache-write
split, which no vendor CSV does, and it is per-message rather than per-day.

**This reader touches usage accounting only.** The transcripts contain entire conversations -
prompts, file contents, tool output. Nothing here reads `message.content`, and a test asserts it,
because a cost tool that quietly ingests the text of everything you have ever asked would be a
worse product than no tool at all.

What a subscription transcript can and cannot say is worth stating plainly, and the report does:
the dollar figures are what this usage *would have cost at API list prices*. On a flat-fee plan
that is not what you paid. It is still the right number for "should we move this workload to the
API", and it is the wrong number for "what did last month cost".
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from mycroftcompute.parsers.model import Issue, ParsedUsage, ParseError, Severity, UsageRecord
from mycroftcompute.vendors import Vendor

#: Only these keys are ever read out of a transcript record. Everything else - and in particular
#: anything under `message.content` - is ignored by construction rather than by care.
USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)

#: A transcript line longer than this is not an accounting record; skip it without parsing.
MAX_LINE_BYTES = 8 * 1024 * 1024


def _as_int(value: object) -> int:
    """Token counts only. Anything that is not a non-negative integer is discarded, never
    coerced - a string where a count should be means the format changed, not that it was zero."""
    return value if isinstance(value, int) and value >= 0 else 0


def _day_of(record: dict[str, object]) -> str | None:
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _project_of(record: dict[str, object]) -> str | None:
    """The working directory's last segment: what you were working on.

    The full path is not used. It is the one field here that could carry something personal -
    a client name in a folder - and the leaf is enough to group by.
    """
    cwd = record.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    return Path(cwd.replace("\\", "/")).name or None


def transcript_paths(root: Path) -> list[Path]:
    """Every transcript under a directory, or the file itself if one was named."""
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.jsonl"))


def parse_claude_code(root: Path) -> ParsedUsage:
    """Read transcripts into daily per-model records.

    Aggregated by (day, model, project) so the output has the same shape as a console export,
    with `requests` set to the number of assistant messages - which is what makes the
    premium-over-allocation finding work on this source too.
    """
    paths = transcript_paths(root)
    if not paths:
        raise ParseError(
            "no_transcripts",
            f"No Claude Code transcripts under {root}. They live in ~/.claude/projects/ as "
            f".jsonl files, one directory per working directory.",
        )

    # (day, model, project) -> accumulated counts
    buckets: dict[tuple[str, str, str | None], dict[str, int]] = defaultdict(
        lambda: dict.fromkeys((*USAGE_FIELDS, "cache_write_1h", "requests"), 0)
    )
    seen = 0
    skipped = 0
    tiers: set[str] = set()

    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if len(line) > MAX_LINE_BYTES:
                        skipped += 1
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        skipped += 1
                        continue
                    if not isinstance(record, dict):
                        continue

                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    model = message.get("model")
                    if not isinstance(usage, dict) or not isinstance(model, str):
                        continue
                    # Claude Code writes synthetic records for its own bookkeeping; they carry
                    # no real billing and would show up as a phantom model in the report.
                    if model.startswith("<"):
                        continue

                    day = _day_of(record)
                    if day is None:
                        skipped += 1
                        continue

                    seen += 1
                    bucket = buckets[(day, model, _project_of(record))]
                    for field in USAGE_FIELDS:
                        bucket[field] += _as_int(usage.get(field))
                    creation = usage.get("cache_creation")
                    if isinstance(creation, dict):
                        bucket["cache_write_1h"] += _as_int(
                            creation.get("ephemeral_1h_input_tokens")
                        )
                    bucket["requests"] += 1
                    tier = usage.get("service_tier")
                    if isinstance(tier, str):
                        tiers.add(tier)
        except OSError:
            skipped += 1
            continue

    if not buckets:
        raise ParseError(
            "no_usage_records",
            f"Read {len(paths)} transcript(s) and found no assistant messages with usage "
            f"accounting in them. Either these are very old transcripts or the format changed.",
        )

    records = [
        UsageRecord(
            day=datetime.fromisoformat(day).date(),
            model=model,
            input_tokens=counts["input_tokens"],
            cached_input_tokens=counts["cache_read_input_tokens"],
            cache_write_tokens=counts["cache_creation_input_tokens"],
            cache_write_1h_tokens=counts["cache_write_1h"],
            output_tokens=counts["output_tokens"],
            requests=counts["requests"],
            project=project,
            service_tier=sorted(tiers)[0] if len(tiers) == 1 else None,
        )
        for (day, model, project), counts in sorted(buckets.items())
    ]

    issues = [
        Issue(
            code="subscription_not_billed_per_token",
            severity=Severity.WARNING,
            message=(
                "These are Claude Code transcripts, so the dollar figures below are what this "
                "usage would cost at API list prices. On a Pro or Max subscription that is not "
                "what you paid - you paid a flat fee. Treat it as the cost of running the same "
                "work through the API, not as a bill."
            ),
        )
    ]
    if skipped:
        issues.append(
            Issue(
                code="unreadable_transcript_lines",
                severity=Severity.INFO,
                message=(
                    f"{skipped:,} transcript line(s) could not be read and were left out rather "
                    f"than counted as zero."
                ),
            )
        )

    return ParsedUsage(
        vendor=Vendor.ANTHROPIC,
        records=records,
        # Every field this source can fill. `cost` is absent on purpose: a subscription
        # transcript has no billed amount, and inventing one is the mistake to avoid.
        fields_present=frozenset(
            {
                "day",
                "model",
                "project",
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
                "requests",
            }
        ),
        rows_seen=seen,
        rows_accepted=seen,
        rows_rejected=skipped,
        issues=issues,
        source_kind="transcripts",
    )
