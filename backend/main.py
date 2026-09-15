import json
import asyncio
import os
import re
import tempfile
from functools import wraps
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

# This is loaded before service imports so a persistent Docker configuration file
# is available to every existing service without changing their env contracts.
DEFAULT_SETTINGS_FILE = Path(__file__).resolve().parent / ".env"
GEMINI_ENV_FILE = Path(os.getenv("INSIGHTFLOW_SETTINGS_FILE", str(DEFAULT_SETTINGS_FILE)))
load_dotenv(GEMINI_ENV_FILE, override=False)


def _configured_origins() -> List[str]:
    raw = os.getenv(
        "INSIGHTFLOW_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080",
    )
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


ALLOWED_ORIGINS = _configured_origins()

try:
    from services.validation import ingest_upload, validate_file, ValidationFailure
    from services.profiling import (
        AIProfilingAgent,
        ProfilingReport,
        get_gemini_runtime_status,
        load_gemini_api_keys,
        mask_api_key,
    )
    from services.cleaning import (
        CleaningAgent,
        IssueResolutionAgent,
        IssueMessageRequest,
        refresh_cleaning_report,
        CleaningRequest,
        save_run_audit,
    )
    from services.datasets import (
        DatasetIdentityError,
        delete_dataset_artifacts,
        export_cleaned_dataframe,
        original_dataset_file,
        validate_dataset_identity,
    )
except ModuleNotFoundError:
    from backend.services.validation import ingest_upload, validate_file, ValidationFailure
    from backend.services.profiling import (
        AIProfilingAgent,
        ProfilingReport,
        get_gemini_runtime_status,
        load_gemini_api_keys,
        mask_api_key,
    )
    from backend.services.cleaning import (
        CleaningAgent,
        IssueResolutionAgent,
        IssueMessageRequest,
        refresh_cleaning_report,
        CleaningRequest,
        save_run_audit,
    )
    from backend.services.datasets import (
        DatasetIdentityError,
        delete_dataset_artifacts,
        export_cleaned_dataframe,
        original_dataset_file,
        validate_dataset_identity,
    )

try:
    from services.report_presentation.api.extractor_router import router as report_extractor_router
    from services.report_presentation.api.presentation_router import router as report_presentation_router
    from services.report_presentation.core.config import settings as report_settings
except ModuleNotFoundError:
    from backend.services.report_presentation.api.extractor_router import router as report_extractor_router
    from backend.services.report_presentation.api.presentation_router import router as report_presentation_router
    from backend.services.report_presentation.core.config import settings as report_settings

# Initialisation de l'application FastAPI
app = FastAPI(
    title="InsightFlow API",
    description="Validation → profilage → nettoyage + Word Report → Presentation Agent",
    version="3.0.0"
)

# Configuration du middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Word Report Extractor + Presentation Agent are mounted as an isolated service
# inside the same FastAPI application. Their original /api/* contracts are kept
# unchanged so the standalone service logic remains reusable and testable.
app.include_router(report_extractor_router)
app.include_router(report_presentation_router)

# Définition des répertoires de base et de stockage
BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
DATAFRAMES_DIR = STORAGE_DIR / "dataframes"
PROFILES_DIR = STORAGE_DIR / "profiles"
CLEANED_DIR = STORAGE_DIR / "cleaned"
CLEANING_REPORTS_DIR = STORAGE_DIR / "cleaning_reports"

FORMAT_TYPES = ["csv", "excel", "json"]
MANAGED_SETTINGS_DEFAULTS = {
    "GEMINI_MODEL": "gemini-2.5-flash",
    "GEMINI_KEY_COOLDOWN_SECONDS": 30,
    "MAX_FILE_SIZE_MB": 50,
    "MAX_DOCX_SIZE_MB": 25,
    "MAX_UNCOMPRESSED_SIZE_MB": 250,
    "MAX_ZIP_ENTRIES": 10000,
}

_dataset_mutation_locks = {}


def serialize_dataset_mutation(function):
    """Keep preview-version checks and saving together in this server process."""
    @wraps(function)
    async def guarded(file_type, stem, *args, **kwargs):
        lock = _dataset_mutation_locks.setdefault((file_type, stem), asyncio.Lock())
        async with lock:
            return await function(file_type, stem, *args, **kwargs)
    return guarded

