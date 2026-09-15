"""
Google ADK (Agent Development Kit) Cleaning Agent Implementation.

Mirrors the architecture of services/profiling/agent.py: Gemini reasons over
deterministic Pandas tool output and selects/sequences operations from a fixed
toolbox; the toolbox itself performs the actual mutation and the deterministic
validator decides whether to keep or reject each change. Gemini never writes
or executes arbitrary transformation code, and its self-reported confidence is
never the acceptance criterion.

Reuses the profiling service's Gemini key manager, model resolution, and
retry/fallback machinery rather than building a second one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import copy
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

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

from ..profiling.agent import (
    get_gemini_key_manager,
    get_gemini_model_name,
    _is_temporary_quota_error,
    _is_authentication_error,
    _clean_json_text,
    _compute_quality_components,
)
from ..profiling.tools import ProfilingToolkit
from ..profiling.schemas import QualityProblem as ProfilingQualityProblem

from .legacy_prompts import CLEANING_SYSTEM_INSTRUCTION, USER_START_PROMPT, ISSUE_RESOLUTION_SYSTEM_INSTRUCTION
from .schemas import CleaningReport
from .tools import CleaningToolkit
from .chat_intent import parse_edit, is_keep_decision
from .proposals import CATALOG, preview, execute, fingerprint
from .validator import build_plain_language_summary, compute_report_status, make_issue_id

logger = logging.getLogger(__name__)

_MAX_PROFILE_JSON_CHARS = 20000


def _dataset_level_validation(original_df: pd.DataFrame, final_df: pd.DataFrame) -> Dict[str, Any]:
    before = ProfilingToolkit(original_df).get_dataset_overview()
    after = ProfilingToolkit(final_df).get_dataset_overview()
    return {
        "rows_before": before.get("rows"),
        "rows_after": after.get("rows"),
        "columns_before": before.get("columns_count"),
        "columns_after": after.get("columns_count"),
        "duplicate_rows_before": before.get("duplicate_rows"),
        "duplicate_rows_after": after.get("duplicate_rows"),
        "missing_cells_before": before.get("total_missing_cells"),
        "missing_cells_after": after.get("total_missing_cells"),
    }


def compute_quality_after(final_df: pd.DataFrame, remaining_issues: List[Dict[str, Any]]) -> float:
    toolkit = ProfilingToolkit(final_df)
    overview = toolkit.get_dataset_overview()
    duplicates = toolkit.detect_duplicates()

    problems: List[ProfilingQualityProblem] = []
    for issue in remaining_issues:
        try:
            problems.append(ProfilingQualityProblem(
                scope=issue.get("scope", "column"),
                column=issue.get("column"),
                type=str(issue.get("type", "issue")),
                severity=str(issue.get("severity", "medium")),
                evidence=str(issue.get("evidence", "")),
                confidence=float(issue.get("confidence", 0.8) or 0.8),
            ))
        except Exception:
            continue

    q = _compute_quality_components(
        rows=int(overview.get("rows", 0)),
        cols=int(overview.get("columns_count", 0)),
        missing_cells=int(overview.get("total_missing_cells", 0)),
        duplicate_rows=int(duplicates.get("total_duplicate_rows", 0)),
        problems=problems,
    )
    return float(q["overall"])


def _issue_key(issue):
    category = issue.get("type", "issue")
    if category == "high_missingness":
        category = "missing_values"
    return (issue.get("scope", "column"), issue.get("column"), category)


def refresh_cleaning_report(report: Dict[str, Any], df: pd.DataFrame) -> Dict[str, Any]:
    """Reconcile stored findings and scores with real data, without editing data."""
    result = copy.deepcopy(report)
    conversations = result.setdefault("issue_conversations", {})
    # Upgrade old reports where an explicit keep decision was only saved as chat.
    accepted_ids = {i.get("id") for i in result.get("accepted_issues", [])}
    for issue in result.get("remaining_issues", []) or []:
        user_turns = [t for t in conversations.get(issue.get("id"), []) if t.get("role") == "user"]
        if user_turns and is_keep_decision(user_turns[-1].get("text", "")) and issue.get("id") not in accepted_ids:
            accepted_issue = dict(issue, review_status="accepted", review_reason=user_turns[-1]["text"])
            result.setdefault("accepted_issues", []).append(accepted_issue)
            accepted_ids.add(issue.get("id"))
    accepted_by_key = {_issue_key(i): i for i in result.get("accepted_issues", [])}
    merged = {}
    for raw in list(result.get("accepted_issues", [])) + list(result.get("remaining_issues", []) or []):
        issue = dict(raw)
        key = _issue_key(issue)
        issue["type"] = key[2]
        issue.setdefault("id", make_issue_id(*key))
        if key in merged:
            primary = merged[key]
            if issue["id"] != primary["id"] and issue["id"] in conversations:
                conversations.setdefault(primary["id"], []).extend(conversations.pop(issue["id"]))
            proposals = result.get("issue_proposals", {})
            if issue["id"] != primary["id"] and issue["id"] in proposals:
                proposals.setdefault(primary["id"], []).extend(proposals.pop(issue["id"]))
            weights = {"low": 0, "medium": 1, "high": 2}
            if weights.get(issue.get("severity"), 1) > weights.get(primary.get("severity"), 1):
                primary["severity"] = issue["severity"]
            continue
        merged[key] = issue

    remaining = []
    for issue in merged.values():
        column = issue.get("column")
        count = None
        label = ""
        category = _profiling_type_to_cleaning_problem(issue.get("type", ""))
        matching_actions = [a for a in result.get("actions", [])
                            if a.get("column") == column
                            and _profiling_type_to_cleaning_problem(a.get("problem", "")) == category
                            and not str(a.get("action", "")).startswith("flag_")]
        if column in df.columns:
            if category == "missing_values":
                count = int(df[column].isna().sum())
                label = "valeur(s) manquante(s)"
            elif issue.get("type") == "future_dates":
                dates = pd.to_datetime(df[column], errors="coerce", utc=True)
                count = int((dates > pd.Timestamp.now(tz="UTC")).sum())
                label = "date(s) future(s)"
            elif category == "outliers" and re.search(r'negativ|négativ', issue.get('evidence', ''), re.I) and any(
                a.get("action") == "absolute_negative_values" for a in matching_actions
            ):
                count = int((pd.to_numeric(df[column], errors="coerce") < 0).sum())
                label = "valeur(s) négative(s)"
            elif category == "outliers" and any(
                a.get("action") == "cap_outliers" and a.get("column") == column
                for a in result.get("actions", [])
            ):
                measured = ProfilingToolkit(df).detect_outliers(column)
                if "error" not in measured:
                    count = measured["total_outliers_count"]
                    label = "valeur(s) extrême(s) selon l’IQR"
            elif matching_actions:
                from .reporting import measure_issue
                count = measure_issue(df, issue)
        elif issue.get("scope") == "dataset" and issue["type"] == "duplicate_records":
            count = int(df.duplicated().sum())
            label = "doublon(s) exact(s)"
        if count is not None:
            if count == 0:
                continue
            percent = round(100 * count / len(df), 2) if len(df) else 0.0
            issue.update(affected_rows=count, affected_percent=percent,
                         evidence=f"{count} {label} sur {len(df)} lignes ({percent:.2f} %) dans « {column} ».")
        remaining.append(issue)
    accepted = [i for i in remaining if _issue_key(i) in accepted_by_key]
    measured_issues = remaining
    remaining = [i for i in remaining if _issue_key(i) not in accepted_by_key]
    result["remaining_issues"] = remaining
    result["accepted_issues"] = accepted
    actions = result.get("actions", []) or []
    result["changes_applied"] = len(actions)
    result["rows_affected"] = sum(int(a.get("rows_affected", 0)) for a in actions)
    result["status"] = compute_report_status(actions, remaining)
    result["summary"] = build_plain_language_summary(actions, remaining, result["status"])
    if accepted:
        if not remaining:
            result["status"] = "reviewed"
            result["summary"] = "Revue terminée : aucun problème en attente de décision."
        result["summary"] += f" {len(accepted)} problème(s) accepté(s) en l’état ; les valeurs correspondantes sont conservées."
    validation = result.setdefault("validation", {})
    columns = validation.get("quality_columns")
    if columns is None:
        # Legacy reports: exclude only audit columns associated with recorded tools.
        suffixes = {"impute_missing": "_was_imputed", "manual_fill_missing": "_manually_filled",
                    "flag_temporal_anomaly": "_temporal_anomaly", "flag_domain_implausible": "_domain_implausible"}
        audit = {str(a.get("column")) + suffixes[a["action"]]
                 for a in actions if a.get("action") in suffixes}
        audit.update(a.get("validation", {}).get("audit", {}).get("audit_column") for a in actions)
        columns = [c for c in df.columns if c not in audit]
    columns = [c for c in columns if c in df.columns]
    validation["quality_columns"] = columns
    business_df = df[columns]
    result["quality_after"] = compute_quality_after(business_df, measured_issues)
    validation["rows_after"] = len(df)
    validation["columns_after"] = len(df.columns)
    validation["missing_cells_after"] = int(business_df.isna().sum().sum())
    validation["duplicate_rows_after"] = int(business_df.duplicated().sum())
    synthetic_columns = {a.get("validation", {}).get("audit", {}).get("audit_column")
                         for a in actions if a.get("action") in {
                             "generate_synthetic_emails", "replace_exact_with_synthetic_emails"
                         }}
    validation["synthetic_cells"] = sum(int(df[c].fillna(False).astype(bool).sum())
                                        for c in synthetic_columns if c in df.columns)
    if result.get("issue_proposals"):
        current_version = fingerprint(df)
        for items in result["issue_proposals"].values():
            for proposal in items:
                if proposal.get("status") == "ready" and proposal.get("fingerprint") != current_version:
                    proposal["status"] = "stale"
    return result


_PROFILING_TYPE_TO_CLEANING_PROBLEM = {
    "high_missingness": "missing_values",
    "severe_row_missingness": "missing_values",
    "semantic_invalid_value": "outliers",
    "suspicious_negative_values": "outliers",
    "future_dates": "temporal_anomaly",
    "suspicious_values": "outliers",
    "domain_implausible": "outliers",
}


def _profiling_type_to_cleaning_problem(profiling_type: str) -> str:
    """Profiling and cleaning use different vocabularies for the same problem
    (e.g. profiling's 'high_missingness' vs the cleaning action's
    'missing_values'). Normalize so 'was this already addressed?' checks work."""
    return _PROFILING_TYPE_TO_CLEANING_PROBLEM.get(profiling_type, profiling_type)


def _issue_from_problem(problem: Dict[str, Any], reason_not_fixed: str) -> Dict[str, Any]:
    scope = problem.get("scope", "column")
    column = problem.get("column")
    p_type = str(problem.get("type", "issue"))
    return {
        "id": make_issue_id(scope, column, p_type),
        "scope": scope,
        "column": column,
        "type": p_type,
        "severity": str(problem.get("severity", "medium")),
        "evidence": str(problem.get("evidence", "")),
        "confidence": float(problem.get("confidence", 0.8) or 0.8),
        "reason_not_fixed": reason_not_fixed,
    }


def _sanitize_remaining_issues(raw_list: Optional[List[Dict[str, Any]]], actual_cols: set) -> List[Dict[str, Any]]:
    safe: List[Dict[str, Any]] = []
    for item in raw_list or []:
        scope = str(item.get("scope", "column")).lower()
        col = item.get("column")
        if scope == "dataset":
            col = None
        else:
            if col is not None and str(col) not in actual_cols:
                continue
        p_type = str(item.get("type", "issue"))
        scope_final = "dataset" if scope == "dataset" else "column"
        safe.append({
            "id": make_issue_id(scope_final, col, p_type),
            "scope": scope_final,
            "column": col,
            "type": p_type,
            "severity": str(item.get("severity", "medium")),
            "evidence": str(item.get("evidence", "")),
            "confidence": float(item.get("confidence", 0.8) or 0.8),
            "reason_not_fixed": str(item.get("reason_not_fixed", "")),
        })
    return safe


def _finalize_cleaning_result(
    original_df: pd.DataFrame,
    toolkit: CleaningToolkit,
    profiling_report: Dict[str, Any],
    proposed_remaining_issues: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Builds the final report dict. `actions` always comes exclusively from the
    toolkit's own execution log (Python-validated), never from the LLM's
    self-reported text. `remaining_issues` comes from the LLM when available
    (sanitized against the real schema), otherwise synthesized deterministically
    from whichever profiling problems no committed action addressed.
    """
    actual_cols = {str(c) for c in toolkit.df.columns} | {str(c) for c in original_df.columns}
    actions = list(toolkit.actions)

    remaining_issues = _sanitize_remaining_issues(proposed_remaining_issues, actual_cols)
    existing_ids = {issue["id"] for issue in remaining_issues}
    addressed_keys = {
        (a.get("column"), a.get("problem")) for a in actions
        if not str(a.get("action", "")).startswith("flag_")
    }
    for problem in profiling_report.get("problems", []) or []:
        column = problem.get("column")
        category = _profiling_type_to_cleaning_problem(str(problem.get("type", "")))
        addressed = (column, category) in addressed_keys
        if category == "missing_values" and column in toolkit.df.columns:
            addressed = not toolkit.df[column].isna().any()
        if addressed:
            continue
        issue = _issue_from_problem(problem, "No verified correction resolved this finding; a flag alone is not a correction.")
        if issue["id"] not in existing_ids:
            remaining_issues.append(issue)
            existing_ids.add(issue["id"])

    status = compute_report_status(actions, remaining_issues)
    summary = build_plain_language_summary(actions, remaining_issues, status)

    quality_before = float(profiling_report.get("quality_summary", {}).get("quality_score", 0.0) or 0.0)
    quality_after = compute_quality_after(toolkit.df, remaining_issues)

    report = {
        "status": status,
        "summary": summary,
        "changes_applied": len(actions),
        "rows_affected": sum(int(a.get("rows_affected", 0)) for a in actions),
        "actions": actions,
        "remaining_issues": remaining_issues,
        "validation": _dataset_level_validation(original_df, toolkit.df),
        "quality_before": quality_before,
        "quality_after": quality_after,
        "steps": toolkit.operation_log,
        "issue_conversations": {},
    }
    report["validation"]["quality_columns"] = list(original_df.columns)
    return refresh_cleaning_report(report, toolkit.df)


