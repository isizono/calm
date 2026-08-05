"""SessionStart hook: 過去 transcript の差分 backfill sanitize。

stdin から JSON (session_id / transcript_path / cwd / source) を読み、
transcript .jsonl 中の cc-memory tool_result から生 X#NNN を {{cite:X#NNN}} に変換し、
target 不在は [deleted X#NNN] に変換する。

書き戻し方式: atomic rename + backup + mtime 再確認。
書き込み直前に mtime が変化していれば harness が並行 append 中とみなして書き戻しを中断、
citation_event_log (source='transcript_session_start_backfill') に failure イベントを
記録し sanitize_offset は据え置きで次回再試行する。
同一セッションで連続 3 回失敗したら以降の SessionStart はスキップする (ループ防止)。
書き戻し成功時は、変化した tool_result block 単位で citation_event_log に 1 行ずつ
INSERT する (write 経路の apply_raw_to_cite_conversion と同じ「field/block 単位で
1 イベント」規約)。

opt-out:
- 環境変数 CC_MEMORY_SANITIZE_DISABLE=1: 即 exit 0
- cwd が cc-memory リポジトリ内 (pyproject.toml [project].name == 'claude-code-memory'
  を上方向探索で検出): 即 exit 0

例外時は stderr 警告 + citation_event_log failure イベント記録 + exit 0 (Claude Code 起動非ブロック)。
"""
import json
import os
import shutil
import sqlite3
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

# プロジェクトルートを path に追加
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.citation_event_log import log_event, log_events_batch
from hooks.hook_state import HookState
from hooks.hook_transcript import _is_cc_memory_tool
from src.services.citations_pure import (
    TYPE_CODE_TO_NAME,
    _RAW_CITE_PATTERN,
    check_target_exists,
    convert_raw_to_cite,
)

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
_REPO_PROJECT_NAME = "claude-code-memory"
_MAX_CONSECUTIVE_FAILURES = 3


# ---------------------------------------------------------------------------
# opt-out: cwd が cc-memory リポジトリ内か判定
# ---------------------------------------------------------------------------


def _is_in_cc_memory_repo(cwd: str | None) -> bool:
    """cwd から上方向に pyproject.toml を探し name=='claude-code-memory' なら True。

    最初に見つかった pyproject.toml の name が一致しない場合は False (別プロジェクト)。
    pyproject.toml が見つからない / 読めない場合は False。
    """
    if not cwd:
        return False
    try:
        start = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for parent in (start, *start.parents):
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        try:
            with open(candidate, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return False
        name = data.get("project", {}).get("name")
        return name == _REPO_PROJECT_NAME
    return False


# ---------------------------------------------------------------------------
# dangling target → [deleted X#NNN] 変換
# convert_raw_to_cite が pure 層の責務でない部分なので hook 側に閉じ込める。
# PR-c (PostToolUse hook) と同型の skip 規則。
# ---------------------------------------------------------------------------


def _replace_dangling_in_line(line: str) -> tuple[str, int]:
    """1 行内の残存生 X#NNN を [deleted X#NNN] に置換する。

    インラインバッククォート / 既存 {{cite:...}} / エスケープ \\X#NNN ・
    \\{{cite:...}} 内はスキップ (convert_raw_to_cite と同じ skip 規則)。
    """
    out: list[str] = []
    i = 0
    n = len(line)
    deleted = 0
    while i < n:
        ch = line[i]
        if ch == "`":
            close = line.find("`", i + 1)
            if close == -1:
                out.append(line[i:])
                break
            out.append(line[i : close + 1])
            i = close + 1
            continue
        if line[i : i + 7] == "{{cite:":
            end = line.find("}}", i + 7)
            if end == -1:
                out.append(ch)
                i += 1
                continue
            out.append(line[i : end + 2])
            i = end + 2
            continue
        if ch == "\\":
            if line[i + 1 : i + 3] == "{{":
                end = line.find("}}", i + 1)
                if end == -1:
                    out.append(ch)
                    i += 1
                    continue
                out.append(line[i : end + 2])
                i = end + 2
                continue
            tail = line[i + 1 : i + 2]
            if tail in TYPE_CODE_TO_NAME:
                m = _RAW_CITE_PATTERN.match(line, i + 1)
                if m and m.start() == i + 1:
                    out.append(line[i : m.end()])
                    i = m.end()
                    continue
            out.append(ch)
            i += 1
            continue
        m = _RAW_CITE_PATTERN.match(line, i)
        if m:
            code = m.group(1)
            target_id = m.group(2)
            out.append(f"[deleted {code}#{target_id}]")
            deleted += 1
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out), deleted


