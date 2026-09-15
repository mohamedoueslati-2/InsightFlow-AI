"""
Comprehensive Verification Tests for AI Profiling Agent with Google ADK.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import asyncio
import json
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient

try:
    from services.profiling import (
        ProfilingToolkit,
        AIProfilingAgent,
        build_deterministic_baseline_profile,
        ProfilingReport,
        GeminiKeyManager,
        load_gemini_api_keys,
    )
    from services.profiling.agent import _sanitize_gemini_report_against_dataframe
    from main import app
except ModuleNotFoundError:
    from backend.services.profiling import (
        ProfilingToolkit,
        AIProfilingAgent,
        build_deterministic_baseline_profile,
        ProfilingReport,
        GeminiKeyManager,
        load_gemini_api_keys,
    )
    from backend.services.profiling.agent import _sanitize_gemini_report_against_dataframe
    from backend.main import app


def test_profiling_tools():
    """Verify that all 12 tools return accurate factual metrics."""
    # Synthetic dataset covering diverse scenarios
    data = {
        "id": [1, 2, 3, 4, 5, 5],
        "name": ["Alice", "Bob", "Charlie", "David", "Eve", "Eve"],
        "age": [25, 30, 180, 45, 29, 29],  # Outlier: 180
        "salary": [50000.0, 60000.0, 75000.0, -1000.0, None, None],  # Negative & missing
        "country": ["USA", "US", "United States", "France", "France", "France"],  # Variants
        "email": ["alice@gmail.com", "bob@corp.com", "charlie@test.org", "david@io.co", "invalid-email", "invalid-email"],
        "signup_date": ["2022-01-15", "2023-05-20", "2024-02-10", "1970-01-01", "2026-08-01", "2026-08-01"],
        "all_null": [None, None, None, None, None, None],
    }
    df = pd.DataFrame(data)
    toolkit = ProfilingToolkit(df)

    # 1. Dataset overview
    overview = toolkit.get_dataset_overview()
    assert overview["rows"] == 6
    assert overview["columns_count"] == 8
    assert overview["duplicate_rows"] == 1
    assert overview["total_missing_cells"] == 8

    # 2. Sample rows
    sample = toolkit.get_sample_rows(3)
    assert sample["sample_count"] == 3
    assert len(sample["rows"]) == 3

    # 3. Column inspect
    col_age = toolkit.inspect_column("age")
    assert col_age["is_numeric"] is True
    assert col_age["min"] == 25
    assert col_age["max"] == 180

    # 4. Numeric analysis
    num_age = toolkit.analyze_numeric_column("age")
    assert num_age["min"] == 25
    assert num_age["max"] == 180
    assert num_age["q2_median"] == 29.5

    num_salary = toolkit.analyze_numeric_column("salary")
    assert num_salary["negative_count"] == 1

    # 5. Text analysis
    text_name = toolkit.analyze_text_column("name")
    assert text_name["unique_count"] == 5

    # 6. Categorical analysis
    cat_country = toolkit.analyze_categorical_column("country")
    assert cat_country["cardinality"] == 4
    assert len(cat_country["inconsistent_representations"]) >= 1

    # 7. Date analysis
    date_signup = toolkit.analyze_date_column("signup_date")
    assert date_signup["appears_date_like"] is True
    assert len(date_signup["suspicious_observations"]) >= 1

    # 8. Pattern detection
    pat_email = toolkit.detect_patterns("email")
    patterns = [p["pattern"] for p in pat_email["detected_patterns"]]
    assert "EMAIL" in patterns

    # 9. Missing values detection
    miss = toolkit.detect_missing_values()
    assert "all_null" in miss["completely_empty_columns"]
    assert any(c["column"] == "salary" for c in miss["high_missingness_columns"])

    # 10. Duplicate detection
    dups = toolkit.detect_duplicates()
    assert dups["total_duplicate_rows"] == 1

    # 11. Outlier detection
    outliers_age = toolkit.detect_outliers("age")
    assert outliers_age["total_outliers_count"] >= 1

    # 12. Compare columns
    comp = toolkit.compare_columns("id", "name")
    assert comp["are_identical"] is False
    assert "jaccard_similarity" in comp

    print("All 12 profiling tools verified successfully!")


def test_boolean_columns_are_not_processed_as_numeric():
    """Boolean columns must not trigger numeric arithmetic paths."""
    df = pd.DataFrame({"is_fraud": [True, False, True, None, False]})
    toolkit = ProfilingToolkit(df)

    inspect = toolkit.inspect_column("is_fraud")
    assert inspect["is_boolean"] is True
    assert inspect["is_numeric"] is False

    num = toolkit.analyze_numeric_column("is_fraud")
    assert "error" in num
    assert "Boolean" in num["error"]

    out = toolkit.detect_outliers("is_fraud")
    assert out.get("total_outliers_count", 0) == 0


def test_baseline_profile_schema():
    """Verify that the generated profiling report conforms strictly to the Pydantic schema."""
    df = pd.DataFrame({
        "num": [10, 20, 30, 400],
        "cat": ["A", "B", "a", "C"],
        "txt": ["hello world", "test sample", "random string", "free text here"],
    })
    report = build_deterministic_baseline_profile(df)
    assert isinstance(report, ProfilingReport)
    assert report.dataset.rows == 4
    assert report.dataset.columns == 3
    assert len(report.columns) == 3
    assert report.understanding.confidence > 0.0
    assert report.quality_summary.quality_score is not None

    # Check serialization to dict / JSON
    dumped = report.model_dump()
    json_str = json.dumps(dumped)
    assert len(json_str) > 50
    print("Profiling report schema validated successfully!")


def test_fastapi_endpoints():
    """Verify all FastAPI routes including upload, read, profile, and list."""
    client = TestClient(app)

    # 1. Root
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert "Google ADK" in r_root.json().get("agent", "")

    # 2. Gemini health
    r_health = client.get("/health/gemini")
    assert r_health.status_code == 200
    assert "gemini_configured" in r_health.json()

    # 3. Upload a test CSV
    csv_content = b"user_id,revenue,rating,country\n1,100,4.5,France\n2,250,5.0,USA\n3,50,3.2,US\n4,-20,1.0,France\n5,10000,5.0,Germany\n"
    r_upload = client.post(
        "/upload",
        files={"file": ("test_transactions.csv", csv_content, "text/csv")}
    )
    assert r_upload.status_code == 200
    assert r_upload.json()["rows"] == 5

    # 4. List files
    r_files = client.get("/files")
    assert r_files.status_code == 200
    files_list = r_files.json()
    assert any(f["stem"] == "test_transactions" for f in files_list)

    # 5. Profile dataset
    r_profile = client.post("/profile/csv/test_transactions")
    assert r_profile.status_code == 200
    profile_data = r_profile.json()
    assert profile_data["dataset"]["rows"] == 5
    assert len(profile_data["columns"]) == 4

    # 6. Get saved profile
    r_get_prof = client.get("/profiles/csv/test_transactions")
    assert r_get_prof.status_code == 200
    assert r_get_prof.json()["dataset"]["rows"] == 5

    # 7. List profiles
    r_list_prof = client.get("/profiles")
    assert r_list_prof.status_code == 200
    assert len(r_list_prof.json()) >= 1

    print("FastAPI endpoints verified successfully!")


class _EnvPatch:
    def __init__(self, updates: dict[str, str | None]):
        self.updates = updates
        self.previous: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.updates.items():
            self.previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _tiny_df() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})


def test_multi_key_single_key_success():
    """Test 1 — Single key success."""
    with _EnvPatch({"GEMINI_API_KEYS": "key1", "GEMINI_API_KEY": None}):
        agent = AIProfilingAgent(_tiny_df())
        calls: list[str] = []

        async def fake_once():
            calls.append(os.environ.get("GOOGLE_API_KEY", ""))
            return build_deterministic_baseline_profile(_tiny_df()).model_dump()

        agent._run_gemini_once = fake_once  # type: ignore[attr-defined]
        result = asyncio.run(agent.run())

        assert calls == ["key1"]
        assert "Google ADK + Gemini" in result["quality_summary"]["engine"]


def test_multi_key_first_fails_second_succeeds():
    """Test 2 — Key1 ResourceExhausted, Key2 success."""
    with _EnvPatch({"GEMINI_API_KEYS": "key1,key2", "GEMINI_API_KEY": None}):
        agent = AIProfilingAgent(_tiny_df())
        calls: list[str] = []

        async def fake_once():
            active = os.environ.get("GOOGLE_API_KEY", "")
            calls.append(active)
            if active == "key1":
                raise RuntimeError("ResourceExhausted")
            return build_deterministic_baseline_profile(_tiny_df()).model_dump()

        agent._run_gemini_once = fake_once  # type: ignore[attr-defined]
        result = asyncio.run(agent.run())

        assert calls == ["key1", "key2"]
        assert "Google ADK + Gemini" in result["quality_summary"]["engine"]


def test_multi_key_all_fail_deterministic_fallback():
    """Test 3 — All keys fail with ResourceExhausted => fallback."""
    with _EnvPatch({"GEMINI_API_KEYS": "key1,key2,key3", "GEMINI_API_KEY": None}):
        agent = AIProfilingAgent(_tiny_df())
        calls: list[str] = []

        async def fake_once():
            active = os.environ.get("GOOGLE_API_KEY", "")
            calls.append(active)
            raise RuntimeError("429 Too Many Requests")

        agent._run_gemini_once = fake_once  # type: ignore[attr-defined]
        result = asyncio.run(agent.run())

        assert calls == ["key1", "key2", "key3"]
        assert "deterministic_fallback" in result["quality_summary"]["engine"]


def test_key_manager_cooldown_skips_failed_key():
    """Test 4 — Cooldown prevents immediate key reuse."""
    now = [100.0]

    def time_fn() -> float:
        return now[0]

    manager = GeminiKeyManager(["key1", "key2"], cooldown_seconds=30, time_fn=time_fn)
    first = manager.get_next_key()
    assert first == "key1"

    manager.mark_key_failed("key1", RuntimeError("ResourceExhausted"))
    second = manager.get_next_key()
    assert second == "key2"


def test_key_manager_cooldown_expiration():
    """Test 5 — Key becomes available after cooldown expiration."""
    now = [100.0]

    def time_fn() -> float:
        return now[0]

    manager = GeminiKeyManager(["key1", "key2"], cooldown_seconds=30, time_fn=time_fn)
    manager.mark_key_failed("key1", RuntimeError("429"))

    # During cooldown, key1 should not be selected.
    assert manager.get_next_key() == "key2"

    # After cooldown expiry, key1 becomes available again.
    now[0] = 131.0
    picks = [manager.get_next_key(), manager.get_next_key()]
    assert "key1" in picks


def test_key_manager_uses_retry_delay_when_higher():
    """Extra guardrail — cooldown should respect API retryDelay when longer."""
    now = [100.0]

    def time_fn() -> float:
        return now[0]

    manager = GeminiKeyManager(["key1", "key2"], cooldown_seconds=30, time_fn=time_fn)
    err = RuntimeError(
        "429 RESOURCE_EXHAUSTED ... quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', "
        "quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests', "
        "Please retry in 43.198104105s."
    )
    applied = manager.mark_key_failed("key1", err)
    assert applied >= 43

    context = manager.extract_error_context(err)
    assert context["retry_delay_seconds"] == 43
    assert context["quota_id"] == "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    assert context["quota_metric"] == "generativelanguage.googleapis.com/generate_content_free_tier_requests"

    # Before retry window ends, key1 should still be unavailable.
    now[0] = 135.0
    assert manager.get_next_key() == "key2"


def test_gemini_keys_deduplication():
    """Test 6 — Deduplicate GEMINI_API_KEYS."""
    with _EnvPatch({"GEMINI_API_KEYS": "key1,key2,key1,key2", "GEMINI_API_KEY": None}):
        assert load_gemini_api_keys() == ["key1", "key2"]


def test_gemini_legacy_single_key():
    """Test 7 — Legacy GEMINI_API_KEY is supported."""
    with _EnvPatch({"GEMINI_API_KEYS": None, "GEMINI_API_KEY": "legacy_key1"}):
        assert load_gemini_api_keys() == ["legacy_key1"]


def test_no_key_configured_uses_deterministic_mode():
    """Test 8 — No key configured => deterministic fallback without crash."""
    with _EnvPatch({"GEMINI_API_KEYS": None, "GEMINI_API_KEY": None, "GOOGLE_API_KEY": None}):
        agent = AIProfilingAgent(_tiny_df())
        result = asyncio.run(agent.run())
        assert "deterministic_fallback" in result["quality_summary"]["engine"]


def test_google_api_key_is_not_used_as_source_key():
    """Guardrail — GOOGLE_API_KEY alone must not auto-enable Gemini."""
    with _EnvPatch({"GEMINI_API_KEYS": None, "GEMINI_API_KEY": None, "GOOGLE_API_KEY": "oauth_or_other_token"}):
        assert load_gemini_api_keys() == []


def test_deterministic_planner_summary_present():
    """Planner summary should be present in deterministic understanding text."""
    df = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "amount": [10.0, 20.0, 30.0, 40.0, 50.0],
        "category": ["A", "B", "A", "B", "C"],
        "text": ["x", "y", "z", "u", "v"],
    })

    report = build_deterministic_baseline_profile(df)
    assert "Planner-based investigation used" in report.understanding.description


def test_identifier_dependency_inconsistency_detection():
    """Identifier -> column inconsistency should be surfaced as a quality problem."""
    df = pd.DataFrame({
        "id": ["A", "A", "B", "C"],
        "country": ["France", "USA", "Germany", "Spain"],
        "value": [1, 2, 3, 4],
    })

    report = build_deterministic_baseline_profile(df)
    problem_types = [p.type for p in report.problems]
    assert "inconsistent_identifier_dependency" in problem_types


def test_dataset_level_problem_scope():
    """Dataset-wide issues should use scope='dataset' and column=None."""
    df = pd.DataFrame({
        "id": [1, 1, 2, 3],
        "name": ["a", "a", "b", "c"],
    })
    report = build_deterministic_baseline_profile(df)
    dup = [p for p in report.problems if p.type == "duplicate_records"]
    assert len(dup) >= 1
    assert dup[0].scope == "dataset"
    assert dup[0].column is None


def test_sanitizer_drops_hallucinated_columns_and_normalizes_scope():
    """Sanitizer must drop invented columns and normalize dataset-level scope."""
    df = pd.DataFrame({
        "order_date": ["2024-01-01", "2024-01-02"],
        "amount": [100.0, 120.0],
    })

    fake_gemini = {
        "dataset": {"rows": 999, "columns": 999, "duplicate_rows": 999, "missing_values": 999},
        "understanding": {"description": "x", "confidence": 0.5},
        "columns": [
            {"name": "order_date", "observed_type": "object", "likely_role": "timestamp", "confidence": 0.9, "observations": []},
            {"name": "dataset", "observed_type": "str", "likely_role": "identifier", "confidence": 0.9, "observations": []},
            {"name": "ghost_col", "observed_type": "str", "likely_role": "categorical attribute", "confidence": 0.9, "observations": []},
        ],
        "problems": [
            {"column": "ghost_col", "type": "outliers", "severity": "high", "evidence": "bad", "confidence": 0.9},
            {"column": "dataset", "type": "duplicate_records", "severity": "medium", "evidence": "legacy format", "confidence": 0.9},
            {"scope": "column", "column": "order_date", "type": "outliers", "severity": "medium", "evidence": "many future values", "confidence": 0.8},
        ],
        "investigations": [
            {"column": "ghost_col", "observation": "fake", "examples": []},
            {"column": "order_date", "observation": "ok", "examples": []},
        ],
        "quality_summary": {"missing_values": 1, "duplicate_rows": 1, "potential_issues": 1, "quality_score": 1},
    }

    sanitized = _sanitize_gemini_report_against_dataframe(fake_gemini, df)

    col_names = [c["name"] for c in sanitized["columns"]]
    assert set(col_names) == {"order_date", "amount"}

    # Legacy dataset scope normalization
    dup = [p for p in sanitized["problems"] if p.get("type") == "duplicate_records"]
    assert len(dup) == 1
    assert dup[0].get("scope") == "dataset"
    assert dup[0].get("column") is None

    # Timestamp outlier renaming
    date_issue = [p for p in sanitized["problems"] if p.get("column") == "order_date"]
    assert len(date_issue) == 1
    assert date_issue[0].get("type") == "future_dates"

    # Deterministic dataset facts must be authoritative
    assert sanitized["dataset"]["rows"] == 2
    assert sanitized["dataset"]["columns"] == 2


def test_sanitizer_normalizes_legacy_role_labels():
    """Legacy role labels should be normalized to canonical taxonomy."""
    df = pd.DataFrame({"country": ["TN", "FR"], "profit": [10.0, 12.0]})
    fake = {
        "dataset": {},
        "understanding": {"description": "x", "confidence": 0.5},
        "columns": [
            {"name": "country", "observed_type": "object", "likely_role": "categorical attribute", "confidence": 0.9, "observations": []},
            {"name": "profit", "observed_type": "float64", "likely_role": "monetary value", "confidence": 0.9, "observations": []},
        ],
        "problems": [],
        "investigations": [],
        "quality_summary": {},
    }

    sanitized = _sanitize_gemini_report_against_dataframe(fake, df)
    roles = {c["name"]: c["likely_role"] for c in sanitized["columns"]}
    assert roles["country"] == "categorical"
    assert roles["profit"] == "monetary"


def test_semantic_classification_targets():
    """Semantic role regression targets requested by product requirements."""
    df = pd.DataFrame({
        "record_id": [1001, 1002, 1003, 1004, 1005],
        "country": ["Tunisia", "France", "Japan", "Brazil", "Egypt"],
        "category": ["A", "B", "A", "C", "B"],
        "product": ["P1", "P2", "P3", "P4", "P5"],
        "channel": ["web", "store", "web", "mobile", "store"],
        "customer_email": [
            "a@example.com",
            "b@example.com",
            "c@example.com",
            "d@example.com",
            "e@example.com",
        ],
        "quantity": [1, 2, 3, 4, 5],
        "profit": [12.5, 20.0, 18.0, 32.0, 25.0],
        "discount_pct": [0.10, 0.15, 0.05, 0.0, 0.2],
        "coupon_used": [True, False, True, False, True],
        "order_date": [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
        ],
    })

    report = build_deterministic_baseline_profile(df)
    roles = {c.name: c.likely_role for c in report.columns}

    assert roles.get("record_id") == "identifier"
    assert roles.get("country") == "categorical"
    assert roles.get("category") == "categorical"
    assert roles.get("product") == "categorical"
    assert roles.get("channel") == "categorical"
    assert roles.get("customer_email") == "email"
    assert roles.get("quantity") == "count"
    assert roles.get("profit") == "monetary"
    assert roles.get("discount_pct") == "percentage/ratio"
    assert roles.get("coupon_used") == "boolean"
    assert roles.get("order_date") == "timestamp"


if __name__ == "__main__":
    test_profiling_tools()
    test_boolean_columns_are_not_processed_as_numeric()
    test_baseline_profile_schema()
    test_multi_key_single_key_success()
    test_multi_key_first_fails_second_succeeds()
    test_multi_key_all_fail_deterministic_fallback()
    test_key_manager_cooldown_skips_failed_key()
    test_key_manager_cooldown_expiration()
    test_key_manager_uses_retry_delay_when_higher()
    test_gemini_keys_deduplication()
    test_gemini_legacy_single_key()
    test_no_key_configured_uses_deterministic_mode()
    test_google_api_key_is_not_used_as_source_key()
    test_deterministic_planner_summary_present()
    test_identifier_dependency_inconsistency_detection()
    test_dataset_level_problem_scope()
    test_sanitizer_drops_hallucinated_columns_and_normalizes_scope()
    test_sanitizer_normalizes_legacy_role_labels()
    test_semantic_classification_targets()
    test_fastapi_endpoints()
    print("ALL TESTS PASSED!")
