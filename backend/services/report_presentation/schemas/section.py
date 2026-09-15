from pydantic import BaseModel, Field


class Section(BaseModel):
    id: str
    title: str
    level: int
    text: str = ''
    blockIds: list[str] = Field(default_factory=list)
    imageIds: list[str] = Field(default_factory=list)
