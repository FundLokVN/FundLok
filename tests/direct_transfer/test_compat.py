"""Spec: HANDOFF-04 B15 (legacy place_order deprecated behind a flag; old behaviour stays by default)."""
import ast
import pathlib

import pytest

import app.direct_transfer as pkg
from app.direct_transfer import compat
from app.direct_transfer.errors import DirectTransferError


def test_legacy_place_order_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LEGACY_PLACE_ORDER_ENABLED", raising=False)
    assert compat.legacy_place_order_enabled() is True
    compat.assert_legacy_place_order_enabled()


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_legacy_place_order_can_be_retired_with_410(monkeypatch, value):
    monkeypatch.setenv("LEGACY_PLACE_ORDER_ENABLED", value)
    assert compat.legacy_place_order_enabled() is False
    with pytest.raises(DirectTransferError) as info:
        compat.assert_legacy_place_order_enabled()
    assert info.value.status_code == 410 and info.value.code == "LEGACY_ORDER_RETIRED"


def test_new_module_never_posts_omnibus_cash_or_uses_gpay():
    """Hard rules: no OMNIBUS_CASH legs, no GPay code, no BackgroundTasks for money (string literals and imports only,
    docstrings and comments may explain why)."""
    root = pathlib.Path(pkg.__file__).parent
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert "OMNIBUS_CASH" not in node.value, (path.name, node.lineno)
                assert "gpay" not in node.value.lower(), (path.name, node.lineno)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any("gpay" in n.lower() or "BackgroundTasks" in n for n in names), (path.name, node.lineno)
