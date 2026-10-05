import ast
import pathlib

import pytest

import bot
from questlog import api, domain, stats

# What each module of the package may import. Anything else (discord in the data layer, bot
# anywhere, os/json in the pure layer) means I/O or Discord objects leaked across a boundary.
ALLOWED_IMPORTS = {
    domain: {"asyncio", "re", "threading", "time", "collections"},
    api: {"json", "logging", "time", "requests", "questlog.domain"},
    stats: {"json", "logging", "threading", "time", "requests", "questlog.api"},
}
# Every module defines its own `log = logging.getLogger(...)` on purpose.
SHARED_NAMES = {"log"}


def parse(module) -> ast.Module:
    return ast.parse(pathlib.Path(module.__file__).read_text())


def top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def imported_modules(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return {name if name.startswith("questlog") else name.split(".")[0] for name in imported}


@pytest.mark.parametrize("module", list(ALLOWED_IMPORTS), ids=lambda m: m.__name__)
def test_each_module_only_imports_what_its_layer_allows(module):
    unexpected = imported_modules(parse(module)) - ALLOWED_IMPORTS[module]

    assert unexpected == set(), f"unexpected imports in {module.__name__}: {unexpected}"


@pytest.mark.parametrize("module", list(ALLOWED_IMPORTS), ids=lambda m: m.__name__)
def test_nothing_defined_in_a_package_module_is_also_defined_in_bot(module):
    duplicated = (top_level_names(parse(module)) & top_level_names(parse(bot))) - SHARED_NAMES

    assert duplicated == set()
