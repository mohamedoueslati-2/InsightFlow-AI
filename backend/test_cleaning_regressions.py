import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pandas as pd
from pydantic import ValidationError
from backend.services.cleaning import llm
from backend.services.cleaning.findings import merge_findings, finding_key
from backend.services.cleaning.reporting import reconcile_report
from backend.services.cleaning.schemas import GeneratedProgram


class CleaningRegressionTests(unittest.TestCase):
    def test_comparison_defers_all_code_until_number_is_chosen(self):
        from backend.services.cleaning.conversation import IssueResolutionAgent
        from backend.services.cleaning.orchestrator import CleaningOrchestrator
        from backend.services.cleaning.schemas import CleaningPlan, CleaningPlanItem
        from backend.services.cleaning.executors.base import ExecutionResult
        frame = pd.DataFrame({'x': [1., None, 3.]})
        issue = dict(id='missing', scope='column', column='x', type='missing_values', evidence='1 missing')
        items = [CleaningPlanItem(id=str(n), problem_id='missing', columns=['x'],
            diagnosis='Missing', strategy=f'Remplir par {n}', expected_effect='Une cellule remplie',
            benefit='Complétude', tradeoff='Valeur estimée', recommendation_rank=n if n < 3 else 0,
            recommendation_reason='Selon le contexte') for n in range(1, 6)]
        planner = Mock(create_plan=AsyncMock(return_value=CleaningPlan(plan_id='p', dataset_understanding='test', items=items)))
        generator = Mock(generate=AsyncMock(return_value=GeneratedProgram(code='def clean_dataframe(df):\n    working_df = df.copy()\n    working_df["x"] = working_df["x"].fillna(2)\n    return working_df')))
        executor = Mock(execute=AsyncMock(return_value=ExecutionResult('success', candidate_dataframe=frame.fillna(2))))
        engine = CleaningOrchestrator(planner=planner, generator=generator, executor=executor)
        report, after, reply = asyncio.run(IssueResolutionAgent(frame, {'remaining_issues':[issue]}, 'missing', 'Compare les solutions', orchestrator=engine).run())
        self.assertIn('5. Remplir par 5', reply)
        self.assertIn('Mon premier choix : option 1', reply)
        self.assertIn('Mon deuxième choix : option 2', reply)
        generator.generate.assert_not_called()
        executor.execute.assert_not_called()
        report, after, reply = asyncio.run(IssueResolutionAgent(frame, report, 'missing', '2', orchestrator=engine).run())
        self.assertTrue(after.equals(frame))
        self.assertEqual(generator.generate.await_count, 1)
        self.assertEqual(generator.generate.call_args.args[0].strategy, 'Remplir par 2')
        report, after, reply = asyncio.run(IssueResolutionAgent(frame, report, 'missing', 'oui', orchestrator=engine).run())
        self.assertTrue(after.equals(frame.fillna(2)))
        self.assertEqual(report['changes_applied'], 1)

    def test_numbered_duplicate_command_prepares_and_applies_in_one_turn(self):
        from backend.services.cleaning.orchestrator import CleaningOrchestrator
        from backend.services.cleaning.conversation import IssueResolutionAgent
        from backend.services.cleaning.schemas import CleaningPlanItem, CleaningPlan
        from backend.services.cleaning.executors.base import ExecutionResult
        from backend.services.cleaning.chat_intent import select_saved_proposal
        frame = pd.DataFrame({'x': [1, 1, 2]})
        candidate = frame.drop_duplicates()
        issue = dict(id='duplicates', scope='dataset', column=None, type='duplicate_records', evidence='1 exact duplicate row')
        item = CleaningPlanItem(id='drop', problem_id='duplicates', scope='dataset', diagnosis='Duplicate', strategy='Deduplicate', expected_effect='Unique rows')
        planner = Mock(create_plan=AsyncMock(return_value=CleaningPlan(plan_id='p', dataset_understanding='test', items=[item])))
        generator = Mock(generate=AsyncMock(return_value=GeneratedProgram(code='def clean_dataframe(df):\n    working_df = df.copy()\n    working_df = working_df.drop_duplicates()\n    return working_df')))
        executor = Mock(execute=AsyncMock(return_value=ExecutionResult('success', candidate_dataframe=candidate)))
        engine = CleaningOrchestrator(planner=planner, generator=generator, executor=executor)
        result, after, reply = asyncio.run(IssueResolutionAgent(frame, {'remaining_issues':[issue], 'actions':[]},
            'duplicates', 'supprimer les 1 lignes dupliquées', orchestrator=engine).run())
        self.assertTrue(after.equals(candidate))
        self.assertEqual(result['changes_applied'], 1)
        self.assertEqual(result['remaining_issues'], [])
        self.assertEqual(executor.execute.await_count, 2)
        self.assertIn('appliquée', reply)
        proposal = result['issue_proposals']['duplicates'][0]
        for message in ['supprimer les 25 lignes dupliquées', 'ne pas supprimer les 1 lignes dupliquées', 'supprimer les 1 lignes dupliquées ?']:
            self.assertIsNone(select_saved_proposal(message, [proposal]))

    def test_explicit_chat_deletion_applies_saved_preview_without_llm(self):
        from backend.services.cleaning.orchestrator import CleaningOrchestrator
        from backend.services.cleaning.conversation import IssueResolutionAgent
        from backend.services.cleaning.schemas import CleaningPlanItem, GeneratedProgram
        from backend.services.cleaning.executors.base import ExecutionResult
        frame = pd.DataFrame({'profit': [1., None, 3.]})
        candidate = frame.dropna(subset=['profit'])
        issue = dict(id='missing', scope='column', column='profit', type='missing_values', evidence='1 missing value')
        generator = Mock(generate=AsyncMock(return_value=GeneratedProgram(code='def clean_dataframe(df):\n    working_df = df.copy()\n    working_df = working_df.dropna(subset=["profit"])\n    return working_df')))
        executor = Mock(execute=AsyncMock(return_value=ExecutionResult('success', candidate_dataframe=candidate)))
        planner = Mock(create_plan=AsyncMock())
        engine = CleaningOrchestrator(planner=planner, generator=generator, executor=executor)
        item = CleaningPlanItem(id='drop', problem_id='missing', columns=['profit'], allow_row_deletion=True,
            diagnosis='Missing profit', strategy='Supprimer les lignes sans profit', expected_effect='One row removed')
        report = {'remaining_issues': [issue], 'actions': []}
        proposal, _, _ = asyncio.run(engine.preview_item(frame, {'problems':[issue]}, item, report))
        report['issue_proposals'] = {'missing': [proposal]}
        result, after, reply = asyncio.run(IssueResolutionAgent(frame, report, 'missing', 'GO SUPPRIMER LES', orchestrator=engine).run())
        self.assertTrue(after.equals(candidate))
        self.assertEqual(result['actions'][0]['resolved_by'], 'user')
        self.assertIn('appliquée', reply)
        self.assertEqual(result['remaining_issues'], [])
        planner.create_plan.assert_not_called()
        self.assertEqual(generator.generate.await_count, 1)

    def test_saved_selection_never_interprets_questions_or_negation_as_consent(self):
        from backend.services.cleaning.chat_intent import select_saved_proposal
        proposal = {'id':'drop', 'validation': {'rows_before':10, 'rows_after':8}}
        for message in ['Ne supprimer les valeurs manquantes', 'supprimer les ?', 'tu recommandes de supprimer les', 'supprimer les ou remplir', 'sans supprimer les']:
            self.assertIsNone(select_saved_proposal(message, [proposal]), message)
        self.assertIsNone(select_saved_proposal('SUPPRIMER LES', [proposal, dict(proposal, id='other')]))
        self.assertEqual(select_saved_proposal('APPLIQUE OPTION 1', [proposal]), proposal)
        self.assertEqual(select_saved_proposal('1', [proposal]), proposal)
        self.assertIsNone(select_saved_proposal('2', [proposal]))
        self.assertIsNone(select_saved_proposal('autre', [proposal]))
        self.assertIsNone(select_saved_proposal('1 ou 2', [proposal]))
        self.assertEqual(select_saved_proposal('SUPPRIMER LZS VALEUR MANQUANTE', [proposal]), proposal)

    def test_report_uses_only_profiling_findings_and_updates_counts(self):
        frame = pd.DataFrame({'country': [None, 'FR'], 'email': [None, None], 'date': ['2099-01-01'] * 2})
        issue = dict(id='country', scope='column', column='country', type='high_missingness', evidence='1 missing value')
        report = {'remaining_issues': [issue], 'actions': []}
        updated = reconcile_report(report, frame)
        self.assertEqual([i['id'] for i in updated['remaining_issues']], ['country'])
        self.assertEqual(updated['remaining_issues'][0]['affected_percent'], 50)
        self.assertEqual(updated['post_clean_profile']['source'], 'remeasured_profiling_findings')
        frame['country'] = frame['country'].fillna('FR')
        self.assertEqual(reconcile_report(updated, frame)['remaining_issues'], [])

    def test_chat_advice_does_not_append_global_plan(self):
        from backend.services.cleaning.conversation import IssueResolutionAgent
        from backend.services.cleaning.schemas import CleaningPlan, CleaningPlanItem
        from backend.services.cleaning.orchestrator import CleaningOrchestrator
        frame = pd.DataFrame({'country': [None, 'FR'], 'region': ['Europe', 'Europe']})
        item = CleaningPlanItem(id='advice', problem_id='country', intent='recommend',
            columns=['country', 'region'], diagnosis='Missing country', strategy='Compare region', expected_effect='Advice')
        planner = Mock(create_plan=AsyncMock(return_value=CleaningPlan(plan_id='p', dataset_understanding='test', items=[item], reply='Comparer les pays de la région.')))
        engine = CleaningOrchestrator(planner=planner, generator=Mock(), executor=Mock())
        report = {'remaining_issues': [dict(id='country', scope='column', column='country', type='missing_values', evidence='1 missing')], 'actions': []}
        for _ in range(2):
            report, after, reply = asyncio.run(IssueResolutionAgent(frame, report, 'country', 'Que recommandes-tu ?', orchestrator=engine).run())
            self.assertNotIn('dépasse', reply)
            self.assertEqual(report['plan']['items'], [])
            self.assertTrue(frame.equals(after))
        self.assertEqual(len(report['issue_conversations']['country']), 4)
        engine.generator.generate.assert_not_called()

    def test_recommendation_does_not_generate_or_execute(self):
        from backend.services.cleaning.orchestrator import CleaningOrchestrator
        from backend.services.cleaning.schemas import CleaningPlanItem
        generator, executor = Mock(), Mock()
        engine = CleaningOrchestrator(generator=generator, executor=executor)
        item = CleaningPlanItem(id='keep', problem_id='missing', intent='recommend',
            columns=['x'], diagnosis='Missing', strategy='Keep values', expected_effect='Unchanged')
        report = {}
        result = asyncio.run(engine.preview_item(pd.DataFrame({'x':[None]}), {}, item, report))
        self.assertEqual(result, (None, None, None))
        self.assertEqual(item.status, 'awaiting_decision')
        generator.generate.assert_not_called()
        executor.execute.assert_not_called()
        self.assertNotIn('code_attempts', report)

    def test_flag_preserves_values_without_claiming_correction(self):
        from backend.services.cleaning.delta import validate_transformation
        from backend.services.cleaning.schemas import CleaningPlanItem
        before = pd.DataFrame({'x': [1., None]})
        after = before.assign(x_missing=before.x.isna())
        item = CleaningPlanItem(id='flag', problem_id='missing', intent='flag',
            columns=['x', 'x_missing'], diagnosis='Missing', strategy='Flag', expected_effect='Visible')
        result = validate_transformation(before, after, item, 1, 1)
        self.assertTrue(result.passed, result.errors)
        self.assertIsNone(result.target_issue_improved)
        after.loc[0, 'x'] = 2
        self.assertFalse(validate_transformation(before, after, item, 1, 1).passed)

    def test_distribution_includes_removed_rows(self):
        from backend.services.cleaning.delta import validate_transformation
        from backend.services.cleaning.schemas import CleaningPlanItem
        before = pd.DataFrame({'x': [1, 1, 10]})
        item = CleaningPlanItem(id='dedup', problem_id='duplicates', scope='dataset',
            diagnosis='Duplicates', strategy='Deduplicate', expected_effect='Unique')
        result = validate_transformation(before, before.drop_duplicates(), item, 1, 0)
        self.assertTrue(result.passed)
        self.assertEqual(result.distribution_changes['x']['before']['mean'], 4)
        self.assertEqual(result.distribution_changes['x']['after']['mean'], 5.5)

    def test_boolean_columns_do_not_break_unrelated_cleaning(self):
        from backend.services.cleaning.delta import validate_transformation
        from backend.services.cleaning.schemas import CleaningPlanItem
        from backend.services.cleaning.risk import assess_risk
        for dtype, values in [('bool', [True, False, True]), ('boolean', [True, False, None])]:
            with self.subTest(dtype=dtype):
                before = pd.DataFrame({'flag': pd.Series(values, dtype=dtype), 'quantity': [-5, 2, 3]})
                after = before.copy()
                after['quantity'] = after['quantity'].abs()
                item = CleaningPlanItem(id='abs', problem_id='negative', columns=['quantity'],
                    diagnosis='Negative quantity', strategy='Absolute value', expected_effect='No negatives')
                result = validate_transformation(before, after, item, 1, 0)
                self.assertTrue(result.passed, result.errors)
                self.assertEqual(result.changed_cells, 1)
                self.assertEqual(result.distribution_changes['flag']['before'], result.distribution_changes['flag']['after'])
                assess_risk(result, item)
                pd.testing.assert_series_equal(before['flag'], after['flag'])

    def test_provider_schema_preserves_contract_and_local_limits(self):
        schema = llm.provider_schema(GeneratedProgram)
        self.assertNotIn('maxLength', json.dumps(schema))
        self.assertEqual(schema['properties']['code']['type'], 'string')
        self.assertIn('code', schema['required'])
        with self.assertRaises(ValidationError):
            GeneratedProgram(code='x' * 20001)

    def test_bad_request_does_not_disable_valid_keys(self):
        manager = Mock(api_keys=['fake'])
        manager.get_next_key.return_value = 'fake'
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.models.generate_content.side_effect = llm.errors.APIError(400, {'error': {'message': 'bad schema'}})
        with patch.object(llm, 'get_gemini_key_manager', return_value=manager), patch.object(llm.genai, 'Client', return_value=SimpleNamespace(aio=client)):
            with self.assertRaisesRegex(llm.LLMUnavailable, 'HTTP 400'):
                asyncio.run(llm.GeminiStructuredClient().generate('test', {}, GeneratedProgram))
        manager.mark_key_failed.assert_not_called()

    def test_duplicate_meanings_preserve_history_and_decisions(self):
        issues = [
            dict(id='future', scope='column', column='date', type='future_dates', evidence='8 future dates'),
            dict(id='old-future', scope='column', column='date', type='suspicious_values', evidence='9 dates are in the future'),
            dict(id='duplicates', scope='dataset', column=None, type='duplicate_records', evidence='25 exact duplicate rows'),
            dict(id='old-duplicates', scope='column', column='id', type='duplicate_records', evidence='25 full row duplicates found'),
        ]
        report = {'remaining_issues': issues, 'accepted_issues': [issues[1]],
                  'issue_conversations': {'future': [{'message': 'one'}], 'old-future': [{'message': 'two'}]},
                  'issue_proposals': {'future': [{'problem_id': 'future', 'code_hash': 'unchanged'}]}}
        merged, accepted = merge_findings(report, [])
        self.assertEqual(len(merged), 2)
        self.assertEqual(accepted, {'old-future'})
        self.assertEqual(len(report['issue_conversations']['old-future']), 2)
        self.assertEqual(report['issue_proposals']['old-future'][0]['code_hash'], 'unchanged')
        self.assertNotEqual(finding_key(issues[2]), finding_key(dict(issues[3], evidence='25 duplicate identifiers')))
        frame = pd.DataFrame({'date': ['2099-01-01', '2000-01-01'], 'id': [1, 2]})
        updated = reconcile_report(report, frame)
        future = next(i for i in updated['accepted_issues'] if i['type'] == 'future_dates')
        self.assertEqual(future['affected_rows'], 1)
        self.assertEqual(future['affected_percent'], 50)
        self.assertEqual(reconcile_report(updated, frame), updated)


if __name__ == '__main__':
    unittest.main()
