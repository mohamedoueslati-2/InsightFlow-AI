"""Isolated execution backends. Never fall back to local generated-code execution."""


def configured_executor():
    import os
    backend = os.getenv('CLEANING_SANDBOX_BACKEND', 'docker')
    if backend == 'docker':
        from .docker_executor import DockerSandboxExecutor
        return DockerSandboxExecutor()
    if backend == 'agent_runtime':
        from .agent_runtime_executor import AgentRuntimeSandboxExecutor
        return AgentRuntimeSandboxExecutor()
    raise ValueError(f'Unknown sandbox backend: {backend}')
