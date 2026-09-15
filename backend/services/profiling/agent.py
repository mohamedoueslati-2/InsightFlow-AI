"""
Google ADK (Agent Development Kit) Profiling Agent Implementation.

Coordinates Google ADK Agent with Gemini model and Pandas analysis tools
to execute iterative reasoning and generate structured profiling reports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from pandas.api.types import is_integer_dtype
from dotenv import load_dotenv

try:
    import google.adk as adk
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    _GEMINI_SDK_AVAILABLE = True
except ModuleNotFoundError:
    adk = None  # type: ignore[assignment]
    InMemorySessionService = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _GEMINI_SDK_AVAILABLE = False

from .gemini_key_manager import GeminiKeyManager
from .prompts import PROFILING_SYSTEM_INSTRUCTION, USER_START_PROMPT
from .schemas import (
    ColumnProfile,
    DatasetMetrics,
    Investigation,
    ProfilingReport,
    QualityProblem,
    QualitySummary,
    Understanding,
)
from .tools import ProfilingToolkit

logger = logging.getLogger(__name__)

# Load environment variables from backend/.env or root .env
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

_TEMPORARY_ERROR_PATTERNS = (
    "resourceexhausted",
    "429",
    "too many requests",
    "quota exceeded",
    "quota_exceeded",
    "rate limit",
    "rate_limit",
)

_AUTH_ERROR_PATTERNS = (
    "invalid api key",
    "authentication",
    "unauthorized",
    "permission denied",
    "forbidden",
    "401",
    "403",
)

_PLACEHOLDER_KEYS = {
    "your_google_ai_studio_api_key_here",
    "your_api_key_here",
    "changeme",
}

_MANAGER_LOCK = threading.Lock()
_KEY_MANAGER: GeminiKeyManager | None = None
_KEY_MANAGER_SIGNATURE: tuple[tuple[str, ...], int] | None = None


def _is_placeholder_key(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDER_KEYS


def _deduplicate_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    cleaned: list[str] = []
    for item in values:
        key = item.strip()
        if not key:
            continue
        if _is_placeholder_key(key):
            continue
        if key not in seen:
            seen.add(key)
            cleaned.append(key)
    return cleaned


def load_gemini_api_keys() -> list[str]:
    """
    Resolve API keys with strict priority:
    1) GEMINI_API_KEYS (comma-separated)
    2) GEMINI_API_KEY (legacy)

    Note: GOOGLE_API_KEY is intentionally NOT used as a source key to avoid
    accidental use of unrelated credentials/tokens.
    """
    api_keys_raw = os.environ.get("GEMINI_API_KEYS", "")
    if api_keys_raw.strip():
        return _deduplicate_preserve_order(api_keys_raw.split(","))

    legacy_single = os.environ.get("GEMINI_API_KEY") or ""
    return _deduplicate_preserve_order([legacy_single])


def get_gemini_model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def get_gemini_key_cooldown_seconds() -> int:
    raw = os.environ.get("GEMINI_KEY_COOLDOWN_SECONDS", "30")
    try:
        value = int(raw)
        return value if value > 0 else 30
    except Exception:
        return 30


def get_gemini_key_manager() -> GeminiKeyManager:
    """Return a process-wide key manager configured from environment."""
    global _KEY_MANAGER, _KEY_MANAGER_SIGNATURE

    api_keys = load_gemini_api_keys()
    cooldown = get_gemini_key_cooldown_seconds()
    signature = (tuple(api_keys), cooldown)

    with _MANAGER_LOCK:
        if _KEY_MANAGER is None or _KEY_MANAGER_SIGNATURE != signature:
            _KEY_MANAGER = GeminiKeyManager(api_keys=api_keys, cooldown_seconds=cooldown)
            _KEY_MANAGER_SIGNATURE = signature
        return _KEY_MANAGER


def get_gemini_runtime_status() -> dict[str, Any]:
    """Lightweight runtime status for health checks (never includes real keys)."""
    manager = get_gemini_key_manager()
    status = manager.get_status()
    return {
        "gemini_configured": status["total_keys"] > 0,
        "model": get_gemini_model_name(),
        "total_keys": status["total_keys"],
        "available_keys": status["available_keys"],
        "cooldown_keys": status["cooldown_keys"],
    }


def _is_temporary_quota_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {str(exc)}".lower()
    return any(pattern in text for pattern in _TEMPORARY_ERROR_PATTERNS)


def _is_authentication_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {str(exc)}".lower()
    return any(pattern in text for pattern in _AUTH_ERROR_PATTERNS)


def _clean_json_text(text: str) -> str:
    """Removes markdown code fences and extraneous text around JSON."""
    text = text.strip()
    # Match ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def _column_name_has_any(name: str, keywords: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(k in lowered for k in keywords)


def _pattern_to_role(pattern_name: str) -> str:
    mapping = {
        "EMAIL": "email",
        "URL": "url",
        "PHONE": "phone",
        "UUID": "uuid",
        "IPV4": "ip_address",
        "IPV6": "ip_address",
        "IP_ADDRESS": "ip_address",
        "POSTAL_CODE": "categorical",
        "PERCENTAGE": "percentage/ratio",
        "CURRENCY_SYMBOL": "monetary",
        "ALPHANUMERIC_CODE": "identifier",
        "DATE_LIKE": "timestamp",
    }
    return mapping.get(pattern_name, f"pattern: {pattern_name.lower()}")


def _normalize_likely_role(value: Any) -> str:
    """Normalize Gemini/free-form role labels to canonical taxonomy."""
    raw = str(value or "").strip().lower()
    if not raw:
        return "unknown"

    canonical = {
        "boolean", "email", "phone", "ip_address", "uuid", "url", "timestamp",
        "identifier", "ordinal", "categorical", "count", "percentage/ratio",
        "monetary", "measure", "text", "unknown",
    }
    if raw in canonical:
        return raw

    alias_map = {
        "bool": "boolean",
        "boolean attribute": "boolean",
        "identifier column": "identifier",
        "id": "identifier",
        "numeric identifier": "identifier",
        "datetime": "timestamp",
        "date": "timestamp",
        "time": "timestamp",
        "temporal": "timestamp",
        "categorical attribute": "categorical",
        "category": "categorical",
        "factor": "categorical",
        "monetary value": "monetary",
        "currency": "monetary",
        "financial": "monetary",
        "numeric": "measure",
        "numerical": "measure",
        "numerical attribute": "measure",
        "number": "measure",
        "ratio": "percentage/ratio",
        "percentage": "percentage/ratio",
        "percent": "percentage/ratio",
        "free text": "text",
        "string": "text",
        "description": "text",
        "unknown role": "unknown",
        "ambiguous": "unknown",
    }
    if raw in alias_map:
        return alias_map[raw]

    if "email" in raw:
        return "email"
    if "phone" in raw:
        return "phone"
    if "ip" in raw:
        return "ip_address"
    if "url" in raw:
        return "url"
    if "uuid" in raw:
        return "uuid"
    if "date" in raw or "time" in raw:
        return "timestamp"
    if "identifier" in raw or raw.endswith(" id"):
        return "identifier"
    if "categor" in raw:
        return "categorical"
    if "percent" in raw or "ratio" in raw:
        return "percentage/ratio"
    if "money" in raw or "monetar" in raw or "currenc" in raw or "financial" in raw:
        return "monetary"
    if "bool" in raw:
        return "boolean"
    if "ordinal" in raw:
        return "ordinal"
    if "count" in raw:
        return "count"
    if "text" in raw or "string" in raw:
        return "text"
    if "numeric" in raw or "measure" in raw:
        return "measure"

    return "unknown"


def _compute_quality_components(
    rows: int,
    cols: int,
    missing_cells: int,
    duplicate_rows: int,
    problems: list[QualityProblem],
) -> dict[str, float]:
    total_cells = rows * cols if rows * cols > 0 else 1
    completeness = max(0.0, min(100.0, round((1.0 - (missing_cells / total_cells)) * 100.0, 2)))
    uniqueness = max(0.0, min(100.0, round((1.0 - ((duplicate_rows / rows) if rows > 0 else 0.0)) * 100.0, 2)))

    severity_weights = {"low": 2.0, "medium": 5.0, "high": 10.0}
    weighted = sum(severity_weights.get(p.severity, 4.0) for p in problems)

    validity = max(0.0, min(100.0, round(100.0 - min(60.0, weighted * 1.5), 2)))
    consistency = max(0.0, min(100.0, round(100.0 - min(50.0, weighted), 2)))

    overall = round((0.30 * completeness) + (0.20 * uniqueness) + (0.30 * validity) + (0.20 * consistency), 1)
    return {
        "completeness": completeness,
        "uniqueness": uniqueness,
        "validity": validity,
        "consistency": consistency,
        "overall": max(0.0, min(100.0, overall)),
    }


def _sanitize_gemini_report_against_dataframe(result: dict[str, Any], df: pd.DataFrame) -> dict[str, Any]:
    """Ensure final report never references hallucinated columns and keeps deterministic facts authoritative."""
    actual_cols = {str(c) for c in df.columns}

    # Deterministic authoritative facts
    toolkit = ProfilingToolkit(df)
    overview = toolkit.get_dataset_overview()
    duplicates = toolkit.detect_duplicates()

    result.setdefault("dataset", {})
    result["dataset"]["rows"] = int(overview.get("rows", 0))
    result["dataset"]["columns"] = int(overview.get("columns_count", 0))
    result["dataset"]["duplicate_rows"] = int(duplicates.get("total_duplicate_rows", 0))
    result["dataset"]["missing_values"] = int(overview.get("total_missing_cells", 0))
    result["dataset"]["memory_usage_kb"] = overview.get("memory_usage_kb")

    # Validate columns list against real schema only
    safe_columns = []
    for col_item in result.get("columns", []) or []:
        name = str(col_item.get("name", ""))
        if name in actual_cols:
            normalized = dict(col_item)
            normalized["likely_role"] = _normalize_likely_role(normalized.get("likely_role", "unknown"))
            safe_columns.append(normalized)

    # Ensure every actual dataframe column exists in report columns
    reported = {str(c.get("name", "")) for c in safe_columns}
    for col in df.columns:
        col_name = str(col)
        if col_name not in reported:
            inspect = toolkit.inspect_column(col_name)
            safe_columns.append({
                "name": col_name,
                "observed_type": str(inspect.get("dtype", "unknown")),
                "likely_role": "unknown",
                "confidence": 0.5,
                "observations": [
                    "Added by schema validation: column exists in DataFrame but was missing from AI output.",
                ],
            })

    result["columns"] = safe_columns

    # Validate problems columns + normalize temporal anomaly types
    safe_problems = []
    col_role_map = {str(c.get("name", "")): str(c.get("likely_role", "unknown")) for c in safe_columns}
    for p in result.get("problems", []) or []:
        raw_scope = str(p.get("scope", "column")).lower()
        scope = "dataset" if raw_scope == "dataset" else "column"
        raw_col = p.get("column", None)
        col_name = None if raw_col is None else str(raw_col)

        if scope == "dataset":
            p["scope"] = "dataset"
            p["column"] = None
        else:
            # Backward compatibility: allow older Gemini output using column='dataset'
            if col_name == "dataset":
                p["scope"] = "dataset"
                p["column"] = None
                scope = "dataset"
            else:
                if not col_name or col_name not in actual_cols:
                    continue
                p["scope"] = "column"
                p["column"] = col_name

        p_type = str(p.get("type", ""))
        evidence = str(p.get("evidence", "")).lower()
        role = col_role_map.get(col_name or "", "unknown")
        if role == "timestamp" and p_type == "outliers":
            if "future" in evidence:
                p["type"] = "future_dates"
            else:
                p["type"] = "temporal_anomaly"

        safe_problems.append(p)

    result["problems"] = safe_problems

    # Validate investigations columns
    safe_investigations = []
    for inv in result.get("investigations", []) or []:
        col_name = str(inv.get("column", ""))
        if col_name == "dataset":
            safe_investigations.append(inv)
            continue
        if col_name in actual_cols:
            safe_investigations.append(inv)
            continue
        # Allow relationship references like "a -> b" or "a, b, c"
        parts = re.split(r"\s*->\s*|\s*,\s*|\s*~\s*", col_name)
        if parts and all((part in actual_cols) for part in parts if part):
            safe_investigations.append(inv)

    result["investigations"] = safe_investigations

    # Authoritative quality summary counts + deterministic explainable score
    result.setdefault("quality_summary", {})
    result["quality_summary"]["missing_values"] = int(overview.get("total_missing_cells", 0))
    result["quality_summary"]["duplicate_rows"] = int(duplicates.get("total_duplicate_rows", 0))
    result["quality_summary"]["potential_issues"] = int(len(safe_problems))

    pydantic_problems = []
    for p in safe_problems:
        try:
            pydantic_problems.append(QualityProblem.model_validate(p))
        except Exception:
            continue

    q = _compute_quality_components(
        rows=int(overview.get("rows", 0)),
        cols=int(overview.get("columns_count", 0)),
        missing_cells=int(overview.get("total_missing_cells", 0)),
        duplicate_rows=int(duplicates.get("total_duplicate_rows", 0)),
        problems=pydantic_problems,
    )
    result["quality_summary"]["quality_score"] = q["overall"]

    result["investigations"].append({
        "column": "dataset",
        "observation": "Deterministic quality score components computed from Pandas metrics.",
        "examples": [
            f"completeness={q['completeness']}",
            f"uniqueness={q['uniqueness']}",
            f"validity={q['validity']}",
            f"consistency={q['consistency']}",
            f"overall={q['overall']}",
        ],
    })

    return result


def build_deterministic_baseline_profile(df: pd.DataFrame) -> ProfilingReport:
    """
    Smart deterministic profiler with explicit hypothesis memory and planner policy.

    Planner strategy:
    1) Bootstrap global metrics + all-column inspection
    2) Prioritize uncertain/high-impact columns
    3) Execute deep analysis under configurable tool budget
    4) Run automatic identifier dependency checks
    5) Synthesize report with factual evidence
    """
    toolkit = ProfilingToolkit(df)
    overview = toolkit.get_dataset_overview()
    missing_data = toolkit.detect_missing_values()
    duplicates_data = toolkit.detect_duplicates()

    rows = int(overview.get("rows", 0))
    cols = int(overview.get("columns_count", 0))
    col_names = [str(c) for c in overview.get("column_names", [])]

    deep_tool_budget = 40
    deep_columns_ratio = 0.5
    max_relation_checks = 8

    deep_calls_used = 0

    def consume_deep_call() -> bool:
        nonlocal deep_calls_used
        if deep_calls_used >= deep_tool_budget:
            return False
        deep_calls_used += 1
        return True

    # Explicit hypothesis memory
    hypothesis_memory: dict[str, dict[str, Any]] = {
        col: {
            "role": "unknown",
            "confidence": 0.5,
            "trace": ["Initial hypothesis: unknown (0.50)"],
        }
        for col in col_names
    }

    def update_hypothesis(col: str, role: str, confidence: float, evidence: str) -> None:
        mem = hypothesis_memory[col]
        prev_role = str(mem["role"])
        prev_conf = float(mem["confidence"])

        role_priority = {
            "boolean": 14,
            "email": 13,
            "phone": 12,
            "ip_address": 11,
            "uuid": 10,
            "url": 9,
            "timestamp": 8,
            "identifier": 7,
            "ordinal": 6,
            "categorical": 5,
            "count": 4,
            "percentage/ratio": 3,
            "monetary": 2,
            "measure": 1,
            "text": 0,
            "unknown": -1,
        }

        # Specialized roles should win over generic identifier when evidence is reasonably strong.
        specialized = {"email", "phone", "ip_address", "uuid", "url"}
        should_update = False

        if role != "unknown":
            if prev_role == "unknown" or role == prev_role:
                should_update = True
            elif confidence > prev_conf:
                should_update = True
            elif role in specialized and prev_role == "identifier" and confidence >= 0.7:
                should_update = True
            elif role_priority.get(role, 0) > role_priority.get(prev_role, 0) and confidence >= (prev_conf - 0.05):
                should_update = True

        if should_update:
            mem["role"] = role
            mem["confidence"] = max(prev_conf, min(1.0, confidence))
            mem["trace"].append(
                f"{prev_role} ({prev_conf:.2f}) -> {mem['role']} ({float(mem['confidence']):.2f}) because {evidence}"
            )
        else:
            mem["trace"].append(
                f"Kept {prev_role} ({prev_conf:.2f}); evidence observed: {evidence}"
            )

    # Phase 1: bootstrap inspection for all columns
    inspections: dict[str, dict[str, Any]] = {}
    priorities: list[tuple[str, float]] = []

    for col in col_names:
        inspect = toolkit.inspect_column(col)
        inspections[col] = inspect

        missing_pct = float(inspect.get("missing_percentage", 0.0))
        unique_pct = float(inspect.get("unique_percentage", 0.0))
        is_num = bool(inspect.get("is_numeric", False))

        uncertainty = 1.0 - 0.5  # starts from baseline confidence 0.5
        impact = 0.0
        anomaly = 0.0

        if _column_name_has_any(col, ("id", "code", "key", "date", "time", "price", "cost", "amount", "qty", "target", "status")):
            impact += 0.35
        if is_num:
            impact += 0.20
        if unique_pct > 95:
            impact += 0.20
        if missing_pct >= 20:
            anomaly += 0.35

        priority = round((0.45 * uncertainty) + (0.35 * impact) + (0.20 * anomaly), 4)
        priorities.append((col, priority))

    priorities.sort(key=lambda x: x[1], reverse=True)
    deep_target_count = max(1, int(len(col_names) * deep_columns_ratio))
    deep_columns = {col for col, _ in priorities[:deep_target_count]}

    columns_profile: list[ColumnProfile] = []
    problems: list[QualityProblem] = []
    investigations: list[Investigation] = []

    # Phase 2: targeted deep investigation
    for col in col_names:
        inspect = inspections[col]
        dtype = str(inspect.get("dtype", "unknown"))
        is_num = bool(inspect.get("is_numeric", False))
        is_bool = bool(inspect.get("is_boolean", False))
        missing_count = int(inspect.get("missing_count", 0))
        missing_pct = float(inspect.get("missing_percentage", 0.0))
        unique_count = int(inspect.get("unique_count", 0))

        observations = [
            f"Observed data type: {dtype}",
            f"{missing_count} missing values ({missing_pct}%)",
            f"{unique_count} unique values",
        ]

        if missing_pct > 20.0:
            problems.append(QualityProblem(
                column=col,
                type="high_missingness",
                severity="high" if missing_pct > 50 else "medium",
                evidence=f"{missing_count} missing rows ({missing_pct}%)",
                confidence=0.95,
            ))

        # Identifier hypothesis from direct inspection (avoid over-classifying semantic categories)
        if rows > 0 and unique_count == rows and missing_count == 0:
            if _column_name_has_any(col, ("id", "code", "key", "uuid", "record", "transaction", "customer_number", "order_number", "invoice_number", "reference", "ref")):
                update_hypothesis(col, "identifier", 0.9, "all values unique/non-null and name indicates identifier semantics")
            elif not _column_name_has_any(col, ("country", "region", "category", "product", "channel", "customer_type", "type", "name", "city")):
                update_hypothesis(col, "identifier", 0.72, "all values unique and non-null")

        if is_bool:
            update_hypothesis(col, "boolean", 0.95, "column dtype is boolean")
            observations.append("Boolean column detected; numeric statistics and outlier analysis intentionally skipped.")
            if "true_count" in inspect and "false_count" in inspect:
                observations.append(
                    f"True/False distribution: {inspect.get('true_count')} true ({inspect.get('true_percentage')}%), "
                    f"{inspect.get('false_count')} false ({inspect.get('false_percentage')}%)."
                )
        elif is_num:
            if consume_deep_call():
                num_data = toolkit.analyze_numeric_column(col)
                update_hypothesis(col, "measure", 0.72, "numeric distribution statistics available")

                min_val = num_data.get("min")
                max_val = num_data.get("max")
                if isinstance(min_val, (int, float)) and isinstance(max_val, (int, float)):
                    if is_integer_dtype(df[col]) and min_val >= 0 and _column_name_has_any(col, ("record_id", "customer_number", "transaction_number", "order_number", "invoice_number")):
                        update_hypothesis(col, "identifier", 0.9, "numeric unique-like identifier naming convention")
                    if is_integer_dtype(df[col]) and min_val >= 0 and _column_name_has_any(col, ("quantity", "qty", "count", "units", "num", "volume")):
                        update_hypothesis(col, "count", 0.9, "non-negative integer values with count-like semantic column name")
                    elif is_integer_dtype(df[col]) and max_val <= 10 and min_val >= 0 and _column_name_has_any(col, ("rating", "score", "priority", "level", "severity", "satisfaction")):
                        update_hypothesis(col, "ordinal", 0.86, "bounded small integer range with ordinal-like semantic name")

                    if _column_name_has_any(col, ("pct", "percent", "percentage", "ratio", "rate", "discount_pct", "discount_rate")):
                        if 0.0 <= float(min_val) and float(max_val) <= 1.0:
                            update_hypothesis(col, "percentage/ratio", 0.9, "value range in [0, 1] with percentage/ratio semantic name")
                        elif 0.0 <= float(min_val) and float(max_val) <= 100.0:
                            update_hypothesis(col, "percentage/ratio", 0.88, "value range in [0, 100] with percentage/ratio semantic name")

                observations.append(
                    f"Range: [{num_data.get('min')}, {num_data.get('max')}], Mean: {num_data.get('mean')}, Median: {num_data.get('median')}"
                )

                skewness = num_data.get("skewness")
                if isinstance(skewness, (int, float)) and abs(float(skewness)) >= 1.5:
                    investigations.append(Investigation(
                        column=col,
                        observation=f"Distribution appears strongly skewed (skewness={round(float(skewness), 3)}).",
                        examples=[str(num_data.get("min")), str(num_data.get("median")), str(num_data.get("max"))],
                    ))

                needs_outlier_check = col in deep_columns or float(num_data.get("outliers_percentage", 0.0)) > 0.0
                if needs_outlier_check and consume_deep_call():
                    outlier_data = toolkit.detect_outliers(col)
                    out_cnt = int(outlier_data.get("total_outliers_count", 0))
                    out_pct = float(outlier_data.get("outlier_percentage", 0.0))
                    if out_cnt > 0:
                        observations.append(f"Detected {out_cnt} outliers ({out_pct}%)")
                        severity = "low" if out_pct < 1 else ("medium" if out_pct < 10 else "high")
                        problems.append(QualityProblem(
                            column=col,
                            type="outliers",
                            severity=severity,
                            evidence=f"{out_cnt} values ({out_pct}%) outside IQR bounds [{outlier_data.get('lower_bound')}, {outlier_data.get('upper_bound')}]",
                            confidence=0.85,
                        ))

                if _column_name_has_any(col, ("price", "cost", "amount", "revenue", "salary", "fee", "total", "profit", "margin")):
                    update_hypothesis(col, "monetary", 0.83, "column name and numeric behavior indicate monetary semantics")
                    neg_cnt = int(num_data.get("negative_count", 0))
                    if neg_cnt > 0:
                        problems.append(QualityProblem(
                            column=col,
                            type="suspicious_negative_values",
                            severity="medium",
                            evidence=f"{neg_cnt} negative values found in a likely monetary column.",
                            confidence=0.8,
                        ))
            else:
                observations.append("Deep numeric analysis skipped due to planner tool budget.")
        else:
            date_like_hint = _column_name_has_any(col, ("date", "time", "timestamp", "created", "updated")) or (col in deep_columns)
            date_data = {"appears_date_like": False}

            if date_like_hint and consume_deep_call():
                date_data = toolkit.analyze_date_column(col)

            if date_data.get("appears_date_like"):
                update_hypothesis(col, "timestamp", 0.85, "values parse reliably as dates")
                observations.append(f"Date range: {date_data.get('min_date')} to {date_data.get('max_date')}")

                for susp in date_data.get("suspicious_observations", []):
                    susp_text = str(susp)
                    p_type = "future_dates" if "future" in susp_text.lower() else "temporal_anomaly"
                    problems.append(QualityProblem(
                        column=col,
                        type=p_type,
                        severity="medium",
                        evidence=susp_text,
                        confidence=0.90,
                    ))
            else:
                pat_data = {"detected_patterns": []}
                if consume_deep_call():
                    pat_data = toolkit.detect_patterns(col)

                detected_patterns = pat_data.get("detected_patterns", [])
                if detected_patterns:
                    top_pat = detected_patterns[0]
                    pat_name = str(top_pat.get("pattern", ""))
                    role = _pattern_to_role(pat_name)
                    conf = float(top_pat.get("match_rate", 0.0))

                    # ALPHANUMERIC_CODE is weak evidence: do not auto-promote semantic categories to identifier.
                    if pat_name == "ALPHANUMERIC_CODE":
                        if _column_name_has_any(col, ("country", "region", "category", "product", "channel", "customer_type", "type")):
                            role = "categorical"
                            conf = max(0.55, min(conf, 0.7))
                        elif not _column_name_has_any(col, ("id", "code", "key", "uuid", "record", "transaction", "customer_number")):
                            role = "categorical"
                            conf = max(0.5, min(conf, 0.65))

                    update_hypothesis(col, role, conf, f"pattern {pat_name} matched at {top_pat.get('match_percentage')}%")
                    observations.append(f"Matches {pat_name} pattern ({top_pat.get('match_percentage')}%)")

                use_cat_analysis = col in deep_columns or unique_count <= 50
                cat_data = None
                if use_cat_analysis and consume_deep_call():
                    cat_data = toolkit.analyze_categorical_column(col)

                if cat_data is not None:
                    if cat_data.get("cardinality", 0) <= max(20, rows // 5 if rows > 0 else 20):
                        update_hypothesis(col, "categorical", 0.72, "low/moderate cardinality and category structure")

                    if cat_data.get("dominant_category"):
                        observations.append(
                            f"Dominant category: {cat_data.get('dominant_category')} ({cat_data.get('dominant_percentage')}%)"
                        )

                    for group in cat_data.get("inconsistent_representations", []):
                        variants = group.get("observed_variants", [])
                        if variants:
                            investigations.append(Investigation(
                                column=col,
                                observation=f"Possible inconsistent representations for '{group.get('normalized_concept')}'.",
                                examples=[str(v) for v in variants[:5]],
                            ))
                            problems.append(QualityProblem(
                                column=col,
                                type="inconsistent_formats",
                                severity="medium",
                                evidence=f"Variants observed: {', '.join([str(v) for v in variants[:8]])}",
                                confidence=0.80,
                            ))

                if rows > 0 and unique_count / rows > 0.75:
                    update_hypothesis(col, "text", 0.65, "high-cardinality non-numeric values suggest free-text or names")

        role = str(hypothesis_memory[col]["role"])
        conf = round(float(hypothesis_memory[col]["confidence"]), 2)
        trace = hypothesis_memory[col]["trace"]
        if trace:
            observations.append(f"Hypothesis trace: {trace[-1]}")

        columns_profile.append(ColumnProfile(
            name=col,
            observed_type=dtype,
            likely_role=role,
            confidence=conf,
            observations=observations,
        ))

    # Phase 3: automatic identifier -> dependency checks (domain-agnostic)
    explicit_identifier_cols = [c.name for c in columns_profile if c.likely_role == "identifier" and c.confidence >= 0.8]
    candidate_identifier_cols = []
    for col in col_names:
        inspect = inspections.get(col, {})
        unique_pct = float(inspect.get("unique_percentage", 0.0))
        missing_pct = float(inspect.get("missing_percentage", 0.0))
        if _column_name_has_any(col, ("id", "code", "key")) and unique_pct >= 50.0 and missing_pct <= 5.0:
            candidate_identifier_cols.append(col)

    identifier_cols = list(dict.fromkeys(explicit_identifier_cols + candidate_identifier_cols))
    relation_checks_used = 0

    for id_col in identifier_cols:
        for other_col in col_names:
            if other_col == id_col:
                continue
            if relation_checks_used >= max_relation_checks:
                break
            if not consume_deep_call():
                break

            relation_checks_used += 1
            comp = toolkit.compare_columns(id_col, other_col)
            pair = df[[id_col, other_col]].dropna()
            if len(pair) == 0:
                continue

            # Functional dependency style check: each id should map to one value of other_col.
            per_id_nunique = pair.groupby(id_col)[other_col].nunique(dropna=True)
            inconsistent_ids = int((per_id_nunique > 1).sum())

            if inconsistent_ids > 0:
                problems.append(QualityProblem(
                    column=other_col,
                    type="inconsistent_identifier_dependency",
                    severity="high" if inconsistent_ids > max(1, len(pair) // 20) else "medium",
                    evidence=(
                        f"{id_col} maps to multiple {other_col} values for {inconsistent_ids} identifiers. "
                        f"Potential violation of expected key dependency."
                    ),
                    confidence=0.9,
                ))
                investigations.append(Investigation(
                    column=f"{id_col} -> {other_col}",
                    observation="Identifier dependency inconsistency detected.",
                    examples=[
                        f"jaccard_similarity={comp.get('jaccard_similarity', 'n/a')}",
                        f"shared_unique_values_count={comp.get('shared_unique_values_count', 'n/a')}",
                    ],
                ))
            else:
                # Only add lightweight positive investigation when informative.
                if pair[id_col].nunique() >= 5 and pair[other_col].nunique() < pair[id_col].nunique():
                    investigations.append(Investigation(
                        column=f"{id_col} -> {other_col}",
                        observation="Stable identifier dependency observed (each identifier maps consistently).",
                        examples=[
                            f"identifier_count={pair[id_col].nunique()}",
                            f"target_unique_values={pair[other_col].nunique()}",
                        ],
                    ))

    # Phase 4: semantic consistency checks across related columns
    lower_col_map = {c.lower(): c for c in col_names}

    def _find_col(candidates: tuple[str, ...]) -> str | None:
        for cand in candidates:
            if cand.lower() in lower_col_map:
                return lower_col_map[cand.lower()]
        for key, real in lower_col_map.items():
            if any(token in key for token in candidates):
                return real
        return None

    qty_col = _find_col(("quantity", "qty", "units", "count"))
    unit_price_col = _find_col(("unit_price", "price", "unit cost", "unit_cost"))
    total_col = _find_col(("total_amount", "amount", "total", "gross_amount"))

    if qty_col and unit_price_col and total_col:
        subset = df[[qty_col, unit_price_col, total_col]].copy().dropna()
        if len(subset) > 0:
            q = pd.to_numeric(subset[qty_col], errors="coerce")
            p = pd.to_numeric(subset[unit_price_col], errors="coerce")
            t = pd.to_numeric(subset[total_col], errors="coerce")
            valid = pd.DataFrame({"q": q, "p": p, "t": t}).dropna()
            rows_checked = int(len(valid))
            if rows_checked > 0:
                expected = valid["q"] * valid["p"]
                tolerance = expected.abs() * 0.01 + 0.01
                consistent_mask = (valid["t"] - expected).abs() <= tolerance
                rows_consistent = int(consistent_mask.sum())
                rows_inconsistent = int(rows_checked - rows_consistent)
                consistency_pct = round((rows_consistent / rows_checked) * 100, 2)

                investigations.append(Investigation(
                    column=f"{qty_col}, {unit_price_col}, {total_col}",
                    observation=f"Arithmetic consistency check: {total_col} ≈ {qty_col} × {unit_price_col}.",
                    examples=[
                        f"rows_checked={rows_checked}",
                        f"rows_consistent={rows_consistent}",
                        f"rows_inconsistent={rows_inconsistent}",
                        f"consistency_percentage={consistency_pct}",
                    ],
                ))

                if rows_inconsistent > 0:
                    problems.append(QualityProblem(
                        column=total_col,
                        type="cross_column_inconsistency",
                        severity="medium" if consistency_pct >= 90 else "high",
                        evidence=(
                            f"{rows_inconsistent}/{rows_checked} rows violate {total_col} ≈ {qty_col} × {unit_price_col} "
                            f"(consistency={consistency_pct}%)."
                        ),
                        confidence=0.9,
                    ))

    # quantity × price × discount ↔ revenue
    discount_col = _find_col(("discount", "discount_rate", "discount_pct", "rebate"))
    revenue_col = _find_col(("revenue", "net_revenue", "net_amount"))
    if qty_col and unit_price_col and discount_col and revenue_col:
        subset = df[[qty_col, unit_price_col, discount_col, revenue_col]].copy().dropna()
        if len(subset) > 0:
            q = pd.to_numeric(subset[qty_col], errors="coerce")
            p = pd.to_numeric(subset[unit_price_col], errors="coerce")
            d = pd.to_numeric(subset[discount_col], errors="coerce")
            r = pd.to_numeric(subset[revenue_col], errors="coerce")
            valid = pd.DataFrame({"q": q, "p": p, "d": d, "r": r}).dropna()
            rows_checked = int(len(valid))
            if rows_checked > 0:
                # Assume discount as fraction when <=1, otherwise percentage
                disc_ratio = valid["d"].where(valid["d"] <= 1.0, valid["d"] / 100.0).clip(lower=0.0, upper=1.0)
                expected_rev = valid["q"] * valid["p"] * (1.0 - disc_ratio)
                tolerance = expected_rev.abs() * 0.02 + 0.05
                consistent_mask = (valid["r"] - expected_rev).abs() <= tolerance
                rows_consistent = int(consistent_mask.sum())
                rows_inconsistent = int(rows_checked - rows_consistent)
                consistency_pct = round((rows_consistent / rows_checked) * 100, 2)

                investigations.append(Investigation(
                    column=f"{qty_col}, {unit_price_col}, {discount_col}, {revenue_col}",
                    observation=f"Revenue consistency check: {revenue_col} ≈ {qty_col} × {unit_price_col} × (1 - discount).",
                    examples=[
                        f"rows_checked={rows_checked}",
                        f"rows_consistent={rows_consistent}",
                        f"rows_inconsistent={rows_inconsistent}",
                        f"consistency_percentage={consistency_pct}",
                    ],
                ))

                if rows_inconsistent > 0:
                    problems.append(QualityProblem(
                        column=revenue_col,
                        type="cross_column_inconsistency",
                        severity="medium" if consistency_pct >= 90 else "high",
                        evidence=(
                            f"{rows_inconsistent}/{rows_checked} rows violate revenue consistency rule "
                            f"(consistency={consistency_pct}%)."
                        ),
                        confidence=0.9,
                    ))

    # revenue ↔ profit (profit should not systematically exceed revenue)
    profit_col = _find_col(("profit", "margin_amount", "net_profit"))
    if revenue_col and profit_col and revenue_col != profit_col:
        subset = df[[revenue_col, profit_col]].copy().dropna()
        if len(subset) > 0:
            rev = pd.to_numeric(subset[revenue_col], errors="coerce")
            prof = pd.to_numeric(subset[profit_col], errors="coerce")
            valid = pd.DataFrame({"rev": rev, "prof": prof}).dropna()
            rows_checked = int(len(valid))
            if rows_checked > 0:
                inconsistent_mask = valid["prof"] > valid["rev"]
                rows_inconsistent = int(inconsistent_mask.sum())
                rows_consistent = int(rows_checked - rows_inconsistent)
                consistency_pct = round((rows_consistent / rows_checked) * 100, 2)
                investigations.append(Investigation(
                    column=f"{revenue_col}, {profit_col}",
                    observation=f"Profit consistency check: {profit_col} should generally be <= {revenue_col}.",
                    examples=[
                        f"rows_checked={rows_checked}",
                        f"rows_consistent={rows_consistent}",
                        f"rows_inconsistent={rows_inconsistent}",
                        f"consistency_percentage={consistency_pct}",
                    ],
                ))
                if rows_inconsistent > 0:
                    problems.append(QualityProblem(
                        column=profit_col,
                        type="cross_column_inconsistency",
                        severity="medium" if consistency_pct >= 90 else "high",
                        evidence=(
                            f"{rows_inconsistent}/{rows_checked} rows have {profit_col} > {revenue_col} "
                            f"(consistency={consistency_pct}%)."
                        ),
                        confidence=0.85,
                    ))

    start_col = _find_col(("start_date", "transaction_date", "order_date", "created_at"))
    end_col = _find_col(("end_date", "ship_date", "delivery_date", "updated_at"))
    if start_col and end_col and start_col != end_col:
        subset = df[[start_col, end_col]].dropna()
        if len(subset) > 0:
            s = pd.to_datetime(subset[start_col], errors="coerce")
            e = pd.to_datetime(subset[end_col], errors="coerce")
            valid = pd.DataFrame({"s": s, "e": e}).dropna()
            rows_checked = int(len(valid))
            if rows_checked > 0:
                consistent_mask = valid["e"] >= valid["s"]
                rows_consistent = int(consistent_mask.sum())
                rows_inconsistent = int(rows_checked - rows_consistent)
                consistency_pct = round((rows_consistent / rows_checked) * 100, 2)

                investigations.append(Investigation(
                    column=f"{start_col} -> {end_col}",
                    observation=f"Temporal consistency check: {end_col} >= {start_col}.",
                    examples=[
                        f"rows_checked={rows_checked}",
                        f"rows_consistent={rows_consistent}",
                        f"rows_inconsistent={rows_inconsistent}",
                        f"consistency_percentage={consistency_pct}",
                    ],
                ))

                if rows_inconsistent > 0:
                    problems.append(QualityProblem(
                        column=end_col,
                        type="temporal_inconsistency",
                        severity="medium" if consistency_pct >= 90 else "high",
                        evidence=(
                            f"{rows_inconsistent}/{rows_checked} rows have {end_col} earlier than {start_col} "
                            f"(consistency={consistency_pct}%)."
                        ),
                        confidence=0.9,
                    ))

    # Phase 5: optional numeric relationship checks
    numeric_cols = [c.name for c in columns_profile if "int" in c.observed_type or "float" in c.observed_type]
    for i in range(min(3, len(numeric_cols))):
        for j in range(i + 1, min(4, len(numeric_cols))):
            if not consume_deep_call():
                break
            a = numeric_cols[i]
            b = numeric_cols[j]
            comp = toolkit.compare_columns(a, b)
            pearson = comp.get("pearson_correlation")
            if isinstance(pearson, (int, float)) and abs(float(pearson)) >= 0.85:
                investigations.append(Investigation(
                    column=f"{a} ~ {b}",
                    observation=f"Strong linear relationship detected (pearson={round(float(pearson), 3)}).",
                    examples=[str(comp.get("spearman_correlation", "n/a"))],
                ))

    # Value-range validations (semantic invalidity checks)
    for c in columns_profile:
        col = c.name
        if col not in df.columns:
            continue
        series_num = pd.to_numeric(df[col], errors="coerce")
        valid_num = series_num.dropna()
        if len(valid_num) == 0:
            continue

        if c.likely_role in {"count", "monetary"}:
            neg_count = int((valid_num < 0).sum())
            if neg_count > 0:
                problems.append(QualityProblem(
                    column=col,
                    type="semantic_invalid_value",
                    severity="medium",
                    evidence=f"{neg_count} negative values detected in {c.likely_role} column.",
                    confidence=0.9,
                ))

        if c.likely_role in {"ordinal"} or _column_name_has_any(col, ("rating", "score", "priority", "level")):
            out_of_range = int(((valid_num < 1) | (valid_num > 5)).sum())
            if out_of_range > 0:
                problems.append(QualityProblem(
                    column=col,
                    type="semantic_invalid_value",
                    severity="medium",
                    evidence=f"{out_of_range} values outside expected ordinal range [1, 5].",
                    confidence=0.85,
                ))

        if c.likely_role == "percentage/ratio" or _column_name_has_any(col, ("percentage", "percent", "pct", "rate", "ratio")):
            out_of_range = int(((valid_num < 0) | (valid_num > 100)).sum())
            if out_of_range > 0:
                problems.append(QualityProblem(
                    column=col,
                    type="semantic_invalid_value",
                    severity="medium",
                    evidence=f"{out_of_range} values outside expected percentage range [0, 100].",
                    confidence=0.85,
                ))

    # Dataset-level duplicate issue
    dup_rows = int(duplicates_data.get("total_duplicate_rows", 0))
    dup_pct = float(duplicates_data.get("duplicate_percentage", 0.0))
    if dup_rows > 0:
        problems.append(QualityProblem(
            scope="dataset",
            column=None,
            type="duplicate_records",
            severity="high" if dup_pct > 5 else "medium",
            evidence=f"{dup_rows} exact duplicate rows detected ({dup_pct}%)",
            confidence=1.0,
        ))

    # Add global missingness signal from dedicated tool output.
    if missing_data.get("rows_with_over_50pct_nulls", 0) > 0:
        severe_rows = int(missing_data.get("rows_with_over_50pct_nulls", 0))
        problems.append(QualityProblem(
            scope="dataset",
            column=None,
            type="severe_row_missingness",
            severity="medium",
            evidence=f"{severe_rows} rows have at least 50% missing cells.",
            confidence=0.9,
        ))

    if investigations and not problems:
        problems.append(QualityProblem(
            scope="dataset",
            column=None,
            type="notable_distribution_patterns",
            severity="low",
            evidence="Investigations found notable patterns worth review, though no hard data-quality failure was detected.",
            confidence=0.7,
        ))

    # Quality score (deterministic and explainable)
    missing_cells = int(overview.get("total_missing_cells", 0))
    q = _compute_quality_components(
        rows=rows,
        cols=cols,
        missing_cells=missing_cells,
        duplicate_rows=dup_rows,
        problems=problems,
    )
    quality_score = q["overall"]

    investigations.append(Investigation(
        column="dataset",
        observation="Deterministic quality score components computed from Pandas metrics.",
        examples=[
            f"completeness={q['completeness']}",
            f"uniqueness={q['uniqueness']}",
            f"validity={q['validity']}",
            f"consistency={q['consistency']}",
            f"overall={q['overall']}",
        ],
    ))

    role_counts: dict[str, int] = {}
    for c in columns_profile:
        role_counts[c.likely_role] = role_counts.get(c.likely_role, 0) + 1
    top_roles = sorted(role_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    role_text = ", ".join([f"{role} ({cnt})" for role, cnt in top_roles]) if top_roles else "unknown roles"

    understanding_text = (
        f"Dataset containing {rows} records across {cols} columns. "
        f"Primary inferred column roles: {role_text}. "
        f"Planner-based investigation used {deep_calls_used}/{deep_tool_budget} deep analysis calls "
        f"with {relation_checks_used} identifier dependency checks."
    )

    return ProfilingReport(
        dataset=DatasetMetrics(
            rows=rows,
            columns=cols,
            duplicate_rows=dup_rows,
            missing_values=missing_cells,
            memory_usage_kb=overview.get("memory_usage_kb"),
        ),
        understanding=Understanding(
            description=understanding_text,
            confidence=0.80,
        ),
        columns=columns_profile,
        problems=problems,
        investigations=investigations,
        quality_summary=QualitySummary(
            missing_values=missing_cells,
            duplicate_rows=dup_rows,
            potential_issues=len(problems),
            quality_score=quality_score,
        ),
    )


class AIProfilingAgent:
    """
    Autonomous Google ADK Profiling Agent that interacts with Gemini to investigate
    a dataset using dynamic tool calls and produce a structured profiling report.
    """

    def __init__(self, df: pd.DataFrame, model_name: Optional[str] = None):
        self.df = df
        self.model_name = model_name or get_gemini_model_name()

        # Initialize the deterministic toolkit
        self.toolkit = ProfilingToolkit(self.df)

        # Shared key manager (process-wide)
        self.key_manager = get_gemini_key_manager()
        self.api_keys = list(self.key_manager.api_keys)

    async def _run_gemini_once(self) -> Dict[str, Any]:
        """Execute one ADK + Gemini call with the currently configured environment key."""
        if not _GEMINI_SDK_AVAILABLE:
            raise RuntimeError("Gemini SDK dependencies are missing (google-adk/google-genai).")

        # 1. Instantiate the Google ADK Agent
        tools = self.toolkit.get_tools()
        agent = adk.Agent(
            name="data_profiling_agent",
            model=self.model_name,
            instruction=PROFILING_SYSTEM_INSTRUCTION,
            tools=tools,
            output_schema=ProfilingReport,
        )

        # 2. Setup InMemorySessionService & Runner
        session_service = InMemorySessionService()
        runner = adk.Runner(
            app_name="ai_profiling_app",
            agent=agent,
            session_service=session_service,
            auto_create_session=True,
        )

        user_id = f"user_{uuid.uuid4().hex[:8]}"
        session_id = f"session_{uuid.uuid4().hex[:8]}"

        # 3. Create initial prompt message for the agent
        user_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=USER_START_PROMPT)],
        )

        final_response_text = ""

        # 4. Run the Google ADK Agent loop
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_message,
        ):
            if event.message and event.message.parts:
                for part in event.message.parts:
                    if hasattr(part, "text") and part.text:
                        final_response_text = part.text

        # 5. Parse and validate output
        if not final_response_text:
            report = build_deterministic_baseline_profile(self.df)
            return report.model_dump()

        cleaned_json = _clean_json_text(final_response_text)
        parsed_data = json.loads(cleaned_json)
        validated_report = ProfilingReport.model_validate(parsed_data)
        sanitized = _sanitize_gemini_report_against_dataframe(validated_report.model_dump(), self.df)
        # Validate once more after sanitization to keep schema compatibility
        return ProfilingReport.model_validate(sanitized).model_dump()

    async def run(self) -> Dict[str, Any]:
        """
        Executes the agent reasoning loop using Google ADK and Gemini API.
        Falls back to baseline deterministic profiling if no API key is supplied
        or if all keys are exhausted.
        """
        # If no Gemini API key is configured, use baseline deterministic profiler
        if not self.api_keys:
            report = build_deterministic_baseline_profile(self.df)
            res = report.model_dump()
            res["quality_summary"]["engine"] = "deterministic_fallback (Gemini disabled: no API key configured)"
            return res

        if not _GEMINI_SDK_AVAILABLE:
            logger.error("[Gemini] SDK dependencies missing: google-adk/google-genai")
            report = build_deterministic_baseline_profile(self.df)
            res = report.model_dump()
            res["quality_summary"]["engine"] = "deterministic_fallback (Gemini unavailable: missing google-adk/google-genai dependencies)"
            return res

        total_attempts = len(self.api_keys)
        attempts = 0
        temp_failure_count = 0

        while attempts < total_attempts:
            key = self.key_manager.get_next_key()
            if not key:
                logger.warning("[Gemini] All API keys are currently in cooldown")
                break

            attempts += 1
            logger.info("[Gemini] Using API key %s", self.key_manager.key_label(key))

            # Set environment variable for ADK / GenAI for this attempt.
            # Keep only GOOGLE_API_KEY at runtime to avoid ADK warning:
            # "Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY."
            os.environ["GOOGLE_API_KEY"] = key
            os.environ.pop("GEMINI_API_KEY", None)

            try:
                result_dict = await self._run_gemini_once()
                self.key_manager.mark_key_success(key)
                logger.info("[Gemini] API key %s succeeded", self.key_manager.key_label(key))
                result_dict["quality_summary"]["engine"] = f"Google ADK + Gemini ({self.model_name})"
                return result_dict
            except Exception as exc:
                is_temp = _is_temporary_quota_error(exc)
                is_auth = _is_authentication_error(exc)

                if is_temp:
                    context = self.key_manager.extract_error_context(exc)
                    cooldown = self.key_manager.mark_key_failed(key, exc)
                    temp_failure_count += 1
                    logger.warning("[Gemini] API key %s rate limited", self.key_manager.key_label(key))
                    logger.warning("[Gemini] API key %s cooldown: %ss", self.key_manager.key_label(key), cooldown)
                    if context.get("quota_id") or context.get("quota_metric") or context.get("retry_delay_seconds"):
                        logger.warning(
                            "[Gemini] Rate-limit details: quota_id=%s quota_metric=%s retry_delay=%ss",
                            context.get("quota_id") or "n/a",
                            context.get("quota_metric") or "n/a",
                            context.get("retry_delay_seconds") or "n/a",
                        )

                    # Exponential backoff for temporary errors without delaying key switch.
                    if attempts < total_attempts:
                        logger.info("[Gemini] Switching to next available API key")

                    if attempts < total_attempts and temp_failure_count > 1:
                        backoff_seconds = min(4, 2 ** (temp_failure_count - 2))
                        await asyncio.sleep(backoff_seconds)
                    continue

                if is_auth and total_attempts > 1:
                    cooldown = self.key_manager.mark_key_failed(key, exc)
                    logger.warning("[Gemini] API key %s auth error, switching key", self.key_manager.key_label(key))
                    logger.warning("[Gemini] API key %s cooldown: %ss", self.key_manager.key_label(key), cooldown)
                    continue

                # Non quota-related exception: keep previous behavior and fallback immediately.
                logger.error("[Gemini] Non-retriable error with key %s: %s", self.key_manager.key_label(key), type(exc).__name__)
                report = build_deterministic_baseline_profile(self.df)
                res = report.model_dump()
                res["quality_summary"]["engine"] = f"deterministic_fallback ({type(exc).__name__})"
                return res

        logger.warning("[Gemini] All API keys exhausted")
        logger.warning("[Gemini] Using deterministic fallback")
        report = build_deterministic_baseline_profile(self.df)
        res = report.model_dump()
        res["quality_summary"]["engine"] = "deterministic_fallback (all Gemini API keys exhausted or unavailable)"
        return res
