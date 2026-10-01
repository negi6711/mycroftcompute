"""What the coding agents actually call things, across two vocabularies and two backends.

There is a vendor-neutral standard - OpenTelemetry's GenAI semantic conventions - and there are
each agent's own richer metrics. Both land in the same metrics backend, and a company may have
either or both.

The standard is the one that makes this product multi-vendor: `gen_ai.client.token.usage` carries
`gen_ai.system` (anthropic, openai, gcp.gemini, aws.bedrock, ...) so a single query returns every
provider side by side, which is the thing no vendor's own dashboard can do.

The agent-specific metrics are richer where they exist. Claude Code's `claude_code.token.usage`
carries `query_source` (main, subagent, auxiliary) and `effort`, which are the attributes that
make routing analysis possible - `auxiliary` is the agent's own housekeeping, and a low-effort
request is one the agent already decided did not need deep reasoning.

**Names arrive in two spellings.** Prometheus-compatible backends flatten dots and hyphens to
underscores and append `_total` to counters; Datadog keeps the dots and lowercases every tag key
and value. Both spellings live in the lists below, in preference order, because only one of them
will exist in any given backend and asking for both costs nothing.

Nothing here is assumed at query time. Discovery asks the backend which of these it has.
"""

from __future__ import annotations

from mycroftcompute.vendors import Vendor

#: Token-count metric families, each in both spellings, standard first.
#:
#: These are families rather than one ordered list because a company usually has **more than
#: one**, and reading only the first loses the half that matters. The standard metric gives
#: breadth - every vendor side by side - and carries no `query_source` or `effort`, because the
#: GenAI conventions have no equivalent. An agent's own metric gives depth on that agent alone.
#: A company running Claude Code alongside Codex typically has both, covering different models.
#:
#: They are never summed. Where two families report the same model we take one and say so,
#: because adding them would double that model's bill.
TOKEN_FAMILIES = (
    ("gen_ai.client.token.usage", "gen_ai_client_token_usage"),  # the standard - breadth
    ("claude_code.token.usage", "claude_code_token_usage"),  # Anthropic's own - depth
    ("agento11y.token.usage", "agento11y_token_usage"),  # Grafana's plugins
)

#: Every token stem, flattened, for callers that only want to know "is this one of ours".
TOKEN_STEMS = tuple(stem for family in TOKEN_FAMILIES for stem in family)

#: Cost metrics. Fewer backends carry these; we price from tokens regardless.
COST_STEMS = (
    "gen_ai.client.cost",
    "gen_ai_client_cost",
    "claude_code.cost.usage",
    "claude_code_cost_usage",
    "agento11y.cost",
    "agento11y_cost",
)

#: Separate metrics some backends expose for cache accounting, when it is not a token type.
CACHE_METRIC_STEMS: dict[str, tuple[str, ...]] = {
    "cached_input_tokens": (
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai_usage_cache_read_input_tokens",
    ),
    "cache_write_tokens": (
        "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai_usage_cache_creation_input_tokens",
    ),
}

#: The label carrying which kind of token a series counts.
KIND_LABELS = ("gen_ai.token.type", "gen_ai_token_type", "type")

#: Which provider served the request. `gen_ai.system` is marked deprecated in the registry, so a
#: newer name is in flight - look for both rather than pick one.
VENDOR_LABELS = (
    "gen_ai.provider.name",
    "gen_ai_provider_name",
    "gen_ai.system",
    "gen_ai_system",
    "vendor",
    "provider",
)

MODEL_LABELS = (
    "gen_ai.request.model",
    "gen_ai_request_model",
    "gen_ai.response.model",
    "gen_ai_response_model",
    "model",
)

#: Developer identity. OTel's `user.id` arrives as `user_id` on Prometheus and `user.id` on
#: Datadog; Grafana's plugins use their own.
USER_LABELS = (
    "user.email",
    "user_email",
    "user.id",
    "user_id",
    "agento11y_tag_user",
    "user",
)

REPO_LABELS = (
    "repository",
    "repo",
    "agento11y_tag_repo",
    "git.repository",
    "git_repository",
)

#: Attributes that make routing analysis possible, and only exist on agent-specific metrics.
#: `auxiliary` is the agent's own housekeeping rather than anyone's work.
SEGMENT_LABELS = ("query_source", "effort")

