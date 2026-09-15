import posixpath
from urllib.parse import unquote, urlsplit
from lxml import etree
from ..core.exceptions import InvalidDocument

NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
}


def xml(data: bytes):
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True))
        if root.getroottree().docinfo.doctype:
            raise InvalidDocument('The uploaded file is not a valid DOCX document.')
        return root
    except etree.XMLSyntaxError as exc:
        raise InvalidDocument('The uploaded file is not a valid DOCX document.') from exc


def resolve_target(source_part: str, target: str) -> str:
    target = unquote(target)
    if '\\' in target or urlsplit(target).scheme or target.startswith('//'):
        raise InvalidDocument('Invalid Word document structure.')
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target)) if not target.startswith('/') else posixpath.normpath(target.lstrip('/'))
    if resolved == '..' or resolved.startswith('../'):
        raise InvalidDocument('Invalid Word document structure.')
    return resolved


def read_relationships(archive, source_part='word/document.xml'):
    path = posixpath.join(posixpath.dirname(source_part), '_rels', posixpath.basename(source_part) + '.rels')
    if path not in archive.namelist():
        return {}
    result = {}
    for item in xml(archive.read(path)):
        if item.get('TargetMode') == 'External':
            continue
        rid, target = item.get('Id'), item.get('Target')
        if rid and target:
            result[rid] = {'target': resolve_target(source_part, target), 'type': item.get('Type', '')}
    return result
