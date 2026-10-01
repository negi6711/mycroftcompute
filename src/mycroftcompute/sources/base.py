"""Where usage comes from.

Everything that can produce usage - a CSV export, a local transcript, a metrics backend - becomes
a `Source` and yields `UsageRecord`. Nothing downstream knows or cares which: `findings.py` and
`report/text.py` only ever see records, which is why adding a connector changes nothing about the
analysis.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from mycroftcompute.parsers.model import ParsedUsage


class Source(Protocol):
    """Anything that can produce usage for a date range."""

    def fetch(self, start: date, end: date) -> ParsedUsage:
        """Records covering `start` to `end` inclusive.

        Implementations report what they could not do through `ParsedUsage.issues` rather than
        raising, unless nothing at all could be read.
        """
        ...


class SourceError(Exception):
    """A source could not be read. Carries the reason a person would need to fix it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