def init_storage_directories() -> None:
    """Crée dynamiquement les dossiers de stockage s'ils n'existent pas."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DATAFRAMES_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    CLEANING_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    for fmt in FORMAT_TYPES:
        (UPLOADS_DIR / fmt).mkdir(parents=True, exist_ok=True)
        (DATAFRAMES_DIR / fmt).mkdir(parents=True, exist_ok=True)
        (PROFILES_DIR / fmt).mkdir(parents=True, exist_ok=True)
        (CLEANED_DIR / fmt).mkdir(parents=True, exist_ok=True)
        (CLEANING_REPORTS_DIR / fmt).mkdir(parents=True, exist_ok=True)


# Création des dossiers au démarrage
init_storage_directories()


def detect_file_type(filename: str) -> str:
    """
    Détecte le type de fichier à partir de son extension.
    Lève une HTTPException 400 si le format n'est pas supporté.
    """
    if not filename:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide ou manquant.")

    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return "csv"
    elif ext in [".xlsx", ".xls"]:
        return "excel"
    elif ext == ".json":
        return "json"
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Format de fichier '{ext}' non supporté. Les formats autorisés sont : .csv, .xlsx, .xls, .json."
        )


def parse_json_file_to_dataframe(file_path: Path) -> pd.DataFrame:
    """
    Parse intelligemment les fichiers JSON en DataFrame Pandas :
    - Liste d'objets : [{"col1": val, ...}, ...]
    - Dictionnaire encapsulant un tableau : {"countries": [...]} ou {"data": [...]}
    - Objet unique / dictionnaire plat ou imbriqué : {"col1": val}
    - Format JSON Lines (JSONL) / standard
    """
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            if len(raw) > 0 and isinstance(raw[0], dict):
                return pd.json_normalize(raw)
            return pd.DataFrame(raw)

        elif isinstance(raw, dict):
            # Vérifier si une des clés contient une liste d'objets
            candidate_lists = []
            for key, val in raw.items():
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                    candidate_lists.append((key, len(val), val))

            if candidate_lists:
                # Sélectionner la liste la plus longue comme collection principale
                candidate_lists.sort(key=lambda x: x[1], reverse=True)
                _, _, chosen_list = candidate_lists[0]
                return pd.json_normalize(chosen_list)

            # Objet unique -> 1 ligne
            return pd.json_normalize([raw])

        return pd.read_json(file_path)
    except Exception:
        return pd.read_json(file_path)


@app.post("/upload", response_model=Dict[str, Any])
async def upload_file(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Validate first, then persist the original and its compatible DataFrame."""
    from fastapi.responses import JSONResponse
    try:
        filename = Path((file.filename or "").replace("\\", "/")).name
        if not filename or filename in {".", ".."}:
            raise HTTPException(400, "Nom de fichier invalide.")
        file_type = detect_file_type(filename)
        report, df = await ingest_upload(file, filename, file_type,
            (STORAGE_DIR, UPLOADS_DIR, DATAFRAMES_DIR), parse_json_file_to_dataframe)
    except ValidationFailure as error:
        return JSONResponse(status_code=422, content={"detail": str(error), "validation": error.report})
    except FileExistsError:
        raise HTTPException(409, "Un fichier portant ce nom existe déjà. Renommez votre fichier pour conserver les analyses existantes.")
    finally:
        await file.close()
    records = json.loads(df.head(100).to_json(orient="records", date_format="iso"))
    return {"filename": filename, "file_type": file_type, "rows": len(df),
        "columns": list(map(str, df.columns)), "validation": report,
        "uploaded_file": str((UPLOADS_DIR / file_type / filename).relative_to(BASE_DIR)).replace("\\", "/"),
        "processed_file": str((DATAFRAMES_DIR / file_type / f"{Path(filename).stem}_dataframe.pkl").relative_to(BASE_DIR)).replace("\\", "/"),
        "data": records, "preview": records[:5], "preview_limit": 100}


def validation_path(file_type, stem):
    if file_type not in FORMAT_TYPES or Path(stem).name != stem or "\\" in stem:
        raise HTTPException(400, "Identifiant de dataset invalide.")
    return STORAGE_DIR / 'validations' / file_type / f'{stem}_validation.json'


@app.get("/validations/{file_type}/{stem}")
def get_validation(file_type: str, stem: str):
    path = validation_path(file_type, stem)
    if not path.exists():
        raise HTTPException(404, "Ce fichier ancien n’a pas encore de rapport de validation.")
    return json.loads(path.read_text(encoding='utf-8'))


@app.post("/validate/{file_type}/{stem}")
async def validate_existing(file_type: str, stem: str):
    path = validation_path(file_type, stem)
    originals = [p for p in (UPLOADS_DIR / file_type).iterdir() if p.is_file() and p.stem == stem]
    if len(originals) != 1:
        raise HTTPException(404, "Fichier original introuvable ou ambigu.")
    try:
        report, _ = await asyncio.to_thread(validate_file, originals[0], originals[0].name, parse_json_file_to_dataframe)
    except ValidationFailure as error:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"detail": str(error), "validation": error.report})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


async def ensure_technical_validation(file_type, stem):
    """Legacy datasets are checked on first use, without replacing their stored frames."""
    path = validation_path(file_type, stem)
    if not path.exists():
        result = await validate_existing(file_type, stem)
        if not isinstance(result, dict):
            raise HTTPException(422, 'Validation technique refusée. Consultez le rapport de validation avant de continuer.')


