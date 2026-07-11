"""archived タグの search 降格（_apply_archived_demotion）テスト

- _apply_archived_demotion 単体テスト
- search() 経由の統合テスト（全タグarchived時の降格、部分archivedの非降格、
  降格がoffset/limit切り出し前に効くことの確認）
"""
import os
import tempfile

import pytest

from src.config import ARCHIVED_DEMOTION_FACTOR
from src.db import init_database
from src.services import search_service
from src.services.search_service import _apply_archived_demotion
from src.services.tag_service import update_tag
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_decision
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _archive(tag: str, reason: str = "退役済み") -> None:
    result = update_tag(tag, archived=True, archived_reason=reason)
    assert "error" not in result


# ========================================
# _apply_archived_demotion 単体テスト
# ========================================


def test_demotion_all_tags_archived(temp_db):
    """全タグがarchivedのアイテムはfinal_scoreがARCHIVED_DEMOTION_FACTOR倍になる"""
    add_topic(title="LegacyTopic", description="退役システム", tags=["domain:legacy"])
    _archive("domain:legacy")

    results = [
        {
            "type": "topic",
            "id": 1,
            "title": "LegacyTopic",
            "final_score": 0.8,
            "score": 0.8,
            "tags": ["domain:legacy"],
        }
    ]
    _apply_archived_demotion(results)

    item = results[0]
    assert item["archived"] is True
    assert item["archived_tags"] == ["domain:legacy"]
    assert item["score_breakdown"]["archived_factor"] == ARCHIVED_DEMOTION_FACTOR
    assert item["final_score"] == pytest.approx(0.8 * ARCHIVED_DEMOTION_FACTOR)
    assert item["score"] == item["final_score"]


def test_demotion_partial_archived_not_demoted(temp_db):
    """非archivedタグを1つでも持てば降格しない（エッジケース#7: 併存時は係数1.0）"""
    add_topic(
        title="MixedTopic", description="混在タグ",
        tags=["domain:legacy", "domain:active"],
    )
    _archive("domain:legacy")

    results = [
        {
            "type": "topic",
            "id": 1,
            "title": "MixedTopic",
            "final_score": 0.8,
            "score": 0.8,
            "tags": ["domain:legacy", "domain:active"],
        }
    ]
    _apply_archived_demotion(results)

    item = results[0]
    assert item["archived"] is False
    assert item["archived_tags"] == []
    assert item["score_breakdown"]["archived_factor"] == 1.0
    assert item["final_score"] == pytest.approx(0.8)


def test_demotion_no_tags_not_demoted(temp_db):
    """タグを持たないアイテムは降格対象外（空リストのall()による誤降格を防ぐ）"""
    results = [
        {
            "type": "topic",
            "id": 1,
            "title": "NoTagsTopic",
            "final_score": 0.5,
            "score": 0.5,
            "tags": [],
        }
    ]
    _apply_archived_demotion(results)

    item = results[0]
    assert item["archived"] is False
    assert item["archived_tags"] == []
    assert item["score_breakdown"]["archived_factor"] == 1.0
    assert item["final_score"] == pytest.approx(0.5)


def test_demotion_no_archived_tags_at_all(temp_db):
    """archivedタグが1件もない場合はDBを問い合わせても全アイテムarchived_factor=1.0"""
    add_topic(title="ActiveTopic", description="現役", tags=["domain:active"])

    results = [
        {
            "type": "topic",
            "id": 1,
            "title": "ActiveTopic",
            "final_score": 0.5,
            "score": 0.5,
            "tags": ["domain:active"],
        }
    ]
    _apply_archived_demotion(results)

    item = results[0]
    assert item["archived"] is False
    assert item["score_breakdown"]["archived_factor"] == 1.0


def test_demotion_empty_list():
    """空リストでエラーにならない"""
    results = []
    _apply_archived_demotion(results)
    assert results == []


def test_demotion_reorders_by_final_score(temp_db):
    """降格後、final_score降順で再ソートされる"""
    add_topic(title="LegacyTopic", description="退役", tags=["domain:legacy"])
    add_topic(title="ActiveTopic", description="現役", tags=["domain:active"])
    _archive("domain:legacy")

    results = [
        {
            "type": "topic", "id": 1, "title": "LegacyTopic",
            "final_score": 0.9, "score": 0.9, "tags": ["domain:legacy"],
        },
        {
            "type": "topic", "id": 2, "title": "ActiveTopic",
            "final_score": 0.5, "score": 0.5, "tags": ["domain:active"],
        },
    ]
    _apply_archived_demotion(results)

    # legacy: 0.9 * 0.3 = 0.27 < active: 0.5 → 順位が逆転する
    assert results[0]["id"] == 2
    assert results[1]["id"] == 1


# ========================================
# search() 統合テスト
# ========================================


