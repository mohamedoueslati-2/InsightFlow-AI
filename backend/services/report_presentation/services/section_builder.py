import re
from pathlib import Path
from ..schemas.section import Section
from ..schemas.asset import ImageContext
from .document_parser import flatten


def report_title(blocks, filename):
    text = [b for b in flatten(blocks) if b.text and b.text.strip()]
    for predicate in (lambda b: b.style == 'Title', lambda b: b.type == 'heading' and b.level == 1, lambda b: True):
        candidate = next((b.text.strip() for b in text if predicate(b)), None)
        if candidate:
            return candidate
    return Path(filename).stem


def _is_caption(block) -> bool:
    text = (block.text or '').strip()
    style = (block.style or '').lower()
    return bool(text) and (
        style == 'caption'
        or bool(re.match(r'^(?:Figure|Fig\.|Chart)\s*\d*\s*[:.\-–—]?\s*', text, re.I))
    )


def _is_visual_title(block) -> bool:
    """Identify explicit visual labels without interpreting image pixels."""
    text = (block.text or '').strip()
    if not text:
        return False
    if _is_caption(block):
        return True
    # Common report convention used by the extractor input: 📊 Chart title
    if text.startswith('📊'):
        return True
    # Standalone visual labels such as "Revenue Trend" are intentionally NOT
    # guessed here; only explicit chart/figure markers are treated as titles.
    return bool(re.match(r'^(?:Chart|Figure|Fig\.)\b', text, re.I))


def _clean_visual_title(text: str | None) -> str | None:
    if not text:
        return None
    value = text.strip()
    value = re.sub(r'^📊\s*', '', value)
    return value or None


def build_sections(blocks, assets, title, warnings):
    sections, current = [], None

    # Headings inside table cells remain table content, not report section boundaries.
    def attach(block, section):
        block.sectionId = section.id
        section.blockIds.append(block.id)
        if block.type == 'image' and block.assetId not in section.imageIds:
            section.imageIds.append(block.assetId)
        elif block.type == 'table':
            for row in block.cells or []:
                for cell in row:
                    for child in cell:
                        attach(child, section)
        elif block.text and block.type != 'heading':
            section.text += ('\n\n' if section.text else '') + block.text
        elif block.text and block.id != section.blockIds[0]:
            section.text += ('\n\n' if section.text else '') + block.text

    for block in blocks:
        if current is None or block.type == 'heading':
            current = Section(id=f'section-{len(sections) + 1:03}', title=block.text if block.type == 'heading' else title, level=block.level or 1)
            sections.append(current)
        attach(block, current)

    ordered = list(flatten(blocks))
    by_id = {s.id: s for s in sections}
    by_asset = {a.id: a for a in assets}

    for index, block in enumerate(ordered):
        if block.type != 'image':
            continue

        section = by_id[block.sectionId]
        before_blocks = [
            b for b in reversed(ordered[:index])
            if b.sectionId == section.id and b.text and b.type in ('paragraph', 'heading')
        ]
        after_blocks = [
            b for b in ordered[index + 1:]
            if b.sectionId == section.id and b.text and b.type in ('paragraph', 'heading')
        ]

        before = before_blocks[0] if before_blocks else None
        after = after_blocks[0] if after_blocks else None

        # Rich context: nearest first. This lets the Presentation Agent distinguish
        # a chart label from the paragraph immediately preceding the image.
        preceding_texts = [b.text.strip() for b in before_blocks[:4] if b.text and b.text.strip()]
        following_texts = [b.text.strip() for b in after_blocks[:3] if b.text and b.text.strip()]

        nearby = []
        for distance in (1, 2):
            if index - distance >= 0:
                nearby.append(ordered[index - distance])
            if index + distance < len(ordered):
                nearby.append(ordered[index + distance])
        nearby = [b for b in nearby if b.sectionId == section.id and b.text]
        caption_block = next((b for b in nearby if _is_caption(b)), None)

        # Prefer the nearest explicit chart/figure title BEFORE the image. This is
        # especially important when explanatory paragraphs sit between title and image.
        chart_title_block = next((b for b in before_blocks[:5] if _is_visual_title(b)), None)

        caption = caption_block.text.strip() if caption_block and caption_block.text else None
        chart_title = _clean_visual_title(chart_title_block.text if chart_title_block else None)

        if caption:
            association_text, method, confidence = caption, 'caption', 0.98
        elif block.altText:
            association_text, method, confidence = block.altText.strip(), 'alt_text', 0.95
        elif chart_title:
            association_text, method, confidence = chart_title, 'explicit_chart_title', 0.90
        elif before and before.text:
            association_text, method, confidence = before.text.strip(), 'nearest_preceding_paragraph', 0.72
        elif after and after.text:
            association_text, method, confidence = after.text.strip(), 'nearest_following_paragraph', 0.55
        else:
            association_text, method, confidence = section.title, 'section_title', 0.40

        context = ImageContext(
            sectionId=section.id,
            sectionTitle=section.title,
            precedingText=before.text if before else None,
            followingText=after.text if after else None,
            precedingTexts=preceding_texts,
            followingTexts=following_texts,
            chartTitle=chart_title,
            caption=caption,
            associationText=association_text,
            associationMethod=method,
            associationConfidence=confidence,
            documentOrder=block.order,
            blockId=block.id,
        )
        asset = by_asset[block.assetId]
        asset.occurrences.append(context)

        if len(asset.occurrences) == 1:
            asset.context = context
            # Deterministic title priority. An explicit chart title beats a nearby
            # analytical paragraph so the planner sees what the visual represents.
            short_preceding = next((
                b.text.strip() for b in before_blocks
                if b.type == 'paragraph' and b.text and len(b.text.strip()) <= 120
            ), None)
            asset.title = (
                caption
                or block.altText
                or chart_title
                or short_preceding
                or section.title
                or f'Image {assets.index(asset) + 1}'
            )

    for index, asset in enumerate(assets, 1):
        if not asset.occurrences:
            asset.title = f'Image {index}'
            warnings.append(f'{asset.sourcePart}: extracted but not placed in the main document body (possibly a header, footer, or unused alternative).')
    return sections
