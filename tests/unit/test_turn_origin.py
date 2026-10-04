"""hooks/turn_origin.py のユニットテスト

is_nonhuman_turnが、人間の発話でないターン（SAの報告中継・他セッションからの
メッセージ・バックグラウンドタスク通知・システム通知）をマーカー文字列の
有無で判定することを検証する。
"""
from hooks.turn_origin import is_nonhuman_turn


def test_plain_human_prompt_is_not_nonhuman():
    assert is_nonhuman_turn("これは普通の質問です") is False


def test_cross_session_relay_preamble_is_nonhuman():
    prompt = (
        'Another Claude session sent a message:\n'
        '<cross-session-message from="abc">hello</cross-session-message>'
    )
    assert is_nonhuman_turn(prompt) is True


def test_agent_message_hand_back_is_nonhuman():
    """マーカーが先頭ではなく前置きの後に来る形でも検出できる
    （先頭一致の正規表現では検出できなかったケース）。"""
    prompt = (
        'Another Claude session sent a message:\n'
        '<agent-message from="abc">[Subagent hand-back] report</agent-message>'
    )
    assert is_nonhuman_turn(prompt) is True


def test_task_notification_tag_is_nonhuman():
    prompt = "<task-notification>\n<status>completed</status>\n</task-notification>"
    assert is_nonhuman_turn(prompt) is True


def test_system_notification_bracket_is_nonhuman():
    prompt = "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n...\n</system-reminder>"
    assert is_nonhuman_turn(prompt) is True


def test_marker_mentioned_mid_sentence_is_still_detected():
    """文頭一致ではなく文中のどこにあっても検出する（前置き後にタグが来る形の対策）。"""
    prompt = "さっきの話の続きだけど、<task-notification>が来た件について"
    assert is_nonhuman_turn(prompt) is True


def test_none_prompt_is_not_nonhuman():
    assert is_nonhuman_turn(None) is False


def test_empty_string_prompt_is_not_nonhuman():
    assert is_nonhuman_turn("") is False
