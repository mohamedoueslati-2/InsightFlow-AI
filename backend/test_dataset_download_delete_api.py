from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("google.genai", reason="google-genai is installed from backend/requirements.txt in the full app environment")

from backend import main
from backend.test_generated_integration import isolated_app, upload


def test_cleaned_download_matches_csv_json_excel_format_family():
    with isolated_app() as root, TestClient(main.app) as client:
        for extension in ["csv", "json", "xlsx"]:
            file_type, stem = upload(client, extension)
            frame = pd.DataFrame({"name": ["Alice", "Bob"], "score": [10, 20]})
            target = root / "cleaned" / file_type / f"{stem}_cleaned.pkl"
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.to_pickle(target)

            response = client.get(f"/cleanings/{file_type}/{stem}/download")
            assert response.status_code == 200, response.text
            disposition = response.headers["content-disposition"]
            if file_type == "csv":
                assert disposition.endswith(f'{stem}_cleaned.csv"')
                assert "Alice" in response.content.decode("utf-8-sig")
            elif file_type == "json":
                assert disposition.endswith(f'{stem}_cleaned.json"')
                assert b"Alice" in response.content
            else:
                assert disposition.endswith(f'{stem}_cleaned.xlsx"')
                restored = pd.read_excel(BytesIO(response.content))
                assert restored.to_dict(orient="records") == frame.to_dict(orient="records")


def test_delete_dataset_removes_all_related_artifacts_only():
    with isolated_app() as root, TestClient(main.app) as client:
        file_type, stem = upload(client, "csv")
        # Add every downstream artifact type owned by this dataset.
        validation = root / "validations" / file_type / f"{stem}_validation.json"
        profile = root / "profiles" / file_type / f"{stem}_profile.json"
        cleaned_pkl = root / "cleaned" / file_type / f"{stem}_cleaned.pkl"
        cleaned_csv = root / "cleaned" / file_type / f"{stem}_cleaned.csv"
        report = root / "reports" / file_type / f"{stem}_cleaning.json"
        audit = root / "cleaning_runs" / file_type / stem / "run-1" / "plan.json"
        for path in [validation, profile, cleaned_pkl, cleaned_csv, report, audit]:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".pkl":
                pd.DataFrame({"x": [1]}).to_pickle(path)
            else:
                path.write_text("{}", encoding="utf-8")

        other = root / "uploads" / "csv" / "other.csv"
        other.write_text("x\n1\n", encoding="utf-8")

        response = client.delete(f"/files/{file_type}/{stem}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "deleted"
        assert not (root / "uploads" / file_type / f"{stem}.csv").exists()
        assert not (root / "dataframes" / file_type / f"{stem}_dataframe.pkl").exists()
        assert not validation.exists()
        assert not profile.exists()
        assert not cleaned_pkl.exists()
        assert not cleaned_csv.exists()
        assert not report.exists()
        assert not (root / "cleaning_runs" / file_type / stem).exists()
        assert other.exists()
