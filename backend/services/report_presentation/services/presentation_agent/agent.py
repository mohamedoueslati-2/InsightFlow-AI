from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from ...schemas.presentation import (
    PresentationAgentTurn,
    PresentationConversationMessage,
    PresentationInteractiveTurn,
    PresentationClaim,
    PresentationPlan,
    PresentationPreferences,
    PresentationSetupSuggestion,
    SourceReference,
)
from .gemini_key_manager import GeminiKeyManager, load_api_keys, model_name
from .prompts import (
    PRESENTATION_SYSTEM_INSTRUCTION,
    INTERACTIVE_PRESENTATION_SYSTEM_INSTRUCTION,
    SETUP_SUGGESTION_SYSTEM_INSTRUCTION,
    build_setup_suggestion_prompt,
    build_user_prompt,
    build_interactive_prompt,
)
from .toolkit import PresentationToolkit
from .visual_association import VisualAssociationResolver
from ..presentation_catalog import PresentationCatalog

logger = logging.getLogger(__name__)

try:
    import google.adk as adk
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    _ADK_AVAILABLE = True
except ModuleNotFoundError:
    adk = None  # type: ignore[assignment]
    InMemorySessionService = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _ADK_AVAILABLE = False


_TEMPORARY_PATTERNS = ('429', 'quota', 'rate limit', 'resourceexhausted', 'too many requests')
_CLOSING_WORDS = ('recommend', 'conclusion', 'next step', 'action', 'priority', 'closing')


def runtime_status() -> dict[str, Any]:
    keys = load_api_keys()
    return {
        'configured': bool(keys),
        'adkAvailable': _ADK_AVAILABLE,
        'model': model_name(),
        'keyCount': len(keys),
    }


def _clean_json_text(text: str) -> str:
    value = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", value, re.IGNORECASE)
    return (match.group(1) if match else value).strip()


def _is_temporary_error(exc: Exception) -> bool:
    text = f'{type(exc).__name__} {exc}'.lower()
    return any(pattern in text for pattern in _TEMPORARY_PATTERNS)


