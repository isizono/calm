"""記録役の回帰ハーネス。

記録役自身のtranscript（Claude Codeのjsonl）を読み、次を検証する。

- 片の指示が届く前のターン（起動確認・`片なし`のターン）で、CALMツールが
  1件も実行されていない（拒否された呼び出しは実行に数えない）
- 片ごとに、成功したsearchが1回以上あり、記録かSKIP行の証跡がある
  （判定は見張りと同じ hooks/recorder_evidence.py）
- 片ごとに、最終メッセージのDONE番号が片番号と一致する

transcriptだけを入力にするので、起動方式（tmux・片単位headless）に依らず使える。
headlessで片ごとにセッションが分かれる場合は、transcriptを処理順に並べて渡す。

使い方:
    uv run python scripts/recorder_harness.py check <transcript.jsonl>...
    uv run python scripts/recorder_harness.py live [--timeout 900]

liveは、tmuxで本物の記録役（claude、sonnet）を起動し、mock CALM MCP
（scripts/recorder_harness_mock_mcp.py）につないで3つの片を流してから
checkする。本物のDBには書かない。claudeを実際に動かすので手動で回す。
実行ディレクトリは終了後に作業ディレクトリ（出力のwork_dir）へ移す。記録役の
transcriptは通常の記録役と同じ場所（~/.claude/projects配下）に残り、main_sidは
`recorder-harness-` で始まる。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.recorder_evidence import (  # noqa: E402
    DONE_RE,
    SHARD_PROMPT_RE,
    calm_calls,
    missing_evidence,
)
from hooks.recorder_watch import _read_lines  # noqa: E402
from src.harness.interface import TranscriptEntry  # noqa: E402


def _is_prompt(entry: TranscriptEntry) -> bool:
    if entry.kind != "user" or entry.is_meta:
        return False
    types = {b.get("type") for b in entry.content}
    return "text" in types and "tool_result" not in types


def _text(entry: TranscriptEntry) -> str:
    return "\n".join(b.get("text", "") for b in entry.content if b.get("type") == "text")


def split_turns(entries: list[TranscriptEntry]) -> list[tuple[str, list[TranscriptEntry]]]:
    """prompt（人・hookが起こした発話）ごとに区切った (prompt本文, 以降のエントリ) の列。"""
    turns: list[tuple[str, list[TranscriptEntry]]] = []
    for entry in entries:
        if _is_prompt(entry):
            turns.append((_text(entry), []))
        elif turns:
            turns[-1][1].append(entry)
    return turns


def _final_text(entries: list[TranscriptEntry]) -> str:
    for entry in reversed(entries):
        if entry.kind == "assistant" and (text := _text(entry)):
            return text
    return ""


def check(entries: list[TranscriptEntry]) -> dict:
    failures: list[str] = []
    off_shard_attempts = 0
    shards: dict[int, list[list[TranscriptEntry]]] = {}
    for prompt, turn in split_turns(entries):
        m = SHARD_PROMPT_RE.search(prompt)
        if m:
            shards.setdefault(int(m.group(1)), []).append(turn)
            continue
        calls = calm_calls(turn)
        off_shard_attempts += len(calls)
        executed = [c.short_name for c in calls if c.ok]
        if executed:
            failures.append(f"片の指示の外のターン（{prompt[:30]!r}）でCALMツールが実行された: {executed}")

    report = []
    for no, turns in shards.items():
        flat = [e for turn in turns for e in turn]
        last_msg = _final_text(turns[-1])
        calls = [c for c in calm_calls(flat) if c.ok]
        dones = {int(d) for d in DONE_RE.findall(last_msg)}
        if dones != {no}:
            failures.append(f"片{no:04d}: 最終メッセージのDONE番号が片番号と一致しない: {sorted(dones)}")
        missing = missing_evidence(flat, last_msg)
        if missing:
            failures.append(f"片{no:04d}: 記録証跡が足りない: {missing}")
        report.append({
            "no": no,
            "turns": len(turns),
            "searches": sum(c.short_name == "search" for c in calls),
            "writes": [c.short_name for c in calls if c.short_name in ("add_logs", "add_material")],
            "final": last_msg,
        })
    if not shards:
        failures.append("片の指示を受けたターンが1つも無い")
    return {
        "ok": not failures,
        "failures": failures,
        "off_shard_attempts": off_shard_attempts,
        "shards": report,
    }


def read_entries(paths: list[Path]) -> list[TranscriptEntry]:
    return [line.entry for path in paths for line in _read_lines(path, 0)[0]]


# ===================================================================
# live: tmuxの記録役をmock MCPにつないで流す
# ===================================================================

_SEGMENTS = [
    # 記録すべき出来事（バグの観察と解決）がある片
    "見張りのテストを回したら、DONEを返したのにcursorが進まないバグを観察した。"
    "原因はpendingの確定前にcursorを書き戻していたことで、順序を入れ替えて解決した。",
    # 記録すべき出来事が無い片
    "ユーザー: おはよう\nアシスタント: おはようございます。今日も続きをやりましょう。",
    # mockのsearchが返す既存記録と同じ経緯の片
    "見張りが片番号を1つ飛ばして渡すバグを観察し、cursorの確定順を直して解決した。",
]


def _main_transcript(path: Path, activity_id: int) -> None:
    lines = []
    for i, text in enumerate(_SEGMENTS):
        if i > 0:
            lines.append({"type": "assistant", "uuid": f"b{i}", "message": {"content": [
                {"type": "tool_use", "id": f"tu-b{i}", "name": "mcp__plugin_calm_calm__check_in",
                 "input": {"activity_id": activity_id}},
            ]}})
        lines.append({"type": "user", "uuid": f"u{i}", "message": {"content": text}})
        lines.append({"type": "assistant", "uuid": f"a{i}", "message": {"content": [{"type": "text", "text": "了解"}]}})
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")


def _wait(cond, timeout: float, interval: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def live(timeout: float) -> dict:
    from src.services import recorder_launcher_service as svc

    work = Path(tempfile.mkdtemp(prefix="recorder-harness-"))
    mock_log = work / "mock_calls.jsonl"
    main_transcript = work / "main.jsonl"
    # 実在しないactivityにしてtopic候補を0件にする（本物のDBは読むだけ）
    _main_transcript(main_transcript, activity_id=10**9)
    venv_python = _project_root / ".venv" / "bin" / "python"
    mock_cfg = {"mcpServers": {"calm": {
        "command": str(venv_python),
        "args": [str(_project_root / "scripts" / "recorder_harness_mock_mcp.py"), str(mock_log)],
    }}}
    svc.build_mcp_config = lambda calm_root: mock_cfg

    main_sid = f"recorder-harness-{uuid.uuid4()}"
    main_proc = subprocess.Popen(["sleep", str(int(timeout) + 600)])
    run_dir = None
    try:
        started = svc.start(
            calm_root=_project_root, session_id=main_sid, pid=main_proc.pid,
            transcript=str(main_transcript), from_start=True,
        )
        run_dir = Path(started["run_dir"])
        cursor_path = run_dir / "cursor.json"

        def _cursor() -> dict:
            return json.loads(cursor_path.read_text(encoding="utf-8"))

        # 最後の片はメインが死んだと確定したときに渡されるので、それ以外を待ってから落とす
        last = len(_SEGMENTS)
        _wait(lambda: _cursor()["next_no"] >= last and _cursor()["pending"] is None, timeout)
        main_proc.kill()
        main_proc.wait()
        _wait(lambda: _cursor()["next_no"] > last and _cursor()["pending"] is None, timeout)

        recorder_sids = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["recorder_sids"]
        projects = Path.home() / ".claude" / "projects"
        paths = [p for sid in recorder_sids for p in projects.glob(f"*/{sid}.jsonl")]
        result = check(read_entries(paths))
        mock_calls = [json.loads(x) for x in mock_log.read_text(encoding="utf-8").splitlines()] if mock_log.exists() else []
        result["mock_calls"] = [c["tool"] for c in mock_calls]
        result["cursor"] = {k: _cursor().get(k) for k in ("next_no", "unacked", "evidence_rejected")}
        result["recorder_transcripts"] = [str(p) for p in paths]
        result["work_dir"] = str(work)
        return result
    finally:
        if main_proc.poll() is None:
            main_proc.kill()
        svc.stop(session_id=main_sid)
        # 本物の記録役の実行ディレクトリ群に混ざらないよう、作業ディレクトリへ移す
        if run_dir is not None and run_dir.exists():
            shutil.move(str(run_dir), str(work / "run_dir"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="記録役の回帰ハーネス")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="記録役のtranscriptを検証する")
    p_check.add_argument("transcripts", nargs="+", type=Path)
    p_live = sub.add_parser("live", help="tmuxの記録役をmock MCPにつないで片を流し、検証する")
    p_live.add_argument("--timeout", type=float, default=900.0, help="各待機の上限秒")
    args = parser.parse_args(argv)

    result = check(read_entries(args.transcripts)) if args.command == "check" else live(args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
