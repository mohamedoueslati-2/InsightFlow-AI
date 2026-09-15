from __future__ import annotations

from pathlib import Path

from ...schemas.presentation import PresentationPlan
from .plan_service import presentation_directory


def build_presenton_prompt(plan: PresentationPlan) -> str:
    lines = [
        f'Create a professional {plan.slideCount}-slide presentation using the supplied analytical Word report and original visuals.',
        '',
        'AUDIENCE', plan.audience, '',
        'OBJECTIVE', plan.objective, '',
        'LANGUAGE', plan.language, '',
        'STYLE', plan.style, '',
    ]
    if plan.durationMinutes:
        lines.extend(['DURATION', f'{plan.durationMinutes} minutes', ''])
    lines.extend([
        'SOURCE ACCURACY RULES',
        '- Use only facts supported by the supplied report.',
        '- Do not invent or change numerical values.',
        '- Distinguish source facts from interpretations and recommendations.',
        '- When an original visual file is specified, use that exact file.',
        '- Do not redraw an original analytical chart with different values.',
        '- Keep slide text concise and presentation-friendly.',
        '- Preserve the final conclusion/recommendations slide when one is specified.',
        '',
    ])
    for slide in plan.slides:
        lines.extend([
            f'SLIDE {slide.order}',
            'STORY ROLE', slide.storyRole,
            'TITLE', slide.title,
            'PURPOSE', slide.purpose,
            'KEY MESSAGE', slide.keyMessage,
        ])
        if slide.bullets:
            lines.append('SUPPORTING POINTS')
            lines.extend(f'- {bullet}' for bullet in slide.bullets)
        if slide.claims:
            lines.append('PROVENANCE')
            for claim in slide.claims:
                refs = ', '.join(f'{s.sourceType}:{s.sourceId}' for s in claim.sources)
                lines.append(f'- {claim.type.upper()}: {claim.text} [sources: {refs}]')
        if slide.visuals:
            lines.append('ORIGINAL VISUALS')
            for visual in slide.visuals:
                label = f' ({visual.title})' if visual.title else ''
                lines.append(f'- {visual.filename}{label} — use the supplied original image; do not recreate it.')
                if visual.associationText:
                    lines.append(f'  Source association: {visual.associationText}')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def save_presenton_prompt(job_dir: Path, plan: PresentationPlan) -> Path:
    path = presentation_directory(job_dir) / 'presenton_prompt.txt'
    path.write_text(build_presenton_prompt(plan), encoding='utf-8', newline='\n')
    return path
