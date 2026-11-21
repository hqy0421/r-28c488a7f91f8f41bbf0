"""
Database-backed tests for the multiset reconciliation semantics.

The same scenario suite runs through both the native and HTTP drivers via
a shared mixin. Everything is computed inside ClickHouse; assertions only
read the small aggregated result sets.
"""
from sqlalchemy import Column, MetaData, select
from sqlalchemy.sql import functions as sa_func

from clickhouse_sqlalchemy import Table, engines, types
from clickhouse_sqlalchemy.reconcile import (
    build_reconciliation,
    reconcile,
)


# Shared fact-table shape: (order_key, amount, note)
def _build_pair(session, name, old_type=types.Nullable(types.Int64()),
                new_type=None):
    new_type = new_type or old_type
    md = MetaData()
    old = Table(
        f'{name}_old', md,
        Column('order_key', types.Int64),
        Column('amount', old_type),
        Column('note', types.Nullable(types.String())),
        engines.Memory(),
    )
    new = Table(
        f'{name}_new', md,
        Column('order_key', types.Int64),
        Column('amount', new_type),
        Column('note', types.Nullable(types.String())),
        engines.Memory(),
    )
    old.drop(bind=session.bind, if_exists=True)
    new.drop(bind=session.bind, if_exists=True)
    old.create(bind=session.bind)
    new.create(bind=session.bind)
    return old, new


def _insert(session, table, rows):
    session.execute(
        table.insert(),
        [dict(zip(('order_key', 'amount', 'note'), r)) for r in rows],
    )


