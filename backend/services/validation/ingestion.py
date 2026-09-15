"""Stream to temporary storage; publish original, table and validation together."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from .technical import validate_file, ValidationFailure


async def ingest_upload(file, filename, file_type, roots, json_parser):
    storage, uploads, frames = roots
    limit = max(1, int(os.getenv('MAX_FILE_SIZE_MB', '50'))) * 1024 * 1024
    temporary = storage / 'temporary'
    temporary.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=temporary) as directory:
            stage = Path(directory)
            source = stage / filename
            size = 0
            with source.open('wb') as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ValidationFailure('FILE_TOO_LARGE', f'Le fichier dépasse la limite de {limit // 1024 // 1024} Mo.', 'Réduisez la taille du fichier ou séparez-le en plusieurs tableaux.')
                    output.write(chunk)
            report, frame = await asyncio.to_thread(validate_file, source, filename, json_parser)
            stem = Path(filename).stem
            staged_pickle, staged_csv, staged_report = stage / 'frame.pkl', stage / 'frame.csv', stage / 'validation.json'
            await asyncio.to_thread(frame.to_pickle, staged_pickle)
            await asyncio.to_thread(frame.to_csv, staged_csv, index=False, encoding='utf-8')
            staged_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            targets = [(source, uploads / file_type / filename),
                (staged_pickle, frames / file_type / f'{stem}_dataframe.pkl'),
                (staged_csv, frames / file_type / f'{stem}_dataframe.csv'),
                (staged_report, storage / 'validations' / file_type / f'{stem}_validation.json')]
            published = []
            try:
                for staged, target in targets:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Exclusive publication prevents overwrites, even across concurrent uploads.
                    with target.open('xb') as output:
                        published.append(target)
                        with staged.open('rb') as source_stream:
                            import shutil
                            shutil.copyfileobj(source_stream, output)
            except BaseException:
                for target in reversed(published):
                    target.unlink(missing_ok=True)
                raise
            return report, frame
    finally:
        await file.close()
