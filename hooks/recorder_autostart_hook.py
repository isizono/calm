"""SessionStart hook: CALM_RECORDER=1のとき、記録役を自動で付ける。

CALM_RECORDER環境変数が"1"のときだけ動作する（未設定・他の値は何もしない）。
対話セッションのみを対象とし、`claude -p`・`claude --bg`はCLAUDE_CODE_SESSION_
ATTENDED（"1"のときだけ対話とみなす。欠落・"0"はどちらも何もしない側に倒す。
実機確認: -p/--bgはいずれも"0"、対話セッションは"1"）で除外する。

main_pid（$CLAUDE_PID。hookプロセスから見て、hookを起動したclaude自身の
pidが入ることを実機確認済み）とsession_id（hook入力のsession_id）の組を
`HookState.BASE_DIR/recorder_runs/*/run.json`（既存の記録役run.json、
hooks.recorder_watch.run_dir_forが書く）に照らし、以下のいずれかに一致する
古い記録役を停止してから起動し直す。

- 同じmain_pidで別のsession_id（/clear。実機確認: main_pidは同一プロセスの
  まま、session_idだけ新しいuuidに変わる）
- 同じsession_idで別のmain_pid（resume。実機確認: session_idは維持され、
  main_pidだけ新しいプロセスのものに変わる。旧main_pid死亡から見張りの
  死亡確定（約30秒）以内にresumeすると旧記録役のmarkerがまだ新鮮で
  start()がno-opになり記録役が付かないまま残るため、pid不一致を検知した
  時点でstopしてから起動し直す）

compact（main_pid・session_idともに不変。実機確認済み）は上記いずれにも
一致しないため何もせず、start()自体の二重起動防止（is_recorder_attached）
がno-opにする。

起動・停止のいずれも`scripts/recorder.py start`/`stop`を切り離したプロセス
として呼ぶ（hookの終了を待たせない。stop側がtmux kill-sessionの完了を
同期的に待つと、その分だけSessionStart自体をブロックしてしまうため）。
main transcriptはSessionStart時点ではまだ存在しない
ことがある（実機確認: startup・/clear直後は未作成。resumeは既存ファイルが
そのまま使われる）ため、resolve_main_transcriptのglob解決に頼らずhook
入力のtranscript_pathを明示的に渡す。transcriptがまだ存在しない場合
（=セッション冒頭）だけ`--from-start`を足す。resumeのように既存の
transcriptがある場合に無条件で付けると、過去の会話全体を読み直して
log/materialとして重複記録してしまう（既定の「起動時点の末尾から」で
十分で、かつ正しい）。

呼び出し環境からはCALM_RECORDERを取り除き、記録役自身のセッションに記録役
が付くのを防ぐ。ただしtmuxサーバーが既に起動済みの場合、新規paneの環境は
サーバー起動時点のグローバル環境に由来し呼び出し側の環境そのものではない
ため、除去だけでは完全には防げない（実機確認: 既存サーバーに対する
`new-session`はCALM_RECORDER=1を渡しても新paneには伝播しなかった＝
サーバー起動時点の環境が優先される。根本対策はtmuxペイン起動側
（`_launch_tmux_session`）の対応が要る）。hook入力のcwdが
`HookState.BASE_DIR/recorder_runs/`の下にある場合は無条件でスキップする
二重の防御を持つ（記録役のtmuxペインは同ディレクトリ直下の共通cwdで
起動されるため、記録役自身のセッションのcwdは必ずここに入る）。

何が起きてもexit 0にし、stdoutには何も出さない（session_start_hook.pyの
additionalContextを汚さないため。本hookはhooks.jsonでsession_start_hook.py
の後に登録する）。この方針は依存モジュールのimport自体にも及ぶ
（下のtry/except）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

try:
    from hooks.hook_state import HookState
    from hooks.recorder_watch import _read_int_env, _spawn_detached_stop, run_dir_for
    from src.harness import select_harness
    from src.infra.process_signature import process_start_signature
except Exception:
    # CALM_RECORDERを使っていない大多数のセッションのSessionStartを壊さない。
    # importしてテストする側（main()を直接呼ぶユニットテスト）には
    # 例外をそのまま見せる。
    if __name__ == "__main__":
        sys.exit(0)
    raise


def _find_stale_sids(current_pid: int, current_sid: str) -> list[str]:
    """current_pid・current_sidの組と食い違う既存run.jsonのsession_idを返す。

    (別sid・同pid)は/clear、(同sid・別pid)はresumeに対応する（モジュール
    docstring参照）。同pid一致（/clear側）は、OSのpid再利用による誤判定を
    避けるため`process_start_signature`（起動時刻）も一致することを確認する
    （`hooks.recorder_marker.is_recorder_attached`・`recorder_watch._watch`と
    同じ照合方式）。同sid一致（resume側）はsession_id自体が実質衝突しない
    識別子のためこの照合を要さない。
    recorder_runs直下を毎回iterdirで総なめする単純な実装（専用の索引は持た
    ない）。
    # ponytail: recorder_runs配下はstop後も削除されずGCが無いため、運用が
    # 長期化するほどこの走査対象は増え続ける。実運用でコストが無視できなく
    # なったら索引化やGCを検討する。
    """
    base = HookState.BASE_DIR / "recorder_runs"
    if not base.is_dir():
        return []
    current_started_at = process_start_signature(current_pid)
    stale: list[str] = []
    for entry in base.iterdir():
        run_json = entry / "run.json"
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = data.get("main_sid")
        pid = data.get("main_pid")
        started_at = data.get("main_pid_started_at")
        if not isinstance(sid, str) or not isinstance(pid, int):
            continue
        if sid != current_sid and pid == current_pid:
            if current_started_at is not None and started_at == current_started_at:
                stale.append(sid)
        elif sid == current_sid and pid != current_pid:
            stale.append(sid)
    return stale


def _spawn_start(session_id: str, main_pid: int, transcript_path: str) -> None:
    venv_python = _project_root / ".venv" / "bin" / "python"
    recorder_script = _project_root / "scripts" / "recorder.py"
    env = {k: v for k, v in os.environ.items() if k != "CALM_RECORDER"}

    cmd = [
        str(venv_python), str(recorder_script), "start",
        "--session-id", session_id,
        "--pid", str(main_pid),
        "--transcript", transcript_path,
    ]
    if not Path(transcript_path).exists():
        cmd.append("--from-start")

    run_dir = run_dir_for(session_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "autostart.log", "a", encoding="utf-8") as log_fh:
        subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh,
            start_new_session=True,
        )


def main() -> int:
    try:
        if os.environ.get("CALM_RECORDER") != "1":
            return 0
        if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") != "1":
            return 0

        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        payload = select_harness(hook_event_name="SessionStart").read_hook_input()
        session_id = payload.get("session_id")
        transcript_path = payload.get("transcript_path")
        cwd = payload.get("cwd")
        if not isinstance(session_id, str) or not session_id:
            return 0
        if not isinstance(transcript_path, str) or not transcript_path:
            return 0

        recorder_runs_dir = (HookState.BASE_DIR / "recorder_runs").resolve()
        if isinstance(cwd, str) and Path(cwd).resolve().is_relative_to(recorder_runs_dir):
            return 0

        main_pid = _read_int_env("CLAUDE_PID")
        if main_pid is None:
            return 0

        for old_sid in _find_stale_sids(main_pid, session_id):
            try:
                _spawn_detached_stop(_project_root, run_dir_for(old_sid), old_sid)
            except Exception:
                pass

        _spawn_start(session_id, main_pid, transcript_path)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
