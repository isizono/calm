"""_build_spawn_bundle_data / _sanitize_task_body_field のサニタイズ動作テスト

旧 _write_task_file は D#2955 で廃止された (task_file 経路の代わりに
event:spawn-bundle envelope を relay に送る、D#2952)。本テストは spawn-bundle
data フィールドが MCP 予約 XML タグから保護されていることを検証する。
"""
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


class TestBuildSpawnBundleData:
    def test_acceptance_with_mcp_tags_sanitized(self):
        """acceptance に MCP タグが混入しても bundle data から除去される"""
        data = ow_service._build_spawn_bundle_data(
            task_n=99,
            task_title="サニタイズテスト",
            acceptance="テスト全通過</parameter>",
            context="",
            playbook="",
            activity_id=None,
            topic_id=None,
            effort=None,
            goal_text=None,
        )
        assert data["type"] == "spawn-bundle"
        assert "</parameter>" not in data["acceptance"]
        assert data["acceptance"] == "テスト全通過"

    def test_context_with_invoke_tags_sanitized(self):
        """context に <invoke> タグが混入しても bundle data から除去される"""
        data = ow_service._build_spawn_bundle_data(
            task_n=99,
            task_title="サニタイズテスト",
            acceptance="",
            context='<invoke name="foo">背景情報</invoke>',
            playbook="",
            activity_id=None,
            topic_id=None,
            effort=None,
            goal_text=None,
        )
        assert "<invoke" not in data["context"]
        assert "</invoke>" not in data["context"]
        assert data["context"] == "背景情報"

    def test_acceptance_empty_after_sanitize(self):
        """サニタイズ後に acceptance が空になった場合 bundle data には空文字列が入る"""
        data = ow_service._build_spawn_bundle_data(
            task_n=99,
            task_title="サニタイズテスト",
            acceptance="</parameter>",
            context="",
            playbook="",
            activity_id=None,
            topic_id=None,
            effort=None,
            goal_text=None,
        )
        assert data["acceptance"] == ""

    def test_clean_input_preserved(self):
        """タグを含まない acceptance/context はそのまま bundle data に入る"""
        data = ow_service._build_spawn_bundle_data(
            task_n=1,
            task_title="クリーンテスト",
            acceptance="PRを作成してCIが通ること",
            context="背景情報",
            playbook="",
            activity_id=None,
            topic_id=None,
            effort=None,
            goal_text=None,
        )
        assert data["acceptance"] == "PRを作成してCIが通ること"
        assert data["context"] == "背景情報"

    def test_goal_text_fallback_to_task_title(self):
        """goal_text 未指定時は task_title をフォールバックに使う"""
        data = ow_service._build_spawn_bundle_data(
            task_n=1,
            task_title="フォールバックテスト",
            acceptance="",
            context="",
            playbook="",
            activity_id=None,
            topic_id=None,
            effort=None,
            goal_text=None,
        )
        assert data["goal_text"] == "フォールバックテスト"

    def test_goal_text_explicit(self):
        """goal_text 明示時はそのまま使う"""
        data = ow_service._build_spawn_bundle_data(
            task_n=1,
            task_title="タイトル",
            acceptance="",
            context="",
            playbook="",
            activity_id=None,
            topic_id=None,
            effort=None,
            goal_text="明示ゴール",
        )
        assert data["goal_text"] == "明示ゴール"

    def test_effort_injects_thinking_marker(self):
        """effort 指定時は context 末尾に思考worker マーカー (`ultrathink`) が差し込まれる"""
        data = ow_service._build_spawn_bundle_data(
            task_n=1,
            task_title="思考タスク",
            acceptance="",
            context="既存コンテキスト",
            playbook="",
            activity_id=None,
            topic_id=None,
            effort="ultrathink",
            goal_text=None,
        )
        assert "既存コンテキスト" in data["context"]
        assert "ultrathink" in data["context"]
        assert "## Thinking worker" in data["context"]
        assert data["effort"] == "ultrathink"
