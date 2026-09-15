"""Merge only findings with the same measured meaning, preserving review history."""
import copy
import re


def normalize_finding(raw):
    issue = dict(raw)
    kind = issue.get('type', 'issue')
    evidence = str(issue.get('evidence', '')).lower()
    if kind == 'high_missingness':
        issue['type'] = 'missing_values'
    if kind == 'suspicious_values' and re.search(r'\bdates?\b', evidence) and re.search(r'\bfutur', evidence):
        if not re.search(r'past|epoch|1970|invalid|unparseable|passé|ancien', evidence):
            issue['type'] = 'future_dates'
    if kind == 'duplicate_records' and re.search(r'(?:full|exact|identical)[ -](?:duplicate[ -])?rows?|rows? duplicates?|doublons? (?:de lignes|exacts)', evidence):
        issue['scope'], issue['column'] = 'dataset', None
    return issue


def finding_key(issue):
    issue = normalize_finding(issue)
    return issue.get('scope', 'column'), issue.get('column'), issue.get('type')


def merge_findings(report, fresh):
    groups, aliases = {}, dict(report.get('issue_aliases', {}))
    accepted_ids = {i['id'] for i in report.get('accepted_issues', [])}
    for raw in [*report.get('accepted_issues', []), *report.get('remaining_issues', []), *fresh]:
        issue = normalize_finding(raw)
        key = finding_key(issue)
        if key not in groups:
            groups[key] = issue
        else:
            primary = groups[key]
            if issue['id'] != primary['id']:
                aliases[issue['id']] = primary['id']
            if {'low':0, 'medium':1, 'high':2}.get(issue.get('severity'), 1) > {'low':0, 'medium':1, 'high':2}.get(primary.get('severity'), 1):
                primary['severity'] = issue['severity']
    def resolve(value):
        seen = set()
        while value in aliases and value not in seen:
            seen.add(value)
            value = aliases[value]
        return value
    aliases = {old: resolve(new) for old, new in aliases.items() if old != resolve(new)}
    report['issue_aliases'] = aliases
    report['resolved_issue_ids'] = list(dict.fromkeys(aliases.get(i, i) for i in report.get('resolved_issue_ids', [])))
    for field in ['issue_conversations', 'issue_proposals']:
        combined = {}
        for old, entries in report.get(field, {}).items():
            current = aliases.get(old, old)
            combined.setdefault(current, []).extend(copy.deepcopy(entries))
        report[field] = combined
    for issue_id, proposals in report['issue_proposals'].items():
        for proposal in proposals:
            if 'problem_id' in proposal:
                proposal['problem_id'] = issue_id
            if proposal.get('plan_item'):
                proposal['plan_item']['problem_id'] = issue_id
    for item in (report.get('plan') or {}).get('items', []):
        item['problem_id'] = aliases.get(item['problem_id'], item['problem_id'])
    accepted = {aliases.get(i, i) for i in accepted_ids}
    return {i['id']: i for i in groups.values()}, accepted
