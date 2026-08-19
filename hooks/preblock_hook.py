"""PreToolUse hook: tool_input に内部 ID リテラル (`[MDLAT]#NNN` または英語フルワード
`log/decision/activity/material/topic #NNN`) が含まれていたら block する。

cc-memory 開発現場以外で内部 ID 形式の文字列を外部に出すケースは想定されないため、
tool 引数段階で機械的に止めることで AI 経由の漏出を防ぐ (scope A 方針)。

cc-memory (calm) project 内 (pyproject.toml の `[project].name` が `_PROJECT_NAMES` のいずれかに一致) のみで有効。
`CALM_LEAK_GUARD=off` 環境変数で緊急時に opt-out 可能。
allowlist tool (cc-memory 自身の MCP / Read 系 / harness 内部 tool) は素通し。
バックスラッシュエスケープ (`\\M#123`, `\\log #123`, `#`省略形の `\\log 123`) は字義扱いで非 block。

検出時は `permissionDecision: "deny"` + 英文 reason を返し、
発火ログを `~/.cc-memory/logs/preblock_hook.jsonl` に append する (書き込み失敗時は silent)。
"""
import json
import pathlib
import sys
import tomllib
from datetime import datetime, timezone

# プラグイン経由で `${CLAUDE_PLUGIN_ROOT}` を cwd として起動されるため、
# 同居ソースを import path に通す。
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.env_compat import env_get  # noqa: E402
from src.services.internal_id_patterns import (  # noqa: E402
    RAW_CITE_CODE_PATTERN,
    RAW_CITE_FULLWORD_PATTERN,
)

# converter (`_convert_line_raw_to_cite`) が `\` 直後の TYPE_CODE 判定に使う集合と
# 揃えるためのローカル定数。converter 側は TYPE_CODE_TO_NAME のキーを参照しているが、
# hook は citations_pure に依存しないので最小集合だけ持つ。
_TYPE_CODES: frozenset[str] = frozenset("MDLAT")

# ローカルプラグイン経由の tool 名は `mcp__plugin_<プラグイン名>_<サーバーキー>__`、
# リモート MCP 経由は `mcp__claude_ai_<コネクタ名>__` の形になる。プラグイン名
# (.claude-plugin/plugin.json)・サーバーキー (.mcp.json)・claude.ai コネクタ名は
# それぞれ別のタイミングで calm へ切り替わるため、新旧の組み合わせをすべて載せる。
# 片方だけにすると未追随の経路から呼ばれた CALM 自身の tool が allowlist から
# 外れ、内部 ID を含む正当な呼び出し (add_decisions の content に D#123 を書く等)
# が誤 block される。
#
# 実際にプラグイン名だけ先に calm へ改名され、サーバーキーが cc-memory のまま
# という期間があり、その間の tool 名は `mcp__plugin_calm_cc-memory__*` だった。
# サーバーキー改名後も、プラグインキャッシュ再生成と各セッションの再接続が
# 済むまでは同じ形が生き続ける。
#
# hook_transcript.py はマーカーの部分一致で同じ問題を吸収しているが、こちらは
# 内部 ID の漏出を止める security hook なので、素通しする対象を既知の識別子の
# 組み合わせだけに限る前方一致を維持する。
_PLUGIN_NAMES: tuple[str, ...] = ("calm", "claude-code-memory")
_SERVER_KEYS: tuple[str, ...] = ("calm", "cc-memory")

ALLOWLIST_PREFIXES: tuple[str, ...] = (
    *(
        f"mcp__plugin_{plugin}_{server}__"
        for plugin in _PLUGIN_NAMES
        for server in _SERVER_KEYS
    ),
    *(f"mcp__claude_ai_{server}__" for server in _SERVER_KEYS),
)

ALLOWLIST_EXACT: frozenset[str] = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
        "Agent",
        "Task",
        "Workflow",
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
# 実プロジェクトの name は "calm" (旧 "claude-code-memory")。過去ドキュメントや一部 fixture が
# "cc-memory" 表記を持つので合わせて受け入れる。
_PROJECT_NAMES: tuple[str, ...] = ("cc-memory", "claude-code-memory", "calm")


def _is_allowed(tool_name: str) -> bool:
    if tool_name in ALLOWLIST_EXACT:
        return True
    return any(tool_name.startswith(p) for p in ALLOWLIST_PREFIXES)


def _scan_text_for_literals(text: str) -> list[str]:
    """1 個の文字列に対し code + fullword 両方を検出し、マッチした raw 文字列を返す。

    バックスラッシュエスケープ (`\\M#123`, `\\log #123`, `#`省略形の `\\log 123`)
    は字義扱いとして block 対象から外す。fullword パターンが `#` 有無どちらにも
    マッチするため、エスケープ判定も両形式を同じ経路でスキップする。エスケープ判定は
    converter (`_convert_line_raw_to_cite`) と同じ条件: `\\` の直後が「TYPE_CODE 1 文字
    + リテラル形式」または「fullword リテラル形式 (`#`有無いずれか)」であるときだけ、
    その全体をスキップする。それ以外の `\\` は単なる文字として残し、
    後段の `\\X` (X は TYPE_CODE) が独立したリテラルとして検出される機会を保つ。

    例:
      - `\\M#1`  (`\\` 1 個): converter はエスケープ扱い → ここでも skip
      - `\\\\M#1` (`\\` 2 個): converter は i=0 で `\\` を文字残し、i=1 の `\\M#1` を
        エスケープ扱い → ここでも同じ判定で skip
      - `\\hello` (`\\` の直後が TYPE_CODE でも fullword でもない): `\\` は単なる
        文字として残し、続く `hello` を通常テキストとして scan する
    """
    # converter と同じ走査経路でエスケープ済み区間を boundary 用空白に置換する。
    # 元テキストと長さを揃え、word boundary の lookbehind/lookahead に影響を与えない。
    sanitized: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            tail = text[i + 1]
            # `\\X#NNN` (X は TYPE_CODE) のエスケープ。
            if tail in _TYPE_CODES:
                m = RAW_CITE_CODE_PATTERN.match(text, i + 1)
                if m and m.start() == i + 1:
                    sanitized.append(" " * (m.end() - i))
                    i = m.end()
                    continue
            # `\\log #NNN` 等の fullword エスケープ。
            m_fw = RAW_CITE_FULLWORD_PATTERN.match(text, i + 1)
            if m_fw and m_fw.start() == i + 1:
                sanitized.append(" " * (m_fw.end() - i))
                i = m_fw.end()
                continue
            # 上記いずれのリテラルにも該当しない `\\X` は単なる文字。`\\` を残す。
        sanitized.append(ch)
        i += 1
    haystack = "".join(sanitized)
    matches: list[str] = []
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

    `[project].name` を tomllib で厳密パースし、`_PROJECT_NAMES` のいずれかに
    一致するケースを cc-memory project とみなす。コメント行や別フィールドに
    たまたま許容値を含むケースで誤判定しないよう、文字列の部分一致ではなく
    パース済みの構造から取り出す。
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
        if env_get("CALM_LEAK_GUARD", "").lower() == "off":
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
            "prefix to indicate a literal (e.g. '\\M#123', '\\log #456', "
            "or '\\log 456' for the '#'-omitted form)."
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
