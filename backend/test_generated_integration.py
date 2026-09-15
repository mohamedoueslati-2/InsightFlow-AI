"""Complete API journey with mocked Gemini and a REAL Docker sandbox.

Run TEST_CLEANING_DOCKER=1 python -m unittest backend.test_generated_integration.
Use python -m backend.test_generated_integration --serve for isolated UI verification.
"""
import asyncio
from contextlib import ExitStack, contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient
from backend import main
from backend.services.profiling.agent import build_deterministic_baseline_profile
from backend.services.cleaning.planner import CleaningPlanner
from backend.services.cleaning.code_generator import CodeGenerator
from backend.services.cleaning.schemas import CleaningPlan, CleaningPlanItem, GeneratedProgram


def fixture_frame():
    return pd.DataFrame({'label': [' A ', 'B', ' C ', 'D'], 'quantity': [-2, 3, -1, 4]})


async def profile_agent(self):
    report = build_deterministic_baseline_profile(self.df).model_dump()
    report['problems'] = [
        {'scope': 'column', 'column': 'label', 'type': 'inconsistent_formats', 'severity': 'low', 'evidence': 'Leading/trailing whitespace', 'confidence': 1},
        {'scope': 'column', 'column': 'quantity', 'type': 'suspicious_values', 'severity': 'medium', 'evidence': '2 negative values', 'confidence': 1}]
    report['quality_summary']['quality_score'] = 80
    return report


async def planner(self, df, profile, request='', history=None):
    items = []
    for issue in profile.get('problems', []):
        column = issue.get('column')
        if column not in {'label', 'quantity'}:
            continue
        items.append(CleaningPlanItem(id=column, problem_id=issue['id'], columns=[column],
            diagnosis=issue['evidence'], strategy='Retirer les espaces' if column == 'label' else 'Appliquer la valeur absolue',
            expected_effect='Préserver le texte' if column == 'label' else 'Convertir les signes négatifs après revue',
            risk_hint='low' if column == 'label' else 'high', requires_human_review=column != 'label'))
    return CleaningPlan(plan_id='fixture-plan', dataset_understanding='Données de test isolées', items=items,
                        reply='Voici les aperçus demandés.' if request else '')


async def generate(self, item, df, profile, previous_code='', error=None):
    column = item.columns[0]
    method = '.str.strip()' if column == 'label' else '.abs()'
    return GeneratedProgram(code=f'def clean_dataframe(df):\n    working_df = df.copy()\n    working_df[{column!r}] = working_df[{column!r}]{method}\n    return working_df', explanation=item.expected_effect)


@contextmanager
def isolated_app():
    with tempfile.TemporaryDirectory(prefix='generated-cleaning-e2e-') as directory, ExitStack() as stack:
        root = Path(directory)
        for field, value in {'BASE_DIR': root, 'STORAGE_DIR': root, 'UPLOADS_DIR': root/'uploads',
                'DATAFRAMES_DIR': root/'dataframes', 'PROFILES_DIR': root/'profiles',
                'CLEANED_DIR': root/'cleaned', 'CLEANING_REPORTS_DIR': root/'reports'}.items():
            stack.enter_context(patch.object(main, field, value))
        main.init_storage_directories()
        stack.enter_context(patch.object(main.AIProfilingAgent, 'run', profile_agent))
        stack.enter_context(patch.object(CleaningPlanner, 'create_plan', planner))
        stack.enter_context(patch.object(CodeGenerator, 'generate', generate))
        stack.enter_context(patch.dict(os.environ, {'CLEANING_SANDBOX_BACKEND': 'docker', 'CLEANING_APPROVAL_MODE': 'auto_safe'}))
        yield root


def upload(client, extension):
    frame = fixture_frame()
    if extension == 'xlsx':
        content = io.BytesIO()
        frame.to_excel(content, index=False)
        payload = content.getvalue()
    elif extension == 'json':
        payload = frame.to_json(orient='records').encode()
    else:
        payload = frame.to_csv(index=False).encode()
    response = client.post('/upload', files={'file': (f'demo_{extension}.{extension}', payload)})
    assert response.status_code == 200, response.text
    return 'excel' if extension == 'xlsx' else extension, f'demo_{extension}'


@unittest.skipUnless(os.getenv('TEST_CLEANING_DOCKER') == '1', 'Real Docker integration is opt-in')
class GeneratedIntegrationTests(unittest.TestCase):
    def test_csv_excel_json_preview_apply_and_original_immutable(self):
        with isolated_app() as root:
            client = TestClient(main.app)
            for extension in ['csv', 'xlsx', 'json']:
                with self.subTest(extension=extension):
                    kind, stem = upload(client, extension)
                    original_path = root/f'dataframes/{kind}/{stem}_dataframe.pkl'
                    original_bytes = original_path.read_bytes()
                    self.assertEqual(client.post(f'/profile/{kind}/{stem}').status_code, 200)
                    response = client.post(f'/clean/{kind}/{stem}')
                    self.assertEqual(response.status_code, 200, response.text)
                    report = response.json()
                    self.assertEqual(len(report['actions']), 1)
                    self.assertEqual(report['actions'][0]['risk_level'], 'low')
                    issue_id, group = next((k, v) for k, v in report['issue_proposals'].items() if v)
                    proposal = group[0]
                    self.assertEqual(proposal['risk']['level'], 'high')
                    path = root/f'cleaned/{kind}/{stem}_cleaned.pkl'
                    self.assertEqual(pd.read_pickle(path).quantity.tolist(), [-2, 3, -1, 4])
                    chat_url = f'/cleanings/{kind}/{stem}/issues/message'
                    response = client.post(chat_url, json={'issue_id': issue_id, 'proposal_id': proposal['id'], 'decision': 'apply'})
                    self.assertEqual(response.status_code, 200, response.text)
                    applied = response.json()
                    self.assertEqual(pd.read_pickle(path).quantity.tolist(), [2, 3, 1, 4])
                    self.assertEqual(original_path.read_bytes(), original_bytes)
                    self.assertFalse(applied['remaining_issues'])
                    self.assertGreater(applied['quality_after'], report['quality_after'])
                    self.assertEqual(applied['actions'][-1]['generated_code'], proposal['generated_code'])
                    self.assertEqual(client.get(f'/cleanings/{kind}/{stem}').status_code, 200)
                    self.assertTrue(list((root/'cleaning_runs'/kind/stem).glob('*/plan.json')))


if __name__ == '__main__':
    import sys
    if '--serve' in sys.argv:
        import socket
        import uvicorn
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import Response
        with isolated_app():
            client = TestClient(main.app)
            for extension in ['csv', 'xlsx', 'json']:
                upload(client, extension)
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            @main.app.get('/runtime-config.js')
            def runtime():
                return Response(f'window.API_BASE_URL="http://127.0.0.1:{port}";', media_type='text/javascript')
            main.app.mount('/', StaticFiles(directory=Path(__file__).resolve().parents[1]/'frontend', html=True))
            print(f'UI_FIXTURE=http://127.0.0.1:{port}/index.html', flush=True)
            uvicorn.Server(uvicorn.Config(main.app, log_level='warning')).run(sockets=[sock])
    else:
        unittest.main()
