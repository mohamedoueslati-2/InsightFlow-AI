# InsightFlow AI — Word Report / Presentation integration

## Goal

Integrate the existing **Word Report Extractor + interactive Presentation Agent** into InsightFlow AI without changing the business logic of either existing domain.

The merged application now has two independent workflows behind one FastAPI process and one dashboard:

```text
DATA WORKFLOW
CSV / XLSX / JSON
  -> technical validation
  -> DataFrame persistence
  -> Google ADK profiling
  -> generated cleaning + sandbox validation
  -> human decisions

REPORT WORKFLOW
DOCX
  -> deterministic OOXML extraction
  -> sections / tables / original Word assets
  -> Google ADK setup suggestion
  -> validated PresentationPlan
  -> interactive plan refinement
  -> presentation-handoff.zip
```

## What was deliberately not changed

The following original InsightFlow service directories are copied without logic changes:

- `backend/services/validation/`
- `backend/services/profiling/`
- `backend/services/cleaning/`

The Word Report project is also kept as an isolated package. Its extraction, asset preservation, presentation planning, interactive chat and ZIP packaging algorithms remain the same. Only imports/configuration paths were adapted so the package can live inside InsightFlow.

## Backend architecture

```text
backend/
  main.py                         # composition root / existing dataset endpoints
  services/
    validation/                   # existing
    profiling/                    # existing
    cleaning/                     # existing
    report_presentation/          # integrated project
      api/
        extractor_router.py
        presentation_router.py
      core/
        config.py
        exceptions.py
      schemas/
      services/
        document_parser.py
        docx_service.py
        image_extractor.py
        job_service.py
        presentation_catalog.py
        presentation_package.py
        presentation_agent/
          agent.py
          conversation_service.py
          gemini_key_manager.py
          plan_service.py
          prompt_builder.py
          prompts.py
          toolkit.py
          visual_association.py
```

`backend/main.py` is the composition root. It keeps all previous InsightFlow routes and mounts the two original report routers. Existing report API contracts are kept (`/api/extract`, `/api/jobs/...`) so the report service stays independently testable.

Report jobs are isolated under:

```text
backend/storage/report_jobs/{job_id}/
  source/report.docx
  assets/
  extraction.json
  presentation_input.json
  report.md
  presentation/
    presentation_plan.json
    presentation_conversation.json
    presenton_prompt.txt
    presentation_handoff.zip
```

Dataset storage remains exactly where InsightFlow already expects it.

## Frontend architecture

The frontend is now a service dashboard rather than a single long workflow page:

- **Dashboard** — global service status and counts.
- **Validation** — existing upload, validation, DataFrame preview and dataset table.
- **Profiling** — dedicated dataset chooser and existing profiling report UI.
- **Cleaning** — dedicated dataset chooser, approval mode and existing cleaning/review UI.
- **Word -> Slides** — DOCX extractor, source preview, setup suggestions, presentation plan, interactive chat and final ZIP download.

Files added for composition only:

```text
frontend/dashboard.js
frontend/dashboard.css
frontend/report-ui.js
frontend/report-service.css
```

`frontend/script.js` still owns the original dataset UI logic. A very small public bridge (`window.InsightFlowData`) lets the dashboard delegate to the existing profile/clean functions instead of duplicating business behavior.

## Shared Gemini configuration

Both AI domains read the same backend `.env` without exposing keys in the frontend:

```dotenv
GEMINI_API_KEYS=key1,key2
# or GEMINI_API_KEY=key1
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_KEY_COOLDOWN_SECONDS=30
```

Report-specific limits:

```dotenv
MAX_DOCX_SIZE_MB=25
MAX_UNCOMPRESSED_SIZE_MB=250
MAX_ZIP_ENTRIES=10000
```

## Important separation

The report workflow does **not** upload a dataset and the dataset workflow does **not** parse DOCX. They share the FastAPI process and Gemini configuration only. Their source data, storage, schemas and domain logic remain separate.
