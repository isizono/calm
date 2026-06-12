"""UserPromptSubmit hook: nudgeリマインダー注入（イベント駆動版）

処理フロー:
1. stdin読み込み → JSON parse（session_id取得）
2. session_idが空/null → 空JSON出力して終了
3. events.jsonl全読み
4. 未消費のnudgeイベント判定 → system-reminder注入
5. 何もなし → 空JSON出力

Stop hookでnudge判定とevents.jsonl追記を行い、本hookで消費して注入する。
注入タイミングが「ユーザーの次の発言時」になるため、文面もその文脈に合わせている。
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

_RECORD_NUDGE_MESSAGE = (
    "<system-reminder>"
    "直近の応答で記録ツール（add_logs/add_decisions/add_topic）が呼ばれていません。"
    "ユーザーの今回の発言に応答する前に、これまでの議論で残すべき事項がないか振り返ってください。"
    "該当があれば応答冒頭で記録してから本題に入ってください。"
    "</system-reminder>"
)


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

        if not events:
            print("{}")
            return

        # 4. 未消費のnudgeイベント判定
        # 最新のnudgeイベントを探す（consumed=Trueでないもの）
        for e in reversed(events):
            if e.get("e") != "nudge":
                continue
            if e.get("consumed"):
                continue

            # nudgeを消費済みにマーク
            e["consumed"] = True
            _rewrite_events(state, events)

            if e.get("type") == "follow_up":
                print(json.dumps(_make_hook_output(_FOLLOW_UP_NUDGE_MESSAGE), ensure_ascii=False))
                return
            elif e.get("type") == "record":
                repeat = e.get("repeat", 1)
                message = _RECORD_NUDGE_MESSAGE * repeat
                print(json.dumps(_make_hook_output(message), ensure_ascii=False))
                return

        # 5. 何もなし
        print("{}")

    except Exception as e:
        # フェイルオープン: 例外時は空JSON + stderrログ
        print(f"user_prompt_submit_hook.py error: {e}", file=sys.stderr)
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