def _convert_dangling_to_deleted(text: str) -> tuple[str, int]:
    """convert_raw_to_cite 通過後の残存生 X#NNN を [deleted X#NNN] に変換する。

    フェンスコードブロック内はスキップ (convert_raw_to_cite の skip 規則と同型)。
    """
    out_lines: list[str] = []
    in_fence = False
    total = 0
    for raw_line in text.split("\n"):
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(raw_line)
            continue
        if in_fence:
            out_lines.append(raw_line)
            continue
        new_line, count = _replace_dangling_in_line(raw_line)
        total += count
        out_lines.append(new_line)
    return "\n".join(out_lines), total


# ---------------------------------------------------------------------------
# tool_result.content の sanitize
# ---------------------------------------------------------------------------


def _sanitize_text(text: str, ro_conn: sqlite3.Connection) -> tuple[str, dict]:
    """text を sanitize し (新 text, 統計) を返す。"""
    def validator(target_type: str, target_id: int) -> bool:
        return check_target_exists(ro_conn, target_type, target_id)
    converted, counters = convert_raw_to_cite(text, target_validator=validator)
    deleted_text, dangling_count = _convert_dangling_to_deleted(converted)
    sanitized = counters["sanitized_count"]
    skipped = (
        counters["skipped_in_codeblock"]
        + counters["skipped_in_existing_cite"]
        + counters["skipped_escape"]
    )
    occurrence = sanitized + dangling_count + skipped
    return deleted_text, {
        "sanitized": sanitized,
        "dangling": dangling_count,
        "occurrence": occurrence,
    }


def _sanitize_tool_result_content(
    content: Any, ro_conn: sqlite3.Connection
) -> tuple[Any, dict]:
    """tool_result.content (str or list of text blocks) を sanitize する。"""
    stats = {"sanitized": 0, "dangling": 0, "occurrence": 0}
    if isinstance(content, str):
        new_text, sub = _sanitize_text(content, ro_conn)
        return new_text, sub
    if isinstance(content, list):
        new_list = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                new_text, sub = _sanitize_text(item.get("text", ""), ro_conn)
                new_list.append({**item, "text": new_text})
                for k in stats:
                    stats[k] += sub[k]
            else:
                new_list.append(item)
        return new_list, stats
    return content, stats


# ---------------------------------------------------------------------------
# transcript scan + 書き換え
# ---------------------------------------------------------------------------


def _parse_transcript_lines(raw_bytes: bytes) -> list[dict]:
    """raw bytes を行単位に分割し、各行の byte 位置 / 元 bytes / parse 済 JSON を返す。"""
    out: list[dict] = []
    pos = 0
    parts = raw_bytes.split(b"\n")
    for i, line_bytes in enumerate(parts):
        json_obj = None
        line_str = line_bytes.decode("utf-8", errors="replace").strip()
        if line_str:
            try:
                json_obj = json.loads(line_str)
            except json.JSONDecodeError:
                pass
        out.append({"start": pos, "bytes": line_bytes, "json": json_obj})
        pos += len(line_bytes)
        if i < len(parts) - 1:
            pos += 1
    return out