def build_deterministic_baseline_cleaning(df: pd.DataFrame, profiling_report: Dict[str, Any]) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """
    Deterministic, Gemini-free cleaner used as fallback. Only performs
    mechanically-safe Tier 1 operations plus Tier 2 flag/impute operations that
    the toolkit itself can justify and validate from tool output alone.
    """
    toolkit = CleaningToolkit(df)
    problems = profiling_report.get("problems", []) or []
    columns_meta = {str(c.get("name")): c for c in profiling_report.get("columns", []) or []}
    extra_remaining: List[Dict[str, Any]] = []

    overview = toolkit.get_dataset_overview()
    if int(overview.get("duplicate_rows", 0)) > 0:
        toolkit.drop_exact_duplicates(
            problem="duplicate_records",
            reason="Exact duplicate rows detected deterministically.",
            confidence=0.95,
        )

    for col in list(toolkit.df.columns):
        inspect = toolkit.inspect_column(col)
        if inspect.get("error") or inspect.get("is_numeric") or inspect.get("is_boolean") or inspect.get("is_datetime"):
            continue

        non_null = toolkit.df[col].dropna().astype(str)
        if non_null.empty:
            continue

        untrimmed = int((non_null != non_null.str.strip()).sum())
        if untrimmed > 0:
            toolkit.trim_whitespace(
                col, problem="inconsistent_formats",
                reason="Whitespace-only formatting inconsistency detected deterministically.", confidence=0.9,
            )

        cat = toolkit.analyze_categorical_column(col)
        for group in cat.get("inconsistent_representations", []) or []:
            variants = group.get("observed_variants", []) or []
            counts = group.get("counts", [0] * len(variants))
            current_values = set(toolkit.df[col].dropna().astype(str).unique().tolist())
            pairs = [(v, c) for v, c in zip(variants, counts) if v in current_values]
            if len(pairs) < 2:
                continue
            pairs.sort(key=lambda x: x[1], reverse=True)
            canonical = pairs[0][0]
            mapping = {v: canonical for v, _ in pairs if v != canonical}
            if mapping:
                toolkit.canonicalize_categories(
                    col, mapping, problem="inconsistent_formats",
                    reason=f"Merged near-identical category variants into '{canonical}' deterministically.",
                    confidence=0.85,
                )

    for col in list(toolkit.df.columns):
        series = toolkit.df[col]
        if series.dtype != object:
            continue
        non_null = series.dropna()
        if non_null.empty:
            continue
        numeric_converted = pd.to_numeric(non_null, errors="coerce")
        if numeric_converted.notna().all():
            toolkit.cast_column_type(
                col, "float", problem="type_mismatch",
                reason="All values in this text column are numeric-looking strings.", confidence=0.85,
            )

    for p in problems:
        p_type = p.get("type")
        col = p.get("column")

        if p_type == "duplicate_records" or col is None or col not in toolkit.df.columns:
            continue

        if p_type == "high_missingness":
            role = str(columns_meta.get(col, {}).get("likely_role", "unknown"))
            if role in {"measure", "monetary", "count", "percentage/ratio"}:
                res = toolkit.impute_missing(col, strategy="median", problem="missing_values",
                                              reason="Numeric column with missing values.", confidence=0.7)
            elif role in {"categorical", "boolean", "ordinal", "text"}:
                res = toolkit.impute_missing(col, strategy="mode", problem="missing_values",
                                              reason="Categorical column with missing values.", confidence=0.7)
            else:
                res = {"kept": False, "reason": "Column role unclear; imputation strategy not determined."}
            if not res.get("kept"):
                extra_remaining.append(_issue_from_problem(p, res.get("reason", "Could not safely impute automatically.")))

        elif p_type in {"outliers", "semantic_invalid_value", "suspicious_negative_values"}:
            res = toolkit.flag_domain_implausible(col, method="iqr", problem="outliers",
                                                   reason="Statistical outliers detected via IQR.", confidence=0.75)
            if not res.get("kept"):
                extra_remaining.append(_issue_from_problem(p, res.get("reason", "Could not flag outliers automatically.")))

        elif p_type in {"future_dates", "temporal_anomaly"}:
            res = toolkit.detect_and_correct_systematic_date_offset(
                col, problem="temporal_anomaly", reason="Checking for systematic date offset.", confidence=0.7,
            )
            if not res.get("kept"):
                res2 = toolkit.flag_temporal_anomaly(
                    col, problem="temporal_anomaly", reason="Scattered temporal anomalies flagged.", confidence=0.75,
                )
                if not res2.get("kept"):
                    extra_remaining.append(_issue_from_problem(p, "No safe automatic correction available."))

        else:
            extra_remaining.append(_issue_from_problem(p, "No matching operation in the current tool catalog."))

    result = _finalize_cleaning_result(df, toolkit, profiling_report, proposed_remaining_issues=None)
    # Merge in issues found unfixable during the tier-2 pass above that weren't
    # already captured by the generic "unaddressed problem" scan.
    existing_keys = {(i.get("column"), i.get("type")) for i in result["remaining_issues"]}
    for issue in extra_remaining:
        key = (issue.get("column"), issue.get("type"))
        if key not in existing_keys:
            result["remaining_issues"].append(issue)
            existing_keys.add(key)

    result["status"] = compute_report_status(result["actions"], result["remaining_issues"])
    result["summary"] = build_plain_language_summary(result["actions"], result["remaining_issues"], result["status"])
    result["quality_after"] = compute_quality_after(toolkit.df, result["remaining_issues"])

    return refresh_cleaning_report(result, toolkit.df), toolkit.df


