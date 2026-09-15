# Word Report Extractor — InsightFlow service

A standalone, deterministic DOCX extraction MVP. Upload a Word report to obtain structured text, sections, original embedded visuals, ordered blocks, image context, and a compact input for a future Presentation Agent.

```text
DOCX → Word Extractor → structured text + original images
                     → normalized presentation input
```

There is no LLM, OCR, PDF conversion, screenshotting, chart reconstruction, database, authentication, or presentation generation.

## Install and run

This service is now mounted inside the InsightFlow backend. Install and run from the InsightFlow project root as documented in the main README. The original extraction and Presentation Agent API contracts below are preserved.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. API documentation is at http://127.0.0.1:8000/docs.

On macOS/Linux, activate with `source .venv/bin/activate`.

Tests:

```powershell
python -m pytest
```

For the environment created during initial implementation at the repository root, run from `backend` using `..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload` or `..\.venv\Scripts\python.exe -m pytest`.

## How extraction works

A DOCX is a ZIP-based OOXML package. `word/document.xml` contains body paragraphs, runs, tables, and image references. `word/_rels/document.xml.rels` resolves those references to media parts such as `word/media/image1.png`. `word/styles.xml` provides heading style names and inheritance.

The parser walks document XML order. Text before and after an inline image becomes separate blocks so the image keeps its actual position. Tables occupy their original position and expose both simple `rows` and ordered nested `cells`. Block `order` is one-based preorder, including table containers and their descendants, so top-level order numbers can have gaps. Repeated references produce repeated image blocks while retaining one asset per media part. Asset numbering is stable storage naming; it does not define document order.

Title priority: Title style, Heading 1, first nonempty text paragraph, filename stem. Heading styles and inherited outline levels take priority; conservative numbered-heading detection applies to unstyled/Normal paragraphs. It is a heuristic: short numbered list items can resemble headings. Each main-body heading begins a flat section at its original level; content belongs to the most recent heading. Table-cell headings remain within the containing section. Introductory content gets a section using the report title.

Assets contain `context` for the first occurrence and `occurrences` for every body placement, including section, neighboring text, block ID, and document order. Context stays within the section. Caption preference is an adjacent Caption-style or Figure/Chart-number paragraph, followed by alt text, nearby short paragraph, section title, and generic fallback.

Every file under `word/media/` is copied directly to the job assets directory. PNG/JPEG dimensions are read with Pillow; SVG dimensions are read from explicit size attributes, including physical units. Unknown dimensions are null. Nothing is resized, recompressed, or resaved through Pillow. SHA-256 hashes verify the exact stored bytes. Supported browser formats preview directly from the original asset URL. Other formats are preserved and marked without preview support.

Original extraction preserves Data Formulator visual style, avoids PDF screenshot quality loss and chart reconstruction differences, and gives the future Presentation Agent the actual visual stored in Word.

## API

| Method | Route | Result |
| --- | --- | --- |
| POST | `/api/extract` | Multipart `file`, `.docx` only; normalized extraction JSON |
| GET | `/api/jobs/{job_id}` | Saved extraction JSON |
| GET | `/api/jobs/{job_id}/assets/{filename}` | Exact original asset bytes |
| GET | `/api/jobs/{job_id}/report` | Markdown with relative `assets/` links |
| GET | `/api/jobs/{job_id}/presentation-input` | Compact future-agent input |
| GET | `/api/health` | `{"status":"ok"}` |

Example: `curl -F "file=@report.docx" http://127.0.0.1:8000/api/extract`

Each job uses a UUID and saves:

```text
storage/jobs/{job_id}/
  source/report.docx
  assets/image_001.png
  report.md
  extraction.json
  presentation_input.json
```

The response includes `jobId`, `report` (`title`, `plainText`, `markdown`, `sections`), `assets`, `blocks`, and `warnings`. Presentation input contains title and sections with content and visual URLs; downstream agents do not need DOCX XML knowledge. Relative Markdown image links work when the Markdown and assets directory are kept together.

## Configuration and security

Environment variables (or a local `.env`):

```text
MAX_DOCX_SIZE_MB=25
MAX_UNCOMPRESSED_SIZE_MB=250
MAX_ZIP_ENTRIES=10000
```

