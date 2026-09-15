import re
from .relationship_service import NS, xml

W = '{' + NS['w'] + '}'


def styles_from_archive(archive):
    if 'word/styles.xml' not in archive.namelist():
        return {}
    styles = {}
    for node in xml(archive.read('word/styles.xml')).findall('w:style', NS):
        name = node.find('w:name', NS)
        base = node.find('w:basedOn', NS)
        outline = node.find('w:pPr/w:outlineLvl', NS)
        styles[node.get(W + 'styleId')] = {
            'name': name.get(W + 'val', '') if name is not None else '',
            'base': base.get(W + 'val') if base is not None else None,
            'outline': outline.get(W + 'val') if outline is not None else None,
        }
    return styles


def paragraph_style(p, styles):
    node = p.find('w:pPr/w:pStyle', NS)
    sid = node.get(W + 'val') if node is not None else 'Normal'
    name = styles.get(sid, {}).get('name') or sid
    current, seen = sid, set()
    level = None
    while current and current not in seen:
        seen.add(current)
        item = styles.get(current, {})
        label = (item.get('name') or current).lower().replace(' ', '')
        if label == 'title':
            return 'Title', 1
        match = re.fullmatch(r'heading([1-9])', label)
        if match:
            level = int(match[1])
            break
        if item.get('outline') in [str(i) for i in range(9)]:
            level = int(item['outline']) + 1
            break
        current = item.get('base')
    direct = p.find('w:pPr/w:outlineLvl', NS)
    if direct is not None and direct.get(W + 'val') in [str(i) for i in range(9)]:
        level = int(direct.get(W + 'val')) + 1
    return name, level


def numbered_level(text):
    match = re.match(r'^\s*(\d+(?:\.\d+)*)(?:[.)]|\s)\s*([A-Z][^\n]{1,100})$', text)
    return min(match[1].count('.') + 1, 9) if match and not text.rstrip().endswith(('.', '!', '?')) else None


def text_token(node):
    if node.tag == W + 't':
        return node.text or ''
    if node.tag == W + 'tab':
        return '\t'
    if node.tag in (W + 'br', W + 'cr'):
        return '\n'
    if node.tag == W + 'noBreakHyphen':
        return '\u2011'
    if node.tag == W + 'softHyphen':
        return '\u00ad'
    return ''
