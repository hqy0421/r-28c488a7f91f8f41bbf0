Reconciling two versions of a fact table
=========================================

.. versionadded:: 0.3.3

A common task is comparing two versions of the same settlement fact table:
the old version and the new one. Order keys may repeat, row order may
differ, and some attributes are nullable. You need an *exact* multiset
reconciliation executed inside ClickHouse -- not a hash comparison, not a
cartesian pairing, and without pulling the whole table into Python.

The :mod:`clickhouse_sqlalchemy.reconcile` package provides composable
SQLAlchemy statements that do exactly that.

Semantics
---------

* Rows are matched on the **full row shape**: the declared business keys
  plus every compared attribute.
* Matching is a **multiset** operation. Every identical old row cancels
  exactly one identical new row (``least(old_count, new_count)``);
  multiplicities are honoured, so three identical old rows against two
  identical new rows leave one missing row.
* ``NULL`` and ``NULL`` are treated as equal. ClickHouse ``GROUP BY``
  puts NULLs in the same group, so no ``IS NOT DISTINCT FROM`` join is
  needed.
* ``NULL``, the empty string ``''`` and the number ``0`` stay three
  independent values and never cancel each other.
* Row order is irrelevant.

How it works
------------

Each side is first grouped over every compared column to obtain the
multiplicity of each distinct row, the two grouped results are stacked
with ``UNION ALL`` and grouped again on the same columns::

    WITH unioned_rows AS (
        SELECT 'old' AS side, <cols>, count() AS row_count
        FROM fact_old GROUP BY <cols>
        UNION ALL
        SELECT 'new' AS side, <cols>, count() AS row_count
        FROM fact_new GROUP BY <cols>
    )
    SELECT <cols>,
           ifNull(sumIf(row_count, side = 'old'), 0) AS old_count,
           ifNull(sumIf(row_count, side = 'new'), 0) AS new_count
    FROM unioned_rows
    GROUP BY <cols>

All cancellation arithmetic (``least`` / ``greatest``) then runs on these
small aggregate results. Key-level summaries and grand totals are derived
by **summing the row-shape-level figures**, never by comparing raw side
totals -- otherwise a row that merely moved between attribute values would
be wrongly reported as cancelled.

One-call usage
--------------

.. code-block:: python

    from sqlalchemy import create_engine, Column, MetaData
    from clickhouse_sqlalchemy import Table, engines, types
    from clickhouse_sqlalchemy.reconcile import reconcile

    engine = create_engine('clickhouse+native://default@localhost:9000/test')
    metadata = MetaData()

    fact_old = Table('fact_settlement_old', metadata,
        Column('order_key', types.Int64),
        Column('line_no', types.Int32),
        Column('amount', types.Nullable(types.Int64)),
        Column('note', types.Nullable(types.String)),
        engines.Memory(),
    )
    fact_new = Table('fact_settlement_new', metadata,
        Column('order_key', types.Int64),
        Column('line_no', types.Int32),
        Column('amount', types.Nullable(types.Int64)),
        Column('note', types.Nullable(types.String)),
        engines.Memory(),
    )

    report = reconcile(
        engine,
        fact_old,
        fact_new,
        business_keys=['order_key', 'line_no'],
        # attributes defaults to every non-key column; pass an explicit
        # list to compare only a subset.
        attributes=['amount', 'note'],
    )

    report.totals
    # {'old_count': 5, 'new_count': 5, 'matched_count': 3,
    #  'missing_count': 2, 'extra_count': 2}

    for row in report.row_differences:
        ...  # one row per unmatched full-row shape

    for row in report.key_summary:
        ...  # per business key: totals, matched, missing, extra

The result objects contain only aggregated rows -- the raw fact rows never
leave the database. ``report.is_balanced`` is ``True`` when no unmatched
full-row shape exists.

Output columns
--------------

Both ``row_differences`` and ``key_summary`` emit:

* ``old_count`` / ``new_count`` -- multiplicities on each side (0 when the
  shape or key exists only on the other side);
* ``matched_count`` -- rows actually cancelled pairwise;
* ``missing_count`` -- ``greatest(old_count - new_count, 0)``, old rows
  without a new counterpart;
* ``extra_count`` -- ``greatest(new_count - old_count, 0)``, new rows
  without an old counterpart.

``row_differences`` additionally carries every compared column so each
unmatched shape is fully identified. By default fully matched shapes are
hidden; pass ``only_unmatched_rows=False`` to keep them.

Composable statements
---------------------

If you prefer to run the statements yourself, or embed them in larger
queries, use the builders directly:

.. code-block:: python

    from clickhouse_sqlalchemy.reconcile import build_reconciliation
    from sqlalchemy import func, select

    bundle = build_reconciliation(
        fact_old, fact_new, business_keys=['order_key', 'line_no']
    )

    bundle.side_counts       # UNION ALL of per-side multiplicities (CTE spine)
    bundle.row_differences   # full-row multiset differences
    bundle.key_summary       # per-business-key rollup
    bundle.totals            # single-row grand total

    # every attribute is a plain Select, so you can filter or wrap it:
    embedded = bundle.row_differences.subquery('diff')
    query = select(func.count()).select_from(embedded).where(
        embedded.c.extra_count > 0
    )

The individual builders (``build_side_counts``, ``build_row_differences``,
``build_key_summary``, ``build_totals``) can also be used independently.

Sources can be tables, reflected tables or any selectable; columns are
aligned **by output name**:

.. code-block:: python

    old_sel = select(fact_old.c.order_key, fact_old.c.amount).alias('o')
    new_sel = select(fact_new.c.order_key, fact_new.c.amount).alias('n')
    reconcile(engine, old_sel, new_sel, business_keys=['order_key'])

Validation
----------

Before any SQL is emitted, the specification is validated and a
:class:`~clickhouse_sqlalchemy.reconcile.ReconciliationError` subclass is
raised:

* ``MissingColumnError`` -- a declared column does not exist on one side;
* ``IncompatibleTypesError`` -- same-named columns have incompatible types;
* ``InvalidSpecificationError`` -- no business key, duplicated names, key
  also listed as an attribute, etc.

Type compatibility rules:

* ``Nullable`` and ``LowCardinality`` wrappers are peeled first, so a
  ``Nullable(Int64)`` column matches a plain ``Int64`` column;
* all ClickHouse integer widths (``Int8`` ... ``UInt256``) are mutually
  compatible, matching ClickHouse's own integer promotion rules;
* every other type family (``String``, ``Float``, ``Decimal``, ``Date``,
  ``DateTime``, ``Bool``, ...) must match exactly, including the item
  types of ``Array`` / ``Tuple`` / ``Map``.

This mirrors what ClickHouse itself accepts in a ``UNION ALL`` /
``GROUP BY`` over the two columns, so an incompatible pair fails fast in
the application instead of surfacing as a database error mid-query.