@app.get("/files", response_model=List[Dict[str, Any]])
def list_files() -> List[Dict[str, Any]]:
    """
    Retourne la liste de tous les fichiers téléversés et de leurs DataFrames et Profils associés.
    """
    file_list = []
    
    for fmt in FORMAT_TYPES:
        upload_dir = UPLOADS_DIR / fmt
        df_dir = DATAFRAMES_DIR / fmt
        prof_dir = PROFILES_DIR / fmt

        if not upload_dir.exists():
            continue

        for file_path in upload_dir.iterdir():
            if file_path.is_file():
                filename = file_path.name
                stem = file_path.stem
                
                pkl_path = df_dir / f"{stem}_dataframe.pkl"
                csv_path = df_dir / f"{stem}_dataframe.csv"
                profile_path = prof_dir / f"{stem}_profile.json"
                cleaning_path = CLEANING_REPORTS_DIR / fmt / f"{stem}_cleaning.json"

                stats = file_path.stat()
                file_size_kb = round(stats.st_size / 1024, 2)
                import datetime
                mtime = datetime.datetime.fromtimestamp(stats.st_mtime).strftime("%d/%m/%Y %H:%M")

                file_list.append({
                    "filename": filename,
                    "stem": stem,
                    "file_type": fmt,
                    "size_kb": file_size_kb,
                    "updated_at": mtime,
                    "has_validation": (STORAGE_DIR / "validations" / fmt / f"{stem}_validation.json").exists(),
                    "has_dataframe": pkl_path.exists(),
                    "has_profile": profile_path.exists(),
                    "has_cleaning": cleaning_path.exists(),
                    "has_cleaned_data": (CLEANED_DIR / fmt / f"{stem}_cleaned.pkl").exists(),
                    "cleaned_download": f"/cleanings/{fmt}/{stem}/download" if (CLEANED_DIR / fmt / f"{stem}_cleaned.pkl").exists() else None,
                    "uploaded_file": str(file_path.relative_to(BASE_DIR)).replace("\\", "/"),
                    "dataframe_file": str(pkl_path.relative_to(BASE_DIR)).replace("\\", "/") if pkl_path.exists() else None,
                    "profile_file": str(profile_path.relative_to(BASE_DIR)).replace("\\", "/") if profile_path.exists() else None,
                    "cleaning_file": str(cleaning_path.relative_to(BASE_DIR)).replace("\\", "/") if cleaning_path.exists() else None,
                })

    # Trier les fichiers du plus récent au plus ancien
    file_list.sort(key=lambda x: x["updated_at"], reverse=True)
    return file_list


