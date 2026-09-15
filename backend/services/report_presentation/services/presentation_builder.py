def presentation_input(report, assets):
    by_id = {asset.id: asset for asset in assets}
    return {'title': report.title, 'sections': [
        {'title': s.title, 'content': s.text, 'visuals': [{'assetId': aid, 'url': by_id[aid].url} for aid in s.imageIds]}
        for s in report.sections
    ]}


def markdown(blocks, assets):
    by_id = {a.id: a for a in assets}
    def escape(text):
        return text.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('*', '\\*').replace('_', '\\_').replace('<', '&lt;').replace('>', '&gt;').replace('#', '\\#').replace('`', '\\`')
    def render(block):
        if block.type == 'image':
            asset = by_id[block.assetId]
            return f'![{escape(asset.title)}](assets/{asset.filename})'
        if block.type == 'heading':
            return '#' * min(block.level or 1, 6) + ' ' + escape(block.text or '')
        if block.type == 'table':
            rows = [['<br>'.join(render(b).replace('\n', '<br>').replace('|', '\\|') for b in cell) for cell in row] for row in block.cells or []]
            if not rows:
                return ''
            width = max(map(len, rows))
            lines = ['| ' + ' | '.join(row + [''] * (width - len(row))) + ' |' for row in rows]
            lines.insert(1, '| ' + ' | '.join(['---'] * width) + ' |')
            return '\n'.join(lines)
        return escape(block.text or '')
    return '\n\n'.join(render(b) for b in blocks) + '\n'