#: Token kinds, in both vocabularies, mapped to the field they belong in. Looked up through
#: `field_for_kind`, never directly: the standard uses snake_case, the agent metrics use
#: camelCase for the same ideas, and Datadog lowercases every tag value on ingestion - so
#: `cacheRead` arrives as `cacheread` there and would silently match nothing.
KIND_TO_FIELD: dict[str, str] = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheRead": "cached_input_tokens",
    "cache_read": "cached_input_tokens",
    "cached_input": "cached_input_tokens",
    "cacheCreation": "cache_write_tokens",
    "cache_creation": "cache_write_tokens",
}

_KIND_TO_FIELD_FOLDED = {
    "".join(c for c in key.lower() if c.isalnum()): value for key, value in KIND_TO_FIELD.items()
}


def field_for_kind(value: str | None) -> str | None:
    """Which record field a token-kind label value belongs in, or None if we do not know it.

    Case and separators are folded away, so `cacheRead`, `cache_read` and Datadog's lowercased
    `cacheread` all land in the same place. An unrecognised kind returns None and is dropped
    rather than folded into another field - a new token type we have never seen must not be
    quietly added to the input total.
    """
    if not value:
        return None
    return _KIND_TO_FIELD_FOLDED.get("".join(c for c in value.lower() if c.isalnum()))


#: Suffixes that mean "this series is the total", in preference order. A counter exported over
#: OTLP gains `_total` on Prometheus and keeps its bare name on Datadog; a histogram gains `_sum`
#: or `.sum` on both.
TOTAL_SUFFIXES = ("_total", "_sum", ".sum")

#: Suffixes that are emphatically **not** a total, and picking one is how a report ends up
#: reporting bucket counts as tokens. `gen_ai.client.token.usage` is a Histogram in the GenAI
#: conventions, so a backend holding it also holds `..._bucket`, `..._count`, `..._min` and
#: `..._max` alongside the sum. `_count` is the number of *measurements*, and instrumentation
#: records one per token type, so it is not even a request count.
NOT_A_TOTAL_SUFFIXES = (
    "_bucket",
    ".bucket",
    "_count",
    ".count",
    "_min",
    ".min",
    "_max",
    ".max",
    "_avg",
    ".avg",
    "_quantile",
    ".quantile",
    ".distribution",
)


def best_metric(stems: tuple[str, ...], names: list[str]) -> str | None:
    """The real metric name for the first stem a backend has, or None.

    Stems are tried in order, so the vendor-neutral standard wins over an agent-specific metric
    when both are present. Within a stem the choice is by suffix rather than by length, because
    the longest name for a histogram is `..._bucket` and reading that as a token count would put
    a confidently wrong number on the page.
    """
    for stem in stems:
        matches = [n for n in names if n == stem or n.startswith(stem)]
        usable = [n for n in matches if not n.endswith(NOT_A_TOTAL_SUFFIXES)]
        if not usable:
            continue
        for suffix in TOTAL_SUFFIXES:
            for name in sorted(usable):
                if name.endswith(suffix):
                    return name
        # A bare counter, with neither a unit nor a suffix appended.
        return sorted(usable, key=len)[0]
    return None


#: `gen_ai.system` values to our vendor enum. Bedrock and Vertex are deliberately absent: they
#: are delivery platforms that serve other vendors' models, so the model name decides, not the
#: platform. A Claude model on Bedrock is priced as Anthropic.
SYSTEM_TO_VENDOR: dict[str, Vendor] = {
    "anthropic": Vendor.ANTHROPIC,
    "openai": Vendor.OPENAI,
    "azure.ai.openai": Vendor.OPENAI,
    "azure.ai.inference": Vendor.OPENAI,
    "gcp.gemini": Vendor.GOOGLE,
    "gcp.vertex_ai": Vendor.GOOGLE,
    "gcp.gen_ai": Vendor.GOOGLE,
}


def vendor_from_system(value: str | None) -> Vendor | None:
    """Map a `gen_ai.system` value to a vendor, or None when it does not name one.

    Used only as a fallback. The model name is the better signal, because a platform like
    Bedrock serves several vendors' models and the price table is keyed by model.
    """
    if not value:
        return None
    return SYSTEM_TO_VENDOR.get(value.strip().lower())
