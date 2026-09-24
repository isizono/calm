"""DeltaNotificationMiddleware のintegrationテスト

複数呼び出し元識別子を模擬し、check_in→baseline記録、別セッションの書き込みに
よるベル注入、announce-once（同じ差分は一度しか通知しない）、
自己通知抑制、再check_inでのscopeリセットを検証する。

呼び出し元識別子はget_caller_session_id()（起動器の恒久識別子優先、
無ければMCP接続単位のephemeral IDにフォールバック、どちらも無ければNone）の
解決結果をキーに使う。大半のテストは`src.middleware.delta_middleware`に注入
されたget_caller_session_id自体をmonkeypatchして識別子を切り替える（キーが
何であれ同じ振る舞いを検証すればよいテスト用の簡便な差し替え）。優先順位・
フォールバック契約そのものを検証する3テスト（起動器識別子優先/ephemeral分離/
無識別子）は、get_caller_session_id()が実際に読みにいく外部境界
（fastmcp.server.dependencies.get_http_headers/get_context）を差し替える。
check_in()も内部で同じget_caller_session_id()を呼ぶため、境界を差し替える
とcheck_in側の解決結果も一致して連動する（delta_middleware側だけを差し替える
方式ではこの一致が保証できない）。

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
from unittest.mock import MagicMock

import pytest
from fastmcp.tools.tool import ToolResult

from src.infra import session_identity
from src.services import session_registry_service
from src.services.activity_service import add_activity
from src.services.checkin_tier_service import collect_and_assemble as check_in
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.material_service import add_material
from src.services.pin_service import add_pin
from src.services.relation_service import add_relation
from src.services.topic_service import add_topic
from src.main import check_in as tool_check_in
from src.main import add_activity as tool_add_activity
import src.middleware.delta_middleware as delta_middleware
from src.middleware.delta_middleware import DeltaNotificationMiddleware, _watermarks
from tests.helpers import add_decision


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding呼び出しを無効化する"""


@pytest.fixture(autouse=True)
def _clear_watermarks():
    """モジュールレベルのwatermark状態をテスト間で共有しないようにする"""
    _watermarks.clear()
    yield
    _watermarks.clear()


@pytest.fixture(autouse=True)
def _isolate_session_registry(tmp_path, monkeypatch):
    """check_in内部のセッション別名レジストリ更新が本番の
    ~/.cc-memory/session_aliases.jsonに触れないよう、置き場を一時パスへ強制する。
    """
    monkeypatch.setenv(
        session_registry_service.REGISTRY_PATH_ENV,
        str(tmp_path / "session_aliases.json"),
    )


def _set_caller(monkeypatch, value):
    """次のon_call_tool呼び出しでget_caller_session_id()が返す値を固定する。"""
    monkeypatch.setattr(delta_middleware, "get_caller_session_id", lambda: value)


def _set_bridge_header(monkeypatch, bridge_id: str | None):
    """get_caller_session_id()が読みにいく起動器ヘッダを外部境界として差し替える。"""
    headers = {session_identity.BRIDGE_SESSION_HEADER: bridge_id} if bridge_id else {}
    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", lambda: headers)


def _set_ephemeral_connection(monkeypatch, connection_id: str | None):
    """get_caller_session_id()が起動器ヘッダ不在時に読みにいくMCP接続コンテキスト
    （ctx.session_id）を外部境界として差し替える。Noneはコンテキスト自体が
    存在しない状態（get_context()がRuntimeErrorを投げる状況）を表す。
    """
    if connection_id is None:
        def _raise():
            raise RuntimeError("no active context")
        monkeypatch.setattr("fastmcp.server.dependencies.get_context", _raise)
    else:
        ctx = MagicMock()
        ctx.session_id = connection_id
        monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)


def _make_context(tool_name: str):
    message = MagicMock()
    message.name = tool_name
    context = MagicMock()
    context.message = message
    return context


def _call_next_returning(tool_result: ToolResult):
    async def _inner(_ctx):
        return tool_result
    return _inner


def _noop_tool_result() -> ToolResult:
    return ToolResult(structured_content={"noop": True})


