"""What we can see in a backend, and what each blind spot costs.

This is the first screen of the onboarding and the most important one. A customer who has just
pasted a credential is deciding whether to trust us, and the decision is made in about ten
seconds. Two ways to lose it: print nothing until the full report arrives, or print a clean page
that quietly omits the half we could not read.

So every capability is listed whether or not it is present, and an absence carries the finding it
costs rather than a shrug. "No developer identity" means nothing to a reader; "per-developer
figures cannot be produced, and that is usually the first thing a platform lead asks for" is
something they can act on - by changing their telemetry, or by deciding they do not care.

It renders from discovery alone, before a single day of data is read. That matters for onboarding
speed and it matters for honesty: these are facts about the instrumentation, not about the bill.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mycroftcompute.sources.metrics_names import field_for_kind

_CACHE_FIELDS = frozenset({"cached_input_tokens", "cache_write_tokens"})


@runtime_checkable
class Discovery(Protocol):
    """What both connectors' discovery results have in common."""

    @property
    def token_metric(self) -> str | None: ...
    @property
    def cost_metric(self) -> str | None: ...
    @property
    def kind_label(self) -> str | None: ...
    @property
    def kinds(self) -> tuple[str, ...]: ...
    @property
    def model_label(self) -> str | None: ...
    @property
    def vendor_label(self) -> str | None: ...
    @property
    def user_label(self) -> str | None: ...
    @property
    def repo_label(self) -> str | None: ...
    @property
    def segment_labels(self) -> tuple[str, ...]: ...
    @property
    def knows_cache(self) -> bool: ...
    @property
    def knows_tokens(self) -> bool: ...
    @property
    def all_names(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    present: bool

    #: What we found, when we found something.
    detail: str = ""

    #: What its absence costs, in the reader's terms rather than ours.
    cost: str = ""


def checks(found: Discovery) -> list[Check]:
    families = getattr(found, "families", None)
    metrics = ", ".join(f.metric for f in families) if families else (found.token_metric or "")

    return [
        Check(
            "Token counts",
            found.token_metric is not None,
            metrics,
            "Nothing can be priced without them. Turn on coding-agent telemetry - org-wide "
            "through managed settings rather than developer by developer - and try again once a "
            "day of data has accumulated.",
        ),
        Check(
            "Input/output split",
            found.kind_label is not None,
            found.kind_label or "",
            "Input and output are priced five to ten times apart, and without a label separating "
            "them a single total cannot be costed at all. We will not split one on a guessed "
            "ratio.",
        ),
        Check(
            "Model names",
            found.model_label is not None,
            found.model_label or "",
            "Prices are per model, so without this nothing can be costed and no swap can be "
            "evaluated.",
        ),
        Check(
            "Cache split",
            found.knows_cache,
            # Only the cache kinds. Listing input and output here would read as though they
            # were evidence of caching, which they are not.
            ", ".join(k for k in found.kinds if field_for_kind(k) in _CACHE_FIELDS),
            "The cache finding - usually the largest - cannot run. We will not report a zero hit "
            "rate from a missing dimension, because that and a cold cache are different things.",
        ),
        Check(
            "Developer identity",
            found.user_label is not None,
            found.user_label or "",
            "Per-developer figures cannot be produced, and that is usually the first thing a "
            "platform lead asks for. It is often stripped deliberately: per-user labels multiply "
            "cardinality, which is what metrics backends bill on.",
        ),
        Check(
            "Repository",
            found.repo_label is not None,
            found.repo_label or "",
            "Spend cannot be attached to a codebase, so the question 'is this where our work is "
            "meant to be?' goes unanswered.",
        ),
        Check(
            "Request segments",
            bool(found.segment_labels),
            ", ".join(found.segment_labels),
            "Without query_source or effort we cannot separate the agent's own housekeeping from "
            "work somebody is waiting on. That segment is usually the cheapest thing to move, "
            "because nobody reads its output and it holds no session cache. Only an agent's own "
            "metrics carry these - the OpenTelemetry GenAI conventions have no equivalent.",
        ),
        Check(
            "Reported cost",
            found.cost_metric is not None,
            found.cost_metric or "",
            "Not needed. We price from token counts, and where a backend does report its own "
            "cost we compare rather than trust it.",
        ),
    ]


WIDTH = 84
_INDENT = "  "


def _fill(text: str, *, initial: str = "", subsequent: str = "") -> str:
    """Wrapped, without breaking a hyphenated identifier across two lines."""
    return textwrap.fill(
        text,
        width=WIDTH,
        initial_indent=initial,
        subsequent_indent=subsequent,
        break_on_hyphens=False,
    )


def render_readiness(found: Discovery, *, where: str) -> str:
    """The page a customer sees seconds after connecting."""
    rows = checks(found)
    lines = [
        "=" * WIDTH,
        "MYCROFT - WHAT WE CAN SEE",
        where,
        "=" * WIDTH,
        "",
        f"{_INDENT}{len(found.all_names):,} metric names visible to these credentials.",
        "",
    ]

    for check in rows:
        mark = "OK" if check.present else "--"
        head = f"{_INDENT}[{mark}] {check.name}"
        if check.detail:
            # Metric names are long and there can be several. Wrapping keeps the page
            # pasteable into an email, which is where this screen usually ends up.
            lines.append(
                _fill(f"{head:<28}  {check.detail}", subsequent=_INDENT + " " * 28)
            )
        else:
            lines.append(head)
        if not check.present:
            lines.append(_fill(check.cost, initial=_INDENT + "       ", subsequent=_INDENT + "       "))

    blocking = [c for c in rows if not c.present and c.name in _REQUIRED]
    lines.extend(["", "-" * WIDTH, ""])
    if blocking:
        lines.append(
            _fill(
                "We cannot produce a report from this backend yet. "
                + " ".join(f"{c.name} is missing." for c in blocking),
                initial=_INDENT,
                subsequent=_INDENT,
            )
        )
    else:
        missing = [c.name for c in rows if not c.present and c.name != "Reported cost"]
        lines.append(f"{_INDENT}Ready. Run without --discover to read a period and price it.")
        if missing:
            lines.append(
                _fill(
                    "Some findings will be unavailable: " + ", ".join(missing) + ". The report "
                    "names each one it could not run, rather than leaving a gap.",
                    initial=_INDENT,
                    subsequent=_INDENT,
                )
            )
    lines.append("")
    return "\n".join(lines)


#: Without these there is no report at all, only an explanation.
_REQUIRED = frozenset({"Token counts", "Input/output split", "Model names"})
