"""main._apply_flavor_to_goal_blockの単体テスト。

check_inのgoalブロックに対するflavor展開の対象範囲（remaining/terminalの
bound文字列とopen_questionsのtitleだけを展開し、statement・条件文・note・
judge_note・waiver_reasonには触れない）を、DB無しで確認する。
"""
import src.main as main_module


def _spy_expand(monkeypatch):
    calls = []

    def fake_expand(content, flavor, conn):
        calls.append(content)
        return f"[[{content}]]"

    monkeypatch.setattr(main_module.citation_renderer, "expand", fake_expand)
    return calls


def test_expands_remaining_and_terminal_bound_and_open_question_titles(monkeypatch):
    calls = _spy_expand(monkeypatch)
    goal_block = {
        "label": "judge_ready",
        "statement": "終わりの一文",
        "remaining": [{"id_raw": 1, "statement": "s1", "bound": "decision『d』: 崩れ"}],
        "terminal": [
            {"id_raw": 1, "statement": "s1", "bound": "decision『d』: 崩れ"},
            {"id_raw": 2, "statement": "s2", "note": "済ませた"},
        ],
        "open_questions": [{"type": "ask", "id_raw": 3, "title": "問い"}],
    }

    main_module._apply_flavor_to_goal_block(goal_block, "readable", conn=None)

    assert goal_block["remaining"][0]["bound"] == "[[decision『d』: 崩れ]]"
    assert goal_block["terminal"][0]["bound"] == "[[decision『d』: 崩れ]]"
    assert goal_block["open_questions"][0]["title"] == "[[問い]]"
    assert calls == ["decision『d』: 崩れ", "decision『d』: 崩れ", "問い"]


def test_does_not_touch_statement_or_note(monkeypatch):
    """goalの文（statement・条件文・note）は展開対象に含まれない。"""
    calls = _spy_expand(monkeypatch)
    goal_block = {
        "label": "active",
        "statement": "終わりの一文",
        "remaining": [{"id_raw": 1, "statement": "s1", "note": "メモ"}],
    }

    main_module._apply_flavor_to_goal_block(goal_block, "readable", conn=None)

    assert goal_block["statement"] == "終わりの一文"
    assert goal_block["remaining"][0]["statement"] == "s1"
    assert goal_block["remaining"][0]["note"] == "メモ"
    assert calls == []


def test_noop_when_goal_block_is_error_shape(monkeypatch):
    calls = _spy_expand(monkeypatch)
    goal_block = {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}

    main_module._apply_flavor_to_goal_block(goal_block, "readable", conn=None)

    assert calls == []
    assert goal_block == {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}


def test_noop_when_goal_block_is_not_a_dict(monkeypatch):
    calls = _spy_expand(monkeypatch)

    main_module._apply_flavor_to_goal_block(None, "readable", conn=None)
    main_module._apply_flavor_to_goal_block("undefined-like", "readable", conn=None)

    assert calls == []
