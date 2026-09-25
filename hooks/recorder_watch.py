"""Stop hook（asyncRewake）: 記録役セッションの見張り。

記録役プロセスのStop hookとして起動し（登録は記録役の実行ディレクトリ
（run_dir）にだけ置かれる。プラグイン共通のhooks.jsonには登録しない）、
メインセッションのtranscriptを差分で読み、ある程度たまったら片
（chunk、markdownファイル）に切り出して記録役へ渡す。CALMのMCPツールは
一切呼ばない（DB読み取りだけsqlite3で直接行う）。

run_dirはhook入力のcwd（記録役プロセスの起動ディレクトリ）から決める。
`run.json`（main_sid・main_pid・main_pid_started_at・main_transcript）が
無ければ、記録役の実行ディレクトリではないとみなして何もしない。

状態は `run_dir/cursor.json` に持つ。`last_uuid`・`byte_offset`・
`activity_id_at_cursor` は「確定済み」の読了位置を表し、片を渡した直後は
`pending` にだけ次の位置を書く。記録役が最終行に `DONE <no>` と書いたこと
を確認して初めて、pendingの内容を確定側へ写す（at-least-once。確定前に
本プロセスが落ちても、次の起動でpendingから再送できる）。

メインのtranscriptはClaudeCodeHarnessの差分読み（`read_transcript_entries_
from_offset`）を使わず、本モジュール内で改めてバイト単位に読み直す。
harness側は読みかけの末尾行（改行未到達）の分もオフセットへ含めてしまう
契約のため、そのまま使うと書きかけの行を「読了済み」として飲み込み、
完成を待たずに消えてしまう。1エントリの正規化（`to_entry`）だけは
ClaudeCodeHarnessを再利用する。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.hook_state import HookState
from hooks.hook_transcript import _extract_short_name, _is_calm_tool, extract_last_activity_id
from hooks.recorder_marker import remove_marker, touch_marker
from src.env_compat import env_get
from src.harness import select_harness
from src.harness.claude_code import ClaudeCodeHarness
from src.harness.interface import TranscriptEntry
from src.infra.process_signature import process_start_signature

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"

POLL_INTERVAL_SECONDS = 10

# 未処理テキストがこの字数（正規化後）を超えたら片を切る。
CHAR_THRESHOLD = 7000

# tool_use/tool_resultの内容を片に収めるときの切り詰め上限。
TRUNCATE_CHARS = 300

# 見張り自身の登録タイムアウト（86400秒）の手前で自分から起こしにいく余裕。
WAIT_LIMIT_SECONDS = 86400 - 300

# メインの生死判定が連続でこの回数「死んでいる」を返したときだけ、確定して
# 終了処理（目印削除・tmux kill-session）に入る。psコマンドの一時的な失敗
# （システム負荷等でNoneが返る）だけで、不可逆な終了処理を走らせないため。
MAIN_DEAD_CONFIRM_POLLS = 3

# 記録役のコンテキストが伸び続けないよう、この片数をDONEで確定するたびに
# 記録役を立て直す（stop→start）。片の続きはcursor.jsonに残るため、立て
# 直しても読み取り位置は失われない。
CHUNKS_PER_RESTART = 20

# activityの境界(check_in/add_activity)判定対象のtool short_name。
_BOUNDARY_TOOLS = {"check_in", "add_activity"}

_DONE_RE = re.compile(r"\bDONE\s+(\d+)\b")

_NOOP_MESSAGE = "片なし。`DONE -` とだけ返せ"

_DEFAULT_CURSOR: dict = {
    "last_uuid": None,
    "byte_offset": 0,
    "activity_id_at_cursor": None,
    "pending": None,
    "unacked": [],
    "next_no": 1,
    "offset_lost": 0,
    "chunks_since_restart": 0,
    "restart_count": 0,
}


def run_dir_for(main_sid: str) -> Path:
    """main_sidに対応する実行ディレクトリを返す。

    `HookState.clear_session` が触るファイル名パターン（`*_<safe_sid>`、
    非再帰glob）とは重ならないため、SessionStartでのクリアの影響を受けない。
    """
    safe = main_sid.replace("/", "_")
    return HookState.BASE_DIR / "recorder_runs" / safe


def tmux_session_name(main_sid: str) -> str:
    """main_sidに対応するtmuxセッション名を返す。

    起動側（セッションの立ち上げ）と終了処理（kill-session）の両方が
    同じ規則を使う必要があるため、ここに一本化する。main_sidは切り詰めず
    全体を使う（先頭8文字だけでは衝突しうる）。tmuxはセッション名の中の
    `:`と`.`をターゲット指定（session:window.pane）の区切り文字として
    解釈するため、`_`に置き換える。
    """
    safe = main_sid.replace(":", "_").replace(".", "_")
    return f"calm-rec-{safe}"


# ===================================================================
# transcriptのバイト単位差分読み
# ===================================================================


@dataclass
class _Line:
    raw: dict
    entry: TranscriptEntry
    end_offset: int


def _read_lines(path: Path, start_offset: int) -> tuple[list[_Line], int]:
    """start_offsetから、改行まで読めた行だけを読む。

    書きかけの末尾行（改行未到達）はoffsetを進めず次回に回す。空行・
    JSONとして読めない行は読み飛ばすが、offsetはその分進める（読了扱い）。
    """
    if not path.exists():
        return [], start_offset
    with open(path, "rb") as f:
        f.seek(start_offset)
        data = f.read()
    if not data:
        return [], start_offset

    lines: list[_Line] = []
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        raw_bytes = data[pos:nl]
        pos = nl + 1
        stripped = raw_bytes.strip()
        if stripped:
            try:
                raw = json.loads(stripped.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict):
                lines.append(
                    _Line(raw=raw, entry=ClaudeCodeHarness.to_entry(raw), end_offset=start_offset + pos)
                )
    return lines, start_offset + pos


# 先頭行の完結性を確かめるのに読む量。実運用のtool_use入力を含む1行でも
# 収まる余裕を見た値（超える場合は「まだ改行に届いていない」側に倒す）。
_VALIDITY_PEEK_BYTES = 1_000_000


def _offset_looks_valid(path: Path, offset: int) -> bool:
    """offsetが本物の行境界を指しているらしいかを判定する。

    backfill（過去transcriptの書き換え。前方の行の長さが変わりうる）の後は、
    古いoffsetが別の行の途中を指してしまうことがある。ファイルが縮んだ・
    直前バイトが改行でない・先頭の完結行がJSONとして読めない、のいずれかで
    「怪しい」と判定する。
    """
    if not path.exists():
        return offset == 0
    size = path.stat().st_size
    if offset > size:
        return False
    if offset == 0:
        return True
    with open(path, "rb") as f:
        f.seek(offset - 1)
        prev = f.read(1)
        if prev != b"\n":
            return False
        rest = f.read(_VALIDITY_PEEK_BYTES)
    nl = rest.find(b"\n")
    if nl == -1:
        # 改行に届いていない＝書きかけの先頭。壊れているとは言えない。
        return True
    first_line = rest[:nl].strip()
    if not first_line:
        return True
    try:
        json.loads(first_line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return False
    return True


def _locate_offset_by_uuid(path: Path, target_uuid: str | None) -> int | None:
    """target_uuidを持つ行の直後のバイト位置を、全体を読み直して探す。

    見つからなければNone（呼び出し側はoffset_lostとして末尾から再開する）。
    """
    if target_uuid is None or not path.exists():
        return None
    lines, _ = _read_lines(path, 0)
    for line in lines:
        if line.raw.get("uuid") == target_uuid:
            return line.end_offset
    return None


def _read_diff(
    path: Path, byte_offset: int, last_uuid: str | None
) -> tuple[list[_Line], int, bool, str | None]:
    """cursorのbyte_offsetから差分を読む。

    Returns:
        (新規行, 新byte_offset, offset_lostが起きたか, offset_lost時の新last_uuid)。
        offset_lost時は戻り値の行は空。新byte_offsetは、ファイル全体を
        読み直したときの最後の完結行の直後（書きかけの末尾行の途中を
        指さない）。last_uuidもその位置と矛盾しない値へ更新する
        （更新しないと、次にbackfillでoffsetがずれたときも同じ古いuuidで
        探しにいき、見つからずこの経路を繰り返しうる）。offset_lostで
        なければ4つ目の要素はNone。

    # ponytail: 片が確定するまで、確定済みbyte_offsetから毎周期まるごと
    # 読み直す（ポーリング間でインメモリのバッファを持ち越さない）。
    # 単純さを優先した設計で、蓄積が大きい・周期が長いケースではI/Oが
    # 周期ごとに増える。実測で問題になったら周期内バッファ方式に変える。
    """
    offset = byte_offset
    if not _offset_looks_valid(path, offset):
        recovered = _locate_offset_by_uuid(path, last_uuid)
        if recovered is None:
            all_lines, end_offset = _read_lines(path, 0)
            new_last_uuid = _last_uuid_up_to(all_lines, end_offset)
            return [], end_offset, True, new_last_uuid
        offset = recovered
    lines, new_offset = _read_lines(path, offset)
    return lines, new_offset, False, None


def _last_uuid_up_to(lines: list[_Line], end_offset: int) -> str | None:
    """end_offset以下の行のうち、uuidを持つ最後の行のuuidを返す。"""
    result = None
    for line in lines:
        if line.end_offset > end_offset:
            break
        uuid = line.raw.get("uuid")
        if uuid:
            result = uuid
    return result


# ===================================================================
# 片への切り出し
# ===================================================================


def _is_chunkable(entry: TranscriptEntry) -> bool:
    if entry.kind not in ("user", "assistant"):
        return False
    if entry.is_meta:
        return False
    if entry.raw.get("isCompactSummary"):
        return False
    return True


def _find_boundary_index(chunkable: list[_Line]) -> int | None:
    """check_in/add_activity呼び出しが現れる最初のindexを返す。

    index 0（直前に何も溜まっていない）は境界として扱わない。そこで切ると
    空の片になってしまうため、その呼び出し自体を次の片の先頭に含める。
    """
    for i, line in enumerate(chunkable):
        if i == 0:
            continue
        for block in line.entry.content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if _is_calm_tool(name) and _extract_short_name(name) in _BOUNDARY_TOOLS:
                return i
    return None


def _normalize_line_text(entry: TranscriptEntry) -> str:
    parts: list[str] = []
    for block in entry.content:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "")
            input_json = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[tool_use {name}] {input_json[:TRUNCATE_CHARS]}")
        elif btype == "tool_result":
            content = block.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            parts.append(f"[tool_result] {str(content)[:TRUNCATE_CHARS]}")
    if not parts:
        return ""
    return f"[{entry.kind}] " + "\n".join(parts)


def _total_chars(chunkable: list[_Line]) -> int:
    return sum(len(_normalize_line_text(line.entry)) for line in chunkable)


def _resolve_db_path() -> str:
    return env_get("CALM_DB_PATH", str(DEFAULT_DB_PATH))


def _topic_candidates(db_path: str, activity_id: int | None) -> list[dict] | None:
    """activity_idに直接関連するtopicの{id, title}一覧を読み取り専用で引く。

    DB接続・クエリ自体が失敗した場合はNoneを返す（0件と区別するため）。
    """
    if activity_id is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            """
            SELECT dt.id AS topic_id, dt.title AS topic_title
            FROM relations_view rv
            JOIN discussion_topics dt ON dt.id = rv.target_id
            WHERE rv.source_type = 'activity' AND rv.source_id = ?
              AND rv.target_type = 'topic'
            ORDER BY dt.id
            """,
            (activity_id,),
        ).fetchall()
        return [{"id": r[0], "title": r[1]} for r in rows]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _format_topics(topics: list[dict] | None) -> str:
    if topics is None:
        return "取得失敗（DB接続エラー）"
    if not topics:
        return "0件"
    parts = [f"#{t['id']} {t['title']}" for t in topics]
    return f"{len(topics)}件 — " + " / ".join(parts)


def _render_chunk(
    no: int, activity_id: int | None, topics: list[dict] | None,
    start_uuid: str | None, end_uuid: str | None, cut_chunkable: list[_Line],
) -> str:
    header = [
        f"# 片 {no:04d}",
        "",
        f"- activity_id: {activity_id if activity_id is not None else '(未設定)'}",
        f"- topic候補: {_format_topics(topics)}",
        f"- 範囲: {start_uuid or '(先頭)'} 〜 {end_uuid or '(不明)'}",
        "",
        "---",
        "",
    ]
    body_parts = [t for line in cut_chunkable if (t := _normalize_line_text(line.entry))]
    return "\n".join(header) + "\n\n".join(body_parts) + "\n"


# ===================================================================
# cursor.json / ロック
# ===================================================================


def _load_cursor(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    cursor = dict(_DEFAULT_CURSOR)
    cursor.update(data)
    return cursor


def _write_json_atomic(path: Path, data: dict, *, indent: int | None = None) -> None:
    """dataをJSONとしてpathへアトミックに書く（同ディレクトリのtempfile→os.replace）。

    並行読み取りが書きかけの中身を掴むことはない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp_path, path)
    except OSError:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _acquire_lock(run_dir: Path):
    """run_dir/watch.lockをexclusive lockして返す。取れなければNone。

    見張りは実測上つねに1本のはず（前のターンのhookが生きたまま次のStop
    が来る）なので、取れないときは即座に諦める。
    """
    lock_path = run_dir / "watch.lock"
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# ===================================================================
# pendingの確定判定
# ===================================================================


