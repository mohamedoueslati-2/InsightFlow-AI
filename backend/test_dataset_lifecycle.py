from io import BytesIO
from pathlib import Path

import pandas as pd

try:
    from services.datasets import delete_dataset_artifacts, export_cleaned_dataframe
except ModuleNotFoundError:
    from backend.services.datasets import delete_dataset_artifacts, export_cleaned_dataframe


def test_export_cleaned_dataframe_keeps_original_format_family():
    df = pd.DataFrame([{"name": "Alice", "amount": 12.5}, {"name": "Bob", "amount": 4.0}])

    csv_bytes, csv_name, csv_type = export_cleaned_dataframe(df, "csv", "sales")
    assert csv_name == "sales_cleaned.csv"
    assert "text/csv" in csv_type
    assert "Alice" in csv_bytes.decode("utf-8-sig")

    json_bytes, json_name, json_type = export_cleaned_dataframe(df, "json", "sales")
    assert json_name == "sales_cleaned.json"
    assert "application/json" in json_type
    assert '"name":"Alice"' in json_bytes.decode("utf-8").replace(" ", "").replace("\n", "")

    xlsx_bytes, xlsx_name, xlsx_type = export_cleaned_dataframe(df, "excel", "sales")
    assert xlsx_name == "sales_cleaned.xlsx"
    assert "spreadsheetml" in xlsx_type
    restored = pd.read_excel(BytesIO(xlsx_bytes))
    assert restored.to_dict(orient="records") == df.to_dict(orient="records")


def test_delete_dataset_artifacts_removes_every_dataset_owned_result(tmp_path: Path):
    storage = tmp_path / "storage"
    uploads = storage / "uploads"
    dataframes = storage / "dataframes"
    profiles = storage / "profiles"
    cleaned = storage / "cleaned"
    cleaning_reports = storage / "cleaning_reports"
    fmt = "csv"
    stem = "customers"

    files = [
        uploads / fmt / f"{stem}.csv",
        dataframes / fmt / f"{stem}_dataframe.pkl",
        dataframes / fmt / f"{stem}_dataframe.csv",
        storage / "validations" / fmt / f"{stem}_validation.json",
        profiles / fmt / f"{stem}_profile.json",
        cleaned / fmt / f"{stem}_cleaned.pkl",
        cleaned / fmt / f"{stem}_cleaned.csv",
        cleaning_reports / fmt / f"{stem}_cleaning.json",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    audit = storage / "cleaning_runs" / fmt / stem / "run-001" / "plan.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("{}", encoding="utf-8")

    unrelated = uploads / fmt / "other.csv"
    unrelated.write_text("keep", encoding="utf-8")

    deleted = delete_dataset_artifacts(
        storage_dir=storage,
        uploads_dir=uploads,
        dataframes_dir=dataframes,
        profiles_dir=profiles,
        cleaned_dir=cleaned,
        cleaning_reports_dir=cleaning_reports,
        file_type=fmt,
        stem=stem,
    )

    assert deleted
    assert all(not path.exists() for path in files)
    assert not (storage / "cleaning_runs" / fmt / stem).exists()
    assert unrelated.exists()