@pytest.fixture
def scope(temp_db):
    """スコープ用のtopicとそれに紐づくactivityを1組作成する"""
    topic = add_topic(title="Scope Topic", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    activity = add_activity(
        title="Scope Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )
    aid = activity["activity_id"]
    return tid, aid


@pytest.mark.asyncio
async def test_check_in_records_baseline_and_scope(scope, monkeypatch):
    _tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    checkin_result = check_in(aid)
    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # topicスコープはwatermarkに保存せず、以降の呼び出しのたびに引き直すため、
    # ここではactivity_idとbaselineだけを確認する（スコープの中身は
    # derive_scopeの単体テスト、および掲示板トピックの取りこぼしを確認する
    # 統合テストで検証する）。
    wm = _watermarks["caller-A"]
    assert wm["activity_id"] == aid
    assert wm["decision_id"] == 0
    assert wm["log_id"] == 0
    assert wm["material_id"] == 0


@pytest.mark.asyncio
async def test_add_activity_default_checkin_records_baseline(temp_db, monkeypatch):
    """add_activity(check_in=True、デフォルト)経由でもbaselineが記録されること。

    check_in結果はresult["check_in_result"]にネストして返るため、ツール名
    "check_in"の場合と同じ処理では拾えない。add_activityは"典型的な使い方"の
    主要経路（新規アクティビティ作成時にそのまま着手する）のため、これが
    未対応だとbaselineが一切セットされずデルタ通知が発動しない（PR #550レビュー指摘）。
    """
    topic = add_topic(title="Scope Topic via add_activity", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    middleware = DeltaNotificationMiddleware()

    add_activity_result = add_activity(
        title="Activity via default check_in", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}],
    )
    aid = add_activity_result["activity_id"]

    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("add_activity"),
        _call_next_returning(ToolResult(structured_content=add_activity_result)),
    )

    wm = _watermarks["caller-A"]
    assert wm["activity_id"] == aid


@pytest.mark.asyncio
async def test_add_activity_explicit_no_checkin_does_not_record_baseline(temp_db, monkeypatch):
    """add_activity(check_in=False)はcheck_in_resultを含まないため、baselineは記録されない。"""
    topic = add_topic(title="Scope Topic no checkin", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    middleware = DeltaNotificationMiddleware()

    add_activity_result = add_activity(
        title="Activity without check_in", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )

    _set_caller(monkeypatch, "caller-B")
    await middleware.on_call_tool(
        _make_context("add_activity"),
        _call_next_returning(ToolResult(structured_content=add_activity_result)),
    )

    assert "caller-B" not in _watermarks


@pytest.mark.asyncio
async def test_cross_session_delta_injected_then_announce_once(scope, monkeypatch):
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    # (a) Aがcheck_in
    checkin_result = check_in(aid)
    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # (b) 別セッションBがscope topicにdecisionを追加（Bはmiddlewareを経由しない
    # 素の書き込みとして表現。ピアセッションからの書き込みを模している）
    b_decision = add_decision("Bの決定", "reason", topic_id=tid)

    # (c) Aの次のツール呼び出しでdeltaがcontentに出る
    result1 = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    injected_text = result1.content[-1].text
    assert "デルタ通知" in injected_text
    assert "Bの決定" in injected_text
    assert result1.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "Bの決定"}
    ]

    # (d) 同じ呼び出しを再度実行 → announce-onceで出ない
    result2 = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result2.content) == 1
    assert "delta" not in (result2.structured_content or {})


