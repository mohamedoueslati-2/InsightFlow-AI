from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator


class PresentationCatalog:
    """Read-only normalized view of the deterministic extractor output for one job."""

    def __init__(self, job_dir: Path):
        self.job_dir = job_dir
        self.extraction = json.loads((job_dir / 'extraction.json').read_text(encoding='utf-8'))
        self.presentation_input = json.loads((job_dir / 'presentation_input.json').read_text(encoding='utf-8'))
        self._sections = self._build_sections()
        self._tables = self._build_tables()
        self._assets = self._build_assets()
        self._closing_guidance = self._build_closing_guidance()

    @staticmethod
    def _iter_blocks(blocks: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for block in blocks:
            yield block
            for row in block.get('cells') or []:
                for cell in row:
                    yield from PresentationCatalog._iter_blocks(cell)

    def _build_sections(self) -> list[dict[str, Any]]:
        result = []
        for section in self.extraction.get('report', {}).get('sections', []):
            result.append({
                'id': section.get('id'),
                'title': section.get('title') or 'Untitled section',
                'level': section.get('level'),
                'text': section.get('text') or '',
                'blockIds': section.get('blockIds') or [],
                'imageIds': section.get('imageIds') or [],
            })
        return result

    def _build_tables(self) -> list[dict[str, Any]]:
        sections = {s['id']: s for s in self._sections}
        tables: list[dict[str, Any]] = []
        for block in self.extraction.get('blocks', []):
            if block.get('type') != 'table':
                continue
            rows = block.get('rows') or []
            section = sections.get(block.get('sectionId')) or {}
            title = section.get('title') or f"Table {len(tables) + 1}"
            tables.append({
                'id': block.get('id'),
                'title': title,
                'sectionId': block.get('sectionId'),
                'sectionTitle': section.get('title'),
                'order': block.get('order'),
                'rows': rows,
                'rowCount': len(rows),
                'columnCount': max((len(row) for row in rows), default=0),
            })
        return tables

    def _build_assets(self) -> list[dict[str, Any]]:
        result = []
        for asset in self.extraction.get('assets', []):
            context = asset.get('context') or {}
            result.append({
                'id': asset.get('id'),
                'filename': asset.get('filename'),
                'title': asset.get('title') or asset.get('filename'),
                'format': asset.get('format'),
                'mimeType': asset.get('mimeType'),
                'widthPx': asset.get('widthPx'),
                'heightPx': asset.get('heightPx'),
                'sectionId': context.get('sectionId'),
                'sectionTitle': context.get('sectionTitle'),
                'precedingText': context.get('precedingText'),
                'followingText': context.get('followingText'),
                'precedingTexts': context.get('precedingTexts') or [],
                'followingTexts': context.get('followingTexts') or [],
                'chartTitle': context.get('chartTitle'),
                'caption': context.get('caption'),
                'associationText': context.get('associationText'),
                'associationMethod': context.get('associationMethod'),
                'associationConfidence': context.get('associationConfidence'),
                'documentOrder': context.get('documentOrder'),
                'url': asset.get('url'),
                'sha256': asset.get('sha256'),
                'previewSupported': bool(asset.get('previewSupported')),
            })
        return result

    def _build_closing_guidance(self) -> dict[str, Any]:
        """Find explicit conclusion/recommendation text already present in the DOCX."""
        blocks = list(self._iter_blocks(self.extraction.get('blocks', [])))
        markers: list[tuple[int, dict[str, Any]]] = []
        marker_re = re.compile(
            r'^(?:\d+[.)\-]\s*)?(?:conclusion|recommendations?|strategic recommendations?|next steps?|actions?)\s*:?[\s]*$',
            re.I,
        )
        for index, block in enumerate(blocks):
            text = (block.get('text') or '').strip()
            if text and marker_re.match(text):
                markers.append((index, block))

        if not markers:
            return {
                'hasClosingGuidance': False,
                'marker': None,
                'sectionId': None,
                'sectionTitle': None,
                'text': '',
                'recommendationSentences': [],
                'sourceBlockIds': [],
            }

        # Prefer the last explicit closing marker in document order.
        marker_index, marker_block = markers[-1]
        section_id = marker_block.get('sectionId')
        section = next((s for s in self._sections if s['id'] == section_id), {})
        selected: list[dict[str, Any]] = []
        for block in blocks[marker_index + 1:]:
            if section_id and block.get('sectionId') != section_id:
                break
            if block.get('type') == 'heading':
                break
            text = (block.get('text') or '').strip()
            if text:
                selected.append(block)
            if len(selected) >= 5:
                break

        closing_text = ' '.join((b.get('text') or '').strip() for b in selected).strip()
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', closing_text) if s.strip()]
        recommendation_re = re.compile(
            r'\b(?:should|recommend|recommended|focus on|future|next step|priority|prioriti|consider|optimi[sz]|need to|action)\b',
            re.I,
        )
        recommendation_sentences = [s for s in sentences if recommendation_re.search(s)]

        return {
            'hasClosingGuidance': bool(closing_text),
            'marker': (marker_block.get('text') or '').strip(),
            'sectionId': section_id,
            'sectionTitle': section.get('title'),
            'text': closing_text,
            'recommendationSentences': recommendation_sentences,
            'sourceBlockIds': [b.get('id') for b in selected if b.get('id')],
        }

    @property
    def title(self) -> str:
        return self.extraction.get('report', {}).get('title') or self.presentation_input.get('title') or 'Presentation'

    def overview(self) -> dict[str, Any]:
        return {
            'title': self.title,
            'sectionCount': len(self._sections),
            'tableCount': len(self._tables),
            'assetCount': len(self._assets),
            'hasClosingGuidance': self._closing_guidance['hasClosingGuidance'],
        }

    def list_sections(self) -> list[dict[str, Any]]:
        return [
            {
                'id': s['id'],
                'title': s['title'],
                'level': s['level'],
                'textPreview': s['text'][:500],
                'imageIds': s['imageIds'],
            }
            for s in self._sections
        ]

    def get_section(self, section_id: str) -> dict[str, Any]:
        item = next((s for s in self._sections if s['id'] == section_id), None)
        return item or {'error': 'SECTION_NOT_FOUND', 'sectionId': section_id}

    def list_tables(self) -> list[dict[str, Any]]:
        return [
            {
                'id': t['id'],
                'title': t['title'],
                'sectionId': t['sectionId'],
                'sectionTitle': t['sectionTitle'],
                'order': t['order'],
                'rowCount': t['rowCount'],
                'columnCount': t['columnCount'],
                'previewRows': t['rows'][:5],
            }
            for t in self._tables
        ]

    def get_table(self, table_id: str) -> dict[str, Any]:
        item = next((t for t in self._tables if t['id'] == table_id), None)
        return item or {'error': 'TABLE_NOT_FOUND', 'tableId': table_id}

    def list_assets(self) -> list[dict[str, Any]]:
        return self._assets.copy()

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        item = next((a for a in self._assets if a['id'] == asset_id), None)
        return item or {'error': 'ASSET_NOT_FOUND', 'assetId': asset_id}

    def get_closing_guidance(self) -> dict[str, Any]:
        return self._closing_guidance.copy()

    def valid_section_ids(self) -> set[str]:
        return {str(s['id']) for s in self._sections if s.get('id')}

    def valid_table_ids(self) -> set[str]:
        return {str(t['id']) for t in self._tables if t.get('id')}

    def assets_by_id(self) -> dict[str, dict[str, Any]]:
        return {str(a['id']): a for a in self._assets if a.get('id')}
