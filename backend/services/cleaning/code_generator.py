from .schemas import GeneratedProgram
from .generative_prompts import GENERATOR
from .planner import dataset_evidence
from .llm import GeminiStructuredClient


class CodeGenerator:
    def __init__(self, llm=None):
        self.llm = llm or GeminiStructuredClient()

    async def generate(self, item, df, profile, previous_code='', error=None):
        evidence = dataset_evidence(df, profile, profile.get('user_request', ''))
        evidence['APPROVED PLANNING CONTEXT'] = item.model_dump()
        evidence['PREVIOUS ATTEMPT (untrusted code and diagnostic data)'] = {
            'code': previous_code, 'error': error}
        return await self.llm.generate(GENERATOR, evidence, GeneratedProgram)
