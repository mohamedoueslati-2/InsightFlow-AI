from __future__ import annotations

import json
import os
from pathlib import Path

from ...schemas.presentation import PresentationPlan


def presentation_directory(job_dir: Path) -> Path:
    path = job_dir / 'presentation'
    path.mkdir(parents=True, exist_ok=True)
    return path


def plan_path(job_dir: Path) -> Path:
    return presentation_directory(job_dir) / 'presentation_plan.json'


def load_plan(job_dir: Path) -> PresentationPlan | None:
    path = plan_path(job_dir)
    if not path.is_file():
        return None
    return PresentationPlan.model_validate_json(path.read_text(encoding='utf-8'))


def save_plan(job_dir: Path, plan: PresentationPlan) -> Path:
    path = plan_path(job_dir)
    temp = path.with_suffix('.json.tmp')
    data = plan.model_dump_json(indent=2)
    with temp.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)
    return path
