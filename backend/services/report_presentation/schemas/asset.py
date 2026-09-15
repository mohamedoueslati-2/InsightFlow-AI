from pydantic import BaseModel, Field


class ImageContext(BaseModel):
    sectionId: str | None = None
    sectionTitle: str | None = None
    precedingText: str | None = None
    followingText: str | None = None
    # Richer deterministic association context for presentation planning.
    precedingTexts: list[str] = Field(default_factory=list)
    followingTexts: list[str] = Field(default_factory=list)
    chartTitle: str | None = None
    caption: str | None = None
    associationText: str | None = None
    associationMethod: str | None = None
    associationConfidence: float | None = Field(default=None, ge=0.0, le=1.0)
    documentOrder: int | None = None
    blockId: str | None = None


class Asset(BaseModel):
    id: str
    filename: str
    originalFilename: str
    format: str
    mimeType: str
    widthPx: float | None = None
    heightPx: float | None = None
    relationshipId: str | None = None
    relationshipIds: list[str] = Field(default_factory=list)
    sourcePart: str
    url: str
    sha256: str
    previewSupported: bool
    title: str = ''
    context: ImageContext = Field(default_factory=ImageContext)
    occurrences: list[ImageContext] = Field(default_factory=list)
