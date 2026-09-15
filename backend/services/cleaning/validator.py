"""
Deterministic, LLM-independent validation logic for the Cleaning Agent.

Every mutation tool in `tools.py` calls into this module to decide, from
measured before/after facts alone, whether a change should be KEPT or
ROLLED BACK. The LLM's confidence score is never part of this decision.

This module also owns the deterministic templating of the human-readable
`summary` field so that report text can never drift from what the
validated `actions` / `remaining_issues` lists actually contain.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from .schemas import ValidationResult


from .delta import validate_transformation


def _vr(passed: bool, metric: str, before: Any, after: Any, details: str) -> Dict[str, Any]:
    return ValidationResult(passed=passed, metric=metric, before=before, after=after, details=details).model_dump()


def make_issue_id(scope: str, column: Optional[str], type_: str) -> str:
    """Stable identifier for a remaining issue, used to address it in a resolution conversation."""
    return f"{scope}:{column or 'dataset'}:{type_}"


# ---------------------------------------------------------------------------
# Tier 1 validators
# ---------------------------------------------------------------------------

def validate_drop_exact_duplicates(before_df: pd.DataFrame, after_df: pd.DataFrame) -> Dict[str, Any]:
    before_dups = int(before_df.duplicated().sum())
    after_dups = int(after_df.duplicated().sum())
    expected_rows = len(before_df) - before_dups
    passed = after_dups == 0 and len(after_df) == expected_rows
    return _vr(
        passed, "duplicate_rows", before_dups, after_dups,
        f"Row count went from {len(before_df)} to {len(after_df)}; duplicate rows went from {before_dups} to {after_dups}.",
    )


def validate_trim_whitespace(before_series: pd.Series, after_series: pd.Series) -> Dict[str, Any]:
    # NB: `.astype(str)` on a Series can leave NaN/None entries as float NaN
    # rather than the string "nan", and NaN != NaN is always True -- so nulls
    # must be dropped before comparing, or every null row falsely counts as
    # "untrimmed".
    before_non_null = before_series.dropna().astype(str)
    after_non_null = after_series.dropna().astype(str)
    before_untrimmed = int((before_non_null != before_non_null.str.strip()).sum())
    after_untrimmed = int((after_non_null != after_non_null.str.strip()).sum())
    non_null_before = int(before_series.notna().sum())
    non_null_after = int(after_series.notna().sum())
    same_content = bool(
        (before_series.dropna().astype(str).str.strip().reset_index(drop=True) ==
         after_series.dropna().astype(str).str.strip().reset_index(drop=True)).all()
    ) if non_null_before == non_null_after else False
    passed = after_untrimmed == 0 and non_null_before == non_null_after and same_content
    return _vr(
        passed, "untrimmed_value_count", before_untrimmed, after_untrimmed,
        f"Untrimmed values went from {before_untrimmed} to {after_untrimmed}; non-null count preserved={non_null_before == non_null_after}.",
    )


def validate_normalize_case(before_series: pd.Series, after_series: pd.Series) -> Dict[str, Any]:
    non_null_before = int(before_series.notna().sum())
    non_null_after = int(after_series.notna().sum())
    if non_null_before != non_null_after:
        return _vr(False, "case_normalized_lossless", non_null_before, non_null_after, "Non-null count changed unexpectedly.")
    lossless = bool(
        (before_series.dropna().astype(str).str.lower().reset_index(drop=True) ==
         after_series.dropna().astype(str).str.lower().reset_index(drop=True)).all()
    )
    unique_before = int(before_series.dropna().nunique())
    unique_after = int(after_series.dropna().nunique())
    passed = lossless and unique_after <= unique_before
    return _vr(
        passed, "unique_value_count", unique_before, unique_after,
        f"Unique values went from {unique_before} to {unique_after}; case-insensitive content preserved={lossless}.",
    )


def validate_canonicalize_categories(before_series: pd.Series, after_series: pd.Series) -> Dict[str, Any]:
    non_null_before = int(before_series.notna().sum())
    non_null_after = int(after_series.notna().sum())
    unique_before = int(before_series.dropna().nunique())
    unique_after = int(after_series.dropna().nunique())
    passed = non_null_before == non_null_after and unique_after <= unique_before
    return _vr(
        passed, "unique_category_count", unique_before, unique_after,
        f"Unique categories went from {unique_before} to {unique_after}; row/null count preserved={non_null_before == non_null_after}.",
    )


def validate_cast_column_type(before_series: pd.Series, after_series: pd.Series) -> Dict[str, Any]:
    missing_before = int(before_series.isna().sum())
    missing_after = int(after_series.isna().sum())
    passed = missing_after <= missing_before and len(before_series) == len(after_series)
    return _vr(
        passed, "missing_count", missing_before, missing_after,
        f"Missing values went from {missing_before} to {missing_after} after casting to '{after_series.dtype}'.",
    )


# ---------------------------------------------------------------------------
# Tier 2 validators
# ---------------------------------------------------------------------------

def validate_flag_domain_implausible(before_series: pd.Series, after_series: pd.Series) -> Dict[str, Any]:
    unchanged = bool((before_series.fillna("§NA§").astype(str) == after_series.fillna("§NA§").astype(str)).all())
    return _vr(
        unchanged, "underlying_values_unchanged", True, unchanged,
        "Flag-only operation: underlying column values must remain byte-identical." if unchanged else
        "FAILED: underlying values changed during a flag-only operation.",
    )


def validate_cap_outliers(before_series: pd.Series, after_series: pd.Series, lower_bound: float, upper_bound: float) -> Dict[str, Any]:
    numeric_after = pd.to_numeric(after_series, errors="coerce").dropna()
    within_bounds = bool(((numeric_after >= lower_bound - 1e-9) & (numeric_after <= upper_bound + 1e-9)).all())
    same_length = len(before_series) == len(after_series)
    same_non_null = int(before_series.notna().sum()) == int(after_series.notna().sum())
    passed = within_bounds and same_length and same_non_null
    return _vr(
        passed, "values_within_bounds", f"[{lower_bound}, {upper_bound}]", within_bounds,
        f"All capped values within bounds={within_bounds}; row count unchanged={same_length}; non-null count unchanged={same_non_null}.",
    )


def validate_impute_missing(before_series: pd.Series, after_series: pd.Series, expected_filled_count: int) -> Dict[str, Any]:
    missing_before = int(before_series.isna().sum())
    missing_after = int(after_series.isna().sum())
    filled = missing_before - missing_after
    passed = filled == expected_filled_count and missing_after == 0 and len(before_series) == len(after_series)
    return _vr(
        passed, "missing_count", missing_before, missing_after,
        f"Filled {filled} of {expected_filled_count} missing cells; remaining missing={missing_after}.",
    )


def validate_manual_row_removal(before_df: pd.DataFrame, after_df: pd.DataFrame, column: str) -> Dict[str, Any]:
    """Validates a human-directed 'drop rows missing this column' decision."""
    missing_before = int(before_df[column].isna().sum())
    missing_after_col = int(after_df[column].isna().sum()) if column in after_df.columns else None
    expected_rows = len(before_df) - missing_before
    passed = len(after_df) == expected_rows and missing_after_col == 0
    return _vr(
        passed, "rows_removed", missing_before, len(before_df) - len(after_df),
        f"Removed {len(before_df) - len(after_df)} rows missing '{column}' (expected {missing_before}); "
        f"remaining missing in '{column}'={missing_after_col}.",
    )


def no_correction_applicable(metric: str, before: Any, details: str) -> Dict[str, Any]:
    """Deterministic 'no-op' validation result: not a failure, just insufficient evidence to act."""
    return _vr(False, metric, before, before, details)


def validate_date_offset_correction(before_series: pd.Series, after_series: pd.Series, anomalies_before: int, anomalies_after: int) -> Dict[str, Any]:
    same_length = len(before_series) == len(after_series)
    improved = anomalies_after < anomalies_before
    passed = improved and same_length
    return _vr(
        passed, "temporal_anomaly_count", anomalies_before, anomalies_after,
        f"Temporal anomalies went from {anomalies_before} to {anomalies_after}.",
    )


def validate_flag_temporal_anomaly(before_series: pd.Series, after_series: pd.Series) -> Dict[str, Any]:
    unchanged = bool((before_series.fillna("§NA§").astype(str) == after_series.fillna("§NA§").astype(str)).all())
    return _vr(
        unchanged, "underlying_values_unchanged", True, unchanged,
        "Flag-only operation: underlying date values must remain unchanged." if unchanged else
        "FAILED: underlying values changed during a flag-only operation.",
    )


# ---------------------------------------------------------------------------
# Deterministic summary templating (never a second Gemini pass)
# ---------------------------------------------------------------------------

_PLAIN_LANGUAGE_ACTION_LABELS: Dict[str, str] = {
    "drop_exact_duplicates": "suppression des doublons exacts",
    "trim_whitespace": "suppression des espaces superflus",
    "normalize_case": "uniformisation de la casse",
    "canonicalize_categories": "regroupement des variantes de catégories",
    "cast_column_type": "conversion du type d’une colonne",
    "flag_domain_implausible": "signalement des valeurs suspectes (sans correction)",
    "cap_outliers": "plafonnement des valeurs extrêmes",
    "impute_missing": "remplacement des valeurs manquantes",
    "manual_fill_missing": "remplacement demandé par l’utilisateur",
    "manual_drop_rows_with_missing": "suppression des lignes demandée par l’utilisateur",
    "generate_synthetic_emails": "génération d’e-mails fictifs uniques (cellules marquées)",
    "replace_exact_with_synthetic_emails": "remplacement d’un placeholder par des e-mails fictifs uniques",
    "absolute_negative_values": "conversion des valeurs négatives en valeurs absolues",
    "detect_and_correct_systematic_date_offset": "correction d’un décalage systématique de date",
    "flag_temporal_anomaly": "signalement des dates inhabituelles (sans correction)",
}

_PLAIN_LANGUAGE_ISSUE_LABELS: Dict[str, str] = {
    "missing_values": "valeurs manquantes",
    "high_missingness": "valeurs manquantes",
    "outliers": "valeurs numériques inhabituelles",
    "suspicious_values": "valeurs suspectes",
    "domain_implausible": "valeurs numériques inhabituelles",
    "duplicate_records": "lignes dupliquées",
    "inconsistent_formats": "formats ou valeurs incohérents",
    "temporal_anomaly": "dates inhabituelles",
    "future_dates": "dates futures à vérifier",
    "cross_column_inconsistency": "incohérences entre colonnes",
    "temporal_inconsistency": "dates dans un ordre incohérent",
    "fuzzy_duplicates": "doublons possibles",
    "identifier_column": "colonnes d’identifiants",
}


def _plain_issue_label(problem_type: str) -> str:
    return _PLAIN_LANGUAGE_ISSUE_LABELS.get(problem_type, problem_type.replace("_", " "))


def build_plain_language_summary(
    actions: List[Dict[str, Any]],
    remaining_issues: List[Dict[str, Any]],
    status: str,
) -> str:
    """
    Deterministically templates a short, non-technical summary from the
    already-validated `actions` and `remaining_issues` lists.

    No severity jargon, no column-type vocabulary, no confidence scores.
    """
    if not actions and not remaining_issues:
        return "Nous avons vérifié votre jeu de données et n'avons rien trouvé à corriger."

    action_sentences: List[str] = []
    if actions:
        action_counter: Dict[str, int] = {}
        rows_by_action: Dict[str, int] = {}
        for a in actions:
            label = _PLAIN_LANGUAGE_ACTION_LABELS.get(a.get("action", ""), a.get("action", "").replace("_", " "))
            action_counter[label] = action_counter.get(label, 0) + 1
            rows_by_action[label] = rows_by_action.get(label, 0) + int(a.get("rows_affected", 0) or 0)

        parts = []
        for label, count in action_counter.items():
            rows = rows_by_action.get(label, 0)
            if rows > 0:
                parts.append(f"{label} ({rows} lignes concernées)")
            else:
                parts.append(label)
        total = len(actions)
        action_sentences.append(
            f"{total} opération{'s' if total > 1 else ''} enregistrée{'s' if total > 1 else ''} : " + ", ".join(parts) + "."
        )

    issue_sentences: List[str] = []
    if remaining_issues:
        counter: Dict[str, int] = {}
        for issue in remaining_issues:
            label = _plain_issue_label(str(issue.get("type", "")))
            counter[label] = counter.get(label, 0) + 1
        total_issues = len(remaining_issues)
        top = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:3]
        top_text = ", ".join(f"{label}" for label, _ in top)
        issue_sentences.append(
            f"{total_issues} élément{'s' if total_issues > 1 else ''} nécessite{'nt' if total_issues > 1 else ''} "
            f"votre attention — "
            f"principalement : {top_text}."
        )

    if status == "unchanged":
        return "Nous avons examiné votre jeu de données et aucune modification automatique n'était nécessaire."

    return " ".join(action_sentences + issue_sentences)


def compute_report_status(actions: List[Dict[str, Any]], remaining_issues: List[Dict[str, Any]]) -> str:
    """Deterministic status derivation — never trusts the LLM's self-reported status."""
    if not actions and not remaining_issues:
        return "unchanged"
    if actions and not remaining_issues:
        return "cleaned"
    if actions and remaining_issues:
        return "partially_cleaned"
    return "partially_cleaned"
