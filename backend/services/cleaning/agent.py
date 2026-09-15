"""Public compatibility façade for the generated-code cleaning architecture."""
from .legacy_agent import (build_deterministic_baseline_cleaning, compute_quality_after,
                           _finalize_cleaning_result, refresh_cleaning_report as _legacy_refresh)
from .orchestrator import CleaningOrchestrator


def refresh_cleaning_report(report, df):
    if report.get('run_id'):
        from .reporting import reconcile_report
        return reconcile_report(report, df)
    return _legacy_refresh(report, df)


class CleaningAgent:
    def __init__(self, df, profiling_report, model_name=None, approval_mode=None, orchestrator=None):
        self.df = df.copy(deep=True)
        self.cleaned_df = self.df.copy(deep=True)
        self.profile = profiling_report
        if orchestrator is not None:
            self.orchestrator = orchestrator
        else:
            from .llm import GeminiStructuredClient
            from .planner import CleaningPlanner
            from .code_generator import CodeGenerator
            llm = GeminiStructuredClient(model_name)
            self.orchestrator = CleaningOrchestrator(CleaningPlanner(llm), CodeGenerator(llm), approval_mode=approval_mode)

    async def run(self):
        report, self.cleaned_df = await self.orchestrator.run(self.df, self.profile)
        return report


from .conversation import IssueResolutionAgent
