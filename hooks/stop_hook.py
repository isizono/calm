"""Stop hook: イベント駆動アーキテクチャ Phase 1

処理フロー:
1. stdin読み込み → JSON parse
2. ブロック上限チェック（_BLOCK_LIMIT回で強制approve）
3. transcript差分読み → イベント抽出 → events.jsonl追記
4. events.jsonl全読み
5. Skill Span判定 → Span中なら即approve（安全弁: MAX_SKILL_SPAN_TURNS）
6. check-in判定（e:toolでcheck_in/add_activityが1件でもあるか、猶予あり）
7. nudge判定 + 状態更新 → approve
"""
import os
import sys
import traceback
from pathlib import Path

# プロジェクトルートをパスに追加（src.db等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.heartbeat import update_heartbeat
from hooks.hook_state import HookState
from hooks.hook_transcript import (
    _CHECKIN_TOOLS,
    _RECORDING_TOOLS,
    extract_events,
    extract_last_activity_id,
)
from hooks.signal_capture import try_capture_signal
from src.harness import Harness, select_harness

_BLOCK_LIMIT = 1
# ユーザー発言がちょうどこのturn数に達した瞬間にcheck-in強制blockを1回発火する
# (one-shot等値判定、下記143行目参照)。2だと「hi」→「continue」のような2発言で
# 終わる軽量セッションが境界値そのものに一致してしまいほぼ確実にblockされていた。
# 3に引き上げることで2発言以内のセッションはblockを経験しない。
_CHECKIN_DEFER_TURNS = 3
_MAX_SKILL_SPAN_TURNS = 20
_NUDGE_INTERVAL = 2


