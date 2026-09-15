from __future__ import annotations

import json
import zipfile
from pathlib import Path

from ..schemas.presentation import PresentationPlan
from .presentation_agent.plan_service import presentation_directory
from .presentation_agent.prompt_builder import save_presenton_prompt


class PresentationPackageService:
    @staticmethod
    def _selected_visual_paths(job_dir: Path, plan: PresentationPlan) -> list[Path]:
        filenames: list[str] = []
        for slide in plan.slides:
            for visual in slide.visuals:
                if visual.filename not in filenames:
                    filenames.append(visual.filename)

        assets_dir = (job_dir / 'assets').resolve()
        paths: list[Path] = []
        for filename in filenames:
            path = (assets_dir / filename).resolve()
            if path.parent != assets_dir or not path.is_file():
                raise RuntimeError(f'Selected original visual is missing: {filename}')
            paths.append(path)
        return paths

    def build(self, job_dir: Path, plan: PresentationPlan) -> Path:
        source = job_dir / 'source' / 'report.docx'
        if not source.is_file():
            raise RuntimeError('Original report.docx is missing from this extraction job.')

        prompt_path = save_presenton_prompt(job_dir, plan)
        visuals = self._selected_visual_paths(job_dir, plan)
        out_dir = presentation_directory(job_dir)
        package_path = out_dir / 'presentation_handoff.zip'

        readme = (
            'PRESENTATION HANDOFF PACKAGE\n'
            '=============================\n\n'
            'This ZIP was generated from the latest validated Presentation Agent plan.\n\n'
            'Use it manually with your presentation tool (for example Presenton):\n'
            '1. Open presenton_prompt.txt and copy the prompt.\n'
            '2. Upload report.docx.\n'
            '3. Upload the original images from the images/ folder if you want to preserve them.\n'
            '4. Keep presentation_plan.json as the source-of-truth plan / audit copy.\n\n'
            'Nothing was sent automatically to any external service.\n'
        )

        with zipfile.ZipFile(package_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(prompt_path, 'presenton_prompt.txt')
            archive.write(source, 'report.docx')
            archive.writestr('presentation_plan.json', json.dumps(plan.model_dump(mode='json'), ensure_ascii=False, indent=2))
            archive.writestr('README.txt', readme)
            for path in visuals:
                archive.write(path, f'images/{path.name}')

        return package_path