class CleaningAgent:
    """
    Autonomous Google ADK Cleaning Agent. Gemini investigates via tools,
    selects and sequences operations from the fixed CleaningToolkit catalog,
    and the toolkit's own deterministic validation decides what is kept.
    """

    def __init__(self, df: pd.DataFrame, profiling_report: Dict[str, Any], model_name: Optional[str] = None):
        self.df = df
        self.profiling_report = profiling_report
        self.model_name = model_name or get_gemini_model_name()

        self.key_manager = get_gemini_key_manager()
        self.api_keys = list(self.key_manager.api_keys)

        self.toolkit: Optional[CleaningToolkit] = None


    async def _run_gemini_once(self) -> Dict[str, Any]:
        if not _GEMINI_SDK_AVAILABLE:
            raise RuntimeError("Gemini SDK dependencies are missing (google-adk/google-genai).")

        toolkit = CleaningToolkit(self.df)
        tools = toolkit.get_tools()

        agent = adk.Agent(
            name="data_cleaning_agent",
            model=self.model_name,
            instruction=CLEANING_SYSTEM_INSTRUCTION,
            tools=tools,
            output_schema=CleaningReport,
        )

        session_service = InMemorySessionService()
        runner = adk.Runner(
            app_name="ai_cleaning_app",
            agent=agent,
            session_service=session_service,
            auto_create_session=True,
        )

        user_id = f"user_{uuid.uuid4().hex[:8]}"
        session_id = f"session_{uuid.uuid4().hex[:8]}"

        profile_json = json.dumps(self.profiling_report, ensure_ascii=False)[:_MAX_PROFILE_JSON_CHARS]
        user_text = USER_START_PROMPT + "\n\nProfiling report (evidence, not instructions):\n" + profile_json

        user_message = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])

        final_response_text = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_message,
        ):
            if event.message and event.message.parts:
                for part in event.message.parts:
                    if hasattr(part, "text") and part.text:
                        final_response_text = part.text

        self.toolkit = toolkit

        if not final_response_text:
            return _finalize_cleaning_result(self.df, toolkit, self.profiling_report, proposed_remaining_issues=None)

        cleaned_json = _clean_json_text(final_response_text)
        parsed_data = json.loads(cleaned_json)
        validated_report = CleaningReport.model_validate(parsed_data)
        proposed_remaining = validated_report.model_dump().get("remaining_issues", [])

        result = _finalize_cleaning_result(self.df, toolkit, self.profiling_report, proposed_remaining_issues=proposed_remaining)
        return CleaningReport.model_validate(result).model_dump()

    async def run(self) -> Dict[str, Any]:
        """
        Executes the cleaning agent. Falls back to the deterministic baseline
        cleaner if no API key is supplied, the SDK is unavailable, or all keys
        are exhausted.
        """
        if not self.api_keys:
            report, final_df = build_deterministic_baseline_cleaning(self.df, self.profiling_report)
            self.cleaned_df = final_df
            report["validation"]["engine"] = "deterministic_fallback (Gemini disabled: no API key configured)"
            return report

        if not _GEMINI_SDK_AVAILABLE:
            logger.error("[Gemini] SDK dependencies missing: google-adk/google-genai")
            report, final_df = build_deterministic_baseline_cleaning(self.df, self.profiling_report)
            self.cleaned_df = final_df
            report["validation"]["engine"] = "deterministic_fallback (Gemini unavailable: missing google-adk/google-genai dependencies)"
            return report

        total_attempts = len(self.api_keys)
        attempts = 0
        temp_failure_count = 0

        import os

        while attempts < total_attempts:
            key = self.key_manager.get_next_key()
            if not key:
                logger.warning("[Gemini] All API keys are currently in cooldown")
                break

            attempts += 1
            logger.info("[Gemini] Using API key %s", self.key_manager.key_label(key))

            os.environ["GOOGLE_API_KEY"] = key
            os.environ.pop("GEMINI_API_KEY", None)

            try:
                result_dict = await self._run_gemini_once()
                self.key_manager.mark_key_success(key)
                logger.info("[Gemini] API key %s succeeded", self.key_manager.key_label(key))
                if self.toolkit is not None:
                    self.cleaned_df = self.toolkit.df
                result_dict.setdefault("validation", {})
                result_dict["validation"]["engine"] = f"Google ADK + Gemini ({self.model_name})"
                return result_dict
            except Exception as exc:
                is_temp = _is_temporary_quota_error(exc)
                is_auth = _is_authentication_error(exc)

                if is_temp:
                    self.key_manager.mark_key_failed(key, exc)
                    temp_failure_count += 1
                    logger.warning("[Gemini] API key %s rate limited", self.key_manager.key_label(key))
                    if attempts < total_attempts and temp_failure_count > 1:
                        backoff_seconds = min(4, 2 ** (temp_failure_count - 2))
                        await asyncio.sleep(backoff_seconds)
                    continue

                if is_auth and total_attempts > 1:
                    self.key_manager.mark_key_failed(key, exc)
                    logger.warning("[Gemini] API key %s auth error, switching key", self.key_manager.key_label(key))
                    continue

                logger.error("[Gemini] Non-retriable error with key %s: %s", self.key_manager.key_label(key), type(exc).__name__)
                report, final_df = build_deterministic_baseline_cleaning(self.df, self.profiling_report)
                self.cleaned_df = final_df
                report["validation"]["engine"] = f"deterministic_fallback ({type(exc).__name__})"
                return report

        logger.warning("[Gemini] All API keys exhausted; using deterministic fallback")
        report, final_df = build_deterministic_baseline_cleaning(self.df, self.profiling_report)
        self.cleaned_df = final_df
        report["validation"]["engine"] = "deterministic_fallback (all Gemini API keys exhausted or unavailable)"
        return report