@app.get("/files/{file_type}/{stem}", response_model=Dict[str, Any])
def read_stored_file(file_type: str, stem: str) -> Dict[str, Any]:
    """
    Lit et retourne le DataFrame complet sauvegardé (.pkl) avec toutes ses lignes et métadonnées.
    """
    if file_type not in FORMAT_TYPES:
        raise HTTPException(status_code=400, detail="Type de fichier invalide.")

    pkl_path = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.pkl"
    csv_path = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.csv"

    if not pkl_path.exists() and not csv_path.exists():
        raise HTTPException(status_code=404, detail="DataFrame non trouvé pour ce fichier.")

    # Chargement du DataFrame depuis le fichier .pkl (ou fallback .csv)
    try:
        if pkl_path.exists():
            df = pd.read_pickle(pkl_path)
        else:
            df = pd.read_csv(csv_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la lecture du DataFrame : {str(e)}")

    # Conversion de toutes les lignes du DataFrame
    all_data = df.where(pd.notnull(df), "").to_dict(orient="records")

    relative_dataframe_pkl = str(pkl_path.relative_to(BASE_DIR)).replace("\\", "/") if pkl_path.exists() else ""
    
    # Trouver le fichier original s'il existe
    upload_dir = UPLOADS_DIR / file_type
    original_files = list(upload_dir.glob(f"{stem}.*"))
    relative_uploaded_path = str(original_files[0].relative_to(BASE_DIR)).replace("\\", "/") if original_files else ""

    return {
        "filename": original_files[0].name if original_files else f"{stem}.{file_type}",
        "file_type": file_type,
        "rows": int(len(df)),
        "columns": [str(col) for col in df.columns],
        "uploaded_file": relative_uploaded_path,
        "processed_file": relative_dataframe_pkl,
        "data": all_data,
        "preview": all_data[:5]
    }


@app.delete("/files/{file_type}/{stem}", response_model=Dict[str, Any])
@serialize_dataset_mutation
async def delete_dataset(file_type: str, stem: str) -> Dict[str, Any]:
    """Delete one dataset and every validation/profiling/cleaning artifact it owns."""
    try:
        validate_dataset_identity(file_type, stem, FORMAT_TYPES)
    except DatasetIdentityError as error:
        raise HTTPException(status_code=400, detail=str(error))

    original = original_dataset_file(UPLOADS_DIR, file_type, stem)
    dataframe = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.pkl"
    if original is None and not dataframe.exists():
        raise HTTPException(status_code=404, detail=f"Dataset [{file_type}] '{stem}' introuvable.")

    try:
        deleted = delete_dataset_artifacts(
            storage_dir=STORAGE_DIR,
            uploads_dir=UPLOADS_DIR,
            dataframes_dir=DATAFRAMES_DIR,
            profiles_dir=PROFILES_DIR,
            cleaned_dir=CLEANED_DIR,
            cleaning_reports_dir=CLEANING_REPORTS_DIR,
            file_type=file_type,
            stem=stem,
        )
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Suppression incomplète du dataset : {error}")

    return {
        "status": "deleted",
        "file_type": file_type,
        "stem": stem,
        "deleted_count": len(deleted),
        "deleted": [str(path.relative_to(BASE_DIR)).replace("\\", "/") if path.is_relative_to(BASE_DIR) else str(path) for path in deleted],
    }


# =============================================================================
# 🔍 AI Profiling Agent Endpoints (Google AI Studio + Google ADK)
# =============================================================================

@app.post("/profile/{file_type}/{stem}", response_model=Dict[str, Any])
async def generate_profile(file_type: str, stem: str) -> Dict[str, Any]:
    """
    Exécute l'agent autonome de profilage IA (Google ADK + Gemini API) sur le DataFrame :
    1. Charge le DataFrame (.pkl / .csv)
    2. Instancie l'agent avec ses 12 outils d'analyse déterministes
    3. Exécute la boucle de raisonnement de l'agent
    4. Enregistre le rapport structuré dans storage/profiles/<file_type>/<stem>_profile.json
    5. Retourne le rapport de profilage structuré
    """
    if file_type not in FORMAT_TYPES:
        raise HTTPException(status_code=400, detail="Type de fichier invalide.")

    pkl_path = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.pkl"
    csv_path = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.csv"

    if not pkl_path.exists() and not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Aucun DataFrame trouvé pour [{file_type}] '{stem}'. Veuillez d'abord téléverser le fichier."
        )

    await ensure_technical_validation(file_type, stem)
    # Chargement du DataFrame
    try:
        if pkl_path.exists():
            df = pd.read_pickle(pkl_path)
        else:
            df = pd.read_csv(csv_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors du chargement du DataFrame : {str(e)}")

    # Exécution de l'Agent de Profilage IA
    try:
        agent = AIProfilingAgent(df)
        report = await agent.run()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'exécution de l'agent de profilage : {str(e)}")

    # Sauvegarde du rapport structuré dans storage/profiles/
    profile_dir = PROFILES_DIR / file_type
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_target_path = profile_dir / f"{stem}_profile.json"

    try:
        with open(profile_target_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'enregistrement du rapport de profilage : {str(e)}")

    return report


@app.get("/profiles/{file_type}/{stem}", response_model=Dict[str, Any])
def get_profile(file_type: str, stem: str) -> Dict[str, Any]:
    """
    Retourne le rapport de profilage existant sauvegardé.
    """
    if file_type not in FORMAT_TYPES:
        raise HTTPException(status_code=400, detail="Type de fichier invalide.")

    profile_path = PROFILES_DIR / file_type / f"{stem}_profile.json"
    if not profile_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Aucun profil trouvé pour [{file_type}] '{stem}'. Cliquez sur 'Profiler' pour en générer un."
        )

    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la lecture du profil : {str(e)}")


@app.get("/profiles", response_model=List[Dict[str, Any]])
def list_profiles() -> List[Dict[str, Any]]:
    """
    Liste tous les rapports de profilage disponibles dans le stockage.
    """
    profiles_list = []
    for fmt in FORMAT_TYPES:
        p_dir = PROFILES_DIR / fmt
        if not p_dir.exists():
            continue

        for p_file in p_dir.iterdir():
            if p_file.is_file() and p_file.name.endswith("_profile.json"):
                stem = p_file.stem.replace("_profile", "")
                stats = p_file.stat()
                import datetime
                mtime = datetime.datetime.fromtimestamp(stats.st_mtime).strftime("%d/%m/%Y %H:%M")

                # Lire les métadonnées de base du rapport
                rows_count = 0
                cols_count = 0
                issues_count = 0
                try:
                    with open(p_file, "r", encoding="utf-8") as f:
                        content = json.load(f)
                        rows_count = content.get("dataset", {}).get("rows", 0)
                        cols_count = content.get("dataset", {}).get("columns", 0)
                        issues_count = len(content.get("problems", []))
                except Exception:
                    pass

                profiles_list.append({
                    "stem": stem,
                    "file_type": fmt,
                    "filename": f"{stem}.{fmt}",
                    "report_file": str(p_file.relative_to(BASE_DIR)).replace("\\", "/"),
                    "created_at": mtime,
                    "rows": rows_count,
                    "columns": cols_count,
                    "potential_issues": issues_count,
                })

    profiles_list.sort(key=lambda x: x["created_at"], reverse=True)
    return profiles_list



# =============================================================================
# 🧹 AI Cleaning Agent Endpoints (Google AI Studio + Google ADK)
# =============================================================================

@app.post("/clean/{file_type}/{stem}", response_model=Dict[str, Any])
@serialize_dataset_mutation
async def clean_dataset(file_type: str, stem: str, payload: Optional[CleaningRequest] = None) -> Dict[str, Any]:
    """
    Exécute l'agent autonome de nettoyage IA (Google ADK + Gemini API) sur le DataFrame :
    1. Charge le DataFrame (.pkl / .csv)
    2. Requiert un profil existant (le génère automatiquement si absent)
    3. Planifie, génère du code et le valide dans un sandbox isolé
    4. Enregistre le DataFrame nettoyé et le rapport dans storage/cleaned/ et storage/cleaning_reports/
    5. Retourne le rapport de nettoyage structuré
    """
    if file_type not in FORMAT_TYPES:
        raise HTTPException(status_code=400, detail="Type de fichier invalide.")

    pkl_path = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.pkl"
    csv_path = DATAFRAMES_DIR / file_type / f"{stem}_dataframe.csv"

    if not pkl_path.exists() and not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Aucun DataFrame trouvé pour [{file_type}] '{stem}'. Veuillez d'abord téléverser le fichier."
        )

    await ensure_technical_validation(file_type, stem)

    try:
        if pkl_path.exists():
            df = pd.read_pickle(pkl_path)
        else:
            df = pd.read_csv(csv_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors du chargement du DataFrame : {str(e)}")

    # Un profil est requis comme preuve d'évidence ; on le génère automatiquement s'il est absent.
    profile_path = PROFILES_DIR / file_type / f"{stem}_profile.json"
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                profiling_report = json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur lors de la lecture du profil existant : {str(e)}")
    else:
        try:
            profiling_agent = AIProfilingAgent(df)
            profiling_report = await profiling_agent.run()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur lors de la génération automatique du profil requis : {str(e)}")

        profile_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profiling_report, f, indent=2, ensure_ascii=False)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur lors de l'enregistrement du profil auto-généré : {str(e)}")

    # Exécution de l'Agent de Nettoyage IA
    try:
        agent = CleaningAgent(df, profiling_report, approval_mode=payload.approval_mode if payload else None)
        report = await agent.run()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'exécution de l'agent de nettoyage : {str(e)}")

    # Sauvegarde du DataFrame nettoyé (séparément des originaux) et du rapport
    cleaned_dir = CLEANED_DIR / file_type
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    cleaned_pkl_path = cleaned_dir / f"{stem}_cleaned.pkl"
    cleaned_csv_path = cleaned_dir / f"{stem}_cleaned.csv"

    report_dir = CLEANING_REPORTS_DIR / file_type
    report_dir.mkdir(parents=True, exist_ok=True)
    report_target_path = report_dir / f"{stem}_cleaning.json"

    try:
        agent.cleaned_df.to_pickle(cleaned_pkl_path)
        agent.cleaned_df.to_csv(cleaned_csv_path, index=False, encoding="utf-8")
        with open(report_target_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        save_run_audit(STORAGE_DIR, file_type, stem, report)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'enregistrement du résultat de nettoyage : {str(e)}")

    return report


@app.get("/cleanings/{file_type}/{stem}", response_model=Dict[str, Any])
def get_cleaning_report(file_type: str, stem: str) -> Dict[str, Any]:
    """
    Retourne le rapport de nettoyage existant sauvegardé.
    """
    if file_type not in FORMAT_TYPES:
        raise HTTPException(status_code=400, detail="Type de fichier invalide.")

    report_path = CLEANING_REPORTS_DIR / file_type / f"{stem}_cleaning.json"
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Aucun rapport de nettoyage trouvé pour [{file_type}] '{stem}'. Cliquez sur 'Nettoyer' pour en générer un."
        )

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cleaned_path = CLEANED_DIR / file_type / f"{stem}_cleaned.pkl"
        if cleaned_path.exists():
            data = refresh_cleaning_report(data, pd.read_pickle(cleaned_path))
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la lecture du rapport de nettoyage : {str(e)}")


