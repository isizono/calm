"""src/services/vessel_rules.py のテスト。

地の文の計算、語彙の照合、人間の打鍵の判別（許可リスト）、本文の一致比較を
検証する。人間でない組の実データは preflight_origins.md の実測（26通り）から
到達済みの組み合わせだけを使う。
"""
from src.services.vessel_rules import (
    ALLOWED_HUMAN_PROMPT_SOURCES,
    compute_flag,
    is_human_speaker,
    plain_text,
    bodies_match,
    transcript_body,
)


class TestPlainText:
    def test_removes_quote_lines(self):
        text = "本文の1行目\n> 引用された行\n本文の2行目"
        assert plain_text(text) == "本文の1行目\n本文の2行目"

    def test_removes_fenced_code_block(self):
        text = "説明\n```\nコードの中身\n```\n続き"
        assert plain_text(text) == "説明\n続き"

    def test_unclosed_fence_drops_rest(self):
        text = "説明\n```\n閉じないコード\nさらに行"
        assert plain_text(text) == "説明"

    def test_indented_quote_line_is_removed(self):
        text = "  > インデントされた引用\n本文"
        assert plain_text(text) == "本文"


class TestVocabDetection:
    def test_strong_vocab_detected(self):
        assert compute_flag("前にも言ったよね、それ") == "strong"

    def test_weak_vocab_detected(self):
        assert compute_flag("それは違う気がする") == "weak"

    def test_strong_takes_precedence_over_weak(self):
        # 「前にも」（強い語）と「違う」（弱い語）を両方含む文
        assert compute_flag("前にも言ったけど、それは違う") == "strong"

    def test_no_vocab_hit_returns_none(self):
        assert compute_flag("ありがとう、それで進めて") is None

    def test_vocab_in_quoted_line_is_ignored(self):
        text = "> 前にも言ったよね\n了解です"
        assert compute_flag(text) is None

    def test_vocab_in_code_block_is_ignored(self):
        text = "```\n前にも言ったよね\n```\nこれは普通の文"
        assert compute_flag(text) is None


class TestHumanDiscriminationAllowlist:
    """許可リストの3つの組（typed/queued/sdk × human）で真、実測にある
    それ以外の組すべてで偽になることを確認する（preflight_origins.md 実測）。
    """

    def test_allowed_combos_are_human(self):
        for source in ("typed", "queued", "sdk"):
            assert is_human_speaker("human", source) is True

    def test_allowed_prompt_sources_constant_matches_spec(self):
        assert ALLOWED_HUMAN_PROMPT_SOURCES == frozenset({"typed", "queued", "sdk"})

    def test_slash_command_turn_is_not_human(self):
        # turnOriginはhumanだがpromptSourceキー自体が欠落する（スラッシュコマンド展開）
        assert is_human_speaker("human", None) is False

    def test_missing_turn_origin_is_not_human(self):
        assert is_human_speaker(None, "typed") is False

    def test_task_notification_is_not_human(self):
        assert is_human_speaker("task_notification", "system") is False

    def test_peer_message_is_not_human(self):
        assert is_human_speaker("peer", "system") is False

    def test_sdk_turn_origin_is_not_human(self):
        # promptSource='sdk' かつ turnOrigin='sdk'（人間でないRemote Control経路）
        assert is_human_speaker("sdk", "sdk") is False

    def test_system_turn_is_not_human(self):
        assert is_human_speaker("system", "system") is False

    def test_unknown_prompt_source_is_not_human(self):
        # 一覧に無い値は許可リスト方式で常に人間でない扱い
        assert is_human_speaker("human", "unknown-source") is False


class TestBodyComparison:
    def test_transcript_body_from_string(self):
        assert transcript_body("plain string") == "plain string"

    def test_transcript_body_joins_text_blocks_only(self):
        content = [
            {"type": "text", "text": "hello "},
            {"type": "image", "source": {}},
            {"type": "text", "text": "world"},
        ]
        assert transcript_body(content) == "hello world"

    def test_transcript_body_unknown_shape_returns_empty(self):
        assert transcript_body(12345) == ""

    def test_bodies_match_ignores_surrounding_whitespace(self):
        assert bodies_match("  hello world  ", "hello world") is True

    def test_bodies_match_collapses_internal_whitespace_and_case(self):
        assert bodies_match("Hello   World", "hello world") is True

    def test_bodies_match_false_for_different_text(self):
        assert bodies_match("hello world", "goodbye world") is False

    def test_bodies_match_with_list_content(self):
        content = [{"type": "text", "text": "hello world"}]
        assert bodies_match("Hello World", content) is True
