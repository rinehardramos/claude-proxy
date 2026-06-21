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


def test_main_cleans_corrupt_decided_and_falls_back(tmp_path, capsys, monkeypatch):
    import io
    import json as _json
    m = _load()

    # Point dirs to tmp_path
    m._common.PENDING_DIR = tmp_path / "pending"
    m._common.PENDING_DIR.mkdir()
    m._common.DECIDED_DIR = tmp_path / "decided"
    m._common.DECIDED_DIR.mkdir()

    monkeypatch.setattr(
        m._common, "load_config",
        lambda: {"bot_token": "t", "chat_id": "c",
                 "decide_route": "always", "decide_timeout": "1"},
    )
    monkeypatch.setattr(m._common, "send_message", lambda *a, **k: 1)

    hook_input = {
        "tool_name": "AskUserQuestion",
        "permission_mode": "default",
        "tool_input": {
            "questions": [{
                "question": "Q?",
                "header": "H",
                "options": [{"label": "A"}, {"label": "B"}],
                "multiSelect": False,
            }],
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(hook_input)))

    # Patch uuid so we know the decision_id
    FIXED_HEX = "abcdef1234567890abcdef1234567890"  # 32 chars
    monkeypatch.setattr(
        m.uuid, "uuid4",
        lambda: type("U", (), {"hex": FIXED_HEX})(),
    )

    # Pre-write a CORRUPT decided file
    corrupt_path = m._common.DECIDED_DIR / f"{FIXED_HEX}.json"
    corrupt_path.write_text("{ not json")

    import pytest
    with pytest.raises(SystemExit):
        m.main()

    out = capsys.readouterr().out.strip()
    assert out == "", f"Expected no stdout (fallback), got: {out!r}"
    assert not corrupt_path.exists(), "Corrupt decided file must be deleted"
