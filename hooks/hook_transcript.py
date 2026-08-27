"""hook共通: transcript解析ユーティリティ

イベント駆動アーキテクチャ用の差分読み・イベント抽出と、
レガシー関数（Phase 3で廃止予定）を含む。
"""
import json
import re
from pathlib import Path

# --- CALM MCPツール判定用マーカー ---
# ローカルプラグイン: mcp__plugin_calm_calm__*
# リモートMCP:       mcp__claude_ai_calm__*
# 接続経路に依存せず "calm__" の有無で判定する。
#
# 旧名 "cc-memory__" も受理する。hookは過去に記録されたtranscriptも解析対象に
# するため、改名前のツール名しか含まないtranscriptで検出が全滅しないようにする。
# 判定は先頭のマーカーから順に行い、最初に一致したものでshort_nameを切り出す。

_TOOL_NAME_MARKERS: tuple[str, ...] = ("calm__", "cc-memory__")


def _find_tool_name_marker(name: str) -> str | None:
    """ツール名に含まれるマーカーを返す。含まれなければNone。"""
    for marker in _TOOL_NAME_MARKERS:
        if marker in name:
            return marker
    return None


def _is_calm_tool(name: str) -> bool:
    """ツール名がCALMのツールかどうかを判定する。"""
    return _find_tool_name_marker(name) is not None


def _extract_short_name(name: str) -> str:
    """ツール名からshort_name（check_in, add_logs等）を取り出す。

    CALMのツール名でない場合はValueErrorを送出する。呼び出し側は
    `_is_calm_tool` で判定してから使うこと。
    """
    marker = _find_tool_name_marker(name)
    if marker is None:
        raise ValueError(f"CALMのツール名ではありません: {name!r}")
    return name.split(marker, 1)[1]

# --- 記録ツール ---

_RECORDING_TOOLS = {
    "add_decisions",
    "add_topic",
    "add_logs",
}

# --- check-in系ツール ---

_CHECKIN_TOOLS = {
    "check_in",
    "add_activity",
}


# ===================================================================
# イベント駆動アーキテクチャ: 差分読み + イベント抽出
# ===================================================================


def read_transcript_from_offset(transcript_path: str, offset: int) -> tuple[list[dict], int, bool]:
    """transcriptをバイトオフセットから読み、(新規エントリ一覧, 新オフセット, リセット発生)を返す。

    transcriptはappend-onlyのJSONL形式。offsetがファイルサイズを超えた場合は
    0にリセットして全読みする（defensive coding）。
    リセット発生時は呼び出し側でcurrent_turnやevents.jsonlもリセットすべき。
    """
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return [], 0, False

    try:
        file_size = path.stat().st_size
        offset_reset = offset > file_size
        if offset_reset:
            offset = 0

        entries = []
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
            new_offset = offset + len(data)

        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        return entries, new_offset, offset_reset

    except Exception:
        return [], offset, False