def _find_done_numbers(text: str) -> set[int]:
    """last_assistant_messageに現れる `DONE <数字>` の値を全部集める。

    片なし応答の `DONE -` はここでは拾わない（そもそも `pending` が無い
    ときにしか送らない文言のため、確定判定の対象にはならない）。
    """
    return {int(m.group(1)) for m in _DONE_RE.finditer(text)}


def _advance_cursor_from_pending(cursor: dict, pending: dict) -> None:
    cursor["last_uuid"] = pending["end_uuid"]
    cursor["byte_offset"] = pending["end_offset"]
    cursor["activity_id_at_cursor"] = pending["end_activity_id"]


def _resolve_pending(cursor: dict, last_msg: str) -> str:
    """cursor["pending"]を直接書き換えつつ、'done'|'retry'|'unacked'を返す。"""
    pending = cursor["pending"]
    done_numbers = _find_done_numbers(last_msg)
    if pending["no"] in done_numbers:
        _advance_cursor_from_pending(cursor, pending)
        cursor["pending"] = None
        return "done"
    if pending.get("retries", 0) == 0:
        pending["retries"] = 1
        return "retry"
    cursor.setdefault("unacked", []).append(pending["no"])
    _advance_cursor_from_pending(cursor, pending)
    cursor["pending"] = None
    return "unacked"