@app.get("/cleanings/{file_type}/{stem}/download")
def download_cleaned_dataset(file_type: str, stem: str) -> Response:
    """Download the latest cleaned dataframe in the original dataset format family."""
    try:
        validate_dataset_identity(file_type, stem, FORMAT_TYPES)
    except DatasetIdentityError as error:
        raise HTTPException(status_code=400, detail=str(error))

    cleaned_path = CLEANED_DIR / file_type / f"{stem}_cleaned.pkl"
    if not cleaned_path.exists():
        raise HTTPException(status_code=404, detail="Aucun fichier nettoyé disponible. Lancez d'abord le nettoyage.")

    try:
        df = pd.read_pickle(cleaned_path)
        content, filename, media_type = export_cleaned_dataframe(df, file_type, stem)
    except DatasetIdentityError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Impossible de préparer le fichier nettoyé : {error}")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/cleanings/{file_type}/{stem}/issues/message", response_model=Dict[str, Any])
@serialize_dataset_mutation
async def send_issue_message(file_type: str, stem: str, payload: IssueMessageRequest) -> Dict[str, Any]:
    """
    Envoie un message de l'utilisateur dans la conversation de résolution d'un
    problème précis. La conversation prépare des aperçus de code généré.
    Une décision Apply réexécute le code sauvegardé en sandbox et le valide
    avant de sauvegarder la modification. Le texte seul ne modifie pas les données.
    """
    if file_type not in FORMAT_TYPES:
        raise HTTPException(status_code=400, detail="Type de fichier invalide.")
    if not payload.decision and (not payload.message or not payload.message.strip()):
        raise HTTPException(status_code=400, detail="Le message ne peut pas être vide.")

    cleaned_pkl_path = CLEANED_DIR / file_type / f"{stem}_cleaned.pkl"
    cleaned_csv_path = CLEANED_DIR / file_type / f"{stem}_cleaned.csv"
    report_path = CLEANING_REPORTS_DIR / file_type / f"{stem}_cleaning.json"

    if not cleaned_pkl_path.exists() or not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Aucun résultat de nettoyage existant pour [{file_type}] '{stem}'. Veuillez d'abord lancer le nettoyage."
        )

    try:
        df = pd.read_pickle(cleaned_pkl_path)
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors du chargement de l'état de nettoyage : {str(e)}")

    message = payload.message.strip() or {"apply": "Appliquer cette proposition", "dismiss": "Écarter cette proposition",
                                         "keep": "Conserver et clôturer", "reopen": "Rouvrir ce problème"}.get(payload.decision, "")
    resolver = IssueResolutionAgent(df, report, payload.issue_id, message,
                                    proposal_id=payload.proposal_id, decision=payload.decision)
    try:
        updated_report, updated_df, _agent_reply = await resolver.run()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la résolution du problème : {str(e)}")

    try:
        updated_df.to_pickle(cleaned_pkl_path)
        updated_df.to_csv(cleaned_csv_path, index=False, encoding="utf-8")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(updated_report, f, indent=2, ensure_ascii=False)
        save_run_audit(STORAGE_DIR, file_type, stem, updated_report)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'enregistrement : {str(e)}")

    return updated_report