class ReconcileScenarioMixin:
    """Driver-agnostic scenarios; concrete classes supply the session."""

    def test_null_matches_null_but_null_empty_zero_are_distinct(self):
        old, new = _build_pair(self.session, 'null_semantics')
        # same key, four distinct value shapes incl. NULL / '' / 0
        _insert(self.session, old, [
            (1, None, None),
            (1, 0, ''),
            (1, None, ''),
            (1, 0, None),
        ])
        _insert(self.session, new, [
            (1, None, None),   # matches the NULL/NULL row
            (1, 0, ''),        # matches the 0/'' row
            (1, None, ''),     # matches the NULL/'' row
            (1, 0, None),      # matches the 0/NULL row
        ])

        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        self.assertTrue(report.is_balanced)
        self.assertEqual(report.row_differences, [])
        self.assertEqual(
            report.totals,
            {'old_count': 4, 'new_count': 4, 'matched_count': 4,
             'missing_count': 0, 'extra_count': 0},
        )

    def test_duplicate_identical_rows_cancel_one_to_one(self):
        old, new = _build_pair(self.session, 'duplicates')
        _insert(self.session, old, [
            (1, 10, 'a'), (1, 10, 'a'), (1, 10, 'a'),
        ])
        _insert(self.session, new, [
            (1, 10, 'a'), (1, 10, 'a'),
        ])

        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        diff = report.row_differences
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]['old_count'], 3)
        self.assertEqual(diff[0]['new_count'], 2)
        self.assertEqual(diff[0]['matched_count'], 2)
        self.assertEqual(diff[0]['missing_count'], 1)
        self.assertEqual(diff[0]['extra_count'], 0)

    def test_missing_and_extra_full_rows(self):
        old, new = _build_pair(self.session, 'missing_extra')
        _insert(self.session, old, [
            (1, 10, 'a'),
            (2, 5, None),       # only on old
        ])
        _insert(self.session, new, [
            (1, 10, 'a'),
            (3, 7, 'z'),        # only on new
        ])

        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        shapes = {
            (r['order_key'], r['amount'], r['note']): r
            for r in report.row_differences
        }
        self.assertEqual(
            (shapes[(2, 5, None)]['missing_count'],
             shapes[(2, 5, None)]['old_count'],
             shapes[(2, 5, None)]['new_count']),
            (1, 1, 0),
        )
        self.assertEqual(
            (shapes[(3, 7, 'z')]['extra_count'],
             shapes[(3, 7, 'z')]['old_count'],
             shapes[(3, 7, 'z')]['new_count']),
            (1, 0, 1),
        )
        self.assertEqual(
            report.totals,
            {'old_count': 2, 'new_count': 2, 'matched_count': 1,
             'missing_count': 1, 'extra_count': 1},
        )

    def test_row_moving_between_attributes_does_not_over_match(self):
        # Regression: equal side totals but no identical full row means
        # matched_count must stay zero at every rollup level.
        old, new = _build_pair(self.session, 'row_move')
        _insert(self.session, old, [(1, 10, 'a')])
        _insert(self.session, new, [(1, 10, 'b')])

        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        key = report.key_summary[0]
        self.assertEqual(key['old_count'], 1)
        self.assertEqual(key['new_count'], 1)
        self.assertEqual(key['matched_count'], 0)
        self.assertEqual(key['missing_count'], 1)
        self.assertEqual(key['extra_count'], 1)
        self.assertEqual(report.totals['matched_count'], 0)
        self.assertEqual(report.unmatched_row_count, 2)

    def test_key_summary_rolls_up_to_totals(self):
        old, new = _build_pair(self.session, 'rollup')
        _insert(self.session, old, [
            (1, 10, 'a'), (1, 10, 'a'),
            (2, 5, None),
        ])
        _insert(self.session, new, [
            (1, 10, 'a'),
            (2, 5, None), (2, 9, 'x'),
            (3, 7, 'z'),
        ])

        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        rolled = {m: 0 for m in (
            'old_count', 'new_count', 'matched_count',
            'missing_count', 'extra_count')}
        for row in report.key_summary:
            for measure in rolled:
                rolled[measure] += row[measure]
        self.assertEqual(rolled, report.totals)

        keys = {r['order_key']: r for r in report.key_summary}
        self.assertEqual(
            (keys[1]['matched_count'], keys[1]['missing_count']), (1, 1)
        )
        self.assertEqual(keys[3]['extra_count'], 1)

    def test_composite_business_key(self):
        md = MetaData()
        old = Table(
            'composite_old', md,
            Column('order_key', types.Int64),
            Column('line_no', types.Int32),
            Column('amount', types.Nullable(types.Int64())),
            engines.Memory(),
        )
        new = Table(
            'composite_new', md,
            Column('order_key', types.Int64),
            Column('line_no', types.Int32),
            Column('amount', types.Nullable(types.Int64())),
            engines.Memory(),
        )
        for t in (old, new):
            t.drop(bind=self.session.bind, if_exists=True)
            t.create(bind=self.session.bind)

        self.session.execute(old.insert(), [
            dict(order_key=1, line_no=1, amount=100),
            dict(order_key=1, line_no=2, amount=200),
        ])
        self.session.execute(new.insert(), [
            dict(order_key=1, line_no=1, amount=100),
            dict(order_key=1, line_no=2, amount=250),
        ])

        report = reconcile(
            self.session.bind, old, new,
            business_keys=['order_key', 'line_no'],
        )
        diff = report.row_differences
        # two distinct unmatched shapes: old 200 missing, new 250 extra
        self.assertEqual(len(diff), 2)
        by_amount = {r['amount']: r for r in diff}
        self.assertEqual(by_amount[200]['missing_count'], 1)
        self.assertEqual(by_amount[200]['new_count'], 0)
        self.assertEqual(by_amount[250]['extra_count'], 1)
        self.assertEqual(by_amount[250]['old_count'], 0)

        by_key = {
            (r['order_key'], r['line_no']): r for r in report.key_summary
        }
        line1 = by_key[(1, 1)]
        line2 = by_key[(1, 2)]
        self.assertEqual(line1['matched_count'], 1)
        self.assertEqual(line2['old_count'], 1)
        self.assertEqual(line2['new_count'], 1)
        self.assertEqual(line2['matched_count'], 0)
        self.assertEqual(line2['missing_count'], 1)
        self.assertEqual(line2['extra_count'], 1)

        # grand totals still see both rows per side
        self.assertEqual(report.totals['old_count'], 2)
        self.assertEqual(report.totals['new_count'], 2)
        self.assertEqual(report.totals['matched_count'], 1)

    def test_empty_tables(self):
        old, new = _build_pair(self.session, 'empty_tables')
        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        self.assertTrue(report.is_balanced)
        self.assertEqual(report.key_summary, [])
        self.assertEqual(report.row_differences, [])
        self.assertEqual(
            report.totals,
            {'old_count': 0, 'new_count': 0, 'matched_count': 0,
             'missing_count': 0, 'extra_count': 0},
        )

    def test_only_unmatched_false_keeps_matched_shapes(self):
        old, new = _build_pair(self.session, 'keep_matched')
        _insert(self.session, old, [(1, 10, 'a'), (2, 5, None)])
        _insert(self.session, new, [(1, 10, 'a'), (3, 7, 'z')])

        bundle = build_reconciliation(
            old, new, ['order_key'], only_unmatched_rows=False
        )
        rows = [dict(r._mapping)
                for r in self.session.execute(bundle.row_differences)]
        # all three distinct shapes are present, including the matched one
        self.assertEqual(len(rows), 3)
        matched = [r for r in rows
                   if r['order_key'] == 1][0]
        self.assertEqual(
            (matched['old_count'], matched['new_count'],
             matched['matched_count']),
            (1, 1, 1),
        )

    def test_explicit_attribute_subset(self):
        md = MetaData()
        old = Table(
            'subset_old', md,
            Column('order_key', types.Int64),
            Column('amount', types.Nullable(types.Int64())),
            Column('note', types.Nullable(types.String())),
            engines.Memory(),
        )
        new = Table(
            'subset_new', md,
            Column('order_key', types.Int64),
            Column('amount', types.Nullable(types.Int64())),
            Column('note', types.Nullable(types.String())),
            engines.Memory(),
        )
        for t in (old, new):
            t.drop(bind=self.session.bind, if_exists=True)
            t.create(bind=self.session.bind)

        # same key+amount, different note -> note excluded => fully matched
        _insert(self.session, old, [(1, 10, 'old-note')])
        _insert(self.session, new, [(1, 10, 'new-note')])

        report = reconcile(
            self.session.bind, old, new,
            business_keys=['order_key'],
            attributes=['amount'],
        )
        self.assertTrue(report.is_balanced)
        self.assertEqual(report.totals['matched_count'], 1)

    def test_selectable_sources_align_by_output_name(self):
        old, new = _build_pair(self.session, 'selectable_src')
        _insert(self.session, old, [(1, 10, 'a')])
        _insert(self.session, new, [(1, 10, 'a'), (2, 5, None)])

        old_sel = select(old.c.order_key, old.c.amount, old.c.note).alias('o')
        new_sel = select(new.c.order_key, new.c.amount, new.c.note).alias('n')

        report = reconcile(
            self.session.bind, old_sel, new_sel,
            business_keys=['order_key'],
        )
        self.assertEqual(report.totals['old_count'], 1)
        self.assertEqual(report.totals['new_count'], 2)
        self.assertEqual(report.totals['matched_count'], 1)

    def test_generated_statement_is_reusable_in_larger_query(self):
        old, new = _build_pair(self.session, 'composable')
        _insert(self.session, old, [(1, 10, 'a'), (2, 5, None)])
        _insert(self.session, new, [(1, 10, 'a'), (3, 7, 'z')])

        bundle = build_reconciliation(old, new, ['order_key'])
        embedded = bundle.row_differences.subquery('diff')
        # add an outer projection/filter on top of the supplied statement
        q = select(sa_func.count()).select_from(embedded).where(
            embedded.c.extra_count > 0
        )
        self.assertEqual(self.session.execute(q).scalar(), 1)

        # the CTE spine can also be consumed directly
        spine = bundle.side_counts.subquery('spine')
        total = self.session.execute(
            select(sa_func.sum(spine.c.row_count))
        ).scalar()
        self.assertEqual(total, 4)

    def test_reflected_tables_are_accepted(self):
        old, new = _build_pair(self.session, 'reflected_src')
        _insert(self.session, old, [(1, 10, 'a')])
        _insert(self.session, new, [(1, 10, 'a')])

        reflected_old = Table(
            'reflected_src_old', MetaData(), autoload_with=self.session.bind
        )
        reflected_new = Table(
            'reflected_src_new', MetaData(), autoload_with=self.session.bind
        )
        report = reconcile(
            self.session.bind, reflected_old, reflected_new,
            business_keys=['order_key'],
        )
        self.assertTrue(report.is_balanced)

    def test_nullable_business_key_nulls_match(self):
        md = MetaData()
        old = Table(
            'nullable_key_old', md,
            Column('order_key', types.Nullable(types.Int64())),
            Column('note', types.Nullable(types.String())),
            engines.Memory(),
        )
        new = Table(
            'nullable_key_new', md,
            Column('order_key', types.Nullable(types.Int64())),
            Column('note', types.Nullable(types.String())),
            engines.Memory(),
        )
        for t in (old, new):
            t.drop(bind=self.session.bind, if_exists=True)
            t.create(bind=self.session.bind)

        self.session.execute(old.insert(), [
            dict(order_key=None, note='x'),
            dict(order_key=1, note='y'),
        ])
        self.session.execute(new.insert(), [
            dict(order_key=None, note='x'),   # NULL key + same note -> match
            dict(order_key=1, note='z'),
        ])

        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        self.assertEqual(
            (report.totals['matched_count'],
             report.totals['missing_count'],
             report.totals['extra_count']),
            (1, 1, 1),
        )
        # the NULL-key identical row does not appear among differences
        self.assertFalse(
            any(r['order_key'] is None for r in report.row_differences)
        )

    def test_row_order_does_not_matter(self):
        old, new = _build_pair(self.session, 'unordered')
        _insert(self.session, old, [
            (3, 30, 'c'), (1, 10, 'a'), (2, 20, 'b'),
            (1, 10, 'a'),
        ])
        _insert(self.session, new, [
            (1, 10, 'a'), (3, 30, 'c'), (1, 10, 'a'),
            (2, 20, 'b'),
        ])
        report = reconcile(
            self.session.bind, old, new, business_keys=['order_key']
        )
        self.assertTrue(report.is_balanced)


from tests.testcase import (  # noqa: E402
    HttpSessionTestCase, NativeSessionTestCase,
)


class NativeReconcileTestCase(ReconcileScenarioMixin, NativeSessionTestCase):
    session = NativeSessionTestCase.session


class HttpReconcileTestCase(ReconcileScenarioMixin, HttpSessionTestCase):
    session = HttpSessionTestCase.session
