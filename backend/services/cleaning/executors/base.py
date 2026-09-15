from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
import pandas as pd


@dataclass
class ExecutionResult:
    status: str
    stdout: str = ''
    stderr: str = ''
    exception: str = ''
    elapsed_ms: int = 0
    candidate_dataframe: pd.DataFrame | None = None
    code_hash: str = ''

    def audit(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if k != 'candidate_dataframe'}


class SandboxExecutor(ABC):
    @abstractmethod
    async def execute(self, code: str, dataframe: pd.DataFrame, timeout: int,
                      execution_id: str) -> ExecutionResult:
        raise NotImplementedError
