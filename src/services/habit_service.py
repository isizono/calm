"""振る舞い管理サービス"""
import logging

from src.db import get_connection, row_to_dict
from src.services.relay.entity_publish import publish_entity_event_with_conn

logger = logging.getLogger(__name__)


def get_active_habit_contents_with_conn(conn) -> list[str]:
    """有効かつtrigger_mode='always'な振る舞いのcontent一覧を取得する（conn共有版）。

    Returns:
        [content, ...]
    """
    rows = conn.execute(
        "SELECT content FROM habits WHERE active = 1 AND trigger_mode = 'always'"
    ).fetchall()
    return [r["content"] for r in rows]


def _importance_label(importance_score) -> str:
    """importance_scoreからマニフェスト表示用ラベルを導出する。

    1: critical, 2: important, それ以外（3等）: default。
    """
    if importance_score == 1:
        return "critical"
    if importance_score == 2:
        return "important"
    return "default"


def list_intelligently_habit_manifest_with_conn(conn) -> list[dict]:
    """trigger_mode='intelligently'な振る舞いのマニフェストを取得する（conn共有版）。

    タグに相当する仕組みが habits に存在しないため tags は含めない。
    title は description を優先し、未設定（棚卸し前等）なら content の
    先頭50文字にフォールバックする。
    importance_score昇順（同値はid昇順）で並べ、1(critical)を先頭に出す。
    status='archived'の振る舞いは除外する（activeとは独立した無効化軸）。

    Returns:
        [{habit_id, title, trigger_mode, importance_score, importance_label}, ...]
    """
    rows = conn.execute(
        "SELECT id, content, description, importance_score FROM habits "
        "WHERE trigger_mode = 'intelligently' AND active = 1 AND status = 'active' "
        "ORDER BY importance_score ASC, id ASC"
    ).fetchall()
    manifest = []
    for row in rows:
        title = row["description"] or row["content"][:50]
        manifest.append({
            "habit_id": row["id"],
            "title": title,
            "trigger_mode": "intelligently",
            "importance_score": row["importance_score"],
            "importance_label": _importance_label(row["importance_score"]),
        })
    return manifest


_VALID_IMPORTANCE_SCORES = (1, 2, 3)
_VALID_STATUSES = ("active", "archived")
_MAX_DESCRIPTION_LENGTH = 100


def _add_habit_with_conn(
    conn, content: str, importance_score: int = 3, status: str = "active"
) -> int:
    """振る舞いをINSERTしてhabit_idを返す（conn共有版）。バリデーションエラー時はValueError。"""
    if not content or not content.strip():
        raise ValueError("content must not be empty")
    if importance_score not in _VALID_IMPORTANCE_SCORES:
        raise ValueError(
            f"importance_score must be one of: {', '.join(map(str, _VALID_IMPORTANCE_SCORES))}"
        )
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(_VALID_STATUSES)}")
    cursor = conn.execute(
        "INSERT INTO habits (content, importance_score, status) VALUES (?, ?, ?)",
        (content, importance_score, status),
    )
    habit_id = cursor.lastrowid
    publish_entity_event_with_conn(conn, entity_type="habit", entity_id=habit_id, event="created")
    return habit_id


def add_habit(content: str, importance_score: int = 3, status: str = "active") -> dict:
    """振る舞いを追加する。

    Args:
        content: 振る舞いの内容（空文字不可）
        importance_score: 優先度（1/2/3のいずれか、既定3）
        status: 'active'/'archived'のいずれか（既定'active'）

    Returns:
        作成された振る舞い情報
    """
    if not content or not content.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "content must not be empty",
            }
        }

    if importance_score not in _VALID_IMPORTANCE_SCORES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    f"importance_score must be one of: "
                    f"{', '.join(map(str, _VALID_IMPORTANCE_SCORES))}"
                ),
            }
        }

    if status not in _VALID_STATUSES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"status must be one of: {', '.join(_VALID_STATUSES)}",
            }
        }

    conn = get_connection()
    try:
        habit_id = _add_habit_with_conn(
            conn, content, importance_score=importance_score, status=status
        )
        conn.commit()

        return {"habit_id": habit_id}

    except Exception as e:
        conn.rollback()
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()


