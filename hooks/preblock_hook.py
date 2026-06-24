"""PreToolUse hook: tool_input に内部 ID リテラル (`[MDLAT]#NNN` または英語フルワード
`log/decision/activity/material/topic #NNN`) が含まれていたら block する。

cc-memory 開発現場以外で内部 ID 形式の文字列を外部に出すケースは想定されないため、
tool 引数段階で機械的に止めることで AI 経由の漏出を防ぐ (scope A 方針)。

cc-memory project 内 (pyproject.toml の name が cc-memory) のみで有効。
`CC_MEMORY_LEAK_GUARD=off` 環境変数で緊急時に opt-out 可能。
allowlist tool (cc-memory 自身の MCP / Read 系 / harness 内部 tool) は素通し。
バックスラッシュエスケープ (`\\M#123`, `\\log #123`) は字義扱いで非 block。

検出時は `permissionDecision: "deny"` + 英文 reason を返し、
発火ログを `~/.cc-memory/logs/preblock_hook.jsonl` に append する (書き込み失敗時は silent)。
"""
import json
import os
import pathlib
import sys
import tomllib
from datetime import datetime, timezone

# プラグイン経由で `${CLAUDE_PLUGIN_ROOT}` を cwd として起動されるため、
# 同居ソースを import path に通す。
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.services.internal_id_patterns import (  # noqa: E402
    RAW_CITE_CODE_PATTERN,
    RAW_CITE_FULLWORD_PATTERN,
)

ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "mcp__plugin_claude-code-memory_cc-memory__",
)

ALLOWLIST_EXACT: frozenset[str] = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
        "TaskCreate",
        "TaskGet",
        "TaskUpdate",
        "TaskList",
        "TaskStop",
        "TaskOutput",
        "Skill",
        "ToolSearch",
        "ScheduleWakeup",
        "EnterPlanMode",
        "ExitPlanMode",
        "Monitor",
        "NotebookEdit",
        "PushNotification",
    }
)

LOG_PATH = pathlib.Path.home() / ".cc-memory" / "logs" / "preblock_hook.jsonl"

# pyproject.toml `[project].name` がこのいずれかに一致したら cc-memory project と判定する。
# 実プロジェクトの name は "claude-code-memory" だが、過去ドキュメントや一部 fixture が
# "cc-memory" 表記を持つので両方を受け入れる。
_PROJECT_NAMES = ("cc-memory", "claude-code-memory")


def _is_allowed(tool_name: str) -> bool:
    if tool_name in ALLOWLIST_EXACT:
        return True
    return any(tool_name.startswith(p) for p in ALLOWLIST_PREFIXES)


def _scan_text_for_literals(text: str) -> list[str]:
    """1 個の文字列に対し code + fullword 両方を検出し、マッチした raw 文字列を返す。

    バックスラッシュエスケープ (`\\M#123`, `\\log #123`) は事前に取り除いた
    擬似テキストで scan する: hook の責務は「AI が tool に渡そうとした文字列に
    内部 ID 形式が現れたら止める」であり、エスケープ表現で書かれた箇所は
    字義扱いとして block 対象から外す。
    """
    matches: list[str] = []
    # エスケープされた箇所を空白で潰してから scan する。
    # `\X#NNN` / `\log #NNN` 等の直後文字数は最大 16 + 1 程度なので十分な余白を取る。
    sanitized = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            # バックスラッシュとその直後 1 文字をスキップする (字義扱い)
            sanitized.append(" ")  # boundary を保つため空白で置換
            sanitized.append(" ")
            i += 2
            continue
        sanitized.append(text[i])
        i += 1
    haystack = "".join(sanitized)
    for m in RAW_CITE_CODE_PATTERN.finditer(haystack):
        matches.append(m.group(0))
    for m in RAW_CITE_FULLWORD_PATTERN.finditer(haystack):
        matches.append(m.group(0))
    return matches


def _scan_tool_input(value) -> list[dict]:
    """tool_input を再帰的に走査し、検出されたリテラルと jq 風 field path を返す。

    Returns:
        [{"match": "M#123", "field": "command"}, ...]
        field は tool_input の中での出現位置 (dict キーは ".key"、list は "[i]")。
        root に直接 string が来た場合は field は "" (空文字)。
    """
    matches: list[dict] = []

    def walk(v, path: str) -> None:
        if isinstance(v, str):
            for literal in _scan_text_for_literals(v):
                matches.append({"match": literal, "field": path})
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")

    walk(value, "")
    return matches


def _is_in_cc_memory_project() -> bool:
    """cwd から上方向に pyproject.toml を探索して cc-memory project か判定する。

    `[project].name` を tomllib で厳密パースする。コメント行や別フィールドに
    たまたま `name = "cc-memory"` を含むケースで誤判定しないよう、文字列の
    部分一致ではなくパース済みの構造から取り出す。
    """
    cwd = pathlib.Path.cwd()
    for p in (cwd, *cwd.parents):
        pj = p / "pyproject.toml"
        if not pj.exists():
            continue
        try:
            with pj.open("rb") as fp:
                data = tomllib.load(fp)
        except (OSError, tomllib.TOMLDecodeError):
            return False
        project = data.get("project")
        if not isinstance(project, dict):
            return False
        return project.get("name") in _PROJECT_NAMES
    return False


def _log_event(record: dict) -> None:
    """発火ログを 1 行 JSON で append する。失敗時は何もしない (block 動作は継続)。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print("{}")
            return

        event = json.loads(raw)
        tool_name = event.get("tool_name") or ""
        tool_input = event.get("tool_input") or {}

        # opt-out: 緊急時の脱出経路
        if os.environ.get("CC_MEMORY_LEAK_GUARD", "").lower() == "off":
            print("{}")
            return

        # allowlist: scan 不要 tool は素通し
        # cwd 判定より先に行うことで、Read/Grep/Glob 等の頻出 tool で
        # 毎回 pyproject.toml を読み直す I/O を回避する。
        if _is_allowed(tool_name):
            print("{}")
            return

        # cwd 判定: cc-memory project 内のみ有効
        if not _is_in_cc_memory_project():
            print("{}")
            return

        matches = _scan_tool_input(tool_input)
        if not matches:
            print("{}")
            return

        matched_literals = [m["match"] for m in matches]
        matched_fields = sorted({m["field"] for m in matches if m["field"]})

        reason = (
            f"Internal ID literal detected in tool_input: {matched_literals}. "
            "These IDs are cc-memory internal references and must not leak "
            "outside the cc-memory development context. Use a natural language "
            "reference (e.g. entity title) instead, or escape with a backslash "
            "prefix to indicate a literal (e.g. '\\M#123', '\\log #456')."
        )

        _log_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool_name": tool_name,
                "decision": "block",
                "matches": matched_literals,
                "tool_input_field": matched_fields,
                "cwd": str(pathlib.Path.cwd()),
                "session_id": event.get("session_id"),
            }
        )

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(output, ensure_ascii=False))

    except Exception as e:
        # hook 自体の不具合で全 tool を止めないため、例外時は素通し + stderr 通知
        print(f"preblock_hook.py error: {e}", file=sys.stderr)
        print("{}")


if __name__ == "__main__":
    main()
