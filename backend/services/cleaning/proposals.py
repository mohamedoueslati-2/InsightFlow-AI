"""Executable, versioned previews for conversational cleaning decisions."""

import hashlib
import json
import uuid
from typing import Any

import pandas as pd

from .tools import CleaningToolkit


CATALOG = {
    "synthetic_emails": ("Créer des e-mails fictifs uniques", {"prefix", "domain", "start"}),
    "replace_exact_with_synthetic_emails": ("Remplacer un placeholder par des e-mails fictifs uniques", {"match_value", "prefix", "domain", "start"}),
    "absolute_negative_values": ("Convertir les valeurs négatives en valeurs absolues", set()),
    "fill_constant": ("Remplir avec une valeur choisie", {"value"}),
    "fill_statistic": ("Remplir depuis les données", {"strategy"}),
    "drop_missing_rows": ("Supprimer les lignes incomplètes", set()),
    "trim_whitespace": ("Retirer les espaces superflus", set()),
    "normalize_case": ("Uniformiser la casse", {"target_case"}),
    "canonicalize_categories": ("Regrouper des variantes", {"mapping"}),
    "cast_column_type": ("Convertir le type", {"target_type"}),
    "cap_outliers": ("Plafonner les valeurs extrêmes", {"method"}),
    "flag_temporal_anomaly": ("Marquer les dates à vérifier", set()),
    "flag_domain_implausible": ("Marquer les valeurs atypiques", set()),
    "drop_exact_duplicates": ("Supprimer les doublons exacts", set()),
}


def fingerprint(df):
    digest = hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes())
    digest.update(str(list(zip(df.columns, map(str, df.dtypes)))).encode())
    return digest.hexdigest()


def execute(toolkit: CleaningToolkit, operation: str, column: str | None, parameters: dict):
    json.dumps(parameters, allow_nan=False)
    if operation not in CATALOG or set(parameters) - CATALOG[operation][1]:
        raise ValueError("Opération ou paramètres non pris en charge.")
    if operation == "drop_exact_duplicates":
        return toolkit.drop_exact_duplicates()
    if column not in toolkit.df.columns:
        raise ValueError("Colonne cible introuvable.")
    if operation == "synthetic_emails":
        return toolkit.generate_synthetic_emails(column, **parameters)
    if operation == "replace_exact_with_synthetic_emails":
        return toolkit.replace_exact_with_synthetic_emails(column, **parameters)
    if operation == "absolute_negative_values":
        return toolkit.absolute_negative_values(column)
    if operation == "fill_constant":
        value = parameters.get("value")
        if value is None or isinstance(value, (dict, list)) or (isinstance(value, str) and not value.strip()):
            raise ValueError("Choisissez une valeur simple non vide.")
        return toolkit.manual_fill_missing(column, value)
    if operation == "fill_statistic":
        strategy = parameters.get("strategy", "median")
        series = toolkit.df[column].dropna()
        if strategy == "mode":
            modes = series.mode()
            if modes.empty:
                raise ValueError("Aucune valeur observée pour calculer le mode.")
            value = modes.iloc[0]
        elif strategy in {"mean", "median"}:
            if not pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
                raise ValueError("Moyenne et médiane nécessitent une colonne numérique.")
            value = getattr(series, strategy)()
        else:
            raise ValueError("Stratégie attendue : median, mean ou mode.")
        if pd.isna(value):
            raise ValueError("Pas assez de valeurs observées.")
        if hasattr(value, "item"):
            value = value.item()
        return toolkit.manual_fill_missing(column, value, reason=f"Choix utilisateur : {strategy} calculé sur les valeurs observées.")
    if operation == "drop_missing_rows":
        return toolkit.manual_drop_rows_with_missing(column)
    # Dispatch only the explicitly listed operations, never arbitrary Python.
    handlers = {name: getattr(toolkit, name) for name in (
        "trim_whitespace", "normalize_case", "canonicalize_categories", "cast_column_type",
        "cap_outliers", "flag_temporal_anomaly", "flag_domain_implausible")}
    return handlers[operation](column, **parameters)


def preview(df, operation, column, parameters, explanation=""):
    toolkit = CleaningToolkit(df)
    result = execute(toolkit, operation, column, parameters)
    if not result.get("kept") or not result.get("rows_affected", 0):
        raise ValueError(result.get("reason") or "Cette opération ne produit aucun changement utile.")
    samples = []
    if column in df.columns and len(df) == len(toolkit.df):
        before, after = df[column], toolkit.df[column]
        changed = ~(before.eq(after) | (before.isna() & after.isna())).fillna(False)
        for position in changed.to_numpy().nonzero()[0][:5]:
            samples.append({"row": int(position) + 1,
                            "before": None if pd.isna(before.iloc[position]) else str(before.iloc[position]),
                            "after": None if pd.isna(after.iloc[position]) else str(after.iloc[position])})
    elif column in df.columns and operation == "drop_missing_rows":
        for position in df[column].isna().to_numpy().nonzero()[0][:5]:
            samples.append({"row": int(position) + 1, "before": None, "after": "Ligne supprimée"})
    impact = ""
    if operation in {"synthetic_emails", "replace_exact_with_synthetic_emails"}:
        impact = "Adresses fictives, pas des contacts réels. Chaque cellule générée sera marquée ; ne pas les compter comme des clients identifiés."
    elif operation == "fill_statistic":
        impact = "L’imputation peut modifier les distributions et réduire la variance. Le score de complétude ne mesure pas ce biais."
    elif operation in {"drop_missing_rows", "drop_exact_duplicates"}:
        impact = "Les lignes supprimées seront absentes des totaux et statistiques calculés sur le fichier nettoyé."
    elif operation == "cap_outliers":
        impact = "Les valeurs extrêmes seront remplacées ; cela modifie les statistiques. Vérifier leur sens métier."
    elif operation == "absolute_negative_values":
        impact = "Le signe sera supprimé. Utiliser uniquement si une quantité négative est une erreur de saisie, pas un retour ou un avoir."
    return {"id": uuid.uuid4().hex, "operation": operation, "column": column,
            "parameters": parameters, "title": CATALOG[operation][0], "explanation": explanation[:1500],
            "impact": impact, "rows_affected": result["rows_affected"], "rows_before": len(df),
            "rows_after": len(toolkit.df), "samples": samples, "fingerprint": fingerprint(df),
            "status": "ready"}