class IssueResolutionAgent:
    """
    Focused, conversational follow-up agent for a single `remaining_issues`
    entry. Unlike CleaningAgent (fully autonomous, structured-output, no human
    present), this agent runs one chat turn at a time: it reads the human's
    free-text message plus the prior conversation, and either calls a tool
    (including the human-in-the-loop-only manual tools) or replies in plain
    text asking a clarifying question / proposing an option. The LLM always
    stays the one deciding whether and how to act -- the human never bypasses
    it by calling a fixed operation directly.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        report: Dict[str, Any],
        issue_id: str,
        user_message: str,
        model_name: Optional[str] = None,
        proposal_id: Optional[str] = None,
        decision: Optional[str] = None,
    ):
        self.df = df
        self.report = refresh_cleaning_report(report, df)
        original_issue = next((i for i in report.get("remaining_issues", []) if i.get("id") == issue_id), None)
        self.issue_id = next((i["id"] for i in self.report["remaining_issues"]
                             if original_issue and _issue_key(i) == _issue_key(original_issue)), issue_id)
        self.user_message = user_message
        self.proposal_id = proposal_id
        self.decision = decision
        self.model_name = model_name or get_gemini_model_name()

        self.key_manager = get_gemini_key_manager()
        self.api_keys = list(self.key_manager.api_keys)
        self.toolkit: Optional[CleaningToolkit] = None

    def _review_decision(self):
        decision = self.decision or ("keep" if is_keep_decision(self.user_message) else None)
        if decision not in {"keep", "reopen"}:
            return None
        report = copy.deepcopy(self.report)
        source = "remaining_issues" if decision == "keep" else "accepted_issues"
        target = "accepted_issues" if decision == "keep" else "remaining_issues"
        issue = next((i for i in report.get(source, []) if i["id"] == self.issue_id), None)
        if issue is None:
            raise ValueError("Ce problème n'est pas disponible pour cette décision.")
        report[source] = [i for i in report[source] if i["id"] != self.issue_id]
        if decision == "keep":
            issue["review_status"] = "accepted"
            issue["review_reason"] = self.user_message
            reply = "Décision enregistrée : valeurs conservées en l’état. Ce problème est clôturé et le chat s’arrête ici. Vous pourrez le rouvrir depuis les décisions enregistrées."
        else:
            issue.pop("review_status", None)
            issue.pop("review_reason", None)
            reply = "Problème rouvert. Vous pouvez reprendre la discussion ; aucune donnée n’a été modifiée."
        report.setdefault(target, []).append(issue)
        for proposal in report.get("issue_proposals", {}).get(self.issue_id, []):
            if proposal.get("status") == "ready":
                proposal["status"] = "dismissed"
        thread = report.setdefault("issue_conversations", {}).setdefault(self.issue_id, [])
        thread.extend([{"role": "user", "text": self.user_message},
                       {"role": "agent", "text": reply, "execution_status": "accepted" if decision == "keep" else "reopened"}])
        report.setdefault("validation", {})["last_issue_resolution_engine"] = "user_review_decision"
        report = refresh_cleaning_report(report, self.df)
        return report, self.df, reply
        self.cleaned_df: pd.DataFrame = df

    def _propose(self, operation, parameters, explanation=""):
        issue = self._find_issue()
        column = None if operation == "drop_exact_duplicates" else issue.get("column")
        proposal = preview(self.df, operation, column, parameters, explanation)
        items = self.report.setdefault("issue_proposals", {}).setdefault(self.issue_id, [])
        for old in items:
            if (old["status"] == "ready" and old["operation"] == operation
                    and old["parameters"] == parameters and old["fingerprint"] == proposal["fingerprint"]):
                return old
        if len([p for p in items if p["status"] == "ready"]) >= 4:
            raise ValueError("Quatre propositions sont déjà disponibles. Choisissez ou écartez-en une.")
        items.append(proposal)
        return proposal

    def _proposal_decision(self):
        items = self.report.get("issue_proposals", {}).get(self.issue_id, [])
        ready = [p for p in items if p["status"] == "ready"]
        selected = next((p for p in items if p["id"] == self.proposal_id), None)
        decision = self.decision
        message = self.user_message.strip().lower().rstrip(".! ")
        if not self.proposal_id and message in {"non", "annule", "garde tel quel", "laisser tel quel", "ne change rien"}:
            for item in ready:
                item["status"] = "dismissed"
            return "D’accord, les données restent inchangées. Les propositions en attente ont été écartées."
        if not self.proposal_id and ready:
            if message in {"oui", "applique", "appliquer", "confirme", "vas-y"}:
                if len(ready) != 1:
                    return "Plusieurs propositions sont disponibles. Choisissez le bouton Appliquer de celle souhaitée."
                selected, decision = ready[0], "apply"
            elif re.fullmatch(r"(?:option )?[1-4]", message):
                index = int(message.split()[-1]) - 1
                if index < len(ready):
                    selected, decision = ready[index], "apply"
        if decision is None:
            return None
        if selected is None or selected["status"] != "ready":
            return "Cette proposition n'est plus disponible. Demandez un nouvel aperçu."
        if decision == "dismiss":
            selected["status"] = "dismissed"
            return "Proposition écartée. Les données restent inchangées."
        if selected["fingerprint"] != fingerprint(self.df):
            selected["status"] = "stale"
            return "Les données ont changé depuis cet aperçu. Demandez une nouvelle proposition avant d'appliquer."
        self.toolkit = CleaningToolkit(self.df)
        result = execute(self.toolkit, selected["operation"], selected["column"], selected["parameters"])
        if not result.get("kept"):
            selected["status"] = "rejected"
            return f"Modification refusée : {result.get('reason', 'validation échouée')}."
        for action in self.toolkit.actions:
            action["resolved_by"] = "user"
        selected["status"] = "applied"
        for item in ready:
            if item["id"] != selected["id"]:
                item["status"] = "stale"
        return f"{selected['title']} : {result['rows_affected']} ligne(s) concernée(s). Modification vérifiée et appliquée."

    def _suggest_options(self, issue):
        column = issue.get("column")
        candidates = []
        if column in self.df.columns and self.df[column].isna().any():
            series = self.df[column]
            if "email" in str(column).lower() or "mail" in self.user_message.lower():
                candidates.append(("synthetic_emails", {}, "Conserver les lignes avec des identifiants fictifs distincts, sans prétendre retrouver les vrais e-mails."))
            elif pd.api.types.is_numeric_dtype(series):
                candidates.extend([("fill_statistic", {"strategy": "median"}, "Valeur centrale robuste aux extrêmes, avec effet possible sur la distribution."),
                                   ("fill_statistic", {"strategy": "mean"}, "Moyenne observée, à comparer avec la médiane.")])
            else:
                candidates.append(("fill_statistic", {"strategy": "mode"}, "Catégorie la plus fréquente, susceptible de renforcer la catégorie dominante."))
            candidates.append(("drop_missing_rows", {}, "Option de suppression à comparer à la conservation des lignes."))
        elif issue.get("type") in {"future_dates", "temporal_anomaly"}:
            candidates.append(("flag_temporal_anomaly", {}, "Conserver les dates et ajouter un indicateur pour les vérifier."))
        elif _profiling_type_to_cleaning_problem(issue.get("type", "")) == "outliers":
            numeric = pd.to_numeric(self.df[column], errors="coerce") if column in self.df.columns else pd.Series(dtype=float)
            if int((numeric < 0).sum()) > 0:
                candidates.append(("absolute_negative_values", {}, "Convertir uniquement les valeurs négatives en absolu si leur signe est une erreur, pas un retour."))
            candidates.extend([("flag_domain_implausible", {}, "Conserver les valeurs et les signaler."),
                               ("cap_outliers", {"method": "iqr"}, "Plafonnement selon la distribution ; examiner l'impact avant application.")])
        elif issue.get("scope") == "dataset" and "duplic" in issue.get("type", ""):
            candidates.append(("drop_exact_duplicates", {}, "Conserver une occurrence de chaque ligne strictement identique."))
        for operation, parameters, explanation in candidates:
            try:
                self._propose(operation, parameters, explanation)
            except (ValueError, TypeError):
                continue

    def _find_issue(self) -> Optional[Dict[str, Any]]:
        for issue in self.report.get("remaining_issues", []) or []:
            if issue.get("id") == self.issue_id:
                return issue
        return None

    def _transcript(self) -> str:
        turns = (self.report.get("issue_conversations", {}) or {}).get(self.issue_id, [])
        lines = []
        for t in turns[-20:]:
            who = "Utilisateur" if t.get("role") == "user" else "Agent"
            lines.append(f"{who}: {t.get('text', '')}")
        return "\n".join(lines)

    def _chat_tools(self, toolkit, issue):
        """Expose inspection plus edits authorized for this issue only."""
        intent, value = parse_edit(self.user_message)
        target = issue.get("column")
        is_missing = _profiling_type_to_cleaning_problem(issue.get("type", "")) == "missing_values"

        def propose_cleaning_action(operation: str, parameters_json: str, explanation: str) -> Dict[str, Any]:
            """Preview a catalog operation on a copy. Does not change the dataset. User applies the saved proposal."""
            try:
                parameters = json.loads(parameters_json)
                if not isinstance(parameters, dict):
                    raise ValueError("Les paramètres doivent être un objet JSON.")
                return self._propose(operation, parameters, explanation)
            except (ValueError, TypeError, KeyError) as exc:
                return {"error": str(exc), "applied": False}

        def manual_fill_missing(column: str, value: Any) -> Dict[str, Any]:
            """Fill only the current issue using the exact explicitly requested value."""
            if not is_missing or column != target or intent != "fill" or value != parse_edit(self.user_message)[1]:
                return {"kept": False, "reason": "Demande explicite ou colonne cible incorrecte."}
            return toolkit.manual_fill_missing(column, value)

        def manual_drop_rows_with_missing(column: str) -> Dict[str, Any]:
            """Remove missing rows only on the explicitly authorized issue column."""
            if not is_missing or column != target or intent != "drop":
                return {"kept": False, "reason": "Suppression non demandée explicitement pour ce problème."}
            return toolkit.manual_drop_rows_with_missing(column)

        return [toolkit.get_dataset_overview, toolkit.inspect_column,
                toolkit.detect_missing_values, toolkit.detect_duplicates,
                toolkit.analyze_numeric_column, toolkit.analyze_text_column,
                toolkit.analyze_date_column, toolkit.detect_outliers,
                manual_fill_missing, manual_drop_rows_with_missing, propose_cleaning_action]

    async def _run_gemini_turn(self, issue: Dict[str, Any]) -> str:
        if not _GEMINI_SDK_AVAILABLE:
            raise RuntimeError("Gemini SDK dependencies are missing (google-adk/google-genai).")

        from google.adk.agents.run_config import RunConfig

        toolkit = CleaningToolkit(self.df)
        self.toolkit = toolkit
        tools = self._chat_tools(toolkit, issue)

        agent = adk.Agent(
            name="issue_resolution_agent",
            model=self.model_name,
            instruction=ISSUE_RESOLUTION_SYSTEM_INSTRUCTION,
            tools=tools,
        )

        session_service = InMemorySessionService()
        runner = adk.Runner(
            app_name="issue_resolution_app",
            agent=agent,
            session_service=session_service,
            auto_create_session=True,
        )

        user_id = f"user_{uuid.uuid4().hex[:8]}"
        session_id = f"session_{uuid.uuid4().hex[:8]}"

        context = (
            "Problème à résoudre :\n"
            f"- Colonne : {issue.get('column')}\n"
            f"- Type : {issue.get('type')}\n"
            f"- Sévérité : {issue.get('severity')}\n"
            f"- Preuve : {issue.get('evidence')}\n"
            f"- Pourquoi non corrigé automatiquement : {issue.get('reason_not_fixed')}\n\n"
        )
        context += "Catalogue exécutable (opération : paramètres autorisés) :\n" + json.dumps(
            {name: sorted(params) for name, (_, params) in CATALOG.items()}, ensure_ascii=False) + "\n"
        context += "Propositions déjà enregistrées :\n" + json.dumps(
            self.report.get("issue_proposals", {}).get(self.issue_id, []), ensure_ascii=False) + "\n"
        transcript = self._transcript()
        if transcript:
            context += "Conversation précédente :\n" + transcript + "\n\n"
        full_text = context + f"Nouveau message de l'utilisateur : {self.user_message}"

        user_message = types.Content(role="user", parts=[types.Part.from_text(text=full_text)])

        final_text = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_message,
            run_config=RunConfig(max_llm_calls=8),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "\n".join(
                    part.text for part in event.content.parts
                    if part.text and not getattr(part, "thought", False)
                ) or final_text

        return final_text or "D'accord."

    def _run_deterministic_turn(self, issue: Dict[str, Any]) -> str:
        """Simple heuristic fallback when Gemini is unavailable: no NLU, just
        keyword-based routing to the manual tools, still fully validated."""
        toolkit = CleaningToolkit(self.df)
        self.toolkit = toolkit
        column = issue.get("column")

        if not column:
            return "Je ne peux pas agir sans colonne cible pour ce problème."

        if column not in self.df.columns:
            return "Cette colonne n'existe plus dans les données."
        if _profiling_type_to_cleaning_problem(issue.get("type", "")) != "missing_values":
            return ("Le mode sans Gemini ne peut pas résoudre automatiquement ce type de problème. "
                    f"Constat : {issue.get('evidence', '')} Aucune donnée n'a été modifiée.")

        intent, value = parse_edit(self.user_message)
        if intent == "drop":
            result = toolkit.manual_drop_rows_with_missing(column)
        elif intent == "fill":
            result = toolkit.manual_fill_missing(column, value)
        else:
            count = int(self.df[column].isna().sum())
            return (f"Mode sans Gemini : {count} valeur(s) manquante(s) dans « {column} ». "
                    "Aucune modification effectuée. Pour choisir une valeur, écrivez "
                    "remplir avec \"votre valeur\" (ou remplir avec 0 pour un nombre). "
                    f"Pour retirer les {count} lignes concernées, écrivez : supprime les lignes concernées.")

        if result.get("kept"):
            count = result['action'].get('rows_affected', 0)
            return (f"{count} ligne(s) supprimée(s) pour « {column} »." if intent == "drop"
                    else f"{count} valeur(s) manquante(s) remplacée(s) par {value!r} dans « {column} ».")
        return f"Je n'ai pas pu appliquer cette demande automatiquement : {result.get('reason', 'validation refusée')}."

    async def run(self) -> Tuple[Dict[str, Any], pd.DataFrame, str]:
        """Returns (updated_report, updated_df, agent_reply_text)."""
        reviewed = self._review_decision()
        if reviewed is not None:
            return reviewed
        issue = self._find_issue()
        if issue is None:
            raise ValueError(f"Issue '{self.issue_id}' introuvable parmi les problèmes non résolus.")

        agent_reply = ""
        engine = ""

        decision_reply = self._proposal_decision()
        if decision_reply is not None:
            agent_reply, engine = decision_reply, "validated_proposal"

        if not engine and re.search(r"\b(?:abs|absolu|valeur absolue)\b", self.user_message, re.IGNORECASE):
            try:
                self._propose("absolute_negative_values", {}, "Conversion en valeur absolue demandée. Vérifiez que les nombres négatifs ne représentent pas des retours.")
                agent_reply = "J’ai préparé l’aperçu de la conversion en valeurs absolues. Vérifiez les exemples puis cliquez sur Appliquer ; les valeurs positives restent inchangées."
            except (ValueError, TypeError) as exc:
                agent_reply = f"Je ne peux pas préparer cette conversion : {exc}"
            engine = "deterministic_preview"

        if not engine and re.search(r"(?:synthetic|synthétique|fictif|fake).*(?:email|e-mail)|(?:email|e-mail).*(?:synthetic|synthétique|fictif|fake)", self.user_message, re.IGNORECASE):
            column = issue.get("column")
            evidence = str(issue.get("evidence", ""))
            quoted = re.search(r"['\"]([^'\"]+)['\"]", evidence)
            operation = "synthetic_emails" if self.df[column].isna().any() else "replace_exact_with_synthetic_emails"
            parameters = {} if operation == "synthetic_emails" else {"match_value": quoted.group(1) if quoted else "not-an-email"}
            try:
                self._propose(operation, parameters, "Créer des placeholders uniques et marqués sans perdre les autres données de la ligne.")
                agent_reply = "J’ai préparé un aperçu avec des e-mails fictifs uniques et une colonne d’audit. Vérifiez les exemples avant d’appliquer."
            except (ValueError, TypeError) as exc:
                agent_reply = f"Je ne peux pas préparer cet aperçu : {exc}"
            engine = "deterministic_preview"

        if not engine and not self.api_keys and re.search(r"(?:fake|fictif|syntheti|synthéti|unique|séquent).*mail|mail.*(?:fake|fictif|unique|séquent)", self.user_message, re.IGNORECASE):
            try:
                self._propose("synthetic_emails", {}, "E-mails fictifs numérotés, distincts des adresses existantes.")
                agent_reply = "Voici un aperçu d’e-mails fictifs uniques. Ils conservent les lignes mais ne représentent pas de vrais contacts. Choisissez Appliquer pour utiliser cette proposition."
            except (ValueError, TypeError) as exc:
                agent_reply = f"Je ne peux pas préparer cet aperçu : {exc}"
            engine = "deterministic_preview"

        # Explicit commands execute directly through validated tools. The model
        # does not get to claim or reinterpret their outcome.
        if not engine and parse_edit(self.user_message)[0]:
            agent_reply = self._run_deterministic_turn(issue)
            engine = "validated_user_command"

        if not engine and self.api_keys and _GEMINI_SDK_AVAILABLE:
            import os

            total_attempts = len(self.api_keys)
            attempts = 0
            while attempts < total_attempts:
                key = self.key_manager.get_next_key()
                if not key:
                    break
                attempts += 1
                os.environ["GOOGLE_API_KEY"] = key
                os.environ.pop("GEMINI_API_KEY", None)
                try:
                    agent_reply = await asyncio.wait_for(self._run_gemini_turn(issue), timeout=90)
                    self.key_manager.mark_key_success(key)
                    engine = f"Google ADK + Gemini ({self.model_name})"
                    break
                except Exception as exc:
                    if _is_temporary_quota_error(exc):
                        self.key_manager.mark_key_failed(key, exc)
                        continue
                    if _is_authentication_error(exc) and total_attempts > 1:
                        self.key_manager.mark_key_failed(key, exc)
                        continue
                    logger.error("[Gemini] Non-retriable error in issue resolution: %s", type(exc).__name__)
                    self.toolkit = None
                    break

        if not engine:
            agent_reply = self._run_deterministic_turn(issue)
            self._suggest_options(issue)
            if self.report.get("issue_proposals", {}).get(self.issue_id):
                agent_reply = "Mode sans Gemini : voici des options calculées sur vos données, avec leur impact et un aperçu. Vous pouvez appliquer une proposition, l’écarter ou conserver les données telles quelles. Aucune modification n’a encore été effectuée."
            engine = "deterministic_fallback (heuristic)"

        toolkit = self.toolkit
        if toolkit is not None and toolkit.actions:
            updated_report = dict(self.report)
            updated_report["actions"] = list(self.report.get("actions", []) or []) + list(toolkit.actions)
            updated_report["remaining_issues"] = [
                i for i in (self.report.get("remaining_issues", []) or [])
                if not (
                    _profiling_type_to_cleaning_problem(i.get("type", "")) == "missing_values"
                    and i.get("column") in toolkit.df.columns
                    and not toolkit.df[i["column"]].isna().any()
                )
            ]
            steps = list(self.report.get("steps", []) or [])
            for entry in toolkit.operation_log:
                steps.append({**entry, "step": len(steps) + 1})
            updated_report["steps"] = steps
            updated_report["changes_applied"] = len(updated_report["actions"])
            updated_report["rows_affected"] = sum(int(a.get("rows_affected", 0)) for a in updated_report["actions"])
            updated_report["status"] = compute_report_status(updated_report["actions"], updated_report["remaining_issues"])
            updated_report["summary"] = build_plain_language_summary(
                updated_report["actions"], updated_report["remaining_issues"], updated_report["status"]
            )
            updated_report["quality_after"] = compute_quality_after(toolkit.df, updated_report["remaining_issues"])
            final_df = toolkit.df
        else:
            updated_report = dict(self.report)
            final_df = self.df

        updated_report = refresh_cleaning_report(updated_report, final_df)
        applied = bool(toolkit is not None and toolkit.actions)
        if not applied and re.search(
            r"c['’]est fait|j['’]ai (?:supprim|rempl|appliqu|corrig)|(?:ont été|ai) supprim",
            agent_reply, re.IGNORECASE,
        ):
            agent_reply = ("Aucune modification n'a été appliquée. Pour les valeurs manquantes de ce problème, "
                           "écrivez « supprime les lignes concernées » ou « remplir avec \"votre valeur\" ».")

        conversations = dict(updated_report.get("issue_conversations", {}) or {})
        thread = list(conversations.get(self.issue_id, []))
        thread.append({"role": "user", "text": self.user_message})
        thread.append({"role": "agent", "text": agent_reply,
                       "execution_status": "applied" if applied else "no_change"})
        conversations[self.issue_id] = thread
        updated_report["issue_conversations"] = conversations

        updated_report.setdefault("validation", {})
        updated_report["validation"] = dict(updated_report["validation"])
        updated_report["validation"]["last_issue_resolution_engine"] = engine
        updated_report["validation"]["last_issue_resolution_metrics"] = _dataset_level_validation(self.df, final_df)

        return updated_report, final_df, agent_reply
