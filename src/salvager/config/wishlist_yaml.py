"""Wishlist YAML loader + saver with comment-preserving round-trip — AR12.

Why this lives in ``config/`` not ``domain/``
---------------------------------------------
The domain wishlist schema (``domain/wishlist.py``) is pure pydantic — no
filesystem, no YAML library, no formatting concerns. This module is the
boundary that converts wishlist.yaml bytes into a typed ``Wishlist`` and
back, preserving the operator's comments + quoting + indentation across
``salvager phase2 enable/disable`` rewrites.

Validation order (locked)
-------------------------
1. ruamel.yaml parses the bytes (parse error → :class:`WishlistParseError`)
2. :func:`check_scope_violations` runs (forbidden-arbitrage → :class:`WishlistScopeError`)
3. pydantic validates the typed shape (field error → :class:`WishlistValidationError`)

Step 2 runs *before* step 3 so the error anchors to the (c3) scope
contract — pointing the operator at ROADMAP.md — instead of looking
like a generic ``extra_forbidden`` typo.

Round-trip strategy
-------------------
On load we keep the underlying ruamel ``CommentedMap`` attached to the
returned ``Wishlist`` (via a pydantic ``PrivateAttr``). On save we walk
the typed model and the YAML doc in lockstep, updating only the YAML
cells whose typed value diverges. Untouched cells keep their original
quoting, ordering, and surrounding comments. Saving a freshly
constructed (un-loaded) ``Wishlist`` falls back to a full serialize.
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from salvager.domain.scope_guard import (
    ScopeViolation,
    check_scope_violations,
)
from salvager.domain.wishlist import Wishlist

# ─────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────


class WishlistError(Exception):
    """Base class for any wishlist-loader failure."""


class WishlistParseError(WishlistError):
    """The YAML parser rejected the file (malformed syntax)."""

    def __init__(self, path: Path, line: int, column: int, message: str) -> None:
        self.path = path
        self.line = line
        self.column = column
        super().__init__(f"{path}:{line}:{column}: {message}")


class WishlistScopeError(WishlistError):
    """The wishlist contains forbidden-arbitrage fields (FR3 / (c3)).

    Carries the parsed ruamel doc so the CLI's error renderer can
    resolve the nearest entry name for the locked error template.
    """

    def __init__(
        self,
        path: Path,
        violations: list[ScopeViolation],
        doc: CommentedMap | None = None,
    ) -> None:
        self.path = path
        self.violations = violations
        self.doc = doc
        first = violations[0]
        loc = f"line {first.line_number}" if first.line_number else first.path
        super().__init__(
            f"{path}:{loc}: forbidden field '{first.field_name}' ({len(violations) - 1} more)"
            if len(violations) > 1
            else f"{path}:{loc}: forbidden field '{first.field_name}'"
        )


class WishlistValidationError(WishlistError):
    """pydantic rejected a field. Wraps the underlying ValidationError
    with the source path and (when resolvable) a line number per error.

    Carries the parsed doc so cross-entry validators (e.g. duplicate-key
    detection) can be re-resolved to line numbers for the CLI's renderer.
    """

    def __init__(
        self,
        path: Path,
        errors: list[dict[str, Any]],
        underlying: ValidationError,
        doc: CommentedMap | None = None,
    ) -> None:
        self.path = path
        self.errors = errors
        self.underlying = underlying
        self.doc = doc
        first = errors[0]
        loc = first.get("loc_str", first.get("loc"))
        line = first.get("line_number")
        line_part = f":{line}" if line else ""
        super().__init__(f"{path}{line_part}: {loc}: {first['msg']}")


# ─────────────────────────────────────────────────────────────────────────
# Wishlist subclass that carries the preserved ruamel doc
# ─────────────────────────────────────────────────────────────────────────


# The doc lives on the model via PrivateAttr so consumers see the same
# `Wishlist` type they imported from `domain/wishlist.py`. Code that
# constructs a Wishlist by hand simply leaves `_yaml_doc` as None and
# save_wishlist falls back to a full serialize.
def _attach_doc(wishlist: Wishlist, doc: CommentedMap | None) -> None:
    """Side-channel-attach the ruamel doc; works around Wishlist not
    declaring the PrivateAttr itself (it lives in pure-domain land)."""
    object.__setattr__(wishlist, "__yaml_doc__", doc)


def _get_doc(wishlist: Wishlist) -> CommentedMap | None:
    return getattr(wishlist, "__yaml_doc__", None)


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────


def _represent_none_as_null(representer: Any, _data: None) -> Any:
    """ruamel's default emits None as an empty scalar; the example wishlist
    uses ``null`` explicitly for unset prices, and a round-trip that
    changes ``null`` → empty would fail the AR12 byte-identical test."""
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


def _build_yaml() -> YAML:
    """One YAML() instance per call — ruamel YAMLs aren't reentrant-safe
    across threads, and the cost of construction is negligible relative
    to the IO it's about to do."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.add_representer(type(None), _represent_none_as_null)
    return yaml


