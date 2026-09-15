# InsightFlow Professional UI

This frontend keeps the original InsightFlow runtime logic unchanged and applies a professional shadcn-inspired interface around it.

## Visual principles

- Zinc/neutral shadcn palette
- Dark collapsible application sidebar
- Compact sticky workspace header
- Clear card hierarchy and restrained shadows
- Accessible semantic colors for validation, risk, claims and execution status
- Cleaning Agent chat/proposals remain functionally identical
- Presentation Agent setup/chat/plan/handoff remain functionally identical
- Data Formulator and Presenton keep the full-main-page iframe behavior

## Logic preservation

The following files in `public/legacy/` are copied from the original frontend without modification:

- `validation-ui.js`
- `cleaning-ui.js`
- `dashboard.js`
- `script.js`
- `report-ui.js`

Only layout markup presentation details and the React/Tailwind visual layer are changed.
