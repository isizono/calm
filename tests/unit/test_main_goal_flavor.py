"""main._apply_flavor_to_goal_blockの単体テスト。

check_inのgoalブロックに対するflavor展開の対象範囲（remaining/terminalの
bound文字列とopen_questionsのtitleだけを展開し、statement・条件文・note・
judge_note・waiver_reasonには触れない）を、実DB＋実際の{{cite:X#NNN}}テンプレで確認する。
"""
import pytest

import src.main as main_module
from src.db import get_connection
from src.services.activity_service import add_activity


def _activity(title: str) -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)[
        "activity_id"
    ]


def test_expands_remaining_and_terminal_bound_and_open_question_titles(temp_db):
    cited_id = _activity("根拠アクティビティ")
    conn = get_connection()
    try:
        bound_template = f"activity『d』: 崩れ {{{{cite:A#{cited_id}}}}}"
        title_template = f"問い {{{{cite:A#{cited_id}}}}}"
        goal_block = {
            "label": "judge_ready",
            "statement": "終わりの一文",
            "remaining": [{"id_raw": 1, "statement": "s1", "bound": bound_template}],
            "terminal": [
                {"id_raw": 1, "statement": "s1", "bound": bound_template},
                {"id_raw": 2, "statement": "s2", "note": "済ませた"},
            ],
            "open_questions": [{"type": "ask", "id_raw": 3, "title": title_template}],
        }

        main_module._apply_flavor_to_goal_block(goal_block, "readable", conn)

        assert "根拠アクティビティ" in goal_block["remaining"][0]["bound"]
        assert f"A#{cited_id}" not in goal_block["remaining"][0]["bound"]
        assert "根拠アクティビティ" in goal_block["terminal"][0]["bound"]
        assert f"A#{cited_id}" not in goal_block["terminal"][0]["bound"]
        assert "根拠アクティビティ" in goal_block["open_questions"][0]["title"]
        assert f"A#{cited_id}" not in goal_block["open_questions"][0]["title"]
    finally:
        conn.close()


def test_does_not_touch_statement_or_note(temp_db):
    """goalの文（statement・条件文・note）は展開対象に含まれない。"""
    cited_id = _activity("根拠アクティビティ")
    conn = get_connection()
    try:
        template = f"参照 {{{{cite:A#{cited_id}}}}}"
        goal_block = {
            "label": "active",
            "statement": template,
            "remaining": [{"id_raw": 1, "statement": template, "note": template}],
            "terminal": [{"id_raw": 2, "statement": template, "note": template}],
        }

        main_module._apply_flavor_to_goal_block(goal_block, "readable", conn)

        assert goal_block["statement"] == template
        assert goal_block["remaining"][0]["statement"] == template
        assert goal_block["remaining"][0]["note"] == template
        assert goal_block["terminal"][0]["statement"] == template
        assert goal_block["terminal"][0]["note"] == template
    finally:
        conn.close()


def test_noop_when_goal_block_is_error_shape():
    goal_block = {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}

    main_module._apply_flavor_to_goal_block(goal_block, "readable", conn=None)

    assert goal_block == {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}


@pytest.mark.parametrize("goal_block", [None, "undefined-like"])
def test_non_dict_goal_block_returns_without_touching_conn(goal_block):
    """dict以外のgoal_blockはno-opで返る（conn=Noneのため、展開に進めば例外になる）。"""
    assert main_module._apply_flavor_to_goal_block(goal_block, "readable", conn=None) is None
