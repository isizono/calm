"""記録役セッションの起動・停止・状態確認ロジック。

CLIエントリポイントは scripts/recorder.py。本モジュールはtmuxで記録役の
claudeプロセスを起動し、そのセッション専用のsettings.json・mcp.json・
run.json・cursor.jsonを実行ディレクトリ（`hooks.recorder_watch.run_dir_for`
が返す場所）に用意する。目印ファイル（write_marker/remove_marker/
is_recorder_attached）とtmuxセッションの終了処理は、見張り
（hooks/recorder_watch.py）が既に持つ実装をそのまま再利用し、命名規則
（tmuxセッション名の組み立て方等）が2つの実装で食い違わないようにする。

CALMのMCPツール（add_*/update_*/check_in等）はここでは一切呼ばない。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from hooks.recorder_marker import is_recorder_attached, write_marker
from hooks.recorder_watch import (
    _DEFAULT_CURSOR,
    _last_uuid_up_to,
    _read_lines,
    _terminate,
    _write_cursor,
    run_dir_for,
)
from src.infra.process_signature import process_start_signature

SUBPROCESS_TIMEOUT_SEC = 10.0

# ponytail: --mcp-config での直接起動時にClaude Codeが組み立てるMCPツール名の
# 接頭辞は実機未確認。プラグイン経由は "mcp__plugin_calm_calm__"、リモートMCP
# 経由は "mcp__claude_ai_calm__" と判明しているが（hooks/hook_transcript.py）、
# どちらとも接続経路が異なるため、この値が実際と一致するかは初回起動で確かめる
# 必要がある。ずれていた場合はここだけ直せばよい。
MCP_TOOL_PREFIX = "mcp__calm__"

INITIAL_PROMPT = "起動確認。片が届くまで何もせず `READY` とだけ返してターンを終えよ"


class RecorderLaunchError(Exception):
    """起動・停止に必要な情報を解決できない、またはtmux/claudeの起動に失敗した。"""


# ===================================================================
# 識別情報の解決
# ===================================================================


def resolve_main_sid(explicit: str | None) -> str:
    sid = explicit or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise RecorderLaunchError(
            "メインのsession_idを解決できない。--session-idで指定せよ"
        )
    return sid


def resolve_main_pid(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get("CLAUDE_PID")
    if not raw:
        raise RecorderLaunchError("メインのpidを解決できない。--pidで指定せよ")
    try:
        return int(raw)
    except ValueError:
        raise RecorderLaunchError(f"CLAUDE_PIDが整数でない: {raw!r}") from None


def resolve_main_transcript(
    main_sid: str, explicit: str | None, *, projects_root: Path | None = None
) -> Path:
    """メインのtranscriptパスを解決する。

    明示指定が無ければ `~/.claude/projects/*/<main_sid>.jsonl` をglobで探す。
    0件・複数件はどちらもエラーにする（複数件は、どのプロジェクトディレクトリの
    ものか機械的に決められないため）。
    """
    if explicit:
        return Path(explicit).expanduser()
    root = projects_root or (Path.home() / ".claude" / "projects")
    matches = sorted(root.glob(f"*/{main_sid}.jsonl"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RecorderLaunchError(
            f"main transcriptが見つからない(session_id={main_sid})。"
            "--transcriptで指定せよ"
        )
    raise RecorderLaunchError(
        f"main transcriptの候補が複数ある(session_id={main_sid})。"
        f"--transcriptで指定せよ: {[str(m) for m in matches]}"
    )


def tmux_session_name(main_sid: str) -> str:
    """記録役のtmuxセッション名。

    `hooks/recorder_watch.py` の `_terminate` が終了時にkillする対象と
    同じ式（`calm-rec-<main_sid先頭8桁>`）である必要がある。停止処理自体は
    `_terminate` をそのまま再利用するためここでは重複しないが、起動時に
    このセッション名を自分で組み立てる箇所がもう1つあるため、式を本関数に
    集約する。
    """
    return f"calm-rec-{main_sid[:8]}"


# ===================================================================
# 実行ディレクトリの生成物（settings.json / mcp.json / run.json / cursor.json）
# ===================================================================


def build_settings(calm_root: Path, run_dir: Path) -> dict:
    """記録役の実行ディレクトリに置くsettings.jsonの中身。

    Stop hookを1本だけ登録する。asyncRewakeを使うhookは、ラッパー
    （`uv run`等）を挟むとラッパー自身の失敗exitが「起床」と誤解釈されるため、
    venvのpythonを直接execする。
    """
    venv_python = calm_root / ".venv" / "bin" / "python"
    watch_script = calm_root / "hooks" / "recorder_watch.py"
    return {
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{venv_python} {watch_script}",
                            "asyncRewake": True,
                            "timeout": 86400,
                        }
                    ],
                }
            ]
        },
        "permissions": {
            "allow": [
                f"{MCP_TOOL_PREFIX}search",
                f"{MCP_TOOL_PREFIX}get_*",
                f"{MCP_TOOL_PREFIX}add_logs",
                f"{MCP_TOOL_PREFIX}add_material",
                f"{MCP_TOOL_PREFIX}add_relation",
                f"Read({run_dir}/**)",
            ]
        },
    }


def write_settings_json(run_dir: Path, calm_root: Path) -> Path:
    path = run_dir / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_settings(calm_root, run_dir), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def build_mcp_config(calm_root: Path) -> dict:
    """`.mcp.json`（プラグイン形式・`mcpServers`ラップ無し）を、`--mcp-config`が
    要求する`{"mcpServers": {...}}`形式に包み直し、`${CLAUDE_PLUGIN_ROOT}`を
    実パスへ置き換える。"""
    raw_text = (calm_root / ".mcp.json").read_text(encoding="utf-8")
    substituted = json.loads(raw_text.replace("${CLAUDE_PLUGIN_ROOT}", str(calm_root)))
    return {"mcpServers": substituted}


def write_mcp_json(run_dir: Path, calm_root: Path) -> Path:
    path = run_dir / "mcp.json"
    path.write_text(
        json.dumps(build_mcp_config(calm_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def update_run_json(
    run_dir: Path,
    *,
    main_sid: str,
    main_pid: int,
    main_pid_started_at: str,
    main_transcript: Path,
    recorder_sid: str,
) -> dict:
    """run.jsonを書く。既存の`recorder_sids`があれば追記する（上書きしない）。

    前の記録役のtranscriptをトークン集計で引けなくなることを避けるため。
    """
    path = run_dir / "run.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    else:
        existing = {}
    recorder_sids = list(existing.get("recorder_sids") or [])
    recorder_sids.append(recorder_sid)
    data = {
        "main_sid": main_sid,
        "main_pid": main_pid,
        "main_pid_started_at": main_pid_started_at,
        "main_transcript": str(main_transcript),
        "recorder_sids": recorder_sids,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def ensure_cursor(run_dir: Path, main_transcript: Path, *, from_start: bool) -> None:
    """cursor.jsonが無いときだけ初期値を書く。既にあれば触らない（再開のため）。

    末尾から始める既定では、byte_offsetとlast_uuidを現時点の最後の完結行に
    合わせる（見張りが使う`_read_lines`をそのまま使い、書きかけの末尾行を
    読了扱いにしない判定を再利用する）。
    """
    cursor_path = run_dir / "cursor.json"
    if cursor_path.exists():
        return
    cursor = dict(_DEFAULT_CURSOR)
    if not from_start:
        lines, end_offset = _read_lines(main_transcript, 0)
        cursor["byte_offset"] = end_offset
        cursor["last_uuid"] = _last_uuid_up_to(lines, end_offset)
    _write_cursor(cursor_path, cursor)


# ===================================================================
# tmux起動
# ===================================================================


def build_recorder_shell_command(calm_root: Path, recorder_sid: str) -> str:
    """記録役のtmuxペインで実行するシェルコマンド文字列を組み立てる。

    `exec`でclaudeに置き換えることで、`tmux display-message '#{pane_pid}'`が
    シェル自身ではなくclaude本体のpidを返すようにする。
    """
    parts = [
        "exec", "claude",
        "--session-id", recorder_sid,
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--mcp-config", "mcp.json",
        "--append-system-prompt-file", str(calm_root / "hooks" / "recorder_instructions.md"),
        "--permission-mode", "dontAsk",
        "--model", "sonnet",
        INITIAL_PROMPT,
    ]
    return " ".join(shlex.quote(part) for part in parts)


def _launch_tmux_session(session_name: str, run_dir: Path, calm_root: Path, recorder_sid: str) -> None:
    pane_command = build_recorder_shell_command(calm_root, recorder_sid)
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", str(run_dir), pane_command],
            check=True, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RecorderLaunchError(f"tmuxセッションの起動に失敗した: {e}") from e


def _pane_pid(session_name: str) -> int:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_name, "#{pane_pid}"],
            check=True, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RecorderLaunchError(f"記録役のpid取得に失敗した: {e}") from e
    try:
        return int(result.stdout.strip())
    except ValueError:
        raise RecorderLaunchError(f"pane_pidが整数でない: {result.stdout!r}") from None


# ===================================================================
# start / stop / status
# ===================================================================


def start(
    *,
    calm_root: Path,
    session_id: str | None = None,
    pid: int | None = None,
    transcript: str | None = None,
    from_start: bool = False,
    projects_root: Path | None = None,
    sid_factory=lambda: str(uuid.uuid4()),
) -> dict[str, Any]:
    main_sid = resolve_main_sid(session_id)

    if is_recorder_attached(main_sid):
        return {"started": False, "reason": "already attached", "main_sid": main_sid}

    main_pid = resolve_main_pid(pid)
    main_pid_started_at = process_start_signature(main_pid)
    if main_pid_started_at is None:
        raise RecorderLaunchError(
            f"main pid {main_pid} の起動時刻を取得できない(プロセスが存在しない可能性)"
        )
    main_transcript = resolve_main_transcript(main_sid, transcript, projects_root=projects_root)

    run_dir = run_dir_for(main_sid)
    run_dir.mkdir(parents=True, exist_ok=True)

    write_settings_json(run_dir, calm_root)
    write_mcp_json(run_dir, calm_root)

    recorder_sid = sid_factory()
    update_run_json(
        run_dir,
        main_sid=main_sid,
        main_pid=main_pid,
        main_pid_started_at=main_pid_started_at,
        main_transcript=main_transcript,
        recorder_sid=recorder_sid,
    )
    ensure_cursor(run_dir, main_transcript, from_start=from_start)

    session_name = tmux_session_name(main_sid)
    _launch_tmux_session(session_name, run_dir, calm_root, recorder_sid)
    pane_pid = _pane_pid(session_name)
    write_marker(main_sid, pane_pid)

    return {
        "started": True,
        "main_sid": main_sid,
        "run_dir": str(run_dir),
        "recorder_sid": recorder_sid,
        "tmux_session": session_name,
        "pane_pid": pane_pid,
    }


def stop(*, session_id: str | None = None) -> dict[str, Any]:
    main_sid = resolve_main_sid(session_id)
    was_attached = is_recorder_attached(main_sid)
    run_dir = run_dir_for(main_sid)
    _terminate(run_dir, main_sid)
    return {"main_sid": main_sid, "was_attached": was_attached}


def status(*, session_id: str | None = None) -> dict[str, Any]:
    main_sid = resolve_main_sid(session_id)
    run_dir = run_dir_for(main_sid)
    cursor_path = run_dir / "cursor.json"
    cursor: dict | None = None
    if cursor_path.exists():
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cursor = None
    return {
        "main_sid": main_sid,
        "attached": is_recorder_attached(main_sid),
        "run_dir": str(run_dir),
        "run_dir_exists": run_dir.exists(),
        "cursor": cursor,
    }


# ===================================================================
# CLI
# ===================================================================


def _calm_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="記録役セッションの起動・停止・状態確認")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="記録役を起動する(二重起動は無視する)")
    p_start.add_argument("--session-id", help="既定: $CLAUDE_CODE_SESSION_ID")
    p_start.add_argument("--pid", type=int, help="既定: $CLAUDE_PID")
    p_start.add_argument("--transcript", help="既定: ~/.claude/projects/*/<session-id>.jsonlをglob")
    p_start.add_argument(
        "--from-start", action="store_true",
        help="transcriptの先頭から読み始める(既定は起動時点の末尾から)",
    )

    p_stop = sub.add_parser("stop", help="記録役を停止する")
    p_stop.add_argument("--session-id", help="既定: $CLAUDE_CODE_SESSION_ID")

    p_status = sub.add_parser("status", help="記録役の状態を表示する")
    p_status.add_argument("--session-id", help="既定: $CLAUDE_CODE_SESSION_ID")

    args = parser.parse_args(argv)

    try:
        if args.command == "start":
            result = start(
                calm_root=_calm_root(),
                session_id=args.session_id,
                pid=args.pid,
                transcript=args.transcript,
                from_start=args.from_start,
            )
        elif args.command == "stop":
            result = stop(session_id=args.session_id)
        else:
            result = status(session_id=args.session_id)
    except RecorderLaunchError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
