"""destabilizesエッジの解消（resolve）管理サービス"""
import logging
import sqlite3

from src.db import get_connection
from src.services.retract_service import retract

logger = logging.getLogger(__name__)

VALID_RESOLUTIONS = {"reaffirmed", "revised", "retracted"}


def resolve_destabilization(
    source_decision_id: int,
    target_decision_id: int,
    resolution: str,
    revised_to_decision_id: int | None = None,
    note: str = "",
) -> dict:
    """destabilizesエッジ1本を解消（resolve）する。

    decision_destabilization_resolutionsに1行INSERTする（PRIMARY KEY: source_id, target_id）。
    エッジ自体（decision_supersedes側のkind='destabilizes'行）は削除しない（履歴を残す）。

    - resolution="reaffirmed": targetの結論を再確認した（揺らぎ解消、結論変更なし）。
      resolution行をINSERTするのみで、他の副作用はない。
    - resolution="revised": revised_to_decision_idを新結論として記録する。
      supersedesエッジ張り（新decisionがtargetをsupersedeする）は本関数の責務ではなく、
      呼び出し側が別途add_relation(relation_type="supersedes")で行う。
    - resolution="retracted": targetを実際にretractする（decisions.retracted_atを更新）。
      既存のretract_service.retract経路を再利用する。

    同一(source_decision_id, target_decision_id)への2回目以降の呼び出しは、
    PRIMARY KEY制約による重複INSERTを避けるため事前チェックで検出し、
    resolution行を追加せず"already_resolved": trueを返す（冪等）。
    このとき副作用（retracted分岐でのretract呼び出し等）も発生しない。

    Args:
        source_decision_id: 揺らぎの発生元（軸変更）のdecision ID
        target_decision_id: 前提が揺らいだ影響先のdecision ID
        resolution: "reaffirmed" | "revised" | "retracted"
        revised_to_decision_id: resolution="revised"のとき必須。新結論となるdecision ID
        note: 自由記述の注記

    Returns:
        成功時: {"resolved": bool, "already_resolved": bool}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if resolution not in VALID_RESOLUTIONS:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"resolution must be one of: {', '.join(sorted(VALID_RESOLUTIONS))}",
            }
        }
    if resolution == "revised" and revised_to_decision_id is None:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "revised_to_decision_id is required when resolution='revised'",
            }
        }

    conn = get_connection()
    try:
        # 既存resolution行の有無を確認（重複INSERTはPK制約でIntegrityErrorになるため事前チェック）。
        # SELECTのみなのでこの時点ではconnは書き込みトランザクションを開始していない。
        existing = conn.execute(
            "SELECT resolution FROM decision_destabilization_resolutions WHERE source_id = ? AND target_id = ?",
            (source_decision_id, target_decision_id),
        ).fetchone()
        already_resolved = existing is not None

        if not already_resolved and resolution == "retracted":
            # retract_serviceは自前でconn/トランザクションを持ち、内部でcommitまで完結する
            # （本関数のconnとは別コネクション）。connがまだ書き込みトランザクションを
            # 開始していないこのタイミングで先に呼ぶ。conn側のINSERTを先に実行してから
            # 呼ぶと、conn未commitの書き込みロックとretract側の書き込みロックが競合し、
            # busy_timeoutを使い切って"database is locked"で失敗する（実機確認済み）。
            #
            # source_decision_idの存在は後続INSERTのFK制約でしか検証されないが、retract
            # 実行後にFK違反で失敗すると「targetはretractされたがresolution行が残らない」
            # 状態になる。retract発火前に存在チェックし、この不整合を避ける。
            if conn.execute("SELECT 1 FROM decisions WHERE id = ?", (source_decision_id,)).fetchone() is None:
                return {
                    "error": {
                        "code": "CONSTRAINT_VIOLATION",
                        "message": f"source decision {source_decision_id} not found",
                    }
                }
            # ids引数はlist必須（retract(entity_type, ids, undo=False)）。
            retract_result = retract("decision", [target_decision_id])
            if "error" in retract_result:
                return {
                    "error": {
                        "code": "DATABASE_ERROR",
                        "message": f"retract failed: {retract_result['error']['message']}",
                    }
                }
            item_errors = retract_result.get("errors") or []
            if item_errors:
                item_error = item_errors[0]["error"]
                return {
                    "error": {
                        "code": item_error.get("code", "DATABASE_ERROR"),
                        "message": (
                            f"retract failed for decision {target_decision_id}: "
                            f"{item_error.get('message')}"
                        ),
                    }
                }

        if not already_resolved:
            conn.execute(
                "INSERT INTO decision_destabilization_resolutions "
                "(source_id, target_id, resolution, revised_to_decision_id, note) VALUES (?, ?, ?, ?, ?)",
                (source_decision_id, target_decision_id, resolution, revised_to_decision_id, note),
            )

        conn.commit()
        return {"resolved": not already_resolved, "already_resolved": already_resolved}
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"resolve_destabilization failed: {e}")
        return {"error": {"code": "CONSTRAINT_VIOLATION", "message": str(e)}}
    except Exception as e:
        conn.rollback()
        logger.error(f"resolve_destabilization failed: {e}")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()
