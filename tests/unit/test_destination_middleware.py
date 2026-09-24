"""DestinationCandidateMiddleware のユニット・結合テスト

goal/goal_hintブロックのjudge_ready検出、候補算出SQLの絞り込み、
is_session_alive/read_cli_sessionによる最終確認、レスポンスへの同梱を検証する。
add_logsでboardタグ付きトピックへ投稿したときの宛先候補同梱（config.PEER_NUDGE_ENABLED
配下）も対象に含む。

sessionsテーブルの行は、候補算出SQLの絞り込み条件を1つずつ単離して検証する
ため、session_ledger_service.register()（世代交代等の無関係なロジックを含む）
を経由せず直接INSERTする。生存確認（is_session_alive）はtests/helpers.pyの
register_alive_heartbeat_session/register_dead_heartbeat_sessionで実ファイルを
書いて成立・不成立させ、is_session_alive自体はmonkeypatchしない
（tests/CLAUDE.mdの規約）。
"""
import contextlib
import copy
import os
from unittest.mock import MagicMock

import pytest
from fastmcp.tools.tool import ToolResult

import src.config as config
from src.db import get_connection
from src.services.activity_service import add_activity, update_activity
from src.services.discussion_log_service import add_logs
from src.services.goal_service import set_goal
from src.services.topic_service import add_topic
import src.middleware.destination_middleware as destination_middleware
from src.middleware.destination_middleware import DestinationCandidateMiddleware
from tests.helpers import register_alive_heartbeat_session, register_dead_heartbeat_session


