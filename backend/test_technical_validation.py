import io
import json
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import main
from backend.test_generated_integration import isolated_app


class TechnicalValidationTests(unittest.TestCase):
    def test_csv_delimiter_encoding_and_catalog(self):
        with isolated_app() as root, TestClient(main.app) as client:
            response = client.post('/upload', files={'file': ('countries.csv', 'country;value\nAlgérie;2\nFrance;3\n'.encode('latin-1'))})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['columns'], ['country', 'value'])
            self.assertEqual(response.json()['validation']['details']['delimiter'], ';')
            self.assertTrue(client.get('/files').json()[0]['has_validation'])
            self.assertEqual(client.get('/validations/csv/countries').status_code, 200)
            self.assertEqual(list((root/'temporary').iterdir()), [])
            original = (root/'uploads/csv/countries.csv').read_bytes()
            duplicate = client.post('/upload', files={'file': ('countries.csv', b'x\n1\n')})
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual((root/'uploads/csv/countries.csv').read_bytes(), original)

    def test_rejection_keeps_permanent_storage_empty(self):
        for filename, payload in [('bad.csv', b'a,b\n1,2,3\n'), ('bad.json', b'{"x":}'), ('bad.xlsx', b'not a workbook'), ('empty.csv', b'')]:
            with self.subTest(filename=filename), isolated_app() as root, TestClient(main.app) as client:
                response = client.post('/upload', files={'file': (filename, payload)})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()['validation']['status'], 'rejected')
                self.assertEqual(client.get('/files').json(), [])
                self.assertEqual([p for p in root.rglob('*') if p.is_file()], [])

    def test_oversized_upload_and_utf8_bom_json(self):
        with isolated_app(), TestClient(main.app) as client:
            with patch.dict('os.environ', {'MAX_FILE_SIZE_MB':'1'}):
                response = client.post('/upload', files={'file': ('large.csv', b'x' * (1024*1024+1))})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()['validation']['reason'], 'FILE_TOO_LARGE')
            response = client.post('/upload', files={'file': ('bom.json', b'\xef\xbb\xbf[{"x":1}]')})
            self.assertEqual(response.status_code, 200, response.text)

    def test_legacy_dataset_can_be_validated_without_replacing_dataframe(self):
        with isolated_app() as root, TestClient(main.app) as client:
            original = root/'uploads/csv/legacy.csv'
            original.write_text('x\n1\n2\n', encoding='utf-8')
            response = client.post('/validate/csv/legacy')
            self.assertEqual(response.status_code, 200)
            self.assertFalse((root/'dataframes/csv/legacy_dataframe.pkl').exists())


if __name__ == '__main__':
    unittest.main()
