"""Persist the numbered discussion menu separately from executed previews."""
import re
import unicodedata


def wants_comparison(message):
    text = ''.join(c for c in unicodedata.normalize('NFD', message.lower()) if not unicodedata.combining(c))
    return bool(re.search(r'compar|solutions|options|alternatives', text)) and not bool(re.search(r'ne .*pas|sans compar', text))


def describe_choices(items):
    lines = ['Solutions à examiner — aucun code exécuté, aucune donnée modifiée.']
    for number, item in enumerate(items, 1):
        lines.append(f'{number}. {item.strategy}\nEffet : {item.expected_effect}\nAvantage : {item.benefit or item.diagnosis}\nLimite : {item.tradeoff or "À vérifier selon le contexte métier."}')
    ranked = sorted([(i, x) for i, x in enumerate(items, 1) if x.recommendation_rank], key=lambda pair: pair[1].recommendation_rank)
    for number, item in ranked[:2]:
        lines.append(f'{"Mon premier choix" if item.recommendation_rank == 1 else "Mon deuxième choix"} : option {number}. {item.recommendation_reason}')
    lines.append('Écrivez le numéro pour préparer uniquement cette solution, ou « autre » et votre règle. Un aperçu devra ensuite être appliqué pour modifier les données.')
    return '\n\n'.join(lines)
