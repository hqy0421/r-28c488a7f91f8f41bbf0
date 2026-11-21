"""
Exceptions raised by the multiset reconciliation helpers.
"""


class ReconciliationError(Exception):
    """Base class for reconciliation configuration errors."""


class ColumnMismatchError(ReconciliationError):
    """The two sides cannot be aligned by column name."""


class MissingColumnError(ColumnMismatchError):
    """A declared column does not exist on one (or both) sides."""


class IncompatibleTypesError(ColumnMismatchError):
    """Same-named columns have type families that cannot be compared."""


class InvalidSpecificationError(ReconciliationError):
    """The reconciliation specification itself is invalid."""