@pytest.mark.asyncio
async def test_self_write_not_notified(scope, monkeypatch):
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # (e) A自身がscope topicにdecisionを追加。add_decisionsのツール呼び出しを
    # middleware経由で処理させ、自己通知抑制のwatermark前進を確認する
    own_write_result = add_decisions([{"topic_id": tid, "decision": "自分の決定", "reason": "r"}])
    await middleware.on_call_tool(
        _make_context("add_decisions"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_self_write_not_notified_for_logs(scope, monkeypatch):
    """add_decisionsだけでなくadd_logs経由の自己通知抑制も別コードパスとして確認する。"""
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    own_write_result = add_logs([{"topic_id": tid, "content": "自分のログ"}])
    await middleware.on_call_tool(
        _make_context("add_logs"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_self_write_not_notified_for_materials(scope, monkeypatch):
    """add_materialはcreated配列を持たずtop-levelにmaterial_idを返す特殊系のため、
    add_decisions/add_logsとは別コードパス（_handle_writeのmaterial分岐）を確認する。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    own_write_result = add_material(
        title="自分のmaterial", content="x", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [tid]}],
    )
    await middleware.on_call_tool(
        _make_context("add_material"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_recheckin_switches_active_scope(temp_db, monkeypatch):
    """再check_in（別activity）で、以降の呼び出しの実効scopeが新activity側に
    切り替わることを確認する（topicスコープは保存せず、activity_idを起点に
    毎回引き直すため、旧activityのtopicへの書き込みはもう届かない）。
    """
    topic1 = add_topic(title="Topic1", description="d", tags=["domain:test"])
    tid1 = topic1["topic_id"]
    activity1 = add_activity(
        title="Activity1", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid1]}], check_in=False,
    )
    aid1 = activity1["activity_id"]

    topic2 = add_topic(title="Topic2", description="d", tags=["domain:test"])
    tid2 = topic2["topic_id"]
    activity2 = add_activity(
        title="Activity2", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid2]}], check_in=False,
    )
    aid2 = activity2["activity_id"]

    middleware = DeltaNotificationMiddleware()
    _set_caller(monkeypatch, "caller-A")

    checkin1 = check_in(aid1)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin1)),
    )
    assert _watermarks["caller-A"]["activity_id"] == aid1

    # 再check_in（別activity）
    checkin2 = check_in(aid2)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin2)),
    )
    assert _watermarks["caller-A"]["activity_id"] == aid2

    # 再check_in後にtopic1へ書かれた決定は、もう実効scope外なので届かない
    add_decision("topic1後の決定", "reason", topic_id=tid1)
    # topic2への決定は現在のscope（activity2の1段先）なので届く
    d2 = add_decision("topic2の決定", "reason", topic_id=tid2)

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert result.structured_content["delta"]["new_decisions"] == [
        {"id": d2["decision_id"], "title": "topic2の決定"}
    ]


@pytest.mark.asyncio
async def test_session_without_checkin_gets_no_notification(scope, monkeypatch):
    tid, _aid = scope
    middleware = DeltaNotificationMiddleware()

    add_decision("誰かの決定", "reason", topic_id=tid)

    _set_caller(monkeypatch, "caller-without-checkin")
    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})
    assert "caller-without-checkin" not in _watermarks


@pytest.mark.asyncio
async def test_self_write_does_not_drop_pending_delta_from_other_session(scope, monkeypatch):
    """Bの未配信の書き込みが、Aの自己書き込みで黙って読み飛ばされないことを確認する。

    (a) Aがcheck_in
    (b) Bがscope内にdecisionを書く（Aへはまだ未配信）
    (c) Aが自分の書き込みをscope内に行う。自己通知抑制の対象だが、watermarkを
        自分のidまで一気に進める前に、Bの分をこの呼び出し自身の応答で配る
        （でなければ以降のcompute_deltaの下限を追い越され、二度と拾えなくなる）
    (d) 直後の別呼び出しでは、Bの決定は再配信されない（announce-once）。
        自分の決定も一貫して届かない（自己通知抑制）
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # (b) 別セッションBがscope内にdecisionを書く。Aはまだこれを読みにいっていない
    b_decision = add_decision("Bの決定（未配信）", "reason", topic_id=tid)

    # (c) Aが自分の決定を書く。b_decisionより大きいidが振られる
    own_write_result = add_decisions([{"topic_id": tid, "decision": "自分の決定", "reason": "r"}])
    own_decision_id = own_write_result["created"][0]["decision_id"]
    assert own_decision_id > b_decision["decision_id"]
    write_result = await middleware.on_call_tool(
        _make_context("add_decisions"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )
    assert write_result.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "Bの決定（未配信）"}
    ]

    # (d) 直後の呼び出しでは何も再配信されない
    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_out_of_scope_write_does_not_suppress_future_in_scope_deltas(temp_db, monkeypatch):
    """scope外topicへの自己書き込みはwatermarkを進めず、後続の別セッションの
    scope内書き込みも正しく検出され続けることを確認する。
    """
    scope_topic = add_topic(title="Scope Topic", description="d", tags=["domain:test"])
    tid = scope_topic["topic_id"]
    other_topic = add_topic(title="Other Topic", description="d", tags=["domain:test"])
    other_tid = other_topic["topic_id"]
    activity = add_activity(
        title="Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )
    aid = activity["activity_id"]

    middleware = DeltaNotificationMiddleware()
    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )
    assert _watermarks["caller-A"]["decision_id"] == 0

    # Aがscope外topicにdecisionを書く
    out_of_scope_write = add_decisions([
        {"topic_id": other_tid, "decision": "scope外の決定", "reason": "r"}
    ])
    await middleware.on_call_tool(
        _make_context("add_decisions"),
        _call_next_returning(ToolResult(structured_content=out_of_scope_write)),
    )
    # scope外への書き込みはwatermarkを進めない
    assert _watermarks["caller-A"]["decision_id"] == 0

    # 別セッションBがscope内topicにdecisionを追加
    b_decision = add_decision("scope内の決定", "reason", topic_id=tid)

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert result.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "scope内の決定"}
    ]


# ---------------------------------------------------------------------------
# 掲示板トピック（boardタグ）によるスコープ拡張。購読テーブルは持たず、
# ツール呼び出しのたびにderive_scopeでscopeを引き直すことを確認する。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_tagged_topic_related_after_checkin_delivers_new_log(scope, monkeypatch):
    """check-in後に新しくboard素タグのtopicを関連付けると、その後そのtopicに
    書かれたログが次のツール呼び出しでdeltaとして届く。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    board_topic = add_topic(title="Board Topic", description="d", tags=["domain:test", "board"])
    board_tid = board_topic["topic_id"]
    add_relation("topic", tid, [{"type": "topic", "ids": [board_tid]}])

    new_log = add_logs(
        [{"topic_id": board_tid, "content": "掲示板への投稿", "title": "掲示板への投稿"}]
    )["created"][0]

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert result.structured_content["delta"]["new_logs"] == [
        {"id": new_log["log_id"], "title": "掲示板への投稿"}
    ]


@pytest.mark.asyncio
async def test_related_topic_without_board_tag_is_not_included_in_scope(scope, monkeypatch):
    """boardタグの無いtopicは、scope topicへ関連付けても2段目としてscopeに
    入らない（2段目をboardタグで絞っていることの確認）。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    plain_topic = add_topic(title="Plain Related Topic", description="d", tags=["domain:test"])
    plain_tid = plain_topic["topic_id"]
    add_relation("topic", tid, [{"type": "topic", "ids": [plain_tid]}])

    add_logs([
        {"topic_id": plain_tid, "content": "boardタグ無しtopicへの投稿", "title": "届かないはずのログ"}
    ])

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_pre_existing_log_in_later_related_board_topic_is_not_flooded(scope, monkeypatch):
    """check-in前から存在するboardトピックの古いログは、check-in後にそのトピックを
    scope topicへ関連付けても、まとめて新規通知されない。既読位置の初期値を
    範囲内maxではなく全体maxにしているのは、このケースで古い記録が一斉に
    押し寄せるのを防ぐため。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    # check-in前に、まだ無関係なboardトピックへ古いログを書いておく
    board_topic = add_topic(title="Board Topic", description="d", tags=["domain:test", "board"])
    board_tid = board_topic["topic_id"]
    add_logs([
        {"topic_id": board_tid, "content": "check-in前の古い投稿", "title": "古い投稿"}
    ])

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # check-in後にboardトピックを関連付ける
    add_relation("topic", tid, [{"type": "topic", "ids": [board_tid]}])

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_global_baseline_matches_scoped_baseline_when_scope_unchanged(temp_db, monkeypatch):
    """scopeがcheck-in後も変わらない通常のケースでは、既読位置の初期値を
    範囲内maxから全体maxに変えても、配られる差分の集合は変わらないことを確認する。
    check-in前にscope内の決定を1件（範囲内maxを非ゼロにする）、さらにscope外の
    決定を1件（全体max > 範囲内maxの状況を作る）作ったうえで検証する。
    """
    scope_topic = add_topic(title="Scope Topic", description="d", tags=["domain:test"])
    tid = scope_topic["topic_id"]
    other_topic = add_topic(title="Other Topic", description="d", tags=["domain:test"])
    other_tid = other_topic["topic_id"]
    activity = add_activity(
        title="Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )
    aid = activity["activity_id"]

    d_old = add_decision("check-in前のscope内決定", "reason", topic_id=tid)
    add_decision("scope外の後発決定", "reason", topic_id=other_tid)

    middleware = DeltaNotificationMiddleware()
    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    d_new = add_decision("scope内の新しい決定", "reason", topic_id=tid)
    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    # check-in後の新しい決定だけが届き、check-in前から存在した決定は再配信されない
    assert result.structured_content["delta"]["new_decisions"] == [
        {"id": d_new["decision_id"], "title": "scope内の新しい決定"}
    ]


@pytest.mark.asyncio
async def test_self_write_to_newly_related_board_topic_is_not_self_notified(scope, monkeypatch):
    """check-in後に新しく関連付けたboardトピックへの自分の投稿は、自己通知抑制の
    対象になる。_handle_writeもscopeを毎回引き直さないと、新しく入ったtopicへの
    自己投稿がscope外と誤判定され、自己通知抑制が効かなくなる。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_caller(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    board_topic = add_topic(title="Board Topic", description="d", tags=["domain:test", "board"])
    board_tid = board_topic["topic_id"]
    add_relation("topic", tid, [{"type": "topic", "ids": [board_tid]}])

    own_write_result = add_logs([
        {"topic_id": board_tid, "content": "自分の投稿", "title": "自分の投稿"}
    ])
    write_result = await middleware.on_call_tool(
        _make_context("add_logs"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )
    assert "delta" not in (write_result.structured_content or {})

    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_same_launcher_identity_shares_watermark_across_reconnect(scope, monkeypatch):
    """起動器ヘッダが同じ値を返す限り、MCP接続（ephemeral ID）が変わっても
    watermarkは1つのエントリを共有する。get_caller_session_id()が実際に読みに
    いく境界（get_http_headers/get_context）を差し替え、優先順位の契約自体を
    検証する。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    # 1本目の接続（ephemeral id "conn-1"）。起動器ヘッダは"launcher-X"
    _set_bridge_header(monkeypatch, "launcher-X")
    _set_ephemeral_connection(monkeypatch, "conn-1")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )
    assert list(_watermarks.keys()) == ["launcher-X"]

    b_decision = add_decision("再接続後に増えた決定", "reason", topic_id=tid)

    # 接続が張り直された（ephemeral idが"conn-2"に変わった）が、起動器ヘッダは同じ
    _set_ephemeral_connection(monkeypatch, "conn-2")
    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )

    # 別キーが増えていない（分裂していない）ことと、1本目のbaselineに基づいて
    # deltaが検出されたことの両方を確認する
    assert list(_watermarks.keys()) == ["launcher-X"]
    assert result.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "再接続後に増えた決定"}
    ]


@pytest.mark.asyncio
async def test_different_ephemeral_identities_do_not_share_watermark(scope, monkeypatch):
    """起動器ヘッダが無くephemeral接続識別子だけの場合、値が異なれば別キーとなり、
    互いのbaseline/deltaに影響しない。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    _set_bridge_header(monkeypatch, None)
    _set_ephemeral_connection(monkeypatch, "conn-1")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    add_decision("conn-1のcheck_in後に増えた決定", "reason", topic_id=tid)

    # 別接続（別ephemeral ID）はbaselineを持たないため、同じ差分があっても通知されない
    _set_ephemeral_connection(monkeypatch, "conn-2")
    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})
    assert "conn-2" not in _watermarks

    # conn-1のwatermarkはconn-2の呼び出しで汚染されていない
    assert _watermarks["conn-1"]["decision_id"] == 0


@pytest.mark.asyncio
async def test_no_identity_resolved_skips_notification_and_does_not_touch_watermarks(scope, monkeypatch):
    """起動器ヘッダ・ephemeral接続識別子のどちらもget_caller_session_id()が
    解決できない(None)場合、共有フォールバックキーへの相乗りはせず、通知も
    watermarkの読み書きも一切行わない（他セッションのwatermarkも汚さない）。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    # 他セッションが先にcheck_inしている状態を作る
    _set_bridge_header(monkeypatch, "caller-A")
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )
    snapshot_before = dict(_watermarks["caller-A"])

    # 識別子が一切解決できない呼び出し: ヘッダ無し + MCP接続コンテキストも無し
    # （check_inでもbaselineを記録しない。中身の検証は次のget_topics呼び出し
    # 側で行う。call_nextの結果がそのまま返ることを見れば十分で、ここでは
    # watermarkが増えないことだけ確認する）。
    _set_bridge_header(monkeypatch, None)
    _set_ephemeral_connection(monkeypatch, None)
    checkin_result_unresolved = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=checkin_result_unresolved)),
    )
    assert set(_watermarks.keys()) == {"caller-A"}

    # scope内にdecisionを追加してから、識別子不明のまま別ツールを呼んでも通知されない
    add_decision("識別子不明呼び出し後の決定", "reason", topic_id=tid)
    result2 = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result2.content) == 1  # 通知が注入されず、call_nextの結果のまま
    assert "delta" not in (result2.structured_content or {})

    # 他セッション（caller-A）のwatermarkは変化していない
    assert set(_watermarks.keys()) == {"caller-A"}
    assert _watermarks["caller-A"] == snapshot_before


# ---------------------------------------------------------------------------
# 応答の形契約テスト: checkin_service.check_in（サービス層の生の戻り値）ではなく
# main.check_in / main.add_activity という「本物のツール」の戻り値を middleware に
# 通す。main側でflavor適用と全体予算（response_budget.apply_budget）が追加で
# かかるため、上のテスト群（checkin_serviceを直接呼んで偽装したToolResult）だけでは
# 形の変化を検出できない。checkin_scopeがこの2つの実ツール経路の応答からも
# 正しくscopeを読めることを保証する。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_check_in_tool_response_sets_baseline(scope, monkeypatch):
    """main.check_in()（flavor適用+全体予算を経た本物の応答）でもbaselineが立つ。"""
    _tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    real_result = tool_check_in(aid)  # flavor既定=internal、予算適用済み
    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=real_result)),
    )

    wm = _watermarks["caller-A"]
    assert wm["activity_id"] == aid


@pytest.mark.asyncio
async def test_real_add_activity_tool_response_sets_baseline(temp_db, monkeypatch):
    """main.add_activity()（check_in_resultにflavor+予算適用済み）でもbaselineが立つ。"""
    topic = add_topic(title="Real Tool Scope Topic", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    middleware = DeltaNotificationMiddleware()

    real_result = tool_add_activity(
        title="Real Tool Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}],
    )
    aid = real_result["activity_id"]

    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("add_activity"),
        _call_next_returning(ToolResult(structured_content=real_result)),
    )

    wm = _watermarks["caller-A"]
    assert wm["activity_id"] == aid


@pytest.mark.asyncio
async def test_real_check_in_tool_response_still_scopes_and_delivers_delta_when_over_budget(
    scope, monkeypatch
):
    """pin合計が全体予算を大きく超えるactivity（43,000字のpinを合成）でも、
    truncatedが付いた本物の応答からbaselineが立ち、以降のdeltaが届く
    （予算切り詰めがcheckin_scopeの読むキー(anchor.activity.id_raw/
    context.topics)を壊していないことの実地確認）。
    """
    tid, aid = scope
    huge = add_material(
        title="huge pinned material", content="z" * 43_000,
        tags=["domain:test"], source="t",
    )
    add_pin("activity", aid, "material", huge["material_id"])
    middleware = DeltaNotificationMiddleware()

    real_result = tool_check_in(aid)
    assert "truncated" in real_result  # 前提: 実際に予算切り詰めが発動している

    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=real_result)),
    )
    wm = _watermarks["caller-A"]
    assert wm["activity_id"] == aid

    # baseline成立後、別セッションの書き込みがdeltaとして届く
    b_decision = add_decision("予算超過後のBの決定", "reason", topic_id=tid)
    result = await middleware.on_call_tool(
        _make_context("get_topics"),
        _call_next_returning(_noop_tool_result()),
    )
    assert result.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "予算超過後のBの決定"}
    ]


@pytest.mark.asyncio
async def test_falsification_broken_checkin_scope_breaks_real_tool_contract(scope, monkeypatch):
    """checkin_scopeが読むキー名が変わると、本物のツール応答を通したこの契約テストが
    落ちることを確認する（形を変えるPRとスコープの読み方を変えるPRが必ず同じになる、
    という6節の設計意図の裏取り）。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()
    real_result = tool_check_in(aid)

    # delta_middlewareはfrom-importでcheckin_scopeを束縛しているため、
    # モジュール属性としてここを差し替える（「形が壊れた」ことのシミュレーション）
    monkeypatch.setattr(delta_middleware, "checkin_scope", lambda result: None)

    _set_caller(monkeypatch, "caller-A")
    await middleware.on_call_tool(
        _make_context("check_in"),
        _call_next_returning(ToolResult(structured_content=real_result)),
    )
    assert "caller-A" not in _watermarks  # scopeが読めないためbaselineが立たない
