"""Render an analysis as the page someone actually reads.

Three rules govern the layout, and all three are about trust rather than looks:

1. **A ceiling is never printed as a saving.** Every dollar figure appears under the words "at
   most", with the reason it is a ceiling directly beneath it. A reader who quotes our number to
   their CFO and is wrong is a reader we lose permanently.
2. **Every figure carries its rows.** "L2, L3, L4 and 17 more" is printed under each finding, so
   any claim can be traced back into the file it came from.
3. **What the file cannot say is printed, not hidden.** It sits in the same visual weight as the
   evidence, because it is the reason to believe the evidence.

An old or expired price table does not stop the report: the table is checked by hand and this
is a personal tool, so it will age. Instead the report says so beside the date the prices were
checked, where the reader judging the dollar figures will see it.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal

from mycroftcompute.analysis.costing import COST_DISCREPANCY, Costed, ranked, share
from mycroftcompute.analysis.findings import Analysis, Finding
from mycroftcompute.economics.prices import PRICES_CHECKED_ON, StalePriceTableError, verify_prices
from mycroftcompute.parsers.model import ParsedUsage, Severity
from mycroftcompute.store.history import Comparison

WIDTH = 84
_INDENT = "  "
_BULLET = "  - "

_VENDOR_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google"}

#: How the page refers to whatever it read. Saying "the file you uploaded" when nothing was
#: uploaded is the first thing a reader notices, and it undermines every figure beneath it.
_SOURCE_NOUN = {
    "file": "this file",
    "metrics": "your metrics backend",
    "transcripts": "these transcripts",
}
#: "these transcripts shows" is the kind of thing a reader trusts a page less for.
_SOURCE_VERB = {"file": "shows", "metrics": "shows", "transcripts": "show"}

_SOURCE_PROVENANCE = {
    "file": "the rows of the file you uploaded, and each finding names the rows it came from",
    "metrics": "daily totals queried read-only from your metrics backend",
    "transcripts": "the usage accounting in your local Claude Code transcripts",
}


def _vendors(parsed: ParsedUsage) -> str:
    """"Anthropic and OpenAI usage" - the headline no vendor's own console can print.

    A metrics backend instrumented through the GenAI conventions returns every provider at once,
    so heading the page with one vendor's name would understate what we just read.
    """
    names = [
        _VENDOR_NAMES.get(str(v), str(v).title()) for v in sorted(parsed.all_vendors, key=str)
    ]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _explained_by_1h_writes(costed: Costed) -> bool:
    """Whether charging cache writes at the one-hour rate would close most of the gap."""
    reported = costed.reported_total
    if reported is None or costed.total_if_writes_1h <= costed.total:
        return False
    return abs(costed.total_if_writes_1h - reported) < abs(costed.total - reported)


def _rule(char: str = "=") -> str:
    return char * WIDTH


def _wrap(text: str, *, initial: str, subsequent: str) -> str:
    # `break_on_hyphens` is off because almost every proper noun on this page is hyphenated -
    # claude-sonnet-4-6, gen_ai.client.token.usage, a repository name - and splitting one across
    # a line break makes a reader doubt they are reading the identifier we meant.
    return textwrap.fill(
        text,
        width=WIDTH,
        initial_indent=initial,
        subsequent_indent=subsequent,
        break_on_hyphens=False,
    )


def _bullets(items: tuple[str, ...] | list[str]) -> list[str]:
    """One bullet per item - except an item that arrives already indented.

    Findings emit sub-lines for their per-model and per-developer tables. Bulleting those turns
    a table into a second list and loses the fact that they belong to the line above.
    """
    out: list[str] = []
    for item in items:
        if item.startswith("  "):
            out.append(_wrap(item.strip(), initial="      ", subsequent="        "))
        else:
            out.append(_wrap(item, initial=_BULLET, subsequent="    "))
    return out


def _period(parsed: ParsedUsage) -> str:
    days = sorted({r.day for r in parsed.records})
    if not days:
        return "no dated rows"
    first, last = days[0], days[-1]
    if first == last:
        return first.strftime("%-d %B %Y") if _supports_dash() else first.isoformat()
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}-{last.day} {first:%B %Y} ({len(days)} days with usage)"
    return f"{first.isoformat()} to {last.isoformat()} ({len(days)} days with usage)"


def _supports_dash() -> bool:
    """`%-d` is glibc-only; Windows uses `%#d` and raises otherwise."""
    try:
        date(2026, 1, 5).strftime("%-d")
    except ValueError:
        return False
    return True


def _count(noun: str, values: list[str]) -> str:
    """"1 model: claude-sonnet-4-6" - never "1 model(s)", which reads like a draft."""
    plural = noun if len(values) == 1 else f"{noun}s"
    return f"{len(values)} {plural}: {', '.join(values)}"


def _source_ref(lines: tuple[int, ...]) -> str:
    """"L2, L3, L4 and 25 more" - enough to find the rows without printing all of them.

    The count must be the true remainder. A finding that covers 28 rows and says "and 17 more"
    invites the reader to add it up, find 20, and stop believing the rest of the page.
    """
    # Not every source has line numbers. Claude Code transcripts aggregate thousands of
    # messages into daily records, so "L0, L0, L0" would be a citation to nothing - worse than
    # no citation, because it looks like one.
    if not lines or not any(lines):
        return ""
    shown = [f"L{n}" for n in lines[:3]]
    rest = len(lines) - len(shown)
    joined = ", ".join(shown)
    return f"{joined} and {rest} more" if rest > 0 else joined


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def reconciliation(costed: Costed) -> list[str]:
    """Our total against the source's own cost figure, as the paragraphs a reader sees.

    Empty when the source reported nothing, or when the two agree closely. Shared by every
    renderer, so the text report and the web page can never explain the same gap differently.
    """
    gap = costed.discrepancy
    if gap is None or abs(gap) < COST_DISCREPANCY:
        return []
    # Worth surfacing rather than silently preferring one number: a real gap usually means a
    # negotiated rate, a batch tier, or a model we are pricing wrong.
    reported = costed.reported_total or Decimal(0)
    first = (
        f"Your source reports {_money(reported)} for the same period, {abs(gap) * 100:.0f}% "
        + ("below" if gap > 0 else "above")
        + " our figure."
    )
    # The cache-write TTL is the first thing to check, not the last. A metrics backend carries no
    # TTL dimension, so we charge the cheaper five-minute rate - and real Claude Code traffic
    # writes at the one-hour rate almost all of the time, which is exactly a 60% difference on
    # those tokens. Against a live stack this accounted for the whole gap, while the page was
    # busy suggesting a negotiated rate.
    if _explained_by_1h_writes(costed):
        return [
            first
            + f" At the one-hour cache-write rate our figure would be "
            f"{_money(costed.total_if_writes_1h)}, which is close enough to account for that. "
            f"These metrics do not report which TTL a write used, so we charge the cheaper "
            f"five-minute rate - understating rather than inflating. Claude Code writes at the "
            f"one-hour TTL almost all of the time, so on Claude Code traffic the higher figure "
            f"is the likelier one."
        ]
    return [
        first
        + " That usually means a negotiated rate or a batch tier, both of which make the real "
        "cost lower than list."
    ]


def _finding_block(index: int, finding: Finding, noun: str, verb: str) -> list[str]:
    # Titles carry model and repository names, so their length is not ours to control. Wrapping
    # is the only thing that keeps the page pasteable into an email at any width.
    heading = _wrap(f"FINDING {index} - {finding.title}", initial="", subsequent="  ")
    out: list[str] = ["", _rule("-"), heading, _rule("-"), ""]

    out.append(f"{_INDENT}What {noun} {verb}")
    out.extend(_bullets(finding.evidence))

    if finding.ceiling is not None:
        out.append("")
        # "At most" is doing load-bearing work here. It is the difference between a number a
        # reader can defend and one that embarrasses them.
        out.append(f"{_INDENT}At most {_money(finding.ceiling)} over this period")
        out.append(
            _wrap(
                "This is the gap between what these tokens cost and what they would cost under "
                "the cheaper option. It is a ceiling, not a saving - see below.",
                initial=_INDENT + "  ",
                subsequent=_INDENT + "  ",
            )
        )

    out.append("")
    out.append(f"{_INDENT}What {noun} cannot tell you")
    out.extend(_bullets(finding.cannot_tell))

    out.append("")
    out.append(f"{_INDENT}Next")
    out.append(_wrap(finding.next_action, initial=_INDENT + "  ", subsequent=_INDENT + "  "))

    ref = _source_ref(finding.source_lines)
    if ref:
        out.append("")
        out.append(f"{_INDENT}From rows {ref}")
    return out


def _since_block(since: Comparison) -> list[str]:
    """What moved since the previous read.

    This section is the reason the product is a subscription, and the reason it has to be
    careful. It reports what changed. It does not claim to have caused any of it - a team
    migrates, a project ends, somebody rewrites a prompt for reasons of their own - and it will
    not turn a shorter month into a saving.
    """
    out: list[str] = ["", "SINCE LAST TIME"]

    previous, current = since.previous_period, since.current_period
    if previous and current:
        out.append(
            f"{_INDENT}Comparing {current[0].isoformat()} to {current[1].isoformat()} "
            f"against {previous[0].isoformat()} to {previous[1].isoformat()}."
        )

    if since.previous_total is not None and since.current_total is not None:
        delta = since.spend_delta or Decimal(0)
        direction = "more" if delta > 0 else "less"
        if since.comparable_periods and since.previous_total:
            pct = abs(delta) / since.previous_total * 100
            out.append(
                f"{_INDENT}{_money(since.current_total)} against "
                f"{_money(since.previous_total)} - {_money(abs(delta))} {direction}, "
                f"{pct:.0f}%."
            )
        else:
            # Different-length windows produce a percentage that is entirely a calendar
            # artefact. Both totals, no arithmetic between them.
            out.append(
                f"{_INDENT}{_money(since.current_total)} against "
                f"{_money(since.previous_total)}."
            )
            out.append(
                _wrap(
                    "The two periods are different lengths, so we are not putting a percentage "
                    "on that. Compare equal windows if you want one.",
                    initial=_INDENT,
                    subsequent=_INDENT,
                )
            )

    appeared = [m for m in since.movements if m.state == "appeared"]
    resolved = [m for m in since.movements if m.state == "resolved"]
    continued = [m for m in since.movements if m.state == "continued"]

    for label, group in (("New", appeared), ("No longer firing", resolved)):
        if group:
            out.append("")
            out.append(f"{_INDENT}{label}")
            out.extend(_wrap(m.title, initial=_BULLET, subsequent="    ") for m in group)

    moved = [m for m in continued if m.delta is not None and m.delta != 0]
    if moved:
        out.append("")
        out.append(f"{_INDENT}Still open, and the ceiling moved")
        for m in moved:
            was, now = m.was or Decimal(0), m.now or Decimal(0)
            arrow = "down" if now < was else "up"
            out.append(
                _wrap(
                    f"{m.title} - {_money(was)} {arrow} to {_money(now)}",
                    initial=_BULLET,
                    subsequent="    ",
                )
            )

    if resolved:
        out.append("")
        out.append(
            _wrap(
                "A finding that stopped firing is a finding that stopped firing. We cannot tell "
                "whether anything you did caused it, and we are not going to claim it did.",
                initial=_INDENT,
                subsequent=_INDENT,
            )
        )
    return out


def render(
    parsed: ParsedUsage,
    analysis: Analysis,
    *,
    today: date,
    since: Comparison | None = None,
) -> str:
    """Render the report, noting in it when the price table has aged."""
    try:
        verify_prices(today)
        price_warning = ""
    except StalePriceTableError as error:
        price_warning = f" Warning: {error}"

    lines: list[str] = [
        _rule(),
        "MYCROFT",
        f"{_vendors(parsed)} usage - {_period(parsed)}",
        _rule(),
        "",
    ]

    # ---------------------------------------------------------------- what we read ---
    noun = _SOURCE_NOUN.get(parsed.source_kind, "this file")
    verb = _SOURCE_VERB.get(parsed.source_kind, "shows")
    lines.append("WHAT WE READ")
    read = f"{parsed.rows_accepted:,} row" + ("" if parsed.rows_accepted == 1 else "s")
    if parsed.rows_rejected:
        read += f", {parsed.rows_rejected:,} skipped because we could not read them"
    lines.append(f"{_INDENT}{read}")

    models = sorted({r.model for r in parsed.records})
    lines.append(_wrap(_count("model", models), initial=_INDENT, subsequent=_INDENT + "  "))

    projects = sorted({r.project for r in parsed.records if r.project})
    if projects:
        lines.append(
            _wrap(_count("project", projects), initial=_INDENT, subsequent=_INDENT + "  ")
        )

    totals = {
        "input (uncached)": sum(r.input_tokens or 0 for r in parsed.records),
        "input (from cache)": sum(r.cached_input_tokens or 0 for r in parsed.records),
        "input (cache writes)": sum(r.cache_write_tokens or 0 for r in parsed.records),
        "output": sum(r.output_tokens or 0 for r in parsed.records),
    }
    for label, value in totals.items():
        if value:
            lines.append(f"{_INDENT}{value:>18,}  {label} tokens")

    # ---------------------------------------------------------------- what it cost ---
    # The first question anyone asks, and for a while this page answered it nowhere: the only
    # dollar figures were buried inside findings.
    costed = analysis.costed
    # "No token counts" and "token counts we hold no price for" are different sentences. A live
    # stack reported a model missing from the price table, and the page then claimed the export
    # carried no tokens while printing four token totals directly above it.
    had_tokens = any(r.total_input_tokens or r.output_tokens for r in parsed.records)
    if costed is not None and costed.total <= 0 and costed.reported_total and not had_tokens:
        # A cost-only export carries money but no token counts, so there is nothing for us to
        # price. Showing their figure is right; pretending we derived it is not.
        lines.extend(
            [
                "",
                "WHAT IT COST",
                f"{_INDENT}{_money(costed.reported_total)} as reported by your source",
                _wrap(
                    "This export carries costs but no token counts, so this is their figure "
                    "rather than ours. We cannot check it, and the analysis that needs token "
                    "detail could not run.",
                    initial=_INDENT,
                    subsequent=_INDENT,
                ),
            ]
        )
    elif costed is not None and costed.total <= 0 and costed.reported_total and had_tokens:
        lines.extend(
            [
                "",
                "WHAT IT COST",
                f"{_INDENT}{_money(costed.reported_total)} as reported by your source",
                _wrap(
                    "We could not price this ourselves: every model in it is one we hold no "
                    "published price for, named below. The token counts above are real; the "
                    "dollar figure is your backend's rather than ours, and we cannot check it.",
                    initial=_INDENT,
                    subsequent=_INDENT,
                ),
            ]
        )
    elif costed is not None and costed.total > 0:
        lines.extend(["", "WHAT IT COST", f"{_INDENT}{_money(costed.total)} at list prices"])

        for label, bucket in (
            ("by model", costed.by_model),
            ("by developer", costed.by_user),
            ("by repository", costed.by_repo),
            ("by request kind", costed.by_query_source),
            ("by reasoning effort", costed.by_effort),
        ):
            if not bucket:
                continue
            lines.append("")
            lines.append(f"{_INDENT}{label}")
            for name, amount in ranked(bucket)[:6]:
                pct = share(amount, costed.total) * 100
                lines.append(f"{_INDENT}  {_money(amount):>12}  {pct:>5.1f}%  {name}")
            if len(bucket) > 6:
                rest = sum((a for _, a in ranked(bucket)[6:]), Decimal(0))
                lines.append(
                    f"{_INDENT}  {_money(rest):>12}         {len(bucket) - 6} more"
                )

        if costed.unpriced_models:
            lines.append("")
            lines.append(
                _wrap(
                    "Not costed, because we hold no published price for them: "
                    + ", ".join(costed.unpriced_models)
                    + ". Their usage is counted above but no dollar figure includes them.",
                    initial=_INDENT,
                    subsequent=_INDENT,
                )
            )

        paragraphs = reconciliation(costed)
        for paragraph in paragraphs:
            lines.append("")
            lines.append(_wrap(paragraph, initial=_INDENT, subsequent=_INDENT))

    # ------------------------------------------------------------- since last time ---
    if since is not None and since.has_history:
        lines.extend(_since_block(since))

    # -------------------------------------------------------------------- findings ---
    if analysis.findings:
        for i, finding in enumerate(analysis.findings, start=1):
            lines.extend(_finding_block(i, finding, noun, verb))
    else:
        lines.extend(
            [
                "",
                _rule("-"),
                "NOTHING TO REPORT",
                _rule("-"),
                "",
                _wrap(
                    "None of the checks this data could support found anything worth raising. "
                    "That is a real result, not an empty page. Any check this data could not "
                    "support is listed below with the reason, so silence here never stands in "
                    "for a check that did not run.",
                    initial=_INDENT,
                    subsequent=_INDENT,
                ),
            ]
        )

    # ----------------------------------------------------------------------- notes ---
    problems = [i for i in parsed.issues if i.severity is not Severity.INFO]
    if problems or analysis.notes:
        lines.extend(["", _rule("-"), "WHAT WE COULD NOT DO", _rule("-"), ""])
        lines.extend(_bullets([i.message for i in problems]))
        lines.extend(_bullets(analysis.notes))

    # ------------------------------------------------------------------ provenance ---
    lines.extend(
        [
            "",
            _rule("-"),
            "HOW TO CHECK THIS",
            _rule("-"),
            "",
            _wrap(
                "Every figure above is arithmetic on "
                + _SOURCE_PROVENANCE.get(parsed.source_kind, _SOURCE_PROVENANCE["file"])
                + f". Model prices were checked against each vendor's own pricing page on "
                f"{PRICES_CHECKED_ON.isoformat()}." + price_warning,
                initial=_INDENT,
                subsequent=_INDENT,
            ),
            "",
            _wrap(
                "Those are list prices. If you have negotiated rates, committed-use discounts, "
                "or run work through a batch tier, your real rates are lower and every dollar "
                "figure here is correspondingly high.",
                initial=_INDENT,
                subsequent=_INDENT,
            ),
            "",
            _wrap(
                "We have not seen any of your prompts. This report is built from token counts "
                "and dates.",
                initial=_INDENT,
                subsequent=_INDENT,
            ),
            "",
        ]
    )

    return "\n".join(lines)
