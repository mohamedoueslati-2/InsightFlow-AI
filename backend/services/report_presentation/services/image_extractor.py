import hashlib
import io
import posixpath
import re
from PIL import Image
from ..schemas.asset import Asset
from .relationship_service import xml

MIME = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'svg': 'image/svg+xml', 'emf': 'image/emf', 'wmf': 'image/wmf', 'gif': 'image/gif', 'tif': 'image/tiff', 'tiff': 'image/tiff', 'bmp': 'image/bmp', 'webp': 'image/webp'}


def dimensions(data, fmt):
    try:
        if fmt == 'svg':
            root = xml(data)
            def length(value):
                m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(px|in|cm|mm|pt|pc)?\s*', value or '')
                return float(m[1]) * {'px': 1, 'in': 96, 'cm': 96 / 2.54, 'mm': 96 / 25.4, 'pt': 96 / 72, 'pc': 16}.get(m[2], 1) if m else None
            return length(root.get('width')), length(root.get('height'))
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None, None


def extract_images(archive, assets_dir, job_id, relationships, warnings):
    assets = []
    for part in sorted(n for n in archive.namelist() if n.startswith('word/media/') and not n.endswith('/')):
        data = archive.read(part)
        fmt = posixpath.splitext(part)[1].lstrip('.').lower() or 'bin'
        if not re.fullmatch('[a-z0-9]{1,10}', fmt):
            fmt = 'bin'
        index = len(assets) + 1
        filename = f'image_{index:03}.{fmt}'
        (assets_dir / filename).write_bytes(data)
        width, height = dimensions(data, fmt)
        rids = [rid for rid, rel in relationships.items() if rel['target'] == part]
        preview = fmt in {'png', 'jpeg', 'jpg', 'svg', 'gif', 'webp', 'bmp'}
        assets.append(Asset(id=f'image-{index:03}', filename=filename, originalFilename=posixpath.basename(part), format=fmt, mimeType=MIME.get(fmt, 'application/octet-stream'), widthPx=width, heightPx=height, relationshipId=rids[0] if rids else None, relationshipIds=rids, sourcePart=part, url=f'/api/jobs/{job_id}/assets/{filename}', sha256=hashlib.sha256(data).hexdigest(), previewSupported=preview))
        if not preview:
            warnings.append(f'{part}: original preserved; browser preview is unavailable.')
    return assets
