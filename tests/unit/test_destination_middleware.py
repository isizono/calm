"""DestinationCandidateMiddleware のユニット・結合テスト

goal/goal_hintブロックのjudge_ready検出、候補算出SQLの絞り込み、
is_session_alive/read_cli_sessionによる最終確認、レスポンスへの同梱を検証する。

sessionsテーブルの行は、候補算出SQLの絞り込み条件を1つずつ単離して検証する
ため、session_ledger_service.register()（世代交代等の無関係なロジックを含む）
を経由せず直接INSERTする。生存確認（is_session_alive）はtests/helpers.pyの
register_alive_heartbeat_session/register_dead_heartbeat_sessionで実ファイルを
書いて成立・不成立させ、is_session_alive自体はmonkeypatchしない
（tests/CLAUDE.mdの規約）。
"""
from unittest.mock import MagicMock

import pytest
from fastmcp.tools.tool import ToolResult

from src.db import get_connection
from src.services.activity_service import add_activity, update_activity
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


def _make_activity(title: str = "Act") -> int:
    topic = add_topic(title="T", description="d", tags=["domain:test"])
    activity = add_activity(
        title=title, description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [topic["topic_id"]]}], check_in=False,
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
    async def test_zero_candidates_omits_key_and_leaves_content_untouched(self, monkeypatch):
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", lambda *_a, **_k: [])
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 1}})
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
    async def test_candidates_found_adds_structured_content_and_text_hint(self, monkeypatch):
        fake_candidates = [
            {"name": "alice", "activity_id_raw": 42, "activity_title": "Some Activity"},
        ]
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", lambda *_a, **_k: fake_candidates)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 1}})
        result = await middleware.on_call_tool(_make_context(), _call_next_returning(tool_result))

        assert result.structured_content["destination_candidates"] == fake_candidates
        assert any("alice" in block.text for block in result.content if hasattr(block, "text"))

    @pytest.mark.asyncio
    async def test_exception_in_injection_is_swallowed(self, monkeypatch):
        """候補算出中の例外は本来のツール応答を道連れにせずベストエフォートで握りつぶす。"""
        def _boom(*_a, **_k):
            raise RuntimeError("boom")
        monkeypatch.setattr(destination_middleware, "_fetch_candidates", _boom)
        monkeypatch.setattr(destination_middleware, "get_caller_session_id", lambda: "self-session")

        middleware = DestinationCandidateMiddleware()
        tool_result = ToolResult(structured_content={"goal": {"label": "judge_ready", "goal_id_raw": 1}})
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
