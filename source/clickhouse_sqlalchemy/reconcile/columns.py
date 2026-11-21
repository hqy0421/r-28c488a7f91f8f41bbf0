"""
Column alignment and type-compatibility validation.

The two sides of a reconciliation are aligned **by column name**: every
column that participates in the comparison must exist on both sides with
compatible ClickHouse types. Nullability wrappers (``Nullable`` /
``LowCardinality``) are peeled before the check, because the reconciliation
comparison itself is NULL-aware.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import inspect
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import FromClause
from sqlalchemy.sql.type_api import to_instance

from .exceptions import (
    IncompatibleTypesError,
    InvalidSpecificationError,
    MissingColumnError,
)

# Side labels used inside the generated SQL ("side" discriminator column).
OLD_SIDE = 'old'
NEW_SIDE = 'new'

_INTEGER_FAMILY = {
    'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32',
    'int64', 'uint64', 'int128', 'uint128', 'int256', 'uint256',
}


@dataclass(frozen=True)
class MatchedColumn:
    """One compared column, aligned by name across the two sides."""

    name: str
    old: ColumnElement
    new: ColumnElement
    is_business_key: bool

    @property
    def pair(self):
        """The aligned column elements as ``(old, new)``."""
        return self.old, self.new


@dataclass(frozen=True)
class AlignedColumns:
    """Result of validating and aligning the two inputs."""

    old_source: FromClause
    new_source: FromClause
    business_keys: tuple
    attributes: tuple
    all_columns: tuple

    @property
    def compared_names(self):
        return tuple(c.name for c in self.all_columns)

    @property
    def key_names(self):
        return tuple(c.name for c in self.business_keys)


def _column_map(source: FromClause) -> dict:
    if hasattr(source, 'columns'):
        cols = list(source.columns)
    else:  # pragma: no cover - FromClause always exposes .columns
        try:
            cols = list(inspect(source).columns)
        except Exception as exc:  # defensive
            raise InvalidSpecificationError(
                f"Cannot read columns from source {source!r}: {exc}"
            ) from exc

    mapped = {}
    for col in cols:
        if col.name in mapped:
            raise InvalidSpecificationError(
                f"Source {source!r} exports a duplicated column name "
                f"{col.name!r}; reconciliation requires unique output names."
            )
        mapped[col.name] = col
    return mapped


def _strip_modifiers(type_):
    """
    Peel Nullable / LowCardinality wrappers, recursively, returning the
    underlying value type. Array/Tuple/Map item types are kept for
    recursive family comparison.
    """
    visit = getattr(type_, '__visit_name__', None)
    if visit == 'nullable':
        return _strip_modifiers(type_.nested_type)
    if visit == 'lowcardinality':
        return _strip_modifiers(type_.nested_type)
    return type_


def _type_family(type_):
    """
    A coarse, conservative family label.

    - every ClickHouse integer width shares the ``'integer'`` family
      (ClickHouse promotes integer widths in UNION/GROUP BY)
    - every other base type is its own visit name family, so e.g.
      ``String`` vs ``Date`` or ``Float64`` vs ``Decimal`` are rejected
    - composite types (Array/Tuple/Map/Nested) must share structure and
      have compatible item types
    """
    type_ = _strip_modifiers(type_)
    visit = type_.__visit_name__

    if visit in _INTEGER_FAMILY:
        return 'integer'

    if visit == 'array':
        return ('array', _type_family(type_.item_type_impl))

    if visit == 'tuple':
        return (
            'tuple',
            tuple(_type_family(to_instance(t)) for t in type_.nested_types),
        )

    if visit == 'map':
        return (
            'map',
            _type_family(to_instance(type_.key_type)),
            _type_family(to_instance(type_.value_type)),
        )

    return visit


def _render_type(type_) -> str:
    try:
        return type_.compile()
    except Exception:  # pragma: no cover - defensive
        return repr(type_)


def _check_compatible(name: str, old_type, new_type) -> None:
    old_family = _type_family(old_type)
    new_family = _type_family(new_type)
    if old_family == new_family:
        return

    raise IncompatibleTypesError(
        f"Column {name!r} has incompatible types across sides: "
        f"old={_render_type(old_type)}, new={_render_type(new_type)}. "
        "Integer widths are mutually compatible; every other type family "
        "(String, Float, Decimal, Date, DateTime, ...) must match exactly, "
        "including Array/Tuple/Map item types."
    )


def align_columns(
    old: FromClause,
    new: FromClause,
    business_keys: Sequence[str],
    attributes: Optional[Sequence[str]] = None,
) -> AlignedColumns:
    """
    Validate the specification and align columns by name.

    ``business_keys`` are always included. When ``attributes`` is ``None``
    every non-key column found on the old side is compared (and must exist
    on the new side too). Explicitly named attribute columns must exist on
    both sides.
    """
    if not business_keys:
        raise InvalidSpecificationError(
            "At least one business key column must be declared."
        )

    old = _normalize_source(old)
    new = _normalize_source(new)

    old_cols = _column_map(old)
    new_cols = _column_map(new)

    if len(set(business_keys)) != len(business_keys):
        raise InvalidSpecificationError(
            f"Duplicate business key names: {list(business_keys)}"
        )

    if attributes is None:
        attr_names = [n for n in old_cols if n not in business_keys]
    else:
        if len(set(attributes)) != len(attributes):
            raise InvalidSpecificationError(
                f"Duplicate attribute names: {list(attributes)}"
            )
        attr_names = list(attributes)

    overlap = set(attr_names).intersection(business_keys)
    if overlap:
        raise InvalidSpecificationError(
            f"Columns declared both as business key and attribute: "
            f"{sorted(overlap)}"
        )

    def _resolve(name: str, is_key: bool) -> MatchedColumn:
        if name not in old_cols and name not in new_cols:
            where = "both sides"
        elif name not in old_cols:
            where = f"the {OLD_SIDE} side"
        else:
            where = f"the {NEW_SIDE} side"
        if name not in old_cols or name not in new_cols:
            role = "business key" if is_key else "attribute"
            raise MissingColumnError(
                f"{role.capitalize()} column {name!r} is missing on "
                f"{where}. Available on old: {sorted(old_cols)}; "
                f"on new: {sorted(new_cols)}."
            )

        old_col = old_cols[name]
        new_col = new_cols[name]
        _check_compatible(name, old_col.type, new_col.type)
        return MatchedColumn(
            name=name,
            old=old_col,
            new=new_col,
            is_business_key=is_key,
        )

    keys = tuple(_resolve(n, True) for n in business_keys)
    attrs = tuple(_resolve(n, False) for n in attr_names)
    return AlignedColumns(
        old_source=old,
        new_source=new,
        business_keys=keys,
        attributes=attrs,
        all_columns=keys + attrs,
    )


def _normalize_source(source) -> FromClause:
    if isinstance(source, FromClause):
        return source
    raise InvalidSpecificationError(
        f"Expected a Table or selectable, got {type(source).__name__}: "
        f"{source!r}"
    )