def _emit_chunk_message(run_dir: Path, pending: dict) -> int:
    no = pending["no"]
    chunk_path = run_dir / "chunks" / f"{no:04d}.md"
    sys.stderr.write(
        f"片{no:04d}を処理せよ。Read {chunk_path}。"
        f"終えたら最終行に `DONE {no:04d}` と書いてターンを終えよ\n"
    )
    return 2


def _ensure_activity_id_at_cursor(cursor: dict, main_transcript: Path, cursor_path: Path) -> None:
    """activity_id_at_cursorが未設定のまま既読分がある場合、遡って初期化する。

    境界の無い最初の片のactivity_idを正しくするために要る（既読分の中に
    check_in/add_activityがあっても、activity_id_at_cursorが未設定のままだと
    その片は「未設定」表示になってしまう）。
    """
    if cursor.get("activity_id_at_cursor") is not None:
        return
    offset = cursor.get("byte_offset", 0)
    if not offset:
        return
    lines, _ = _read_lines(main_transcript, 0)
    prior_entries = [line.entry for line in lines if line.end_offset <= offset]
    activity_id = extract_last_activity_id(prior_entries)
    if activity_id is not None:
        cursor["activity_id_at_cursor"] = activity_id
        _write_json_atomic(cursor_path, cursor)