class PresentationPlanningAgent:
    def __init__(self, catalog: PresentationCatalog):
        self.catalog = catalog
        self.toolkit = PresentationToolkit(catalog)
        self.key_manager = GeminiKeyManager()
        self.model = model_name()
        self.visual_resolver = VisualAssociationResolver(catalog)

    def _source_is_valid(self, source: SourceReference, sections: set[str], tables: set[str], assets: dict[str, dict]) -> bool:
        return (
            (source.sourceType == 'section' and source.sourceId in sections)
            or (source.sourceType == 'table' and source.sourceId in tables)
            or (source.sourceType == 'asset' and source.sourceId in assets)
        )

    def _validate_sources(self, plan: PresentationPlan) -> PresentationPlan:
        sections = self.catalog.valid_section_ids()
        tables = self.catalog.valid_table_ids()
        assets = self.catalog.assets_by_id()

        if plan.jobId != self.catalog.job_dir.name:
            raise ValueError('Agent returned an invalid jobId.')

        for slide in plan.slides:
            for source in slide.sources:
                if not self._source_is_valid(source, sections, tables, assets):
                    raise ValueError(f'Agent referenced unknown {source.sourceType} source: {source.sourceId}')

            for claim in slide.claims:
                if not claim.sources:
                    raise ValueError('Every presentation claim must include source provenance.')
                for source in claim.sources:
                    if not self._source_is_valid(source, sections, tables, assets):
                        raise ValueError(
                            f'Agent claim referenced unknown {source.sourceType} source: {source.sourceId}'
                        )

            for visual in slide.visuals:
                asset = assets.get(visual.assetId)
                if asset is None:
                    raise ValueError(f'Agent referenced unknown asset: {visual.assetId}')
                if visual.filename != asset.get('filename'):
                    raise ValueError(f'Agent changed filename for asset {visual.assetId}.')
                # Enrich the plan deterministically with the extractor's visual association metadata.
                self.visual_resolver.enrich_visual(visual, asset)
        return plan

    def _ensure_grounded_claims(self, plan: PresentationPlan) -> PresentationPlan:
        """Guarantee a provenance-bearing semantic statement even if a model omitted claims.

        The fallback intentionally labels the key message as interpretation, not fact,
        so we never upgrade model prose into a source fact automatically.
        """
        for slide in plan.slides:
            if slide.claims or not slide.sources:
                continue
            claim_type = 'recommendation' if (
                slide.storyRole == 'closing'
                or any(word in f'{slide.purpose} {slide.title}'.lower() for word in _CLOSING_WORDS)
            ) else 'interpretation'
            slide.claims = [
                PresentationClaim(
                    type=claim_type,
                    text=slide.keyMessage,
                    sources=slide.sources[:3],
                )
            ]
        return plan

    def _ensure_source_grounded_closing(self, plan: PresentationPlan) -> PresentationPlan:
        """Guarantee a real ending when the source report contains explicit closing guidance."""
        guidance = self.catalog.get_closing_guidance()
        if not guidance.get('hasClosingGuidance') or len(plan.slides) < 4:
            return plan

        last = plan.slides[-1]
        closing_text = f'{last.title} {last.purpose}'.lower()
        has_closing_shape = last.storyRole == 'closing' or any(word in closing_text for word in _CLOSING_WORDS)
        has_recommendation_claim = any(c.type == 'recommendation' for c in last.claims)
        section_id = guidance.get('sectionId')
        source = SourceReference(sourceType='section', sourceId=section_id) if section_id else None

        if has_closing_shape and has_recommendation_claim:
            last.storyRole = 'closing'
            if source and all(not (s.sourceType == 'section' and s.sourceId == section_id) for s in last.sources):
                last.sources.append(source)
            return plan

        sentences = [
            sentence.strip()
            for sentence in re.split(r'(?<=[.!?])\s+', guidance.get('text') or '')
            if sentence.strip()
        ]
        recommendations = guidance.get('recommendationSentences') or []
        interpretations = [sentence for sentence in sentences if sentence not in recommendations]

        # Reuse any visual already selected for this same closing section; discard a
        # cross-section visual rather than forcing it onto the conclusion slide.
        assets = self.catalog.assets_by_id()
        last.visuals = [
            visual for visual in last.visuals
            if not section_id or (assets.get(visual.assetId) or {}).get('sectionId') == section_id
        ]

        last.title = 'Conclusion & Strategic Recommendations'
        last.purpose = 'recommendations'
        last.storyRole = 'closing'
        last.keyMessage = recommendations[0] if recommendations else (sentences[-1] if sentences else guidance.get('text'))
        last.bullets = (interpretations[:1] + recommendations[:3])[:4]
        if source:
            last.sources = [source]
            claims: list[PresentationClaim] = []
            for sentence in interpretations[:1]:
                claims.append(PresentationClaim(type='interpretation', text=sentence, sources=[source]))
            for sentence in recommendations[:3]:
                claims.append(PresentationClaim(type='recommendation', text=sentence, sources=[source]))
            if not claims and last.keyMessage:
                claims.append(PresentationClaim(type='interpretation', text=last.keyMessage, sources=[source]))
            last.claims = claims
        return plan

    def _post_process_plan(self, plan: PresentationPlan, ensure_closing: bool = True) -> PresentationPlan:
        plan = self._validate_sources(plan)
        if plan.slides and plan.slides[0].storyRole == 'evidence':
            plan.slides[0].storyRole = 'opening'
        plan = self._ensure_grounded_claims(plan)
        if ensure_closing:
            plan = self._ensure_source_grounded_closing(plan)
        # Use extractor chart titles / adjacent paragraphs to improve visual placement.
        plan = self.visual_resolver.resolve(plan)
        # Revalidate after deterministic enrichment/reassignment/fallback.
        return self._validate_sources(PresentationPlan.model_validate(plan.model_dump()))

    async def _run_once(self, key: str, preferences: PresentationPreferences, current_plan: PresentationPlan | None) -> PresentationAgentTurn:
        if not _ADK_AVAILABLE:
            raise RuntimeError('Google ADK dependencies are not installed.')

        # Google GenAI / ADK reads Google AI Studio keys from environment.
        previous_google = os.environ.get('GOOGLE_API_KEY')
        previous_gemini = os.environ.get('GEMINI_API_KEY')
        os.environ['GOOGLE_API_KEY'] = key
        # Avoid conflicting values in SDK resolution while this run executes.
        os.environ.pop('GEMINI_API_KEY', None)
        try:
            agent = adk.Agent(
                name='presentation_planning_agent',
                model=self.model,
                instruction=PRESENTATION_SYSTEM_INSTRUCTION,
                tools=self.toolkit.get_tools(),
                output_schema=PresentationAgentTurn,
            )
            session_service = InMemorySessionService()
            runner = adk.Runner(
                app_name='word_report_presentation_assistant',
                agent=agent,
                session_service=session_service,
                auto_create_session=True,
            )
            job_id = self.catalog.job_dir.name
            message = types.Content(
                role='user',
                parts=[types.Part.from_text(text=build_user_prompt(preferences, current_plan))],
            )
            final_text = ''
            async for event in runner.run_async(
                user_id=f'presentation_user_{job_id}',
                session_id=f'presentation_session_{job_id}',
                new_message=message,
            ):
                if event.message and event.message.parts:
                    for part in event.message.parts:
                        if getattr(part, 'text', None):
                            final_text = part.text
            if not final_text:
                raise RuntimeError('Presentation Agent returned no final response.')
            parsed = json.loads(_clean_json_text(final_text))
            turn = PresentationAgentTurn.model_validate(parsed)
            turn.plan.jobId = job_id
            turn.plan.audience = preferences.audience
            turn.plan.objective = preferences.objective
            turn.plan.language = preferences.language
            turn.plan.durationMinutes = preferences.durationMinutes
            turn.plan.style = preferences.style
            if len(turn.plan.slides) != preferences.slideCount:
                raise ValueError(
                    f'Presentation Agent returned {len(turn.plan.slides)} slides; '
                    f'{preferences.slideCount} were requested.'
                )
            turn.plan.slideCount = preferences.slideCount
            for index, slide in enumerate(turn.plan.slides, 1):
                slide.order = index
            turn.plan = self._post_process_plan(PresentationPlan.model_validate(turn.plan.model_dump()))
            return turn
        finally:
            if previous_google is None:
                os.environ.pop('GOOGLE_API_KEY', None)
            else:
                os.environ['GOOGLE_API_KEY'] = previous_google
            if previous_gemini is None:
                os.environ.pop('GEMINI_API_KEY', None)
            else:
                os.environ['GEMINI_API_KEY'] = previous_gemini

    @staticmethod
    def _changed_slide_ids(before: PresentationPlan, after: PresentationPlan) -> list[str]:
        before_map = {slide.id: slide.model_dump() for slide in before.slides}
        after_map = {slide.id: slide.model_dump() for slide in after.slides}
        changed: list[str] = []
        for slide in before.slides:
            if slide.id not in after_map or before_map[slide.id] != after_map[slide.id]:
                changed.append(slide.id)
        for slide in after.slides:
            if slide.id not in before_map and slide.id not in changed:
                changed.append(slide.id)
        return changed

    async def _chat_once(
        self,
        key: str,
        message: str,
        current_plan: PresentationPlan,
        conversation: list[PresentationConversationMessage] | None = None,
    ) -> PresentationInteractiveTurn:
        if not _ADK_AVAILABLE:
            raise RuntimeError('Google ADK dependencies are not installed.')

        previous_google = os.environ.get('GOOGLE_API_KEY')
        previous_gemini = os.environ.get('GEMINI_API_KEY')
        os.environ['GOOGLE_API_KEY'] = key
        os.environ.pop('GEMINI_API_KEY', None)
        try:
            agent = adk.Agent(
                name='interactive_presentation_agent',
                model=self.model,
                instruction=INTERACTIVE_PRESENTATION_SYSTEM_INSTRUCTION,
                tools=self.toolkit.get_tools(),
                output_schema=PresentationInteractiveTurn,
            )
            session_service = InMemorySessionService()
            runner = adk.Runner(
                app_name='word_report_interactive_presentation',
                agent=agent,
                session_service=session_service,
                auto_create_session=True,
            )
            job_id = self.catalog.job_dir.name
            prompt = build_interactive_prompt(message, current_plan, conversation or [])
            new_message = types.Content(
                role='user',
                parts=[types.Part.from_text(text=prompt)],
            )
            final_text = ''
            async for event in runner.run_async(
                user_id=f'presentation_user_{job_id}',
                session_id=f'presentation_chat_{job_id}',
                new_message=new_message,
            ):
                if event.message and event.message.parts:
                    for part in event.message.parts:
                        if getattr(part, 'text', None):
                            final_text = part.text
            if not final_text:
                raise RuntimeError('Interactive Presentation Agent returned no final response.')

            parsed = json.loads(_clean_json_text(final_text))
            plan_data = parsed.get('plan') or {}
            slides_data = plan_data.get('slides') or []
            plan_data['jobId'] = job_id
            plan_data['slideCount'] = len(slides_data)
            for index, slide in enumerate(slides_data, 1):
                slide['order'] = index
            parsed['plan'] = plan_data
            turn = PresentationInteractiveTurn.model_validate(parsed)

            # Interactive mode respects explicit user storytelling choices. It still
            # validates provenance and visual placement, but does not forcibly restore
            # a closing slide that the user explicitly chose to remove.
            turn.plan = self._post_process_plan(
                PresentationPlan.model_validate(turn.plan.model_dump()),
                ensure_closing=False,
            )
            changed = self._changed_slide_ids(current_plan, turn.plan)
            turn.planChanged = current_plan.model_dump() != turn.plan.model_dump()
            turn.changedSlideIds = changed
            return turn
        finally:
            if previous_google is None:
                os.environ.pop('GOOGLE_API_KEY', None)
            else:
                os.environ['GOOGLE_API_KEY'] = previous_google
            if previous_gemini is None:
                os.environ.pop('GEMINI_API_KEY', None)
            else:
                os.environ['GEMINI_API_KEY'] = previous_gemini

    async def _suggest_once(self, key: str) -> PresentationSetupSuggestion:
        if not _ADK_AVAILABLE:
            raise RuntimeError('Google ADK dependencies are not installed.')

        previous_google = os.environ.get('GOOGLE_API_KEY')
        previous_gemini = os.environ.get('GEMINI_API_KEY')
        os.environ['GOOGLE_API_KEY'] = key
        os.environ.pop('GEMINI_API_KEY', None)
        try:
            agent = adk.Agent(
                name='presentation_setup_advisor',
                model=self.model,
                instruction=SETUP_SUGGESTION_SYSTEM_INSTRUCTION,
                tools=self.toolkit.get_tools(),
                output_schema=PresentationSetupSuggestion,
            )
            session_service = InMemorySessionService()
            runner = adk.Runner(
                app_name='word_report_presentation_setup',
                agent=agent,
                session_service=session_service,
                auto_create_session=True,
            )
            job_id = self.catalog.job_dir.name
            message = types.Content(
                role='user',
                parts=[types.Part.from_text(text=build_setup_suggestion_prompt())],
            )
            final_text = ''
            async for event in runner.run_async(
                user_id=f'presentation_user_{job_id}',
                session_id=f'presentation_setup_{job_id}',
                new_message=message,
            ):
                if event.message and event.message.parts:
                    for part in event.message.parts:
                        if getattr(part, 'text', None):
                            final_text = part.text
            if not final_text:
                raise RuntimeError('Presentation Agent returned no setup suggestion.')
            parsed = json.loads(_clean_json_text(final_text))
            return PresentationSetupSuggestion.model_validate(parsed)
        finally:
            if previous_google is None:
                os.environ.pop('GOOGLE_API_KEY', None)
            else:
                os.environ['GOOGLE_API_KEY'] = previous_google
            if previous_gemini is None:
                os.environ.pop('GEMINI_API_KEY', None)
            else:
                os.environ['GEMINI_API_KEY'] = previous_gemini

    async def suggest_setup(self) -> PresentationSetupSuggestion:
        if not self.key_manager.configured():
            raise RuntimeError('Presentation Agent is not configured. Add a Google AI Studio Gemini API key.')
        last_error: Exception | None = None
        attempted: set[int] = set()
        while len(attempted) < len(self.key_manager.keys):
            selected = self.key_manager.next_key()
            if selected is None:
                break
            index, key = selected
            if index in attempted:
                break
            attempted.add(index)
            try:
                return await self._suggest_once(key)
            except Exception as exc:
                last_error = exc
                if _is_temporary_error(exc):
                    logger.warning('Temporary Gemini error using %s; cooling down key.', self.key_manager.safe_label(index))
                    self.key_manager.cooldown(index)
                    continue
                raise
        raise RuntimeError('All configured Gemini API keys are temporarily unavailable.') from last_error

    async def chat(
        self,
        message: str,
        current_plan: PresentationPlan,
        conversation: list[PresentationConversationMessage] | None = None,
    ) -> PresentationInteractiveTurn:
        if not self.key_manager.configured():
            raise RuntimeError('Presentation Agent is not configured. Add a Google AI Studio Gemini API key.')
        last_error: Exception | None = None
        attempted: set[int] = set()
        while len(attempted) < len(self.key_manager.keys):
            selected = self.key_manager.next_key()
            if selected is None:
                break
            index, key = selected
            if index in attempted:
                break
            attempted.add(index)
            try:
                return await self._chat_once(key, message, current_plan, conversation)
            except Exception as exc:
                last_error = exc
                if _is_temporary_error(exc):
                    logger.warning('Temporary Gemini error using %s; cooling down key.', self.key_manager.safe_label(index))
                    self.key_manager.cooldown(index)
                    continue
                raise
        raise RuntimeError('All configured Gemini API keys are temporarily unavailable.') from last_error

    async def generate(self, preferences: PresentationPreferences, current_plan: PresentationPlan | None = None) -> PresentationAgentTurn:
        if not self.key_manager.configured():
            raise RuntimeError('Presentation Agent is not configured. Add a Google AI Studio Gemini API key.')
        last_error: Exception | None = None
        attempted: set[int] = set()
        while len(attempted) < len(self.key_manager.keys):
            selected = self.key_manager.next_key()
            if selected is None:
                break
            index, key = selected
            if index in attempted:
                break
            attempted.add(index)
            try:
                return await self._run_once(key, preferences, current_plan)
            except Exception as exc:
                last_error = exc
                if _is_temporary_error(exc):
                    logger.warning('Temporary Gemini error using %s; cooling down key.', self.key_manager.safe_label(index))
                    self.key_manager.cooldown(index)
                    continue
                raise
        raise RuntimeError('All configured Gemini API keys are temporarily unavailable.') from last_error
