"""Model prices and cache parameters for Anthropic, OpenAI and Google.

All three, from the start. They hold roughly 88% of enterprise LLM spend between them (Menlo:
Anthropic 40%, OpenAI 27%, Google 21%) and most production systems run two or three at once, so
a table covering only two vendors would be wrong for the majority of the people we want to serve.

Everything here is a published vendor number, not a derived one. Prices move monthly, so the
table carries the date it was last checked and `verify_prices()` makes a stale table loud rather
than silently wrong.

Money is Decimal throughout. A float here would round a per-token price and the error compounds
across billions of tokens, which is the one thing this product cannot get wrong.

Three mechanics differ by vendor and each of them changes a routing decision:

1. **Cache minimums vary from 512 to 4,096 tokens.** Below the minimum, caching silently does
   nothing. Gemini 3.x needs 4,096; Sonnet needs 1,024. So a prefix in between is cacheable on
   one and permanently uncacheable on the other.
2. **Google's implicit caching has no write premium.** Anthropic and OpenAI charge 1.25x list on
   the request that fills the cache; Google's implicit cache (on by default since Gemini 2.5)
   just starts discounting. That makes Gemini a structurally cheaper model to *move to*, because
   there is no warm-up cost to earn back.
3. **Google charges cache storage by the hour, but only for explicit caching.** Implicit caching
   is free to hold. Anthropic instead charges the holding cost up front, as a 2x write premium
   for a one-hour TTL against 1.25x for five minutes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mycroftcompute.vendors import Vendor

__all__ = [
    "MAX_PRICE_AGE_DAYS",
    "PRICES_CHECKED_ON",
    "TABLE",
    "ModelPrice",
    "StalePriceTableError",
    # Re-exported deliberately: a price and the vendor it belongs to travel together, and one
    # shared enum is the point - two copies were silently never equal.
    "Vendor",
    "models_by_vendor",
    "normalize_model_name",
    "price_of",
    "resolve_model",
    "verify_prices",
]

# The last date every price in TABLE was checked against the vendor's own pricing page.
PRICES_CHECKED_ON = date(2026, 9, 24)

# How long a price table may go unchecked before we refuse to publish a dollar figure from it.
# A month is roughly the observed cadence of vendor price changes.
MAX_PRICE_AGE_DAYS = 31


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """List prices and cache parameters for one model.

    `cache_read_multiplier` and `cache_write_multiplier` are applied to `input_per_mtok`; all
    three vendors express cache pricing that way rather than as an absolute rate.
    """

    model_id: str
    vendor: Vendor
    input_per_mtok: Decimal
    output_per_mtok: Decimal

    # A cached input token costs this fraction of the list input price. 0.1 nearly everywhere,
    # which is the single number the whole routing argument turns on - but Claude Fable 5.1 and
    # Mythos 5.1 charge 0.025x, a 40x discount rather than 10x. That raises the bar for routing
    # away from them fourfold, so it cannot be assumed.
    cache_read_multiplier: Decimal = Decimal("0.1")

    # What the request that fills the cache pays over list. Anthropic and OpenAI charge 1.25x;
    # Google's implicit caching charges nothing, which is why Gemini has no warm-up cost.
    cache_write_multiplier: Decimal = Decimal("1.25")

    # Anthropic sells a longer-lived cache at a higher write price: 2x list for a one-hour TTL
    # against 1.25x for five minutes. This is not a footnote. In a real Claude Code transcript
    # 6,636 of 7,317 cache writes used the one-hour TTL, so pricing every write at 1.25x would
    # understate the dominant case by 60%. None elsewhere - nobody else sells the choice.
    cache_write_1h_multiplier: Decimal | None = None

    # A prompt shorter than this cannot be cached at all, whatever you ask for. It varies from
    # 512 to 4,096 across models and it is the trap in cross-model routing: a swap can make a
    # request uncacheable rather than merely re-cached.
    min_cacheable_tokens: int = 1024

    # Google only: holding an *explicit* cache costs this per million tokens per hour. None
    # everywhere else, including Google's implicit cache, which is free to hold.
    cache_storage_per_mtok_hour: Decimal | None = None

    # Some models bill long prompts at a higher rate. Gemini 3.1 Pro doubles input above 200k
    # tokens (and raises output 1.5x, which is not modelled here because routing decisions turn
    # on input). Left unset, input bills flat.
    long_context_threshold_tokens: int | None = None
    long_context_input_multiplier: Decimal = Decimal("1")

    # Promotional pricing with a published end date. A price that is *scheduled* to change is
    # not caught by the staleness check, because the table is not stale - it is correct today
    # and wrong tomorrow.
    price_valid_until: date | None = None

    def input_rate(self, total_input_tokens: int) -> Decimal:
        """List input price for a prompt of this size, after any long-context surcharge."""
        if (
            self.long_context_threshold_tokens is not None
            and total_input_tokens > self.long_context_threshold_tokens
        ):
            return self.input_per_mtok * self.long_context_input_multiplier
        return self.input_per_mtok

    def cache_read_rate(self, total_input_tokens: int) -> Decimal:
        return self.input_rate(total_input_tokens) * self.cache_read_multiplier

    def cache_write_rate(self, total_input_tokens: int) -> Decimal:
        """The five-minute (or only) cache write rate."""
        return self.input_rate(total_input_tokens) * self.cache_write_multiplier

    def cache_write_1h_rate(self, total_input_tokens: int) -> Decimal:
        """The one-hour cache write rate, falling back to the standard one where there is no
        such option. Never silently cheaper than the short TTL."""
        multiplier = self.cache_write_1h_multiplier or self.cache_write_multiplier
        return self.input_rate(total_input_tokens) * multiplier


# --------------------------------------------------------------------------- the table ---
#
# Cache mechanics, from the vendors' own documentation, checked on PRICES_CHECKED_ON:
#   Anthropic - per-model minimums, 0.1x read, 1.25x/2x write:
#     https://platform.claude.com/docs/en/build-with-claude/prompt-caching
#   OpenAI - 1,024 minimum on GPT-5.6+, 0.1x read, 1.25x write:
#     https://developers.openai.com/api/docs/guides/prompt-caching
#   Google - implicit caching on by default since Gemini 2.5, 4,096 minimum on Gemini 3.x
#   (2,048 on 2.0/2.5), 0.1x read, no write premium, hourly storage on explicit caches only:
#     https://ai.google.dev/gemini-api/docs/caching
#
# LIST PRICES below were checked against the vendors' own pricing pages on PRICES_CHECKED_ON:
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/pricing
#   https://ai.google.dev/gemini-api/docs/pricing
#
# That pass corrected three numbers taken from secondary sources: GPT-5.6 Sol was listed
# elsewhere at $5/$30 as a "regular" rate against a $4/$20 promotion, but the vendor's page
# shows $4/$20 as standard; Gemini 3.1 Flash-Lite holds an explicit cache at $1.00/M/hr rather
# than $0.50; and Gemini 3.8 Flash's whole price line is promotional until 31 December 2026.
#
# Worth knowing for the next model added: the 0.1x cache-read multiplier is not universal even
# within a vendor. Anthropic's Fable 5.1 and Mythos 5.1 charge 0.025x, and OpenAI's older GPT-4o
# family charges 0.5x. Read the number, never assume it.

# Anthropic and OpenAI both take the 0.1x read / 1.25x write defaults on ModelPrice. Google's
# implicit cache costs nothing to fill, so its entries set the write multiplier to 1 explicitly.
_NO_WRITE_PREMIUM = Decimal("1")

TABLE: dict[str, ModelPrice] = {
    m.model_id: m
    for m in (
        # ------------------------------------------------------------------ Anthropic ---
        ModelPrice(
            model_id="claude-fable-5-1",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("10.00"),
            output_per_mtok=Decimal("50.00"),
            # The exception to the 0.1x rule: a cache hit costs 2.5% of list, $0.25/M. So Fable
            # cached is cheaper per token than Sonnet 5 at list, and the bar a target must beat
            # to be worth routing to is four times lower than it looks.
            cache_read_multiplier=Decimal("0.025"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            # Verified 2026-09-24 against platform.claude.com/docs/en/about-claude/pricing,
            # after a live Grafana Cloud read turned up a model the table did not hold.
            # Its cache read is 0.05x base, a third distinct multiplier alongside the 0.1x
            # standard and Fable 5.1's 0.025x - pricing it at 0.1x would double every cached
            # token on the model this product is most likely to be reading.
            model_id="claude-opus-5-5",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("4.00"),
            output_per_mtok=Decimal("20.00"),
            cache_read_multiplier=Decimal("0.05"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-opus-5",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("5.00"),
            output_per_mtok=Decimal("25.00"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-sonnet-5",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("2.00"),
            output_per_mtok=Decimal("10.00"),
            min_cacheable_tokens=1024,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            # The 4.x Opus line shares one price. Present because a company mid-upgrade has
            # several of them live at once, and an unpriced model is a hole in their bill.
            model_id="claude-opus-4-8",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("5.00"),
            output_per_mtok=Decimal("25.00"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-opus-4-7",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("5.00"),
            output_per_mtok=Decimal("25.00"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-opus-4-6",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("5.00"),
            output_per_mtok=Decimal("25.00"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-opus-4-5",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("5.00"),
            output_per_mtok=Decimal("25.00"),
            min_cacheable_tokens=512,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-sonnet-4-6",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            min_cacheable_tokens=1024,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-sonnet-4-5",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            min_cacheable_tokens=1024,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        ModelPrice(
            model_id="claude-haiku-4-5",
            vendor=Vendor.ANTHROPIC,
            input_per_mtok=Decimal("1.00"),
            output_per_mtok=Decimal("5.00"),
            # Four times Sonnet's minimum. This is why a Sonnet -> Haiku swap on a 2k prefix
            # does not move the cache, it removes the possibility of one.
            min_cacheable_tokens=4096,
            cache_write_1h_multiplier=Decimal("2"),
        ),
        # --------------------------------------------------------------------- OpenAI ---
        ModelPrice(
            model_id="gpt-6-astra",
            vendor=Vendor.OPENAI,
            input_per_mtok=Decimal("10.00"),
            output_per_mtok=Decimal("50.00"),
            min_cacheable_tokens=1024,
        ),
        ModelPrice(
            model_id="gpt-5-6-sol",
            vendor=Vendor.OPENAI,
            # Vendor lists $4.00 input against $0.40 cached input, confirming the 0.1x read.
            # A secondary source had this at $5/$30 as the "regular" rate with $4/$20 as
            # promotional; the pricing page shows $4/$20 as standard. Trust the vendor.
            input_per_mtok=Decimal("4.00"),
            output_per_mtok=Decimal("20.00"),
            min_cacheable_tokens=1024,
        ),
        ModelPrice(
            model_id="gpt-5-6-terra",
            vendor=Vendor.OPENAI,
            input_per_mtok=Decimal("2.00"),
            output_per_mtok=Decimal("12.00"),
            min_cacheable_tokens=1024,
        ),
        ModelPrice(
            model_id="gpt-5-4-mini",
            vendor=Vendor.OPENAI,
            input_per_mtok=Decimal("0.75"),
            output_per_mtok=Decimal("4.50"),
            min_cacheable_tokens=1024,
        ),
        ModelPrice(
            model_id="gpt-5-6-luna",
            vendor=Vendor.OPENAI,
            input_per_mtok=Decimal("0.20"),
            output_per_mtok=Decimal("1.20"),
            min_cacheable_tokens=1024,
        ),
        # --------------------------------------------------------------------- Google ---
        ModelPrice(
            model_id="gemini-3-1-pro",
            vendor=Vendor.GOOGLE,
            input_per_mtok=Decimal("2.00"),
            output_per_mtok=Decimal("12.00"),
            min_cacheable_tokens=4096,
            cache_storage_per_mtok_hour=Decimal("4.50"),
            # Prompts above 200k tokens bill at 2x input. Agent traffic sits just under this,
            # which makes it exactly the kind of cliff a router must not walk off.
            long_context_threshold_tokens=200_000,
            long_context_input_multiplier=Decimal("2"),
            cache_write_multiplier=_NO_WRITE_PREMIUM,
        ),
        ModelPrice(
            model_id="gemini-3-8-flash",
            vendor=Vendor.GOOGLE,
            # Vendor lists $0.75 input against $0.075 cached, confirming the 0.1x read.
            input_per_mtok=Decimal("0.75"),
            output_per_mtok=Decimal("3.75"),
            min_cacheable_tokens=4096,
            cache_storage_per_mtok_hour=Decimal("0.50"),
            cache_write_multiplier=_NO_WRITE_PREMIUM,
            # Promotional. Every figure here doubles on 1 January 2027 ($1.50 input, $7.50
            # output, $1.00/hr storage), which would silently halve every saving we quote.
            price_valid_until=date(2026, 12, 31),
        ),
        ModelPrice(
            model_id="gemini-3-1-flash-lite",
            vendor=Vendor.GOOGLE,
            input_per_mtok=Decimal("0.25"),
            output_per_mtok=Decimal("1.50"),
            min_cacheable_tokens=4096,
            # $1.00/M/hr - twice what the larger 3.8 Flash charges to hold a cache. Cheaper to
            # run, dearer to remember.
            cache_storage_per_mtok_hour=Decimal("1.00"),
            cache_write_multiplier=_NO_WRITE_PREMIUM,
        ),
    )
}


class StalePriceTableError(Exception):
    """Raised rather than publishing a dollar figure derived from prices nobody has checked."""


def verify_prices(today: date) -> None:
    """Fail loudly rather than publish a figure derived from prices we cannot vouch for.

    Two different failures, because they have different causes:

    * The table has gone **unchecked** for longer than MAX_PRICE_AGE_DAYS. Nobody has looked; a
      vendor may have moved a price under us.
    * A price has **expired**. Someone did look, and recorded that this number was promotional
      and would change on a known date. Staleness checking cannot catch this, because the table
      is not stale - it was right when written and is wrong now.

    Both are reported together rather than whichever trips first, because a table can easily be
    both stale and expired, and fixing one would only reveal the other.
    """
    problems: list[str] = []

    age = (today - PRICES_CHECKED_ON).days
    if age > MAX_PRICE_AGE_DAYS:
        problems.append(
            f"Model prices last checked {PRICES_CHECKED_ON} ({age} days ago). "
            f"Re-check each vendor's pricing page and update PRICES_CHECKED_ON."
        )

    expired = sorted(
        m.model_id
        for m in TABLE.values()
        if m.price_valid_until is not None and today > m.price_valid_until
    )
    if expired:
        problems.append(
            "Promotional pricing has ended for: "
            + ", ".join(expired)
            + ". These were recorded as temporary and are now wrong. Re-check the vendor page "
            "and update both the price and price_valid_until."
        )

    if problems:
        raise StalePriceTableError(" ".join(problems))


def price_of(model_id: str) -> ModelPrice:
    try:
        return TABLE[model_id]
    except KeyError:
        raise KeyError(
            f"No price on file for {model_id!r}. Add it to prices.TABLE with its source, "
            f"rather than guessing: every downstream figure inherits this number."
        ) from None


def models_by_vendor(vendor: Vendor) -> list[ModelPrice]:
    """Every priced model for one vendor, cheapest input first."""
    return sorted(
        (m for m in TABLE.values() if m.vendor is vendor),
        key=lambda m: m.input_per_mtok,
    )


#: Trailing words an export appends to a model name that are not part of the model.
_NAME_NOISE = re.compile(
    r"[ \-_](input|output|tokens?|cached?|standard|batch|preview|latest|v\d+)$", re.I
)
#: A dated release suffix: claude-sonnet-4-6-20260514.
_DATE_SUFFIX = re.compile(r"[-_]\d{8}$")


def normalize_model_name(raw: str) -> str:
    """Fold the many spellings of a model name into the one used in TABLE.

    Exports are not consistent: "Claude Sonnet 4.6", "claude-sonnet-4-6-20260514" and
    "Gemini 3.8 Flash Input" all name models we price. Dots become hyphens because "4.6" and
    "4-6" are the same version, dated suffixes are dropped, and trailing words like "Input" that
    describe the *line item* rather than the model are stripped.
    """
    name = raw.strip().lower().replace(".", "-").replace(" ", "-").replace("_", "-")
    name = re.sub(r"-+", "-", name).strip("-")
    name = _DATE_SUFFIX.sub("", name)
    previous = None
    while previous != name:  # "gemini-3-8-flash-input-tokens" needs more than one pass
        previous = name
        name = _NAME_NOISE.sub("", name)
    return name


def resolve_model(raw: str) -> ModelPrice | None:
    """Find the price for a model named by an export, or None if we do not recognise it.

    None is a real answer and callers must handle it. Charging a Sonnet request at Haiku's rate
    because the names looked similar would corrupt every figure that follows, so an unrecognised
    model is reported to the reader instead of being approximated.
    """
    return TABLE.get(normalize_model_name(raw))
