"""
Multiset reconciliation helpers for two versions of a fact table.

Exact, NULL-aware full-row matching executed entirely inside ClickHouse
through composable SQLAlchemy statements. See :mod:`.query` for the SQL
design and :func:`reconcile` for the one-call entry point.
"""
from .api import ReconciliationResult, reconcile
from .columns import AlignedColumns, MatchedColumn, align_columns
from .exceptions import (
    ColumnMismatchError,
    IncompatibleTypesError,
    InvalidSpecificationError,
    MissingColumnError,
    ReconciliationError,
)
from .query import (
    ReconciliationQuery,
    build_key_summary,
    build_reconciliation,
    build_row_differences,
    build_side_counts,
    build_totals,
)

__all__ = (
    # one-call API
    'reconcile',
    'ReconciliationResult',
    # composable builders
    'build_reconciliation',
    'ReconciliationQuery',
    'build_side_counts',
    'build_row_differences',
    'build_key_summary',
    'build_totals',
    'align_columns',
    'AlignedColumns',
    'MatchedColumn',
    # errors
    'ReconciliationError',
    'ColumnMismatchError',
    'MissingColumnError',
    'IncompatibleTypesError',
    'InvalidSpecificationError',
)
