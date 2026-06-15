"""_write_task_file / _sanitize_task_body_field のサニタイズ動作テスト"""
from pathlib import Path

import pytest

from src.services import ow_service


class TestSanitizeTaskBodyField:
    def test_clean_input_unchanged(self):
        """タグを含まない入力はそのまま返す"""
        value = "完了条件: テスト全通過 + PR作成"
        result = ow_service._sanitize_task_body_field(value, "acceptance")
        assert result == value

    def test_removes_closing_parameter_tag(self):
        """末尾に混入した </parameter> タグを除去する"""
        value = "テストが全通過すること</parameter>"
        result = ow_service._sanitize_task_body_field(value, "acceptance")
        assert result == "テストが全通過すること"
        assert "</parameter>" not in result

    def test_removes_opening_parameter_tag(self):
        """開始タグ <parameter name="..."> を除去する"""
        value = '<parameter name="acceptance">完了条件'
        result = ow_service._sanitize_task_body_field(value, "acceptance")
        assert result == "完了条件"

    def test_removes_invoke_tags(self):
        """<invoke> / </invoke> タグを除去する"""
        value = '<invoke name="ow_spawn_worker">残骸</invoke>'
        result = ow_service._sanitize_task_body_field(value, "context")
        assert result == "残骸"
        assert "<invoke" not in result
        assert "</invoke>" not in result

    def test_removes_function_calls_tags(self):
        """<function_calls> / </function_calls> タグを除去する"""
        value = "<function_calls>本文</function_calls>"
        result = ow_service._sanitize_task_body_field(value)
        assert result == "本文"

    def test_removes_antml_prefix_tags(self):
        """antml:プレフィックス付きタグも除去する"""
        value = "<invoke>残骸</invoke>"
        result = ow_service._sanitize_task_body_field(value, "acceptance")
        assert result == "残骸"

    def test_removes_tool_result_tags(self):
        """<tool_result> タグを除去する"""
        value = "<tool_result>出力</tool_result>"
        result = ow_service._sanitize_task_body_field(value, "context")
        assert result == "出力"

    def test_removes_multiple_tags(self):
        """複数のMCP予約タグが混在している場合も全て除去する"""
        value = '</parameter></invoke><function_calls>本文</function_calls>'
        result = ow_service._sanitize_task_body_field(value, "acceptance")
        assert result == "本文"
        assert "<" not in result

    def test_case_insensitive(self):
        """大文字小文字を区別しない"""
        value = "</PARAMETER></INVOKE>"
        result = ow_service._sanitize_task_body_field(value, "acceptance")
        assert result.strip() == ""

    def test_non_mcp_xml_preserved(self):
        """MCP予約タグ以外のXMLタグはそのまま残す"""
        value = "出力形式: <json>{'key': 'value'}</json>"
        result = ow_service._sanitize_task_body_field(value, "context")
        assert "<json>" in result

    def test_empty_string(self):
        """空文字列は空文字列を返す"""
        assert ow_service._sanitize_task_body_field("", "acceptance") == ""


class TestWriteTaskFileSanitize:
    def test_acceptance_with_mcp_tags_written_clean(self, tmp_path: Path):
        """acceptanceにMCPタグが混入してもtask_fileのAcceptanceセクションにタグが残らない"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path,
            task_n=99,
            alias="w-test",
            channel="testchan",
            cwd="/tmp",
            model="claude-sonnet-4-6",

            task_title="サニタイズテスト",
            acceptance="テスト全通過</parameter>",
            context="",
            playbook="",
            timeout_min=30,
            activity_id=None,
            topic_id=None,
        )
        content = task_file.read_text(encoding="utf-8")
        assert "</parameter>" not in content
        assert "テスト全通過" in content
        assert "## Acceptance" in content

    def test_context_with_invoke_tags_written_clean(self, tmp_path: Path):
        """contextに<invoke>タグが混入してもtask_fileのContextセクションにタグが残らない"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path,
            task_n=99,
            alias="w-test",
            channel="testchan",
            cwd="/tmp",
            model="claude-sonnet-4-6",

            task_title="サニタイズテスト",
            acceptance="",
            context='<invoke name="foo">背景情報</invoke>',
            playbook="",
            timeout_min=30,
            activity_id=None,
            topic_id=None,
        )
        content = task_file.read_text(encoding="utf-8")
        assert "<invoke" not in content
        assert "</invoke>" not in content
        assert "背景情報" in content
        assert "## Context" in content

    def test_acceptance_empty_after_sanitize_omits_section(self, tmp_path: Path):
        """サニタイズ後にacceptanceが空になった場合はAcceptanceセクション自体を出力しない"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path,
            task_n=99,
            alias="w-test",
            channel="testchan",
            cwd="/tmp",
            model="claude-sonnet-4-6",

            task_title="サニタイズテスト",
            acceptance="</parameter>",
            context="",
            playbook="",
            timeout_min=30,
            activity_id=None,
            topic_id=None,
        )
        content = task_file.read_text(encoding="utf-8")
        assert "## Acceptance" not in content

    def test_clean_input_acceptance_preserved(self, tmp_path: Path):
        """タグを含まないacceptanceはそのまま書き出される"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path,
            task_n=1,
            alias="w-a",
            channel="chan",
            cwd="/tmp",
            model="claude-sonnet-4-6",

            task_title="クリーンテスト",
            acceptance="PRを作成してCIが通ること",
            context="背景情報",
            playbook="",
            timeout_min=60,
            activity_id=None,
            topic_id=None,
        )
        content = task_file.read_text(encoding="utf-8")
        assert "PRを作成してCIが通ること" in content
        assert "背景情報" in content
