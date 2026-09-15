"""Conservative interpretation of explicit data edits, shared by both engines."""

import json
import math
import re
from typing import Any, Optional, Tuple
import unicodedata


def is_keep_decision(message: str) -> bool:
    """Explicit closure only, never a question or a quoted model recommendation."""
    text = "".join(c for c in unicodedata.normalize("NFD", message.lower()) if not unicodedata.combining(c))
    text = " ".join(text.strip().rstrip(".!").split())
    return text in {
        "conserver les valeurs inchangees", "conserve les valeurs inchangees",
        "je veux conserver les valeurs inchangees", "conserver en l'etat",
        "conserver en l’etat", "garde tel quel", "laisser tel quel",
        "conserver et cloturer", "accepter en l'etat", "accepter en l’etat",
        "keep unchanged", "keep as is",
    }


def select_saved_proposal(message, proposals):
    """Resolve explicit choices only; questions, negations and alternatives never commit."""
    text = ''.join(c for c in unicodedata.normalize('NFD', message.lower()) if not unicodedata.combining(c))
    text = ' '.join(text.strip().rstrip('.!').split())
    choice = re.fullmatch(r'(?:(?:applique|appliquer|execute|executer)(?: l[’\x27]option| option)?\s+)?(\d+)', text)
    if choice:
        index = int(choice.group(1)) - 1
        return proposals[index] if 0 <= index < len(proposals) else None
    duplicates = re.fullmatch(r'(?:go |vas-y )?(?:supprime|supprimer) les (?:(\d+) )?(?:lignes dupliquees|doublons(?: exacts)?)', text)
    if duplicates:
        matches = [p for p in proposals if p.get('validation', {}).get('exact_duplicate_removal')
                   and (duplicates.group(1) is None or
                        p['validation']['rows_before'] - p['validation']['rows_after'] == int(duplicates.group(1)))]
        return matches[0] if len(matches) == 1 else None
    deletion = re.fullmatch(r'(?:go |vas-y )?(?:supprime|supprimer)(?: les| les lignes| les lignes concernees| (?:les|lzs) valeurs? manquantes?| les lignes (?:avec|contenant des) (?:valeurs? )?manquantes?)', text)
    if deletion:
        matches = [p for p in proposals if p.get('validation', {}).get('rows_after', 0) < p.get('validation', {}).get('rows_before', 0)
                   and not p.get('validation', {}).get('columns_removed')]
        return matches[0] if len(matches) == 1 else None
    return None


def parse_edit(message: str) -> Tuple[Optional[str], Any]:
    text = message.strip()
    # A whole-message command is required: negations and questions never match.
    if re.fullmatch(
        r"(?:donc )?(?:supprime|supprimer|delete|drop)"
        r"(?: (?:le|les|les lignes concernées|lignes concernées|les lignes avec valeurs manquantes|"
        r"lignes avec valeurs manquantes|the rows with missing values|rows with missing values))?[.!]?",
        text, re.IGNORECASE,
    ):
        return "drop", None
    match = re.fullmatch(r'(?:remplir avec|remplace les valeurs manquantes par|utilise|fill with)\s+(.+)', text, re.IGNORECASE)
    if match:
        raw = match.group(1).strip()
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
        if isinstance(value, float) and not math.isfinite(value):
            return None, None
        if isinstance(value, (str, int, float, bool)) and value != "":
            return "fill", value
    # Retain the existing shortcut for a single explicit email address.
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
        return "fill", text
    return None, None
