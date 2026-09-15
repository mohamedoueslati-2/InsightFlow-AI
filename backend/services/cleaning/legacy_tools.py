"""
Deterministic Python/Pandas tools for the AI Cleaning Agent.

Two categories of tools are exposed to the ADK agent:
  1) Inspection tools  -- delegate directly to ProfilingToolkit (read-only).
  2) Mutation tools     -- a fixed, closed catalog of named, deterministic
                            operations. Every mutation tool:
       - operates only on a private working copy (`self.df`);
       - checks preconditions and fails loudly instead of silently no-oping;
       - snapshots state before mutating and only COMMITS the change when the
         deterministic validator (validator.py) confirms the targeted problem
         improved without new problems being introduced;
       - is bounded by a per-problem attempt budget enforced in code, not by
         LLM self-discipline.

Gemini never writes or executes transformation code here -- it only selects,
sequences, and parameterizes these fixed tool calls.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..profiling.tools import ProfilingToolkit
from . import validator as V


_IDENTIFIER_NAME_HINTS = ("id", "code", "key", "uuid", "guid", "reference", "ref_number", "record_number")


def _column_name_looks_identifier(name: str) -> bool:
    lowered = str(name).lower()
    return any(hint in lowered for hint in _IDENTIFIER_NAME_HINTS)


def _to_native(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if pd.isna(v):
        return None
    return v


class CleaningToolkit:
    """
    Toolkit bound to a specific pandas DataFrame. Exposes read-only
    inspection tools (delegated to ProfilingToolkit) and a fixed catalog of
    mutation tools with built-in snapshot/validate/commit-or-reject logic.
    """

    def __init__(self, df: pd.DataFrame, max_attempts_per_problem: int = 2):
        self.original_df: pd.DataFrame = df.copy()
        self.df: pd.DataFrame = df.copy()
        self.max_attempts_per_problem = max_attempts_per_problem

        self._attempt_counts: Dict[str, int] = {}
        self._last_snapshot: Optional[pd.DataFrame] = None
        self._last_snapshot_meta: Optional[Dict[str, Any]] = None

        self.actions: List[Dict[str, Any]] = []
        self.operation_log: List[Dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Tool registration
    # -------------------------------------------------------------------------
    def get_tools(self) -> List[Callable]:
        return [
            # Inspection tools (read-only, delegate to ProfilingToolkit)
            self.get_dataset_overview,
            self.get_sample_rows,
            self.inspect_column,
            self.analyze_numeric_column,
            self.analyze_text_column,
            self.analyze_categorical_column,
            self.analyze_date_column,
            self.detect_patterns,
            self.detect_missing_values,
            self.detect_duplicates,
            self.detect_outliers,
            self.compare_columns,
            # Tier 1 mutation tools
            self.drop_exact_duplicates,
            self.trim_whitespace,
            self.normalize_case,
            self.canonicalize_categories,
            self.cast_column_type,
            # Tier 2 mutation tools
            self.flag_domain_implausible,
            self.cap_outliers,
            self.impute_missing,
            self.detect_and_correct_systematic_date_offset,
            self.flag_temporal_anomaly,
            # State management
            self.rollback_last,
        ]

    def get_tools_with_manual(self) -> List[Callable]:
        """
        Extends get_tools() with the human-in-the-loop-only manual tools.
        Used exclusively by the per-issue resolution conversation agent, where
        a human is explicitly present and directing the decision -- never by
        the autonomous full-dataset cleaning agent.
        """
        return self.get_tools() + [
            self.manual_fill_missing,
            self.manual_drop_rows_with_missing,
        ]

    # -------------------------------------------------------------------------
    # Inspection tools -- reuse ProfilingToolkit against the *current* working copy
    # -------------------------------------------------------------------------
    def _profiler(self) -> ProfilingToolkit:
        return ProfilingToolkit(self.df)

    def get_dataset_overview(self) -> Dict[str, Any]:
        """Structural metrics of the current working copy (rows, columns, dtypes, missing, duplicates)."""
        return self._profiler().get_dataset_overview()

    def get_sample_rows(self, n: int = 10) -> Dict[str, Any]:
        """Up to n representative sample rows from the current working copy."""
        return self._profiler().get_sample_rows(n)

    def inspect_column(self, column_name: str) -> Dict[str, Any]:
        """Dtype, missingness, cardinality, and sample/frequent values for one column."""
        return self._profiler().inspect_column(column_name)

    def analyze_numeric_column(self, column_name: str) -> Dict[str, Any]:
        """Full statistical analysis of a numeric non-boolean column."""
        return self._profiler().analyze_numeric_column(column_name)

    def analyze_text_column(self, column_name: str) -> Dict[str, Any]:
        """Length/character-set statistics for a textual column."""
        return self._profiler().analyze_text_column(column_name)

    def analyze_categorical_column(self, column_name: str) -> Dict[str, Any]:
        """Category distribution and inconsistent-representation detection."""
        return self._profiler().analyze_categorical_column(column_name)

    def analyze_date_column(self, column_name: str) -> Dict[str, Any]:
        """Temporal characteristics: valid/invalid dates, ranges, suspicious epochs/future dates."""
        return self._profiler().analyze_date_column(column_name)

    def detect_patterns(self, column_name: str) -> Dict[str, Any]:
        """Regex/format pattern matches (email, url, phone, uuid, etc.) for a column."""
        return self._profiler().detect_patterns(column_name)

    def detect_missing_values(self) -> Dict[str, Any]:
        """Dataset-wide missingness analysis, including semantic-missing suspects."""
        return self._profiler().detect_missing_values()

    def detect_duplicates(self) -> Dict[str, Any]:
        """Exact duplicate row analysis and candidate key columns."""
        return self._profiler().detect_duplicates()

    def detect_outliers(self, column_name: str) -> Dict[str, Any]:
        """IQR-based outlier counts and bounds for a numeric column."""
        return self._profiler().detect_outliers(column_name)

    def compare_columns(self, column_a: str, column_b: str) -> Dict[str, Any]:
        """Correlation/overlap/dependency comparison between two columns."""
        return self._profiler().compare_columns(column_a, column_b)

    # -------------------------------------------------------------------------
    # Internal mutation executor (snapshot -> mutate copy -> validate -> commit/reject)
    # -------------------------------------------------------------------------
    def _budget_key(self, column: Optional[str], problem: str) -> str:
        return f"{column or 'dataset'}::{problem}"

    def _log_step(self, tool: str, column: Optional[str], outcome: str, detail: str) -> None:
        self.operation_log.append({
            "step": len(self.operation_log) + 1,
            "tool": tool,
            "column": column,
            "outcome": outcome,
            "detail": detail,
        })

    def _apply(
        self,
        op_name: str,
        column: Optional[str],
        problem: str,
        reason: str,
        confidence: float,
        mutate_fn: Callable[[pd.DataFrame], Tuple[pd.DataFrame, Dict[str, Any]]],
        validate_fn: Callable[[pd.DataFrame, pd.DataFrame, Dict[str, Any]], Dict[str, Any]],
        precondition_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
        resolved_by: str = "agent",
    ) -> Dict[str, Any]:
        key = self._budget_key(column, problem)

        if self._attempt_counts.get(key, 0) >= self.max_attempts_per_problem:
            msg = f"Attempt budget ({self.max_attempts_per_problem}) exhausted for problem '{problem}' on {column or 'dataset'}."
            self._log_step(op_name, column, "budget_exhausted", msg)
            return {"kept": False, "outcome": "budget_exhausted", "reason": msg}

        self._attempt_counts[key] = self._attempt_counts.get(key, 0) + 1

        if precondition_fn is not None:
            ok, msg = precondition_fn()
            if not ok:
                self._log_step(op_name, column, "precondition_failed", msg)
                return {"kept": False, "outcome": "precondition_failed", "reason": msg}

        before_df = self.df.copy()

        try:
            after_df, extra = mutate_fn(before_df)
        except Exception as exc:
            self._log_step(op_name, column, "error", str(exc))
            return {"kept": False, "outcome": "error", "reason": str(exc)}

        validation = validate_fn(before_df, after_df, extra)
        rows_affected = int(extra.get("rows_affected", 0))

        if validation.get("passed"):
            audit = {k: v for k, v in extra.items() if k not in ("rows_affected", "no_op")}
            if audit:
                validation = dict(validation)
                validation["audit"] = audit

            self._last_snapshot = before_df
            self._last_snapshot_meta = {"op": op_name, "column": column}
            self.df = after_df

            action = {
                "problem": problem,
                "column": column,
                "action": op_name,
                "reason": reason,
                "rows_affected": rows_affected,
                "confidence": confidence,
                "validation": validation,
                "resolved_by": resolved_by,
            }
            self.actions.append(action)
            self._log_step(op_name, column, "kept", validation.get("details", ""))
            return {"kept": True, "outcome": "kept", "validation": validation, "rows_affected": rows_affected, "action": action}

        outcome = "no_correction_applicable" if extra.get("no_op") else "rolled_back"
        self._log_step(op_name, column, outcome, validation.get("details", ""))
        return {"kept": False, "outcome": outcome, "validation": validation, "reason": validation.get("details", "")}

    def rollback_last(self) -> Dict[str, Any]:
        """Restores the DataFrame to its state before the last COMMITTED mutation, undoing that action."""
        if self._last_snapshot is None:
            return {"success": False, "message": "No committed operation available to roll back."}

        self.df = self._last_snapshot
        removed_action = self.actions.pop() if self.actions else None
        meta = self._last_snapshot_meta or {}
        self._last_snapshot = None
        self._last_snapshot_meta = None

        self._log_step("rollback_last", meta.get("column"), "rolled_back", "Manual rollback of last committed operation.")
        return {"success": True, "rolled_back_action": removed_action}

    # -------------------------------------------------------------------------
    # Tier 1 mutation tools
    # -------------------------------------------------------------------------
    def drop_exact_duplicates(
        self, problem: str = "duplicate_records", reason: str = "Exact duplicate rows detected.", confidence: float = 0.9
    ) -> Dict[str, Any]:
        """Removes exact duplicate rows (keeping the first occurrence)."""

        def precondition() -> Tuple[bool, str]:
            if int(self.df.duplicated().sum()) == 0:
                return False, "No exact duplicate rows found; nothing to drop."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.drop_duplicates(keep="first").reset_index(drop=True)
            return after_df, {"rows_affected": len(before_df) - len(after_df)}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_drop_exact_duplicates(before_df, after_df)

        return self._apply("drop_exact_duplicates", None, problem, reason, confidence, mutate, validate, precondition)

    def trim_whitespace(
        self, column: str, problem: str = "inconsistent_formats", reason: str = "Leading/trailing whitespace detected.",
        confidence: float = 0.9,
    ) -> Dict[str, Any]:
        """Strips leading/trailing whitespace from a text-like column."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}

        def precondition() -> Tuple[bool, str]:
            series = self.df[column]
            if series.dtype != object and not pd.api.types.is_string_dtype(series):
                return False, f"Column '{column}' is not text-like; whitespace trimming not applicable."
            non_null = series.dropna().astype(str)
            untrimmed = int((non_null != non_null.str.strip()).sum())
            if untrimmed == 0:
                return False, f"No untrimmed whitespace found in '{column}'."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            mask = after_df[column].notna()
            before_vals = after_df.loc[mask, column].astype(str)
            stripped = before_vals.str.strip()
            rows_affected = int((before_vals != stripped).sum())
            after_df.loc[mask, column] = stripped
            return after_df, {"rows_affected": rows_affected}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_trim_whitespace(before_df[column], after_df[column])

        return self._apply("trim_whitespace", column, problem, reason, confidence, mutate, validate, precondition)

    def normalize_case(
        self, column: str, target_case: str = "lower", problem: str = "inconsistent_formats",
        reason: str = "Inconsistent letter casing detected.", confidence: float = 0.85,
    ) -> Dict[str, Any]:
        """Normalizes casing of a text column ('lower' | 'upper' | 'title')."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}
        if target_case not in {"lower", "upper", "title"}:
            return {"kept": False, "outcome": "error", "reason": "target_case must be 'lower', 'upper', or 'title'."}

        def precondition() -> Tuple[bool, str]:
            series = self.df[column]
            if series.dtype != object and not pd.api.types.is_string_dtype(series):
                return False, f"Column '{column}' is not text-like."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            mask = after_df[column].notna()
            s = after_df.loc[mask, column].astype(str)
            if target_case == "lower":
                transformed = s.str.lower()
            elif target_case == "upper":
                transformed = s.str.upper()
            else:
                transformed = s.str.title()
            rows_affected = int((s != transformed).sum())
            after_df.loc[mask, column] = transformed
            return after_df, {"rows_affected": rows_affected}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_normalize_case(before_df[column], after_df[column])

        return self._apply("normalize_case", column, problem, reason, confidence, mutate, validate, precondition)

    def canonicalize_categories(
        self, column: str, mapping: Dict[str, str], problem: str = "inconsistent_formats",
        reason: str = "Near-identical categorical variants detected.", confidence: float = 0.85,
    ) -> Dict[str, Any]:
        """Replaces known variant strings with a single canonical value (mapping keys must already exist in the column)."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}
        if not mapping:
            return {"kept": False, "outcome": "error", "reason": "mapping must be a non-empty dict of variant -> canonical value."}

        def precondition() -> Tuple[bool, str]:
            current_values = set(self.df[column].dropna().astype(str).unique().tolist())
            missing_keys = [k for k in mapping if k not in current_values]
            if missing_keys:
                return False, f"mapping keys not present in column '{column}': {missing_keys}"
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            col_str = after_df[column].astype(str)
            replace_mask = after_df[column].notna() & col_str.isin(mapping.keys())
            rows_affected = int(replace_mask.sum())
            after_df.loc[replace_mask, column] = col_str[replace_mask].map(mapping)
            return after_df, {"rows_affected": rows_affected, "mapping": dict(mapping)}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_canonicalize_categories(before_df[column], after_df[column])

        return self._apply("canonicalize_categories", column, problem, reason, confidence, mutate, validate, precondition)

    def cast_column_type(
        self, column: str, target_type: str, problem: str = "type_mismatch",
        reason: str = "Column values are unambiguously miscast.", confidence: float = 0.85,
    ) -> Dict[str, Any]:
        """Casts a column to 'int' | 'float' | 'string' | 'datetime' | 'bool', refusing casts that would create new missing values."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}
        if target_type not in {"int", "float", "string", "datetime", "bool"}:
            return {"kept": False, "outcome": "error", "reason": "target_type must be one of int/float/string/datetime/bool."}

        def _convert(series: pd.Series) -> pd.Series:
            if target_type in ("int", "float"):
                return pd.to_numeric(series, errors="coerce")
            if target_type == "datetime":
                return pd.to_datetime(series, errors="coerce")
            if target_type == "bool":
                def to_bool(x: Any) -> Optional[bool]:
                    if pd.isna(x):
                        return None
                    if isinstance(x, (bool, np.bool_)):
                        return bool(x)
                    t = str(x).strip().lower()
                    if t in {"true", "1", "yes", "y", "t"}:
                        return True
                    if t in {"false", "0", "no", "n", "f"}:
                        return False
                    return None
                return series.map(to_bool)
            return series.astype(str)

        def precondition() -> Tuple[bool, str]:
            series = self.df[column]
            missing_before = int(series.isna().sum())
            converted = _convert(series)
            missing_after = int(converted.isna().sum())
            if missing_after > missing_before:
                return False, (
                    f"Casting '{column}' to {target_type} would turn "
                    f"{missing_after - missing_before} valid values into missing data; not a safe/unambiguous cast."
                )
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            converted = _convert(before_df[column])
            if target_type == "int":
                non_null = converted.dropna()
                if len(non_null) > 0 and (non_null == non_null.round()).all():
                    converted = converted.astype("Int64")
            after_df[column] = converted
            rows_affected = int((before_df[column].astype(str) != after_df[column].astype(str)).sum())
            return after_df, {"rows_affected": rows_affected}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_cast_column_type(before_df[column], after_df[column])

        return self._apply("cast_column_type", column, problem, reason, confidence, mutate, validate, precondition)

    # -------------------------------------------------------------------------
    # Tier 2 mutation tools
    # -------------------------------------------------------------------------
    def flag_domain_implausible(
        self, column: str, method: str = "iqr", lower_bound: Optional[float] = None, upper_bound: Optional[float] = None,
        problem: str = "domain_implausible", reason: str = "Physically/logically implausible values detected.",
        confidence: float = 0.8,
    ) -> Dict[str, Any]:
        """Flags (never deletes) rows outside plausible bounds. method='iqr' or 'bounds' (requires lower_bound/upper_bound)."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}
        if method not in {"iqr", "bounds"}:
            return {"kept": False, "outcome": "error", "reason": "method must be 'iqr' or 'bounds'."}
        if method == "bounds" and (lower_bound is None or upper_bound is None):
            return {"kept": False, "outcome": "error", "reason": "method='bounds' requires lower_bound and upper_bound."}

        def precondition() -> Tuple[bool, str]:
            nums = pd.to_numeric(self.df[column], errors="coerce").dropna()
            if nums.empty:
                return False, f"Column '{column}' has no numeric values to evaluate."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            nums = pd.to_numeric(before_df[column], errors="coerce")
            if method == "iqr":
                q1, q3 = nums.quantile(0.25), nums.quantile(0.75)
                iqr = q3 - q1
                lb, ub = float(q1 - 1.5 * iqr), float(q3 + 1.5 * iqr)
            else:
                lb, ub = float(lower_bound), float(upper_bound)
            flag_col = f"{column}_domain_implausible"
            implausible_mask = nums.notna() & ((nums < lb) | (nums > ub))
            after_df[flag_col] = implausible_mask
            return after_df, {"rows_affected": int(implausible_mask.sum()), "bounds": [lb, ub]}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_flag_domain_implausible(before_df[column], after_df[column])

        return self._apply("flag_domain_implausible", column, problem, reason, confidence, mutate, validate, precondition)

    def cap_outliers(
        self, column: str, method: str = "iqr", lower_percentile: float = 1.0, upper_percentile: float = 99.0,
        problem: str = "outliers", reason: str = "Extreme outlier values detected.", confidence: float = 0.8,
    ) -> Dict[str, Any]:
        """Winsorizes (caps, never deletes) outlier values. method='iqr' or 'percentile'. Records original replaced values."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}
        if method not in {"iqr", "percentile"}:
            return {"kept": False, "outcome": "error", "reason": "method must be 'iqr' or 'percentile'."}

        def precondition() -> Tuple[bool, str]:
            nums = pd.to_numeric(self.df[column], errors="coerce").dropna()
            if nums.empty:
                return False, f"Column '{column}' has no numeric values."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            nums = pd.to_numeric(before_df[column], errors="coerce")
            if method == "iqr":
                q1, q3 = nums.quantile(0.25), nums.quantile(0.75)
                iqr = q3 - q1
                lb, ub = float(q1 - 1.5 * iqr), float(q3 + 1.5 * iqr)
            else:
                lb, ub = float(nums.quantile(lower_percentile / 100.0)), float(nums.quantile(upper_percentile / 100.0))

            capped = nums.clip(lower=lb, upper=ub)
            changed_mask = nums.notna() & (nums != capped)
            original_values = {str(idx): _to_native(nums.loc[idx]) for idx in nums[changed_mask].index}

            new_col = before_df[column].copy()
            new_col.loc[nums.notna()] = capped[nums.notna()]
            after_df[column] = new_col

            return after_df, {
                "rows_affected": int(changed_mask.sum()),
                "bounds": [lb, ub],
                "original_values": original_values,
            }

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            lb, ub = extra["bounds"]
            return V.validate_cap_outliers(before_df[column], after_df[column], lb, ub)

        return self._apply("cap_outliers", column, problem, reason, confidence, mutate, validate, precondition)

    def absolute_negative_values(
        self, column: str, problem: str = "outliers",
        reason: str = "Valeurs négatives converties en valeurs absolues à la demande de l’utilisateur.",
        confidence: float = 1.0,
    ) -> Dict[str, Any]:
        """Convert numeric negative values to their absolute value, preserving nulls and positives."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}

        def precondition() -> Tuple[bool, str]:
            numeric = pd.to_numeric(self.df[column], errors="coerce")
            if numeric.notna().sum() != self.df[column].notna().sum():
                return False, "La colonne contient des valeurs non numériques."
            count = int((numeric < 0).sum())
            return (count > 0, "" if count else "Aucune valeur négative à convertir.")

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            numeric = pd.to_numeric(before_df[column], errors="coerce")
            mask = numeric < 0
            original = {str(idx): _to_native(numeric.loc[idx]) for idx in numeric[mask].index}
            after_df.loc[mask, column] = numeric.loc[mask].abs()
            return after_df, {"rows_affected": int(mask.sum()), "original_values": original}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            before = pd.to_numeric(before_df[column], errors="coerce")
            after = pd.to_numeric(after_df[column], errors="coerce")
            mask = before < 0
            unchanged = before.loc[~mask].equals(after.loc[~mask])
            passed = bool((after.loc[mask] == before.loc[mask].abs()).all() and unchanged)
            return {"passed": passed, "metric": "negative_value_count",
                    "before": int(mask.sum()), "after": int((after < 0).sum()),
                    "details": "Valeurs négatives converties en absolu ; autres valeurs conservées."}

        return self._apply("absolute_negative_values", column, problem, reason, confidence,
                           mutate, validate, precondition, resolved_by="user")

    def impute_missing(
        self, column: str, strategy: str = "median", problem: str = "missing_values",
        reason: str = "Missing values imputed from the column's own distribution.", confidence: float = 0.75,
    ) -> Dict[str, Any]:
        """Fills missing values (median/mean for numeric, mode for categorical). Tags every imputed cell. Refuses identifier-like columns."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}
        if strategy not in {"median", "mean", "mode"}:
            return {"kept": False, "outcome": "error", "reason": "strategy must be 'median', 'mean', or 'mode'."}

        def precondition() -> Tuple[bool, str]:
            series = self.df[column]
            missing = int(series.isna().sum())
            if missing == 0:
                return False, f"Column '{column}' has no missing values; nothing to impute."

            non_null = series.dropna()
            unique_ratio = (non_null.nunique() / len(non_null)) if len(non_null) > 0 else 0.0
            looks_identifier = (len(non_null) >= 20 and unique_ratio >= 0.95) or _column_name_looks_identifier(column)
            if looks_identifier:
                return False, f"Column '{column}' looks identifier-like (high cardinality); imputation refused."

            if strategy in {"median", "mean"} and pd.to_numeric(non_null, errors="coerce").dropna().empty:
                return False, f"Column '{column}' has no numeric values; use strategy='mode' for non-numeric columns."

            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            series = before_df[column]
            missing_mask = series.isna()

            if strategy == "mode":
                modes = series.dropna().mode()
                fill_value = modes.iloc[0] if not modes.empty else None
            else:
                nums = pd.to_numeric(series, errors="coerce").dropna()
                fill_value = float(nums.median()) if strategy == "median" else float(nums.mean())

            after_df[column] = series.fillna(fill_value)
            tag_col = f"{column}_was_imputed"
            after_df[tag_col] = missing_mask

            return after_df, {
                "rows_affected": int(missing_mask.sum()),
                "fill_value": _to_native(fill_value),
                "expected_filled_count": int(missing_mask.sum()),
            }

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_impute_missing(before_df[column], after_df[column], extra["expected_filled_count"])

        return self._apply("impute_missing", column, problem, reason, confidence, mutate, validate, precondition)

    def detect_and_correct_systematic_date_offset(
        self, column: str, problem: str = "temporal_anomaly",
        reason: str = "Measurable systematic year offset detected across many rows.", confidence: float = 0.75,
    ) -> Dict[str, Any]:
        """Corrects a date column ONLY when a single offset explains most anomalous rows; otherwise applies no correction."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}

        def precondition() -> Tuple[bool, str]:
            parsed = pd.to_datetime(self.df[column], errors="coerce")
            if parsed.dropna().empty:
                return False, f"Column '{column}' has no parseable dates."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            parsed = pd.to_datetime(before_df[column], errors="coerce")
            valid = parsed.dropna()
            now = pd.Timestamp.now()

            reference = valid[valid <= now]
            anomalous = valid[valid > now]
            anomalies_before = int(len(anomalous))
            total_valid = int(len(valid))

            def no_op() -> Tuple[pd.DataFrame, Dict[str, Any]]:
                return after_df, {
                    "rows_affected": 0, "no_op": True,
                    "anomalies_before": anomalies_before, "anomalies_after": anomalies_before,
                }

            if len(reference) == 0 or anomalies_before == 0 or anomalies_before < max(3, int(0.05 * total_valid)):
                return no_op()

            offset_years = (anomalous.dt.year - now.year)
            offset_counts = offset_years.value_counts()
            top_offset = int(offset_counts.index[0])
            top_count = int(offset_counts.iloc[0])
            systematic_ratio = top_count / len(anomalous) if len(anomalous) > 0 else 0.0

            if systematic_ratio < 0.6 or top_offset == 0:
                return no_op()

            target_mask = parsed.notna() & (parsed > now) & ((parsed.dt.year - now.year) == top_offset)
            corrected = parsed.copy()
            corrected.loc[target_mask] = parsed.loc[target_mask].apply(
                lambda d: d.replace(year=d.year - top_offset)
            )
            after_df[column] = corrected.dt.strftime("%Y-%m-%d").where(corrected.notna(), before_df[column])
            anomalies_after = int((corrected.dropna() > now).sum())

            return after_df, {
                "rows_affected": int(target_mask.sum()),
                "no_op": False,
                "anomalies_before": anomalies_before,
                "anomalies_after": anomalies_after,
                "offset_years": top_offset,
            }

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            if extra.get("no_op"):
                return V.no_correction_applicable(
                    "temporal_anomaly_count", extra["anomalies_before"],
                    "No systematic offset evidence found across enough rows; no correction applied.",
                )
            return V.validate_date_offset_correction(
                before_df[column], after_df[column], extra["anomalies_before"], extra["anomalies_after"]
            )

        return self._apply(
            "detect_and_correct_systematic_date_offset", column, problem, reason, confidence, mutate, validate, precondition
        )

    def flag_temporal_anomaly(
        self, column: str, problem: str = "temporal_anomaly", reason: str = "Isolated/scattered temporal anomalies detected.",
        confidence: float = 0.8,
    ) -> Dict[str, Any]:
        """Flags (never modifies) future/epoch/very-old/far-future dates."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}

        def precondition() -> Tuple[bool, str]:
            parsed = pd.to_datetime(self.df[column], errors="coerce")
            if parsed.dropna().empty:
                return False, f"Column '{column}' has no parseable dates."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            parsed = pd.to_datetime(before_df[column], errors="coerce")
            now = pd.Timestamp.now()
            anomaly_mask = parsed.notna() & (
                (parsed > now) | (parsed.dt.year == 1970) | (parsed.dt.year < 1900)
                | (parsed > (now + pd.DateOffset(years=10)))
            )
            flag_col = f"{column}_temporal_anomaly"
            after_df[flag_col] = anomaly_mask
            return after_df, {"rows_affected": int(anomaly_mask.sum())}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_flag_temporal_anomaly(before_df[column], after_df[column])

        return self._apply("flag_temporal_anomaly", column, problem, reason, confidence, mutate, validate, precondition)

    # -------------------------------------------------------------------------
    # Manual (human-in-the-loop) resolution tools
    #
    # These are deliberately NOT included in get_tools(), so the LLM never sees
    # or calls them. They exist only for a human reviewer to resolve a specific
    # `remaining_issues` entry that the agent correctly refused to touch on its
    # own (e.g. an identifier-like column). Human intent is a fundamentally
    # different authority than an LLM guess, so these tools accept a value or
    # a row-removal decision that the automatic toolbox never would -- but
    # they still go through the same snapshot/validate/commit machinery as
    # every other operation; there is still no free-form code execution.
    # -------------------------------------------------------------------------
    def manual_fill_missing(
        self, column: str, value: Any, problem: str = "missing_values",
        reason: str = "User-provided value applied manually.", confidence: float = 1.0,
    ) -> Dict[str, Any]:
        """Fills missing values in `column` with an explicit value chosen by a human reviewer."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}

        def precondition() -> Tuple[bool, str]:
            if int(self.df[column].isna().sum()) == 0:
                return False, f"Column '{column}' has no missing values."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            after_df = before_df.copy()
            series = before_df[column]
            missing_mask = series.isna()
            after_df[column] = series.fillna(value)
            tag_col = f"{column}_manually_filled"
            after_df[tag_col] = missing_mask
            return after_df, {
                "rows_affected": int(missing_mask.sum()),
                "fill_value": _to_native(value),
                "expected_filled_count": int(missing_mask.sum()),
            }

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_impute_missing(before_df[column], after_df[column], extra["expected_filled_count"])

        return self._apply(
            "manual_fill_missing", column, problem, reason, confidence, mutate, validate, precondition,
            resolved_by="user",
        )

    def generate_synthetic_emails(
        self, column: str, prefix: str = "client", domain: str = "example.invalid", start: int = 1,
    ) -> Dict[str, Any]:
        """Fill missing emails with collision-free numbered placeholders, tagged for audit."""
        import re
        if column not in self.df.columns or not re.fullmatch(r"[A-Za-z0-9._+-]+", prefix):
            return {"kept": False, "reason": "Colonne ou préfixe invalide."}
        if (not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", domain)
                or not isinstance(start, int) or isinstance(start, bool) or start < 1):
            return {"kept": False, "reason": "Domaine invalide ou numéro initial inférieur à 1."}
        missing = self.df[column].isna()
        if not missing.any():
            return {"kept": False, "reason": "Aucune valeur manquante."}

        def mutate(before_df):
            after_df = before_df.copy()
            values = before_df[column].astype(object).copy()
            used = {str(v).casefold() for v in values.dropna()}
            number = start
            for position in np.flatnonzero(missing.to_numpy()):
                candidate = f"{prefix}{number}@{domain}"
                while candidate.casefold() in used:
                    number += 1
                    candidate = f"{prefix}{number}@{domain}"
                values.iloc[position] = candidate
                used.add(candidate.casefold())
                number += 1
            after_df[column] = values
            tag = f"{column}_synthetic"
            while tag in before_df.columns:
                tag += "_audit"
            after_df[tag] = missing
            return after_df, {"rows_affected": int(missing.sum()), "audit_column": tag,
                              "synthetic": True, "prefix": prefix, "domain": domain}

        def validate(before_df, after_df, extra):
            generated = after_df.loc[missing, column].astype(str).str.casefold()
            existing = set(before_df.loc[~missing, column].astype(str).str.casefold())
            passed = (not after_df[column].isna().any() and generated.is_unique
                      and not set(generated).intersection(existing)
                      and before_df.loc[~missing, column].astype(str).equals(after_df.loc[~missing, column].astype(str)))
            return {"passed": bool(passed), "metric": "unique_synthetic_emails",
                    "before": int(missing.sum()), "after": int(after_df[column].isna().sum()),
                    "details": "E-mails fictifs uniques ; valeurs existantes conservées et cellules générées marquées."}

        return self._apply("generate_synthetic_emails", column, "missing_values",
                           "E-mails fictifs séquentiels demandés par l’utilisateur ; ne pas utiliser pour contacter des clients.",
                           1.0, mutate, validate, resolved_by="user")

    def replace_exact_with_synthetic_emails(
        self, column: str, match_value: str, prefix: str = "client",
        domain: str = "example.invalid", start: int = 1,
    ) -> Dict[str, Any]:
        """Replace an exact placeholder string with unique synthetic emails and an audit flag."""
        import re
        if column not in self.df.columns or not isinstance(match_value, str) or not match_value:
            return {"kept": False, "reason": "Colonne ou valeur recherchée invalide."}
        if (not re.fullmatch(r"[A-Za-z0-9._+-]+", prefix)
                or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", domain)
                or not isinstance(start, int) or isinstance(start, bool) or start < 1):
            return {"kept": False, "reason": "Paramètres de génération invalides."}
        mask = self.df[column].notna() & self.df[column].astype(str).eq(match_value)
        if not mask.any():
            return {"kept": False, "reason": f"Aucune occurrence exacte de {match_value!r}."}

        def mutate(before_df):
            after_df = before_df.copy()
            values = before_df[column].astype(object).copy()
            used = {str(v).casefold() for v in values.loc[~mask].dropna()}
            number = start
            for position in np.flatnonzero(mask.to_numpy()):
                candidate = f"{prefix}{number}@{domain}"
                while candidate.casefold() in used:
                    number += 1
                    candidate = f"{prefix}{number}@{domain}"
                values.iloc[position] = candidate
                used.add(candidate.casefold())
                number += 1
            after_df[column] = values
            tag = f"{column}_synthetic"
            while tag in before_df.columns:
                tag += "_audit"
            after_df[tag] = mask
            return after_df, {"rows_affected": int(mask.sum()), "audit_column": tag,
                              "match_value": match_value, "prefix": prefix, "domain": domain,
                              "synthetic": True}

        def validate(before_df, after_df, extra):
            generated = after_df.loc[mask, column].astype(str).str.casefold()
            existing = set(before_df.loc[~mask, column].dropna().astype(str).str.casefold())
            passed = (generated.is_unique and not set(generated).intersection(existing)
                      and not after_df[column].astype(str).eq(match_value).any()
                      and before_df.loc[~mask, column].astype(str).equals(after_df.loc[~mask, column].astype(str)))
            return {"passed": bool(passed), "metric": "exact_placeholder_count",
                    "before": int(mask.sum()), "after": int(after_df[column].astype(str).eq(match_value).sum()),
                    "details": "Valeur exacte remplacée par des e-mails fictifs uniques ; cellules marquées."}

        return self._apply("replace_exact_with_synthetic_emails", column, "inconsistent_formats",
                           "Placeholder textuel remplacé par des e-mails fictifs uniques à la demande de l’utilisateur.",
                           1.0, mutate, validate, resolved_by="user")

    def manual_drop_rows_with_missing(
        self, column: str, problem: str = "missing_values",
        reason: str = "User chose to remove rows missing this value.", confidence: float = 1.0,
    ) -> Dict[str, Any]:
        """Drops every row where `column` is missing, per an explicit human reviewer decision."""
        if column not in self.df.columns:
            return {"kept": False, "outcome": "error", "reason": f"Column '{column}' not found."}

        def precondition() -> Tuple[bool, str]:
            if int(self.df[column].isna().sum()) == 0:
                return False, f"Column '{column}' has no missing values; nothing to drop."
            return True, ""

        def mutate(before_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
            missing_mask = before_df[column].isna()
            after_df = before_df.loc[~missing_mask].reset_index(drop=True)
            return after_df, {"rows_affected": int(missing_mask.sum())}

        def validate(before_df: pd.DataFrame, after_df: pd.DataFrame, extra: Dict[str, Any]) -> Dict[str, Any]:
            return V.validate_manual_row_removal(before_df, after_df, column)

        return self._apply(
            "manual_drop_rows_with_missing", column, problem, reason, confidence, mutate, validate, precondition,
            resolved_by="user",
        )
