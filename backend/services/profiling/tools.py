"""
Deterministic Python/Pandas Analysis Tools for the AI Profiling Agent.

Provides the ProfilingToolkit class containing 12 read-only tools that execute
factual calculations on a DataFrame and return structured observations.
"""

from __future__ import annotations

import ipaddress
import re
import math
from typing import Any, Dict, List, Optional, Union, Callable
import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype, is_integer_dtype


def _is_boolean_like_series(series: pd.Series) -> bool:
    if is_bool_dtype(series):
        return True

    non_null = series.dropna()
    if len(non_null) == 0:
        return False

    allowed = {"true", "false", "yes", "no", "y", "n", "0", "1", "t", "f"}
    for v in non_null.tolist():
        if isinstance(v, (bool, np.bool_)):
            continue
        if isinstance(v, (int, np.integer, float, np.floating)):
            if float(v) in (0.0, 1.0):
                continue
            return False
        if isinstance(v, str):
            if v.strip().lower() in allowed:
                continue
            return False
        return False
    return True


def _is_numeric_non_bool(series: pd.Series) -> bool:
    return bool(is_numeric_dtype(series) and not is_bool_dtype(series))


def _clean_val(v: Any) -> Any:
    """Recursively converts NaN, NaT, Infinity, and NumPy types into JSON-safe Python types."""
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        if np.isnan(v) or math.isnan(v):
            return None
        if np.isinf(v) or math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return float(round(v, 4))
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        try:
            return pd.to_datetime(v).isoformat()
        except Exception:
            return str(v)
    if isinstance(v, (pd.Timedelta, np.timedelta64)):
        return str(v)
    if pd.isna(v):
        return None
    if isinstance(v, (list, tuple, set)):
        return [_clean_val(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _clean_val(val) for k, val in v.items()}
    return str(v)


def _profile_boolean_series(series: pd.Series, column_name: str) -> Dict[str, Any]:
    total_rows = int(len(series))
    missing_count = int(series.isna().sum())
    non_null = series.dropna()

    def to_bool_like(x: Any) -> Optional[bool]:
        if isinstance(x, (bool, np.bool_)):
            return bool(x)
        if isinstance(x, (int, np.integer, float, np.floating)) and not pd.isna(x):
            if float(x) == 1.0:
                return True
            if float(x) == 0.0:
                return False
        if isinstance(x, str):
            t = x.strip().lower()
            if t in {"true", "t", "yes", "y", "1"}:
                return True
            if t in {"false", "f", "no", "n", "0"}:
                return False
        return None

    mapped = non_null.map(to_bool_like)
    valid_bool = mapped.dropna()
    valid_count = int(len(valid_bool))

    true_count = int((valid_bool == True).sum())
    false_count = int((valid_bool == False).sum())

    return {
        "column_name": column_name,
        "type": "boolean",
        "total_rows": total_rows,
        "missing_count": missing_count,
        "missing_percentage": round((missing_count / total_rows * 100), 2) if total_rows > 0 else 0.0,
        "valid_boolean_count": valid_count,
        "true_count": true_count,
        "false_count": false_count,
        "true_percentage": round((true_count / valid_count * 100), 2) if valid_count > 0 else 0.0,
        "false_percentage": round((false_count / valid_count * 100), 2) if valid_count > 0 else 0.0,
        "unique_values": [_clean_val(v) for v in non_null.unique().tolist()],
    }


class ProfilingToolkit:
    """
    Toolkit providing 12 read-only analysis tools bound to a specific pandas DataFrame.
    """

    def __init__(self, df: pd.DataFrame):
        # Work on a copy or read-only view
        self.df = df.copy()

    def get_tools(self) -> List[Callable]:
        """Returns the list of all 12 bound tool functions for the agent."""
        return [
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
        ]

    # -------------------------------------------------------------------------
    # 1. Dataset Overview Tool
    # -------------------------------------------------------------------------
    def get_dataset_overview(self) -> Dict[str, Any]:
        """
        Returns high-level structural metrics and technical information about the entire dataset.
        Includes row count, column count, column names, pandas dtypes, memory usage,
        total duplicate rows, and global missing values count.
        """
        df = self.df
        total_rows = int(len(df))
        total_cols = int(len(df.columns))
        total_cells = total_rows * total_cols
        total_missing = int(df.isna().sum().sum())
        missing_pct = round((total_missing / total_cells * 100), 2) if total_cells > 0 else 0.0

        # Memory usage
        try:
            memory_bytes = int(df.memory_usage(deep=True).sum())
            memory_kb = round(memory_bytes / 1024, 2)
        except Exception:
            memory_kb = 0.0

        # Duplicates
        duplicate_rows = int(df.duplicated().sum())

        # Column dtypes
        dtypes_map = {str(col): str(dtype) for col, dtype in df.dtypes.items()}

        return {
            "rows": total_rows,
            "columns_count": total_cols,
            "column_names": [str(c) for c in df.columns],
            "dtypes": dtypes_map,
            "memory_usage_kb": memory_kb,
            "duplicate_rows": duplicate_rows,
            "duplicate_percentage": round((duplicate_rows / total_rows * 100), 2) if total_rows > 0 else 0.0,
            "total_missing_cells": total_missing,
            "missing_percentage": missing_pct,
        }

    # -------------------------------------------------------------------------
    # 2. Sample Rows Tool
    # -------------------------------------------------------------------------
    def get_sample_rows(self, n: int = 10) -> Dict[str, Any]:
        """
        Returns up to n representative sample rows from the dataset.
        Safely serializes NaN, NaT, Infinity, Timestamp, and numpy types.
        """
        df = self.df
        if len(df) == 0:
            return {"sample_count": 0, "rows": []}

        sample_size = min(max(1, int(n)), len(df))
        sampled_df = df.head(sample_size)

        rows = []
        for record in sampled_df.to_dict(orient="records"):
            rows.append({str(k): _clean_val(v) for k, v in record.items()})

        return {
            "sample_count": len(rows),
            "rows": rows,
        }

    # -------------------------------------------------------------------------
    # 3. Column Inspection Tool
    # -------------------------------------------------------------------------
    def inspect_column(self, column_name: str) -> Dict[str, Any]:
        """
        Inspects an individual column and returns its dtype, missing count, missing percentage,
        cardinality, sample values, and top frequent values.
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found in dataset."}

        series = self.df[column_name]
        total_count = len(series)
        missing_count = int(series.isna().sum())
        missing_pct = round((missing_count / total_count * 100), 2) if total_count > 0 else 0.0

        non_null = series.dropna()
        unique_count = int(non_null.nunique())
        unique_pct = round((unique_count / len(non_null) * 100), 2) if len(non_null) > 0 else 0.0

        # Sample values
        sample_values = [_clean_val(v) for v in non_null.head(8).tolist()]

        # Top frequent values
        val_counts = non_null.value_counts().head(5)
        top_frequent = [
            {
                "value": _clean_val(val),
                "count": int(count),
                "percentage": round((count / len(non_null) * 100), 2) if len(non_null) > 0 else 0.0,
            }
            for val, count in val_counts.items()
        ]

        result = {
            "column_name": column_name,
            "dtype": str(series.dtype),
            "total_rows": total_count,
            "missing_count": missing_count,
            "missing_percentage": missing_pct,
            "unique_count": unique_count,
            "unique_percentage": unique_pct,
            "sample_values": sample_values,
            "top_frequent_values": top_frequent,
            "is_boolean": _is_boolean_like_series(series),
            "is_numeric": _is_numeric_non_bool(series),
            "is_datetime": bool(pd.api.types.is_datetime64_any_dtype(series)),
        }

        # Min / Max when applicable (numeric non-bool only)
        if _is_numeric_non_bool(series) and len(non_null) > 0:
            result["min"] = _clean_val(non_null.min())
            result["max"] = _clean_val(non_null.max())
        elif _is_boolean_like_series(series):
            bool_profile = _profile_boolean_series(series, column_name)
            result.update({
                "true_count": bool_profile["true_count"],
                "false_count": bool_profile["false_count"],
                "true_percentage": bool_profile["true_percentage"],
                "false_percentage": bool_profile["false_percentage"],
            })
        elif pd.api.types.is_string_dtype(series) or series.dtype == object:
            str_series = non_null.astype(str)
            lengths = str_series.str.len()
            result["string_length_stats"] = {
                "min_length": int(lengths.min()) if len(lengths) > 0 else 0,
                "max_length": int(lengths.max()) if len(lengths) > 0 else 0,
                "avg_length": round(float(lengths.mean()), 2) if len(lengths) > 0 else 0.0,
            }

        return result

    # -------------------------------------------------------------------------
    # 4. Numeric Analysis Tool
    # -------------------------------------------------------------------------
    def analyze_numeric_column(self, column_name: str) -> Dict[str, Any]:
        """
        Performs thorough statistical analysis on a numeric non-boolean column.
        Returns min, max, mean, median, standard deviation, Q1, Q2, Q3, IQR,
        zeros count, negative values count, percentiles, and outlier counts.
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found."}

        raw = self.df[column_name]
        if _is_boolean_like_series(raw):
            return {
                "column_name": column_name,
                "error": "Boolean column should not be processed as numeric.",
                "boolean_profile": _profile_boolean_series(raw, column_name),
            }

        if not _is_numeric_non_bool(raw):
            return {
                "column_name": column_name,
                "error": "Column is not numeric.",
            }

        total_rows = int(len(raw))
        missing_count = int(raw.isna().sum())
        series = pd.to_numeric(raw, errors="coerce").dropna()
        if len(series) == 0:
            return {
                "column_name": column_name,
                "error": "Column contains no valid numeric values.",
            }

        q1 = float(series.quantile(0.25))
        q2 = float(series.quantile(0.50))
        q3 = float(series.quantile(0.75))
        iqr = float(q3 - q1)

        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        outliers_series = series[(series < lower_bound) | (series > upper_bound)]

        zeros_count = int((series == 0).sum())
        neg_count = int((series < 0).sum())

        # Distribution skewness
        skew = float(series.skew()) if len(series) > 2 else 0.0

        return {
            "column_name": column_name,
            "count": int(len(series)),
            "missing_count": missing_count,
            "missing_percentage": round((missing_count / total_rows * 100), 2) if total_rows > 0 else 0.0,
            "min": _clean_val(series.min()),
            "max": _clean_val(series.max()),
            "mean": _clean_val(series.mean()),
            "median": _clean_val(q2),
            "std": _clean_val(series.std() if len(series) > 1 else 0.0),
            "q1": _clean_val(q1),
            "q2_median": _clean_val(q2),
            "q3": _clean_val(q3),
            "iqr": _clean_val(iqr),
            "zeros_count": zeros_count,
            "zeros_percentage": round((zeros_count / len(series) * 100), 2),
            "negative_count": neg_count,
            "negative_percentage": round((neg_count / len(series) * 100), 2),
            "outliers_count": int(len(outliers_series)),
            "outliers_percentage": round((len(outliers_series) / len(series) * 100), 2),
            "percentiles": {
                "p1": _clean_val(series.quantile(0.01)),
                "p5": _clean_val(series.quantile(0.05)),
                "p25": _clean_val(q1),
                "p50": _clean_val(q2),
                "p75": _clean_val(q3),
                "p95": _clean_val(series.quantile(0.95)),
                "p99": _clean_val(series.quantile(0.99)),
            },
            "skewness": _clean_val(skew),
        }

    # -------------------------------------------------------------------------
    # 5. Text Analysis Tool
    # -------------------------------------------------------------------------
    def analyze_text_column(self, column_name: str) -> Dict[str, Any]:
        """
        Analyzes a textual or string column for length statistics, empty strings,
        whitespace-only strings, character types, and cardinality.
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found."}

        series = self.df[column_name].dropna().astype(str)
        if len(series) == 0:
            return {"column_name": column_name, "error": "No non-null text values."}

        lengths = series.str.len()
        empty_count = int((series == "").sum())
        whitespace_count = int((series.str.strip() == "").sum() - empty_count)

        # Character set structure
        digits_only = int(series.str.isdigit().sum())
        alpha_only = int(series.str.isalpha().sum())
        alnum_only = int(series.str.isalnum().sum())

        # Rare values (frequency == 1)
        val_counts = series.value_counts()
        rare_count = int((val_counts == 1).sum())
        rare_samples = [_clean_val(v) for v in val_counts[val_counts == 1].head(5).index]

        return {
            "column_name": column_name,
            "total_text_rows": len(series),
            "unique_count": int(series.nunique()),
            "cardinality_ratio": round(series.nunique() / len(series), 4),
            "empty_strings_count": empty_count,
            "whitespace_only_count": max(0, whitespace_count),
            "min_length": int(lengths.min()),
            "max_length": int(lengths.max()),
            "avg_length": round(float(lengths.mean()), 2),
            "median_length": float(lengths.median()),
            "digits_only_percentage": round(digits_only / len(series) * 100, 2),
            "alpha_only_percentage": round(alpha_only / len(series) * 100, 2),
            "alnum_only_percentage": round(alnum_only / len(series) * 100, 2),
            "rare_values_count": rare_count,
            "rare_sample_values": rare_samples,
        }

    # -------------------------------------------------------------------------
    # 6. Categorical Analysis Tool
    # -------------------------------------------------------------------------
    def analyze_categorical_column(self, column_name: str) -> Dict[str, Any]:
        """
        Analyzes a categorical column, reporting top categories, rare categories,
        dominant percentage, and possible inconsistent representations (casing, whitespace, synonyms).
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found."}

        series = self.df[column_name].dropna().astype(str)
        if len(series) == 0:
            return {"column_name": column_name, "error": "No non-null values."}

        val_counts = series.value_counts()
        nunique = int(len(val_counts))
        dominant_val = str(val_counts.index[0]) if nunique > 0 else ""
        dominant_count = int(val_counts.iloc[0]) if nunique > 0 else 0
        dominant_pct = round(dominant_count / len(series) * 100, 2) if len(series) > 0 else 0.0

        # Top 10 categories
        top_categories = [
            {
                "category": _clean_val(cat),
                "count": int(cnt),
                "percentage": round(cnt / len(series) * 100, 2),
            }
            for cat, cnt in val_counts.head(10).items()
        ]

        # Rare categories (count < 2 or < 1%)
        rare_cats = [
            {"category": _clean_val(cat), "count": int(cnt)}
            for cat, cnt in val_counts.items()
            if cnt == 1 or (cnt / len(series) < 0.01)
        ][:10]

        # Detect inconsistent representations (case variations, trailing whitespace, acronyms/synonyms)
        cleaned_map: Dict[str, List[str]] = {}
        for original in val_counts.index:
            normalized = original.strip().lower()
            if normalized not in cleaned_map:
                cleaned_map[normalized] = []
            cleaned_map[normalized].append(original)

        inconsistent_groups = []
        # 1. Exact lowercase / whitespace variants
        for norm_key, variants in cleaned_map.items():
            if len(variants) > 1:
                inconsistent_groups.append({
                    "normalized_concept": norm_key,
                    "observed_variants": variants,
                    "counts": [int(val_counts[v]) for v in variants],
                })

        # 2. Known synonym / acronym groups (e.g. USA, US, United States)
        known_synonym_clusters = [
            {"usa", "us", "united states", "u.s.", "u.s.a."},
            {"uk", "united kingdom", "great britain", "gb"},
            {"uae", "united arab emirates"},
        ]
        present_categories_lower = {cat.strip().lower(): cat for cat in val_counts.index}
        for cluster in known_synonym_clusters:
            matches = [present_categories_lower[c] for c in cluster if c in present_categories_lower]
            if len(matches) > 1:
                inconsistent_groups.append({
                    "normalized_concept": "/".join(sorted(cluster)),
                    "observed_variants": matches,
                    "counts": [int(val_counts[m]) for m in matches],
                })

        return {
            "column_name": column_name,
            "total_count": len(series),
            "cardinality": nunique,
            "dominant_category": dominant_val,
            "dominant_percentage": dominant_pct,
            "top_categories": top_categories,
            "rare_categories_sample": rare_cats,
            "inconsistent_representations": inconsistent_groups[:10],
        }

    # -------------------------------------------------------------------------
    # 7. Date Analysis Tool
    # -------------------------------------------------------------------------
    def analyze_date_column(self, column_name: str) -> Dict[str, Any]:
        """
        Analyzes a column for temporal characteristics without modifying the original DataFrame.
        Detects valid dates, invalid dates, date ranges, future dates, and suspicious epochs.
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found."}

        raw = self.df[column_name]
        total_rows = int(len(raw))
        missing_count = int(raw.isna().sum())

        series = raw.dropna()
        if len(series) == 0:
            return {"column_name": column_name, "error": "No non-null values."}

        str_series = series.astype(str)
        sample_size = min(300, len(str_series))
        sample = str_series.head(sample_size)

        # Attempt date parsing
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            parsed = pd.to_datetime(sample, errors="coerce")
            valid_dates = parsed.dropna()
            valid_count = int(len(valid_dates))
            valid_rate = round(valid_count / sample_size, 4) if sample_size > 0 else 0.0

            if valid_rate < 0.2:
                return {
                    "column_name": column_name,
                    "appears_date_like": False,
                    "missing_count": missing_count,
                    "valid_date_rate_in_sample": valid_rate,
                    "reason": "Less than 20% of sampled values could be parsed as dates.",
                }

            full_parsed_raw = pd.to_datetime(str_series, errors="coerce")
            full_parsed = full_parsed_raw.dropna()
            min_date = str(full_parsed.min()) if len(full_parsed) > 0 else None
            max_date = str(full_parsed.max()) if len(full_parsed) > 0 else None

        now = pd.Timestamp.now()
        future_dates = int((full_parsed > now).sum())
        future_pct = round((future_dates / len(full_parsed) * 100), 2) if len(full_parsed) > 0 else 0.0
        epoch_dates = int((full_parsed.dt.year == 1970).sum())
        pre_1900 = int((full_parsed.dt.year < 1900).sum())
        far_future = int((full_parsed > (now + pd.DateOffset(years=10))).sum())

        suspicious_dates = []
        if epoch_dates > 0:
            suspicious_dates.append(f"{epoch_dates} dates at Unix Epoch (1970)")
        if pre_1900 > 0:
            suspicious_dates.append(f"{pre_1900} very old dates before year 1900")
        if far_future > 0:
            suspicious_dates.append(f"{far_future} dates far in the future (>10 years ahead)")
        if future_dates > 0:
            suspicious_dates.append(f"{future_dates} dates in the future")

        return {
            "column_name": column_name,
            "appears_date_like": True,
            "total_tested": len(str_series),
            "missing_count": missing_count,
            "valid_date_count": int(len(full_parsed)),
            "invalid_date_count": int(len(str_series) - len(full_parsed)),
            "min_date": min_date,
            "max_date": max_date,
            "future_dates_count": future_dates,
            "future_percentage": future_pct,
            "epoch_dates_count": epoch_dates,
            "very_old_dates_count": pre_1900,
            "far_future_dates_count": far_future,
            "suspicious_observations": suspicious_dates,
        }

    # -------------------------------------------------------------------------
    # 8. Pattern Detection Tool
    # -------------------------------------------------------------------------
    def detect_patterns(self, column_name: str) -> Dict[str, Any]:
        """
        Deterministic pattern profiling with validity counts.
        Patterns: email, phone, ipv4, ipv6, url, uuid, postal code, date-like, percentage,
        currency symbol, alphanumeric code.
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found."}

        raw = self.df[column_name]
        total_rows = int(len(raw))
        missing_count = int(raw.isna().sum())
        series = raw.dropna().astype(str).str.strip()
        if len(series) == 0:
            return {"column_name": column_name, "detected_patterns": [], "pattern_profiles": []}

        def _safe_ipv4(x: str) -> bool:
            try:
                return isinstance(ipaddress.ip_address(x), ipaddress.IPv4Address)
            except Exception:
                return False

        def _safe_ipv6(x: str) -> bool:
            try:
                return isinstance(ipaddress.ip_address(x), ipaddress.IPv6Address)
            except Exception:
                return False

        def _date_like(x: str) -> bool:
            try:
                return pd.notna(pd.to_datetime(x, errors="coerce"))
            except Exception:
                return False

        regex_patterns = {
            "EMAIL": re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"),
            "URL": re.compile(r"^(https?://|www\.)[^\s/$.?#].[^\s]*$", re.IGNORECASE),
            "PHONE": re.compile(r"^[\+]?\d[\d\s\-\.\(\)]{6,19}$"),
            "UUID": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),
            "POSTAL_CODE": re.compile(r"^(\d{5}(-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)$", re.IGNORECASE),
            "PERCENTAGE": re.compile(r"^-?\d+\.?\d*\s*%$"),
            "CURRENCY_SYMBOL": re.compile(r"^[\$€£¥₹]\s*-?\d"),
            "ALPHANUMERIC_CODE": re.compile(r"^[A-Z0-9\-_]{2,24}$", re.IGNORECASE),
        }

        custom_validators = {
            "IPV4": _safe_ipv4,
            "IPV6": _safe_ipv6,
            "DATE_LIKE": _date_like,
        }

        pattern_profiles = []
        detected = []
        non_missing_count = int(len(series))

        for pattern_name, regex in regex_patterns.items():
            valid_mask = series.map(lambda v: bool(regex.match(v)))
            valid_count = int(valid_mask.sum())
            invalid_count = int(non_missing_count - valid_count)
            valid_pct = round((valid_count / non_missing_count * 100), 2) if non_missing_count > 0 else 0.0

            profile = {
                "pattern": pattern_name,
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "missing_count": missing_count,
                "valid_percentage": valid_pct,
            }
            pattern_profiles.append(profile)

            if valid_pct >= 15.0:
                sample_matches = series[valid_mask].head(5).tolist()
                detected.append({
                    "pattern": pattern_name,
                    "match_rate": round(valid_count / non_missing_count, 4) if non_missing_count > 0 else 0.0,
                    "match_percentage": valid_pct,
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "missing_count": missing_count,
                    "valid_percentage": valid_pct,
                    "sample_matches": [_clean_val(m) for m in sample_matches],
                })

        for pattern_name, validator in custom_validators.items():
            valid_mask = series.map(lambda v: bool(validator(v)))
            valid_count = int(valid_mask.sum())
            invalid_count = int(non_missing_count - valid_count)
            valid_pct = round((valid_count / non_missing_count * 100), 2) if non_missing_count > 0 else 0.0

            profile = {
                "pattern": pattern_name,
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "missing_count": missing_count,
                "valid_percentage": valid_pct,
            }
            pattern_profiles.append(profile)

            if valid_pct >= 15.0:
                sample_matches = series[valid_mask].head(5).tolist()
                detected.append({
                    "pattern": pattern_name,
                    "match_rate": round(valid_count / non_missing_count, 4) if non_missing_count > 0 else 0.0,
                    "match_percentage": valid_pct,
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "missing_count": missing_count,
                    "valid_percentage": valid_pct,
                    "sample_matches": [_clean_val(m) for m in sample_matches],
                })

        return {
            "column_name": column_name,
            "sample_size": non_missing_count,
            "detected_patterns": detected,
            "pattern_profiles": pattern_profiles,
        }

    # -------------------------------------------------------------------------
    # 9. Missing-Value Analysis Tool
    # -------------------------------------------------------------------------
    def detect_missing_values(self) -> Dict[str, Any]:
        """
        Analyzes missingness across all columns and rows in the dataset.
        Distinguishes hard-missing values (NaN/None) from semantic-missing suspects
        (empty strings, whitespace-only strings, placeholders) for text-like columns.
        """
        df = self.df
        total_rows = len(df)
        if total_rows == 0:
            return {"error": "Empty dataset."}

        placeholder_tokens = {
            "n/a", "na", "null", "none", "unknown", "-", "?", "not available", "not-an-email"
        }

        missing_per_col = df.isna().sum()
        missing_stats = {}
        high_missing = []
        empty_cols = []
        semantic_missing_columns = []

        for col in df.columns:
            series = df[col]
            cnt_int = int(missing_per_col[col])
            pct = round((cnt_int / total_rows * 100), 2)
            non_missing_count = int(total_rows - cnt_int)

            details = {
                "missing_count": cnt_int,
                "missing_percentage": pct,
                "non_missing_count": non_missing_count,
            }

            if series.dtype == object or pd.api.types.is_string_dtype(series):
                str_vals = series.dropna().astype(str)
                empty_count = int((str_vals == "").sum())
                whitespace_count = int((str_vals.str.strip() == "").sum() - empty_count)
                normalized = str_vals.str.strip().str.lower()
                placeholder_count = int(normalized.isin(placeholder_tokens).sum())

                details.update({
                    "empty_string_count": empty_count,
                    "whitespace_only_count": max(0, whitespace_count),
                    "placeholder_count": placeholder_count,
                    "semantic_missing_suspects": int(empty_count + max(0, whitespace_count) + placeholder_count),
                })

                suspect_ratio = (details["semantic_missing_suspects"] / total_rows) if total_rows > 0 else 0.0
                if suspect_ratio >= 0.05:
                    semantic_missing_columns.append({
                        "column": str(col),
                        "semantic_missing_suspects": details["semantic_missing_suspects"],
                        "suspect_percentage": round(suspect_ratio * 100, 2),
                    })

            missing_stats[str(col)] = details

            if pct == 100.0:
                empty_cols.append(str(col))
            elif pct >= 20.0:
                high_missing.append({
                    "column": str(col),
                    "missing_count": cnt_int,
                    "missing_percentage": pct,
                })

        # Rows with >50% null values
        row_null_counts = df.isna().sum(axis=1)
        severe_null_rows = int((row_null_counts >= (len(df.columns) * 0.5)).sum())

        return {
            "total_rows": total_rows,
            "total_missing_cells": int(missing_per_col.sum()),
            "missing_by_column": missing_stats,
            "completely_empty_columns": empty_cols,
            "high_missingness_columns": high_missing,
            "semantic_missing_columns": semantic_missing_columns,
            "rows_with_over_50pct_nulls": severe_null_rows,
        }

    # -------------------------------------------------------------------------
    # 10. Duplicate Analysis Tool
    # -------------------------------------------------------------------------
    def detect_duplicates(self) -> Dict[str, Any]:
        """
        Analyzes duplicate rows and detects potential key-like columns.
        """
        df = self.df
        total_rows = len(df)
        if total_rows == 0:
            return {"error": "Empty dataset."}

        duplicate_mask = df.duplicated()
        duplicate_count = int(duplicate_mask.sum())
        duplicate_pct = round((duplicate_count / total_rows * 100), 2)

        # Strict unique key candidates (legacy behavior compatibility)
        unique_key_candidates = []
        candidate_key_columns = []
        duplicate_candidate_key_values = []

        for col in df.columns:
            series = df[col]
            non_null_unique = int(series.dropna().nunique())
            missing_count = int(series.isna().sum())
            unique_ratio = (non_null_unique / total_rows) if total_rows > 0 else 0.0

            if missing_count == 0 and non_null_unique == total_rows:
                unique_key_candidates.append(str(col))

            if unique_ratio >= 0.95 and missing_count <= max(1, int(total_rows * 0.01)):
                candidate_key_columns.append({
                    "column": str(col),
                    "unique_ratio": round(unique_ratio, 4),
                    "missing_count": missing_count,
                    "likely_primary_key": bool(unique_ratio == 1.0 and missing_count == 0 and re.search(r"id|code|key", str(col), re.IGNORECASE)),
                })

            # Capture repeated values for key-like columns that are not perfectly unique
            if unique_ratio >= 0.90 and unique_ratio < 1.0:
                vc = series.value_counts(dropna=True)
                repeats = vc[vc > 1].head(5)
                if len(repeats) > 0:
                    duplicate_candidate_key_values.append({
                        "column": str(col),
                        "sample_repeated_values": [
                            {"value": _clean_val(idx), "count": int(cnt)} for idx, cnt in repeats.items()
                        ],
                    })

        sample_duplicates = []
        if duplicate_count > 0:
            dup_records = df[duplicate_mask].head(3).to_dict(orient="records")
            sample_duplicates = [{str(k): _clean_val(v) for k, v in rec.items()} for rec in dup_records]

        return {
            "total_rows": total_rows,
            "total_duplicate_rows": duplicate_count,
            "full_row_duplicates": duplicate_count,
            "duplicate_percentage": duplicate_pct,
            "unique_key_candidates": unique_key_candidates,
            "candidate_key_columns": candidate_key_columns,
            "duplicate_candidate_key_values": duplicate_candidate_key_values,
            "sample_duplicate_rows": sample_duplicates,
        }

    # -------------------------------------------------------------------------
    # 11. Outlier Detection Tool
    # -------------------------------------------------------------------------
    def detect_outliers(self, column_name: str) -> Dict[str, Any]:
        """
        Calculates deterministic IQR-based outliers and extreme percentiles for a numeric non-boolean column.
        """
        if column_name not in self.df.columns:
            return {"error": f"Column '{column_name}' not found."}

        raw = self.df[column_name]
        if _is_boolean_like_series(raw):
            return {
                "column_name": column_name,
                "error": "Boolean column does not support numeric outlier analysis.",
                "total_outliers_count": 0,
                "outlier_percentage": 0.0,
                "boolean_profile": _profile_boolean_series(raw, column_name),
            }

        if not _is_numeric_non_bool(raw):
            return {"column_name": column_name, "error": "Column has no numeric values."}

        series = pd.to_numeric(raw, errors="coerce").dropna()
        if len(series) == 0:
            return {"column_name": column_name, "error": "Column has no numeric values."}

        q1 = float(series.quantile(0.25))
        q3 = float(series.quantile(0.75))
        iqr = q3 - q1
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr

        low_outliers = series[series < lower_bound]
        high_outliers = series[series > upper_bound]
        total_outliers = len(low_outliers) + len(high_outliers)

        return {
            "column_name": column_name,
            "total_numeric_values": len(series),
            "lower_bound": _clean_val(lower_bound),
            "upper_bound": _clean_val(upper_bound),
            "total_outliers_count": int(total_outliers),
            "outlier_percentage": round(total_outliers / len(series) * 100, 2),
            "low_outliers_count": int(len(low_outliers)),
            "high_outliers_count": int(len(high_outliers)),
            "sample_low_outliers": [_clean_val(x) for x in low_outliers.head(5).tolist()],
            "sample_high_outliers": [_clean_val(x) for x in high_outliers.head(5).tolist()],
            "p1": _clean_val(series.quantile(0.01)),
            "p99": _clean_val(series.quantile(0.99)),
        }

    # -------------------------------------------------------------------------
    # 12. Column Relationship Tool
    # -------------------------------------------------------------------------
    def compare_columns(self, column_a: str, column_b: str) -> Dict[str, Any]:
        """
        Compares two columns to evaluate correlation, value overlap (Jaccard similarity),
        identity, and potential dependencies.
        """
        if column_a not in self.df.columns or column_b not in self.df.columns:
            return {"error": f"One or both columns ('{column_a}', '{column_b}') not found."}

        col_a_series = self.df[column_a]
        col_b_series = self.df[column_b]

        # Check identity
        are_identical = bool(col_a_series.equals(col_b_series))

        # Value overlap
        set_a = set(col_a_series.dropna().astype(str).unique())
        set_b = set(col_b_series.dropna().astype(str).unique())
        intersection = set_a.intersection(set_b)
        union = set_a.union(set_b)
        jaccard = round(len(intersection) / len(union), 4) if len(union) > 0 else 0.0

        # Equality on aligned non-null rows
        aligned = self.df[[column_a, column_b]].dropna()
        exact_eq_ratio = None
        norm_eq_ratio = None
        if len(aligned) > 0:
            exact_eq_ratio = float((aligned[column_a].astype(str) == aligned[column_b].astype(str)).mean())
            norm_eq_ratio = float(
                (
                    aligned[column_a].astype(str).str.strip().str.lower()
                    == aligned[column_b].astype(str).str.strip().str.lower()
                ).mean()
            )

        unique_a = int(col_a_series.dropna().nunique())
        unique_b = int(col_b_series.dropna().nunique())
        total_rows = int(len(self.df))
        unique_ratio_a = round(unique_a / total_rows, 4) if total_rows > 0 else 0.0
        unique_ratio_b = round(unique_b / total_rows, 4) if total_rows > 0 else 0.0

        result: Dict[str, Any] = {
            "column_a": column_a,
            "column_b": column_b,
            "are_identical": are_identical,
            "jaccard_similarity": jaccard,
            "shared_unique_values_count": len(intersection),
            "exact_equality_ratio": _clean_val(exact_eq_ratio),
            "normalized_equality_ratio": _clean_val(norm_eq_ratio),
            "column_a_unique_ratio": unique_ratio_a,
            "column_b_unique_ratio": unique_ratio_b,
        }

        # Numeric correlation if both columns are numeric non-boolean
        if _is_numeric_non_bool(col_a_series) and _is_numeric_non_bool(col_b_series):
            valid_subset = self.df[[column_a, column_b]].dropna()
            if len(valid_subset) > 1:
                corr_pearson = valid_subset[column_a].corr(valid_subset[column_b], method="pearson")
                corr_spearman = valid_subset[column_a].corr(valid_subset[column_b], method="spearman")
                result["pearson_correlation"] = _clean_val(corr_pearson)
                result["spearman_correlation"] = _clean_val(corr_spearman)

        return result
