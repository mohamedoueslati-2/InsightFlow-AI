from pydantic import BaseModel, Field
from .asset import Asset
from .block import Block
from .section import Section


class Report(BaseModel):
    title: str
    plainText: str
    markdown: str
    sections: list[Section]


class Extraction(BaseModel):
    jobId: str
    report: Report
    assets: list[Asset]
    blocks: list[Block]
    warnings: list[str] = Field(default_factory=list)
