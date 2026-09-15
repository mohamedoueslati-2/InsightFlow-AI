"""Offline tests of preview, confirmation and synthetic data generation."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from backend.services.cleaning.legacy_agent import IssueResolutionAgent
from backend.services.cleaning.proposals import preview, execute
from backend.services.cleaning.tools import CleaningToolkit


class ProposalTests(unittest.TestCase):
    def data(self):
        return pd.DataFrame({"email": ["client1@example.invalid", None, None], "amount": [10, 20, 30]})

    def report(self):
        return {"remaining_issues": [{"id": "email", "type": "missing_values", "column": "email"}], "actions": []}

    def agent(self, df=None, report=None, message="Propose des fake emails uniques", **kwargs):
        agent = IssueResolutionAgent(df if df is not None else self.data(), report or self.report(), "email", message, **kwargs)
        agent.api_keys = []
        return agent

    def test_preview_does_not_mutate_and_skips_existing_email(self):
        agent = self.agent()
        report, df, _ = asyncio.run(agent.run())
        pd.testing.assert_frame_equal(df, self.data())
        proposal = report["issue_proposals"]["email"][0]
        self.assertEqual(proposal["rows_affected"], 2)
        self.assertEqual(proposal["samples"][0]["after"], "client2@example.invalid")
        self.assertEqual(report["actions"], [])

    def test_apply_persisted_preview_creates_unique_tagged_values(self):
        report, df, _ = asyncio.run(self.agent().run())
        proposal = report["issue_proposals"]["email"][0]
        agent = self.agent(df, report, "Appliquer", proposal_id=proposal["id"], decision="apply")
        result, cleaned, _ = asyncio.run(agent.run())
        self.assertEqual(cleaned.email.nunique(), 3)
        self.assertFalse(cleaned.email.isna().any())
        self.assertEqual(cleaned.email_synthetic.sum(), 2)
        self.assertEqual(result["validation"]["synthetic_cells"], 2)
        self.assertEqual(result["remaining_issues"], [])
        self.assertEqual(result["issue_proposals"]["email"][0]["status"], "applied")
        self.assertEqual(result["actions"][0]["resolved_by"], "user")

    def test_yes_uses_exact_saved_custom_parameters(self):
        agent = self.agent()
        agent._propose("synthetic_emails", {"prefix": "test-", "start": 42, "domain": "example.invalid"})
        report, df, _ = asyncio.run(self.agent(report=agent.report, message="oui").run())
        self.assertEqual(df.email.iloc[1], "test-42@example.invalid")

    def test_ambiguous_confirmation_cannot_choose_between_options(self):
        agent = self.agent()
        agent._propose("synthetic_emails", {})
        agent._propose("drop_missing_rows", {})
        report, df, reply = asyncio.run(self.agent(report=agent.report, message="oui").run())
        pd.testing.assert_frame_equal(df, self.data())
        self.assertIn("Plusieurs", reply)

    def test_changed_dataset_invalidates_saved_proposal(self):
        report, df, _ = asyncio.run(self.agent().run())
        proposal = report["issue_proposals"]["email"][0]
        changed = df.copy()
        changed.loc[0, "amount"] = 999
        result, after, _ = asyncio.run(self.agent(changed, report, "Appliquer", proposal_id=proposal["id"], decision="apply").run())
        pd.testing.assert_frame_equal(after, changed)
        self.assertEqual(result["issue_proposals"]["email"][0]["status"], "stale")

    def test_dismiss_and_malformed_operation_do_not_change_data(self):
        report, df, _ = asyncio.run(self.agent().run())
        proposal = report["issue_proposals"]["email"][0]
        result, after, _ = asyncio.run(self.agent(df, report, "Écarter", proposal_id=proposal["id"], decision="dismiss").run())
        pd.testing.assert_frame_equal(df, after)
        self.assertEqual(result["issue_proposals"]["email"][0]["status"], "dismissed")
        with self.assertRaises(ValueError):
            preview(df, "eval", "email", {"code": "anything"})

    def test_statistics_are_computed_from_data_not_invented(self):
        df = pd.DataFrame({"profit": [10.0, None, 100.0]})
        p = preview(df, "fill_statistic", "profit", {"strategy": "median"})
        self.assertEqual(p["samples"][0]["after"], "55.0")
        self.assertIn("variance", p["impact"])

    def test_existing_synthetic_column_is_preserved(self):
        df = self.data()
        df["email_synthetic"] = "original"
        toolkit = CleaningToolkit(df)
        result = execute(toolkit, "synthetic_emails", "email", {})
        self.assertTrue(result["kept"])
        self.assertTrue((toolkit.df.email_synthetic == "original").all())
        self.assertEqual(toolkit.df.email_synthetic_audit.sum(), 2)

    def test_llm_tool_prepares_preview_without_mutating(self):
        agent = self.agent()
        toolkit = CleaningToolkit(agent.df)
        tools = {t.__name__: t for t in agent._chat_tools(toolkit, agent._find_issue())}
        result = tools["propose_cleaning_action"]("synthetic_emails", '{"prefix":"customer-"}', "Keep rows")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(toolkit.actions, [])
        error = tools["propose_cleaning_action"]("fill_constant", '{"column":"amount", "value":0}', "wrong scope")
        self.assertIn("error", error)

    def test_gemini_turn_can_return_a_custom_preview_without_applying(self):
        from backend.services.cleaning import legacy_agent as module
        if not module._GEMINI_SDK_AVAILABLE:
            self.skipTest("Optional Gemini SDK is not installed")
        from google.adk.events import Event
        from google.genai import types

        class FakeRunner:
            def __init__(self, **kwargs):
                self.agent = kwargs["agent"]

            async def run_async(self, **kwargs):
                assert kwargs["run_config"].max_llm_calls == 8
                tool = next(t for t in self.agent.tools if getattr(t, "__name__", "") == "propose_cleaning_action")
                tool("synthetic_emails", '{"prefix":"demo-", "start":10}', "Conserver les lignes")
                yield Event(author="issue_resolution_agent", content=types.Content(role="model", parts=[types.Part(text="Voici l’aperçu demandé.")]))

        agent = self.agent()
        with patch.object(module.adk, "Runner", FakeRunner):
            reply = asyncio.run(agent._run_gemini_turn(agent._find_issue()))
        self.assertIn("aperçu", reply)
        self.assertEqual(agent.toolkit.actions, [])
        self.assertEqual(agent.report["issue_proposals"]["email"][0]["samples"][0]["after"], "demo-10@example.invalid")

    def test_api_preview_then_apply_and_reload(self):
        from backend import main
        from unittest.mock import AsyncMock
        from backend.services.cleaning.planner import CleaningPlanner
        from backend.services.cleaning.code_generator import CodeGenerator
        from backend.services.cleaning.executors.docker_executor import DockerSandboxExecutor
        from backend.services.cleaning.executors.base import ExecutionResult
        from backend.services.cleaning.schemas import CleaningPlan, CleaningPlanItem, GeneratedProgram
        candidate = self.data().copy()
        candidate['email'] = ['client1@example.invalid', 'client2@example.invalid', 'client3@example.invalid']
        candidate['email_synthetic'] = [False, True, True]
        plan = CleaningPlan(plan_id='test', dataset_understanding='email', items=[CleaningPlanItem(
            id='item', problem_id='email', columns=['email', 'email_synthetic'], diagnosis='Missing',
            strategy='Synthetic email preview', expected_effect='2 synthetic cells')])
        code = 'def clean_dataframe(df):\n    working_df = df.copy()\n    working_df["email"] = ["client1@example.invalid", "client2@example.invalid", "client3@example.invalid"]\n    working_df["email_synthetic"] = [False, True, True]\n    return working_df'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ["reports", "cleaned"]:
                (root / folder / "csv").mkdir(parents=True)
            report_path = root / "reports/csv/example_cleaning.json"
            data_path = root / "cleaned/csv/example_cleaned.pkl"
            report_path.write_text(json.dumps(self.report()), encoding="utf-8")
            self.data().to_pickle(data_path)
            with patch.object(main, "STORAGE_DIR", root), patch.object(main, "CLEANING_REPORTS_DIR", root / "reports"), patch.object(main, "CLEANED_DIR", root / "cleaned"), patch.object(CleaningPlanner, 'create_plan', AsyncMock(return_value=plan)), patch.object(CodeGenerator, 'generate', AsyncMock(return_value=GeneratedProgram(code=code))), patch.object(DockerSandboxExecutor, 'execute', AsyncMock(return_value=ExecutionResult('success', candidate_dataframe=candidate))):
                client = TestClient(main.app)
                url = "/cleanings/csv/example/issues/message"
                first = client.post(url, json={"issue_id": "email", "message": "Propose des fake emails uniques"})
                self.assertEqual(first.status_code, 200, first.text)
                proposal = first.json()["issue_proposals"]["email"][0]
                pd.testing.assert_frame_equal(pd.read_pickle(data_path), self.data())
                applied = client.post(url, json={"issue_id": "email", "proposal_id": proposal["id"], "decision": "apply"})
                self.assertEqual(applied.status_code, 200, applied.text)
                reloaded = client.get("/cleanings/csv/example").json()
                self.assertEqual(reloaded["validation"]["synthetic_cells"], 2)
                self.assertFalse(pd.read_pickle(data_path).email.isna().any())
                self.assertEqual(len(reloaded["actions"]), 1)

    def test_absolute_negative_values_preview_and_apply(self):
        df = pd.DataFrame({"quantity": [2, -5, 3, -1]})
        proposal = preview(df, "absolute_negative_values", "quantity", {})
        self.assertEqual(proposal["rows_affected"], 2)
        self.assertEqual(proposal["samples"][0]["after"], "5")
        pd.testing.assert_series_equal(df.quantity, pd.Series([2, -5, 3, -1], name="quantity"))
        toolkit = CleaningToolkit(df)
        result = execute(toolkit, "absolute_negative_values", "quantity", {})
        self.assertTrue(result["kept"])
        self.assertEqual(toolkit.df.quantity.tolist(), [2, 5, 3, 1])

    def test_abs_natural_request_creates_preview_and_resolves_after_apply(self):
        df = pd.DataFrame({"quantity": [1, -5, 2]})
        report = {"remaining_issues": [{"id": "qty", "scope": "column", "column": "quantity",
                  "type": "suspicious_values", "evidence": "1 negative quantity"}], "actions": []}
        agent = IssueResolutionAgent(df, report, "qty", "appliquer abs")
        agent.api_keys = []
        proposed, unchanged, _ = asyncio.run(agent.run())
        pd.testing.assert_frame_equal(unchanged, df)
        proposal = proposed["issue_proposals"]["qty"][0]
        applied_agent = IssueResolutionAgent(df, proposed, "qty", "Appliquer",
                                             proposal_id=proposal["id"], decision="apply")
        applied_agent.api_keys = []
        applied, cleaned, _ = asyncio.run(applied_agent.run())
        self.assertEqual(cleaned.quantity.tolist(), [1, 5, 2])
        self.assertEqual(applied["remaining_issues"], [])

    def test_replace_exact_placeholder_with_unique_synthetic_emails(self):
        df = pd.DataFrame({"contact_email": ["a@x.com", "not-an-email", "not-an-email"]})
        p = preview(df, "replace_exact_with_synthetic_emails", "contact_email",
                    {"match_value": "not-an-email"})
        self.assertEqual(p["rows_affected"], 2)
        toolkit = CleaningToolkit(df)
        result = execute(toolkit, p["operation"], p["column"], p["parameters"])
        self.assertTrue(result["kept"])
        self.assertEqual(toolkit.df.contact_email.tolist(),
                         ["a@x.com", "client1@example.invalid", "client2@example.invalid"])
        self.assertEqual(toolkit.df.contact_email_synthetic.sum(), 2)

    def test_fake_email_request_for_text_placeholder_is_supported(self):
        df = pd.DataFrame({"contact_email": ["ok@x.com", "not-an-email"]})
        report = {"remaining_issues": [{"id": "mail", "scope": "column", "column": "contact_email",
                  "type": "inconsistent_formats", "evidence": "1 occurrence of 'not-an-email' placeholder string"}], "actions": []}
        agent = IssueResolutionAgent(df, report, "mail", "je choisis les emails synthétiques")
        agent.api_keys = []
        proposed, unchanged, _ = asyncio.run(agent.run())
        pd.testing.assert_frame_equal(unchanged, df)
        proposal = proposed["issue_proposals"]["mail"][0]
        self.assertEqual(proposal["operation"], "replace_exact_with_synthetic_emails")
        applied_agent = IssueResolutionAgent(df, proposed, "mail", "Appliquer",
                                             proposal_id=proposal["id"], decision="apply")
        applied_agent.api_keys = []
        applied, cleaned, _ = asyncio.run(applied_agent.run())
        self.assertEqual(cleaned.contact_email.iloc[1], "client1@example.invalid")
        self.assertEqual(applied["remaining_issues"], [])


if __name__ == "__main__":
    unittest.main()