@app.get("/health/gemini", response_model=Dict[str, Any])
def gemini_health() -> Dict[str, Any]:
    """
    Retourne l'état runtime Gemini (sans exposer aucune clé API).
    """
    return get_gemini_runtime_status()


def _normalise_gemini_keys(payload: Dict[str, Any], *, required: bool = True) -> List[str]:
    """Validate UI-submitted keys without ever logging or returning them."""
    raw_keys = payload.get("keys")
    if raw_keys is None and not required:
        return []
    if not isinstance(raw_keys, list):
        raise HTTPException(status_code=422, detail="A list of Gemini API keys is required.")

    keys: List[str] = []
    seen = set()
    for raw_key in raw_keys:
        if not isinstance(raw_key, str):
            raise HTTPException(status_code=422, detail="Each Gemini API key must be text.")
        key = raw_key.strip()
        if not key:
            continue
        if len(key) < 8 or len(key) > 512 or any(char in key for char in (",", "\r", "\n")):
            raise HTTPException(status_code=422, detail="One Gemini API key has an invalid format.")
        if key not in seen:
            seen.add(key)
            keys.append(key)

    if not keys and required:
        raise HTTPException(status_code=422, detail="Enter at least one Gemini API key.")
    if len(keys) > 12:
        raise HTTPException(status_code=422, detail="You can configure up to 12 Gemini API keys.")
    return keys


def _managed_int_setting(payload: Dict[str, Any], name: str, minimum: int, maximum: int) -> int:
    value = payload.get(name.lower())
    try:
        value = int(value)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"{name} must be a whole number.") from error
    if not minimum <= value <= maximum:
        raise HTTPException(status_code=422, detail=f"{name} must be between {minimum} and {maximum}.")
    return value


def _normalise_managed_settings(payload: Dict[str, Any]) -> Dict[str, str]:
    """Validate runtime values that are safe for a local administrator to manage in the UI."""
    model = payload.get("gemini_model")
    if not isinstance(model, str):
        raise HTTPException(status_code=422, detail="GEMINI_MODEL is required.")
    model = model.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", model):
        raise HTTPException(status_code=422, detail="GEMINI_MODEL has an invalid format.")

    values = {
        "GEMINI_MODEL": model,
        "GEMINI_KEY_COOLDOWN_SECONDS": _managed_int_setting(payload, "GEMINI_KEY_COOLDOWN_SECONDS", 1, 3600),
        "MAX_FILE_SIZE_MB": _managed_int_setting(payload, "MAX_FILE_SIZE_MB", 1, 1024),
        "MAX_DOCX_SIZE_MB": _managed_int_setting(payload, "MAX_DOCX_SIZE_MB", 1, 1024),
        "MAX_UNCOMPRESSED_SIZE_MB": _managed_int_setting(payload, "MAX_UNCOMPRESSED_SIZE_MB", 1, 4096),
        "MAX_ZIP_ENTRIES": _managed_int_setting(payload, "MAX_ZIP_ENTRIES", 1, 100000),
    }
    if values["MAX_UNCOMPRESSED_SIZE_MB"] < values["MAX_DOCX_SIZE_MB"]:
        raise HTTPException(status_code=422, detail="MAX_UNCOMPRESSED_SIZE_MB must be at least MAX_DOCX_SIZE_MB.")
    return {name: str(value) for name, value in values.items()}


def _current_managed_settings() -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for name, default in MANAGED_SETTINGS_DEFAULTS.items():
        raw = os.environ.get(name, str(default))
        if name == "GEMINI_MODEL":
            values[name.lower()] = raw.strip() or default
            continue
        try:
            values[name.lower()] = int(raw)
        except (TypeError, ValueError):
            values[name.lower()] = default
    return values


def _require_local_settings_origin(request: Request) -> None:
    """Allow settings writes from localhost or an explicitly configured production UI."""
    origin = request.headers.get("origin")
    if not origin:  # CLI/server-side calls remain possible for local administration.
        return
    normalized_origin = origin.rstrip("/")
    if urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"} and normalized_origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Settings can only be changed from an authorized InsightFlow UI.")


def _replace_env_value(lines: List[str], name: str, value: str) -> List[str]:
    """Replace one dotenv value while keeping unrelated local configuration intact."""
    expression = re.compile(rf"^\s*{re.escape(name)}\s*=")
    replacement = f"{name}={value}\n"
    output: List[str] = []
    replaced = False
    for line in lines:
        if expression.match(line):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line if line.endswith("\n") else f"{line}\n")
    if not replaced:
        if output and output[-1].strip():
            output.append("\n")
        output.append(replacement)
    return output


