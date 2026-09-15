from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ...schemas.presentation import PresentationPlan, PresentationSlide, PresentationVisual
from ..presentation_catalog import PresentationCatalog


_STOPWORDS = {
    'about', 'after', 'again', 'against', 'also', 'among', 'and', 'are', 'because', 'been', 'before',
    'between', 'both', 'business', 'from', 'have', 'into', 'more', 'most', 'over', 'report', 'slide',
    'that', 'their', 'there', 'these', 'this', 'through', 'using', 'with', 'while', 'your', 'total',
}


def _tokens(text: str | None) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{2,}", (text or '').lower())
    return {w for w in words if w not in _STOPWORDS}


def _cosine_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def _slide_text(slide: PresentationSlide) -> str:
    claim_text = ' '.join(claim.text for claim in slide.claims)
    return ' '.join([slide.title, slide.keyMessage, *slide.bullets, claim_text])


def _slide_section_ids(slide: PresentationSlide) -> set[str]:
    return {s.sourceId for s in slide.sources if s.sourceType == 'section'}


@dataclass(frozen=True)
class VisualMatch:
    slide_id: str
    score: float


class VisualAssociationResolver:
    """Deterministically improve asset-to-slide placement using extractor context.

    This never reads image pixels. It only compares the chart title/caption/nearby
    text already extracted from the DOCX against the planned slide content.
    """

    def __init__(self, catalog: PresentationCatalog):
        self.catalog = catalog

    def _score(self, slide: PresentationSlide, asset: dict) -> float:
        slide_tokens = _tokens(_slide_text(slide))
        explicit = asset.get('chartTitle') or asset.get('caption')
        association = asset.get('associationText') or asset.get('title')

        # Explicit chart/caption labels are the strongest signal.
        explicit_score = _cosine_overlap(_tokens(explicit), slide_tokens)
        context_score = _cosine_overlap(_tokens(association), slide_tokens)
        score = (0.72 * explicit_score + 0.28 * context_score) if explicit else context_score

        if asset.get('sectionId') and asset.get('sectionId') in _slide_section_ids(slide):
            score += 0.08
        return min(score, 1.0)

    def enrich_visual(self, visual: PresentationVisual, asset: dict) -> None:
        visual.preserveOriginal = True
        visual.title = asset.get('title')
        visual.associationText = asset.get('associationText')
        visual.associationMethod = asset.get('associationMethod')
        visual.associationConfidence = asset.get('associationConfidence')

    def resolve(self, plan: PresentationPlan) -> PresentationPlan:
        assets = self.catalog.assets_by_id()
        placements: list[tuple[PresentationSlide, PresentationVisual]] = []
        for slide in plan.slides:
            for visual in list(slide.visuals):
                placements.append((slide, visual))

        for current_slide, visual in placements:
            asset = assets.get(visual.assetId)
            if not asset:
                continue
            self.enrich_visual(visual, asset)

            scores = [VisualMatch(slide.id, self._score(slide, asset)) for slide in plan.slides]
            scores.sort(key=lambda item: item.score, reverse=True)
            if not scores:
                continue
            best = scores[0]
            current_score = next((m.score for m in scores if m.slide_id == current_slide.id), 0.0)

            # Reassign only when the extracted association text provides a materially
            # stronger semantic match. This avoids surprising moves on weak evidence.
            if best.slide_id != current_slide.id and best.score >= 0.28 and best.score >= current_score + 0.14:
                target = next(slide for slide in plan.slides if slide.id == best.slide_id)
                if all(existing.assetId != visual.assetId for existing in target.visuals):
                    current_slide.visuals = [v for v in current_slide.visuals if v.assetId != visual.assetId]
                    target.visuals.append(visual)

        return plan
