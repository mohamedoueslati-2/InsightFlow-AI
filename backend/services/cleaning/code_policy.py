"""Conservative AST guard. Container isolation remains the security boundary."""
import ast
import hashlib


class CodePolicyError(ValueError):
    pass


BLOCKED = set('os sys subprocess socket requests urllib http pathlib shutil multiprocessing threading open eval exec compile __import__ globals locals vars getattr setattr delattr hasattr input breakpoint help dir type object super memoryview print exit quit pip'.split())
BLOCKED_ATTRIBUTES = set('eval query load loads dump dumps save savetxt savez savez_compressed fromfile tofile memmap ctypes system popen get_handle read_clipboard to_clipboard'.split())
ALLOWED_EXPORTS = {'to_numeric', 'to_datetime', 'to_timedelta'}


def validate_code(code: str) -> str:
    if len(code.encode('utf-8')) > 20000:
        raise CodePolicyError('Code exceeds 20000 bytes')
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        raise CodePolicyError(f'Syntax error at line {error.lineno}: {error.msg}') from error
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise CodePolicyError('Exactly one top-level function is required')
    fn = tree.body[0]
    args = fn.args
    if (fn.name != 'clean_dataframe' or fn.decorator_list or fn.returns or
            len(args.args) != 1 or args.args[0].arg != 'df' or args.args[0].annotation or
            args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.defaults):
        raise CodePolicyError('Required signature: def clean_dataframe(df)')
    if not fn.body or ast.dump(fn.body[0]) != ast.dump(ast.parse('working_df = df.copy()').body[0]):
        raise CodePolicyError('First statement must be working_df = df.copy()')
    if not isinstance(fn.body[-1], ast.Return) or not isinstance(fn.body[-1].value, ast.Name) or fn.body[-1].value.id != 'working_df':
        raise CodePolicyError('Last statement must return working_df')
    nodes = list(ast.walk(tree))
    if len(nodes) > 2500:
        raise CodePolicyError('AST complexity limit exceeded')
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef,
                             ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Delete,
                             ast.Yield, ast.YieldFrom, ast.Await)):
            raise CodePolicyError(f'Forbidden construct: {type(node).__name__}')
        if isinstance(node, ast.FunctionDef) and node is not fn:
            raise CodePolicyError('Nested function definitions are not supported; use expressions')
        if isinstance(node, ast.Name) and (node.id in BLOCKED or node.id.startswith('_')):
            raise CodePolicyError(f'Forbidden name: {node.id}')
        if isinstance(node, ast.Attribute):
            name = node.attr
            if (name.startswith('_') or name in BLOCKED or name in BLOCKED_ATTRIBUTES or
                    name.startswith('read_') or (name.startswith('to_') and name not in ALLOWED_EXPORTS and name not in {'to_numpy', 'to_list', 'to_dict', 'to_period', 'to_timestamp', 'to_frame', 'to_series'})):
                raise CodePolicyError(f'Forbidden attribute: {name}')
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and '__' in node.value:
            raise CodePolicyError('Dunder strings are forbidden')
    return hashlib.sha256(code.encode('utf-8')).hexdigest()
