"""Data-only before/after measurement, independent from generated code."""
import math
import numpy as np
import pandas as pd
from .schemas import ValidationReport


def scalar(value):
    if pd.isna(value):
        return None
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, (str, int, float, bool)):
        return value if not isinstance(value, float) or math.isfinite(value) else str(value)
    return str(value)


def statistics(series):
    numeric = pd.to_numeric(series, errors='coerce').dropna()
    if numeric.empty:
        return {}
    # NumPy quantile interpolates by subtraction, which is undefined for bool.
    # Measure boolean proportions numerically without changing dataset values.
    if pd.api.types.is_bool_dtype(numeric):
        numeric = numeric.astype(float)
    return {name: scalar(value) for name, value in {
        'mean': numeric.mean(), 'median': numeric.median(), 'std': numeric.std(),
        'min': numeric.min(), 'max': numeric.max(),
        'q25': numeric.quantile(.25), 'q75': numeric.quantile(.75),
        'q01': numeric.quantile(.01), 'q99': numeric.quantile(.99)}.items()}


def validate_transformation(before, after, item, target_before=None, target_after=None):
    result = ValidationReport(rows_before=len(before), columns_before=len(before.columns))
    if not isinstance(after, pd.DataFrame):
        result.errors.append('Output is not a DataFrame')
        return result
    result.rows_after, result.columns_after = after.shape
    if not before.index.is_unique or not after.index.is_unique or not after.columns.is_unique:
        result.errors.append('Unique stable row index and column labels are required')
        return result
    if any(not isinstance(c, str) for c in after.columns):
        result.errors.append('Column names must be strings')
        return result
    result.columns_added = [c for c in after if c not in before]
    result.columns_removed = [c for c in before if c not in after]
    result.dtypes_before = {c: str(t) for c, t in before.dtypes.items()}
    result.dtypes_after = {c: str(t) for c, t in after.dtypes.items()}
    result.missing_before = {c: int(n) for c, n in before.isna().sum().items()}
    result.missing_after = {c: int(n) for c, n in after.isna().sum().items()}
    result.duplicates_before = int(before.duplicated().sum())
    result.duplicates_after = int(after.duplicated().sum())
    result.unique_before = {c: int(before[c].nunique()) for c in before}
    result.unique_after = {c: int(after[c].nunique()) for c in after}
    common_rows = before.index.intersection(after.index, sort=False)
    common_columns = before.columns.intersection(after.columns, sort=False)
    removed = before.index.difference(after.index)
    added = after.index.difference(before.index)
    changed_rows = set(removed) | set(added)
    changed_columns = set(result.columns_added + result.columns_removed)
    representation_only = not changed_columns and not len(removed) and not len(added)
    for column in common_columns:
        left, right = before.loc[common_rows, column], after.loc[common_rows, column]
        equal = (left.eq(right) | (left.isna() & right.isna())).fillna(False)
        changed = ~equal
        dtype_changed = str(left.dtype) != str(right.dtype)
        if changed.any() or dtype_changed:
            changed_columns.add(column)
        result.changed_cells += int(changed.sum())
        changed_rows.update(common_rows[changed])
        result.new_missing_values += int((left.notna() & right.isna()).sum())
        harmless_text = equal.all()
        if left.map(lambda x: isinstance(x, str)).any():
            string_equivalent = left.map(lambda x: x.strip() if isinstance(x, str) else x)
            harmless_text = (string_equivalent.eq(right) | (left.isna() & right.isna())).fillna(False).all()
        numeric_left, numeric_right = pd.to_numeric(left, errors='coerce'), pd.to_numeric(right, errors='coerce')
        harmless_numeric = (numeric_left.eq(numeric_right) | (left.isna() & right.isna())).fillna(False).all() and pd.api.types.is_numeric_dtype(right)
        representation_only &= bool(harmless_text or harmless_numeric)
        if pd.api.types.is_numeric_dtype(left) and not pd.api.types.is_numeric_dtype(right):
            result.errors.append(f'Numeric dtype damaged: {column}')
        if pd.api.types.is_datetime64_any_dtype(left) and not pd.api.types.is_datetime64_any_dtype(right):
            result.errors.append(f'Datetime dtype damaged: {column}')
        if pd.api.types.is_numeric_dtype(right):
            introduced_inf = np.isinf(pd.to_numeric(right, errors='coerce').astype(float)) & ~np.isinf(pd.to_numeric(left, errors='coerce').astype(float))
            if introduced_inf.any():
                result.errors.append(f'New infinite values: {column}')
        if pd.api.types.is_numeric_dtype(left) or pd.api.types.is_numeric_dtype(right):
            result.distribution_changes[column] = {'before': statistics(before[column]), 'after': statistics(after[column])}
        if pd.api.types.is_datetime64_any_dtype(left) or pd.api.types.is_datetime64_any_dtype(right):
            result.date_ranges[column] = {'before': [scalar(before[column].min()), scalar(before[column].max())],
                                          'after': [scalar(after[column].min()), scalar(after[column].max())]}
        for idx in common_rows[changed][:max(0, 5-len(result.samples))]:
            result.samples.append({'row': str(idx), 'column': column,
                                   'before': scalar(left.loc[idx]), 'after': scalar(right.loc[idx])})
    result.changed_cells += len(removed)*len(before.columns) + len(added)*len(after.columns)
    result.changed_cells += len(common_rows)*(len(result.columns_added)+len(result.columns_removed))
    if result.columns_added or result.columns_removed:
        changed_rows.update(common_rows)
    result.changed_rows = len(changed_rows)
    result.changed_ratio = result.changed_cells / max(1, before.size)
    result.schema_changes = sorted(changed_columns & set(result.columns_added + result.columns_removed))
    result.exact_duplicate_removal = bool(len(removed) and before.drop_duplicates().equals(after))
    result.representation_only = bool(representation_only)
    if len(added):
        result.errors.append('Row creation or index rewriting is forbidden')
    if len(removed) and not (item.allow_row_deletion or result.exact_duplicate_removal):
        result.errors.append('Unexpected row deletion')
    if len(before) and not len(after):
        result.errors.append('Deleting the entire dataset is forbidden')
    if result.columns_removed and not item.allow_column_deletion:
        result.errors.append('Unexpected column deletion')
    if item.scope == 'column' and changed_columns - set(item.columns):
        result.errors.append('Changes outside the declared target columns')
    if result.new_missing_values and not item.allow_new_missing:
        result.errors.append('Unexpected new missing values')
    if not result.changed_cells and result.dtypes_before == result.dtypes_after:
        result.errors.append('No measurable transformation')
    if item.intent == 'flag':
        preserved = not result.columns_removed and before.equals(after[list(before.columns)])
        if not preserved:
            result.errors.append('Flagging must preserve all existing rows and values')
        if not result.columns_added:
            result.errors.append('Flagging requires a new audit column')
        result.warnings.append('Signalement uniquement : le problème reste ouvert et les valeurs sont conservées.')
    elif target_before is not None and target_after is not None:
        result.target_issue_improved = target_after < target_before
        if not result.target_issue_improved:
            result.errors.append('Target issue did not improve')
    else:
        result.warnings.append('Semantic target improvement needs human verification')
    result.passed = not result.errors
    return result
