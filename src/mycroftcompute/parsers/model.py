"""What a parsed usage export turns into.

The type that matters here is `UsageRecord.cached_input_tokens`, which is `int | None`. None means
*the export did not say*, and it is not the same as zero.

That distinction is the whole product. Our headline finding is a cache hit rate, and an export
without a cached-token column would otherwise produce "0% of your input was cached" - a confident,
alarming, completely fabricated number. Roughly 28% of real LLM calls do show cached reads, so a
fabricated 0% is not even a safe default. When the column is missing we say the column is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from mycroftcompute.vendors import Vendor

__all__ = [
    "Issue",
    "ParseError",
    "ParsedUsage",
    "Severity",
    "UsageRecord",
    "Vendor",
]


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Issue:
    """Something the reader needs to know about the file, in words they can act on."""

    code: str
    message: str
    severity: Severity = Severity.WARNING
    column: str | None = None


class ParseError(Exception):
    """The file cannot be read at all. Carries the reason a person would need to fix it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One row: a day's usage of one model, optionally attributed to a project and a key.

    Token counts are ints because they are counts. Cost is Decimal because it is money. Both are
    optional in their own ways: a usage export often has no cost column, and a cost export often
    has no token columns, which is exactly why the two are kept separate rather than merged into
    one hopeful number.
    """

    day: date
    model: str

    #: Uncached input tokens - what the vendor billed at full list price.
    input_tokens: int | None = None

    #: Tokens served from cache at the read rate. **None means the export did not say.**
    cached_input_tokens: int | None = None

    #: Tokens written into the cache, billed at the write premium. Anthropic reports this
    #: separately; OpenAI and Google fold it into input.
    cache_write_tokens: int | None = None

    #: The portion of `cache_write_tokens` written at Anthropic's one-hour TTL, billed at 2x
    #: list rather than 1.25x. Only Anthropic sells the choice, and only its own transcripts
    #: report the split - a console export does not, so this is usually None.
    cache_write_1h_tokens: int | None = None

    output_tokens: int | None = None
    requests: int | None = None

    cost: Decimal | None = None
    currency: str | None = None

    #: Whatever the vendor calls the owning bucket: OpenAI project, Anthropic workspace,
    #: Google project. Normalised to one field because the question is the same.
    project: str | None = None
    api_key: str | None = None

    #: Who ran it. Present when a metrics backend carries a developer identity label - which is
    #: often deliberately stripped, because per-user labels multiply the cardinality these
    #: backends bill on. None means "not available", never "everyone".
    user: str | None = None

    #: The repository being worked on, where the source reports it.
    repo: str | None = None

    #: Which part of the agent made the request: "main", "subagent", or "auxiliary". Only an
    #: agent's own metrics carry this, and it is the single most useful attribute in the whole
    #: dataset for routing: "auxiliary" is the agent's own housekeeping rather than anyone's
    #: work, so nobody is judging its output and it carries no session cache to break.
    query_source: str | None = None

    #: The reasoning effort the agent asked for - "low" through "max". The agent has already
    #: decided this request did not need deep reasoning; the model it ran on may not reflect it.
    effort: str | None = None

    #: "batch" vs "standard" where the vendor reports it. Batch is a flat 50% discount, so the
    #: share of spend outside it is a finding on its own.
    service_tier: str | None = None

    #: 1-indexed line in the source file, so every figure can be traced back to its row.
    source_line: int = 0

    @property
    def total_input_tokens(self) -> int | None:
        """Every input token the request carried, cached or not. None if we cannot know."""
        parts = [self.input_tokens, self.cached_input_tokens, self.cache_write_tokens]
        known = [p for p in parts if p is not None]
        return sum(known) if known else None


@dataclass(slots=True)
class ParsedUsage:
    """The result of reading one export."""

    vendor: Vendor
    records: list[UsageRecord]

    #: Columns we found, normalised. Drives what the report is allowed to claim.
    fields_present: frozenset[str]

    rows_seen: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0

    currency: str | None = None
    issues: list[Issue] = field(default_factory=list)

    #: What this data came out of, so the report can describe itself honestly. A page that says
    #: "the file you uploaded" when nothing was uploaded is the first thing a reader notices, and
    #: it undermines every figure beneath it.
    source_kind: str = "file"

    #: Every vendor represented in these records. A single file export is one vendor by
    #: construction, but a metrics backend instrumented through the OpenTelemetry GenAI
    #: conventions returns Anthropic, OpenAI and Google side by side - which is the one view no
    #: vendor's own console can give you. Empty means "just `vendor`", so older sources need no
    #: change; read it through `all_vendors`.
    vendors: frozenset[Vendor] = frozenset()

    @property
    def all_vendors(self) -> frozenset[Vendor]:
        return self.vendors or frozenset({self.vendor})

    @property
    def is_multi_vendor(self) -> bool:
        return len(self.all_vendors) > 1

    @property
    def knows_cache_split(self) -> bool:
        """Whether this export can support a cache hit rate at all.

        Gate every caching finding on this. Without the column the honest output is "your export
        does not report cached tokens; here is how to get one that does", not a zero.
        """
        return "cached_input_tokens" in self.fields_present

    @property
    def knows_cost(self) -> bool:
        return "cost" in self.fields_present
