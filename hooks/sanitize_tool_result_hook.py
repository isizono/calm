"""PostToolUse hook: cc-memory tool_result の生 X#NNN を {{cite:X#NNN}} に変換する。

stdin から JSON (tool_name / tool_response / cwd / session_id / transcript_path) を読み、
cc-memory tool の場合に tool_response.content をサニタイズして
hookSpecificOutput.updatedToolOutput で stdout へ返す。updatedToolOutput の型
(dict か content block 配列そのものか) は tool_response の元の型に合わせて出し分ける
(理由は main() 内の分岐直上コメントを参照)。

opt-out:
- 環境変数 `CC_MEMORY_SANITIZE_DISABLE=1` set: 即 exit 0
- cwd が cc-memory リポジトリ内 (pyproject.toml `[project].name == 'claude-code-memory'`
  を上方向探索で検出): 即 exit 0

dangling (target 不在) は `[deleted X#NNN]` 形式へ変換する。
本文が実際に変化した場合のみ citation_event_log (source='transcript_post_tool_use')
に 1 行 INSERT する (write conn 別張り、WAL モード)。変化なし (occurrence 0 件) の
呼び出しは記録しない (write 経路の apply_raw_to_cite_conversion と同じ「変化なし
イベントは記録しない」規約に合わせる)。
例外時は stderr 警告 + before_text/after_text 空文字・verification_result NULL の
failure イベントを citation_event_log に記録 + exit 0 (非ブロック)。
"""
import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.citation_event_log import log_event
from hooks.hook_transcript import _is_cc_memory_tool
from src.services.citations_pure import (
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


def _resolve_db_path() -> str:
    return os.environ.get("CC_MEMORY_DB_PATH", str(DEFAULT_DB_PATH))


def _sanitize_content(content: str, db_path: str) -> tuple[str, dict]:
    """content を read-only conn で sanitize する。

    sqlite3.Connection のコンテキストマネージャは `__exit__` で commit/rollback のみ
    呼び `close()` は呼ばないため、try/finally で明示的に閉じて fd リークを防ぐ。

    Returns (sanitized_text, counters)。counters は convert_raw_to_cite 由来 +
    `deleted_count` (dangling → [deleted] に変換した件数)。dangling → [deleted]
    への確定書き換えは convert_raw_to_cite 内部で完結しているため、後段の
    追加変換は行わない。
    """
    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def validator(target_type: str, target_id: int) -> bool:
            return check_target_exists(ro_conn, target_type, target_id)

        converted, counters = convert_raw_to_cite(content, target_validator=validator)
    finally:
        ro_conn.close()
    counters["deleted_count"] = counters["skipped_dangling"]
    return converted, counters


def main() -> int:
    db_path = _resolve_db_path()
    session_id: str | None = None
    transcript_path: str | None = None
    tool_name: str | None = None
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
        content_block = [{"type": "text", "text": sanitized_text}]
        # updatedToolOutput は tool_response の元の型構造をそのまま保って返す必要がある。
        # tool_response が dict の場合、CLI 側は content 以外のフィールドも維持された
        # dict を前提にしている (test_case_10_official_hook_output_schema)。tool_response
        # が非 dict (生文字列等) の場合は、updatedToolOutput 自体が content block 配列と
        # してそのまま扱われる (test_case_14_non_dict_tool_response_yields_unwrapped_content_block)。
        # ここで型を dict/配列のどちらかに統一すると、統一しなかった側の経路で content が
        # 二重にラップされ、CLI 側のサイズ計算処理がクラッシュする。tool_response の内部
        # 構造自体はハーネス依存で公開仕様が無い (hooks/relay_monitor_watch_hook.py:23-24
        # 参照) ため、この非対称性は暗黙の契約として扱い、統一しないこと。
        if isinstance(tool_response, dict):
            updated_output = {**tool_response, "content": content_block}
        else:
            updated_output = content_block
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated_output,
            }
        }
        print(json.dumps(output))
        # 本文が実際に変化した (sanitized または dangling→[deleted] 変換が1件以上
        # 発生した) 場合のみイベントを記録する。変化なしの呼び出しは記録しない
        # (write 経路の apply_raw_to_cite_conversion と同じ規約)。
        if sanitized_text != content:
            deleted_count = counters.get("deleted_count", 0)
            verification_result = "dangling" if deleted_count else "exists"
            log_event(
                db_path,
                source="transcript_post_tool_use",
                hook_label="sanitize_tool_result_hook",
                session_id=session_id,
                transcript_path=transcript_path,
                tool_name=tool_name,
                before_text=content,
                after_text=sanitized_text,
                verification_result=verification_result,
                extra={
                    "sanitized_count": counters.get("sanitized_count", 0),
                    "deleted_count": deleted_count,
                    "skipped_dangling": counters.get("skipped_dangling", 0),
                    "skipped_in_codeblock": counters.get("skipped_in_codeblock", 0),
                    "skipped_in_existing_cite": counters.get("skipped_in_existing_cite", 0),
                    "skipped_escape": counters.get("skipped_escape", 0),
                },
            )
        return 0
    except Exception as exc:
        sys.stderr.write(f"[sanitize_tool_result_hook] {exc}\n")
        try:
            # 例外時は before_text/after_text を空文字・verification_result を NULL
            # にした failure イベントを記録する (NOT NULL 制約は空文字で満たす)。
            log_event(
                db_path,
                source="transcript_post_tool_use",
                hook_label="sanitize_tool_result_hook",
                session_id=session_id,
                transcript_path=transcript_path,
                tool_name=tool_name,
                before_text="",
                after_text="",
                verification_result=None,
                extra={"error": str(exc)},
            )
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
