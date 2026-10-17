"""Hash-checked pure prompt and parse functions from the frozen AML release."""
import ast
from functools import lru_cache
from pathlib import Path
import json
import re
import types
import math
import random

from ..runtime.artifacts import file_hash, read_json

ROOT = Path(__file__).resolve().parents[3]
DIRECTORIES = {"locomo_refined": "locomo-refined", "longmemeval_s": "longmemeval-s"}
FILES = {**{key: value + "/pipeline.py" for key, value in DIRECTORIES.items()},
         "beam": "beam/pipeline.py", "clbench": "clbench/pipeline.py",
         "personamem_v1": "personamem/pipeline_v1.py", "personamem_v2": "personamem/pipeline_v2.py"}


@lru_cache(maxsize=6)
def contract(benchmark):
    relative = FILES[benchmark]
    path = ROOT / "sources/aml" / relative
    audit = read_json(ROOT / "sources/source_audit.json")
    expected = audit["sources"][f"data/{relative}"]["sha256"]
    if file_hash(path) != expected:
        raise ValueError("Frozen native scorer source changed")
    tree = ast.parse(path.read_text())
    # Extract definitions only: no upstream imports, API clients or module execution.
    selected = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    selected += [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    scope = {"json": json, "re": re, "ast": ast, "math": math, "random": random, "Path": Path}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), scope)
    return types.SimpleNamespace(**{name: value for name, value in scope.items() if name != "__builtins__"},
                                 source_sha256=expected)