def _persist_workspace_settings(keys: Optional[List[str]], settings: Dict[str, str]) -> None:
    """Persist UI-managed local settings and update this running server immediately."""
    existing = GEMINI_ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True) if GEMINI_ENV_FILE.exists() else []
    updated = existing
    if keys is not None:
        updated = _replace_env_value(updated, "GEMINI_API_KEYS", ",".join(keys))
        # The pooled setting has precedence; remove an old active single-key value to avoid retaining a stale secret.
        updated = [line for line in updated if not re.match(r"^\s*GEMINI_API_KEY\s*=", line)]
    for name, value in settings.items():
        updated = _replace_env_value(updated, name, value)

    GEMINI_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=GEMINI_ENV_FILE.parent,
            prefix=".gemini-settings-", suffix=".tmp", delete=False,
        ) as temporary:
            temporary.writelines(updated)
            temp_name = temporary.name
        os.replace(temp_name, GEMINI_ENV_FILE)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)

    if keys is not None:
        os.environ["GEMINI_API_KEYS"] = ",".join(keys)
        os.environ.pop("GEMINI_API_KEY", None)
    os.environ.update(settings)
    # Report presentation uses a settings instance created at import time; keep it in sync too.
    report_settings.gemini_model = settings["GEMINI_MODEL"]
    report_settings.max_docx_size_mb = int(settings["MAX_DOCX_SIZE_MB"])
    report_settings.max_uncompressed_size_mb = int(settings["MAX_UNCOMPRESSED_SIZE_MB"])
    report_settings.max_zip_entries = int(settings["MAX_ZIP_ENTRIES"])


def _gemini_test_message(error: Exception) -> str:
    """Give a useful but non-sensitive validation result to the settings UI."""
    status_code = getattr(error, "status_code", None)
    text = str(error).lower()
    if status_code in (401, 403) or any(marker in text for marker in ("invalid api key", "unauthorized", "permission denied", "forbidden")):
        return "Invalid or unauthorized key"
    if status_code == 429 or any(marker in text for marker in ("quota", "rate limit", "resource exhausted", "429")):
        return "Key accepted, but currently rate limited"
    return "Unable to validate this key right now"


def _save_individual_gemini_key_pool(keys: List[str]) -> Dict[str, Any]:
    """Persist a targeted key edit while retaining all other current workspace settings."""
    settings = _normalise_managed_settings(_current_managed_settings())
    try:
        _persist_workspace_settings(keys, settings)
    except OSError as error:
        raise HTTPException(status_code=500, detail="The local Gemini configuration could not be saved.") from error
    return {
        "total_keys": len(keys),
        "keys": [{"index": index, "masked": mask_api_key(key)} for index, key in enumerate(keys, start=1)],
    }


@app.get("/settings/gemini", response_model=Dict[str, Any])
def get_gemini_settings(request: Request) -> Dict[str, Any]:
    """Return safe Gemini configuration metadata only; full API keys never leave the server."""
    _require_local_settings_origin(request)
    runtime = get_gemini_runtime_status()
    return {
        "configured": bool(runtime.get("gemini_configured")),
        "model": runtime.get("model"),
        "total_keys": len(load_gemini_api_keys()),
        "keys": [
            {"index": index, "masked": mask_api_key(key)}
            for index, key in enumerate(load_gemini_api_keys(), start=1)
        ],
        "settings": _current_managed_settings(),
    }


