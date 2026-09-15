"""Format checks inspired by InsightFlow-AI, adapted to the existing Pandas pipeline."""
import csv
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, BadZipFile

import pandas as pd


class ValidationFailure(ValueError):
    def __init__(self, reason, message, fix):
        super().__init__(message)
        self.report = {'status': 'rejected', 'reason': reason, 'message': message, 'suggested_fix': fix}


def reject(reason, message, fix):
    raise ValidationFailure(reason, message, fix)


def validate_file(path: Path, filename: str, json_parser):
    extension = Path(filename).suffix.lower()
    if extension not in {'.csv', '.xlsx', '.xls', '.json'}:
        reject('UNSUPPORTED_FORMAT', 'Ce format n’est pas pris en charge.', 'Choisissez un fichier CSV, XLSX, XLS ou JSON.')
    if not path.stat().st_size:
        reject('EMPTY_FILE', 'Le fichier est vide.', 'Exportez un fichier contenant un tableau de données.')
    with path.open('rb') as source:
        sample = source.read(65536)
    details, warnings = {}, []
    try:
        if extension == '.csv':
            encoding = 'utf-8-sig'
            try:
                # Decode the whole stream, not a possibly truncated multibyte sample.
                with path.open(encoding=encoding) as stream:
                    for line in stream:
                        pass
            except UnicodeDecodeError:
                encoding = 'latin-1'
                warnings.append('Encodage Latin-1 détecté ; il sera utilisé pour lire le fichier.')
            text = sample.decode(encoding, errors='ignore')
            try:
                delimiter = csv.Sniffer().sniff(text, delimiters=',;\t|').delimiter
            except csv.Error:
                delimiter = max(',;\t|', key=text.count) if any(c in text for c in ',;\t|') else ','
            width, rows = None, 0
            with path.open(encoding=encoding, newline='') as stream:
                reader = csv.reader(stream, delimiter=delimiter, strict=True)
                for row in reader:
                    if not row:
                        continue
                    if any(any(ord(c) < 32 and c not in '\t\r\n' for c in cell) for cell in row):
                        reject('INVALID_SIGNATURE', 'Le CSV contient des caractères binaires.', 'Réexportez le tableau en CSV texte.')
                    if width is None:
                        width = len(row)
                    elif len(row) != width:
                        reject('CSV_PARSE_ERROR', f'La ligne {reader.line_num} contient {len(row)} champs au lieu de {width}.', 'Vérifiez le séparateur, les guillemets et les colonnes de cette ligne.')
                    rows += 1
            if rows < 2:
                reject('EMPTY_TABLE', 'Le CSV ne contient aucune ligne de données.', 'Ajoutez des données sous les en-têtes.')
            details = {'encoding': encoding, 'delimiter': delimiter}
            frame = pd.read_csv(path, encoding=encoding, sep=delimiter)
        elif extension == '.json':
            # Strict syntax gate before the application's existing normalization.
            with path.open(encoding='utf-8-sig') as stream:
                raw = json.load(stream, parse_constant=lambda value: reject('INVALID_JSON', f'Constante JSON invalide : {value}.', 'Utilisez null à la place de NaN ou Infinity.'))
            if not isinstance(raw, (dict, list)):
                reject('NON_TABULAR_JSON', 'Le JSON ne représente pas un tableau ou un objet.', 'Exportez une liste d’objets JSON.')
            # Existing parser expects UTF-8; UTF-8 BOM is handled in the shared parser.
            frame = json_parser(path)
            details = {'encoding': 'utf-8', 'structure': 'array' if isinstance(raw, list) else 'object'}
        else:
            if extension == '.xlsx':
                if not sample.startswith(b'PK\x03\x04'):
                    reject('INVALID_SIGNATURE', 'Ce fichier n’est pas un classeur XLSX valide.', 'Ouvrez le fichier dans Excel puis enregistrez-le en XLSX.')
                with ZipFile(path) as archive:
                    if sum(entry.file_size for entry in archive.infolist()) > 250 * 1024 * 1024:
                        reject('WORKBOOK_TOO_LARGE', 'Le classeur décompressé dépasse 250 Mo.', 'Exportez une feuille plus petite.')
            elif not sample.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
                reject('INVALID_SIGNATURE', 'La signature du fichier XLS est invalide.', 'Réexportez le fichier au format XLSX.')
            with pd.ExcelFile(path) as workbook:
                sheets = workbook.sheet_names
                frame = None
                chosen = None
                for sheet in sheets:
                    candidate = workbook.parse(sheet)
                    if frame is None and not candidate.empty:
                        frame, chosen = candidate, sheet
                if frame is None:
                    reject('EMPTY_WORKBOOK', 'Le classeur ne contient aucun tableau de données.', 'Ajoutez une feuille avec des en-têtes et des lignes.')
                details = {'sheets': sheets, 'selected_sheet': chosen}
                if len(sheets) > 1:
                    warnings.append(f'Plusieurs feuilles détectées. La feuille « {chosen} » sera utilisée pour le profilage ; le classeur original est conservé.')
        if frame.empty or not len(frame.columns):
            reject('EMPTY_TABLE', 'Aucun tableau exploitable n’a été trouvé.', 'Exportez un tableau avec des en-têtes et au moins une ligne.')
    except ValidationFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError, BadZipFile, KeyError, OSError) as error:
        reject('PARSE_ERROR', f'Lecture du fichier impossible : {str(error)[:200]}', 'Vérifiez la syntaxe du fichier ou réexportez-le dans son format d’origine.')
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    return {'status': 'accepted', 'message': 'Fichier valide et lisible. Prêt pour le profilage.',
            'filename': filename, 'format': extension.lstrip('.'), 'size_bytes': path.stat().st_size,
            'sha256': digest, 'rows': len(frame), 'columns_count': len(frame.columns),
            'details': details, 'warnings': warnings,
            'checks': ['Format reconnu', 'Contenu lisible', 'Structure vérifiée', 'Tableau exploitable']}, frame