`STORAGE_DIR` optionally overrides the job directory. Defaults are resolved relative to the backend, independent of working directory.

The service reads ZIP members directly rather than extracting archive paths. It rejects unsafe paths, duplicate entries, excessive expanded size/count, malformed XML, DTDs, and non-DOCX content types. External relationships are skipped and never fetched. Macro-enabled documents are rejected by their main-part content type; embedded code is never executed. SVG bytes remain intact; asset responses enforce a sandboxed content security policy to prevent scripts when opened directly. The UI previews SVG with an image element and inserts extracted text using `textContent`.

Invalid formats return 415, malformed documents 400, oversized uploads 413, and missing jobs/assets 404. Unexpected extraction errors produce a safe 500 message; diagnostic stack traces stay in server logs. Failed jobs are removed.

This is a local MVP. Upload data persists on disk until manually removed. Before public deployment, add proxy-level request size/rate limits and operational storage controls; multipart parsing can spool incoming bodies before endpoint validation.

## MVP limitations

- Main body order is supported, including inline/floating image XML anchors, tables, and table images. Floating visual layout coordinates are not reconstructed.
- Header/footer/footnote-only media and unused alternatives are extracted with null body context and a warning; those story parts are not merged into the body sequence.
- Native editable Word charts, SmartArt, and drawings may have no embedded preview. The service warns for chart references and does not render or reconstruct them.
- Compatibility markup uses its first Choice (or fallback). SVG and raster alternatives can both exist in media; unused versions remain available as unplaced assets.
- Numbered-heading and caption detection are deterministic heuristics, not semantic analysis. Automatic Word numbering fields are not recalculated.
- Table `rows` represent physical XML cells; merged grid spans are not expanded into repeated values. Markdown treats the first row as a header.
- Strict OOXML namespace variants are not supported; ordinary Word transitional DOCX is supported.

## Future work (not implemented)

Presentation Agent; multimodal analysis of extracted charts; Presenton integration; PPTX generation; DOCX drawings/shapes; EMF/WMF conversion when required; automatic job cleanup; optional image enhancement as a separate derivative; SVG preference; human-in-the-loop slide selection.

The current boundary ends at `presentation_input.json`.

## Presentation Agent — current implemented stage

The project now includes a first Presentation Assistant stage on top of the deterministic extractor. The user still uploads only one `report.docx`; no CSV/XLSX/JSON dataset upload was added.

```text
report.docx
  ↓
existing deterministic extractor
  ↓
extraction.json + presentation_input.json + original assets
  ↓
Google ADK Presentation Agent
  ├─ Report tools: overview / sections
  ├─ Data tools: extracted Word tables
  └─ Asset tools: original Word visuals
  ↓
validated presentation_plan.json
  ↓
presenton_prompt.txt
```

At this stage the agent only selects source material from the extractor and creates the presentation story/plan. Presenton API integration, AssetMapper, final PPTX/PDF export, and external Data Formulator integration are intentionally not implemented yet.

### Gemini / Google AI Studio configuration

Install dependencies, then create `backend/.env` from `.env.example` and add a Google AI Studio Gemini key:

```text
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
```

Multiple keys are also supported:

```text
GEMINI_API_KEYS=key1,key2
GEMINI_KEY_COOLDOWN_SECONDS=30
```

Keys stay in the backend only and are never returned to the frontend.

### Presentation Agent API

| Method | Route | Result |
| --- | --- | --- |
| GET | `/api/jobs/{job_id}/presentation/context` | Extracted sections, Word tables, original asset metadata and agent status |
| POST | `/api/jobs/{job_id}/presentation/plan` | Run Google ADK agent and create/refine the plan |
| GET | `/api/jobs/{job_id}/presentation/plan` | Read persisted validated plan |
| PUT | `/api/jobs/{job_id}/presentation/plan` | Save a human-edited validated plan |
| GET | `/api/jobs/{job_id}/presentation/presenton-prompt` | Human-readable prompt generated from the plan for Presenton |

The plan is stored at:

```text
storage/jobs/{job_id}/presentation/presentation_plan.json
storage/jobs/{job_id}/presentation/presenton_prompt.txt
```

The frontend keeps the existing extraction UI and adds **Prepare Presentation**, audience/objective/language/duration/slide-count/style inputs, extractor-source previews, generated slide cards, and a copyable Presenton prompt.


