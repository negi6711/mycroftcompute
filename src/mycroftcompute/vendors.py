"""The one Vendor enum.

This lives on its own because both the parser and the price table need it, and for a while they
each had their own copy. Two StrEnums with the same members are not the same type: members from
one are never `is`-identical to members of the other, so a comparison would have been silently
False forever. mypy caught it; nothing else would have.
"""

from __future__ import annotations

from enum import StrEnum


class Vendor(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
