import io
import zipfile
from pathlib import PurePosixPath
from ..core.config import settings
from ..core.exceptions import InvalidDocument
from ..schemas.extraction import Extraction, Report
from .relationship_service import xml, NS, read_relationships
from .text_extractor import styles_from_archive
from .image_extractor import extract_images
from .document_parser import parse_document, flatten
from .section_builder import report_title, build_sections
from .presentation_builder import markdown


def extract(data, filename, job_id, assets_dir):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > settings.max_zip_entries or sum(i.file_size for i in entries) > settings.max_uncompressed_size_mb * 1024 * 1024:
                raise InvalidDocument('DOCX archive exceeds safe extraction limits.')
            names = [i.filename for i in entries]
            if len(set(names)) != len(names):
                raise InvalidDocument('The uploaded file is not a valid DOCX document.')
            for name in names:
                if '\\' in name or ':' in name or name.startswith('/') or '..' in PurePosixPath(name).parts:
                    raise InvalidDocument('Invalid Word document structure.')
            if 'word/document.xml' not in names:
                raise InvalidDocument('Invalid Word document structure.')
            if '[Content_Types].xml' not in names:
                raise InvalidDocument('The uploaded file is not a valid DOCX document.')
            types = xml(archive.read('[Content_Types].xml'))
            if not any(n.get('PartName') == '/word/document.xml' and n.get('ContentType') == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' for n in types):
                raise InvalidDocument('The uploaded file is not a valid DOCX document.')
            root = xml(archive.read('word/document.xml'))
            if root.tag != '{' + NS['w'] + '}document' or root.find('w:body', NS) is None:
                raise InvalidDocument('Invalid Word document structure.')
            warnings = []
            relationships = read_relationships(archive)
            assets = extract_images(archive, assets_dir, job_id, relationships, warnings)
            blocks = parse_document(root, styles_from_archive(archive), relationships, assets, warnings)
            title = report_title(blocks, filename)
            sections = build_sections(blocks, assets, title, warnings)
            plain = '\n\n'.join(b.text for b in flatten(blocks) if b.text)
            report = Report(title=title, plainText=plain, markdown=markdown(blocks, assets), sections=sections)
            return Extraction(jobId=job_id, report=report, assets=assets, blocks=blocks, warnings=list(dict.fromkeys(warnings)))
    except (zipfile.BadZipFile, RuntimeError, EOFError, KeyError, NotImplementedError) as exc:
        raise InvalidDocument('The uploaded file is not a valid DOCX document.') from exc