def _terminate(run_dir: Path, main_sid: str) -> None:
    remove_marker(main_sid)
    tmux_name = tmux_session_name(main_sid)
    try:
        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True, timeout=10)
    except Exception:
        pass


def _spawn_detached(calm_root: Path, run_dir: Path, log_name: str, args: list[str]) -> None:
    """`scripts/recorder.py`のサブコマンドを、切り離したプロセスとして起動する。

    tmux kill-sessionを伴うサブコマンド（stop・restart）を呼び出し元自身の
    プロセスから直接呼ぶと、呼び出し元（記録役のtmuxセッション内で動く見張り
    や、SessionStart hook）を巻き添えで終了・ブロックしうる。実際のコマンドは
    `start_new_session=True`で切り離した別プロセスに行わせ、本関数はその起動
    だけを行ってすぐ戻る。切り離しプロセスには呼び出し元のセッション環境変数
    が伝わらないため、必要な引数はargsで明示的に渡す。stdout/stderrは
    `run_dir/log_name`に追記し、失敗したときに手がかりを残す。
    """
    venv_python = calm_root / ".venv" / "bin" / "python"
    recorder_script = calm_root / "scripts" / "recorder.py"
    with open(run_dir / log_name, "a", encoding="utf-8") as log_fh:
        subprocess.Popen(
            [str(venv_python), str(recorder_script), *args],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh,
        )


