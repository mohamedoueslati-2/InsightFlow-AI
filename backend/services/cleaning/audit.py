"""Persist data-only run audit alongside existing cleaned output/report storage."""
import json
from pathlib import Path


def save_run_audit(storage, file_type, stem, report):
    if not report.get('run_id'):
        return
    root = Path(storage).resolve() / 'cleaning_runs'
    target = (root / file_type / stem / report['run_id']).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Invalid cleaning audit path')
    target.mkdir(parents=True, exist_ok=True)
    for filename, field in [('plan', 'plan'), ('programs', 'generated_programs'),
                            ('attempts', 'code_attempts'), ('validation', 'validation'), ('risk', 'risk_summary')]:
        (target / f'{filename}.json').write_text(json.dumps(report.get(field, {}), ensure_ascii=False, indent=2), encoding='utf-8')