def _build_tool_use_id_map(lines: list[dict]) -> dict[str, str]:
    """全 entries から tool_use_id → tool_name の map を構築する。"""
    mp: dict[str, str] = {}
    for line_info in lines:
        entry = line_info["json"]
        if not entry or entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            use_id = block.get("id", "")
            name = block.get("name", "")
            if use_id:
                mp[use_id] = name
    return mp


def _stringify_block_content(content: Any) -> str:
    """tool_result.content (str or list of blocks) を citation_event_log の

    before_text/after_text (TEXT NOT NULL) に格納できる文字列へ変換する。
    str はそのまま、list はそのまま JSON シリアライズする。
    """
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _sanitize_transcript_bytes(
    raw_bytes: bytes,
    offset: int,
    ro_conn: sqlite3.Connection,
) -> tuple[bytes, dict, bool, list[dict]]:
    """transcript bytes を読み offset 以降の cc-memory tool_result を sanitize して再構築。

    offset 未満の行は元バイト列のままパススルー (byte-perfect 維持)。
    Returns: (new_bytes, stats, modified, events)
    events は変化した tool_result block 単位のリスト
    ({tool_name, before_text, after_text, verification_result, stats})。
    """
    lines = _parse_transcript_lines(raw_bytes)
    tool_name_map = _build_tool_use_id_map(lines)

    stats = {"sanitized": 0, "dangling": 0, "occurrence": 0}
    out_chunks: list[bytes] = []
    modified = False
    events: list[dict] = []

    for line_info in lines:
        if line_info["start"] < offset:
            out_chunks.append(line_info["bytes"])
            continue
        entry = line_info["json"]
        if entry is None or entry.get("type") != "user":
            out_chunks.append(line_info["bytes"])
            continue
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            out_chunks.append(line_info["bytes"])
            continue

        new_blocks: list[Any] = []
        line_modified = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                new_blocks.append(block)
                continue
            tool_use_id = block.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_use_id, "")
            if not _is_cc_memory_tool(tool_name):
                new_blocks.append(block)
                continue
            block_content = block.get("content", "")
            new_content, sub_stats = _sanitize_tool_result_content(block_content, ro_conn)
            for k in stats:
                stats[k] += sub_stats[k]
            if new_content != block_content:
                new_blocks.append({**block, "content": new_content})
                line_modified = True
                events.append(
                    {
                        "tool_name": tool_name,
                        "before_text": _stringify_block_content(block_content),
                        "after_text": _stringify_block_content(new_content),
                        "verification_result": (
                            "dangling" if sub_stats["dangling"] else "exists"
                        ),
                        "stats": dict(sub_stats),
                    }
                )
            else:
                new_blocks.append(block)

        if line_modified:
            new_entry = {
                **entry,
                "message": {**entry["message"], "content": new_blocks},
            }
            out_chunks.append(json.dumps(new_entry, ensure_ascii=False).encode("utf-8"))
            modified = True
        else:
            out_chunks.append(line_info["bytes"])

    new_bytes = b"\n".join(out_chunks)
    return new_bytes, stats, modified, events


# ---------------------------------------------------------------------------
# atomic rename + backup で transcript を書き戻す
# ---------------------------------------------------------------------------


