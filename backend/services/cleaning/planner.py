import json
from .schemas import CleaningPlan
from .generative_prompts import PLANNER
from .llm import GeminiStructuredClient


def dataset_evidence(df, profile, request='', history=None):
    # Keep text as serialized strings, separate from deterministic numeric metrics.
    samples = json.loads(df.head(12).to_json(orient='records', date_format='iso'))
    for row in samples:
        for key, value in row.items():
            if isinstance(value, str):
                row[key] = value[:300]
    return {'TRUSTED PROFILE METRICS': {'rows': len(df), 'columns': len(df.columns),
                'missing_cells': int(df.isna().sum().sum()), 'duplicates': int(df.duplicated().sum())},
            'UNTRUSTED DATASET CONTENT': {'profile': {key: profile[key] for key in
                ('problems', 'user_request', 'available_proposals', 'comparison_mode') if key in profile},
                'column_metadata': {str(c): str(t) for c, t in df.dtypes.items()},
                'representative_samples': samples},
            'USER REQUEST': request, 'CONVERSATION HISTORY (context only)': (history or [])[-20:]}


class CleaningPlanner:
    def __init__(self, llm=None):
        self.llm = llm or GeminiStructuredClient()

    async def create_plan(self, df, profile, request='', history=None):
        result = await self.llm.generate(PLANNER, dataset_evidence(df, profile, request, history), CleaningPlan)
        ids = {p['id'] for p in profile.get('problems', [])}
        if len({item.id for item in result.items}) != len(result.items):
            raise ValueError('Duplicate plan item identifiers')
        if any(item.problem_id not in ids for item in result.items):
            raise ValueError('Plan targets an unknown problem')
        return result
