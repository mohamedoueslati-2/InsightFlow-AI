from lxml import etree
from ..schemas.block import Block
from .relationship_service import NS
from .text_extractor import W, paragraph_style, numbered_level, text_token


def parse_document(root, styles, relationships, assets, warnings):
    by_part = {a.sourcePart: a for a in assets}
    def image_block(node):
        local = etree.QName(node).localname
        rid = node.get('{' + NS['r'] + '}embed') or node.get('{' + NS['r'] + '}id')
        if local not in ('blip', 'svgBlip', 'imagedata') or not rid:
            return None
        target = relationships.get(rid, {}).get('target')
        asset = by_part.get(target)
        if asset is None:
            warnings.append(f'Image relationship {rid} has no extracted embedded asset.')
            return None
        alt = None
        for parent in node.iterancestors():
            if parent.tag in (W + 'drawing', W + 'pict'):
                props = parent.xpath('.//*[local-name()="docPr" or local-name()="cNvPr"]')
                alt = next((p.get('descr') or p.get('title') for p in props if p.get('descr') or p.get('title')), None)
                break
        return Block(type='image', assetId=asset.id, relationshipId=rid, altText=alt)

    def visit(node):
        # A compatibility branch is an alternative representation, not another occurrence.
        if etree.QName(node).localname == 'AlternateContent':
            choices = [c for c in node if etree.QName(c).localname == 'Choice']
            branch = choices[0] if choices else next(iter(node), None)
            if branch is not None:
                yield from visit(branch)
            return
        if node.tag == W + 'del':
            return
        yield node
        for child in node:
            yield from visit(child)

    def paragraph(p):
        style, level = paragraph_style(p, styles)
        tokens, buffer = [], []
        def flush():
            if buffer:
                value = ''.join(buffer)
                if value.strip():
                    tokens.append(Block(type='paragraph', text=value, style=style))
                buffer.clear()
        for node in visit(p):
            item = image_block(node)
            if item:
                flush()
                tokens.append(item)
            else:
                buffer.append(text_token(node))
            if etree.QName(node).localname == 'chart':
                warnings.append('Native Word chart encountered: no reconstruction is performed; only embedded media is extracted.')
        flush()
        texts = [b for b in tokens if b.type == 'paragraph']
        inferred = level or (numbered_level(''.join(b.text for b in texts)) if style.lower() in ('normal', '') else None)
        if inferred and texts:
            texts[0].type, texts[0].level = 'heading', inferred
        return tokens

    def children(parent):
        blocks = []
        for node in parent:
            if node.tag == W + 'p':
                blocks.extend(paragraph(node))
            elif node.tag == W + 'tbl':
                cells = [[children(cell) for cell in row.findall('w:tc', NS)] for row in node.findall('w:tr', NS)]
                rows = [['\n'.join(b.text or '' for b in flatten(cell) if b.type in ('paragraph', 'heading')) for cell in row] for row in cells]
                blocks.append(Block(type='table', rows=rows, cells=cells))
            elif node.tag not in (W + 'sectPr', W + 'tcPr', W + 'tblPr', W + 'tblGrid', W + 'del'):
                blocks.extend(children(node))
        return blocks

    body = root.find('w:body', NS)
    blocks = children(body)
    for index, block in enumerate(flatten(blocks), 1):
        block.id, block.order = f'block-{index:03}', index
    return blocks


def flatten(blocks):
    for block in blocks:
        yield block
        for row in block.cells or []:
            for cell in row:
                yield from flatten(cell)
