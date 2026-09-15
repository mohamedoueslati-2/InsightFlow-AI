# Dataset lifecycle: cleaned download and full deletion

This change is intentionally isolated from the validation/profiling/cleaning agent logic.

## Cleaned export

`GET /cleanings/{file_type}/{stem}/download` loads the latest cleaned pickle and serializes it on demand. No extra export copy is persisted.

| Original family | Download |
| --- | --- |
| CSV | `<stem>_cleaned.csv` (UTF-8-SIG) |
| JSON | `<stem>_cleaned.json` (records array) |
| Excel (`.xlsx` or legacy `.xls` upload) | `<stem>_cleaned.xlsx` |

Using the cleaned pickle as the source guarantees that later human-in-the-loop decisions are included.

## Dataset deletion

`DELETE /files/{file_type}/{stem}` removes only artifacts owned by the selected dataset:

- `storage/uploads/<type>/<original>`
- `storage/dataframes/<type>/<stem>_dataframe.pkl|csv`
- `storage/validations/<type>/<stem>_validation.json`
- `storage/profiles/<type>/<stem>_profile.json`
- `storage/cleaned/<type>/<stem>_cleaned.*`
- `storage/cleaning_reports/<type>/<stem>_cleaning.json`
- `storage/cleaning_runs/<type>/<stem>/...`

The Word Report / Presentation Agent storage remains untouched.
