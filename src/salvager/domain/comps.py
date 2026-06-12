"""Reserved-comp price summary — shared math for PR #7 Layer 2.

Sellers flag a Wallapop listing *reserved* when the inventory is gone
but the post is still up. Those listings never reach the evaluator or
the buy path (you can't buy a sold listing), but their prices are a
useful *comp* signal: what someone was recently willing to pay for the
same gear. The poll cycle partitions them out (``_split_reserved``);
this module turns a batch of comp prices into a count / min / median /
max summary.

The arithmetic lives here — not in the alert renderer or the
``test-search`` CLI footer — so both surfaces format from the same
numbers and cannot drift. In particular the even-length median is the
average of the two central values, NOT the upper-middle pick (a 2-element
list's "median" must not equal its max — caught by Devin on PR #7).

Pure decimal arithmetic, zero IO.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

_TWO: Final[Decimal] = Decimal("2")


@dataclass(frozen=True)
class CompSummary:
    """Count + min / median / max over a batch of reserved comp prices.

    Always built via :func:`summarize_comps`; an empty batch yields
    ``None`` rather than a zero-count summary, so a present
    ``CompSummary`` always describes at least one comp.
    """

    count: int
    min_eur: Decimal
    median_eur: Decimal
    max_eur: Decimal


def summarize_comps(prices: Iterable[Decimal]) -> CompSummary | None:
    """Summarize reserved comp prices, or ``None`` when there are none.

    The median of an even-length set is the arithmetic mean of the two
    central values; an odd-length set takes the single central value.
    """
    ordered = sorted(prices)
    if not ordered:
        return None

    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / _TWO

    return CompSummary(
        count=len(ordered),
        min_eur=ordered[0],
        median_eur=median,
        max_eur=ordered[-1],
    )


__all__ = ["CompSummary", "summarize_comps"]