def load_wishlist(path: str | Path) -> Wishlist:
    """Parse, scope-guard, validate, and return a typed :class:`Wishlist`.

    The returned model carries the underlying ruamel doc so subsequent
    :func:`save_wishlist` calls preserve comments + formatting.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    try:
        doc = _build_yaml().load(text)
    except YAMLError as exc:
        line, column = _extract_parse_position(exc)
        raise WishlistParseError(path, line, column, str(exc)) from exc

    if doc is None:
        doc = CommentedMap()

    violations = check_scope_violations(doc)
    if violations:
        raise WishlistScopeError(path, violations, doc=doc)

    try:
        wishlist = Wishlist.model_validate(_to_python(doc))
    except ValidationError as exc:
        errors = _enrich_validation_errors(exc, doc)
        raise WishlistValidationError(path, errors, exc, doc=doc) from exc

    _attach_doc(wishlist, doc)
    return wishlist


def save_wishlist(path: str | Path, wishlist: Wishlist) -> None:
    """Persist ``wishlist`` to ``path``.

    When the wishlist was loaded by :func:`load_wishlist`, only fields
    whose typed value diverges from the YAML cell are written through —
    comments, quoting, and ordering are preserved. When the wishlist was
    constructed in Python, we serialize from scratch.
    """
    path = Path(path)
    doc = _get_doc(wishlist)

    if doc is None:
        doc = CommentedMap()
        _sync_model_into_yaml(wishlist, doc)
    else:
        _sync_model_into_yaml(wishlist, doc)

    buf = StringIO()
    _build_yaml().dump(doc, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────


def _to_python(node: object) -> object:
    """Convert a ruamel parsed tree into plain Python containers for
    pydantic validation. We don't want ruamel's lazy-evaluated scalar
    wrappers reaching the typed model — pydantic gets confused by
    ScalarFloat / ScalarString in some validators."""
    if isinstance(node, CommentedMap | dict):
        return {str(key): _to_python(value) for key, value in node.items()}
    if isinstance(node, CommentedSeq | list):
        return [_to_python(item) for item in node]
    return node


def _extract_parse_position(exc: YAMLError) -> tuple[int, int]:
    """Pull (1-based) line + column from ruamel's YAMLError, with fallbacks."""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is None:
        return (0, 0)
    return (int(mark.line) + 1, int(mark.column) + 1)


def _enrich_validation_errors(exc: ValidationError, doc: CommentedMap) -> list[dict[str, Any]]:
    """Add a resolved line number + dotted loc string to each pydantic error."""
    enriched: list[dict[str, Any]] = []
    for raw in exc.errors():
        loc = raw["loc"]
        enriched.append(
            {
                **raw,
                "loc_str": _format_loc(loc),
                "line_number": _resolve_line_number(doc, loc),
            }
        )
    return enriched


def _format_loc(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for step in loc:
        if isinstance(step, int):
            parts.append(f"[{step}]")
        else:
            parts.append(f".{step}" if parts else str(step))
    return "".join(parts)


def _resolve_line_number(doc: object, loc: tuple[Any, ...]) -> int | None:
    """Walk a (str | int)-step loc into the ruamel doc, returning the
    1-based line number of the final key when available."""
    node: Any = doc
    parent: Any = None
    last_step: Any = None
    for step in loc:
        parent = node
        last_step = step
        if isinstance(step, int):
            if not isinstance(node, list | CommentedSeq) or step >= len(node):
                return None
            node = node[step]
        else:
            if not isinstance(node, dict | CommentedMap) or step not in node:
                return None
            node = node[step]
    if last_step is None:
        return None
    lc = getattr(parent, "lc", None)
    if lc is None:
        return None
    try:
        position = lc.item(last_step) if isinstance(last_step, int) else lc.key(last_step)
    except (KeyError, AttributeError, TypeError):
        return None
    if not position:
        return None
    return int(position[0]) + 1


def _sync_model_into_yaml(model: BaseModel, yaml_node: Any) -> None:
    """Walk a pydantic model and update the parallel ruamel node only for
    fields whose typed value diverges. Untouched cells keep their
    original quoting, comments, and ordering."""
    for field_name, field_info in model.__class__.model_fields.items():
        typed_value = getattr(model, field_name)
        if field_name not in yaml_node:
            # An absent cell whose typed value still equals the model default
            # stays absent — otherwise adding an optional field to the schema
            # (e.g. `offer:`) would inject noise into every saved wishlist
            # and break the byte-identical round-trip guarantee.
            default = field_info.get_default(call_default_factory=True)
            if _values_equal(typed_value, default):
                continue
            yaml_node[field_name] = _to_yaml(typed_value)
            continue
        yaml_value = yaml_node[field_name]

        if isinstance(typed_value, BaseModel) and isinstance(yaml_value, CommentedMap | dict):
            _sync_model_into_yaml(typed_value, yaml_value)
            continue

        if isinstance(typed_value, list) and isinstance(yaml_value, CommentedSeq | list):
            for index, item in enumerate(typed_value):
                if index < len(yaml_value) and isinstance(item, BaseModel):
                    _sync_model_into_yaml(item, yaml_value[index])
                elif index < len(yaml_value):
                    if not _values_equal(item, yaml_value[index]):
                        yaml_value[index] = _to_yaml(item)
                else:
                    yaml_value.append(_to_yaml(item))
            continue

        if not _values_equal(typed_value, yaml_value):
            yaml_node[field_name] = _to_yaml(typed_value)


def _values_equal(typed_value: Any, yaml_value: Any) -> bool:
    """Decimal / float / int compare numerically in Python; None compares
    by identity. Strings + bools fall through to default ``==``."""
    if typed_value is None or yaml_value is None:
        return typed_value is None and yaml_value is None
    return bool(typed_value == yaml_value)


def _to_yaml(value: Any) -> Any:
    """Coerce a typed value into a form ruamel will round-trip cleanly."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, BaseModel):
        out = CommentedMap()
        for field_name in value.__class__.model_fields:
            out[field_name] = _to_yaml(getattr(value, field_name))
        return out
    if isinstance(value, list):
        out_seq = CommentedSeq()
        for item in value:
            out_seq.append(_to_yaml(item))
        return out_seq
    return value
