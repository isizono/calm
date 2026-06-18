"""add_activity の議論未経過バリデーション (IMPLEMENT_WORKFLOW_GUARD) のユニットテスト

intent:implement を含む add_activity 呼び出しが related に decision タイプ直接
1件以上（実在するdecision_id）を持たない場合に IMPLEMENT_WORKFLOW_GUARD で
弾かれることを検証する。canonical_id を辿ったエイリアス intent タグの判定や、
存在しない/取り消し済 decision_id によるバイパス試行のブロックもカバーする。

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
import pytest

from src.db import get_connection
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic
from src.services.retract_service import retract
from tests.helpers import add_decision


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding服を無効化する"""


@pytest.fixture
def topic_with_decision(temp_db):
    """テスト用にtopicとそこに紐づくdecisionを1件作成し、(topic_id, decision_id) を返す"""
    topic = add_topic(
        title="Topic for guard test",
        description="Topic with a decision attached.",
        tags=["domain:test"],
    )
    topic_id = topic["topic_id"]
    decision = add_decision(
        decision="Agreed approach for the implementation.",
        reason="Reviewed alternatives and picked this one.",
        topic_id=topic_id,
    )
    return topic_id, decision["decision_id"]


def _set_canonical(conn, alias_id, canonical_id):
    conn.execute(
        "UPDATE tags SET canonical_id = ? WHERE id = ?",
        (canonical_id, alias_id),
    )


def _create_tag(conn, namespace, name):
    conn.execute(
        "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
        (namespace, name),
    )
    row = conn.execute(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?",
        (namespace, name),
    ).fetchone()
    return row["id"]


class TestImplementWorkflowGuard:
    """IMPLEMENT_WORKFLOW_GUARD の通過・ブロック判定"""

    def test_no_intent_implement_passes(self, topic_with_decision):
        """intent:implement を含まない場合、related が無くても通過する"""
        result = add_activity(
            title="Discuss something",
            description="Just a discussion activity.",
            tags=["domain:test", "intent:discuss"],
            check_in=False,
        )
        assert "error" not in result
        assert "activity_id" in result

    def test_implement_without_related_blocks(self, topic_with_decision):
        """intent:implement かつ related なし → IMPLEMENT_WORKFLOW_GUARD で弾かれる"""
        result = add_activity(
            title="Naked implement",
            description="Tries to implement without any decision.",
            tags=["domain:test", "intent:implement"],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"
        assert "decision" in result["error"]["message"]

    def test_implement_with_only_topic_blocks(self, topic_with_decision):
        """intent:implement + related に topic のみ → 弾かれる（間接decision参照は廃止）"""
        topic_id, _ = topic_with_decision
        result = add_activity(
            title="Implement linked to topic only",
            description="topic 経由の間接decisionでは通過しない。",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "topic", "ids": [topic_id]}],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"

    def test_implement_with_decision_passes(self, topic_with_decision):
        """intent:implement + related に decision 1件（実在ID）→ 通過する"""
        _, decision_id = topic_with_decision
        result = add_activity(
            title="Implement with agreement",
            description="Has a decision in related.",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        assert "error" not in result
        assert "activity_id" in result

    def test_implement_with_topic_and_decision_passes(self, topic_with_decision):
        """intent:implement + related に topic + decision 複数タイプ → 通過する"""
        topic_id, decision_id = topic_with_decision
        result = add_activity(
            title="Implement with both",
            description="Mix of topic and decision in related.",
            tags=["domain:test", "intent:implement"],
            related=[
                {"type": "topic", "ids": [topic_id]},
                {"type": "decision", "ids": [decision_id]},
            ],
            check_in=False,
        )
        assert "error" not in result
        assert "activity_id" in result

    def test_multiple_intent_tags_with_implement_blocks(self, topic_with_decision):
        """複数 intent tag のうち intent:implement が含まれる → 判定対象でブロックされる"""
        result = add_activity(
            title="Mixed intents implement",
            description="Both review and implement intents, no decision related.",
            tags=["domain:test", "intent:review", "intent:implement"],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"

    def test_multiple_intent_tags_without_implement_passes(self, topic_with_decision):
        """複数 intent tag に intent:implement が無ければ通過する"""
        result = add_activity(
            title="Design + discuss",
            description="No implement intent here.",
            tags=["domain:test", "intent:design", "intent:discuss"],
            check_in=False,
        )
        assert "error" not in result

    def test_aliased_intent_resolves_to_implement_and_blocks(self, topic_with_decision):
        """エイリアス intent タグ (canonical_id 経由) も intent:implement として判定される"""
        conn = get_connection()
        try:
            canonical_id = _create_tag(conn, "intent", "implement")
            alias_id = _create_tag(conn, "intent", "impl")
            _set_canonical(conn, alias_id, canonical_id)
            conn.commit()
        finally:
            conn.close()

        result = add_activity(
            title="Implement via alias",
            description="Uses intent:impl which aliases to intent:implement.",
            tags=["domain:test", "intent:impl"],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"

    def test_aliased_intent_passes_with_decision(self, topic_with_decision):
        """エイリアス intent タグでも related に decision があれば通過する"""
        _, decision_id = topic_with_decision
        conn = get_connection()
        try:
            canonical_id = _create_tag(conn, "intent", "implement")
            alias_id = _create_tag(conn, "intent", "impl")
            _set_canonical(conn, alias_id, canonical_id)
            conn.commit()
        finally:
            conn.close()

        result = add_activity(
            title="Implement via alias with decision",
            description="Alias + decision relate → passes.",
            tags=["domain:test", "intent:impl"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        assert "error" not in result

    def test_empty_related_treated_same_as_none(self, topic_with_decision):
        """related=[] は related なしと同じ扱いで弾かれる"""
        result = add_activity(
            title="Implement empty related",
            description="Empty list still blocks.",
            tags=["domain:test", "intent:implement"],
            related=[],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"

    def test_nonexistent_decision_id_blocks(self, topic_with_decision):
        """存在しない decision_id では通過させない（バイパス試行のブロック）"""
        # 99999 のような未存在IDを並べてもガード通過しない。
        # _validate_targets は構造のみ検証、relations テーブルは FK 制約を
        # 持たないため、存在チェックをガード側で行う必要がある。
        result = add_activity(
            title="Bypass attempt with fake id",
            description="Non-existent decision ID should not pass.",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [99999]}],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"

    def test_retracted_decision_blocks(self, topic_with_decision):
        """retract済 decision は通過の根拠にしない"""
        _, decision_id = topic_with_decision
        # 取り消し
        retract_result = retract("decision", [decision_id])
        assert "error" not in retract_result

        result = add_activity(
            title="Implement with retracted decision",
            description="Retracted decisions should not pass the guard.",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        assert "error" in result
        assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"

    def test_decision_with_empty_ids_blocks(self, topic_with_decision):
        """related に decision タイプはあるが ids が空 → ガード単体で弾く

        公開 API (add_activity) 経由だと _validate_targets が空idsを先に
        VALIDATION_ERROR で弾くため、このシナリオは公開APIから直接は起きない。
        ガード単体の仕様としての保証（直接呼び出しでも誤って通過しないこと）を
        ここで明示する。
        """
        from src.services.activity_service import _check_implement_workflow_guard

        conn = get_connection()
        try:
            err = _check_implement_workflow_guard(
                conn,
                [("intent", "implement"), ("domain", "test")],
                [{"type": "decision", "ids": []}],
            )
        finally:
            conn.close()
        assert err is not None
        assert err["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"
