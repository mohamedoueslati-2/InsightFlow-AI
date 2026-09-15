"""Docker process boundary. No project directory or credentials enter the container."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import logging

import pandas as pd

from .base import SandboxExecutor, ExecutionResult
from ..code_policy import validate_code
from .transport import encode_frame, decode_frame

logger = logging.getLogger(__name__)


def run_bounded(command, root, timeout):
    """Bound output on disk while supervising the Docker attach process."""
    with (root/'stdout').open('w+b') as stdout, (root/'stderr').open('w+b') as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        deadline = time.monotonic()+timeout
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                if (root/'stdout').stat().st_size > 64*1024*1024 or (root/'stderr').stat().st_size > 1024*1024:
                    raise ValueError('Sandbox output limit exceeded')
                time.sleep(.05)
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read(64*1024*1024+1)
            if len(output) > 64*1024*1024:
                raise ValueError('Sandbox output limit exceeded')
            return subprocess.CompletedProcess(command, process.returncode, output.decode('utf-8'), stderr.read(1024*1024).decode('utf-8', errors='replace'))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


class DockerSandboxExecutor(SandboxExecutor):
    def __init__(self, image=None):
        self.image = image or os.getenv('CLEANING_SANDBOX_IMAGE', 'cleaning-sandbox:1')
        self.memory = int(os.getenv('CLEANING_SANDBOX_MEMORY_MB', '512'))
        self.cpu = float(os.getenv('CLEANING_SANDBOX_CPU_LIMIT', '1'))

    async def execute(self, code, dataframe, timeout=30, execution_id=''):
        code_hash = validate_code(code)
        return await asyncio.to_thread(self._execute, code, dataframe, timeout, code_hash)

    def _execute(self, code, dataframe, timeout, code_hash):
        started = time.monotonic()
        name = 'cleaning-' + uuid.uuid4().hex
        result = ExecutionResult('error', code_hash=code_hash)
        with tempfile.TemporaryDirectory(prefix='cleaning-sandbox-') as folder:
            root = Path(folder)
            source = root / 'input'
            source.mkdir()
            # JSON table is data-only, not pickle. The wrapper restores declared dtypes.
            (source / 'data.json').write_text(encode_frame(dataframe), encoding='utf-8')
            (source / 'program.py').write_text(code, encoding='utf-8')
            command = ['docker', 'create', '--name', name, '--network', 'none',
                       '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                       '--user', '65534:65534', '--pids-limit', '32',
                       '--memory', f'{self.memory}m', '--memory-swap', f'{self.memory}m',
                       '--cpus', str(self.cpu), '--ulimit', 'nofile=64:64',
                       '--ulimit', 'fsize=67108864:67108864', '--log-driver', 'none',
                       '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                       '--tmpfs', '/input:rw,noexec,nosuid,size=64m,mode=0555',
                       '--tmpfs', '/output:rw,noexec,nosuid,size=64m,mode=1777',
                       self.image]
            try:
                created = subprocess.run(command, capture_output=True, text=True, timeout=20)
                if created.returncode:
                    result.status = 'unavailable'
                    result.exception = 'Docker sandbox unavailable. Build cleaning-sandbox:1 and check Docker Desktop. ' + created.stderr[-2000:]
                    return result
                # Send the data-only input archive through the Docker API instead of a host bind mount.
                # This works when FastAPI itself runs in Docker: the daemon never needs access to the
                # API container's private /tmp path.
                copied = subprocess.run(['docker', 'cp', f'{source}{os.sep}.', f'{name}:/input'],
                                        capture_output=True, text=True, timeout=20)
                if copied.returncode:
                    result.status = 'unavailable'
                    result.exception = 'Docker sandbox input transfer failed. ' + copied.stderr[-2000:]
                    return result
                completed = run_bounded(['docker', 'start', '-a', name], root, timeout)
                result.stderr = completed.stderr[-4000:]
                if completed.returncode:
                    result.exception = 'Sandbox runtime failure: ' + result.stderr
                    return result
                # Read a bounded data-only artifact over stdout, never extract container archives.
                # docker cp to stdout produces an archive; we do not use or deserialize it.
                inspect = subprocess.run(['docker', 'inspect', '--format', '{{.State.ExitCode}}', name],
                                         capture_output=True, text=True, timeout=10)
                if inspect.stdout.strip() != '0':
                    result.exception = 'Sandbox exited with code ' + inspect.stdout.strip()
                    return result
                payload = completed.stdout
                if len(payload.encode()) > 64 * 1024 * 1024:
                    result.exception = 'Output exceeds 64 MB'
                    return result
                from io import StringIO
                envelope = json.loads(payload)
                if envelope.get('status') != 'ok':
                    result.exception = str(envelope.get('error', 'Missing DataFrame output'))[:4000]
                    return result
                result.candidate_dataframe = decode_frame(envelope['data'])
                result.status = 'success'
            except subprocess.TimeoutExpired:
                result.status = 'timeout'
                result.exception = f'Sandbox exceeded {timeout} seconds'
            except (OSError, ValueError, KeyError, TypeError) as error:
                result.exception = f'Sandbox transport error: {type(error).__name__}: {error}'
            finally:
                try:
                    cleanup = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
                    if cleanup.returncode and result.status == 'success':
                        result.status = 'error'
                        result.exception = 'Sandbox cleanup failed; commit refused'
                except (OSError, subprocess.TimeoutExpired) as error:
                    logger.error('Docker cleanup failed: %s', type(error).__name__)
                    result.status = 'error'
                    result.exception = 'Sandbox cleanup could not complete; commit refused'
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result
