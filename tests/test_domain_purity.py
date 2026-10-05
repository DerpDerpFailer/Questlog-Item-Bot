import ast
import pathlib

import bot
from questlog import domain

# Standard-library pieces domain.py is allowed to use. Anything else (discord, requests, os,
# json, io, ...) would mean I/O or Discord objects leaked into the pure layer.
ALLOWED_IMPORTS = {"asyncio", "re", "threading", "time", "collections"}


def parse(module) -> ast.Module:
    return ast.parse(pathlib.Path(module.__file__).read_text())


def top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_domain_only_imports_the_standard_library_pieces_it_needs():
    imported: set[str] = set()
    for node in ast.walk(parse(domain)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert imported <= ALLOWED_IMPORTS, f"unexpected imports in domain.py: {imported - ALLOWED_IMPORTS}"


def test_nothing_defined_in_domain_is_also_defined_in_bot():
    duplicated = top_level_names(parse(domain)) & top_level_names(parse(bot))

    assert duplicated == set()
