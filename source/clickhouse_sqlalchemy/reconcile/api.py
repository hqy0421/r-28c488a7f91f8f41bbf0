"""
Public entry points for multiset reconciliation.

The query builders in :mod:`clickhouse_sqlalchemy.reconcile.query` produce
plain SQLAlchemy statements. This module adds thin execution helpers and
structured result objects so callers can either take the statements and
run them themselves, or get a complete report in one call.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy.engine import Engine
from sqlalchemy.sql.selectable import FromClause

from .columns import AlignedColumns
from .exceptions import ReconciliationError  # re-exported
from .query import (
    COL_EXTRA_COUNT,
    COL_MISSING_COUNT,
    ReconciliationQuery,
    build_reconciliation,
)

__all__ = [
    'ReconciliationResult',
    'reconcile',
    'build_reconciliation',
    'ReconciliationError',
]


@dataclass(frozen=True)
class ReconciliationResult:
    """Executed reconciliation report. All aggregates live in ClickHouse."""

    aligned: AlignedColumns
    row_differences: list
    key_summary: list
    totals: Optional[dict]

    @property
    def is_balanced(self) -> bool:
        """True when no unmatched full rows exist on either side."""
        return not self.row_differences

    @property
    def unmatched_row_count(self) -> int:
        return sum(
            row[COL_MISSING_COUNT] + row[COL_EXTRA_COUNT]
            for row in self.row_differences
        )


def _rows_to_dicts(result) -> list:
    return [dict(row._mapping) for row in result]


def reconcile(
    engine: Engine,
    old: FromClause,
    new: FromClause,
    business_keys: Sequence[str],
    attributes: Optional[Sequence[str]] = None,
    *,
    include_totals: bool = True,
    only_unmatched_rows: bool = True,
) -> ReconciliationResult:
    """
    Run a full reconciliation against ClickHouse and return the report.

    Only aggregated rows cross the wire -- the raw fact rows never leave
    the database. Column alignment/type validation happens before any SQL
    is emitted and raises :class:`ReconciliationError` subclasses.
    """
    bundle: ReconciliationQuery = build_reconciliation(
        old,
        new,
        business_keys,
        attributes,
        include_totals=include_totals,
        only_unmatched_rows=only_unmatched_rows,
    )

    with engine.connect() as conn:
        row_diffs = _rows_to_dicts(
            conn.execute(bundle.row_differences)
        )
        key_summary = _rows_to_dicts(
            conn.execute(bundle.key_summary)
        )
        totals = None
        if bundle.totals is not None:
            totals = dict(conn.execute(bundle.totals).one()._mapping)

    return ReconciliationResult(
        aligned=bundle.aligned,
        row_differences=row_diffs,
        key_summary=key_summary,
        totals=totals,
    )
