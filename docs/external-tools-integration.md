# External Docker tools in InsightFlow

InsightFlow keeps Data Formulator and Presenton as independent Docker services and displays them inside the existing workspace through lazy-loaded iframes.

## Start the tools

```powershell
docker compose -f docker-compose.tools.yml up -d
```

Then start InsightFlow normally:

```powershell
python run.py
```

Open `http://localhost:3000`. The sidebar now contains **Data Formulator** and **Presenton AI**.

Default URLs:

- Data Formulator: `http://localhost:5567`
- Presenton AI: `http://localhost:5001`

Override them when needed:

```powershell
$env:DATA_FORMULATOR_URL="https://data.example.com"
$env:PRESENTON_URL="https://slides.example.com"
python run.py
```

The current integration is UI access only. Cleaned datasets and ZIP handoff files are not automatically transferred between services yet; that should be implemented as a separate API integration step.
