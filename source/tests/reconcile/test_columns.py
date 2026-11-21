"""
Unit tests for column alignment and type-compatibility validation.

These build metadata objects only -- no ClickHouse round-trip is needed.
"""
from sqlalchemy import Column, MetaData

from clickhouse_sqlalchemy import Table, engines, types
from clickhouse_sqlalchemy.reconcile import (
    align_columns,
    build_reconciliation,
    IncompatibleTypesError,
    InvalidSpecificationError,
    MissingColumnError,
)
from tests.testcase import BaseTestCase


def _tables(old_cols, new_cols, name='t'):
    md = MetaData()
    old = Table(f'{name}_old', md, *old_cols, engines.Memory())
    new = Table(f'{name}_new', md, *new_cols, engines.Memory())
    return old, new


class AlignColumnsTestCase(BaseTestCase):

    def test_aligns_by_name_and_separates_keys_from_attributes(self):
        old, new = _tables([
            Column('order_key', types.Int64),
            Column('amount', types.Nullable(types.Int64)),
        ], [
            Column('order_key', types.Int32),
            Column('amount', types.Nullable(types.Int64)),
        ])

        aligned = align_columns(old, new, business_keys=['order_key'])

        self.assertEqual(aligned.key_names, ('order_key',))
        self.assertEqual(aligned.compared_names, ('order_key', 'amount'))
        self.assertTrue(aligned.business_keys[0].is_business_key)
        self.assertFalse(aligned.attributes[0].is_business_key)

    def test_explicit_attributes_and_default_all_non_key_columns(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('a', types.String()),
            Column('b', types.Int32),
        ], [
            Column('k', types.Int64),
            Column('a', types.String()),
            Column('b', types.Int32),
        ])

        self.assertEqual(
            align_columns(old, new, ['k']).compared_names, ('k', 'a', 'b')
        )
        self.assertEqual(
            align_columns(old, new, ['k'], ['a']).compared_names, ('k', 'a')
        )

    def test_missing_column_on_one_side_raises(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('only_old', types.String()),
        ], [
            Column('k', types.Int64),
        ], name='missing_one')

        with self.assertRaises(MissingColumnError) as cm:
            align_columns(old, new, ['k'], ['only_old'])
        self.assertIn('only_old', str(cm.exception))
        self.assertIn('new side', str(cm.exception))

    def test_missing_column_on_both_sides_raises(self):
        old, new = _tables([
            Column('k', types.Int64),
        ], [
            Column('k', types.Int64),
        ], name='missing_both')

        with self.assertRaises(MissingColumnError) as cm:
            align_columns(old, new, ['ghost'])
        self.assertIn('both sides', str(cm.exception))

    def test_integer_widths_are_compatible(self):
        old, new = _tables([
            Column('k', types.Int32),
        ], [
            Column('k', types.UInt64),
        ], name='int_width')

        aligned = align_columns(old, new, ['k'])
        self.assertEqual(aligned.compared_names, ('k',))

    def test_nullable_and_lowcardinality_wrappers_are_peeled(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('v', types.Nullable(types.LowCardinality(types.String()))),
        ], [
            Column('k', types.Int64),
            Column('v', types.LowCardinality(types.String())),
        ], name='wrappers')

        aligned = align_columns(old, new, ['k'])
        self.assertEqual(aligned.compared_names, ('k', 'v'))

    def test_string_vs_integer_is_incompatible(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('v', types.Int64),
        ], [
            Column('k', types.Int64),
            Column('v', types.String()),
        ], name='str_int')

        with self.assertRaises(IncompatibleTypesError) as cm:
            align_columns(old, new, ['k'], ['v'])
        self.assertIn('v', str(cm.exception))

    def test_float_vs_decimal_is_incompatible(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('v', types.Float64()),
        ], [
            Column('k', types.Int64),
            Column('v', types.Decimal(10, 2)),
        ], name='float_dec')

        with self.assertRaises(IncompatibleTypesError):
            align_columns(old, new, ['k'], ['v'])

    def test_string_vs_date_is_incompatible(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('v', types.String()),
        ], [
            Column('k', types.Int64),
            Column('v', types.Date()),
        ], name='str_date')

        with self.assertRaises(IncompatibleTypesError):
            align_columns(old, new, ['k'], ['v'])

    def test_array_item_types_must_match(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('v', types.Array(types.Int32())),
        ], [
            Column('k', types.Int64),
            Column('v', types.Array(types.String())),
        ], name='array_mismatch')

        with self.assertRaises(IncompatibleTypesError):
            align_columns(old, new, ['k'], ['v'])

    def test_map_value_types_must_match(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('v', types.Map(types.String(), types.Int32())),
        ], [
            Column('k', types.Int64),
            Column('v', types.Map(types.String(), types.Int64())),
        ], name='map_mismatch')
        # same integer family -> compatible
        self.assertEqual(
            align_columns(old, new, ['k']).compared_names, ('k', 'v')
        )

        md = MetaData()
        old2 = Table('map_old2', md,
                     Column('k', types.Int64),
                     Column('v', types.Map(types.String(), types.Int32())),
                     engines.Memory())
        new2 = Table('map_new2', md,
                     Column('k', types.Int64),
                     Column('v', types.Map(types.String(), types.String())),
                     engines.Memory())
        with self.assertRaises(IncompatibleTypesError):
            align_columns(old2, new2, ['k'], ['v'])

    def test_empty_business_keys_rejected(self):
        old, new = _tables([
            Column('k', types.Int64),
        ], [
            Column('k', types.Int64),
        ], name='empty_keys')
        with self.assertRaises(InvalidSpecificationError):
            align_columns(old, new, [])

    def test_key_attribute_overlap_rejected(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('a', types.String()),
        ], [
            Column('k', types.Int64),
            Column('a', types.String()),
        ], name='overlap')
        with self.assertRaises(InvalidSpecificationError):
            align_columns(old, new, ['k', 'a'], ['a'])

    def test_duplicate_declarations_rejected(self):
        old, new = _tables([
            Column('k', types.Int64),
        ], [
            Column('k', types.Int64),
        ], name='dup')
        with self.assertRaises(InvalidSpecificationError):
            align_columns(old, new, ['k', 'k'])

    def test_bundle_exposes_composable_statements(self):
        old, new = _tables([
            Column('k', types.Int64),
            Column('a', types.Nullable(types.String())),
        ], [
            Column('k', types.Int64),
            Column('a', types.Nullable(types.String())),
        ], name='bundle')

        bundle = build_reconciliation(old, new, ['k'])
        self.assertIsNotNone(bundle.side_counts)
        self.assertIsNotNone(bundle.row_differences)
        self.assertIsNotNone(bundle.key_summary)
        self.assertIsNotNone(bundle.totals)

        bundle_no_totals = build_reconciliation(
            old, new, ['k'], include_totals=False
        )
        self.assertIsNone(bundle_no_totals.totals)
