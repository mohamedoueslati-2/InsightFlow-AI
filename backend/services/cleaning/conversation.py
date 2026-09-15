"""Issue discussion produces sandbox previews; only explicit decisions can commit."""
import copy
import uuid
from .chat_intent import is_keep_decision, select_saved_proposal
from .orchestrator import CleaningOrchestrator
from .llm import LLMUnavailable
from .reporting import issue_from_problem, reconcile_report
from .choices import wants_comparison, describe_choices
from .schemas import CleaningPlanItem
from .proposals import fingerprint


class IssueResolutionAgent:
    def __init__(self, df, report, issue_id, user_message, model_name=None,
                 proposal_id=None, decision=None, orchestrator=None):
        self.df = df.copy(deep=True)
        self.report = reconcile_report(report, self.df)
        self.issue_id = self.report.get('issue_aliases', {}).get(issue_id, issue_id)
        self.user_message = user_message
        self.proposal_id, self.decision = proposal_id, decision
        self.orchestrator = orchestrator or CleaningOrchestrator(approval_mode='review_all')

    async def run(self):
        report, df = self.report, self.df
        report.setdefault('run_id', uuid.uuid4().hex)
        report.setdefault('remaining_issues', [])
        report.setdefault('actions', [])
        report.setdefault('accepted_issues', [])
        report.setdefault('issue_proposals', {})
        report.setdefault('validation', {}).setdefault('quality_columns', list(df.columns))
        thread = report.setdefault('issue_conversations', {}).setdefault(self.issue_id, [])
        decision = self.decision or ('keep' if is_keep_decision(self.user_message) else None)
        source = 'accepted_issues' if decision == 'reopen' else 'remaining_issues'
        issue = next((i for i in report[source] if i.get('id') == self.issue_id), None)
        if issue is None:
            raise ValueError('Ce problème est clôturé ou introuvable. Rouvrez-le pour continuer.')
        proposals = report['issue_proposals'].setdefault(self.issue_id, [])
        status = 'no_change'
        if decision in {'keep', 'reopen'}:
            target = 'remaining_issues' if decision == 'reopen' else 'accepted_issues'
            report[source] = [i for i in report[source] if i['id'] != self.issue_id]
            report[target].append(issue)
            for proposal in proposals:
                if proposal.get('status') == 'ready':
                    proposal['status'] = 'dismissed'
            reply = 'Valeurs conservées. Discussion clôturée.' if decision == 'keep' else 'Discussion rouverte.'
            status = 'accepted' if decision == 'keep' else 'reopened'
        else:
            ready = [p for p in proposals if p.get('status') == 'ready']
            menu = report.setdefault('issue_choices', {}).get(self.issue_id)
            if not decision and menu and self.user_message.strip().isdigit():
                if menu['fingerprint'] != fingerprint(df):
                    return self._finish(report, df, 'Les données ont changé. Demandez une nouvelle comparaison avant de choisir.', status)
                index = int(self.user_message.strip()) - 1
                if not 0 <= index < len(menu['items']):
                    return self._finish(report, df, f'Choisissez un numéro entre 1 et {len(menu["items"])} ou écrivez « autre ».', status)
                item = CleaningPlanItem.model_validate(menu['items'][index])
                if item.intent == 'recommend':
                    return self._finish(report, df, f'{item.strategy}\n{item.expected_effect}\nAucune modification. Pour accepter ce problème en l’état, choisissez « Conserver et clôturer ».', status)
                if issue.get('column') and (item.scope != 'column' or any(c in df and c != issue['column'] for c in item.columns)):
                    return self._finish(report, df, 'Cette solution dépasse la colonne concernée. Décrivez une règle ciblée.', status)
                item.id = uuid.uuid4().hex
                proposal, _, _ = await self.orchestrator.preview_item(df, {'problems': [issue], 'user_request': item.strategy}, item, report)
                if not proposal:
                    return self._finish(report, df, 'La préparation de cette solution a échoué. Aucune modification ; vous pouvez choisir une autre solution.', status)
                for old in proposals:
                    if old.get('status') == 'ready':
                        old['status'] = 'dismissed'
                proposals.append(proposal)
                report['issue_choices'].pop(self.issue_id, None)
                report.setdefault('plan', {'items': []})['items'].append(dict(item.model_dump(), status='waiting_review'))
                return self._finish(report, df, f'Aperçu prêt : {item.strategy}\n{proposal["rows_before"]} → {proposal["rows_after"]} lignes. Aucune modification enregistrée.\nÉcrivez « oui » pour appliquer, ou demandez une autre solution.', status)
            selected = select_saved_proposal(self.user_message, ready) if not decision else None
            if selected:
                decision, self.proposal_id = 'apply', selected['id']
            elif not decision and self.user_message.strip().isdigit():
                return self._finish(report, df, f'Ce numéro ne correspond à aucune option disponible. Choisissez un numéro parmi les {len(ready)} aperçus affichés, ou décrivez une autre solution.', status)
            if not decision and self.user_message.strip().lower() in {'other', 'autre'}:
                return self._finish(report, df, 'Décrivez votre autre solution : quelles valeurs souhaitez-vous changer et par quelle règle ? Aucune modification pour le moment.', status)
            if not decision and self.user_message.strip().lower() in {'oui', 'appliquer', 'applique'}:
                if len(ready) == 1:
                    decision, self.proposal_id = 'apply', ready[0]['id']
                else:
                    return self._finish(report, df, 'Choisissez une proposition précise à appliquer.', status)
            if decision in {'apply', 'dismiss'}:
                proposal = next((p for p in proposals if p['id'] == self.proposal_id), None)
                if proposal is None:
                    raise ValueError('Proposition introuvable pour ce problème.')
                if decision == 'dismiss':
                    if proposal.get('status') == 'ready':
                        proposal['status'] = 'dismissed'
                    reply = 'Proposition écartée. Aucune modification.'
                elif not proposal.get('generated_code'):
                    proposal['status'] = 'stale'
                    reply = 'Ancien aperçu : demandez un nouvel aperçu avec le moteur de code généré.'
                else:
                    df, reply = await self.orchestrator.apply_saved(df, report, proposal)
                    if proposal['status'] == 'applied':
                        status = 'applied'
            else:
                profile = {'problems': [issue_from_problem(issue)], 'user_request': self.user_message,
                    'comparison_mode': wants_comparison(self.user_message),
                    'available_proposals': [{'id': p['id'], 'title': p.get('title'),
                        'rows_affected': p.get('rows_affected'), 'rows_before': p.get('rows_before'),
                        'rows_after': p.get('rows_after')} for p in ready]}
                try:
                    plan = await self.orchestrator.planner.create_plan(df, profile, self.user_message, thread)
                    if profile['comparison_mode']:
                        report.setdefault('issue_choices', {})[self.issue_id] = {
                            'fingerprint': fingerprint(df), 'items': [item.model_dump() for item in plan.items]}
                        return self._finish(report, df, describe_choices(plan.items) if plan.items else
                            plan.reply or 'Précisez le résultat souhaité pour comparer les solutions.', status)
                    saved_plan = report.get('plan') or {'plan_id': uuid.uuid4().hex, 'dataset_understanding': '', 'items': []}
                    report['plan'] = saved_plan
                    count = 0
                    for item in plan.items:
                        if item.intent != 'recommend' and issue.get('column') and (item.scope != 'column' or any(c in df.columns and c != issue['column'] for c in item.columns)):
                            raise ValueError('Le plan dépasse la colonne de cette conversation.')
                        item.id = uuid.uuid4().hex
                        proposal, _, _ = await self.orchestrator.preview_item(df, profile, item, report)
                        if proposal and any(p.get('status') == 'ready' and p.get('code_hash') == proposal['code_hash']
                                            and p.get('fingerprint') == proposal['fingerprint'] for p in proposals):
                            count += 1
                            continue
                        if proposal:
                            item.status = 'waiting_review'
                        # Chat advice belongs to its conversation, not repeated global plan rows.
                        if item.intent != 'recommend':
                            saved_plan['items'].append(item.model_dump())
                        if proposal:
                            proposals.append(proposal)
                            count += 1
                    reply = plan.reply or ' '.join(item.strategy for item in plan.items if item.intent == 'recommend')
                    selected = select_saved_proposal(self.user_message, [p for p in proposals if p.get('status') == 'ready'])
                    if selected and count:
                        df, reply = await self.orchestrator.apply_saved(df, report, selected)
                        if selected['status'] == 'applied':
                            status = 'applied'
                        return self._finish(report, df, reply, status)
                    if count:
                        available = [p for p in proposals if p.get('status') == 'ready']
                        lines = []
                        for number, option in enumerate(available, 1):
                            v = option.get('validation', {})
                            removed = option['rows_before'] - option['rows_after']
                            impact = (f'{removed} ligne(s) seraient supprimées.' if removed > 0 else
                                'Une colonne de signalement serait ajoutée ; les valeurs initiales resteraient inchangées.' if option.get('plan_item', {}).get('intent') == 'flag' else
                                f'{v.get("changed_cells", 0)} cellule(s) seraient modifiées.')
                            lines.append(f'{number}. {option["title"]}\n{impact} {option["rows_before"]} → {option["rows_after"]} lignes.')
                        reply = 'Voici les options dont l’aperçu a été validé. Aucune modification n’a encore été appliquée.\n\n' + '\n\n'.join(lines)
                        reply += '\n\nÉcrivez le numéro pour appliquer cette option, ou « autre » puis décrivez votre solution.'
                    elif plan.items and all(item.intent == 'recommend' for item in plan.items):
                        reply += ' Recommandation sans modification. Le problème reste ouvert jusqu’à votre décision.'
                    elif plan.items:
                        reply += ' Aucun aperçu validé après les tentatives autorisées. Données conservées.'
                    elif not reply:
                        reply = 'Aucune modification. Précisez la transformation ou demandez des options.'
                except (LLMUnavailable, ValueError) as error:
                    reply = f'Aucune modification. {error}'
        return self._finish(report, df, reply, status)

    def _finish(self, report, df, reply, status):
        report['issue_conversations'][self.issue_id].extend([
            {'role': 'user', 'text': self.user_message},
            {'role': 'agent', 'text': reply.strip(), 'execution_status': status}])
        return reconcile_report(report, df), df, reply.strip()
