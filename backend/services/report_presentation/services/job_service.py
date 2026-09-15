import json
import shutil
from uuid import uuid4, UUID
from ..core.config import settings
from .docx_service import extract
from .presentation_builder import presentation_input


def create_job(data, filename):
    job_id = str(uuid4())
    directory = settings.storage_dir / job_id
    (directory / 'assets').mkdir(parents=True)
    try:
        (directory / 'source').mkdir()
        (directory / 'source' / 'report.docx').write_bytes(data)
        result = extract(data, filename, job_id, directory / 'assets')
        (directory / 'extraction.json').write_text(result.model_dump_json(indent=2), encoding='utf-8')
        (directory / 'report.md').write_text(result.report.markdown, encoding='utf-8', newline='\n')
        (directory / 'presentation_input.json').write_text(json.dumps(presentation_input(result.report, result.assets), ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    except Exception:
        # Only remove the newly allocated UUID directory under configured storage.
        if directory.resolve().parent == settings.storage_dir.resolve():
            shutil.rmtree(directory)
        raise


def job_directory(job_id):
    try:
        if str(UUID(job_id)) != job_id:
            return None
    except ValueError:
        return None
    path = settings.storage_dir / job_id
    return path if (path / 'extraction.json').is_file() else None
