# Validation technique en première étape

Référence étudiée : [InsightFlow-AI](https://github.com/mohamedoueslati-2/InsightFlow-AI), commit `c363582`.
Le service de validation technique de ce dépôt inspire cette intégration ; les agents de profilage et de nettoyage du projet local sont conservés.

## Structure et responsabilités

| Module | Responsabilité |
| --- | --- |
| `backend/main.py` | Routes compatibles avec l’application actuelle ; validation avant profilage et nettoyage |
| `backend/services/validation/technical.py` | Vérifications de format et parsing CSV, JSON, XLSX et XLS |
| `backend/services/validation/ingestion.py` | Upload par blocs, répertoire temporaire, publication sans écrasement et nettoyage en cas d’échec |
| `backend/services/profiling/` | Profilage existant, après validation du fichier |
| `backend/services/cleaning/` | Plan, comparaison, génération Pandas isolée et décisions utilisateur |
| `frontend/validation-ui.js` | Rapport de validation lisible et continuation vers le profilage |
| `frontend/workflow.css` | Interface responsive et repères des quatre étapes |

## Contrats et compatibilité

- `POST /upload` conserve ses champs de réponse et ajoute `validation`. Le tableau retourné est limité aux 100 premières lignes ; le DataFrame enregistré reste complet.
- `GET /validations/{type}/{stem}` retourne le rapport enregistré.
- `POST /validate/{type}/{stem}` contrôle un fichier ancien sans remplacer son DataFrame.
- `GET /files` ajoute `has_validation`.
- Les fichiers anciens sans rapport sont validés avant leur prochain profilage/nettoyage. Leur original doit être disponible.
- Un import portant le même nom retourne 409, sans écraser les données ni les anciens rapports. Renommer le fichier permet de créer un dataset distinct.
- Un rejet technique retourne 422 avec une raison et une suggestion de correction.
- Les rapports sont dans `storage/validations/{type}/`. Les originaux et DataFrames gardent leurs chemins historiques.

## Règles

La limite d’upload est `MAX_FILE_SIZE_MB=50` par défaut. Le CSV accepte UTF-8/BOM et Latin-1, ainsi que virgule, point-virgule, tabulation et barre verticale. Les largeurs de ligne sont contrôlées. JSON exige une syntaxe JSON standard (pas JSONL), un objet ou une liste. XLSX nécessite un conteneur ZIP valide ; sa taille décompressée est limitée à 250 Mo. XLS est conservé pour compatibilité via xlrd.

Pour Excel, toutes les feuilles sont lues et la première feuille non vide devient le DataFrame ; un avertissement indique la feuille retenue. Le classeur original est conservé. La validation technique ne certifie ni la justesse des valeurs, ni l’absence de doublons : ces questions appartiennent au profilage.

## Vérification

`python -m unittest backend.test_technical_validation` couvre les rejets, les encodages et séparateurs, la taille maximale, le BOM JSON, les conflits de nom et les fichiers anciens. `backend.test_generated_integration` vérifie le parcours CSV/XLSX/JSON jusqu’au nettoyage dans Docker avec Gemini simulé.
