"""Remeasure supplied profiling findings without discovering additional problems."""
import copy
import re
import pandas as pd
from .validator import make_issue_id, build_plain_language_summary, compute_report_status
from .proposals import fingerprint
from .findings import merge_findings, normalize_finding


def issue_from_problem(problem):
    issue = dict(problem)
    issue.setdefault('id', make_issue_id(issue.get('scope', 'column'), issue.get('column'), issue.get('type', 'issue')))
    issue.setdefault('reason_not_fixed', 'En attente de transformation validée ou de décision humaine.')
    return issue


def measure_issue(df, issue):
    issue = normalize_finding(issue)
    column, kind = issue.get('column'), issue.get('type', '')
    if kind == 'duplicate_records' and issue.get('scope', 'column') == 'dataset':
        return int(df.duplicated().sum())
    if column not in df:
        return None
    series = df[column]
    if kind in {'missing_values', 'high_missingness'}:
        return int(series.isna().sum())
    if kind == 'future_dates':
        return int((pd.to_datetime(series, errors='coerce', utc=True) > pd.Timestamp.now(tz='UTC')).sum())
    if kind == 'outliers' and 'iqr' in issue.get('evidence', '').lower() and pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        values = pd.to_numeric(series, errors='coerce').dropna()
        q1, q3 = values.quantile(.25), values.quantile(.75)
        return int(((values < q1 - 1.5 * (q3 - q1)) | (values > q3 + 1.5 * (q3 - q1))).sum())
    if kind in {'suspicious_negative_values', 'suspicious_values'} and re.search('negativ|négativ', issue.get('evidence', ''), re.I):
        return int((pd.to_numeric(series, errors='coerce') < 0).sum())
    # An exact quoted invalid placeholder is measurable; never guess a literal.
    if kind in {'inconsistent_formats', 'semantic_invalid_value'}:
        if 'leading/trailing whitespace' in issue.get('evidence', '').lower():
            return int(series.map(lambda x: isinstance(x, str) and x != x.strip()).sum())
        match = re.search(r"(?:occurrences? of|occurrences? de)\s+['\"]([^'\"]+)['\"]", issue.get('evidence', ''), re.I)
        if match:
            return int(series.eq(match.group(1)).sum())
    return None


def reconcile_report(report, df):
    result = copy.deepcopy(report)
    quality_columns = result.get('validation', {}).get('quality_columns', list(df.columns))
    business = df[[c for c in quality_columns if c in df]]
    measured, accepted = merge_findings(result, [])
    closed = set(result.get('resolved_issue_ids', []))
    for issue_id, issue in list(measured.items()):
        count = measure_issue(business, issue)
        if count == 0 or issue_id in closed:
            measured.pop(issue_id, None)
            continue
        if count is not None:
            issue = dict(issue, affected_rows=count, affected_percent=round(100*count/max(1, len(df)), 2))
            # Preserve diagnostic evidence used by future independent measurements.
            label = {'future_dates': 'Dates futures', 'missing_values': 'Valeurs manquantes',
                     'duplicate_records': 'Lignes dupliquées', 'outliers': 'Valeurs hors des bornes IQR'}.get(issue.get('type'), 'Valeurs concernées')
            issue['current_evidence'] = f'{label} : {count} ligne(s) sur {len(df)} ({issue["affected_percent"]} %)'
        measured[issue_id] = issue
    result['remaining_issues'] = [i for key, i in measured.items() if key not in accepted and key not in closed]
    result['accepted_issues'] = [dict(i, review_status='accepted') for key, i in measured.items() if key in accepted]
    # Score computed from current evidence including retained semantic findings.
    from .agent import compute_quality_after
    result['quality_after'] = compute_quality_after(business, list(measured.values()))
    result['post_clean_profile'] = {
        'source': 'remeasured_profiling_findings',
        'problems': result['remaining_issues'] + result['accepted_issues'],
        'dataset_overview': {'rows': len(business), 'columns_count': len(business.columns),
            'total_missing_cells': int(business.isna().sum().sum()),
            'duplicate_rows': int(business.duplicated().sum())}}
    result['validation'] = {**result.get('validation', {}), 'rows_after': len(df),
        'columns_after': len(df.columns), 'missing_cells_after': int(df.isna().sum().sum()),
        'duplicate_rows_after': int(df.duplicated().sum()), 'engine': 'Gemini + isolated generated Pandas'}
    audit_columns = {c for a in result.get('actions', []) for c in a.get('validation', {}).get('columns_added', [])}
    result['validation']['synthetic_cells'] = sum(int(df[c].fillna(False).sum()) for c in audit_columns
        if c in df and c.endswith('_synthetic') and pd.api.types.is_bool_dtype(df[c]))
    actions = result.get('actions', [])
    result['changes_applied'] = len(actions)
    result['rows_affected'] = len(set(result.get('affected_row_ids', []))) if 'affected_row_ids' in result else min(len(df), sum(a.get('rows_affected', 0) for a in actions))
    result['status'] = compute_report_status(actions, result['remaining_issues'])
    result['summary'] = build_plain_language_summary(actions, result['remaining_issues'], result['status'])
    if result['accepted_issues']:
        result['summary'] += f' {len(result["accepted_issues"])} problème(s) accepté(s) en l’état.'
        if not result['remaining_issues']:
            result['status'] = 'reviewed'
    current = fingerprint(df)
    for proposals in result.get('issue_proposals', {}).values():
        for proposal in proposals:
            if proposal.get('status') == 'ready' and proposal.get('fingerprint') != current:
                proposal['status'] = 'stale'
            for item in (result.get('plan') or {}).get('items', []):
                if item['id'] == proposal.get('plan_item_id'):
                    item['status'] = 'waiting_review' if proposal['status'] == 'ready' else proposal['status']
    return result
