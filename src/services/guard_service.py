"""role 別 capability gating の guard。

各 tool 呼び出しの冒頭で `check_capability(tool_name, args)` を呼ぶことで、
capability_matrix に基づき role 違反を CapabilityError で拒否する。
hide 層 (visibility_middleware) と二層構造で動作し、本 guard は
LLM が hide をすり抜けて tool を呼んだ場合の最終防御を担う。

非 ow セッション (lookup_role が None) は role 不明扱いとして通過させる。
これは regular Claude session の従来挙動を維持するため。ow セッションは
SessionStart hook で auto-register されるか env OW_ROLE で fallback されるため、
role None になるのは非 ow セッションのみという前提。
"""
import os
import sqlite3
from typing import Any, Optional, TYPE_CHECKING

from src.db import get_connection

if TYPE_CHECKING:
    from src.services.role_service import Role


class CapabilityError(RuntimeError):
    """capability matrix に違反する tool 呼び出しで raise される。"""


class WorkerGuardError(CapabilityError):
    """worker セッションが直接呼び出せないツールを呼んだときに raise される。

    後方互換のため CapabilityError のサブクラスとして残す。
    """


_ROLE_ENV = "OW_ROLE"
_ROLE_WORKER = "worker"
_ESCALATION_ENV = "OW_ESCALATION"
_ESCALATION_PASS = "1"


def is_worker_session() -> bool:
    """ow worker として起動されたセッションかを判定する。

    lookup_role が DB → env fallback を内包しているため、その結果が worker かを返す。
    DB 優先 (DB に session が登録済みなら DB が真実) で、env 二重チェックは行わない。
    """
    from src.services.role_service import lookup_role, get_caller_session_id

    session_id = get_caller_session_id()
    conn = get_connection()
    try:
        return lookup_role(conn, session_id) == _ROLE_WORKER
    finally:
        conn.close()


def current_role() -> Optional["Role"]:
    """現在のセッションの role を返す。

    lookup_role を経由して DB → env の順で解決する。
    MCP コンテキスト外（テスト・hook など）でも安全に呼べる。
    """
    from src.services.role_service import lookup_role, get_caller_session_id

    session_id = get_caller_session_id()
    conn = get_connection()
    try:
        return lookup_role(conn, session_id)
    finally:
        conn.close()


def is_escalation_mode() -> bool:
    """orch_proxy 経由でエスカレーション通路に乗っているかを判定する。"""
    return os.environ.get(_ESCALATION_ENV) == _ESCALATION_PASS


_WORKER_GUARD_MESSAGE_TMPL = (
    "{tool_name} は worker セッションから直接呼び出せません。"
    "ユーザー合意に基づいて orch 経由で記録してください "
    "(orch_proxy 経路では OW_ESCALATION=1 で通過します)。"
)

_ROLE_VIOLATION_MESSAGE_TMPL = (
    "{tool_name} は {role} role からは呼び出せません。"
    "capability matrix の許可表に従い、適切な role の session から呼び出すか、"
    "OW_ESCALATION=1 でエスカレーション通路に切り替えてください。"
)

_SELF_VIOLATION_MESSAGE_TMPL = (
    "{tool_name} の self-target 制約に違反しています。"
    "{role} role からは caller 自身が target の場合のみ許可されます。"
)


def check_capability(
    tool_name: str,
    args: Optional[dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """caller の role と tool_name から capability gate を判定する。

    違反時に CapabilityError を raise する。
    - role None (非 ow session): 通過 (backward compat)
    - escalation mode (OW_ESCALATION=1): 通過 (orch_proxy 経路の通過弁)
    - matrix 行 True: 通過
    - matrix 行 False: CapabilityError
    - matrix 行 "self": self-target 判定し、違反なら CapabilityError
    """
    from src.services import capability_matrix, role_service

    session_id = role_service.get_caller_session_id()
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        role = role_service.lookup_role(conn, session_id)
        if role is None:
            return  # 非 ow session: 通過
        if is_escalation_mode():
            return  # 通過弁
        decision = capability_matrix.is_allowed(tool_name, role)
        if decision is True:
            return
        if decision == "self":
            _check_self_target(tool_name, role, args or {}, conn, session_id)
            return
        # worker role の denial は WorkerGuardError 経路で raise する。
        # WorkerGuardError は CapabilityError の subclass なので意味的整合は保たれる。
        if role == _ROLE_WORKER:
            raise WorkerGuardError(
                _WORKER_GUARD_MESSAGE_TMPL.format(tool_name=tool_name)
            )
        raise CapabilityError(
            _ROLE_VIOLATION_MESSAGE_TMPL.format(tool_name=tool_name, role=role)
        )
    finally:
        if own_conn and conn is not None:
            conn.close()


def _check_self_target(
    tool_name: str,
    role: str,
    args: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: Optional[str],
) -> None:
    """"self" 扱い tool の caller_session_id 一致判定。"""
    if tool_name == "update_material":
        material_id = args.get("material_id")
        if material_id is None or session_id is None:
            raise CapabilityError(
                _SELF_VIOLATION_MESSAGE_TMPL.format(tool_name=tool_name, role=role)
            )
        row = conn.execute(
            "SELECT caller_session_id FROM materials WHERE id = ?",
            (material_id,),
        ).fetchone()
        if not row or row[0] != session_id:
            raise CapabilityError(
                _SELF_VIOLATION_MESSAGE_TMPL.format(tool_name=tool_name, role=role)
            )
        return

    raise CapabilityError(
        f"self-target check is not implemented for {tool_name}"
    )


# matrix で "self" を返す tool 名と、_check_self_target が処理する tool 名は一致しなければならない。
# 新たに matrix に "self" エントリを追加した際、ここを更新し忘れると実行時に
# `self-target check is not implemented for ...` で初めて発覚するため、起動時に検出するための集合。
_SELF_TARGET_HANDLERS: frozenset[str] = frozenset({"update_material"})


def _matrix_self_tools() -> set[str]:
    """capability matrix で "self" decision を持つ tool 名を集める。"""
    from src.services.capability_matrix import CAPABILITY_MATRIX

    return {
        name
        for name, row in CAPABILITY_MATRIX.items()
        if any(d == "self" for d in row.values())
    }


def assert_self_target_handlers_consistent() -> None:
    """matrix の "self" 集合と _check_self_target が処理する集合の不一致を検出する。

    起動時 or テストから呼ぶ。不一致は AssertionError として早期に顕在化させる。
    """
    matrix_self = _matrix_self_tools()
    missing = matrix_self - _SELF_TARGET_HANDLERS
    extra = _SELF_TARGET_HANDLERS - matrix_self
    if missing or extra:
        raise AssertionError(
            "self-target handler set is out of sync with capability matrix: "
            f"missing handlers for {sorted(missing)}, "
            f"extra handlers for {sorted(extra)}"
        )


# ---------------------------------------------------------------------------
# 既存 API: check_worker_guard は段階的置換のために残す thin wrapper。
# 新規コードは check_capability を呼ぶ。
# ---------------------------------------------------------------------------


def check_worker_guard(tool_name: str) -> None:
    """worker セッションかつ非エスカレーション時に WorkerGuardError を raise する。

    新規コードでは check_capability を使う。本 wrapper は既存 call site の
    段階的置換のために残す。
    """
    if is_worker_session() and not is_escalation_mode():
        raise WorkerGuardError(_WORKER_GUARD_MESSAGE_TMPL.format(tool_name=tool_name))
