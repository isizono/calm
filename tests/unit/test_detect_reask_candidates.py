"""scripts/detect_reask_candidates.py のテスト。

transcript JSONLからのAskUserQuestion呼び出し・ユーザー訂正発話の抽出正確性、
除外辞書によるexcluded_reason付与、dedup、不正行への堅牢性を検証する。
"""
import json

import pytest

from scripts.detect_reask_candidates import (
    DEFAULT_CORRECTION_PATTERNS,
    DEFAULT_EXCLUSION_RULES,
    _extract_option_labels,
    _load_dict,
    _match_exclusion,
    _normalize_text,
    extract_candidates,
    format_candidates_jsonl,
    main,
)


# --- ヘルパー ---


def _assistant_ask(questions: list[dict], preceding_text: str | None = None) -> dict:
    content = []
    if preceding_text is not None:
        content.append({"type": "text", "text": preceding_text})
    content.append(
        {
            "type": "tool_use",
            "name": "AskUserQuestion",
            "input": {"questions": questions},
        }
    )
    return {"type": "assistant", "message": {"content": content}}


def _user_text(text: str, is_meta: bool = False) -> dict:
    return {
        "type": "user",
        "isMeta": is_meta,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _user_tool_result_and_text(result_text: str, text: str) -> dict:
    return {
        "type": "user",
        "isMeta": False,
        "message": {
            "content": [
                {"type": "tool_result", "content": result_text},
                {"type": "text", "text": text},
            ]
        },
    }


@pytest.fixture
def make_transcript(tmp_path):
    def _make(entries: list[dict], name: str = "transcript.jsonl") -> str:
        path = tmp_path / name
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return str(path)

    return _make


def _write_raw(tmp_path, lines: list[str], name: str = "transcript.jsonl") -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# --- 設計書 §6 fixture 1〜5 ---


class TestFixture1AskOnly:
    def test_three_ask_questions_all_extracted(self, make_transcript):
        entries = [
            _assistant_ask([{"question": "Q1?"}]),
            _assistant_ask([{"question": "Q2?"}]),
            _assistant_ask([{"question": "Q3?"}]),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert len(candidates) == 3
        assert all(c["kind"] == "ask" for c in candidates)
        assert {c["text"] for c in candidates} == {"Q1?", "Q2?", "Q3?"}


class TestFixture2CorrectionOnly:
    def test_three_correction_utterances_all_extracted(self, make_transcript):
        entries = [
            _user_text("これ前に決めなかったっけ?"),
            _user_text("また同じ話してる気がする"),
            _user_text("グルグル回ってない?"),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert len(candidates) == 3
        assert all(c["kind"] == "user_correction" for c in candidates)

    def test_tool_result_only_user_entry_is_excluded(self, make_transcript):
        entry = _user_tool_result_and_text("結果テキスト", "これ前に決めなかったっけ?")
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates == []

    def test_is_meta_true_user_entry_is_excluded(self, make_transcript):
        entry = _user_text("これ前に決めなかったっけ?", is_meta=True)
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates == []

    def test_non_matching_user_text_is_not_a_candidate(self, make_transcript):
        entry = _user_text("了解です、進めてください")
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates == []


class TestFixture3ExclusionCategories:
    def test_opinion_preference_and_normal_questions(self, make_transcript):
        entries = [
            _assistant_ask([{"question": "A案とB案どっちがいい?"}]),
            _assistant_ask([{"question": "今日どこまでやりたい?"}]),
            _assistant_ask([{"question": "この関数をrenameしてもよいですか"}]),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert len(candidates) == 3
        by_text = {c["text"]: c for c in candidates}
        assert by_text["A案とB案どっちがいい?"]["excluded_reason"] == "opinion_request"
        assert by_text["今日どこまでやりたい?"]["excluded_reason"] == "user_preference_request"
        assert "excluded_reason" not in by_text["この関数をrenameしてもよいですか"]

    def test_environment_fact_question_excluded(self, make_transcript):
        entry = _assistant_ask([{"question": "CIは通っていますか"}])
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates[0]["excluded_reason"] == "environment_fact"

    def test_excluded_candidate_is_not_dropped_from_output(self, make_transcript):
        """除外は候補からは消さない（後段のスキップ判断に委ねる）。"""
        entry = _assistant_ask([{"question": "A案とB案どっちがいい?"}])
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert len(candidates) == 1


class TestFixture4EmptyTranscript:
    def test_empty_file_returns_no_candidates(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        candidates = extract_candidates(str(path))

        assert candidates == []

    def test_only_assistant_text_and_neutral_user_turns_returns_no_candidates(self, make_transcript):
        entries = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "了解しました"}]}},
            _user_text("ありがとう"),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert candidates == []


class TestFixture5LargeTranscript:
    def test_max_cap_limits_output_count(self, make_transcript):
        entries = [_assistant_ask([{"question": f"Q{i}?"}]) for i in range(500)]
        path = make_transcript(entries)
        candidates = extract_candidates(path, max_candidates=50)

        assert len(candidates) == 50

    def test_stops_reading_once_max_reached_skips_trailing_malformed_lines(self, tmp_path):
        """maxに達した後段の不正行に到達しても例外を出さず完走できる（早期打ち切り）。"""
        lines = [json.dumps(_assistant_ask([{"question": f"Q{i}?"}]), ensure_ascii=False) for i in range(5)]
        lines.append("{this line is not valid json at all")
        path = _write_raw(tmp_path, lines)

        candidates = extract_candidates(path, max_candidates=3)

        assert len(candidates) == 3

    def test_large_number_of_entries_completes(self, make_transcript):
        entries = [_assistant_ask([{"question": f"Q{i}?"}]) for i in range(5000)]
        path = make_transcript(entries)
        candidates = extract_candidates(path, max_candidates=5000)

        assert len(candidates) == 5000


# --- 設計書 §5 Edge cases / エッジケース表 #6, #10 ---


class TestDedup:
    def test_whitespace_and_case_only_diff_collapses_to_one(self, make_transcript):
        entries = [
            _assistant_ask([{"question": "  Fix the Bug  "}]),
            _assistant_ask([{"question": "fix the bug"}]),
            _assistant_ask([{"question": "別の質問"}]),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert len(candidates) == 2

    def test_different_kind_same_text_not_collapsed(self, make_transcript):
        entries = [
            _assistant_ask([{"question": "これ前に決めなかったっけ?"}]),
            _user_text("これ前に決めなかったっけ?"),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert len(candidates) == 2
        assert {c["kind"] for c in candidates} == {"ask", "user_correction"}

    def test_fullwidth_question_mark_variant_is_not_deduped(self, make_transcript):
        """正規化は空白畳み込み+小文字化のみ。全角/半角ゆれの吸収はしない（意図的な仕様）。"""
        entries = [
            _assistant_ask([{"question": "同じ質問ですか?"}]),
            _assistant_ask([{"question": "同じ質問ですか？"}]),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert len(candidates) == 2


class TestMalformedLines:
    def test_malformed_line_between_valid_entries_is_skipped(self, tmp_path):
        lines = [
            json.dumps(_assistant_ask([{"question": "Q1?"}]), ensure_ascii=False),
            "{not valid json",
            json.dumps(_assistant_ask([{"question": "Q2?"}]), ensure_ascii=False),
        ]
        path = _write_raw(tmp_path, lines)
        candidates = extract_candidates(path)

        assert len(candidates) == 2
        assert {c["text"] for c in candidates} == {"Q1?", "Q2?"}

    def test_blank_lines_are_skipped(self, tmp_path):
        lines = [
            json.dumps(_assistant_ask([{"question": "Q1?"}]), ensure_ascii=False),
            "",
            "   ",
            json.dumps(_assistant_ask([{"question": "Q2?"}]), ensure_ascii=False),
        ]
        path = _write_raw(tmp_path, lines)
        candidates = extract_candidates(path)

        assert len(candidates) == 2


# --- 網羅性の指摘: 複数tool_use / 複数question / question欠落 / turn / context_snippet ---


class TestMultipleToolUseBlocks:
    def test_only_ask_user_question_block_extracted_among_others(self, make_transcript):
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__plugin_x_y__search", "input": {"keyword": "abc"}},
                    {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "本当に実行しますか"}]},
                    },
                ]
            },
        }
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert len(candidates) == 1
        assert candidates[0]["text"] == "本当に実行しますか"