def _write_back_transcript(
    transcript_path: Path, new_bytes: bytes, original_mtime: float, now_ts: int
) -> str | None:
    """atomic rename + backup で transcript を書き戻す。

    成功時 None、失敗時 failure_reason 文字列を返す。
    rename 直前に mtime を再確認し、変化していれば harness 並行 append とみなして
    書き戻しを中断する (harness_race)。
    """
    tmp_path = transcript_path.with_suffix(transcript_path.suffix + ".tmp")
    backup_path = transcript_path.with_suffix(
        transcript_path.suffix + f".bak-{now_ts}"
    )

    try:
        with open(tmp_path, "wb") as f:
            f.write(new_bytes)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        return f"io_error: {exc}"

    try:
        current_mtime = transcript_path.stat().st_mtime
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        return f"io_error: {exc}"

    if current_mtime != original_mtime:
        tmp_path.unlink(missing_ok=True)
        return "harness_race"

    try:
        shutil.copy2(transcript_path, backup_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        return f"io_error: {exc}"

    try:
        os.replace(tmp_path, transcript_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        return f"rename_failed: {exc}"

    try:
        backup_path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# db path
# ---------------------------------------------------------------------------


def _resolve_db_path() -> str:
    return os.environ.get("CC_MEMORY_DB_PATH", str(DEFAULT_DB_PATH))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    db_path = _resolve_db_path()
    session_id: str | None = None
    transcript_path: str | None = None
    state: HookState | None = None
    failure_count = 0

    try:
        if os.environ.get("CC_MEMORY_SANITIZE_DISABLE") == "1":
            return 0

        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)

        session_id = data.get("session_id") or None
        transcript_path = data.get("transcript_path") or None
        cwd = data.get("cwd", "")

        if not transcript_path:
            return 0
        if _is_in_cc_memory_repo(cwd):
            return 0

        path = Path(transcript_path).expanduser()
        if not path.exists():
            return 0

        state = HookState(session_id) if session_id else None
        failure_count = state.get_sanitize_failure_count() if state else 0
        if failure_count >= _MAX_CONSECUTIVE_FAILURES:
            return 0

        offset = state.get_sanitize_offset() if state else 0

        try:
            stat_info = path.stat()
        except OSError:
            return 0
        original_mtime = stat_info.st_mtime
        file_size = stat_info.st_size

        if offset > file_size:
            offset = 0

        raw_bytes = path.read_bytes()

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as ro_conn:
            new_bytes, stats, modified, events = _sanitize_transcript_bytes(
                raw_bytes, offset, ro_conn
            )

        failure_reason: str | None = None
        if modified:
            failure_reason = _write_back_transcript(
                path, new_bytes, original_mtime, int(time.time())
            )

        if failure_reason is None:
            # 書き戻しが成功した (または変化がそもそも無かった) 場合のみ、変化した
            # block 単位で citation_event_log にイベントを記録する。変化が無かった
            # 呼び出し (events == []) は何も記録しない。
            if events:
                log_events_batch(
                    db_path,
                    source="transcript_session_start_backfill",
                    hook_label="sanitize_backfill_hook",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    events=events,
                )
            if state:
                new_offset = len(new_bytes) if modified else file_size
                state.set_sanitize_offset(new_offset)
                state.set_sanitize_failure_count(0)
        else:
            # 書き戻し失敗時は個々の block イベントを記録せず (実際に永続化されて
            # いないため)、failure イベント1件のみを記録する。
            log_event(
                db_path,
                source="transcript_session_start_backfill",
                hook_label="sanitize_backfill_hook",
                session_id=session_id,
                transcript_path=transcript_path,
                tool_name=None,
                before_text="",
                after_text="",
                verification_result=None,
                extra={"failure_reason": failure_reason, "stats": stats, "event_count": len(events)},
            )
            if state:
                state.set_sanitize_failure_count(failure_count + 1)

        return 0

    except Exception as exc:
        sys.stderr.write(f"[sanitize_backfill_hook] {exc}\n")
        try:
            log_event(
                db_path,
                source="transcript_session_start_backfill",
                hook_label="sanitize_backfill_hook",
                session_id=session_id,
                transcript_path=transcript_path,
                tool_name=None,
                before_text="",
                after_text="",
                verification_result=None,
                extra={"error": str(exc)},
            )
        except Exception:
            pass
        # スキャン / sanitize フェーズの例外も連続失敗カウンタに積む。
        # 毎セッションで同じ例外が出続けるケース (DB スキーマ不一致等) で
        # _MAX_CONSECUTIVE_FAILURES に達したら以降の SessionStart をスキップしループを止める。
        if state is not None:
            try:
                state.set_sanitize_failure_count(failure_count + 1)
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
