# Precise merge review — InsightFlow AI + Word Report Extractor

## What the source projects actually contain

### InsightFlow AI

The backend is a single FastAPI composition file (`backend/main.py`) over three already-separated domain packages:

- `services/validation`: technical ingestion and validation.
- `services/profiling`: Google ADK + Gemini profiling with deterministic Pandas tools.
- `services/cleaning`: generated-cleaning orchestration, sandbox execution, validation, risk decisions and issue chat.

The original public dataset API is concentrated in `main.py`: `/upload`, `/files`, `/validate`, `/profile`, `/clean`, `/cleanings`, etc. Storage is filesystem-based and separated by purpose (`uploads`, `dataframes`, `profiles`, `cleaned`, `cleaning_reports`).

The original frontend was one long page driven mainly by `frontend/script.js`, with `validation-ui.js` and `cleaning-ui.js` as supporting render modules.

### Word Report Extractor / Presentation Agent

This project already has a more modular FastAPI structure: routers, schemas, deterministic DOCX extraction services, Presentation Agent services, and job-scoped storage. It exposes stable `/api/*` routes and persists each report under a UUID job directory.

Its two important boundaries are preserved:

1. The extractor is deterministic and does not use an LLM to parse DOCX content or original images.
2. The Presentation Agent only works on extractor outputs and produces a validated plan / downloadable handoff package.

## Merge decision

A large rewrite of InsightFlow `main.py` would create regression risk in validation/profiling/cleaning. Therefore the merge uses **composition instead of rewriting**:

- Existing InsightFlow service code is untouched.
- The report project is moved intact under `services/report_presentation`.
- Internal imports are made package-relative.
- Only environment/storage roots are adapted to the InsightFlow repository.
- Its original routers are mounted into the existing FastAPI app.

This gives a cleaner architecture immediately while keeping the known dataset behavior stable.

## Verified invariants

The directories below are byte-for-byte unchanged from the supplied InsightFlow project:

- `backend/services/validation`
- `backend/services/profiling`
- `backend/services/cleaning`

The Word Report service differs from the supplied project only where integration requires it:

- Python package import paths.
- Backend `.env` path resolution.
- Report job storage root (`backend/storage/report_jobs`).

The extraction/planning algorithms, schemas, prompt logic, visual association, interactive chat and ZIP package builder are otherwise preserved.

## Frontend architecture improvement

The original long page is now a shell with independent service views:

```text
Dashboard
  ├─ Validation
  ├─ Profiling
  ├─ Cleaning
  └─ Word -> Slides
```

The original dataset JavaScript remains the owner of dataset behavior. The new dashboard calls it through a small `window.InsightFlowData` UI bridge. This avoids duplicating profiling/cleaning logic in the dashboard layer.

The report frontend is adapted from the supplied report project. Only API URL resolution was changed so it works with InsightFlow's dynamic backend port served by `run.py`.

## Recommended future refactor (not required for this merge)

Once the merged build is stable, `backend/main.py` can be decomposed into dataset routers (`validation_router.py`, `profiling_router.py`, `cleaning_router.py`) while keeping the same URLs. That refactor should be done separately because it is architectural work, not required to integrate the report service and would otherwise make regression diagnosis harder.
