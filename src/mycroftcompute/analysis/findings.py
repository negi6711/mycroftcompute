"""The three findings a usage export can support, and nothing beyond them.

Each finding states what the data shows, what it *cannot* show, and a lever with a published
ceiling. None of them claims a saving, because a usage export cannot prove one:

* it shows that tokens were billed at full price, not that those tokens were cacheable;
* it shows that a premium model handled short requests, not that a cheaper one would have
  answered them acceptably;
* it shows that usage continues at the weekend, not that the traffic is unwanted.

So every finding carries `cannot_tell`, and every dollar figure is a `ceiling` - the most the
lever could be worth if every assumption went our way, which they will not. Calling that a saving
would be the one lie that discredits the whole report.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from mycroftcompute.economics.prices import ModelPrice, models_by_vendor, resolve_model
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord

if TYPE_CHECKING:
    from mycroftcompute.analysis.costing import Costed

#: Below this share of input tokens served from cache, the caching finding is worth raising.
#: Datadog measured 28% of calls showing any cached read at all, so most teams clear this bar
#: only by accident.
LOW_CACHE_HIT_RATE = Decimal("0.50")

#: At or above this share of the bill spent writing the prompt cache, the writes are worth a
#: finding. On live Claude Code traffic, where around 96% of input is already served from cache,
#: writes were 41% of the bill at the five-minute rate - the largest line, and the one no
#: dashboard separates out.
CACHE_WRITE_SHARE = Decimal("0.20")

#: A premium model answering requests this small is a candidate for a cheaper one. Not proof:
#: a short prompt can still need a strong model.
SHORT_REQUEST_TOKENS = 2_000

#: A model is "premium" if a cheaper one from the same vendor lists at or below this fraction
#: of its input price. Relative, because "expensive" means nothing without an alternative.
CHEAPER_ALTERNATIVE_RATIO = Decimal("0.5")

#: Weekend share of a flat, automated workload. Two days in seven is 28.6%; traffic at or above
#: this is running without humans.
AUTOMATED_WEEKEND_SHARE = Decimal("0.25")

#: Below this many identifiable developers, a finding about the team's shape is really a finding
#: about a few people's habits. Shared by every finding that reasons about a team, so they
#: cannot drift into disagreeing about what "a team" is.
MIN_DEVELOPERS = 4

_PER_MTOK = Decimal(1_000_000)


class FindingKind(StrEnum):
    UNCACHED_INPUT = "uncached_input"
    CACHE_WRITES = "cache_writes"
    DEVELOPER_CONCENTRATION = "developer_concentration"
    REPOSITORY_CONCENTRATION = "repository_concentration"
    MODEL_MIX = "model_mix"
    ROUTABLE_SEGMENT = "routable_segment"
    PREMIUM_OVER_ALLOCATION = "premium_over_allocation"
    CONTINUOUS_USAGE = "continuous_usage"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    title: str

    #: Statements with the numbers in them. Each one is derived from the rows, never asserted.
    evidence: tuple[str, ...]

    #: What this export cannot settle. Printed, not hidden - it is the reason to trust the rest.
    cannot_tell: tuple[str, ...]

    next_action: str

    #: The most this lever could be worth, if every uncertainty resolved in our favour. Never
    #: described as a saving.
    ceiling: Decimal | None = None

    subject: str | None = None

    #: **Every** row this finding was computed from, not a sample. The renderer shows the first
    #: few and counts the rest, and that count has to be true - provenance that undercounts is
    #: worse than none, because it invites a reader to check and find the numbers do not tie.
    source_lines: tuple[int, ...] = ()


@dataclass(slots=True)
class Analysis:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unpriced_models: tuple[str, ...] = ()

    #: What the whole read cost, broken down. The report leads with it, because "what did this
    #: cost" is the first question and the page used to answer it nowhere.
    costed: Costed | None = None


def _sum(values: list[int | None]) -> int:
    return sum(v for v in values if v is not None)


def _by_model(records: list[UsageRecord]) -> dict[str, list[UsageRecord]]:
    grouped: dict[str, list[UsageRecord]] = defaultdict(list)
    for r in records:
        grouped[r.model].append(r)
    return dict(grouped)


def _money(tokens: int, per_mtok: Decimal) -> Decimal:
    return Decimal(tokens) * per_mtok / _PER_MTOK


def _pct(part: Decimal, whole: Decimal) -> str:
    return f"{(part / whole * 100):.1f}%" if whole else "0.0%"


# ------------------------------------------------------------------ finding 1: caching ---


def uncached_input(parsed: ParsedUsage) -> Finding | None:
    """How much of the input bill was paid at full price when a 10x discount existed.

    The headline. Datadog measured ~69% of input tokens to be system prompts while only 28% of
    calls showed any cached read, so most teams are re-paying for the same static prefix on every
    request. A cached read costs a tenth of list, and on Claude Fable 5.1 a fortieth.
    """
    if not parsed.knows_cache_split:
        return None  # the caller surfaces the missing column; we refuse to invent a rate

    cached = _sum([r.cached_input_tokens for r in parsed.records])
    uncached = _sum([r.input_tokens for r in parsed.records])
    written = _sum([r.cache_write_tokens for r in parsed.records])
    total = cached + uncached + written
    if total == 0 or uncached == 0:
        return None

    hit_rate = Decimal(cached) / Decimal(total)
    if hit_rate >= LOW_CACHE_HIT_RATE:
        return None

    evidence = [
        f"{cached:,} of {total:,} input tokens were served from cache - a "
        f"{_pct(Decimal(cached), Decimal(total))} hit rate.",
        f"{uncached:,} input tokens were billed at full list price.",
    ]

    ceiling = Decimal(0)
    priced = 0
    for model_name, rows in sorted(_by_model(parsed.records).items()):
        price = resolve_model(model_name)
        model_uncached = _sum([r.input_tokens for r in rows])
        if price is None or model_uncached == 0:
            continue
        priced += 1
        at_list = _money(model_uncached, price.input_per_mtok)
        at_cache = _money(model_uncached, price.cache_read_rate(0))
        ceiling += at_list - at_cache
        evidence.append(
            f"{model_name}: {model_uncached:,} uncached input tokens cost "
            f"${at_list:,.2f} at ${price.input_per_mtok}/M; the same tokens read from cache "
            f"would cost ${at_cache:,.2f}."
        )

    return Finding(
        kind=FindingKind.UNCACHED_INPUT,
        title=f"{_pct(Decimal(cached), Decimal(total))} of your input tokens were served from cache",
        evidence=tuple(evidence),
        cannot_tell=(
            "Which of these tokens repeat. A cache only pays for a prefix that is identical "
            "across requests, and a usage export does not show prompt contents - so the figure "
            "above is a ceiling, not a saving.",
            "Whether your prefixes clear each model's cache minimum, which ranges from 512 to "
            "4,096 tokens depending on the model.",
        ),
        next_action=(
            "Check whether your largest prompts put stable content first. Caching keys on an "
            "exact prefix, so a timestamp or a user id near the top of a system prompt defeats "
            "it entirely."
        ),
        ceiling=ceiling if priced else None,
        source_lines=tuple(r.source_line for r in parsed.records),
    )


def cache_writes(parsed: ParsedUsage) -> Finding | None:
    """How much of the bill went on writing the prompt cache rather than reading it.

    On a coding agent the cache already works: the live Claude Code data this was built against
    served 96% of its input from cache, so the uncached-input finding rightly stays silent. The
    money in the cache is on the other side. Every write is billed at a premium over list - 1.25x
    for a five-minute lifetime, 2x for an hour - and every time a warm prefix is written again
    (a new session, a compaction, a model switch, a change to the tools, a pause longer than the
    cache lives) a read at a tenth of list or less becomes a write at more than list.

    The ceiling is what those tokens would have cost read from cache instead. It assumes every
    write was avoidable, which is never true - some context is new and has to be written once -
    so it is an upper limit and the report says so.
    """
    if not parsed.knows_cache_split:
        return None

    from mycroftcompute.analysis.costing import cost_of

    written = _sum([r.cache_write_tokens for r in parsed.records])
    if written == 0:
        return None

    total_cost = Decimal(0)
    write_cost = Decimal(0)
    write_cost_1h = Decimal(0)
    as_reads = Decimal(0)
    per_model: dict[str, list[Decimal]] = {}
    rows: list[UsageRecord] = []

    for record in parsed.records:
        price = resolve_model(record.model)
        if price is None:
            continue
        total_cost += cost_of(record, price)
        tokens = record.cache_write_tokens or 0
        if tokens == 0:
            continue
        n = record.total_input_tokens or 0
        # Where the source reports the one-hour split (Claude Code transcripts do), bill each
        # part at its own rate. Only metrics, which carry no TTL, fall back to five minutes.
        long_lived = min(record.cache_write_1h_tokens or 0, tokens)
        at_5m = _money(tokens - long_lived, price.cache_write_rate(n)) + _money(
            long_lived, price.cache_write_1h_rate(n)
        )
        write_cost += at_5m
        write_cost_1h += _money(tokens, price.cache_write_1h_rate(n))
        as_reads += _money(tokens, price.cache_read_rate(n))
        line = per_model.setdefault(record.model, [Decimal(0), Decimal(0)])
        line[0] += tokens
        line[1] += at_5m
        rows.append(record)

    if total_cost == 0 or write_cost / total_cost < CACHE_WRITE_SHARE:
        return None

    read = _sum([r.cached_input_tokens for r in parsed.records])
    split_known = any(r.cache_write_1h_tokens for r in rows)
    priced_as = (
        "billed at each write's own lifetime"
        if split_known
        else "at the five-minute write rate"
    )
    evidence = [
        f"{written:,} tokens were written to the prompt cache, costing ${write_cost:,.2f} "
        f"{priced_as} - {_pct(write_cost, total_cost)} of the ${total_cost:,.2f} priced here."
        + (
            ""
            if split_known
            else f" At the one-hour rate the same writes would cost ${write_cost_1h:,.2f}."
        ),
        f"{read:,} tokens were read back from cache, about {read / written:,.1f} reads for every "
        f"token written.",
    ]
    if len(per_model) > 1:
        for model, (model_written, amount) in sorted(per_model.items(), key=lambda kv: -kv[1][1]):
            evidence.append(f"{model}: {int(model_written):,} tokens written, ${amount:,.2f}.")
    evidence.append(
        f"Read from cache instead of written, the same tokens would have cost ${as_reads:,.2f}."
    )

    return Finding(
        kind=FindingKind.CACHE_WRITES,
        title=f"{_pct(write_cost, total_cost)} of your bill went on writing the prompt cache",
        evidence=tuple(evidence),
        cannot_tell=(
            "How many of these writes were a warm prefix written again - after a new session, a "
            "compaction, a model switch, a change to the tools or system prompt, or a pause "
            "longer than the cache lives - and how many were new context that had to be written "
            "once. Only the first kind can be avoided, so the figure above is a ceiling, not a "
            "saving.",
            *(
                ()
                if split_known
                else (
                    "Which cache lifetime each write used. These metrics do not say, so every "
                    "write is priced at the cheaper five-minute rate.",
                )
            ),
        ),
        next_action=(
            "Look for what rewrites a warm prefix: sessions restarted or cleared often, tools or "
            "MCP servers that change mid-session, model switches inside a session, and long "
            "pauses. Each one turns a cheap cache read into a write billed above list price."
        ),
        ceiling=write_cost - as_reads,
        source_lines=tuple(r.source_line for r in rows),
    )


# --------------------------------------------------- finding 2: premium over-allocation ---


def _cheaper_sibling(price: ModelPrice) -> ModelPrice | None:
    """The cheapest model from the same vendor that is materially cheaper than this one."""
    for candidate in models_by_vendor(price.vendor):
        if candidate.input_per_mtok <= price.input_per_mtok * CHEAPER_ALTERNATIVE_RATIO:
            return candidate
    return None


def premium_over_allocation(parsed: ParsedUsage) -> Finding | None:
    """A premium model answering consistently small requests.

    Deliberately weak-worded. A short prompt can still need the strongest model available, and a
    usage export cannot tell a hard question from an easy one. What it can show is the shape:
    a $10/M model averaging 700 tokens a request is at least worth someone looking at.
    """
    if "requests" not in parsed.fields_present:
        return None

    for model_name, rows in sorted(_by_model(parsed.records).items()):
        price = resolve_model(model_name)
        requests = _sum([r.requests for r in rows])
        if price is None or requests == 0:
            continue

        total_input = sum(r.total_input_tokens or 0 for r in rows)
        avg_input = total_input // requests
        if avg_input == 0 or avg_input > SHORT_REQUEST_TOKENS:
            continue

        cheaper = _cheaper_sibling(price)
        if cheaper is None:
            continue

        at_current = _money(total_input, price.input_per_mtok)
        at_cheaper = _money(total_input, cheaper.input_per_mtok)
        return Finding(
            kind=FindingKind.PREMIUM_OVER_ALLOCATION,
            title=f"{model_name} answered {requests:,} requests averaging {avg_input:,} input tokens",
            subject=model_name,
            evidence=(
                f"{requests:,} requests, {total_input:,} input tokens, averaging "
                f"{avg_input:,} tokens each - below the {SHORT_REQUEST_TOKENS:,}-token mark "
                f"where a request is usually a short, well-specified task.",
                f"At ${price.input_per_mtok}/M that input cost ${at_current:,.2f}. The same "
                f"tokens on {cheaper.model_id} at ${cheaper.input_per_mtok}/M would cost "
                f"${at_cheaper:,.2f}.",
            ),
            cannot_tell=(
                "Whether these requests need this model. A short prompt can still be a hard "
                "question, and nothing in a usage export distinguishes the two.",
                "Whether output quality would hold on the cheaper model. That needs an "
                "evaluation against your own task, not a billing figure.",
            ),
            next_action=(
                f"Sample a few of these requests. If they are classification, extraction or "
                f"formatting, try them on {cheaper.model_id} and compare the outputs before "
                f"moving anything."
            ),
            ceiling=at_current - at_cheaper,
            source_lines=tuple(r.source_line for r in rows),
        )
    return None


# --------------------------------------------------------- finding 3: continuous usage ---


def _developers(parsed: ParsedUsage) -> set[str]:
    return {r.user for r in parsed.records if r.user}


def weekly_shape_blocked(parsed: ParsedUsage) -> str | None:
    """Why the weekend check cannot run on this data, or None if it can.

    The gate only. What the reader is told lives in `unavailable_checks`, which covers every
    blocked check in one place.
    """
    days = {r.day for r in parsed.records}
    if len(days) < 7:
        return None  # too short to talk about weekly shape at all; nothing worth explaining
    people = _developers(parsed)
    if not people:
        return (
            "The weekend-usage check did not run: it needs developer identity, and this source "
            "carries none. Without it, one person who works Saturdays and a job nobody is "
            "watching look identical."
        )
    if len(people) < MIN_DEVELOPERS:
        return (
            f"The weekend-usage check did not run: it needs at least {MIN_DEVELOPERS} "
            f"identifiable developers, and this data has {len(people)}. On a team that small, "
            f"weekend spend describes somebody's working habits, not an unwatched job."
        )
    return None


def unavailable_checks(parsed: ParsedUsage) -> list[str]:
    """One sentence for every check this data cannot support, and why.

    Silence for "we looked and found nothing" and silence for "we could not look" must never read
    the same. The report promises that any check it could not run is listed with its reason; this
    is what keeps that promise. The cache check explains itself separately in `analyse`.
    """
    notes: list[str] = []
    days = {r.day for r in parsed.records}
    people = _developers(parsed)

    # The two team checks share a bar, so they share a sentence.
    team_checks = ["Developer concentration"]
    if len(days) >= 7:
        team_checks.append("the weekend-usage check")
    names = " and ".join(team_checks)
    verb = "Each needs" if len(team_checks) > 1 else "It needs"
    if not people:
        why = (
            "Without it, one person who works Saturdays and a job nobody is watching look "
            "identical."
            if len(team_checks) > 1
            else "Without it, spend cannot be attached to people."
        )
        notes.append(
            f"{names} did not run. {verb} developer identity, and this source carries none. {why}"
        )
    elif len(people) < MIN_DEVELOPERS:
        notes.append(
            f"{names} did not run. {verb} at least {MIN_DEVELOPERS} identifiable developers, "
            f"and this data has {len(people)}. On a team that small they describe a few "
            f"people's habits rather than a pattern worth acting on."
        )

    if not any(r.repo for r in parsed.records):
        notes.append(
            "Repository concentration did not run: this source carries no repository label. "
            "Claude Code does not emit one."
        )

    if not any(r.query_source or r.effort for r in parsed.records):
        notes.append(
            "The housekeeping check did not run: it needs query_source or effort, which only an "
            "agent's own metrics carry. The vendor-neutral OpenTelemetry GenAI conventions have "
            "no equivalent."
        )

    if not any(r.requests for r in parsed.records):
        notes.append(
            "The short-request check did not run: it needs request counts, which this source "
            "does not carry."
        )
    return notes


def continuous_usage(parsed: ParsedUsage) -> Finding | None:
    """Usage that does not stop at the weekend, which means nobody is driving it.

    The signal is the one daily totals carry: a workload with a real human audience drops at the
    weekend, and one that does not is automated.

    **It only means that across a team.** The first run of this finding against a solo
    developer's own transcripts called their Saturday work a cron job. With one person, "does
    not drop at the weekend" just means that person works weekends. So it needs developer
    identity and at least `MIN_DEVELOPERS` people - the same bar as every other finding that
    reasons about a team - and with identity in hand it says *whose* traffic runs at the
    weekend, which is what makes it actionable: a CI service account shows up by name.
    """
    if weekly_shape_blocked(parsed) is not None:
        return None

    by_bucket: dict[str, int] = defaultdict(int)
    weekend_by_user: dict[str, int] = defaultdict(int)
    days: set[object] = set()
    for r in parsed.records:
        tokens = r.total_input_tokens or 0
        weekend_day = r.day.weekday() >= 5
        by_bucket["weekend" if weekend_day else "weekday"] += tokens
        if weekend_day and r.user:
            weekend_by_user[r.user] += tokens
        days.add(r.day)

    if len(days) < 7:
        return None  # too short a window to talk about weekly shape

    weekend, weekday = by_bucket["weekend"], by_bucket["weekday"]
    total = weekend + weekday
    if total == 0 or weekend == 0:
        return None

    share = Decimal(weekend) / Decimal(total)
    if share < AUTOMATED_WEEKEND_SHARE:
        return None

    drivers = sorted(weekend_by_user.items(), key=lambda kv: (-kv[1], kv[0]))
    evidence = [
        f"{weekend:,} input tokens on Saturdays and Sundays against {weekday:,} on weekdays, "
        f"across {len(days)} days and {len(_developers(parsed))} developers.",
        "Two days in seven is 28.6%. Team spend at or above that share does not drop at the "
        "weekend, which is the shape of a scheduled job - or of a team that works weekends.",
    ]
    if drivers:
        evidence.append("Most of the weekend traffic comes from:")
        evidence.extend(
            f"  {name}: {_pct(Decimal(tokens), Decimal(weekend))} of weekend input"
            for name, tokens in drivers[:3]
        )

    return Finding(
        kind=FindingKind.CONTINUOUS_USAGE,
        title=f"{_pct(Decimal(weekend), Decimal(total))} of input tokens were spent at weekends",
        subject=drivers[0][0] if drivers else None,
        evidence=tuple(evidence),
        cannot_tell=(
            "Whether the identities above are people or jobs. A service account running a "
            "scheduled agent and an engineer who works Saturdays look the same in daily totals; "
            "the name is usually the fastest way to tell.",
            "Whether the work is wanted, if it is automated. Batch pipelines, evaluation runs "
            "and monitoring all look like this and are all deliberate.",
        ),
        next_action=(
            "Check the identities above. If a person is behind the traffic, there is nothing to "
            "do. If a scheduled job is, check it is still needed and whether it could run "
            "through the Batch API, which is a flat 50% discount on work that can wait."
        ),
        ceiling=None,
        source_lines=tuple(r.source_line for r in parsed.records),
    )


# ------------------------------------------------------------------------------ driver ---


def analyse(parsed: ParsedUsage) -> Analysis:
    """Run every finding this data can support, in the order a reader wants them.

    Team findings come first when the source carries developer or repository identity, because
    "where did the money go" is the question that brought the reader here. The export-shaped
    findings follow.
    """
    from mycroftcompute.analysis.costing import cost
    from mycroftcompute.analysis.team import TEAM_FINDINGS

    analysis = Analysis()
    costed = cost(parsed)
    analysis.costed = costed

    for team_detector in TEAM_FINDINGS:
        finding = team_detector(parsed, costed)
        if finding is not None:
            analysis.findings.append(finding)

    for detector in (uncached_input, cache_writes, premium_over_allocation, continuous_usage):
        finding = detector(parsed)
        if finding is not None:
            analysis.findings.append(finding)

    unpriced = sorted({r.model for r in parsed.records if resolve_model(r.model) is None})
    analysis.unpriced_models = tuple(unpriced)
    if unpriced:
        analysis.notes.append(
            "No published price on file for: "
            + ", ".join(unpriced)
            + ". Their usage is counted but not costed, because guessing a rate would put a "
            "wrong number next to a real one."
        )
    analysis.notes.extend(unavailable_checks(parsed))
    if not parsed.knows_cache_split:
        analysis.notes.append(
            "This export does not report cached tokens, so the cache finding - usually the "
            "largest - could not be run."
        )
    return analysis
