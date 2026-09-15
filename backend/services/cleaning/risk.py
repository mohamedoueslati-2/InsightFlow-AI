"""Impact-driven risk; model confidence cannot reduce a measured risk."""
from .schemas import RiskAssessment


def assess_risk(validation, item):
    reasons = []
    score = 40
    if validation.representation_only or validation.exact_duplicate_removal:
        score = 10
        reasons.append('Verified representation preservation or exact duplicate removal')
    if validation.columns_removed:
        score = max(score, 90)
        reasons.append('Columns removed')
    deleted_ratio = (validation.rows_before-validation.rows_after)/max(1, validation.rows_before)
    if deleted_ratio > .05 and not validation.exact_duplicate_removal:
        score = max(score, 90)
        reasons.append('More than 5% of rows deleted')
    if validation.changed_ratio > .2 and not validation.representation_only and not validation.exact_duplicate_removal:
        score = max(score, 80)
        reasons.append('More than 20% of cells modified')
    if validation.new_missing_values:
        score = max(score, 80)
        reasons.append('New missing values introduced')
    filled = sum(validation.missing_before.values())-sum(validation.missing_after.values())
    if filled > max(10, validation.rows_before*.05):
        score = max(score, 80)
        reasons.append('Large-scale imputation')
    for column, values in validation.distribution_changes.items():
        before, after = values['before'], values['after']
        if isinstance(before.get('mean'), (int, float)) and isinstance(after.get('mean'), (int, float)):
            scale = max(abs(before['mean']), abs(before.get('std') or 0), 1e-9)
            if abs(after['mean']-before['mean'])/scale > .2:
                score = max(score, 85)
                reasons.append(f'Large numeric distribution change: {column}')
    if validation.date_ranges and not validation.representation_only:
        score = max(score, 80)
        reasons.append('Date values changed')
    if item.risk_hint == 'high':
        score = max(score, 80)
        reasons.append('Planner reports semantic ambiguity or destructive impact')
    level = 'high' if score >= 70 else 'medium' if score >= 30 else 'low'
    return RiskAssessment(level=level, score=score, reasons=reasons or ['Semantic transformation requires review'],
                          requires_human_review=level != 'low' or item.requires_human_review)