@app.post("/settings/gemini/test", response_model=Dict[str, Any])
def test_gemini_settings(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """Test user-entered Gemini keys without persisting or revealing them."""
    _require_local_settings_origin(request)
    submitted_keys = _normalise_gemini_keys(payload, required=False)
    keys = submitted_keys or load_gemini_api_keys()
    if not keys:
        raise HTTPException(status_code=422, detail="Enter at least one Gemini API key before testing.")
    model = _normalise_managed_settings({**_current_managed_settings(), **payload})["GEMINI_MODEL"]
    try:
        from google import genai
    except ModuleNotFoundError as error:
        raise HTTPException(status_code=503, detail="The Gemini SDK is not installed on this server.") from error

    results = []
    for index, key in enumerate(keys, start=1):
        try:
            client = genai.Client(api_key=key)
            client.models.generate_content(model=model, contents="Reply with OK.")
            results.append({"index": index, "masked": mask_api_key(key), "valid": True, "message": "Validated"})
        except Exception as error:  # The provider's exception structure differs by transport/version.
            results.append({"index": index, "masked": mask_api_key(key), "valid": False, "message": _gemini_test_message(error)})

    return {"model": model, "results": results}


@app.post("/settings/gemini/keys/{key_index}/test", response_model=Dict[str, Any])
def test_saved_gemini_key(key_index: int, request: Request) -> Dict[str, Any]:
    """Test one saved key by index while keeping the underlying secret server-side."""
    _require_local_settings_origin(request)
    current_keys = load_gemini_api_keys()
    if key_index < 1 or key_index > len(current_keys):
        raise HTTPException(status_code=404, detail="Gemini key not found.")
    key = current_keys[key_index - 1]
    model = _current_managed_settings()["gemini_model"]
    try:
        from google import genai
        client = genai.Client(api_key=key)
        client.models.generate_content(model=model, contents="Reply with OK.")
        result = {"index": key_index, "masked": mask_api_key(key), "valid": True, "message": "Validated"}
    except ModuleNotFoundError as error:
        raise HTTPException(status_code=503, detail="The Gemini SDK is not installed on this server.") from error
    except Exception as error:  # The provider's exception structure differs by transport/version.
        result = {"index": key_index, "masked": mask_api_key(key), "valid": False, "message": _gemini_test_message(error)}
    return {"model": model, "result": result}


@app.put("/settings/gemini", response_model=Dict[str, Any])
def save_gemini_settings(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """Save a UI-managed Gemini key pool to the backend's local configuration."""
    _require_local_settings_origin(request)
    submitted_keys = _normalise_gemini_keys(payload, required=False)
    keys = submitted_keys or load_gemini_api_keys()
    if not keys:
        raise HTTPException(status_code=422, detail="Enter at least one Gemini API key before saving.")
    settings = _normalise_managed_settings({**_current_managed_settings(), **payload})
    try:
        _persist_workspace_settings(submitted_keys or None, settings)
    except OSError as error:
        raise HTTPException(status_code=500, detail="The local Gemini configuration could not be saved.") from error

    return {
        "saved": True,
        "total_keys": len(keys),
        "keys": [{"index": index, "masked": mask_api_key(key)} for index, key in enumerate(keys, start=1)],
        "settings": _current_managed_settings(),
    }


@app.put("/settings/gemini/keys/{key_index}", response_model=Dict[str, Any])
def replace_gemini_key(key_index: int, payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """Replace one saved key without ever returning its previous secret value."""
    _require_local_settings_origin(request)
    current_keys = load_gemini_api_keys()
    if key_index < 1 or key_index > len(current_keys):
        raise HTTPException(status_code=404, detail="Gemini key not found.")
    replacement = _normalise_gemini_keys({"keys": [payload.get("key")]})[0]
    if replacement in current_keys and replacement != current_keys[key_index - 1]:
        raise HTTPException(status_code=422, detail="That Gemini API key is already in the pool.")
    current_keys[key_index - 1] = replacement
    return _save_individual_gemini_key_pool(current_keys)


@app.delete("/settings/gemini/keys/{key_index}", response_model=Dict[str, Any])
def delete_gemini_key(key_index: int, request: Request) -> Dict[str, Any]:
    """Remove one key from the local pool; removing the last key disables Gemini until another is added."""
    _require_local_settings_origin(request)
    current_keys = load_gemini_api_keys()
    if key_index < 1 or key_index > len(current_keys):
        raise HTTPException(status_code=404, detail="Gemini key not found.")
    current_keys.pop(key_index - 1)
    return _save_individual_gemini_key_pool(current_keys)


@app.get("/services/status", response_model=Dict[str, Any])
def services_status() -> Dict[str, Any]:
    """Lightweight dashboard status without changing any service execution logic."""
    datasets = 0
    profiles = 0
    cleanings = 0
    for fmt in FORMAT_TYPES:
        datasets += sum(1 for p in (UPLOADS_DIR / fmt).glob("*") if p.is_file())
        profiles += sum(1 for p in (PROFILES_DIR / fmt).glob("*_profile.json") if p.is_file())
        cleanings += sum(1 for p in (CLEANING_REPORTS_DIR / fmt).glob("*_cleaning.json") if p.is_file())
    report_jobs = 0
    report_root = report_settings.storage_dir
    if report_root.exists():
        report_jobs = sum(1 for p in report_root.iterdir() if p.is_dir() and (p / "extraction.json").is_file())
    gemini = get_gemini_runtime_status()
    return {
        "validation": {"datasets": datasets, "status": "ready"},
        "profiling": {"profiles": profiles, "status": "ready", "gemini_configured": bool(gemini.get("gemini_configured"))},
        "cleaning": {"reports": cleanings, "status": "ready"},
        "report_presentation": {"jobs": report_jobs, "status": "ready"},
    }


@app.get("/")
def read_root():
    return {
        "app": "File Parser & AI Profiling Agent API",
        "status": "online",
        "agent": "Google ADK + Gemini API",
        "endpoints": {
            "upload": "POST /upload",
            "list_files": "GET /files",
            "read_file": "GET /files/{file_type}/{stem}",
            "delete_dataset": "DELETE /files/{file_type}/{stem}",
            "profile_file": "POST /profile/{file_type}/{stem}",
            "get_profile": "GET /profiles/{file_type}/{stem}",
            "list_profiles": "GET /profiles",
            "clean_file": "POST /clean/{file_type}/{stem}",
            "get_cleaning": "GET /cleanings/{file_type}/{stem}",
            "download_cleaned": "GET /cleanings/{file_type}/{stem}/download",
            "send_issue_message": "POST /cleanings/{file_type}/{stem}/issues/message",
            "gemini_health": "GET /health/gemini",
            "extract_word_report": "POST /api/extract",
            "report_job": "GET /api/jobs/{job_id}",
            "presentation_context": "GET /api/jobs/{job_id}/presentation/context",
            "presentation_plan": "POST /api/jobs/{job_id}/presentation/plan",
            "presentation_chat": "POST /api/jobs/{job_id}/presentation/chat",
            "presentation_package": "GET /api/jobs/{job_id}/presentation/download-package"
        }
    }