@pytest.fixture(autouse=True)
def _force_runtime_db_path(monkeypatch):
    """get_db_path()がtemp_dbのDISCUSSION_DB_PATHを確実に見るようにする。

    src.config.DB_PATHはモジュール初回import時に一度だけ解決され固定される
    （src/db.pyのget_db_path()参照）。他のテストが先にDB系フィクスチャを
    動かしていると初回import時の解決結果が固定されたままになりうるため、
    毎テストでNoneへ戻しDISCUSSION_DB_PATH（temp_db）を必ず優先させる。
    """
    import src.config as config
    monkeypatch.setattr(config, "DB_PATH", None)


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding呼び出しを無効化する"""


def _make_context(tool_name: str = "update_activity"):
    message = MagicMock()
    message.name = tool_name
    context = MagicMock()
    context.message = message
    return context


def _call_next_returning(tool_result: ToolResult):
    async def _inner(_ctx):
        return tool_result
    return _inner


def _call_recorder(return_value=None):
    """例外を投げず呼び出しを記録するだけのspy。

    on_call_toolの本体はベストエフォートのtry/exceptで囲われているため、
    「呼ばれてはいけない関数」を例外送出で差し替えると、実際に呼ばれても
    例外がそこで握りつぶされてテストの外まで伝播しない（検出力が無くなる）。
    呼び出し回数だけを記録し、正常系と同じ型の値を返すことで、誤って
    呼ばれた場合でも後続処理をクラッシュさせずに素通りさせ、呼び出し
    回数のassertでregressionを検出できるようにする。
    """
    calls = []

    def _fn(*args, **kwargs):
        calls.append((args, kwargs))
        return return_value

    return _fn, calls


def _make_activity(title: str = "Act") -> int:
    topic = add_topic(title="T", description="d", tags=["domain:test"])
    activity = add_activity(
        title=title, description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [topic["topic_id"]]}], check_in=False,
    )
    return activity["activity_id"]


def _make_board_topic(title: str = "Board Topic", related_topic_id: int | None = None) -> int:
    """素タグ`board`付きのトピックを作る。related_topic_idを指定すると、そのトピックとrelatedで関連付ける
    （board skillの手順で「boardトピックはrelated引数で元トピックと同時に関連付ける」形を再現する）。
    """
    related = [{"type": "topic", "ids": [related_topic_id]}] if related_topic_id is not None else None
    topic = add_topic(title=title, description="d", tags=["domain:test", "board"], related=related)
    return topic["topic_id"]


def _make_plain_topic(title: str = "Parent Topic") -> int:
    """boardタグを持たない、通常のトピックを作る。"""
    topic = add_topic(title=title, description="d", tags=["domain:test"])
    return topic["topic_id"]


def _make_board_activity(board_topic_id: int, title: str = "Board Activity") -> int:
    """board_topic_idへbelongs_toで紐づくアクティビティを作る（参加者がcheck-inする先）。"""
    activity = add_activity(
        title=title, description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [board_topic_id]}], check_in=False,
    )
    return activity["activity_id"]


def _make_judge_ready_goal(activity_id: int) -> int:
    """指定activityに、条件1件が最初からsatisfiedのgoalを新規作成し紐づける。

    条件がopenを経ないため、set_goal自身の応答がlabel=judge_readyになる。
    """
    result = set_goal(
        activity_id,
        goal={
            "new": {
                "handle": f"goal-for-{activity_id}",
                "statement": "stmt",
                "conditions": [{"statement": "c1", "actor": "claude", "state": "satisfied"}],
            }
        },
    )
    assert "error" not in result, result
    assert result["goal"]["label"] == "judge_ready"
    return result["goal_id_raw"]


def _seed_session_row(
    *,
    session_id: str,
    cli_session_id: str | None,
    cli_pid: int | None,
    activity_id: int | None,
    ended: bool = False,
    heartbeat_seconds_ago: int = 0,
) -> None:
    ended_clause = "CURRENT_TIMESTAMP" if ended else "NULL"
    ended_reason_clause = "'unregister'" if ended else "NULL"
    conn = get_connection(load_vec=False)
    try:
        conn.execute(
            f"""
            INSERT INTO sessions (
                session_id, id_kind, cli_session_id, cli_pid, mode,
                last_heartbeat_at, last_checkin_activity_id, ended_at, ended_reason
            ) VALUES (?, 'bridge', ?, ?, 'interactive',
                datetime('now', '-' || ? || ' seconds'), ?, {ended_clause}, {ended_reason_clause})
            """,
            (session_id, cli_session_id, cli_pid, heartbeat_seconds_ago, activity_id),
        )
        conn.commit()
    finally:
        conn.close()


# ========================================
# goal/goal_hintブロックの検出（DB不要）
# ========================================


class TestFindJudgeReadyGoalId:
    def test_no_goal_or_goal_hint_key_returns_none(self):
        result = ToolResult(structured_content={"noop": True})
        assert destination_middleware._find_judge_ready_goal_id(result) is None

    def test_goal_key_with_other_label_returns_none(self):
        result = ToolResult(structured_content={"goal": {"label": "active", "goal_id_raw": 1}})
        assert destination_middleware._find_judge_ready_goal_id(result) is None

    def test_goal_key_judge_ready_returns_goal_id(self):
        result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 7}})
        assert destination_middleware._find_judge_ready_goal_id(result) == 7

    def test_goal_hint_key_judge_ready_returns_goal_id(self):
        """update_activityのgoal_hintも同じ形で検出できる（check_in系専用のキー名に依存しない）。"""
        result = ToolResult(structured_content={"goal_hint": {"label": "judge_ready", "goal_id_raw": 9}})
        assert destination_middleware._find_judge_ready_goal_id(result) == 9


# ========================================
# on_call_tool: トリガー不成立時は高コスト処理を一切走らせない
# ========================================


class TestTriggerGating:
    @pytest.mark.asyncio
    async def test_no_goal_block_never_queries_candidates(self, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("goalブロックが無いのに候補算出処理が走った")
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", _boom)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"noop": True})
        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert "destination_candidates" not in result.structured_content

    @pytest.mark.asyncio
    async def test_label_active_never_queries_candidates(self, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("label=judge_ready以外なのに候補算出処理が走った")
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", _boom)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "active", "goal_id_raw": 1}})
        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert "destination_candidates" not in result.structured_content

    @pytest.mark.asyncio
    async def test_non_whitelisted_tool_never_queries_candidates_even_if_judge_ready(self, monkeypatch):
        """get_goal等の読み取りツールは、goalブロックがjudge_readyでも対象外にする。"""
        def _boom(*_a, **_k):
            raise AssertionError("ホワイトリスト対象外のツールなのに候補算出処理が走った")
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", _boom)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 1}})
        result = await middleware.on_call_tool(
            _make_context("get_goal"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content

    @pytest.mark.asyncio
    async def test_all_target_tool_names_are_allowed_through(self, monkeypatch):
        """plan-b.mdが列挙する書き込み系ツール名は、すべて候補算出処理まで到達する。

        実装の_TARGET_TOOL_NAMES自体をループ対象にすると、そこから名前が
        1つ抜け落ちてもテストが検出できない。仕様上の名前をここに書き写す。
        """
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")
        target_tool_names = ("check_in", "set_goal", "update_goal", "judge_goal", "update_activity")
        for tool_name in target_tool_names:
            called = []
            monkeypatch.setattr(
                destination_middleware, "_fetch_candidates",
                lambda *_a, **_k: (called.append(True) or []),
            )
            middleware = DestinationCandidateMiddleware()
            tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 1}})
            await middleware.on_call_tool(_make_context(tool_name), _call_next_returning(tool_result))
            assert called, f"{tool_name} で候補算出処理が呼ばれなかった"


# ========================================
# 候補算出SQLの絞り込み（_fetch_candidates）
# ========================================


class TestFetchCandidates:
    def test_excludes_session_not_checked_in_to_this_goals_activity(self, temp_db):
        """判定待ちgoalに紐づくactivityへcheck-inしていないセッションは候補に含めない。

        除外対象の行も生存・名前解決を素通りできる状態で用意し、goal_idの不一致
        だけで落ちることを検証する（cli_pid=None等の別条件で無条件に落ちる行では
        goal_idの絞り込み自体が壊れていても検出できないため）。
        """
        goal_activity_id = _make_activity("goal activity")
        goal_id = _make_judge_ready_goal(goal_activity_id)
        other_activity_id = _make_activity("unrelated activity")
        other_goal_id = _make_judge_ready_goal(other_activity_id)

        pid = register_alive_heartbeat_session("cli-other")
        _seed_session_row(
            session_id="other-session", cli_session_id="cli-other", cli_pid=pid,
            activity_id=other_activity_id,
        )

        assert destination_middleware._fetch_candidates(goal_id, "self-session") == []
        # 対照: 正しいgoal_idで問い合わせれば同じ行が候補として返る
        assert destination_middleware._fetch_candidates(other_goal_id, "self-session") == [
            {"name": "test-cli", "activity_id_raw": other_activity_id, "activity_title": "unrelated activity"}
        ]

    def test_excludes_caller_session_itself(self, temp_db):
        activity_id = _make_activity()
        goal_id = _make_judge_ready_goal(activity_id)
        pid = register_alive_heartbeat_session("cli-self")
        _seed_session_row(
            session_id="self-session", cli_session_id="cli-self", cli_pid=pid,
            activity_id=activity_id,
        )

        candidates = destination_middleware._fetch_candidates(goal_id, "self-session")
        assert candidates == []

    def test_excludes_ended_session_row(self, temp_db):
        activity_id = _make_activity()
        goal_id = _make_judge_ready_goal(activity_id)
        pid = register_alive_heartbeat_session("cli-ended")
        _seed_session_row(
            session_id="ended-session", cli_session_id="cli-ended", cli_pid=pid,
            activity_id=activity_id, ended=True,
        )

        candidates = destination_middleware._fetch_candidates(goal_id, "self-session")
        assert candidates == []

    def test_excludes_stale_heartbeat_row(self, temp_db):
        """心拍しきい値を超えて更新が無い行は事前絞り込みの時点で候補から落ちる。"""
        activity_id = _make_activity()
        goal_id = _make_judge_ready_goal(activity_id)
        pid = register_alive_heartbeat_session("cli-stale")
        _seed_session_row(
            session_id="stale-session", cli_session_id="cli-stale", cli_pid=pid,
            activity_id=activity_id, heartbeat_seconds_ago=10_000,
        )

        candidates = destination_middleware._fetch_candidates(goal_id, "self-session")
        assert candidates == []

    def test_excludes_when_is_session_alive_returns_false(self, temp_db):
        """事前絞り込みを通っても、is_session_alive()がFalseなら最終的に除外する。"""
        activity_id = _make_activity()
        goal_id = _make_judge_ready_goal(activity_id)
        register_dead_heartbeat_session("cli-dead")
        _seed_session_row(
            session_id="dead-session", cli_session_id="cli-dead", cli_pid=999999,
            activity_id=activity_id,
        )

        candidates = destination_middleware._fetch_candidates(goal_id, "self-session")
        assert candidates == []

    def test_excludes_when_display_name_cannot_be_resolved(self, temp_db):
        """is_session_alive()がTrueでも、台帳のcli_pidに対応するCLIセッションファイルが
        無く名前解決できない候補は除外する（is_session_aliveとread_cli_sessionは
        別々の入力（前者はalias registryのpid、後者は台帳のcli_pid）で判定されるため、
        別々に崩れうる）。
        """
        activity_id = _make_activity()
        goal_id = _make_judge_ready_goal(activity_id)
        # alias registry側は生存として書くが、使うpidは実際のCLIセッションファイルのpidとは別。
        register_alive_heartbeat_session("cli-name-missing")
        _seed_session_row(
            session_id="name-missing-session", cli_session_id="cli-name-missing",
            cli_pid=999999,  # ~/.claude/sessions/999999.json は存在しない
            activity_id=activity_id,
        )

        candidates = destination_middleware._fetch_candidates(goal_id, "self-session")
        assert candidates == []

    def test_includes_alive_named_session_checked_in_to_goal_activity(self, temp_db):
        activity_id = _make_activity("Target Activity")
        goal_id = _make_judge_ready_goal(activity_id)
        pid = register_alive_heartbeat_session("cli-alive")
        _seed_session_row(
            session_id="alive-session", cli_session_id="cli-alive", cli_pid=pid,
            activity_id=activity_id,
        )

        candidates = destination_middleware._fetch_candidates(goal_id, "self-session")
        assert candidates == [
            {"name": "test-cli", "activity_id_raw": activity_id, "activity_title": "Target Activity"}
        ]

    def test_shared_identifier_excludes_subagent_like_call(self, temp_db):
        """subagentは親セッションと同じ識別子(cli_session_id相当)を共有し、台帳に
        別行を持たない。呼び出し元識別子と一致する行が自己除外される仕組みにより、
        subagent単位の宛先が別候補として現れることもない。
        """
        activity_id = _make_activity()
        goal_id = _make_judge_ready_goal(activity_id)
        pid = register_alive_heartbeat_session("cli-shared")
        _seed_session_row(
            session_id="shared-session", cli_session_id="cli-shared", cli_pid=pid,
            activity_id=activity_id,
        )

        # 親セッション自身（subagentも同じcaller_session_idを名乗る）からの呼び出し
        candidates = destination_middleware._fetch_candidates(goal_id, "shared-session")
        assert candidates == []


# ========================================
# on_call_tool: 応答への同梱
# ========================================


class TestInjection:
    @pytest.mark.asyncio
    async def test_zero_candidates_omits_key_and_leaves_content_untouched(self, temp_db, monkeypatch):
        """候補が実際に0件（DBに該当行が無い）の場合、注入自体を行わない。"""
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")
        goal_id = _make_judge_ready_goal(_make_activity())

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": goal_id}})
        original_content_len = len(tool_result.content)

        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert "destination_candidates" not in result.structured_content
        assert len(result.content) == original_content_len

    @pytest.mark.asyncio
    async def test_caller_session_unresolved_skips_injection(self, monkeypatch):
        """呼び出し元識別子が解決できない場合は自己除外の判定ができないため注入しない。"""
        def _boom(*_a, **_k):
            raise AssertionError("呼び出し元識別子が無いのに候補算出処理が走った")
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", _boom)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: None)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 1}})
        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert "destination_candidates" not in result.structured_content

    @pytest.mark.asyncio
    async def test_candidates_found_adds_structured_content_and_text_hint(self, temp_db, monkeypatch):
        """候補が実際にDBから見つかった場合、structured_contentとテキストヒントの両方に載る。"""
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")
        activity_id = _make_activity("Some Activity")
        goal_id = _make_judge_ready_goal(activity_id)
        pid = register_alive_heartbeat_session("cli-alice")
        _seed_session_row(
            session_id="alice-session", cli_session_id="cli-alice", cli_pid=pid,
            activity_id=activity_id,
        )

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": goal_id}})
        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert result.structured_content["destination_candidates"] == [
            {"name": "test-cli", "activity_id_raw": activity_id, "activity_title": "Some Activity"}
        ]
        assert any("test-cli" in block.text for block in result.content if hasattr(block, "text"))

    @pytest.mark.asyncio
    async def test_exception_in_injection_is_swallowed(self, temp_db, monkeypatch):
        """候補算出中の例外は本来のツール応答を道連れにせずベストエフォートで握りつぶす。

        DB接続の取得（get_connection、外部境界）が失敗する状況を模して、
        _fetch_candidates自体は実処理のまま例外を発生させる。
        """
        def _boom(*_a, **_k):
            raise RuntimeError("boom")
        monkeypatch.setattr(destination_middleware, "get_connection", _boom)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")
        goal_id = _make_judge_ready_goal(_make_activity())

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": goal_id}})
        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert "destination_candidates" not in result.structured_content


# ========================================
# 結合テスト: 実際にgoalをjudge_readyまで持っていく
# ========================================


class TestIntegration:
    @pytest.mark.asyncio
    async def test_set_goal_response_gains_destination_candidates_from_real_flow(self, temp_db, monkeypatch):
        target_activity_id = _make_activity("Target Activity")
        pid = register_alive_heartbeat_session("cli-integration")
        _seed_session_row(
            session_id="other-session", cli_session_id="cli-integration", cli_pid=pid,
            activity_id=target_activity_id,
        )

        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        set_goal_result = set_goal(
            target_activity_id,
            goal={
                "new": {
                    "handle": "integration-goal",
                    "statement": "stmt",
                    "conditions": [{"statement": "c1", "actor": "claude", "state": "satisfied"}],
                }
            },
        )
        assert set_goal_result["goal"]["label"] == "judge_ready"

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=set_goal_result)
        result = await middleware.on_call_tool(_make_context("set_goal"), _call_next_returning(tool_result))

        assert result.structured_content["destination_candidates"] == [
            {"name": "test-cli", "activity_id_raw": target_activity_id, "activity_title": "Target Activity"}
        ]

    @pytest.mark.asyncio
    async def test_update_activity_goal_hint_gains_destination_candidates_from_real_flow(
        self, temp_db, monkeypatch
    ):
        """update_activityのgoal_hint（goalとは別のトップレベルキー）経由でも同様に注入される。

        判定待ちgoalに紐づく別activity(other_activity_id)へcheck-inしたセッションを
        候補として拾えることも合わせて確認する（goal単位の絞り込みであり、
        completedにするactivity自身への紐づけを要求しない）。
        """
        activity_id = _make_activity("Closing Activity")
        other_activity_id = _make_activity("Other Linked Activity")
        goal_id = _make_judge_ready_goal(activity_id)
        link_result = set_goal(other_activity_id, goal={"goal_id": goal_id})
        assert "error" not in link_result, link_result

        pid = register_alive_heartbeat_session("cli-hint")
        _seed_session_row(
            session_id="other-session", cli_session_id="cli-hint", cli_pid=pid,
            activity_id=other_activity_id,
        )
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        update_result = update_activity(activity_id, status="completed")
        assert update_result["goal_hint"]["label"] == "judge_ready"

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=update_result)
        result = await middleware.on_call_tool(
            _make_context("update_activity"), _call_next_returning(tool_result)
        )

        assert result.structured_content["destination_candidates"] == [
            {"name": "test-cli", "activity_id_raw": other_activity_id, "activity_title": "Other Linked Activity"}
        ]


# ========================================
# board拡張: config.PEER_NUDGE_ENABLED配下のゲーティング
# ========================================


class TestBoardGating:
    @pytest.mark.asyncio
    async def test_peer_nudge_disabled_never_queries_board_candidates(self, monkeypatch):
        """既定（PEER_NUDGE_ENABLED=False）ではadd_logsで候補算出処理が一切走らない。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", False)

        is_board_topic, is_board_topic_calls = _call_recorder(return_value=False)
        monkeypatch.setattr(destination_middleware, "_is_board_topic", is_board_topic)
        fetch_board_candidates, fetch_board_candidates_calls = _call_recorder(return_value=[])
        monkeypatch.setattr(destination_middleware, "_fetch_board_candidates", fetch_board_candidates)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"created": [{"topic_id": 1}], "errors": []})
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content
        assert is_board_topic_calls == []
        assert fetch_board_candidates_calls == []

    @pytest.mark.asyncio
    async def test_non_board_tool_ignored_even_when_enabled(self, monkeypatch):
        """add_logs以外のツールはPEER_NUDGE_ENABLED=Trueでも board 経路の対象外。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)

        is_board_topic, is_board_topic_calls = _call_recorder(return_value=False)
        monkeypatch.setattr(destination_middleware, "_is_board_topic", is_board_topic)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"created": [{"topic_id": 1}], "errors": []})
        result = await middleware.on_call_tool(
            _make_context("add_material"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content
        assert is_board_topic_calls == []

    @pytest.mark.asyncio
    async def test_no_created_items_never_queries(self, monkeypatch):
        """createdが空（全item失敗）ならboard候補算出処理を走らせない。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)

        is_board_topic, is_board_topic_calls = _call_recorder(return_value=False)
        monkeypatch.setattr(destination_middleware, "_is_board_topic", is_board_topic)

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"created": [], "errors": [{"index": 0, "error": {}}]})
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content
        assert is_board_topic_calls == []

    @pytest.mark.asyncio
    async def test_non_board_topic_never_queries_candidates(self, temp_db, monkeypatch):
        """投稿先topicがboardタグを持たなければ候補算出処理まで到達しない。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        fetch_board_candidates, fetch_board_candidates_calls = _call_recorder(return_value=[])
        monkeypatch.setattr(destination_middleware, "_fetch_board_candidates", fetch_board_candidates)

        plain_activity_id = _make_activity("Plain Activity")
        # plain_activity_idの親topic（boardタグ無し）へ実際にログを投稿する
        plain_topic_id = _get_belongs_to_topic_id(plain_activity_id)
        log_result = add_logs([{"topic_id": plain_topic_id, "content": "hello"}])
        assert not log_result["errors"], log_result

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content
        assert fetch_board_candidates_calls == []


def _get_belongs_to_topic_id(activity_id: int) -> int:
    """_make_activityが作ったactivityの親topic_idを取得する。"""
    with contextlib.closing(get_connection(load_vec=False)) as conn:
        row = conn.execute(
            "SELECT target_id FROM relations WHERE source_type='activity' AND source_id=?"
            " AND target_type='topic' AND relation_type='belongs_to'",
            (activity_id,),
        ).fetchone()
    assert row is not None
    return row["target_id"]


# ========================================
# board拡張: 候補算出・応答同梱
# ========================================


class TestBoardInjection:
    @pytest.mark.asyncio
    async def test_board_topic_post_gains_destination_candidates_from_real_flow(self, temp_db, monkeypatch):
        """boardタグ付きトピックへの投稿で、関連activityへcheck-inした生存中の他セッションが候補に載る。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        board_topic_id = _make_board_topic("Design Discussion")
        board_activity_id = _make_board_activity(board_topic_id, "Design Discussion Activity")
        pid = register_alive_heartbeat_session("cli-board")
        _seed_session_row(
            session_id="board-session", cli_session_id="cli-board", cli_pid=pid,
            activity_id=board_activity_id,
        )

        log_result = add_logs([{"topic_id": board_topic_id, "content": "[self] 意見ください"}])
        assert not log_result["errors"], log_result

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert result.structured_content["destination_candidates"] == [
            {"name": "test-cli", "activity_id_raw": board_activity_id, "activity_title": "Design Discussion Activity"}
        ]
        assert any("test-cli" in block.text for block in result.content if hasattr(block, "text"))

    @pytest.mark.asyncio
    async def test_caller_itself_excluded_from_board_candidates(self, temp_db, monkeypatch):
        """呼び出し元自身がboard activityへcheck-inしていても自己除外される。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        board_topic_id = _make_board_topic()
        board_activity_id = _make_board_activity(board_topic_id)
        pid = register_alive_heartbeat_session("cli-self")
        _seed_session_row(
            session_id="self-session", cli_session_id="cli-self", cli_pid=pid,
            activity_id=board_activity_id,
        )

        log_result = add_logs([{"topic_id": board_topic_id, "content": "c"}])
        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content

    @pytest.mark.asyncio
    async def test_dead_session_excluded_from_board_candidates(self, temp_db, monkeypatch):
        """生存していないセッションは候補から除外される。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        board_topic_id = _make_board_topic()
        board_activity_id = _make_board_activity(board_topic_id)
        register_dead_heartbeat_session("cli-dead")
        _seed_session_row(
            session_id="dead-session", cli_session_id="cli-dead", cli_pid=999999,
            activity_id=board_activity_id,
        )

        log_result = add_logs([{"topic_id": board_topic_id, "content": "c"}])
        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content

    @pytest.mark.asyncio
    async def test_multiple_board_topics_dedupe_same_session(self, temp_db, monkeypatch):
        """一括投稿(最大10件)で複数のboardトピックに同じセッションの候補が重なる場合、1回だけ載る。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        board_topic_a = _make_board_topic("Board A")
        board_topic_b = _make_board_topic("Board B")
        # 同一activityを両方のboardトピックへbelongs_toで関連付ける
        shared_activity_id = add_activity(
            title="Shared Activity", description="d", tags=["domain:test"],
            related=[
                {"type": "topic", "ids": [board_topic_a]},
                {"type": "topic", "ids": [board_topic_b]},
            ],
            check_in=False,
        )["activity_id"]
        pid = register_alive_heartbeat_session("cli-shared-board")
        _seed_session_row(
            session_id="shared-board-session", cli_session_id="cli-shared-board", cli_pid=pid,
            activity_id=shared_activity_id,
        )

        log_result = add_logs([
            {"topic_id": board_topic_a, "content": "c1"},
            {"topic_id": board_topic_b, "content": "c2"},
        ])
        assert not log_result["errors"], log_result

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert result.structured_content["destination_candidates"] == [
            {"name": "test-cli", "activity_id_raw": shared_activity_id, "activity_title": "Shared Activity"}
        ]

    @pytest.mark.asyncio
    async def test_same_display_name_different_sessions_both_included(self, temp_db, monkeypatch):
        """表示名(name)が同じでも別セッション（別session_id）なら両方候補に残ることを確認する。

        nameだけでdedupすると、たまたま同じ表示名を持つ別セッションが黙って
        片方だけになる（read_cli_sessionはnameの一意性を保証しない）。
        """
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        board_topic_a = _make_board_topic("Board A")
        board_topic_b = _make_board_topic("Board B")
        activity_a = _make_board_activity(board_topic_a, "Activity A")
        activity_b = _make_board_activity(board_topic_b, "Activity B")

        # 2つの別セッションが、たまたま同じ表示名("test-cli")を持つ状況を作る。
        # pidは互いに異なる実在のalive pidにする必要があるため、テストプロセス自身の
        # pidと親プロセスのpidを使う（どちらもテスト実行中は生存が保証される）。
        pid_a = register_alive_heartbeat_session("session-a", pid=os.getpid())
        _seed_session_row(
            session_id="session-a", cli_session_id="session-a", cli_pid=pid_a,
            activity_id=activity_a,
        )
        pid_b = register_alive_heartbeat_session("session-b", pid=os.getppid())
        _seed_session_row(
            session_id="session-b", cli_session_id="session-b", cli_pid=pid_b,
            activity_id=activity_b,
        )

        log_result = add_logs([
            {"topic_id": board_topic_a, "content": "c1"},
            {"topic_id": board_topic_b, "content": "c2"},
        ])
        assert not log_result["errors"], log_result

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        candidates = result.structured_content["destination_candidates"]
        assert {c["activity_id_raw"] for c in candidates} == {activity_a, activity_b}
        assert all(c["name"] == "test-cli" for c in candidates)

    @pytest.mark.asyncio
    async def test_board_topic_related_to_parent_topic_pulls_candidates_from_parent_activity(
        self, temp_db, monkeypatch
    ):
        """質問・周知・事前の声かけでは[議論]アクティビティを立てないため、boardトピック自身へ
        check-inする者がいない。boardトピックとrelatedで結ばれた元トピック側のアクティビティへ
        check-inした生存中セッションが候補として拾えることを確認する。
        """
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        parent_topic_id = _make_plain_topic("Parent Topic")
        parent_activity_id = add_activity(
            title="Parent Activity", description="d", tags=["domain:test"],
            related=[{"type": "topic", "ids": [parent_topic_id]}], check_in=False,
        )["activity_id"]
        board_topic_id = _make_board_topic("Announcement Board", related_topic_id=parent_topic_id)

        pid = register_alive_heartbeat_session("cli-parent")
        _seed_session_row(
            session_id="parent-session", cli_session_id="cli-parent", cli_pid=pid,
            activity_id=parent_activity_id,
        )

        log_result = add_logs([{"topic_id": board_topic_id, "content": "[self] 前提が変わりました"}])
        assert not log_result["errors"], log_result

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert result.structured_content["destination_candidates"] == [
            {"name": "test-cli", "activity_id_raw": parent_activity_id, "activity_title": "Parent Activity"}
        ]

    @pytest.mark.asyncio
    async def test_unrelated_topic_activity_not_pulled_as_candidate(self, temp_db, monkeypatch):
        """boardトピックとrelatedで結ばれていないトピックのアクティビティは候補に含まれない。"""
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        unrelated_activity_id = _make_activity("Unrelated Activity")
        board_topic_id = _make_board_topic("Isolated Board")  # related_topic_id指定なし

        pid = register_alive_heartbeat_session("cli-unrelated")
        _seed_session_row(
            session_id="unrelated-session", cli_session_id="cli-unrelated", cli_pid=pid,
            activity_id=unrelated_activity_id,
        )

        log_result = add_logs([{"topic_id": board_topic_id, "content": "c"}])
        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert "destination_candidates" not in result.structured_content


# ========================================
# フラグOFF時のバイト同一性（1バイトも応答が変わらないこと）
# ========================================


class TestPeerNudgeDisabledResponseParity:
    @pytest.mark.asyncio
    async def test_add_logs_response_unchanged_when_disabled_even_with_live_board_candidate(
        self, temp_db, monkeypatch
    ):
        """PEER_NUDGE_ENABLED=False（既定）なら、board候補が実在してもadd_logsの応答は1バイトも変わらない。

        ON時に候補が実際に載る状態（test_board_topic_post_gains_destination_candidates_from_real_flow
        と同じ仕込み）を用意したうえでOFFにして呼び、構造化コンテンツとcontent件数の両方が
        投入直後の状態から一切変化しないことを確認する。
        """
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        board_topic_id = _make_board_topic("Design Discussion")
        board_activity_id = _make_board_activity(board_topic_id, "Design Discussion Activity")
        pid = register_alive_heartbeat_session("cli-board-parity")
        _seed_session_row(
            session_id="board-parity-session", cli_session_id="cli-board-parity", cli_pid=pid,
            activity_id=board_activity_id,
        )

        log_result = add_logs([{"topic_id": board_topic_id, "content": "[self] 意見ください"}])
        assert not log_result["errors"], log_result

        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", False)
        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content=log_result)
        original_structured = copy.deepcopy(tool_result.structured_content)
        original_texts = [block.text for block in tool_result.content if hasattr(block, "text")]

        result = await middleware.on_call_tool(
            _make_context("add_logs"), _call_next_returning(tool_result)
        )

        assert result.structured_content == original_structured
        assert [block.text for block in result.content if hasattr(block, "text")] == original_texts

    @pytest.mark.asyncio
    async def test_goal_response_off_is_byte_identical_on_adds_one_skill_hint_line(
        self, temp_db, monkeypatch
    ):
        """goal系（set_goal等）の宛先候補injectionは、PEER_NUDGE_ENABLED=Falseのとき
        フラグ導入前と完全に同じ文言を返す。Trueのときは末尾にpeer-nudgeスキルへの
        誘導が1行だけ追加される。
        """
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")
        target_activity_id = _make_activity("Target Activity")
        pid = register_alive_heartbeat_session("cli-goal-parity")
        _seed_session_row(
            session_id="other-session", cli_session_id="cli-goal-parity", cli_pid=pid,
            activity_id=target_activity_id,
        )
        set_goal_result = set_goal(
            target_activity_id,
            goal={
                "new": {
                    "handle": "parity-goal",
                    "statement": "stmt",
                    "conditions": [{"statement": "c1", "actor": "claude", "state": "satisfied"}],
                }
            },
        )
        assert set_goal_result["goal"]["label"] == "judge_ready"

        middleware = DestinationCandidateMiddleware()

        # ToolResult(structured_content=...)はcontent[0]にJSONシリアライズを自動生成するため、
        # middlewareが.appendした宛先候補ブロックはcontent[-1]で取り出す。
        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", False)
        off_result = await middleware.on_call_tool(
            _make_context("set_goal"),
            _call_next_returning(ToolResult(structured_content=copy.deepcopy(set_goal_result))),
        )
        off_text = off_result.content[-1].text
        assert off_text == (
            "📮 [宛先候補] 判定待ちのgoalに関連する他セッションが1件あります。必要ならSendMessageで知らせてください。\n"
            "  - test-cli（Target Activity）"
        )

        monkeypatch.setattr(config, "PEER_NUDGE_ENABLED", True)
        on_result = await middleware.on_call_tool(
            _make_context("set_goal"),
            _call_next_returning(ToolResult(structured_content=copy.deepcopy(set_goal_result))),
        )
        on_text = on_result.content[-1].text
        assert on_text == off_text + "\n話しかける前にpeer-nudgeスキルを確認してください。"