class TestMultiQuestionArray:
    def test_multiple_questions_in_one_call_expand_to_separate_candidates(self, make_transcript):
        entry = _assistant_ask(
            [
                {"question": "Q1?", "options": [{"label": "A"}, {"label": "B"}]},
                {"question": "Q2?"},
            ]
        )
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert len(candidates) == 2
        assert candidates[0]["turn"] == candidates[1]["turn"]
        assert candidates[0]["options"] == ["A", "B"]
        assert "options" not in candidates[1]

    def test_question_missing_question_key_is_skipped(self, make_transcript):
        entry = _assistant_ask(
            [
                {"options": [{"label": "A"}]},
                {"question": "Q2?"},
            ]
        )
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert len(candidates) == 1
        assert candidates[0]["text"] == "Q2?"

    def test_options_as_plain_string_list_also_supported(self, make_transcript):
        entry = _assistant_ask([{"question": "Q1?", "options": ["A", "B"]}])
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates[0]["options"] == ["A", "B"]

    def test_no_options_key_omits_options_field(self, make_transcript):
        entry = _assistant_ask([{"question": "Q1?"}])
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert "options" not in candidates[0]


class TestContextSnippet:
    def test_truncated_to_200_chars_from_tail(self, make_transcript):
        long_text = "あ" * 300
        entry = _assistant_ask([{"question": "Q?"}], preceding_text=long_text)
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert len(candidates[0]["context_snippet"]) == 200
        assert candidates[0]["context_snippet"] == long_text[-200:]

    def test_no_preceding_text_yields_empty_snippet(self, make_transcript):
        entry = _assistant_ask([{"question": "Q?"}])
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates[0]["context_snippet"] == ""

    def test_user_correction_snippet_is_full_utterance(self, make_transcript):
        entry = _user_text("これ前に決めなかったっけ?")
        path = make_transcript([entry])
        candidates = extract_candidates(path)

        assert candidates[0]["context_snippet"] == "これ前に決めなかったっけ?"


