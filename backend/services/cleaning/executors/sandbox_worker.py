"""Container entrypoint ONLY. Never import this module from the web application."""
import contextlib
import datetime
import io
import json
import math
import re
import statistics
import traceback
import pandas as pd
import numpy as np
from transport import encode_frame, decode_frame


def main():
    try:
        with open('/input/data.json', encoding='utf-8') as source:
            frame = decode_frame(source.read())
        with open('/input/program.py', encoding='utf-8') as source:
            code = source.read()
        builtins = {}
        import builtins as builtin_module
        for name in 'abs all any bool dict enumerate float int isinstance len list max min range round set sorted str sum tuple zip ValueError TypeError'.split():
            builtins[name] = getattr(builtin_module, name)
        namespace = {'__builtins__': builtins, 'pd': pd, 'np': np, 're': re,
                     'datetime': datetime, 'math': math, 'statistics': statistics}
        # This is the sole generated-code execution site, inside the isolated image.
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, '<generated-cleaning>', 'exec'), namespace)
            candidate = namespace['clean_dataframe'](frame.copy(deep=True))
        if not isinstance(candidate, pd.DataFrame):
            raise TypeError('clean_dataframe must return one pandas DataFrame')
        output = encode_frame(candidate)
        if len(output.encode()) > 60 * 1024 * 1024:
            raise ValueError('Output exceeds limit')
        print(json.dumps({'status': 'ok', 'data': output}))
    except Exception:
        # Deliberate sandbox boundary: traceback is returned to the repair agent.
        print(json.dumps({'status': 'error', 'error': traceback.format_exc()[-4000:]}))


if __name__ == '__main__':
    main()
