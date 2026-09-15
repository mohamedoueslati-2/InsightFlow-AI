"""Offline regressions for conversational cleaning; no provider calls."""

import asyncio
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backend.services.cleaning.legacy_agent import IssueResolutionAgent, _finalize_cleaning_result, refresh_cleaning_report
from backend.services.cleaning.chat_intent import parse_edit
from backend.services.cleaning.schemas import IssueMessageRequest
from backend.services.cleaning.tools import CleaningToolkit
from pydantic import ValidationError


class ChatSafetyTests(unittest.TestCase):
    def resolver(self, message, issue_type="missing_values"):
        df = pd.DataFrame({"email": ["a@x.com", None], "other": [None, "x"]})
        report = {"remaining_issues": [{"id": "email-issue", "column": "email",
                  "type": issue_type, "evidence": "One missing value"}], "actions": []}
        agent = IssueResolutionAgent(df, report, "email-issue", message)
        agent.api_keys = []
        return agent

    def test_questions_negations_and_acknowledgements_do_not_edit(self):
        for message in ["bonjour", "oui", "pourquoi ?", "ne supprime pas ces lignes",
                        "peux-tu supprimer les lignes ?", "explique les options"]:
            with self.subTest(message=message):
                agent = self.resolver(message)
                report, df, reply = asyncio.run(agent.run())
                pd.testing.assert_frame_equal(df, agent.df)
                self.assertEqual(report["actions"], [])
                self.assertEqual(len(report["remaining_issues"]), 1)
                self.assertIn("Aucune modification", reply)

    def test_exact_string_value_not_whole_sentence_is_applied(self):
        agent = self.resolver('remplir avec "unknown@example.com"')
        report, df, reply = asyncio.run(agent.run())
        self.assertEqual(df.loc[1, "email"], "unknown@example.com")
        self.assertEqual(report["remaining_issues"], [])
        self.assertEqual(len(report["issue_conversations"]["email-issue"]), 2)
        self.assertTrue(report["steps"])
        self.assertIn("1 valeur", reply)
        self.assertTrue(pd.isna(agent.df.loc[1, "email"]))

    def test_drop_is_explicit_and_measured(self):
        agent = self.resolver("supprime les lignes concernées")
        report, df, reply = asyncio.run(agent.run())
        self.assertEqual(len(df), 1)
        self.assertIn("1 ligne", reply)
        self.assertEqual(report["validation"]["last_issue_resolution_metrics"]["rows_after"], 1)

    def test_non_missing_issue_does_not_trigger_missing_value_edit(self):
        agent = self.resolver('remplir avec "test"', "outliers")
        report, df, _ = asyncio.run(agent.run())
        pd.testing.assert_frame_equal(df, agent.df)
        self.assertEqual(len(report["remaining_issues"]), 1)

    def test_tool_cannot_change_another_column_or_invent_value(self):
        agent = self.resolver('remplir avec "test"')
        toolkit = CleaningToolkit(agent.df)
        tools = {tool.__name__: tool for tool in agent._chat_tools(toolkit, agent._find_issue())}
        self.assertNotIn("cap_outliers", tools)
        self.assertFalse(tools["manual_fill_missing"]("other", "test")["kept"])
        self.assertFalse(tools["manual_fill_missing"]("email", "invented")["kept"])
        self.assertFalse(tools["manual_drop_rows_with_missing"]("email")["kept"])
        self.assertEqual(toolkit.actions, [])

    def test_unrelated_accepted_action_does_not_resolve_issue(self):
        agent = self.resolver("explain", "outliers")
        def fake_turn(issue):
            agent.toolkit = CleaningToolkit(agent.df)
            agent.toolkit.manual_fill_missing("other", "x")
            return "Other column changed"
        agent._run_deterministic_turn = fake_turn
        report, _, _ = asyncio.run(agent.run())
        self.assertEqual(len(report["remaining_issues"]), 1)

    def test_numeric_value_preserves_type_and_plain_email_shortcut(self):
        self.assertEqual(parse_edit("remplir avec 0"), ("fill", 0))
        self.assertEqual(parse_edit("unknown@example.com"), ("fill", "unknown@example.com"))
        self.assertEqual(parse_edit('remplir avec {"a":1}'), (None, None))

    def test_message_length_is_bounded(self):
        with self.assertRaises(ValidationError):
            IssueMessageRequest(issue_id="x", message="x" * 4001)

    def test_model_cannot_hide_unaddressed_profiling_findings(self):
        df = pd.DataFrame({"x": [1, None]})
        profile = {"problems": [{"column": "x", "type": "high_missingness"}]}
        report = _finalize_cleaning_result(df, CleaningToolkit(df), profile, [])
        self.assertEqual(len(report["remaining_issues"]), 1)

    def test_flagging_does_not_count_as_fixing(self):
        df = pd.DataFrame({"x": [1, 2, 10000]})
        toolkit = CleaningToolkit(df)
        toolkit.actions = [{"column": "x", "problem": "outliers", "action": "flag_domain_implausible"}]
        profile = {"problems": [{"column": "x", "type": "outliers"}]}
        report = _finalize_cleaning_result(df, toolkit, profile, [])
        self.assertEqual(len(report["remaining_issues"]), 1)

    def test_duplicate_names_merge_history_and_remeasure(self):
        df = pd.DataFrame({"email": [None, "x", "y"]})
        report = {"remaining_issues": [
            {"id": "a", "column": "email", "type": "missing_values", "evidence": "75 missing"},
            {"id": "b", "column": "email", "type": "high_missingness", "evidence": "76 missing"},
        ], "issue_conversations": {"a": [{"role": "user", "text": "first"}],
                                    "b": [{"role": "user", "text": "second"}]}}
        result = refresh_cleaning_report(report, df)
        self.assertEqual(len(result["remaining_issues"]), 1)
        issue = result["remaining_issues"][0]
        self.assertEqual(issue["affected_rows"], 1)
        self.assertEqual(issue["affected_percent"], 33.33)
        self.assertEqual(len(result["issue_conversations"]["a"]), 2)
        self.assertNotIn("b", result["issue_conversations"])
        self.assertEqual(refresh_cleaning_report(result, df), result)

    def test_short_delete_command_updates_real_data_and_score(self):
        for text in ["supprimer", "donc supprime le"]:
            agent = self.resolver(text)
            before = agent.report["quality_after"]
            result, df, _ = asyncio.run(agent.run())
            self.assertEqual(len(df), 1)
            self.assertGreater(result["quality_after"], before)
            self.assertEqual(result["issue_conversations"]["email-issue"][-1]["execution_status"], "applied")

    def test_text_without_action_cannot_confirm_deletion(self):
        agent = self.resolver("explique")
        agent._run_deterministic_turn = lambda issue: "C'est fait ! J'ai supprimé les lignes."
        report, df, reply = asyncio.run(agent.run())
        self.assertIn("Aucune modification", reply)
        self.assertEqual(report["issue_conversations"]["email-issue"][-1]["execution_status"], "no_change")
        pd.testing.assert_frame_equal(df, agent.df)

    def test_audit_columns_do_not_inflate_quality(self):
        agent = self.resolver("explain")
        df = agent.df.copy()
        baseline = refresh_cleaning_report(agent.report, df)["quality_after"]
        df["email_temporal_anomaly"] = False
        report = dict(agent.report, actions=[{"action": "flag_temporal_anomaly", "column": "email"}])
        report["validation"] = {}  # Exercise legacy inference, not saved column list.
        self.assertEqual(refresh_cleaning_report(report, df)["quality_after"], baseline)

    def test_loading_saved_report_refreshes_without_recleaning(self):
        from backend import main
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports" / "csv").mkdir(parents=True)
            (root / "cleaned" / "csv").mkdir(parents=True)
            agent = self.resolver("explain")
            report = dict(agent.report, quality_after=0)
            report["remaining_issues"].append({"id": "alias", "column": "email", "type": "high_missingness"})
            path = root / "reports" / "csv" / "sample_cleaning.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            agent.df.to_pickle(root / "cleaned" / "csv" / "sample_cleaned.pkl")
            with patch.object(main, "CLEANING_REPORTS_DIR", root / "reports"), patch.object(main, "CLEANED_DIR", root / "cleaned"):
                response = TestClient(main.app).get("/cleanings/csv/sample")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["remaining_issues"]), 1)
            self.assertGreater(response.json()["quality_after"], 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["quality_after"], 0)

    def test_keep_closes_discussion_without_improving_score(self):
        agent = self.resolver("conserver les valeurs inchangées")
        before = agent.report["quality_after"]
        report, df, reply = asyncio.run(agent.run())
        self.assertEqual(report["remaining_issues"], [])
        self.assertEqual(len(report["accepted_issues"]), 1)
        self.assertEqual(report["status"], "reviewed")
        self.assertEqual(report["quality_after"], before)
        self.assertIn("clôturé", reply)
        self.assertEqual(report["actions"], [])
        pd.testing.assert_frame_equal(df, agent.df)
        self.assertEqual(refresh_cleaning_report(report, df)["remaining_issues"], [])
        reopened = IssueResolutionAgent(df, report, "email-issue", "Rouvrir ce problème", decision="reopen")
        opened_report, _, _ = asyncio.run(reopened.run())
        self.assertEqual(len(opened_report["remaining_issues"]), 1)
        self.assertEqual(opened_report["accepted_issues"], [])

    def test_old_keep_message_is_migrated_but_question_is_not(self):
        for message, accepted in [("conserver les valeurs inchangées", True),
                                  ("faut-il conserver les valeurs inchangées ?", False)]:
            agent = self.resolver("explain")
            agent.report["issue_conversations"] = {"email-issue": [{"role": "user", "text": message}]}
            result = refresh_cleaning_report(agent.report, agent.df)
            self.assertEqual(bool(result["accepted_issues"]), accepted)
            self.assertEqual(refresh_cleaning_report(result, agent.df), result)

    def test_review_api_persists_and_can_reopen(self):
        from backend import main
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports/csv").mkdir(parents=True)
            (root / "cleaned/csv").mkdir(parents=True)
            agent = self.resolver("explain")
            report_path = root / "reports/csv/sample_cleaning.json"
            report_path.write_text(json.dumps(agent.report), encoding="utf-8")
            data_path = root / "cleaned/csv/sample_cleaned.pkl"
            agent.df.to_pickle(data_path)
            with patch.object(main, "STORAGE_DIR", root), patch.object(main, "CLEANING_REPORTS_DIR", root / "reports"), patch.object(main, "CLEANED_DIR", root / "cleaned"):
                client = TestClient(main.app)
                url = "/cleanings/csv/sample/issues/message"
                closed = client.post(url, json={"issue_id": "email-issue", "decision": "keep"})
                self.assertEqual(closed.status_code, 200, closed.text)
                refreshed = client.get("/cleanings/csv/sample").json()
                self.assertNotIn('email-issue', [i['id'] for i in refreshed['remaining_issues']])
                self.assertIn('email-issue', [i['id'] for i in refreshed['accepted_issues']])
                self.assertEqual(len(json.loads(report_path.read_text(encoding="utf-8"))["accepted_issues"]), 1)
                opened = client.post(url, json={"issue_id": "email-issue", "decision": "reopen"})
                self.assertEqual(opened.status_code, 200, opened.text)
                self.assertIn('email-issue', [i['id'] for i in opened.json()['remaining_issues']])
                pd.testing.assert_frame_equal(pd.read_pickle(data_path), agent.df)


if __name__ == "__main__":
    unittest.main()