def _spawn_detached_restart(
    calm_root: Path, run_dir: Path, main_sid: str, main_pid: int, main_transcript: Path
) -> None:
    """記録役の立て直し（stop→start）を、切り離したプロセスとして起動する。

    この関数は、まだ生きている記録役のtmuxセッション内（そのStop hookの
    実行中）から呼ばれる。stdout/stderrは`run_dir/restart.log`に追記する。
    """
    _spawn_detached(
        calm_root, run_dir, "restart.log",
        ["restart", "--session-id", main_sid, "--pid", str(main_pid), "--transcript", str(main_transcript)],
    )


def _spawn_detached_stop(calm_root: Path, run_dir: Path, main_sid: str) -> None:
    """古い記録役の停止を、切り離したプロセスとして起動する。

    `hooks.recorder_autostart_hook`のSessionStart hookから、/clear・resumeで
    不要になった古い記録役を止めるために呼ばれる。tmux kill-sessionの
    タイムアウトでSessionStart自体をブロックしないよう、`_spawn_detached_
    restart`と同じく切り離したプロセス（`scripts/recorder.py stop`）に行わ
    せる。stdout/stderrは`run_dir/autostart_stop.log`に追記する。
    """
    _spawn_detached(calm_root, run_dir, "autostart_stop.log", ["stop", "--session-id", main_sid])


def _read_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ===================================================================
# 本体
# ===================================================================


def _emit_new_chunk(
    run_dir: Path, cursor: dict, cursor_path: Path,
    lines: list[_Line], cut_chunkable: list[_Line], cut_end_offset: int,
) -> int:
    no = cursor["next_no"]
    cursor["next_no"] = no + 1

    end_uuid = _last_uuid_up_to(lines, cut_end_offset) or cursor.get("last_uuid")
    activity_id = extract_last_activity_id([line.entry for line in cut_chunkable])
    if activity_id is None:
        activity_id = cursor.get("activity_id_at_cursor")

    topics = _topic_candidates(_resolve_db_path(), activity_id)

    chunk_path = run_dir / "chunks" / f"{no:04d}.md"
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_path.write_text(
        _render_chunk(no, activity_id, topics, cursor.get("last_uuid"), end_uuid, cut_chunkable),
        encoding="utf-8",
    )

    cursor["pending"] = {
        "no": no,
        "end_uuid": end_uuid,
        "end_offset": cut_end_offset,
        "end_activity_id": activity_id,
        "retries": 0,
    }
    _write_json_atomic(cursor_path, cursor)
    return _emit_chunk_message(run_dir, cursor["pending"])


