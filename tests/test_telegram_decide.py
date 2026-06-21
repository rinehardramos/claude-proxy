import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "hooks" / "telegram_decide.py"


def _load():
    spec = importlib.util.spec_from_file_location("telegram_decide", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_QS = [{
    "question": "Tabs or spaces?",
    "header": "Indent",
    "options": [{"label": "Tabs", "description": "t"},
                {"label": "Spaces", "description": "s"}],
    "multiSelect": False,
}]


def test_should_route_off():
    m = _load()
    assert m.should_route({"permission_mode": "acceptEdits"}, {"decide_route": "off"}) is False


def test_should_route_always():
    m = _load()
    assert m.should_route({"permission_mode": "default"}, {"decide_route": "always"}) is True


def test_should_route_auto_when_away():
    m = _load()
    assert m.should_route({"permission_mode": "acceptEdits"}, {"decide_route": "auto"}) is True


def test_should_route_auto_at_keyboard():
    m = _load()
    assert m.should_route({"permission_mode": "default"}, {"decide_route": "auto"}) is False


def test_has_multiselect():
    m = _load()
    assert m.has_multiselect(_QS) is False
    assert m.has_multiselect([{"multiSelect": True, "options": []}]) is True


def test_build_question_messages():
    m = _load()
    msgs = m.build_question_messages("abc123", _QS)
    assert len(msgs) == 1
    kb = msgs[0]["reply_markup"]["inline_keyboard"]
    # one button per option, correct callback_data
    assert kb[0][0]["callback_data"] == "qopt:abc123:0:0"
    assert kb[1][0]["callback_data"] == "qopt:abc123:0:1"
    assert "Tabs or spaces?" in msgs[0]["text"]


def test_assemble_answers():
    m = _load()
    ans = m.assemble_answers(_QS, {0: 1})
    assert ans == {"Tabs or spaces?": "Spaces"}