class TestTurnNumbering:
    def test_turn_increments_per_parsed_entry_regardless_of_type(self, make_transcript):
        entries = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            _user_text("了解"),
            _assistant_ask([{"question": "Q?"}]),
        ]
        path = make_transcript(entries)
        candidates = extract_candidates(path)

        assert candidates[0]["turn"] == 2

    def test_turn_does_not_increment_for_malformed_lines(self, tmp_path):
        lines = [
            "{not valid json",
            json.dumps(_assistant_ask([{"question": "Q?"}]), ensure_ascii=False),
        ]
        path = _write_raw(tmp_path, lines)
        candidates = extract_candidates(path)

        assert candidates[0]["turn"] == 0


# --- 内部ヘルパーの単体テスト ---


class TestNormalizeText:
    def test_collapses_whitespace_and_lowercases(self):
        assert _normalize_text("  Fix   the  Bug  ") == "fix the bug"


class TestMatchExclusion:
    def test_returns_first_matching_category_in_definition_order(self):
        assert _match_exclusion("A案とB案どっちがいい?", DEFAULT_EXCLUSION_RULES) == "opinion_request"
        assert _match_exclusion("今日どこまでやる?", DEFAULT_EXCLUSION_RULES) == "user_preference_request"
        assert _match_exclusion("CIが落ちてる", DEFAULT_EXCLUSION_RULES) == "environment_fact"

    def test_returns_none_when_no_pattern_matches(self):
        assert _match_exclusion("この関数の名前をrenameしてもよいですか", DEFAULT_EXCLUSION_RULES) is None


class TestExtractOptionLabels:
    def test_dict_shaped_options_extract_label(self):
        assert _extract_option_labels([{"label": "A", "description": "desc"}, {"label": "B"}]) == ["A", "B"]

    def test_string_shaped_options_pass_through(self):
        assert _extract_option_labels(["A", "B"]) == ["A", "B"]

    def test_empty_or_missing_returns_none(self):
        assert _extract_option_labels([]) is None
        assert _extract_option_labels(None) is None
        assert _extract_option_labels("not a list") is None