def test_search_demotes_fully_archived_decision(temp_db):
    """search: 全タグarchivedのdecisionはarchived: True・archived_factor付きで返る

    decisionはtopicのタグをUNIONで継承するため（get_effective_tags_batch_by_ids）、
    topic自体のタグも同じarchived対象タグに揃える（DEFAULT_TAGSのdomain:testが混ざると
    「全タグarchived」の条件を満たさなくなる）。
    """
    t = add_topic(title="ArchivedFlowTopic", description="テスト用", tags=["domain:legacy-system"])
    add_decision(
        decision="ArchivedDemotionUniqueDecision",
        reason="退役システムに関する記録",
        topic_id=t["topic_id"],
        tags=["domain:legacy-system"],
    )
    _archive("domain:legacy-system")

    result = search_service.search(keyword="ArchivedDemotionUniqueDecision")
    assert "error" not in result
    matches = [r for r in result["results"] if r["title"] == "ArchivedDemotionUniqueDecision"]
    assert len(matches) == 1
    item = matches[0]
    assert item["archived"] is True
    assert item["archived_tags"] == ["domain:legacy-system"]
    assert item["score_breakdown"]["archived_factor"] == pytest.approx(ARCHIVED_DEMOTION_FACTOR)


def test_search_does_not_demote_mixed_tags_decision(temp_db):
    """search: archived併存でも非archivedタグがあれば降格せずarchived: False"""
    t = add_topic(title="MixedFlowTopic", description="テスト用", tags=DEFAULT_TAGS)
    add_decision(
        decision="MixedTagsUniqueDecision",
        reason="現役ドメインとarchivedドメイン両方を持つ記録",
        topic_id=t["topic_id"],
        tags=["domain:legacy-system", "domain:active-system"],
    )
    _archive("domain:legacy-system")

    result = search_service.search(keyword="MixedTagsUniqueDecision")
    assert "error" not in result
    matches = [r for r in result["results"] if r["title"] == "MixedTagsUniqueDecision"]
    assert len(matches) == 1
    item = matches[0]
    assert item["archived"] is False
    assert item["archived_tags"] == []
    assert item["score_breakdown"]["archived_factor"] == 1.0


def test_search_new_entity_with_archived_tag_demoted_from_creation(temp_db):
    """search: archivedタグを新規エンティティに付与しても降格が一貫して効く（エッジケース#11）"""
    _pretag_topic = add_topic(
        title="PretagTopic", description="事前にarchivedタグを作る", tags=["domain:legacy-preexist"]
    )
    _archive("domain:legacy-preexist")

    # topic自体のタグもdomain:legacy-preexistに揃える（decisionはtopicタグをUNIONで
    # 継承するため、DEFAULT_TAGS等の非archivedタグが混ざると全タグarchived判定を満たさない）
    t = add_topic(title="NewEntityFlowTopic", description="テスト用", tags=["domain:legacy-preexist"])
    add_decision(
        decision="NewEntityArchivedUniqueDecision",
        reason="archived後に作られた記録",
        topic_id=t["topic_id"],
        tags=["domain:legacy-preexist"],
    )

    result = search_service.search(keyword="NewEntityArchivedUniqueDecision")
    assert "error" not in result
    matches = [r for r in result["results"] if r["title"] == "NewEntityArchivedUniqueDecision"]
    assert len(matches) == 1
    assert matches[0]["archived"] is True


def test_search_demotion_affects_pagination_boundary(temp_db):
    """降格がoffset/limit切り出しより前に効き、下位アイテムが繰り上がる

    limit=1で、archivedのみを持つ高スコアアイテムと非archivedの低スコアアイテムを
    用意する。降格が切り出し後に効くだけの実装だと、1件目は常にarchived側のままに
    なる。切り出し前に効いていれば、降格後の順位で非archived側が1件目に来る。
    """
    # topic自体のタグをdomain:legacy-boundaryにする。decisionはtopicタグをUNIONで
    # 継承するため、archived側decisionはtopicタグ+own tagとも同じarchived対象タグに
    # 揃い「全タグarchived」を満たす。active側decisionはown tagのdomain:active-boundary
    # （非archived）が混ざるため降格対象外のまま
    t = add_topic(title="BoundaryFlowTopic", description="テスト用", tags=["domain:legacy-boundary"])
    # 同じキーワードを両方のdecisionのdecision本文に含め、FTSで両方ヒットさせる
    add_decision(
        decision="PaginationBoundaryUniqueKeyword archived side",
        reason="archived側。本文中にキーワードを複数回含め元のスコアを高くする "
               "PaginationBoundaryUniqueKeyword PaginationBoundaryUniqueKeyword",
        topic_id=t["topic_id"],
        tags=["domain:legacy-boundary"],
    )
    add_decision(
        decision="PaginationBoundaryUniqueKeyword active side",
        reason="非archived側",
        topic_id=t["topic_id"],
        tags=["domain:active-boundary"],
    )
    _archive("domain:legacy-boundary")

    result = search_service.search(
        keyword="PaginationBoundaryUniqueKeyword", limit=1,
    )
    assert "error" not in result
    assert len(result["results"]) == 1
    # 降格後の順位で非archived側が繰り上がって1件目に来ることを確認
    assert result["results"][0]["archived"] is False
    assert "active side" in result["results"][0]["title"]


def test_search_activity_type_archived_demotion(temp_db):
    """search: activity typeでも降格が横断して効く（typeをまたいだ動作確認）"""
    add_activity(
        title="ArchivedActivityCrossTypeUnique",
        description="退役システムのactivity",
        tags=["domain:legacy-activity"],
    )
    _archive("domain:legacy-activity")

    result = search_service.search(keyword="ArchivedActivityCrossTypeUnique")
    assert "error" not in result
    matches = [r for r in result["results"] if r["title"] == "ArchivedActivityCrossTypeUnique"]
    assert len(matches) == 1
    assert matches[0]["archived"] is True
