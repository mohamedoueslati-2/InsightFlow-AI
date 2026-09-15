"""
Comprehensive Verification Tests for the AI Cleaning Agent with Google ADK.
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
    from services.cleaning import (
        CleaningToolkit,
        CleaningAgent,
        build_deterministic_baseline_cleaning,
        CleaningReport,
    )
    from services.profiling import build_deterministic_baseline_profile, GeminiKeyManager, load_gemini_api_keys
    from main import app
except ModuleNotFoundError:
    from backend.services.cleaning import (
        CleaningToolkit,
        CleaningAgent,
        build_deterministic_baseline_cleaning,
        CleaningReport,
    )
    from backend.services.profiling import build_deterministic_baseline_profile, GeminiKeyManager, load_gemini_api_keys
    from backend.main import app


def _profile(df: pd.DataFrame) -> dict:
    return build_deterministic_baseline_profile(df).model_dump()


# ---------------------------------------------------------------------------
# Tier 1 mutation tools
# ---------------------------------------------------------------------------

def test_drop_exact_duplicates():
    df = pd.DataFrame({"a": [1, 2, 2, 3], "b": ["x", "y", "y", "z"]})
    toolkit = CleaningToolkit(df)
    res = toolkit.drop_exact_duplicates()
    assert res["kept"] is True
    assert len(toolkit.df) == 3
    assert toolkit.df.duplicated().sum() == 0

    # Precondition failure: no duplicates left
    res2 = toolkit.drop_exact_duplicates()
    assert res2["kept"] is False
    assert res2["outcome"] == "precondition_failed"


def test_trim_whitespace():
    df = pd.DataFrame({"country": [" Tunisia ", "France", "  Egypt", None]})
    toolkit = CleaningToolkit(df)
    res = toolkit.trim_whitespace("country")
    assert res["kept"] is True
    assert toolkit.df["country"].tolist()[0] == "Tunisia"
    assert toolkit.df["country"].tolist()[2] == "Egypt"
    assert pd.isna(toolkit.df["country"].tolist()[3])

    # Precondition failure: nothing left to trim
    res2 = toolkit.trim_whitespace("country")
    assert res2["kept"] is False
    assert res2["outcome"] == "precondition_failed"


def test_normalize_case():
    df = pd.DataFrame({"country": ["TUNISIA", "tunisia", "Tunisia", "France"]})
    toolkit = CleaningToolkit(df)
    res = toolkit.normalize_case("country", target_case="lower")
    assert res["kept"] is True
    assert set(toolkit.df["country"].unique()) == {"tunisia", "france"}


def test_canonicalize_categories():
    df = pd.DataFrame({"country": ["USA", "US", "United States", "France"]})
    toolkit = CleaningToolkit(df)
    mapping = {"US": "USA", "United States": "USA"}
    res = toolkit.canonicalize_categories("country", mapping)
    assert res["kept"] is True
    assert toolkit.df["country"].tolist().count("USA") == 3

    # Precondition failure: unknown key not present in column
    res2 = toolkit.canonicalize_categories("country", {"Ghost Value": "USA"})
    assert res2["kept"] is False
    assert res2["outcome"] == "precondition_failed"


def test_cast_column_type_numeric_looking_strings():
    df = pd.DataFrame({"amount": ["10.5", "20.1", "30.0"]})
    toolkit = CleaningToolkit(df)
    res = toolkit.cast_column_type("amount", "float")
    assert res["kept"] is True
    assert pd.api.types.is_float_dtype(toolkit.df["amount"])


def test_cast_column_type_refuses_lossy_cast():
    df = pd.DataFrame({"mixed": ["10", "abc", "30"]})
    toolkit = CleaningToolkit(df)
    res = toolkit.cast_column_type("mixed", "float")
    assert res["kept"] is False
    assert res["outcome"] == "precondition_failed"
    # Original values must remain untouched
    assert toolkit.df["mixed"].tolist() == ["10", "abc", "30"]


# ---------------------------------------------------------------------------
# Tier 2 mutation tools
# ---------------------------------------------------------------------------

def test_flag_domain_implausible_does_not_mutate_values():
    df = pd.DataFrame({"age": [25, 30, 180, 45, -5]})
    toolkit = CleaningToolkit(df)
    res = toolkit.flag_domain_implausible("age", method="bounds", lower_bound=0, upper_bound=120)
    assert res["kept"] is True
    assert toolkit.df["age"].tolist() == [25, 30, 180, 45, -5]
    assert "age_domain_implausible" in toolkit.df.columns
    assert toolkit.df["age_domain_implausible"].tolist() == [False, False, True, False, True]


def test_cap_outliers_records_originals_and_stays_in_bounds():
    df = pd.DataFrame({"salary": [50000, 52000, 51000, 100000000, 49000]})
    toolkit = CleaningToolkit(df)
    res = toolkit.cap_outliers("salary", method="iqr")
    assert res["kept"] is True
    assert len(toolkit.df) == 5
    validation = res["validation"]
    assert "audit" in validation
    assert "original_values" in validation["audit"]
    assert len(validation["audit"]["original_values"]) >= 1
    lb, ub = validation["audit"]["bounds"]
    assert toolkit.df["salary"].max() <= ub + 1e-6
    assert toolkit.df["salary"].min() >= lb - 1e-6


def test_impute_missing_tags_cells_and_uses_column_strategy():
    df = pd.DataFrame({"score": [10.0, 20.0, None, 40.0, None]})
    toolkit = CleaningToolkit(df)
    res = toolkit.impute_missing("score", strategy="median")
    assert res["kept"] is True
    assert toolkit.df["score"].isna().sum() == 0
    assert "score_was_imputed" in toolkit.df.columns
    assert toolkit.df["score_was_imputed"].sum() == 2
    assert toolkit.df.loc[toolkit.df["score_was_imputed"], "score"].tolist() == [20.0, 20.0]


def test_impute_missing_refuses_identifier_like_column():
    df = pd.DataFrame({
        "customer_id": [f"C{i}" if i != 10 else None for i in range(30)],
    })
    toolkit = CleaningToolkit(df)
    res = toolkit.impute_missing("customer_id", strategy="mode")
    assert res["kept"] is False
    assert res["outcome"] == "precondition_failed"
    assert toolkit.df["customer_id"].isna().sum() == 1


def test_detect_and_correct_systematic_date_offset_fires_on_systematic_pattern():
    # 20 normal past dates + 10 dates all shifted exactly +8 years (systematic bug)
    normal_dates = [f"2023-01-{d:02d}" for d in range(1, 21)]
    shifted_dates = [f"{2023 + 8}-02-{d:02d}" for d in range(1, 11)]
    df = pd.DataFrame({"event_date": normal_dates + shifted_dates})
    toolkit = CleaningToolkit(df)
    res = toolkit.detect_and_correct_systematic_date_offset("event_date")
    assert res["kept"] is True
    now = pd.Timestamp.now()
    corrected = pd.to_datetime(toolkit.df["event_date"], errors="coerce")
    assert int((corrected > now).sum()) == 0


def test_detect_and_correct_systematic_date_offset_no_op_on_scattered_future_dates():
    # Mostly normal dates, plus 2 scattered/inconsistent future dates (no shared offset)
    normal_dates = [f"2023-01-{d:02d}" for d in range(1, 21)]
    scattered_future = ["2099-05-01", "2150-11-20"]
    df = pd.DataFrame({"event_date": normal_dates + scattered_future})
    toolkit = CleaningToolkit(df)
    res = toolkit.detect_and_correct_systematic_date_offset("event_date")
    assert res["kept"] is False
    assert res["outcome"] == "no_correction_applicable"
    # Values must remain untouched
    assert toolkit.df["event_date"].tolist() == normal_dates + scattered_future

    res2 = toolkit.flag_temporal_anomaly("event_date")
    assert res2["kept"] is True
    assert "event_date_temporal_anomaly" in toolkit.df.columns
    assert int(toolkit.df["event_date_temporal_anomaly"].sum()) == 2


# ---------------------------------------------------------------------------
# Manual (human-in-the-loop) resolution tools
# ---------------------------------------------------------------------------

def test_manual_fill_missing_uses_user_provided_value():
    df = pd.DataFrame({"customer_email": ["a@x.com", None, "c@x.com", None]})
    toolkit = CleaningToolkit(df)
    res = toolkit.manual_fill_missing("customer_email", "unknown@example.com")
    assert res["kept"] is True
    assert res["action"]["resolved_by"] == "user"
    assert toolkit.df["customer_email"].isna().sum() == 0
    assert (toolkit.df["customer_email"] == "unknown@example.com").sum() == 2
    assert "customer_email_manually_filled" in toolkit.df.columns
    assert toolkit.df["customer_email_manually_filled"].sum() == 2


def test_manual_fill_missing_works_on_identifier_like_column():
    # The automatic impute_missing tool refuses identifier-like columns; the
    # manual tool must still allow it since it's an explicit human decision.
    df = pd.DataFrame({"customer_id": [f"C{i}" for i in range(29)] + [None]})
    toolkit = CleaningToolkit(df)
    res = toolkit.manual_fill_missing("customer_id", "UNKNOWN")
    assert res["kept"] is True
    assert toolkit.df["customer_id"].isna().sum() == 0


def test_manual_drop_rows_with_missing():
    df = pd.DataFrame({"profit": [10.0, None, 30.0, None, 50.0]})
    toolkit = CleaningToolkit(df)
    res = toolkit.manual_drop_rows_with_missing("profit")
    assert res["kept"] is True
    assert len(toolkit.df) == 3
    assert toolkit.df["profit"].isna().sum() == 0

    res2 = toolkit.manual_drop_rows_with_missing("profit")
    assert res2["kept"] is False
    assert res2["outcome"] == "precondition_failed"


def test_manual_tools_are_not_exposed_to_the_llm_toolbox():
    df = pd.DataFrame({"a": [1, None]})
    toolkit = CleaningToolkit(df)
    tool_names = {t.__name__ for t in toolkit.get_tools()}
    assert "manual_fill_missing" not in tool_names
    assert "manual_drop_rows_with_missing" not in tool_names


# ---------------------------------------------------------------------------
# Rollback mechanics
# ---------------------------------------------------------------------------

def test_rollback_last_restores_snapshot():
    df = pd.DataFrame({"a": [1, 2, 2, 3]})
    toolkit = CleaningToolkit(df)
    before_snapshot = toolkit.df.copy()
    res = toolkit.drop_exact_duplicates()
    assert res["kept"] is True
    assert len(toolkit.df) == 3

    rollback_res = toolkit.rollback_last()
    assert rollback_res["success"] is True
    assert toolkit.df.equals(before_snapshot)
    assert len(toolkit.actions) == 0


def test_failed_validation_never_commits_change():
    # Force a scenario where mutate produces a result that would fail validation:
    # cast_column_type with an ambiguous column is refused at precondition, so the
    # dataframe must be byte-identical to before the call.
    df = pd.DataFrame({"mixed": ["10", "N/A", "30"]})
    toolkit = CleaningToolkit(df)
    snapshot = toolkit.df.copy()
    res = toolkit.cast_column_type("mixed", "int")
    assert res["kept"] is False
    assert toolkit.df.equals(snapshot)


# ---------------------------------------------------------------------------
# Bounded retry / attempt budget
# ---------------------------------------------------------------------------

def test_attempt_budget_exhausts_and_reports_budget_exhausted():
    df = pd.DataFrame({"mixed": ["10", "abc", "30"]})
    toolkit = CleaningToolkit(df, max_attempts_per_problem=2)

    r1 = toolkit.cast_column_type("mixed", "float", problem="type_mismatch")
    r2 = toolkit.cast_column_type("mixed", "int", problem="type_mismatch")
    r3 = toolkit.cast_column_type("mixed", "float", problem="type_mismatch")

    assert r1["outcome"] == "precondition_failed"
    assert r2["outcome"] == "precondition_failed"
    assert r3["outcome"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# Dataset-agnostic behavior (no hard-coded column names)
# ---------------------------------------------------------------------------

def test_deterministic_cleaning_is_dataset_agnostic():
    df_a = pd.DataFrame({
        "zorp_field": [" alpha ", "ALPHA", "alpha", "beta", "beta"],
        "quantity_x": [1, 2, 3, 4, 4],
    })
    df_a = pd.concat([df_a, df_a.iloc[[0]]], ignore_index=True)  # inject a duplicate row

    df_b = pd.DataFrame({
        "wibble_status": [" open ", "OPEN", "open", "closed", "closed"],
        "count_y": [10, 20, 30, 40, 40],
    })
    df_b = pd.concat([df_b, df_b.iloc[[0]]], ignore_index=True)

    for df in (df_a, df_b):
        profile = _profile(df)
        report, cleaned_df = build_deterministic_baseline_cleaning(df, profile)
        assert report["changes_applied"] >= 1
        assert cleaned_df.duplicated().sum() == 0
        assert isinstance(report["summary"], str) and len(report["summary"]) > 0


# ---------------------------------------------------------------------------
# Quality score consistency
# ---------------------------------------------------------------------------

def test_quality_before_equals_after_when_unchanged():
    df = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "value": [10.0, 20.0, 30.0, 40.0, 50.0],
    })
    profile = _profile(df)
    report, cleaned_df = build_deterministic_baseline_cleaning(df, profile)
    if report["status"] == "unchanged":
        assert report["quality_before"] == report["quality_after"]


def test_cleaning_report_schema_validates():
    df = pd.DataFrame({
        "id": [1, 2, 2, 3],
        "country": [" Tunisia ", "TUNISIA", "tunisia", "France"],
        "age": [25, 30, 180, 45],
    })
    profile = _profile(df)
    report, cleaned_df = build_deterministic_baseline_cleaning(df, profile)
    validated = CleaningReport.model_validate(report)
    assert validated.status in {"cleaned", "partially_cleaned", "unchanged"}
    dumped = validated.model_dump()
    json_str = json.dumps(dumped)
    assert len(json_str) > 20


# ---------------------------------------------------------------------------
# Multi-key fallback / deterministic fallback (shared key manager)
# ---------------------------------------------------------------------------

class _EnvPatch:
    def __init__(self, updates: dict):
        self.updates = updates
        self.previous: dict = {}

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
    return pd.DataFrame({"a": [1, 2, 2], "b": ["x", "y", "y"]})


def test_no_key_configured_uses_deterministic_cleaning_fallback():
    # Retained deterministic implementation remains independently regression-tested.
    from backend.services.cleaning.legacy_agent import CleaningAgent
    with _EnvPatch({"GEMINI_API_KEYS": None, "GEMINI_API_KEY": None, "GOOGLE_API_KEY": None}):
        df = _tiny_df()
        profile = _profile(df)
        agent = CleaningAgent(df, profile)
        result = asyncio.run(agent.run())
        assert "deterministic_fallback" in result["validation"]["engine"]
        assert agent.cleaned_df.duplicated().sum() == 0


def test_multi_key_first_fails_second_succeeds_cleaning():
    from backend.services.cleaning.legacy_agent import CleaningAgent
    with _EnvPatch({"GEMINI_API_KEYS": "key1,key2", "GEMINI_API_KEY": None}):
        df = _tiny_df()
        profile = _profile(df)
        agent = CleaningAgent(df, profile)
        calls = []

        async def fake_once():
            active = os.environ.get("GOOGLE_API_KEY", "")
            calls.append(active)
            if active == "key1":
                raise RuntimeError("ResourceExhausted")
            report, final_df = build_deterministic_baseline_cleaning(df, profile)
            agent.cleaned_df = final_df
            return report

        agent._run_gemini_once = fake_once
        result = asyncio.run(agent.run())

        assert calls == ["key1", "key2"]
        assert "Google ADK + Gemini" in result["validation"]["engine"]


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------

def test_fastapi_cleaning_endpoints():
    client = TestClient(app)

    csv_content = (
        b"user_id,country,revenue\n"
        b"1, France ,100\n"
        b"2,FRANCE,250\n"
        b"3,france,50\n"
        b"4,Germany,-20\n"
        b"4,Germany,-20\n"
    )
    r_upload = client.post("/upload", files={"file": ("test_cleaning_ds.csv", csv_content, "text/csv")})
    assert r_upload.status_code == 200

    r_clean = client.post("/clean/csv/test_cleaning_ds")
    assert r_clean.status_code == 200
    report = r_clean.json()
    assert "status" in report
    assert "summary" in report

    r_get = client.get("/cleanings/csv/test_cleaning_ds")
    assert r_get.status_code == 200
    assert r_get.json()["status"] == report["status"]

    r_files = client.get("/files")
    assert r_files.status_code == 200
    files_list = r_files.json()
    entry = next(f for f in files_list if f["stem"] == "test_cleaning_ds")
    assert entry["has_cleaning"] is True


def test_fastapi_issue_message_endpoint_conversational_resolution():
    """
    The remaining-issue resolution endpoint is conversational and agent-driven,
    not a fixed action button: the user sends a free-text message, and the
    agent (Gemini, or the deterministic heuristic fallback when no key is
    configured) decides which toolbox tool to call. We force the deterministic
    fallback here for a predictable, fast assertion.
    """
    client = TestClient(app)

    # customer_email and order_reference are both identifier-like/high-cardinality,
    # so the automatic agent must refuse to impute them and leave them in
    # remaining_issues -- exactly the case this conversational endpoint targets.
    csv_content = (
        b"customer_email,order_reference,filler\n"
        b"a@x.com,REF-0001,1\n"
        b",REF-0002,2\n"
        b"c@x.com,,3\n"
        b"d@x.com,REF-0004,4\n"
    )
    r_upload = client.post("/upload", files={"file": ("test_issue_conv_ds.csv", csv_content, "text/csv")})
    assert r_upload.status_code == 200

    with _EnvPatch({"GEMINI_API_KEYS": None, "GEMINI_API_KEY": None, "GOOGLE_API_KEY": None}):
        r_clean = client.post("/clean/csv/test_issue_conv_ds")
        assert r_clean.status_code == 200
        cleaned_report = r_clean.json()

        email_issue = next(i for i in cleaned_report["remaining_issues"] if i["column"] == "customer_email")
        ref_issue = next(i for i in cleaned_report["remaining_issues"] if i["column"] == "order_reference")

        # Propose a value in plain language -> deterministic fallback treats the
        # message itself as the fill value (no "supprim/delete/drop" keyword).
        r_msg1 = client.post(
            f"/cleanings/csv/test_issue_conv_ds/issues/message",
            json={"issue_id": email_issue["id"], "message": "unknown@example.com"},
        )
        assert r_msg1.status_code == 200
        report1 = r_msg1.json()
        # V2: text never commits; without a provider there is no generated preview.
        assert report1["actions"] == []
        assert any(i["id"] == email_issue["id"] for i in report1["remaining_issues"])
        assert email_issue["id"] in report1["issue_conversations"]
        assert report1["issue_conversations"][email_issue["id"]][0]["role"] == "user"

        # Ask for rows to be removed -> deterministic fallback detects the keyword.
        r_msg2 = client.post(
            f"/cleanings/csv/test_issue_conv_ds/issues/message",
            json={"issue_id": ref_issue["id"], "message": "supprime les lignes concernées"},
        )
        assert r_msg2.status_code == 200
        report2 = r_msg2.json()
        assert report2["actions"] == []

        # Unknown issue id
        r_bad = client.post(
            f"/cleanings/csv/test_issue_conv_ds/issues/message",
            json={"issue_id": "column:does_not_exist:missing_values", "message": "test"},
        )
        assert r_bad.status_code == 404

        # Empty message
        r_empty = client.post(
            f"/cleanings/csv/test_issue_conv_ds/issues/message",
            json={"issue_id": ref_issue["id"], "message": "   "},
        )
        assert r_empty.status_code == 400

    # Unknown file (outside the env patch, doesn't matter)
    r_missing = client.post(
        "/cleanings/csv/no_such_stem/issues/message",
        json={"issue_id": "column:x:missing_values", "message": "test"},
    )
    assert r_missing.status_code == 404


if __name__ == "__main__":
    test_drop_exact_duplicates()
    test_trim_whitespace()
    test_normalize_case()
    test_canonicalize_categories()
    test_cast_column_type_numeric_looking_strings()
    test_cast_column_type_refuses_lossy_cast()
    test_flag_domain_implausible_does_not_mutate_values()
    test_cap_outliers_records_originals_and_stays_in_bounds()
    test_impute_missing_tags_cells_and_uses_column_strategy()
    test_impute_missing_refuses_identifier_like_column()
    test_detect_and_correct_systematic_date_offset_fires_on_systematic_pattern()
    test_detect_and_correct_systematic_date_offset_no_op_on_scattered_future_dates()
    test_manual_fill_missing_uses_user_provided_value()
    test_manual_fill_missing_works_on_identifier_like_column()
    test_manual_drop_rows_with_missing()
    test_manual_tools_are_not_exposed_to_the_llm_toolbox()
    test_rollback_last_restores_snapshot()
    test_failed_validation_never_commits_change()
    test_attempt_budget_exhausts_and_reports_budget_exhausted()
    test_deterministic_cleaning_is_dataset_agnostic()
    test_quality_before_equals_after_when_unchanged()
    test_cleaning_report_schema_validates()
    test_no_key_configured_uses_deterministic_cleaning_fallback()
    test_multi_key_first_fails_second_succeeds_cleaning()
    test_fastapi_cleaning_endpoints()
    test_fastapi_issue_message_endpoint_conversational_resolution()
    print("ALL CLEANING TESTS PASSED!")
