"""role 別 tool capability matrix。

各 tool が orch / dispatcher / worker のどの role から呼び出せるかを表現する。
matrix の値:
  - True: 許可
  - False: 拒否 (hide 候補)
  - "self": 条件付き許可 (caller_session_id 比較は呼び出し側 guard が行う、tools/list には表示)

判定経路は role_service.lookup_role に集約。
"""
from typing import Literal

Role = Literal["orch", "dispatcher", "worker"]
Decision = bool | Literal["self"]
CapabilityMatrix = dict[str, dict[str, Decision]]


CAPABILITY_MATRIX: CapabilityMatrix = {
    # ow tools (worker lifecycle / messaging)
    "ow_spawn_worker":   {"orch": False, "dispatcher": True,  "worker": False},
    "ow_close_worker":   {"orch": False, "dispatcher": True,  "worker": "self"},
    "ow_recover":        {"orch": False, "dispatcher": True,  "worker": False},
    "ow_send":           {"orch": True,  "dispatcher": True,  "worker": True},
    "ow_status":         {"orch": True,  "dispatcher": True,  "worker": True},
    "ow_history":        {"orch": True,  "dispatcher": True,  "worker": True},
    "ow_spawn_dispatcher": {"orch": True,  "dispatcher": False, "worker": False},
    "ow_close_dispatcher": {"orch": True,  "dispatcher": False, "worker": False},

    # 書き込み
    "add_topic":         {"orch": True,  "dispatcher": False, "worker": False},
    "add_activity":      {"orch": True,  "dispatcher": False, "worker": False},
    "add_decisions":     {"orch": True,  "dispatcher": False, "worker": False},
    "add_logs":          {"orch": True,  "dispatcher": True,  "worker": True},
    "add_material":      {"orch": True,  "dispatcher": False, "worker": True},
    "add_relation":      {"orch": True,  "dispatcher": False, "worker": False},
    "add_pin":           {"orch": True,  "dispatcher": False, "worker": False},
    "add_habit":         {"orch": True,  "dispatcher": False, "worker": False},

    # 更新
    "update_activity":   {"orch": True,  "dispatcher": False, "worker": False},
    "update_material":   {"orch": True,  "dispatcher": False, "worker": "self"},
    "update_habit":      {"orch": True,  "dispatcher": False, "worker": False},
    "update_tag":        {"orch": True,  "dispatcher": False, "worker": False},

    # 削除・取消
    "retract":           {"orch": True,  "dispatcher": False, "worker": False},
    "remove_pin":        {"orch": True,  "dispatcher": False, "worker": False},
    "remove_relation":   {"orch": True,  "dispatcher": False, "worker": False},

    # 読み出し
    "search":            {"orch": True,  "dispatcher": True,  "worker": True},
    "search_tags":       {"orch": True,  "dispatcher": True,  "worker": True},
    "analyze_tags":      {"orch": True,  "dispatcher": True,  "worker": True},
    "check_in":          {"orch": True,  "dispatcher": True,  "worker": True},
    "get_activities":    {"orch": True,  "dispatcher": True,  "worker": True},
    "get_decisions":     {"orch": True,  "dispatcher": True,  "worker": True},
    "get_logs":          {"orch": True,  "dispatcher": True,  "worker": True},
    "get_topics":        {"orch": True,  "dispatcher": True,  "worker": True},
    "get_material":      {"orch": True,  "dispatcher": True,  "worker": True},
    "export_material":   {"orch": True,  "dispatcher": True,  "worker": True},
    "get_habits":        {"orch": True,  "dispatcher": True,  "worker": True},
    "get_map":           {"orch": True,  "dispatcher": True,  "worker": True},
    "get_timeline":      {"orch": True,  "dispatcher": True,  "worker": True},
    "get_by_ids":        {"orch": True,  "dispatcher": True,  "worker": True},
    "get_config":        {"orch": True,  "dispatcher": True,  "worker": True},
    "pull_precedents":   {"orch": True,  "dispatcher": True,  "worker": True},

    # その他
    "roll_dice":         {"orch": True,  "dispatcher": True,  "worker": True},
}


def hidden_tools_for(role: str) -> set[str]:
    """role に対して tools/list から hide すべき tool 名集合。

    decision が False の tool のみ hide する。"self" は表示する (guard 層で判定)。
    matrix に role 自体が無いケース (=未定義 role) では全 tool を hide。
    """
    hidden: set[str] = set()
    for name, matrix in CAPABILITY_MATRIX.items():
        decision = matrix.get(role)
        if decision is False or decision is None:
            hidden.add(name)
    return hidden


def is_allowed(tool_name: str, role: str) -> Decision:
    """capability matrix から (tool, role) の decision を返す。

    matrix に無い tool は False (default deny)。
    """
    matrix_row = CAPABILITY_MATRIX.get(tool_name)
    if matrix_row is None:
        return False
    return matrix_row.get(role, False)