def get_habits(active: bool = True, habit_id: int | None = None) -> dict:
    """振る舞い一覧を取得する。habit_id指定時はその1件のみを取得する。

    Args:
        active: habit_id未指定時のみ有効。Trueのとき（既定）active=1の振る舞いのみ返す。
            全件（無効化済み含む）取得したい場合はFalseを明示的に渡す。
        habit_id: 指定時は該当habitのみ返す（activeは無視される）。取得と同時に
            last_recalled_atを現在時刻に更新する（intelligently層の参照実績記録）。

    Returns:
        振る舞い一覧とtotal_count（habit_id指定時はhabitsが0件または1件）
    """
    conn = get_connection()
    try:
        if habit_id is not None:
            conn.execute(
                "UPDATE habits SET last_recalled_at = CURRENT_TIMESTAMP WHERE id = ?",
                (habit_id,),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM habits WHERE id = ?", (habit_id,)
            ).fetchall()
        elif active:
            rows = conn.execute(
                "SELECT * FROM habits WHERE active = 1 ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM habits ORDER BY id"
            ).fetchall()

        habits = []
        for row in rows:
            habit = row_to_dict(row)
            habits.append({
                "habit_id": habit["id"],
                "content": habit["content"],
                "active": habit["active"],
                "created_at": habit["created_at"],
                "trigger_mode": habit["trigger_mode"],
                "description": habit["description"],
                "importance_score": habit["importance_score"],
                "last_recalled_at": habit["last_recalled_at"],
                "status": habit["status"],
            })

        return {
            "habits": habits,
            "total_count": len(habits),
        }

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()


_VALID_TRIGGER_MODES = ("always", "intelligently")


def update_habit(
    habit_id: int,
    content: str | None = None,
    active: bool | None = None,
    trigger_mode: str | None = None,
    description: str | None = None,
    importance_score: int | None = None,
    status: str | None = None,
) -> dict:
    """振る舞いを更新する。

    Args:
        habit_id: 振る舞いID
        content: 新しい内容（optional）
        active: 有効/無効フラグ（True/False、optional）
        trigger_mode: 'always'（全文をSessionStartで常時注入）または
            'intelligently'（マニフェストのみ表示、詳細はget_habits(habit_id=...)で
            on-demand取得）のいずれか（optional）
        description: intelligently層のマニフェスト表示に使う要旨（100文字以内、optional）
        importance_score: 優先度（1/2/3のいずれか、optional）
        status: 'active'/'archived'のいずれか（optional）

    Returns:
        更新された振る舞い情報
    """
    if (
        content is None
        and active is None
        and trigger_mode is None
        and description is None
        and importance_score is None
        and status is None
    ):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    "At least one of content, active, trigger_mode, description, "
                    "importance_score or status must be provided"
                ),
            }
        }

    if content is not None and not content.strip():
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "content must not be empty",
            }
        }

    if active is not None and not isinstance(active, bool):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "active must be True or False",
            }
        }

    if trigger_mode is not None and trigger_mode not in _VALID_TRIGGER_MODES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"trigger_mode must be one of: {', '.join(_VALID_TRIGGER_MODES)}",
            }
        }

    if description is not None and len(description) > _MAX_DESCRIPTION_LENGTH:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"description must be at most {_MAX_DESCRIPTION_LENGTH} characters",
            }
        }

    if importance_score is not None and importance_score not in _VALID_IMPORTANCE_SCORES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    f"importance_score must be one of: "
                    f"{', '.join(map(str, _VALID_IMPORTANCE_SCORES))}"
                ),
            }
        }

    if status is not None and status not in _VALID_STATUSES:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"status must be one of: {', '.join(_VALID_STATUSES)}",
            }
        }

    conn = get_connection()
    try:
        # 存在チェック
        row = conn.execute(
            "SELECT * FROM habits WHERE id = ?",
            (habit_id,),
        ).fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Habit with id {habit_id} not found",
                }
            }

        # 動的SQL構築
        set_parts = []
        values = []

        if content is not None:
            set_parts.append("content = ?")
            values.append(content)

        if active is not None:
            set_parts.append("active = ?")
            values.append(active)

        if trigger_mode is not None:
            set_parts.append("trigger_mode = ?")
            values.append(trigger_mode)

        if description is not None:
            set_parts.append("description = ?")
            values.append(description)

        if importance_score is not None:
            set_parts.append("importance_score = ?")
            values.append(importance_score)

        if status is not None:
            set_parts.append("status = ?")
            values.append(status)

        set_clause = ", ".join(set_parts)
        values.append(habit_id)

        conn.execute(
            f"UPDATE habits SET {set_clause} WHERE id = ?",
            tuple(values),
        )
        publish_entity_event_with_conn(conn, entity_type="habit", entity_id=habit_id, event="updated")
        conn.commit()

        # 更新後の振る舞いを取得
        row = conn.execute(
            "SELECT * FROM habits WHERE id = ?",
            (habit_id,),
        ).fetchone()
        if not row:
            raise Exception("Failed to retrieve updated habit")

        habit = row_to_dict(row)
        return {
            "habit_id": habit["id"],
            "content": habit["content"],
            "active": habit["active"],
            "created_at": habit["created_at"],
            "trigger_mode": habit["trigger_mode"],
            "description": habit["description"],
            "importance_score": habit["importance_score"],
            "status": habit["status"],
        }

    except Exception as e:
        conn.rollback()
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