def is_user_message(entry: dict) -> bool:
    """エントリがUser Message（ターン境界）かどうかを判定する。

    User Message = ユーザーが実際に送信したuserエントリ。
    tool_resultやsystem-reminderを含むuser/humanエントリは除外する。
    isMeta=trueのエントリ（スキル内容注入等）も除外する。
    string形式のsystem-reminderの誤判定は許容（発生率0.02%）。
    """
    if entry.get("type") not in ("user", "human"):
        return False
    if entry.get("isMeta"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def extract_events(entries: list[dict], current_turn: int) -> tuple[list[dict], int]:
    """transcriptエントリ群からイベントを抽出する。

    2型イベントを抽出:
    - tool: cc-memoryツール呼び出し（assistantのtool_useブロック）
    - skill: スキル開始検出（User Messageの<command-name>タグ）

    Args:
        entries: transcriptの新規エントリ一覧
        current_turn: 現在のturn番号

    Returns:
        (抽出されたイベントのリスト, 更新後のturn番号)
    """
    events: list[dict] = []

    for entry in entries:
        entry_type = entry.get("type", "")

        # Turn境界検出: User Message到着で新turnが始まる
        if is_user_message(entry):
            current_turn += 1
            # skillイベント: User Messageに<command-name>が含まれる場合
            text = _extract_user_content_text(entry)
            match = re.search(r"<command-name>/?(.*?)</command-name>", text)
            if match:
                events.append({
                    "e": "skill",
                    "name": match.group(1).strip(),
                    "turn": current_turn,
                })
            continue

        # assistantエントリからtoolイベントを抽出
        if entry_type == "assistant":
            message = entry.get("message", {})
            content = message.get("content", [])

            if isinstance(content, str):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")

                if block_type == "tool_use":
                    name = block.get("name", "")
                    if _is_calm_tool(name):
                        short_name = _extract_short_name(name)
                        event: dict = {
                            "e": "tool",
                            "name": short_name,
                            "turn": current_turn,
                        }
                        if short_name == "check_in":
                            aid = block.get("input", {}).get("activity_id")
                            if aid is not None:
                                try:
                                    event["activity_id"] = int(aid)
                                except (ValueError, TypeError):
                                    pass
                        elif short_name == "add_decisions":
                            items = block.get("input", {}).get("items", [])
                            topic_ids: list[int] = []
                            if isinstance(items, list):
                                for item in items:
                                    if not isinstance(item, dict):
                                        continue
                                    tid = item.get("topic_id")
                                    if tid is None:
                                        continue
                                    try:
                                        topic_ids.append(int(tid))
                                    except (ValueError, TypeError):
                                        pass
                            if topic_ids:
                                event["topic_ids"] = topic_ids
                        events.append(event)

    return events, current_turn


# ===================================================================
# 共通ユーティリティ（イベント駆動でも使用）
# ===================================================================


def _extract_user_content_text(entry: dict) -> str:
    """userエントリからcontent文字列を取得する。"""
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return ""


# ===================================================================
# レガシー関数（Phase 3で廃止予定）
# 現在はテストからの参照があるため残置
# ===================================================================


_CONTEXT_RETRIEVAL_SHORT_NAMES = {
    "search", "get_topics", "get_decisions",
    "get_logs", "get_activities", "get_by_ids",
}


def _has_tool_calls(entries: list[dict], short_names: set[str]) -> bool:
    """entriesに指定short_nameのcc-memoryツール呼び出しがあるかチェック。"""
    for entry in entries:
        message = entry.get("message", {})
        content = message.get("content", [])

        if isinstance(content, str):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if _is_calm_tool(name) and _extract_short_name(name) in short_names:
                return True

    return False


def has_recent_recording(entries: list[dict]) -> bool:
    """entriesにadd_decisions/add_topic/add_logsのツール呼び出しがあるかチェック。"""
    return _has_tool_calls(entries, _RECORDING_TOOLS)


def has_activity_checkin_calls(entries: list[dict]) -> bool:
    """entriesにcheck_in/add_activityのツール呼び出しがあるかチェック。"""
    return _has_tool_calls(entries, _CHECKIN_TOOLS)


def extract_checkin_activity_id(entries: list[dict]) -> int | None:
    """transcriptからcheck_inのtool_use入力を逆順走査し、最後のactivity_idを返す"""
    for entry in reversed(entries):
        message = entry.get("message", {})
        content = message.get("content", [])

        if isinstance(content, str):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if _is_calm_tool(name) and _extract_short_name(name) == "check_in":
                tool_input = block.get("input", {})
                aid = tool_input.get("activity_id")
                if aid is not None:
                    return int(aid)

    return None


def extract_last_activity_id(transcript_path: str) -> int | None:
    """transcriptからcheck_in/add_activityのactivity_idを取得する。

    check_in: tool_useのinput.activity_idから取得
    add_activity: tool_use_idを記録し、対応するtool_resultのactivity_idから取得
    順序通りに走査し、最後に見つかったactivity_idを返す。
    """
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return None

    last_activity_id = None
    add_activity_use_ids: set[str] = set()

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                message = entry.get("message", {})
                content = message.get("content", [])
                if isinstance(content, str):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")

                    if block_type == "tool_use":
                        name = block.get("name", "")
                        if not _is_calm_tool(name):
                            continue
                        short = _extract_short_name(name)
                        if short == "check_in":
                            aid = block.get("input", {}).get("activity_id")
                            if aid is not None:
                                try:
                                    last_activity_id = int(aid)
                                except (ValueError, TypeError):
                                    pass
                        elif short == "add_activity":
                            use_id = block.get("id")
                            if use_id:
                                add_activity_use_ids.add(use_id)

                    elif block_type == "tool_result":
                        use_id = block.get("tool_use_id")
                        if use_id not in add_activity_use_ids:
                            continue
                        result_content = block.get("content", "")
                        aid = _parse_activity_id_from_result(result_content)
                        if aid is not None:
                            last_activity_id = aid

    except Exception:
        pass

    return last_activity_id


def _parse_activity_id_from_result(result_content) -> int | None:
    """tool_resultのcontentからactivity_idをパースする。"""
    if isinstance(result_content, str):
        return _try_parse_activity_id(result_content)
    if isinstance(result_content, list):
        for item in result_content:
            if isinstance(item, dict) and item.get("type") == "text":
                aid = _try_parse_activity_id(item.get("text", ""))
                if aid is not None:
                    return aid
    return None


def _try_parse_activity_id(text: str) -> int | None:
    """JSON文字列からactivity_idを抽出する"""
    try:
        data = json.loads(text)
        aid = data.get("activity_id")
        if aid is not None:
            return int(aid)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def has_context_retrieval_calls(entries: list[dict]) -> bool:
    """entriesにget系APIの呼び出しがあるかチェック。"""
    return _has_tool_calls(entries, _CONTEXT_RETRIEVAL_SHORT_NAMES)


def has_decision_without_activity(entries: list[dict]) -> bool:
    """entriesにadd_decisionがあり、かつcheck_in/add_activityがない場合True。"""
    has_decision = _has_tool_calls(entries, {"add_decisions"})
    if not has_decision:
        return False
    has_activity = _has_tool_calls(entries, _CHECKIN_TOOLS)
    return not has_activity


def get_transcript_info(transcript_path: str) -> tuple[list[dict], bool]:
    """transcript全行を1パスで読み、(assistant_entries, has_skill_command)を返す。

    has_skill_commandは直近のuserエントリに<command-name>が含まれるかを示す。
    """
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return [], False

    entries: list[dict] = []
    last_user_has_command = False

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_type = entry.get("type", "")
                    if entry_type == "assistant":
                        entries.append(entry)
                    elif entry_type in ("user", "human"):
                        content = entry.get("message", {}).get("content", "")
                        if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                            continue
                        text = _extract_user_content_text(entry)
                        last_user_has_command = "<command-name>" in text
                except json.JSONDecodeError:
                    continue
    except Exception:
        return [], False

    return entries, last_user_has_command