def _watch(run_dir: Path, hook_input: dict, *, sleep, now) -> int:
    run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    main_sid = run_data["main_sid"]
    main_pid = int(run_data["main_pid"])
    main_pid_started_at = run_data["main_pid_started_at"]
    main_transcript = Path(run_data["main_transcript"]).expanduser()

    cursor_path = run_dir / "cursor.json"
    cursor = _load_cursor(cursor_path)

    last_msg = hook_input.get("last_assistant_message") or ""

    if cursor.get("pending") is not None:
        outcome = _resolve_pending(cursor, last_msg)
        if outcome == "done":
            cursor["chunks_since_restart"] = cursor.get("chunks_since_restart", 0) + 1
        _write_json_atomic(cursor_path, cursor)
        if outcome == "retry":
            touch_marker(main_sid)
            return _emit_chunk_message(run_dir, cursor["pending"])
        if outcome == "done" and cursor["chunks_since_restart"] >= CHUNKS_PER_RESTART:
            cursor["chunks_since_restart"] = 0
            cursor["restart_count"] = cursor.get("restart_count", 0) + 1
            _write_json_atomic(cursor_path, cursor)
            _spawn_detached_restart(_project_root, run_dir, main_sid, main_pid, main_transcript)
            return 0

    _ensure_activity_id_at_cursor(cursor, main_transcript, cursor_path)

    claude_pid = _read_int_env("CLAUDE_PID")
    start = now()
    dead_poll_count = 0

    while True:
        if claude_pid is not None:
            try:
                os.kill(claude_pid, 0)
            except ProcessLookupError:
                return 0
            except PermissionError:
                pass

        if process_start_signature(main_pid) == main_pid_started_at:
            dead_poll_count = 0
        else:
            dead_poll_count += 1
        main_confirmed_dead = dead_poll_count >= MAIN_DEAD_CONFIRM_POLLS
        touch_marker(main_sid)

        lines, new_offset, lost, lost_uuid = _read_diff(
            main_transcript, cursor["byte_offset"], cursor["last_uuid"]
        )
        if lost:
            cursor["offset_lost"] = cursor.get("offset_lost", 0) + 1
            cursor["byte_offset"] = new_offset
            cursor["last_uuid"] = lost_uuid
            _write_json_atomic(cursor_path, cursor)
            lines = []

        chunkable = [line for line in lines if _is_chunkable(line.entry)]
        boundary_idx = _find_boundary_index(chunkable)

        if boundary_idx is not None:
            cut = chunkable[:boundary_idx]
            return _emit_new_chunk(run_dir, cursor, cursor_path, lines, cut, cut[-1].end_offset)

        if _total_chars(chunkable) >= CHAR_THRESHOLD:
            end_offset = lines[-1].end_offset if lines else cursor["byte_offset"]
            return _emit_new_chunk(run_dir, cursor, cursor_path, lines, chunkable, end_offset)

        if main_confirmed_dead:
            if chunkable:
                end_offset = lines[-1].end_offset if lines else cursor["byte_offset"]
                return _emit_new_chunk(run_dir, cursor, cursor_path, lines, chunkable, end_offset)
            _terminate(run_dir, main_sid)
            return 0

        if now() - start > WAIT_LIMIT_SECONDS:
            sys.stderr.write(_NOOP_MESSAGE + "\n")
            return 2

        sleep(POLL_INTERVAL_SECONDS)


def main(*, sleep=time.sleep, now=time.time) -> int:
    try:
        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        hook_input = select_harness(hook_event_name="Stop").read_hook_input()
        run_dir = Path(hook_input.get("cwd") or os.getcwd())
        if not (run_dir / "run.json").exists():
            return 0

        lock_fh = _acquire_lock(run_dir)
        if lock_fh is None:
            return 0
        try:
            return _watch(run_dir, hook_input, sleep=sleep, now=now)
        finally:
            lock_fh.close()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
