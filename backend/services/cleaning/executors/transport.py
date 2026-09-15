"""Bounded JSON-only DataFrame transport. Never unpickle sandbox output."""
import json
import math
import re
import pandas as pd


def encode_frame(frame):
    def value(x):
        if x is None or x is pd.NA or x is pd.NaT:
            return None
        if isinstance(x, (pd.Timestamp, pd.Timedelta)):
            return x.isoformat()
        if hasattr(x, 'item'):
            x = x.item()
        if isinstance(x, float) and not math.isfinite(x):
            return {'special_float': str(x)}
        if isinstance(x, (str, float, int, bool)):
            return x
        raise ValueError(f'Unsupported transport value: {type(x).__name__}')
    columns = []
    for name in frame:
        series = frame[name]
        meta = {'name': name, 'dtype': str(series.dtype), 'values': [value(x) for x in series]}
        if isinstance(series.dtype, pd.CategoricalDtype):
            meta.update(categories=[value(x) for x in series.cat.categories], ordered=series.cat.ordered)
        columns.append(meta)
    return json.dumps({'columns': columns, 'index': [value(x) for x in frame.index],
                       'index_name': frame.index.name}, allow_nan=False, ensure_ascii=False)


def decode_frame(payload):
    if len(payload.encode()) > 64*1024*1024:
        raise ValueError('DataFrame exceeds transport limit')
    obj = json.loads(payload)
    if len(obj['columns']) > 10000 or len(obj['index']) > 2000000:
        raise ValueError('DataFrame dimensions exceed transport limit')
    def value(x):
        if isinstance(x, dict):
            if set(x) != {'special_float'} or x['special_float'] not in {'nan', 'inf', '-inf'}:
                raise ValueError('Invalid scalar envelope')
            return float(x['special_float'])
        if x is not None and not isinstance(x, (str, int, float, bool)):
            raise ValueError('Invalid scalar type')
        return x
    columns = {}
    for entry in obj['columns']:
        if not isinstance(entry['name'], str) or entry['name'] in columns:
            raise ValueError('Invalid column name')
        dtype = entry['dtype']
        if not isinstance(dtype, str) or not re.fullmatch(r'(?:object|str|string|bool|boolean|category|(?:u?int|UInt|Int|float|Float)(?:8|16|32|64)|datetime64\[(?:s|ms|us|ns)(?:, [A-Za-z0-9_+\-/]+)?\]|timedelta64\[(?:s|ms|us|ns)\])', dtype):
            raise ValueError('Unsupported or unsafe dtype in sandbox output')
        if dtype == 'category':
            dtype = pd.CategoricalDtype([value(x) for x in entry['categories']], ordered=entry['ordered'])
        columns[entry['name']] = pd.Series([value(x) for x in entry['values']], dtype=dtype)
    frame = pd.DataFrame(columns)
    frame.index = pd.Index([value(x) for x in obj['index']], name=obj.get('index_name'))
    return frame
