import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "hooks" / "_tg_hook_common.py"


def _load():
    spec = importlib.util.spec_from_file_location("tg_hook_common", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_parse_toml_basic():
    m = _load()
    d = m.parse_toml('a = "x"\nb = "y"  # c\n[sec]\n')
    assert d["a"] == "x" and d["b"] == "y"


def test_session_auto_approves_true_for_accept_edits():
    m = _load()
    assert m.session_auto_approves({"permission_mode": "acceptEdits"}) is True


def test_session_auto_approves_false_for_default():
    m = _load()
    assert m.session_auto_approves({"permission_mode": "default"}) is False
