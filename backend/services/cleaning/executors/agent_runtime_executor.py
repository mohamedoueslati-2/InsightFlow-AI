"""Optional managed Google sandbox adapter; no local execution fallback."""
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time
from .base import SandboxExecutor, ExecutionResult
from .transport import encode_frame, decode_frame
from ..code_policy import validate_code

logger = logging.getLogger(__name__)


class AgentRuntimeSandboxExecutor(SandboxExecutor):
    def __init__(self, client=None):
        self.client = client

    async def execute(self, code, dataframe, timeout=30, execution_id=''):
        code_hash = validate_code(code)
        return await asyncio.to_thread(self._execute, code, dataframe, timeout, code_hash)

    def _execute(self, code, dataframe, timeout, code_hash):
        started = time.monotonic()
        result = ExecutionResult('error', code_hash=code_hash)
        resource = os.getenv('GCP_AGENT_ENGINE_RESOURCE_NAME', '')
        template = os.getenv('GCP_SANDBOX_RESOURCE_NAME', '')
        if not resource and template:
            resource = template.split('/sandboxEnvironments/')[0]
        match = re.fullmatch(r'projects/([^/]+)/locations/([^/]+)/reasoningEngines/([^/]+)', resource)
        if not match:
            result.exception = 'Configure GCP_AGENT_ENGINE_RESOURCE_NAME with an existing Agent Engine.'
            return result
        sandbox = None
        try:
            if self.client is None:
                import vertexai
                self.client = vertexai.Client(project=match[1], location=match[2])
            api = self.client.agent_engines.sandboxes
            # Always create a fresh environment: never reuse another dataset's state.
            operation = api.create(name=resource, spec={'code_execution_environment': {}},
                                   config={'display_name': 'data-cleaning', 'ttl': '300s'})
            sandbox = operation.response.name
            folder = Path(__file__).parent
            worker = (folder / 'sandbox_worker.py').read_text(encoding='utf-8').replace('/input/', '')
            wrapper = ('import signal\n'
                       'def deadline(signum, frame):\n    raise TimeoutError("Cleaning execution deadline")\n'
                       f'signal.signal(signal.SIGALRM, deadline)\nsignal.alarm({int(timeout)})\n'
                       'import sandbox_worker\nsandbox_worker.main()\nsignal.alarm(0)\n')
            response = api.execute_code(name=sandbox, input_data={'code': wrapper, 'files': [
                {'name': 'data.json', 'content': encode_frame(dataframe).encode()},
                {'name': 'program.py', 'content': code.encode()},
                {'name': 'sandbox_worker.py', 'content': worker.encode()},
                {'name': 'transport.py', 'content': (folder/'transport.py').read_bytes()}]})
            for output in response.outputs:
                if output.mime_type == 'application/json':
                    body = json.loads(output.data.decode())
                    result.stderr = body.get('msg_err', '')[-4000:]
                    if body.get('msg_out'):
                        envelope = json.loads(body['msg_out'])
                        if envelope.get('status') == 'ok':
                            result.candidate_dataframe = decode_frame(envelope['data'])
                            result.status = 'success'
                        else:
                            result.exception = str(envelope.get('error', 'Missing output'))[:4000]
            if result.status != 'success' and not result.exception:
                result.exception = result.stderr or 'Managed sandbox did not return a valid DataFrame'
        except Exception as error:
            # Explicit remote service boundary: no credentials or raw request in report.
            logger.warning('Managed cleaning sandbox failed (%s)', type(error).__name__)
            result.exception = f'Managed sandbox unavailable ({type(error).__name__}). Check GCP SDK, ADC and resource permissions.'
        finally:
            if sandbox:
                try:
                    self.client.agent_engines.sandboxes.delete(name=sandbox)
                except Exception as error:
                    logger.error('Managed sandbox cleanup failed (%s); TTL will expire it', type(error).__name__)
                    result.status = 'error'
                    result.exception = 'Sandbox cleanup failed; no commit permitted.'
            result.elapsed_ms = int((time.monotonic()-started)*1000)
        return result
