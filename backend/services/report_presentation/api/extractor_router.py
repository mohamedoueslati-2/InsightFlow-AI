import json
import logging
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from ..core.config import settings
from ..core.exceptions import InvalidDocument
from ..schemas.extraction import Extraction
from ..services.job_service import create_job, job_directory

router = APIRouter(prefix='/api')
logger = logging.getLogger(__name__)


@router.get('/health')
def health():
    return {'status': 'ok'}


@router.post('/extract', response_model=Extraction)
async def upload(file: UploadFile = File(...)):
    try:
        if Path(file.filename or '').suffix.lower() != '.docx':
            raise HTTPException(415, 'Only .docx Word reports are supported.')
        data = bytearray()
        limit = int(settings.max_docx_size_mb * 1024 * 1024)
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, f'DOCX exceeds the {settings.max_docx_size_mb:g} MB upload limit.')
        return await run_in_threadpool(create_job, bytes(data), file.filename)
    except InvalidDocument as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception('Report extraction failed')
        raise HTTPException(500, 'Report extraction failed. Please try another DOCX file.')
    finally:
        await file.close()


def directory(job_id):
    result = job_directory(job_id)
    if result is None:
        raise HTTPException(404, 'Job not found.')
    return result


@router.get('/jobs/{job_id}')
def job(job_id: str):
    return json.loads((directory(job_id) / 'extraction.json').read_text(encoding='utf-8'))


@router.get('/jobs/{job_id}/assets/{filename}')
def asset(job_id: str, filename: str):
    path = directory(job_id)
    extraction = json.loads((path / 'extraction.json').read_text(encoding='utf-8'))
    item = next((a for a in extraction['assets'] if a['filename'] == filename), None)
    if not item or Path(filename).name != filename:
        raise HTTPException(404, 'Asset not found.')
    return FileResponse(path / 'assets' / filename, media_type=item['mimeType'], headers={
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'",
    })


@router.get('/jobs/{job_id}/report')
def report(job_id: str):
    return FileResponse(directory(job_id) / 'report.md', media_type='text/markdown')


@router.get('/jobs/{job_id}/presentation-input')
def presentation(job_id: str):
    return json.loads((directory(job_id) / 'presentation_input.json').read_text(encoding='utf-8'))
