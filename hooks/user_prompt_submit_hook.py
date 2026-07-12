"""UserPromptSubmit hook: nudgeリマインダー注入（イベント駆動版）

処理フロー:
1. stdin読み込み → JSON parse（session_id取得）
2. session_idが空/null → 空JSON出力して終了
3. events.jsonl全読み
4. 未消費のnudgeイベント判定 → system-reminder注入
5. id_leak_count > 0 → 内部 ID 漏出 system-reminder 注入 + count reset
6. relay session-aware nudge（CCM_RELAY_SESSION_AWARE=1のときのみ） →
   system-reminder注入
7. 何もなし → 空JSON出力

Stop hookでnudge判定とevents.jsonl追記を行い、本hookで消費して注入する。
MessageDisplay hookが内部IDリテラル件数をid_leak_countに蓄積し、本hookで参照する。
注入タイミングが「ユーザーの次の発言時」になるため、文面もその文脈に合わせている。
relay session-aware nudge（手順6）はSessionStartの一回きりの起動指示が読み流されて
機能しない問題への対応で、events.jsonl/id_leakとは独立にHookState.monitor_started
マーカー（hooks/relay_monitor_watch_hook.pyがPostToolUseで書く）とrelay inboxの
未読件数を毎ターン判定する。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# プロジェクトルートをパスに追加
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.hook_state import HookState
from hooks.signal_capture import try_capture_signal
from src import config


def _make_hook_output(message: str) -> dict:
    """UserPromptSubmit hookのsystem-reminder注入用JSON構造を返す"""
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": message,
        }
    }


_FOLLOW_UP_NUDGE_MESSAGE = (
    "<system-reminder>"
    "直近で add_decisions を呼んだものの、関連エンティティ（topic/logs/activity/material/tag_notes）"
    "の更新が行われていません。応答に入る前に、補完すべき記録がないか確認してください。"
    "該当なしなら無視してOK。"
    "</system-reminder>"
)

_RECORD_NUDGE_TIER_LOW = (
    "直近の応答で記録ツール（add_logs/add_decisions/add_topic）が呼ばれていません。"
    "ユーザーの今回の発言に応答する前に、これまでの議論で残すべき事項がないか"
    "振り返ってください。該当があれば応答冒頭で記録してから本題に入ってください。"
    "該当なしなら無視してOK。"
)
_RECORD_NUDGE_TIER_MID = (
    "{turns_since}ターン記録ツール（add_logs/add_decisions/add_topic）が"
    "呼ばれていません。議論や作業が進んでいる場合、経緯が失われつつあります。"
    "応答前に記録すべき内容がないか確認してください。"
)
_RECORD_NUDGE_TIER_HIGH = (
    "{turns_since}ターン以上記録ツールが呼ばれていません。このまま進むと"
    "セッションの経緯が失われる可能性が高い状態です。今すぐ振り返って記録するか、"
    "記録すべき内容が本当にないかを明示的に判断してください。"
)

_ID_LEAK_NUDGE_MESSAGE = (
    "<system-reminder>"
    "Your previous response included internal IDs (e.g., `A#xxx`, `M#xxx`, `log #xxx`). "
    "Before responding to the user, revisit your reference style and switch to descriptive "
    "natural language; the user cannot resolve raw IDs."
    "</system-reminder>"
)


def _wrap_system_reminder(body: str) -> str:
    return f"<system-reminder>{body}</system-reminder>"


def _record_nudge_body(repeat: int, turns_since: int) -> str:
    """repeat段階に応じた文面を返す（1-2: 軽い促し, 3-4: 中程度, 5: 強い）。"""
    if repeat >= 5:
        template = _RECORD_NUDGE_TIER_HIGH
    elif repeat >= 3:
        template = _RECORD_NUDGE_TIER_MID
    else:
        return _RECORD_NUDGE_TIER_LOW
    return template.format(turns_since=turns_since)


def _format_nudge_message(event: dict, ntype: str | None) -> str | None:
    """nudgeイベントから注入文面を生成する。未知typeは None を返す。

    既存type名 (follow_up / record) も HintService type 名 (follow_up_after_decision /
    record_missing) も受け付ける。
    """
    if ntype in ("follow_up_after_decision", "follow_up"):
        return _FOLLOW_UP_NUDGE_MESSAGE
    if ntype in ("record_missing", "record"):
        repeat = event.get("repeat", 1)
        # turns_sinceは後方互換フィールド。旧events.jsonl(本フィールド追加前に
        # 書かれたnudgeイベント)には存在しないため、_NUDGE_INTERVALから逆算した
        # 近似値(repeat*2)にフォールバックする。
        turns_since = event.get("turns_since", repeat * 2)
        return _wrap_system_reminder(_record_nudge_body(repeat, turns_since))
    if ntype == "logs_sparse":
        body = event.get("message", "")
        if not body:
            return None
        return _wrap_system_reminder(body)
    return None


def _build_relay_turn_nudge(state: HookState) -> str | None:
    """relay session-aware毎ターンnudge（CCM_RELAY_SESSION_AWARE=1のときのみ動作）。

    SessionStart一回きりの起動指示はエージェントに読み流されて機能しないことが
    確認されたため、毎ターン判定してリマインダーを注入する。

    relay未構成（token未設定）・identity解決失敗（祖先pidチェーンで同一launcher
    プロセスを特定できない）のセッションはrelay非参加とみなし、Noneを返す
    （fail-open。エラーにしない）。

    Monitor監視が未起動（HookState.get_monitor_startedがFalse）なら起動指示、
    未読が1件以上あれば消化指示を、該当する行だけ束ねて返す。どちらも
    非該当（起動済みかつ未読0件）ならNoneを返す。
    """
    if not config.RELAY_SESSION_AWARE_ENABLED:
        return None

    from src.services.relay import config as relay_config

    if not relay_config.get_token():
        return None

    from src.services.relay.identity import get_relay_identity, resolve_identity_by_ancestry

    identity = get_relay_identity() or resolve_identity_by_ancestry()
    if not identity:
        return None

    from src.services.relay.inbox import count_unread, inbox_path

    monitor_started = state.get_monitor_started()
    unread = count_unread(identity)

    if monitor_started and unread <= 0:
        return None

    lines = []
    if not monitor_started:
        path = inbox_path(identity)
        lines.append(
            f"relay inboxの監視がまだ起動されていません。"
            f"Monitorツールで {path} を監視してください。"
        )
    if unread > 0:
        lines.append(f"relay inbox 未読: {unread}件 → relay_receiveで消化してください。")

    return _wrap_system_reminder("\n".join(lines))


def main() -> None:
    try:
        # 環境変数によるテスト用オーバーライド
        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        # 1. stdin読み込み
        raw = sys.stdin.read()
        data = json.loads(raw)
        session_id = data.get("session_id", "")

        # 2. session_idが空/null → 空JSON出力
        if not session_id:
            print("{}")
            return

        # 3. events.jsonl全読み
        state = HookState(session_id)
        events = state.read_events()

        # 4. 未消費のnudgeイベント判定（events空なら for loop は即抜ける）
        # 最新のnudgeイベントを探す（consumed=Trueでないもの）
        for e in reversed(events):
            if e.get("e") != "nudge":
                continue
            if e.get("consumed"):
                continue

            ntype = e.get("type")
            message = _format_nudge_message(e, ntype)
            if message is None:
                # 未知typeはconsumed扱いせず温存する (将来バージョンが追加handlerで
                # 消費する想定)。長期肥大化は session_end でevents.jsonlがローテート
                # されるため抑えられる。
                continue

            e["consumed"] = True
            _rewrite_events(state, events)
            print(json.dumps(_make_hook_output(message), ensure_ascii=False))
            return

        # 5. id_leak count チェック（既存 nudge を消費せず loop 抜けた場合のみ）
        # MessageDisplay hook が観測した内部 ID 漏出件数 > 0 ならリマインダー注入。
        # 既存 nudge と同 turn に立っていた場合は既存 nudge を優先し id_leak は
        # 次ターンに繰り越す (count は reset しない)。1 turn に 1 リマインダー
        # で認知負荷を抑える方針。
        if state.get_id_leak_count() > 0:
            state.reset_id_leak_count()
            print(json.dumps(_make_hook_output(_ID_LEAK_NUDGE_MESSAGE), ensure_ascii=False))
            return

        # 6. relay session-aware nudge（CCM_RELAY_SESSION_AWARE=1のときのみ、
        # 既存nudge・id_leakのどちらも非該当だった場合のみ判定）
        relay_message = _build_relay_turn_nudge(state)
        if relay_message:
            print(json.dumps(_make_hook_output(relay_message), ensure_ascii=False))
            return

        # 7. 何もなし
        print("{}")

    except Exception as e:
        # フェイルオープン: 例外時は空JSON + stderrログ
        print(f"user_prompt_submit_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:user_prompt_submit", summary=str(e)[:200])
        print("{}")


def _rewrite_events(state: HookState, events: list[dict]) -> None:
    """events.jsonlを全書き換えする（nudge消費マーク用）。
    tempfile + os.replace()でアトミックに書き換える。
    Note: stop_hookのappend_eventsと同じファイルを操作するが、
    発火順（Stop→ユーザー入力→UserPromptSubmit）上は通常競合しない。"""
    dir_ = state.events_path.parent
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8") as f:
        tmp = f.name
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    os.replace(tmp, state.events_path)


if __name__ == "__main__":
    main()
