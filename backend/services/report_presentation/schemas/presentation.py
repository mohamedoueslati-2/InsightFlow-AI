from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


class PresentationPreferences(BaseModel):
    audience: str = Field(default='General audience', min_length=1, max_length=200)
    objective: str = Field(default='Present the most important findings', min_length=1, max_length=500)
    language: str = Field(default='English', min_length=1, max_length=80)
    durationMinutes: int | None = Field(default=None, ge=1, le=240)
    slideCount: int = Field(default=6, ge=3, le=30)
    style: str = Field(default='Professional', min_length=1, max_length=120)
    instructions: str | None = Field(default=None, max_length=2000)


class PresentationSetupReasoning(BaseModel):
    audience: str | None = Field(default=None, max_length=500)
    objective: str | None = Field(default=None, max_length=500)
    slideCount: str | None = Field(default=None, max_length=500)
    duration: str | None = Field(default=None, max_length=500)


class PresentationSetupSuggestion(BaseModel):
    audience: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=500)
    slideCount: int = Field(ge=3, le=30)
    durationMinutes: int = Field(ge=1, le=240)
    reasoning: PresentationSetupReasoning | None = None


class SourceReference(BaseModel):
    sourceType: Literal['section', 'table', 'asset']
    sourceId: str
    label: str | None = None


class PresentationClaim(BaseModel):
    """A presentation statement with explicit provenance and semantic type."""

    type: Literal['fact', 'interpretation', 'recommendation']
    text: str = Field(min_length=1, max_length=1200)
    sources: list[SourceReference] = Field(min_length=1, max_length=6)


class PresentationVisual(BaseModel):
    assetId: str
    filename: str
    preserveOriginal: bool = True
    fit: Literal['contain', 'cover'] = 'contain'
    # Filled deterministically from the extractor catalog after agent output.
    title: str | None = Field(default=None, max_length=300)
    associationText: str | None = Field(default=None, max_length=1200)
    associationMethod: str | None = Field(default=None, max_length=120)
    associationConfidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PresentationSlide(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    order: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    purpose: str = Field(default='content', min_length=1, max_length=120)
    storyRole: Literal['opening', 'context', 'evidence', 'decision', 'closing'] = 'evidence'
    keyMessage: str = Field(min_length=1, max_length=1000)
    bullets: list[str] = Field(default_factory=list, max_length=8)
    sources: list[SourceReference] = Field(default_factory=list)
    claims: list[PresentationClaim] = Field(default_factory=list, max_length=10)
    visuals: list[PresentationVisual] = Field(default_factory=list, max_length=3)


class PresentationPlan(BaseModel):
    jobId: str
    title: str = Field(min_length=1, max_length=240)
    audience: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=500)
    language: str = Field(min_length=1, max_length=80)
    durationMinutes: int | None = Field(default=None, ge=1, le=240)
    style: str = Field(min_length=1, max_length=120)
    slideCount: int = Field(ge=1, le=30)
    slides: list[PresentationSlide]

    @model_validator(mode='after')
    def validate_plan(self):
        if self.slideCount != len(self.slides):
            raise ValueError('slideCount must equal the number of slides.')
        orders = [slide.order for slide in self.slides]
        if orders != list(range(1, len(self.slides) + 1)):
            raise ValueError('Slide order must be sequential starting at 1.')
        ids = [slide.id for slide in self.slides]
        if len(ids) != len(set(ids)):
            raise ValueError('Slide ids must be unique.')
        return self


class PresentationAgentTurn(BaseModel):
    assistantMessage: str = Field(min_length=1, max_length=2000)
    plan: PresentationPlan


class PresentationChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class PresentationConversationMessage(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=6000)


class PresentationConversation(BaseModel):
    messages: list[PresentationConversationMessage] = Field(default_factory=list, max_length=100)


class PresentationInteractiveTurn(BaseModel):
    assistantMessage: str = Field(min_length=1, max_length=4000)
    planChanged: bool = False
    changedSlideIds: list[str] = Field(default_factory=list, max_length=30)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    plan: PresentationPlan



class GeneratePresentationPlanRequest(BaseModel):
    preferences: PresentationPreferences = Field(default_factory=PresentationPreferences)


class PresentationContext(BaseModel):
    jobId: str
    title: str
    sectionCount: int
    tableCount: int
    assetCount: int
    sections: list[dict]
    tables: list[dict]
    assets: list[dict]
    closingGuidance: dict | None = None
    agentConfigured: bool
    model: str
