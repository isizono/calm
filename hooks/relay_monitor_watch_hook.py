"""PostToolUse hook (matcher: Monitor): relay inbox監視の起動判定。

CALM_RELAY_SESSION_AWARE（デフォルトOFF）が有効なとき、Monitorツール呼び出しの
tool_inputがそのセッションのrelay inbox pathを`persistent: true`で監視する
コマンドだったことを検知し、セッションIDキーのマーカーファイル
（HookState.set_monitor_started）を書く。user_prompt_submit_hookはこの
マーカーの有無で「そのセッション中にMonitor監視が起動済みか」を判定し、
未起動なら毎ターンリマインダーを注入する。

`persistent: true`を必須条件とするのは、既定の`persistent: false`だと
MonitorはtimeoutMs既定値（5分）で自動終了するため。マーカーだけ立てて
watch実体が5分後にサイレントに切れると、本hookが解決しようとしている
「起動指示の読み流しによる機能停止」を「監視のサイレント終了」という
形で再発させてしまう。

本hookはPostToolUse（ツール呼び出し完了後）で発火する独立プロセスであり、
SessionStart hook同様Claude Code CLIが起動する独立プロセスでMCPリクエスト
コンテキストを持たないため、identity解決はresolve_identity_by_ancestry()
（祖先pidチェーン一致、ps最大5回spawn）に依存する。HookState.relay_identity
キャッシュを読み書きし、user_prompt_submit_hookと双方向で共有する
（どちらか一方が先に解決すれば、もう片方はps spawnを避けられる）。

tool_response の内部構造はMonitorツール（ハーネス実装）依存で公開仕様が
無いため、判定は保守的に倒す: dict形式で明確に `is_error` が真のときのみ
「呼び出し失敗」とみなしスキップし、それ以外（構造不明を含む）はマーカーを
書く側に倒す（fail-open。マーカーを書き損ねてリマインダーが出続ける方が、
誤ってマーカーを書いて本来必要なリマインダーを消してしまうより実害が
小さいため）。
"""
import json
import os
import sys
from pathlib import Path

# プロジェクトルートをパスに追加（src.config等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.hook_state import HookState
from hooks.signal_capture import try_capture_signal
from src import config


def _looks_like_failure(tool_response) -> bool:
    """tool_responseが明確な失敗を示しているときのみTrueを返す。

    判定不能（tool_responseがdict以外、is_errorキーが無い等）はFalse
    （fail-open。マーカーを書く側に倒す）。
    """
    if isinstance(tool_response, dict):
        return bool(tool_response.get("is_error"))
    return False


def main() -> None:
    try:
        if not config.RELAY_SESSION_AWARE_ENABLED:
            print("{}")
            return

        # 環境変数によるテスト用オーバーライド
        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        raw = sys.stdin.read()
        data = json.loads(raw)

        session_id = data.get("session_id", "")
        tool_name = data.get("tool_name", "")
        if not session_id or tool_name != "Monitor":
            print("{}")
            return

        tool_input = data.get("tool_input") or {}
        command = tool_input.get("command", "")
        if not isinstance(command, str) or not command:
            print("{}")
            return

        if tool_input.get("persistent") is not True:
            # persistent:false（既定）はtimeout_msの既定5分でwatchが自動終了する。
            # マーカーだけ立てるとその後の生存確認手段が無いため、persistent:true
            # で呼ばれた場合のみ起動成功とみなす。
            print("{}")
            return

        state = HookState(session_id)

        identity = state.get_cached_relay_identity()
        if not identity:
            from src.services.relay.identity import get_relay_identity, resolve_identity_by_ancestry

            identity = get_relay_identity() or resolve_identity_by_ancestry()
            if identity:
                state.set_cached_relay_identity(identity)
        if not identity:
            # identity解決に失敗するセッションはrelay非参加とみなし何もしない（fail-open）
            print("{}")
            return

        from src.services.relay.inbox import inbox_path

        path = str(inbox_path(identity))
        if path not in command:
            # 別目的のMonitor呼び出し（このセッションのinbox監視ではない）
            print("{}")
            return

        if _looks_like_failure(data.get("tool_response")):
            print("{}")
            return

        state.set_monitor_started()
        print("{}")

    except Exception as e:
        # フェイルオープン: 例外時は状態記録をスキップするのみでtool実行自体は妨げない
        print(f"relay_monitor_watch_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:relay_monitor_watch", summary=str(e)[:200])
        print("{}")


if __name__ == "__main__":
    main()