def _is_orch_managed_activity(activity_id) -> bool:
    """指定アクティビティが orch 管理かを activities.orch_managed カラムで判定する。

    orchが管理するアクティビティにcheck-in済みのセッションは個人フローでなく
    orchフローなので、check-inブロック・nudgeの対象外とする。
    DB参照に失敗した場合はフェイルオープン（False=通常フロー扱い）。
    """
    if not activity_id:
        return False
    try:
        from src.db import get_connection

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT orch_managed FROM activities WHERE id = ?",
                (int(activity_id),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        return bool(row["orch_managed"])
    except Exception:
        return False


def main() -> None:
    harness = select_harness()
    try:
        # 環境変数によるテスト用オーバーライド
        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        # 1. hook入力読み込み
        data = harness.read_hook_input()
        transcript_path = data.get("transcript_path", "")
        session_id = data.get("session_id", "")

        if not session_id:
            harness.emit_approve("session_id is empty")
            return

        state = HookState(session_id)

        # 2. ブロック上限チェック
        if state.get_block_count() >= _BLOCK_LIMIT:
            state.reset_block_count()
            harness.emit_approve(f"ブロック上限（{_BLOCK_LIMIT}回）に達しました。強制的に通します。")
            return

        # 3. transcript差分読み → イベント抽出 → events.jsonl追記
        offset = state.get_transcript_offset()
        current_turn = state.get_current_turn()
        new_entries, new_offset, offset_was_reset = harness.read_transcript_entries_from_offset(transcript_path, offset)

        # オフセットリセット時はcurrent_turnとevents.jsonlもリセット
        if offset_was_reset:
            current_turn = 0
            # events.jsonlを空にする（古いイベントは無効）
            if state.events_path.exists():
                state.events_path.unlink()

        new_events, current_turn = extract_events(new_entries, current_turn)

        state.append_events(new_events)
        state.set_transcript_offset(new_offset)
        state.set_current_turn(current_turn)

        # 4. events.jsonl全読み
        all_events = state.read_events()

        # 5. Skill Span判定
        in_skill_span = _is_in_skill_span(all_events, current_turn)
        if in_skill_span:
            state.reset_block_count()
            harness.emit_approve("Skill Span中のためチェックをスキップします。")
            _safe_post_approve(state, all_events, transcript_path, session_id=session_id)
            return

        # 6. check-in判定
        has_checkin = any(
            e["e"] == "tool" and e.get("name") in _CHECKIN_TOOLS
            for e in all_events
        )
        if has_checkin:
            # activity_idを抽出して保存
            _update_checked_in_activity(state, all_events, transcript_path, harness)

        # orch_managed=1 のアクティビティにcheck-in済みのセッションでは
        # 個人フロー用のcheck-inブロック・nudgeを抑制する。
        suppress_personal_flow = _is_orch_managed_activity(
            state.get_checked_in_activity()
        )

        if not has_checkin and not suppress_personal_flow:
            if current_turn == _CHECKIN_DEFER_TURNS:
                # one-shot block: 正確にdefer turnで1回だけblock
                state.increment_block_count()
                harness.emit_block(
                    "アクティビティにcheck-inしてください。"
                    "該当するものがなければadd_activityで作成してください。"
                )
                return

        # 7. nudge判定 + 状態更新 + approve
        state.reset_block_count()
        harness.emit_approve()
        _safe_post_approve(
            state, all_events, transcript_path, current_turn,
            run_nudges=not suppress_personal_flow,
            session_id=session_id,
        )

    except Exception as e:
        # フェイルオープン: 例外時はapprove
        print(f"stop_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:stop", summary=str(e)[:200])
        harness.emit_approve(f"stop_hook.py internal error: {e}")


def _is_in_skill_span(events: list[dict], current_turn: int) -> bool:
    """Skill Span中かどうかを判定する。

    最後のskillイベントのturnから現在のturnまでの距離が
    MAX_SKILL_SPAN_TURNS以内で、かつ直近のturnにskillイベントがある場合。
    """
    last_skill_turn = None
    for e in events:
        if e["e"] == "skill":
            last_skill_turn = e.get("turn", 0)

    if last_skill_turn is None:
        return False

    # 安全弁: MAX_SKILL_SPAN_TURNS超過で強制終了
    if current_turn - last_skill_turn > _MAX_SKILL_SPAN_TURNS:
        return False

    # 直近turnにskillイベントがあるか（= skillイベントがないturnが来たらSpan終了）
    return last_skill_turn >= current_turn


def _turns_since_last_recording(events: list[dict], current_turn: int) -> int:
    """最後に記録ツールが呼ばれたturnからの経過ターン数を返す。"""
    # 記録ゼロの場合はセッション開始(turn 0)から経過したとみなす
    last_recording_turn = 0
    for e in events:
        if e["e"] == "tool" and e.get("name") in _RECORDING_TOOLS and e.get("turn", 0) > last_recording_turn:
            last_recording_turn = e.get("turn", 0)
    return current_turn - last_recording_turn


def _update_checked_in_activity(
    state: HookState, events: list[dict], transcript_path: str, harness: Harness
) -> None:
    """check_inイベントからactivity_idを抽出し、checked_in_activityを更新する。"""
    # check_inイベントからactivity_idを取得
    for e in reversed(events):
        if e["e"] == "tool" and e.get("name") == "check_in" and "activity_id" in e:
            state.set_checked_in_activity(e["activity_id"])
            return

    # フォールバック: transcript全走査（add_activityのtool_result対応）
    aid = extract_last_activity_id(harness.read_transcript_entries(transcript_path))
    if aid is not None:
        state.set_checked_in_activity(aid)


def _safe_post_approve(
    state: HookState, events: list[dict], transcript_path: str,
    current_turn: int = 0,
    *,
    run_nudges: bool = False,
    session_id: str | None = None,
) -> None:
    """approve出力後の状態更新。例外はstderrログのみ（double-output防止）。

    各処理は独立したtryブロックで囲み、一方の失敗が他方を阻害しないようにする。
    """
    try:
        _update_state_on_approve(state, events, transcript_path, session_id=session_id)
    except Exception as e:
        print(
            f"stop_hook.py post-approve error (state): {e}\n{traceback.format_exc()}",
            file=sys.stderr,
        )
    if run_nudges:
        try:
            _handle_nudges(state, events, current_turn)
        except Exception as e:
            print(
                f"stop_hook.py post-approve error (nudge): {e}\n{traceback.format_exc()}",
                file=sys.stderr,
            )


def _update_state_on_approve(
    state: HookState, events: list[dict], transcript_path: str,
    *,
    session_id: str | None = None,
) -> None:
    """approve時の状態更新（heartbeat）"""
    # heartbeat更新
    activity_id = state.get_checked_in_activity()
    if activity_id is not None:
        update_heartbeat(activity_id, session_id)


def _handle_nudges(state: HookState, events: list[dict], current_turn: int) -> None:
    """nudge判定: events.jsonlから直接判定してnudgeイベントを追記する。

    user_prompt_submit_hookがevents.jsonlを読んでnudge注入を判定するため、
    nudgeフラグの代わりにnudgeイベントをevents.jsonlに追記する。
    typeはHintServiceの値域 (record_missing / follow_up_after_decision / logs_sparse) と統一する。
    """
    nudge_events: list[dict] = []

    if current_turn > 0 and current_turn % _NUDGE_INTERVAL == 0:
        recent_turn_threshold = current_turn - _NUDGE_INTERVAL
        has_recent_record = any(
            e["e"] == "tool"
            and e.get("name") in _RECORDING_TOOLS
            and e.get("turn", 0) > recent_turn_threshold
            for e in events
        )
        if not has_recent_record:
            turns_since = _turns_since_last_recording(events, current_turn)
            repeat = max(1, min(turns_since // _NUDGE_INTERVAL, 5))
            nudge_events.append({
                "e": "nudge",
                "type": "record_missing",
                "turn": current_turn,
                "repeat": repeat,
                "turns_since": turns_since,
            })

    recent_events = [e for e in events if e.get("turn", 0) == current_turn]
    decision_events = [
        e for e in recent_events
        if e["e"] == "tool" and e.get("name") == "add_decisions"
    ]
    if decision_events:
        companion_tools = (_RECORDING_TOOLS | _CHECKIN_TOOLS) - {"add_decisions"}
        has_companion = any(
            e["e"] == "tool" and e.get("name") in companion_tools for e in recent_events
        )
        if not has_companion:
            nudge_events.append({
                "e": "nudge",
                "type": "follow_up_after_decision",
                "turn": current_turn,
            })

        nudge_events.extend(
            _collect_logs_sparse_nudges(decision_events, current_turn)
        )

    if nudge_events:
        state.append_events(nudge_events)


def _collect_logs_sparse_nudges(
    decision_events: list[dict], current_turn: int
) -> list[dict]:
    """直近turnのadd_decisions topic_idからlogs_sparse判定を行う。

    HintService経由でtopic scopeの遅延hint (logs_sparse) を抽出する。
    """
    topic_ids: list[int] = []
    seen: set[int] = set()
    for e in decision_events:
        for tid in e.get("topic_ids", []) or []:
            try:
                tid_int = int(tid)
            except (ValueError, TypeError):
                continue
            if tid_int in seen:
                continue
            seen.add(tid_int)
            topic_ids.append(tid_int)
    if not topic_ids:
        return []

    try:
        from src.services import hint_service
    except ImportError as e:
        print(f"stop_hook.py logs_sparse import error: {e}", file=sys.stderr)
        return []

    nudges: list[dict] = []
    for tid in topic_ids:
        # 1topicのDB障害が他topicの判定を止めないようループ内で握る (フェイルオープン)
        try:
            hints = hint_service.get_hints("topic", tid)
        except Exception as e:
            print(f"stop_hook.py logs_sparse get_hints error: {e}", file=sys.stderr)
            continue
        for h in hints:
            if h.get("delivery_hint") != "deferred":
                continue
            if h.get("type") != "logs_sparse":
                continue
            nudges.append({
                "e": "nudge",
                "type": "logs_sparse",
                "turn": current_turn,
                "topic_id": tid,
                "message": h["message"],
            })
    return nudges


if __name__ == "__main__":
    main()
