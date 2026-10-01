"""Reading more than one metric family, and combining them without double-counting.

A company usually has more than one. The vendor-neutral `gen_ai.client.token.usage` gives breadth
- every provider side by side - and carries no `query_source` or `effort`, because the GenAI
conventions have no equivalent. An agent's own metric gives depth on that agent alone. Reading
only the first found loses whichever half matters more, and the half that matters more is usually
the segments: "38% of your Opus spend is the agent talking to itself" is the finding that sells.

The rule for combining them is the whole of this module, and it is a subtraction rather than an
addition:

**Families are never summed.** Where two report the same model, one is chosen and the reader is
told. Summing would double that model's bill, and a doubled bill is the one error that ends the
relationship - far worse than a missing dimension, which we can at least describe.

In practice the families are usually disjoint: Claude Code's own metric covers Claude models, the
standard covers whatever else was instrumented. So the common case is a clean union, and the
precedence rule below only bites in the uncommon one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mycroftcompute.sources.metrics_names import field_for_kind
from mycroftcompute.sources.records import Accumulator


@dataclass(slots=True)
class Family:
    """One metric, with the labels that belong to it.

    Labels are per-metric rather than per-backend because the two vocabularies do not mix: the
    standard metric carries `gen_ai.request.model`, Claude Code's carries `model`, and a company
    with both has both spellings live at once.
    """

    metric: str

    kind_label: str | None = None
    kinds: tuple[str, ...] = ()

    vendor_label: str | None = None
    model_label: str | None = None
    user_label: str | None = None
    repo_label: str | None = None
    segment_labels: tuple[str, ...] = ()

    @property
    def knows_tokens(self) -> bool:
        """Whether this family's series can be split into input and output.

        Without the kind label the metric is one undifferentiated total, and input and output are
        priced five to ten times apart - so there is nothing safe to do with it.
        """
        return self.kind_label is not None

    @property
    def knows_cache(self) -> bool:
        return any(field_for_kind(k) == "cached_input_tokens" for k in self.kinds)

    @property
    def knows_segments(self) -> bool:
        """Whether this family can say which requests were the agent's own housekeeping."""
        return bool(self.segment_labels)


@dataclass(slots=True)
class Part:
    """One family's reading, ready to be merged with the others."""

    family: Family
    accumulator: Accumulator
    #: True when this family's tokens also carry the separate cache metrics.
    knows_cache: bool = False

    @property
    def models(self) -> set[str]:
        return {key.model for key in self.accumulator.tokens}


@dataclass(slots=True)
class Merged:
    accumulator: Accumulator = field(default_factory=Accumulator)

    #: Models more than one family reported. Named in the report, because the reader deserves to
    #: know which figures came from where when the sources disagreed about who owns a model.
    contested: tuple[str, ...] = ()

    #: Which family won each contested model, for the sentence that explains it.
    resolved_by: dict[str, str] = field(default_factory=dict)

    knows_cache: bool = False
    knows_tokens: bool = False
    knows_segments: bool = False


def merge(parts: list[Part]) -> Merged:
    """Combine families by model, preferring the richer source and never adding two together.

    Precedence on a contested model goes to the family that knows its segments. That is the
    agent's own metric, and it is the better authority for its own traffic: it is emitted by the
    process that made the request, rather than by whatever else happened to be instrumented.
    """
    usable = [p for p in parts if p.family.knows_tokens and p.accumulator.tokens]
    if not usable:
        return Merged()

    owner: dict[str, Part] = {}
    contested: set[str] = set()

    for part in usable:
        for model in part.models:
            held = owner.get(model)
            if held is None:
                owner[model] = part
                continue
            contested.add(model)
            # The richer family wins. Ties keep the incumbent, which is the earlier family and
            # therefore the standard - the one with the broader view.
            if part.family.knows_segments and not held.family.knows_segments:
                owner[model] = part

    out = Merged(
        knows_tokens=True,
        knows_cache=any(p.family.knows_cache or p.knows_cache for p in usable),
        knows_segments=any(p.family.knows_segments for p in usable),
    )

    for part in usable:
        for key, counts in part.accumulator.tokens.items():
            if owner.get(key.model) is not part:
                continue
            for field_name, count in counts.items():
                if count:
                    out.accumulator.add_tokens(key, field_name, count)
            # A key with no counts at all still has to survive, or a cost-only row disappears.
            out.accumulator.tokens.setdefault(key, dict(counts))
        for key, amount in part.accumulator.costs.items():
            if owner.get(key.model) is part:
                out.accumulator.add_cost(key, amount)
        out.accumulator.systems |= part.accumulator.systems

    out.contested = tuple(sorted(contested))
    out.resolved_by = {m: owner[m].family.metric for m in out.contested}
    return out


def contested_message(merged: Merged) -> str:
    lines = ", ".join(f"{model} (read from {merged.resolved_by[model]})" for model in merged.contested)
    return (
        f"More than one metric family reports these models: {lines}. We read each of them from "
        f"one family rather than adding the families together, because summing them would double "
        f"that model's bill. If some of that traffic came from outside the agent, it is not "
        f"counted here."
    )
