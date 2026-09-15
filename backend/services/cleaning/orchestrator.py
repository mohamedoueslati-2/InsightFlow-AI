"""Plan → generate → guard → isolate → measure → risk → commit or preview."""
import os
import uuid
import hashlib
from pydantic import ValidationError
from .schemas import CleaningPlan, CleaningPlanItem, CleaningProposal
from .planner import CleaningPlanner
from .code_generator import CodeGenerator
from .code_policy import validate_code, CodePolicyError
from .executors import configured_executor
from .delta import validate_transformation
from .risk import assess_risk
from .llm import LLMUnavailable
from .proposals import fingerprint
from .reporting import issue_from_problem, measure_issue


class CleaningOrchestrator:
    def __init__(self, planner=None, generator=None, executor=None, approval_mode=None):
        self.planner = planner or CleaningPlanner()
        self.generator = generator or CodeGenerator()
        self.executor = executor or configured_executor()
        self.mode = approval_mode or os.getenv('CLEANING_APPROVAL_MODE', 'auto_safe')
        if self.mode not in {'auto_safe', 'review_all', 'full_auto'}:
            raise ValueError('Invalid approval mode')
        self.attempts = max(1, min(3, int(os.getenv('CLEANING_MAX_CODE_ATTEMPTS', '3'))))
        self.timeout = max(1, int(os.getenv('CLEANING_CODE_TIMEOUT_SECONDS', '30')))

    async def preview_item(self, df, profile, item, report):
        if item.intent == 'recommend':
            item.status = 'awaiting_decision'
            return None, None, None
        code, error = '', None
        issue = next((i for i in profile.get('problems', []) if i['id'] == item.problem_id), {})
        for attempt in range(1, self.attempts+1):
            audit = {'plan_item_id': item.id, 'attempt': attempt, 'stage': 'generating'}
            report.setdefault('code_attempts', []).append(audit)
            try:
                generated = await self.generator.generate(item, df, profile, code, error)
                code = generated.code
                audit['code_hash'] = hashlib.sha256(code.encode('utf-8')).hexdigest()
                report.setdefault('generated_programs', []).append({'plan_item_id': item.id,
                    'attempt': attempt, 'code': code, 'code_hash': audit['code_hash']})
                validate_code(code)
                audit['stage'] = 'safety_passed'
                execution = await self.executor.execute(code, df.copy(deep=True), self.timeout, uuid.uuid4().hex)
                audit['execution'] = execution.audit()
                if execution.status != 'success':
                    audit['stage'] = 'sandbox_error'
                    error = execution.exception
                    audit['error'] = error
                    if execution.status == 'unavailable':
                        report.setdefault('sandbox', {})['error'] = error
                        break
                    continue
                candidate = execution.candidate_dataframe
                validation = validate_transformation(df, candidate, item,
                    measure_issue(df, issue), measure_issue(candidate, issue))
                audit['validation'] = validation.model_dump()
                if not validation.passed:
                    audit['stage'] = 'validation_failed'
                    error = validation.model_dump()
                    continue
                audit['stage'] = 'validation_passed'
                risk = assess_risk(validation, item)
                proposal = CleaningProposal(id=uuid.uuid4().hex, plan_item_id=item.id,
                    problem_id=item.problem_id, title=item.strategy, explanation=generated.explanation or item.expected_effect,
                    generated_code=code, code_hash=audit['code_hash'], risk=risk, validation=validation,
                    rows_affected=validation.changed_rows, rows_before=len(df), rows_after=len(candidate),
                    changed_cells=validation.changed_cells, samples=validation.samples,
                    fingerprint=fingerprint(df), candidate_fingerprint=fingerprint(candidate),
                    plan_item=item, attempt_count=attempt)
                return proposal.model_dump(), candidate, execution
            except (CodePolicyError, ValidationError, ValueError, LLMUnavailable) as failure:
                audit['stage'] = 'failed'
                # Never include provider credentials or raw SDK request objects.
                error = str(failure)[:4000]
                audit['error'] = error
                if isinstance(failure, LLMUnavailable):
                    break
        item.status = 'failed'
        return None, None, None

    def commit(self, report, before, candidate, proposal, execution, resolved_by='agent'):
        item = proposal['plan_item']
        report.setdefault('actions', []).append({'problem': item['problem_id'],
            'column': item['columns'][0] if len(item['columns']) == 1 else None,
            'action': 'generated_pandas', 'reason': proposal['explanation'],
            'rows_affected': proposal['rows_affected'], 'resolved_by': resolved_by,
            'plan_item_id': item['id'], 'generated_code': proposal['generated_code'],
            'code_hash': proposal['code_hash'], 'risk_level': proposal['risk']['level'],
            'attempt_count': proposal['attempt_count'], 'execution': execution.audit(),
            'validation': proposal['validation']})
        proposal['status'] = 'applied'
        common = before.index.intersection(candidate.index)
        columns = before.columns.intersection(candidate.columns)
        unequal = ~(before.loc[common, columns].eq(candidate.loc[common, columns]) |
                    (before.loc[common, columns].isna() & candidate.loc[common, columns].isna())).fillna(False)
        ids = set(map(str, common[unequal.any(axis=1)])) | set(map(str, before.index.difference(candidate.index)))
        if set(before.columns) != set(candidate.columns):
            ids.update(map(str, before.index))
        report['affected_row_ids'] = sorted(set(report.get('affected_row_ids', [])) | ids)
        for plan in (report.get('plan') or {}).get('items', []):
            if plan['id'] == item['id']:
                plan['status'] = 'applied'

    async def run(self, df, profiling_report):
        from .reporting import reconcile_report
        frame = df.copy(deep=True)
        profile = dict(profiling_report, problems=[issue_from_problem(p) for p in profiling_report.get('problems', [])])
        report = {'run_id': uuid.uuid4().hex, 'approval_mode': self.mode,
            'quality_before': profiling_report.get('quality_summary', {}).get('quality_score', 0) or 0,
            'actions': [], 'steps': [], 'remaining_issues': profile['problems'], 'accepted_issues': [],
            'issue_conversations': {}, 'issue_proposals': {}, 'code_attempts': [], 'generated_programs': [],
            'sandbox': {'backend': type(self.executor).__name__}, 'validation': {'rows_before': len(df), 'quality_columns': list(df.columns)},
            'affected_row_ids': []}
        report = reconcile_report(report, frame)
        report['quality_before'] = report['quality_after']
        profile['problems'] = report['remaining_issues']
        try:
            plan = await self.planner.create_plan(frame, profile)
        except (LLMUnavailable, ValueError) as error:
            plan = CleaningPlan(plan_id=uuid.uuid4().hex, dataset_understanding='Plan indisponible', reply=str(error))
            report['sandbox']['error'] = str(error)
        report['plan'] = plan.model_dump()
        for item in plan.items:
            if report['sandbox'].get('error'):
                item.status = 'failed'
                continue
            proposal, candidate, execution = await self.preview_item(frame, profile, item, report)
            if proposal is None:
                continue
            auto = self.mode == 'full_auto' or (self.mode == 'auto_safe' and not proposal['risk']['requires_human_review'])
            if auto:
                self.commit(report, frame, candidate, proposal, execution)
                frame = candidate
                item.status = 'applied'
            else:
                report['issue_proposals'].setdefault(item.problem_id, []).append(proposal)
                item.status = 'waiting_review'
        report['plan'] = plan.model_dump()
        report['risk_summary'] = {level: sum(a.get('risk_level') == level for a in report['actions']) +
            sum(p['risk']['level'] == level for group in report['issue_proposals'].values() for p in group)
            for level in ('low', 'medium', 'high')}
        return reconcile_report(report, frame), frame

    async def apply_saved(self, df, report, proposal):
        if proposal['fingerprint'] != fingerprint(df):
            proposal['status'] = 'stale'
            return df, 'Les données ont changé. Demandez un nouvel aperçu.'
        if proposal['status'] != 'ready':
            return df, 'Cette proposition ne peut plus être appliquée.'
        code = proposal['generated_code']
        if validate_code(code) != proposal['code_hash']:
            proposal['status'] = 'failed'
            return df, 'Le code enregistré ne correspond pas à son empreinte.'
        execution = await self.executor.execute(code, df.copy(deep=True), self.timeout, uuid.uuid4().hex)
        if execution.status != 'success':
            proposal['status'] = 'failed'
            return df, 'Échec du sandbox : ' + execution.exception
        candidate = execution.candidate_dataframe
        issue = next((i for i in report['remaining_issues'] if i['id'] == proposal['problem_id']), {})
        validation = validate_transformation(df, candidate, CleaningPlanItem.model_validate(proposal['plan_item']),
            measure_issue(df, issue), measure_issue(candidate, issue))
        if not validation.passed or fingerprint(candidate) != proposal['candidate_fingerprint']:
            proposal['status'] = 'failed'
            return df, 'Résultat différent de l’aperçu ou validation refusée. Aucun changement.'
        proposal['validation'] = validation.model_dump()
        self.commit(report, df, candidate, proposal, execution, 'user')
        return candidate, f'Modification validée et appliquée : {validation.changed_rows} ligne(s).'
