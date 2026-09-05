"""propagate_to オプションのテスト

decision保存と同時にhabit/tag-noteへの伝搬を検証する。
"""
import os
import tempfile
import pytest
from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.decision_service import add_decisions
from src.services.tag_service import _injected_tags
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic(temp_db):
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


class TestPropagateToHabit:
    """decision + habit伝搬のテスト"""

    def test_propagate_to_habit_success(self, topic):
        """decision + habit伝搬 → propagation.status=="ok", habit_id取得可能"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "常に簡潔に応答する",
                "reason": "冗長な応答を避けるため",
                "propagate_to": {
                    "type": "habit",
                    "content": "応答は簡潔にすること",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 0

        created = result["created"][0]
        assert created["decision_id"] > 0
        assert "propagation" in created
        assert created["propagation"]["status"] == "ok"
        assert created["propagation"]["type"] == "habit"
        assert created["propagation"]["id"] > 0

        # habitがDBに実際に存在することを確認
        habit_id = created["propagation"]["id"]
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT content, active FROM habits WHERE id = ?",
                (habit_id,),
            ).fetchone()
            assert row is not None
            assert row["content"] == "応答は簡潔にすること"
            assert row["active"] == 1
        finally:
            conn.close()


class TestPropagateToTagNote:
    """decision + tag-note伝搬のテスト"""

    def test_propagate_to_tag_note_with_existing_notes(self, topic):
        """既存notes有のタグにappend → notes末尾に追記されている"""
        tid = topic["topic_id"]

        # 既存notesを持つタグを作成
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "myproject", "既存のメモ"),
            )
            conn.commit()
        finally:
            conn.close()

        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "命名規則をcamelCaseに統一する",
                "reason": "チーム間の一貫性のため",
                "propagate_to": {
                    "type": "tag_note",
                    "tag": "domain:myproject",
                    "content": "命名規則: camelCaseを使用すること",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1

        created = result["created"][0]
        assert "propagation" in created
        assert created["propagation"]["status"] == "ok"
        assert created["propagation"]["type"] == "tag_note"

        # notesが追記されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = ? AND name = ?",
                ("domain", "myproject"),
            ).fetchone()
            assert row is not None
            assert row["notes"] == "既存のメモ\n\n命名規則: camelCaseを使用すること"
        finally:
            conn.close()

    def test_propagate_to_tag_note_without_existing_notes(self, topic):
        """既存notesなしのタグ → contentがそのままnotesにSET"""
        tid = topic["topic_id"]

        # notesなしのタグを作成
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                ("domain", "newproject"),
            )
            conn.commit()
        finally:
            conn.close()

        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "テストカバレッジ80%以上を維持する",
                "reason": "品質保証のため",
                "propagate_to": {
                    "type": "tag_note",
                    "tag": "domain:newproject",
                    "content": "テストカバレッジ80%以上を維持すること",
                },
            },
        ])

        assert "error" not in result
        created = result["created"][0]
        assert created["propagation"]["status"] == "ok"

        # notesがそのままSETされていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = ? AND name = ?",
                ("domain", "newproject"),
            ).fetchone()
            assert row is not None
            assert row["notes"] == "テストカバレッジ80%以上を維持すること"
        finally:
            conn.close()


class TestPropagateToErrors:
    """propagate_to エラーケースのテスト"""

    def test_nonexistent_tag_error_decision_remains(self, topic):
        """存在しないタグ → propagation.status=="error", decisionは残る"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "存在しないタグへの伝搬テスト",
                "reason": "エラー処理のテスト",
                "propagate_to": {
                    "type": "tag_note",
                    "tag": "domain:nonexistent",
                    "content": "伝搬失敗するはず",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 0

        created = result["created"][0]
        assert created["decision_id"] > 0
        assert created["propagation"]["status"] == "error"
        assert created["propagation"]["type"] == "tag_note"
        assert "not found" in created["propagation"]["message"].lower()

        # decisionがDBに残っていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?",
                (created["decision_id"],),
            ).fetchone()
            assert row is not None
            assert row["decision"] == "存在しないタグへの伝搬テスト"
        finally:
            conn.close()

    def test_empty_content_error_decision_remains(self, topic):
        """空content → propagation.status=="error", decisionは残る"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "空contentでの伝搬テスト",
                "reason": "バリデーションテスト",
                "propagate_to": {
                    "type": "habit",
                    "content": "",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1

        created = result["created"][0]
        assert created["decision_id"] > 0
        assert created["propagation"]["status"] == "error"
        assert created["propagation"]["type"] == "habit"

        # decisionがDBに残っていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?",
                (created["decision_id"],),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_invalid_type_error_decision_remains(self, topic):
        """不正type → propagation.status=="error", decisionは残る"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "不正typeでの伝搬テスト",
                "reason": "バリデーションテスト",
                "propagate_to": {
                    "type": "invalid_type",
                    "content": "何かしらのコンテンツ",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1

        created = result["created"][0]
        assert created["decision_id"] > 0
        assert created["propagation"]["status"] == "error"
        assert created["propagation"]["type"] == "invalid_type"
        assert "invalid" in created["propagation"]["message"].lower()

        # decisionがDBに残っていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?",
                (created["decision_id"],),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_missing_type_key_error_decision_remains(self, topic):
        """typeキー省略 → type=Noneでpropagation.status=="error", decisionは残る"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "typeキー省略での伝搬テスト",
                "reason": "バリデーションテスト",
                "propagate_to": {
                    "content": "何かしらのコンテンツ",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1

        created = result["created"][0]
        assert created["decision_id"] > 0
        assert created["propagation"]["status"] == "error"
        assert "Invalid propagate_to.type: None" in created["propagation"]["message"]

        # decisionがDBに残っていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?",
                (created["decision_id"],),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


class TestPropagationFailedTopLevel:
    """propagate_to失敗時、応答トップレベルのpropagation_failedで可視化されることのテスト"""

    def test_tag_note_ceiling_exceeded_surfaces_top_level_and_decision_still_created(self, topic):
        """タグのnotesが文字数上限に達している状態でtag_note伝搬 →
        トップレベルpropagation_failedに載り、かつdecision自体は正常に作成される"""
        tid = topic["topic_id"]

        # notesを上限4000字ちょうどまで埋めたタグを用意する（追記で必ず超過する状態）
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "atceiling", "x" * 4000),
            )
            conn.commit()
        finally:
            conn.close()

        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "上限到達タグへの伝搬テスト",
                "reason": "propagation_failed可視化のテスト",
                "propagate_to": {
                    "type": "tag_note",
                    "tag": "domain:atceiling",
                    "content": "この追記は上限超過で拒否されるはず",
                },
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 0

        created = result["created"][0]
        assert created["propagation"]["status"] == "error"

        # トップレベルに失敗が明示される
        assert "propagation_failed" in result
        assert len(result["propagation_failed"]) == 1
        failure = result["propagation_failed"][0]
        assert failure["index"] == 0
        assert failure["decision_id"] == created["decision_id"]
        assert failure["type"] == "tag_note"
        assert failure["tag"] == "domain:atceiling"
        assert "ratchet ceiling" in failure["message"].lower()

        # decision自体は正常に作成されている
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?",
                (created["decision_id"],),
            ).fetchone()
            assert row is not None
            assert row["decision"] == "上限到達タグへの伝搬テスト"

            # notesは上限超過の追記が拒否されロールバックされ、元のままである
            notes_row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = ? AND name = ?",
                ("domain", "atceiling"),
            ).fetchone()
            assert notes_row["notes"] == "x" * 4000
        finally:
            conn.close()

    def test_successful_propagation_has_no_top_level_failure_field(self, topic):
        """伝搬が全件成功 → propagation_failedキー自体が付かない"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "成功する伝搬テスト",
                "reason": "propagation_failed非付与の確認",
                "propagate_to": {
                    "type": "habit",
                    "content": "正常に伝搬される内容",
                },
            },
        ])

        assert "error" not in result
        assert result["created"][0]["propagation"]["status"] == "ok"
        assert "propagation_failed" not in result


class TestPropagateToAbsent:
    """propagate_to未指定時のテスト"""

    def test_no_propagate_to_decision_only(self, topic):
        """propagate_toなし → decisionのみ保存され、propagationフィールドは含まれない"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "通常の決定事項",
                "reason": "通常の理由",
            },
        ])

        assert "error" not in result
        assert len(result["created"]) == 1

        created = result["created"][0]
        assert created["decision_id"] > 0
        assert "propagation" not in created
