"""Property-based tests for :func:`escape_markdown_v2` — Story 3.15.

The parametrized examples in ``test_alert_renderer.py`` cover the
hand-picked cases; this file feeds Hypothesis 500 random strings
sampled from the full unicode-printable + MarkdownV2-reserved set
and asserts the post-escape result never contains an *unescaped*
reserved character.

The property under test is "no injection": after
``escape_markdown_v2(s)``, every reserved character in the output
is preceded by a backslash (and backslashes themselves are
doubled). A regression here would let a listing title with a stray
``*`` or ``[`` either break the markup or open a markup-injection
path.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hardware_hunter.domain.alert import escape_markdown_v2

# Order matches the renderer module's _MD_V2_RESERVED string.
# Backslash is handled separately because it doubles itself.
_RESERVED_CHARS_NO_BACKSLASH = "_*[]()~`>#+-=|{}.!"


def _every_reserved_is_escaped(escaped: str) -> bool:
    """Walk ``escaped`` and verify every reserved char is preceded by ``\\``.

    Returns False the moment an unescaped reserved char is found, so
    Hypothesis's minimal-counterexample shrinker has a clean signal.
    """
    i = 0
    while i < len(escaped):
        ch = escaped[i]
        if ch == "\\":
            # A backslash always escapes the NEXT character — skip the pair.
            i += 2
            continue
        if ch in _RESERVED_CHARS_NO_BACKSLASH:
            return False
        i += 1
    return True


# Strategy: arbitrary unicode text with all reserved chars over-represented
# so Hypothesis spends its budget on cases that actually exercise the
# regex (not 500 plain ASCII strings).
_TEXT_STRATEGY = st.text(
    alphabet=st.one_of(
        st.sampled_from(_RESERVED_CHARS_NO_BACKSLASH + "\\"),
        st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        st.characters(min_codepoint=0xA0, max_codepoint=0xFFFF),
    ),
    max_size=80,
)


@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
@given(raw=_TEXT_STRATEGY)
def test_no_reserved_char_appears_unescaped(raw: str) -> None:
    escaped = escape_markdown_v2(raw)
    assert _every_reserved_is_escaped(escaped), (
        f"escape_markdown_v2({raw!r}) -> {escaped!r} has an unescaped reserved char"
    )


@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
@given(raw=_TEXT_STRATEGY)
def test_escape_is_idempotent_on_double_application(raw: str) -> None:
    """Applying the escape twice is well-defined: every backslash from
    pass 1 itself becomes ``\\\\`` in pass 2 — there's no input where
    the second pass loses information."""
    once = escape_markdown_v2(raw)
    twice = escape_markdown_v2(once)
    assert _every_reserved_is_escaped(twice)


@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
@given(raw=_TEXT_STRATEGY)
def test_escape_never_drops_or_adds_non_reserved_chars(raw: str) -> None:
    """The escape only INSERTS backslashes — it never alters or drops
    non-reserved characters. Stripping the backslashes recovers ``raw``."""
    escaped = escape_markdown_v2(raw)
    # Walk and rebuild: every `\X` pair contributes X; bare chars pass through.
    rebuilt_chars: list[str] = []
    i = 0
    while i < len(escaped):
        if escaped[i] == "\\":
            assert i + 1 < len(escaped), "trailing backslash in escaped output"
            rebuilt_chars.append(escaped[i + 1])
            i += 2
        else:
            rebuilt_chars.append(escaped[i])
            i += 1
    assert "".join(rebuilt_chars) == raw
