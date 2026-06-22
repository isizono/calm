"""PostToolUse hook: cc-memory tool_result の生 X#NNN を {{cite:X#NNN}} に変換する。

stdin から JSON (tool_name / tool_response / cwd / session_id / transcript_path) を読み、
cc-memory tool の場合に tool_response.content をサニタイズして
hookSpecificOutput.updatedToolOutput で stdout へ返す。

opt-out:
- 環境変数 `CC_MEMORY_SANITIZE_DISABLE=1` set: 即 exit 0
- cwd が cc-memory リポジトリ内 (pyproject.toml `[project].name == 'claude-code-memory'`
  を上方向探索で検出): 即 exit 0

dangling (target 不在) は `[deleted X#NNN]` 形式へ変換する。
sanitize_log には 1 行 INSERT (write conn 別張り、WAL モード)。
例外時は stderr 警告 + sanitize_log に failure_reason 記録 + exit 0 (非ブロック)。
"""
import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path

from hooks.hook_transcript import _is_cc_memory_tool
from src.services.citations_pure import (
    TYPE_CODE_TO_NAME,
    _RAW_CITE_PATTERN,
    check_target_exists,
    convert_raw_to_cite,
)

DEFAULT_DB_PATH = Path.home() / ".claude" / ".claude-code-memory" / "discussion.db"
_REPO_PROJECT_NAME = "claude-code-memory"


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


def _replace_dangling_in_line(line: str) -> tuple[str, int]:
    """1 行内の生 `X#NNN` を `[deleted X#NNN]` に置換する。

    インラインバッククォート / 既存 `{{cite:...}}` / エスケープ `\\X#NNN` ・
    `\\{{cite:...}}` 内はスキップする。convert_raw_to_cite と同じスキップ規則を
    意図的に複製する: pure 関数 (citations_pure._convert_line_raw_to_cite) は
    本 PR スコープ外で変更しない方針のため、dangling → `[deleted]` 変換は hook
    側で post-process パスとして独立に実装する。
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


def convert_dangling_to_deleted(text: str) -> tuple[str, int]:
    """convert_raw_to_cite 通過後の残存生 `X#NNN` を `[deleted X#NNN]` に変換する。

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


def _resolve_db_path() -> str:
    return os.environ.get("CC_MEMORY_DB_PATH", str(DEFAULT_DB_PATH))


def _log_sanitize_event(
    db_path: str,
    *,
    session_id: str | None,
    transcript_path: str | None,
    occurrence_count: int,
    sanitized_count: int,
    failed_count: int,
    failure_reason: str | None,
) -> None:
    """sanitize_log に 1 行 INSERT する。write conn を別張りして close する。

    sanitize_log の CHECK 制約 (session_id IS NOT NULL OR transcript_path IS NOT NULL) を
    満たさない呼び出しはスキップする。INSERT 自体の失敗は stderr に出すのみで例外を伝播しない。
    """
    if not session_id and not transcript_path:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                INSERT INTO sanitize_log (
                    session_id, transcript_path, hook_kind,
                    occurrence_count, sanitized_count, failed_count, failure_reason
                ) VALUES (?, ?, 'post_tool_use', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    transcript_path,
                    occurrence_count,
                    sanitized_count,
                    failed_count,
                    failure_reason,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        sys.stderr.write(
            f"[sanitize_tool_result_hook] sanitize_log write failed: {exc}\n"
        )


def _sanitize_content(content: str, db_path: str) -> tuple[str, dict]:
    """content を read-only conn で sanitize する。

    sqlite3.Connection のコンテキストマネージャは `__exit__` で commit/rollback のみ
    呼び `close()` は呼ばないため、try/finally で明示的に閉じて fd リークを防ぐ。

    Returns (sanitized_text, counters)。counters は convert_raw_to_cite 由来 +
    `deleted_count` (dangling → [deleted] に変換した件数)。
    """
    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def validator(target_type: str, target_id: int) -> bool:
            return check_target_exists(ro_conn, target_type, target_id)

        converted, counters = convert_raw_to_cite(content, target_validator=validator)
    finally:
        ro_conn.close()
    deleted_text, deleted_count = convert_dangling_to_deleted(converted)
    counters["deleted_count"] = deleted_count
    return deleted_text, counters


def main() -> int:
    db_path = _resolve_db_path()
    session_id: str | None = None
    transcript_path: str | None = None
    try:
        if os.environ.get("CC_MEMORY_SANITIZE_DISABLE") == "1":
            return 0
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
        session_id = data.get("session_id")
        transcript_path = data.get("transcript_path")
        tool_name = data.get("tool_name", "")
        if not _is_cc_memory_tool(tool_name):
            return 0
        cwd = data.get("cwd", "")
        if _is_in_cc_memory_repo(cwd):
            return 0
        tool_response = data.get("tool_response", {})
        if isinstance(tool_response, dict):
            content = tool_response.get("content")
        else:
            content = tool_response
        if not isinstance(content, str):
            return 0
        sanitized_text, counters = _sanitize_content(content, db_path)
        if isinstance(tool_response, dict):
            updated_output = {**tool_response, "content": sanitized_text}
        else:
            updated_output = {"content": sanitized_text}
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated_output,
            }
        }
        print(json.dumps(output))
        # occurrence_count = 検出した全 X#NNN (sanitized + dangling + 全 skip カテゴリ)。
        # コードブロック等で意図的に skip した件数も「検出件数」に含める (運用監視の
        # 完全性のため)。
        # sanitized_count = 実際に {{cite:}} に変換した件数。
        # failed_count は例外による失敗のみを表現する (本 try ブロック内は成功パスなので
        # 常に 0)。dangling は正常変換 ([deleted X#NNN]) として扱い failed には含めない。
        # dangling 件数は occurrence と sanitized の差から算出可能 (但し skip カテゴリ
        # との内訳までは schema 上区別できない: 受容したトレードオフ)。
        occurrence_count = (
            counters.get("sanitized_count", 0)
            + counters.get("skipped_dangling", 0)
            + counters.get("skipped_in_codeblock", 0)
            + counters.get("skipped_in_existing_cite", 0)
            + counters.get("skipped_escape", 0)
        )
        _log_sanitize_event(
            db_path,
            session_id=session_id,
            transcript_path=transcript_path,
            occurrence_count=occurrence_count,
            sanitized_count=counters.get("sanitized_count", 0),
            failed_count=0,
            failure_reason=None,
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"[sanitize_tool_result_hook] {exc}\n")
        try:
            # 例外時は failure_reason に発生内容を記録する。failed_count は CHECK 制約
            # (sanitized + failed <= occurrence) を満たすため 0 に固定する (count 系は
            # 例外前に確定していない可能性がある)。
            _log_sanitize_event(
                db_path,
                session_id=session_id,
                transcript_path=transcript_path,
                occurrence_count=0,
                sanitized_count=0,
                failed_count=0,
                failure_reason=str(exc),
            )
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
