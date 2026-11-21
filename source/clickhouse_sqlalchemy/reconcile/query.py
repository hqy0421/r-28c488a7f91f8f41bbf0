"""
Composable SQL builders for exact multiset reconciliation.

Design (fully executed inside ClickHouse, no hashing, no cartesian join,
no data pulled into Python):

1. Each side is grouped over every compared column to obtain the
   *multiset multiplicity* of every distinct row.
2. The two grouped sides are stacked with ``UNION ALL`` and grouped again
   on the same columns. ClickHouse ``GROUP BY`` puts two NULLs in the same
   group, while NULL / '' / 0 remain distinct values, so NULL-aware
   matching comes for free without ``IS NOT DISTINCT FROM`` joins.
3. Per distinct *row shape* the old/new multiplicities ``o``/``n`` are
   computed. The cancelled count is ``least(o, n)``; the unmatched
   remainder is ``greatest(o - n, 0)`` (missing) and
   ``greatest(n - o, 0)`` (extra). Every matching pair cancels exactly
   one row on each side.
4. The key-level summary and grand totals are derived by **summing the
   row-shape-level figures** (never ``least`` of raw side totals, which
   would overstate cancellation when rows move between attribute values).

Every builder returns an ordinary SQLAlchemy ``Select`` so callers can add
their own WHERE/ORDER BY/LIMIT, wrap the result in another CTE, or join it
to dimension tables before execution.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import literal
from sqlalchemy.sql import func, select
from sqlalchemy.sql.selectable import FromClause, Select

from .columns import (
    AlignedColumns,
    MatchedColumn,
    NEW_SIDE,
    OLD_SIDE,
    align_columns,
)

# Standard output column names.
COL_SIDE_OLD_COUNT = 'old_count'
COL_SIDE_NEW_COUNT = 'new_count'
COL_MATCHED_COUNT = 'matched_count'
COL_MISSING_COUNT = 'missing_count'
COL_EXTRA_COUNT = 'extra_count'
COL_SIDE = 'side'
COL_ROW_COUNT = 'row_count'


@dataclass(frozen=True)
class ReconciliationQuery:
    """
    The composable statements of one reconciliation.

    Each attribute is a standalone executable ``Select`` built from the
    same CTE spine, so they can be executed independently or embedded
    into larger queries.
    """

    aligned: AlignedColumns
    side_counts: Select
    row_differences: Select
    key_summary: Select
    totals: Optional[Select]


# ---------------------------------------------------------------------------
# Layer 1: per-side multiplicities stacked with UNION ALL
# ---------------------------------------------------------------------------

def _grouped_side(
    source: FromClause,
    side_label: str,
    columns: Sequence[MatchedColumn],
    side_attr: str,
) -> Select:
    """
    ``SELECT '<side>' AS side, <compared cols>, count() AS row_count
        FROM <source> GROUP BY <compared cols>``

    ``side_attr`` picks which aligned element ('old'/'new') of each
    :class:`MatchedColumn` belongs to this source.
    """
    elements = [getattr(c, side_attr) for c in columns]
    return select(
        literal(side_label).label(COL_SIDE),
        *elements,
        func.count().label(COL_ROW_COUNT),
    ).select_from(source).group_by(*elements)


def build_side_counts(aligned: AlignedColumns) -> Select:
    """
    Stack the per-side multiplicities.

    Returns one row per distinct (side, compared-columns...) combination
    with its raw multiplicity. This is the shared spine every other
    statement is built from.
    """
    old_grouped = _grouped_side(
        aligned.old_source, OLD_SIDE, aligned.all_columns, 'old'
    )
    new_grouped = _grouped_side(
        aligned.new_source, NEW_SIDE, aligned.all_columns, 'new'
    )
    return old_grouped.union_all(new_grouped)


# ---------------------------------------------------------------------------
# Layer 2: per-row-shape old/new multiplicities
# ---------------------------------------------------------------------------

def _side_count(union_cte, side: str):
    """``ifNull(sumIf(row_count, side = '<side>'), 0)``."""
    return func.ifNull(
        func.sumIf(
            union_cte.columns[COL_ROW_COUNT],
            union_cte.columns[COL_SIDE] == side,
        ),
        0,
    )


def build_row_counts(
    aligned: AlignedColumns,
    side_counts: Optional[Select] = None,
) -> Select:
    """
    One row per distinct full-row shape with old/new multiplicities.

    This is the authoritative multiset-diff layer: ``matched_count`` on
    every higher-level report is the sum of ``least(old, new)`` from here.
    """
    if side_counts is None:
        side_counts = build_side_counts(aligned)

    union_cte = side_counts.cte('unioned_rows')
    grouped = [union_cte.columns[c.name] for c in aligned.all_columns]

    return select(
        *grouped,
        _side_count(union_cte, OLD_SIDE).label(COL_SIDE_OLD_COUNT),
        _side_count(union_cte, NEW_SIDE).label(COL_SIDE_NEW_COUNT),
    ).group_by(*grouped)


def _diff_columns(row_counts):
    """The five derived measure columns shared by every report level."""
    o = row_counts.columns[COL_SIDE_OLD_COUNT]
    n = row_counts.columns[COL_SIDE_NEW_COUNT]
    return (
        o.label(COL_SIDE_OLD_COUNT),
        n.label(COL_SIDE_NEW_COUNT),
        func.least(o, n).label(COL_MATCHED_COUNT),
        func.greatest(o - n, 0).label(COL_MISSING_COUNT),
        func.greatest(n - o, 0).label(COL_EXTRA_COUNT),
    )


# ---------------------------------------------------------------------------
# Layer 3a: full-row difference report
# ---------------------------------------------------------------------------

def build_row_differences(
    aligned: AlignedColumns,
    side_counts: Optional[Select] = None,
    *,
    only_unmatched: bool = True,
) -> Select:
    """
    Full-row multiset comparison.

    Returns one row per distinct compared-column combination with:

    * ``old_count`` / ``new_count`` -- multiplicities on each side
      (0 when the row shape exists only on the other side)
    * ``matched_count`` -- rows cancelled pairwise (``least``)
    * ``missing_count`` -- old rows left without a new counterpart
    * ``extra_count`` -- new rows left without an old counterpart

    When ``only_unmatched`` is ``True`` (default) fully matched row shapes
    are removed, which is what a reconciliation report wants. Pass
    ``False`` to keep every distinct row shape including matched ones.
    """
    row_counts = build_row_counts(aligned, side_counts).subquery('row_counts')
    shape_cols = [row_counts.columns[c.name] for c in aligned.all_columns]

    stmt = select(*shape_cols, *_diff_columns(row_counts))

    if only_unmatched:
        stmt = stmt.where(
            row_counts.columns[COL_SIDE_OLD_COUNT]
            != row_counts.columns[COL_SIDE_NEW_COUNT]
        )

    return stmt.order_by(*shape_cols)


# ---------------------------------------------------------------------------
# Layer 3b: per-business-key rollup (sums of the row-shape figures)
# ---------------------------------------------------------------------------

def build_key_summary(
    aligned: AlignedColumns,
    side_counts: Optional[Select] = None,
) -> Select:
    """
    Per-business-key rollup.

    For every business-key value present on either side:

    * ``old_count`` / ``new_count`` -- total rows on each side
    * ``matched_count`` -- full rows actually cancelled pairwise, summed
      over the row shapes of this key
    * ``missing_count`` / ``extra_count`` -- per-key net differences

    A key can be balanced in totals yet still contain mismatched full
    rows (rows moved between attribute values); ``matched_count`` only
    counts genuinely identical row pairs, so such a key shows both
    missing and extra rows. The row-difference statement lists them.
    """
    row_counts = build_row_counts(aligned, side_counts).subquery('row_counts')
    key_cols = [
        row_counts.columns[c.name] for c in aligned.business_keys
    ]
    o = row_counts.columns[COL_SIDE_OLD_COUNT]
    n = row_counts.columns[COL_SIDE_NEW_COUNT]

    stmt = select(
        *key_cols,
        func.sum(o).label(COL_SIDE_OLD_COUNT),
        func.sum(n).label(COL_SIDE_NEW_COUNT),
        func.sum(func.least(o, n)).label(COL_MATCHED_COUNT),
        func.sum(func.greatest(o - n, 0)).label(COL_MISSING_COUNT),
        func.sum(func.greatest(n - o, 0)).label(COL_EXTRA_COUNT),
    ).group_by(*key_cols)

    return stmt.order_by(*key_cols)


# ---------------------------------------------------------------------------
# Layer 3c: grand totals
# ---------------------------------------------------------------------------

def build_totals(
    aligned: AlignedColumns,
    side_counts: Optional[Select] = None,
) -> Select:
    """
    Single-row grand total over the whole fact table (no key columns).

    Derived from the row-shape layer, so ``matched_count`` is the total
    number of genuinely cancelled pairs; the per-key summary sums up to
    exactly these figures.
    """
    row_counts = build_row_counts(aligned, side_counts).subquery('row_counts')
    o = row_counts.columns[COL_SIDE_OLD_COUNT]
    n = row_counts.columns[COL_SIDE_NEW_COUNT]

    return select(
        func.sum(o).label(COL_SIDE_OLD_COUNT),
        func.sum(n).label(COL_SIDE_NEW_COUNT),
        func.sum(func.least(o, n)).label(COL_MATCHED_COUNT),
        func.sum(func.greatest(o - n, 0)).label(COL_MISSING_COUNT),
        func.sum(func.greatest(n - o, 0)).label(COL_EXTRA_COUNT),
    )


# ---------------------------------------------------------------------------
# One-call facade
# ---------------------------------------------------------------------------

def build_reconciliation(
    old: FromClause,
    new: FromClause,
    business_keys: Sequence[str],
    attributes: Optional[Sequence[str]] = None,
    *,
    include_totals: bool = True,
    only_unmatched_rows: bool = True,
) -> ReconciliationQuery:
    """
    Validate the inputs and produce the complete, composable query bundle.
    """
    aligned = align_columns(old, new, business_keys, attributes)
    side_counts = build_side_counts(aligned)
    return ReconciliationQuery(
        aligned=aligned,
        side_counts=side_counts,
        row_differences=build_row_differences(
            aligned, side_counts, only_unmatched=only_unmatched_rows
        ),
        key_summary=build_key_summary(aligned, side_counts),
        totals=build_totals(aligned, side_counts) if include_totals else None,
    )
