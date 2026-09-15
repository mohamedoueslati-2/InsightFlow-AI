# Frontend migration rule: preserve behavior first

This React/Vite migration intentionally keeps the original InsightFlow frontend runtime JavaScript unchanged under `public/legacy/`.

Preserved logic files:

- `validation-ui.js`
- `cleaning-ui.js`
- `dashboard.js`
- `script.js`
- `report-ui.js`

The React application mounts the **same original DOM contract** from `src/legacy-layout.html`, then loads those files in the same order as the original page.

This means the following are not reimplemented or simplified:

- dataset upload and technical validation
- profiling agent workflow
- cleaning agent execution
- approval modes (`auto_safe`, `review_all`, `full_auto`)
- generated cleaning proposals and sandbox previews
- per-issue cleaning chat, quick suggestions, apply/dismiss decisions
- generated-code validation and attempt history
- cleaned dataset/report downloads
- Word DOCX extractor and original assets
- Presentation Agent context + setup suggestion
- presentation plan generation
- provenance (Fact / Interpretation / Recommendation)
- interactive Presentation Agent chat and quick prompts
- Presenton prompt refresh/copy
- presentation handoff ZIP download
- Data Formulator / Presenton embedded Docker views
- collapsible sidebar behavior

`src/index.css` is a visual override only. Backend code and API contracts are unchanged.
