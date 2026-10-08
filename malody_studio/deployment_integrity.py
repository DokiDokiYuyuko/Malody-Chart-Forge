"""Fingerprint inference definitions separately from model catalog display text."""
import ast
import hashlib
from pathlib import Path


def registry_inference_hash(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'))
    tree.body = [item for item in tree.body if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or item.name != 'catalog']
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
