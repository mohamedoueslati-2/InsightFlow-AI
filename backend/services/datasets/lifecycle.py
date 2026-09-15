"""Dataset lifecycle helpers shared by API routes.

This module intentionally does not know anything about profiling/cleaning agent
logic. It only handles safe dataset identity checks, cleaned-file serialization,
and deletion of artifacts that belong to one dataset.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from shutil import rmtree
from typing import Iterable

import pandas as pd


class DatasetIdentityError(ValueError):
    """Raised when a file_type/stem pair could escape the dataset storage tree."""


def validate_dataset_identity(file_type: str, stem: str, allowed_types: Iterable[str]) -> None:
    allowed = set(allowed_types)
    if file_type not in allowed:
        raise DatasetIdentityError("Type de fichier invalide.")
    if not stem or Path(stem).name != stem or stem in {".", ".."} or "\\" in stem or "/" in stem:
        raise DatasetIdentityError("Identifiant de dataset invalide.")


def original_dataset_file(uploads_dir: Path, file_type: str, stem: str) -> Path | None:
    """Return the unique original file for a dataset, if one exists."""
    root = uploads_dir / file_type
    if not root.exists():
        return None
    matches = [p for p in root.iterdir() if p.is_file() and p.stem == stem]
    return matches[0] if len(matches) == 1 else None


def export_cleaned_dataframe(df: pd.DataFrame, file_type: str, stem: str) -> tuple[bytes, str, str]:
    """Serialize the latest cleaned DataFrame in the dataset's original format family.

    CSV -> UTF-8-SIG CSV
    JSON -> JSON records array
    Excel -> XLSX (including legacy .xls uploads, because openpyxl writes XLSX safely)
    """
    if file_type == "csv":
        payload = df.to_csv(index=False).encode("utf-8-sig")
        return payload, f"{stem}_cleaned.csv", "text/csv; charset=utf-8"

    if file_type == "json":
        payload = df.to_json(orient="records", force_ascii=False, indent=2, date_format="iso").encode("utf-8")
        return payload, f"{stem}_cleaned.json", "application/json; charset=utf-8"

    if file_type == "excel":
        buffer = BytesIO()
        df.to_excel(buffer, index=False, engine="openpyxl")
        return (
            buffer.getvalue(),
            f"{stem}_cleaned.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    raise DatasetIdentityError("Type de fichier invalide.")


def delete_dataset_artifacts(
    *,
    storage_dir: Path,
    uploads_dir: Path,
    dataframes_dir: Path,
    profiles_dir: Path,
    cleaned_dir: Path,
    cleaning_reports_dir: Path,
    file_type: str,
    stem: str,
) -> list[Path]:
    """Delete every persisted artifact owned by one dataset.

    The Word-report/presentation storage is deliberately not touched because it
    is a separate service and does not belong to the dataset lifecycle.
    """
    deleted: list[Path] = []

    def unlink(path: Path) -> None:
        if path.is_file():
            path.unlink()
            deleted.append(path)

    # Original upload(s). A collision should not normally exist because upload
    # rejects duplicate names, but remove every exact-stem original defensively.
    upload_root = uploads_dir / file_type
    if upload_root.exists():
        for path in upload_root.iterdir():
            if path.is_file() and path.stem == stem:
                unlink(path)

    # Parsed dataframe representations.
    for suffix in ("_dataframe.pkl", "_dataframe.csv"):
        unlink(dataframes_dir / file_type / f"{stem}{suffix}")

    # Technical validation and profiling outputs.
    unlink(storage_dir / "validations" / file_type / f"{stem}_validation.json")
    unlink(profiles_dir / file_type / f"{stem}_profile.json")

    # Cleaning state. Glob makes future persisted download formats safe to clean
    # up too, while remaining scoped to this exact dataset prefix.
    clean_root = cleaned_dir / file_type
    if clean_root.exists():
        for path in clean_root.glob(f"{stem}_cleaned.*"):
            unlink(path)
    unlink(cleaning_reports_dir / file_type / f"{stem}_cleaning.json")

    # Generated cleaning run audit tree (programs, attempts, risk, validation).
    audit_root = storage_dir / "cleaning_runs" / file_type / stem
    if audit_root.is_dir():
        rmtree(audit_root)
        deleted.append(audit_root)

    return deleted
