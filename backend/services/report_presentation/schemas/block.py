from typing import Literal
from pydantic import BaseModel


class Block(BaseModel):
    id: str = ''
    type: Literal['heading', 'paragraph', 'image', 'table']
    order: int = 0
    text: str | None = None
    style: str | None = None
    level: int | None = None
    assetId: str | None = None
    relationshipId: str | None = None
    altText: str | None = None
    sectionId: str | None = None
    rows: list[list[str]] | None = None
    # Table children preserve paragraph/image order within each cell.
    cells: list[list[list['Block']]] | None = None