class TestCorrectionDictDefaults:
    def test_all_audit_tb_phrases_match(self):
        phrases = [
            "これ前に決めなかったっけ?",
            "また同じ話してる",
            "またこの話か",
            "過去の情報と矛盾してない?",
            "自分の知っている情報と矛盾してない?",
            "グルグル回ってない?",
            "ちゃんと過去の議論を踏まえてる?",
        ]
        for phrase in phrases:
            assert any(p.search(phrase) for p in DEFAULT_CORRECTION_PATTERNS), phrase


# --- CLI ---


class TestFormatCandidatesJsonl:
    def test_one_line_per_candidate(self):
        candidates = [{"kind": "ask", "turn": 0, "text": "Q1"}, {"kind": "ask", "turn": 1, "text": "Q2"}]
        output = format_candidates_jsonl(candidates)
        lines = output.splitlines()

        assert len(lines) == 2
        assert json.loads(lines[0])["text"] == "Q1"

    def test_empty_list_yields_empty_string(self):
        assert format_candidates_jsonl([]) == ""


class TestCliMain:
    def test_writes_to_out_file(self, make_transcript, tmp_path):
        path = make_transcript([_assistant_ask([{"question": "Q1?"}])])
        out_path = tmp_path / "out.jsonl"

        exit_code = main(["--transcript", path, "--out", str(out_path)])

        assert exit_code == 0
        lines = out_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "Q1?"

    def test_prints_to_stdout_when_out_omitted(self, make_transcript, capsys):
        path = make_transcript([_assistant_ask([{"question": "Q1?"}])])

        exit_code = main(["--transcript", path])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip())["text"] == "Q1?"

    def test_empty_transcript_prints_nothing(self, tmp_path, capsys):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")

        exit_code = main(["--transcript", str(path)])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_max_flag_overrides_default(self, make_transcript):
        entries = [_assistant_ask([{"question": f"Q{i}?"}]) for i in range(10)]
        path = make_transcript(entries)
        out_path = None

        candidates = extract_candidates(path, max_candidates=3)
        assert len(candidates) == 3

        exit_code = main(["--transcript", path, "--max", "3", "--out", path + ".out"])
        assert exit_code == 0


class TestLoadDict:
    def test_custom_correction_patterns_override_default(self, tmp_path):
        dict_path = tmp_path / "dict.json"
        dict_path.write_text(
            json.dumps({"correction_patterns": ["カスタム訂正パターン"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        correction_patterns, exclusion_rules = _load_dict(str(dict_path))

        assert any(p.search("カスタム訂正パターンです") for p in correction_patterns)
        assert exclusion_rules == DEFAULT_EXCLUSION_RULES

    def test_custom_exclusion_patterns_override_default(self, tmp_path):
        dict_path = tmp_path / "dict.json"
        dict_path.write_text(
            json.dumps({"exclusion_patterns": {"custom_category": ["カスタム除外語"]}}, ensure_ascii=False),
            encoding="utf-8",
        )

        correction_patterns, exclusion_rules = _load_dict(str(dict_path))

        assert correction_patterns == DEFAULT_CORRECTION_PATTERNS
        assert _match_exclusion("カスタム除外語を含む質問", exclusion_rules) == "custom_category"

    def test_dict_flag_wires_into_main(self, make_transcript, tmp_path):
        dict_path = tmp_path / "dict.json"
        dict_path.write_text(
            json.dumps({"exclusion_patterns": {"custom": ["特別なキーワード"]}}, ensure_ascii=False),
            encoding="utf-8",
        )
        transcript_path = make_transcript([_assistant_ask([{"question": "特別なキーワードを含む質問?"}])])
        out_path = tmp_path / "out.jsonl"

        exit_code = main(
            ["--transcript", transcript_path, "--dict", str(dict_path), "--out", str(out_path)]
        )

        assert exit_code == 0
        result = json.loads(out_path.read_text(encoding="utf-8").splitlines()[0])
        assert result["excluded_reason"] == "custom"
