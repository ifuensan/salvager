"""Reserved-comp summary tests — PR #7 Layer 2 shared math.

Covers the four shapes the renderer + CLI footer depend on:
empty (→ None), single comp (min=median=max), odd-length central
median, and even-length averaged median (the Devin pitfall: the
median of two values must not collapse to the max).
"""

from __future__ import annotations

from decimal import Decimal

from salvager.domain.comps import CompSummary, summarize_comps


def test_empty_yields_none() -> None:
    assert summarize_comps([]) is None
    assert summarize_comps(iter(())) is None


def test_single_comp_min_median_max_collapse() -> None:
    summary = summarize_comps([Decimal("200.00")])
    assert summary == CompSummary(
        count=1,
        min_eur=Decimal("200.00"),
        median_eur=Decimal("200.00"),
        max_eur=Decimal("200.00"),
    )


def test_odd_length_takes_central_value() -> None:
    summary = summarize_comps([Decimal("240"), Decimal("180"), Decimal("200")])
    assert summary is not None
    assert summary.count == 3
    assert summary.min_eur == Decimal("180")
    assert summary.median_eur == Decimal("200")
    assert summary.max_eur == Decimal("240")


def test_even_length_averages_two_central_values() -> None:
    # Two-element list: the median must be the average (190), NOT the
    # upper-middle pick (200) — the regression Devin caught on PR #7.
    summary = summarize_comps([Decimal("180"), Decimal("200")])
    assert summary is not None
    assert summary.median_eur == Decimal("190")


def test_even_length_half_cent_median() -> None:
    # (200 + 201) / 2 = 200.5 — a half-euro median the renderer
    # quantizes to 2dp when formatting.
    summary = summarize_comps([Decimal("201"), Decimal("200")])
    assert summary is not None
    assert summary.median_eur == Decimal("200.5")


def test_unsorted_input_is_sorted() -> None:
    summary = summarize_comps([Decimal("240"), Decimal("180"), Decimal("220"), Decimal("200")])
    assert summary is not None
    assert summary.min_eur == Decimal("180")
    assert summary.max_eur == Decimal("240")
    # Even length (4): average of the two central sorted values 200, 220.
    assert summary.median_eur == Decimal("210")