## Agent-proposed presentation setup

After a DOCX is extracted, **Prepare Presentation** now asks the Google ADK Presentation Agent to inspect the existing extractor output and recommend **Audience, Objective, Slides, and Duration**. These are suggestions only: the frontend pre-fills editable inputs and the user's final values are authoritative when the full presentation plan is generated.

Endpoint: `POST /api/jobs/{job_id}/presentation/suggest-setup`. This step does not build slides yet and uses only the already-extracted report, Word tables, and original asset metadata.

## Presentation Agent `.env` location

Create the runtime environment file at `backend/.env` (next to `requirements.txt`).
The Presentation Agent loads this file by absolute backend path, so it works even if Uvicorn is started from another working directory.
After changing `.env`, restart Uvicorn so the Google ADK runtime reloads the configuration.

## Presentation Agent planning quality (Stage 1.1)

The Presentation Agent now adds three grounding/quality improvements before a plan is saved:

- **Visual association:** original Word visuals carry nearby paragraph context plus explicit chart/caption candidates (`chartTitle`, `caption`, `associationText`, `associationMethod`, `associationConfidence`). The planner must inspect this metadata before selecting a visual, and a deterministic resolver can move a selected visual to a materially better-matching slide without reading image pixels.
- **Claim provenance:** slide claims are classified as `fact`, `interpretation`, or `recommendation`, and every claim carries real extractor source references. Unknown source ids are rejected. Presenton prompt output preserves these semantic labels.
- **Stronger ending:** explicit `Conclusion` / `Recommendations` text is exposed through a read-only agent tool. For plans with at least four slides, the agent is instructed to reserve the final slide for a source-grounded conclusion/recommendations ending. A deterministic fallback ensures an explicit source conclusion is not lost if the model ends on a raw metric.

The user still uploads only the original DOCX. There is no secondary dataset upload and Presenton is not called automatically in this stage.

## Interactive Presentation Agent — current stage

After the initial plan is generated, the same Presentation Agent can now refine it conversationally. The user can ask natural-language requests such as "reduce to 5 slides", "focus more on Tunisia", "combine slides 3 and 4", "make this for my professor", or "why did you choose image_003?".

The interactive agent always receives the current validated `PresentationPlan`, recent conversation history, and the same read-only extractor tools. It may update audience/objective/duration/style, add/remove/reorder/combine slides, change emphasis, and select or omit visuals. Source facts, real source ids, original asset filenames, and Fact / Interpretation / Recommendation provenance remain protected by backend validation.

Interactive endpoints:

| Method | Route | Result |
| --- | --- | --- |
| POST | `/api/jobs/{job_id}/presentation/chat` | Refine or discuss the current plan using natural language |
| GET | `/api/jobs/{job_id}/presentation/conversation` | Read persisted refinement conversation |
| DELETE | `/api/jobs/{job_id}/presentation/conversation` | Clear chat history while keeping the current plan |

The conversation is stored without a database at:

```text
storage/jobs/{job_id}/presentation/presentation_conversation.json
```

Every successful interactive change immediately re-saves `presentation_plan.json` and regenerates `presenton_prompt.txt`, so the Presenton handoff always reflects the latest user-approved direction. Presenton API automation is still intentionally outside this milestone.

## Final presentation download package

After the user finishes the initial plan and any interactive refinements, the frontend exposes **Download Result**. No Presenton API connection is used. Nothing is sent automatically to any external service.

The backend creates a ZIP from the **latest validated** PresentationPlan:

```text
presentation-handoff.zip
├── presenton_prompt.txt
├── report.docx
├── presentation_plan.json
├── README.txt
└── images/
    └── only the original visuals selected by the agent
```

Endpoint:

| Method | Route | Result |
| --- | --- | --- |
| GET | `/api/jobs/{job_id}/presentation/download-package` | ZIP with prompt, original report, validated plan and selected original visuals |

The user can then open Presenton manually (or any other presentation tool), paste `presenton_prompt.txt`, upload `report.docx`, and upload the selected images as needed. Template, tone, design, and final generation remain entirely under user control.

The frontend keeps visible loading states while the Presentation Agent proposes setup, builds/refines the plan, and while the final ZIP package is being prepared.
