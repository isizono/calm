"""アクティビティ管理サービス"""
import logging
import re
import sqlite3
from typing import Optional

from src.db import get_connection, row_to_dict
from src.services.citations_service import (
    apply_and_writeback_conversions,
    upsert_citations_for_owner_with_conn,
)
from src.services.readable_id import strip_entity_id_inplace
from src.services.embedding_service import build_embedding_text, generate_and_store_embedding
from src.services.pin_service import ENTITY_TABLE_MAP as PIN_ENTITY_TABLE_MAP, _add_pin_with_conn
from src.services.relation_service import _add_relation_with_conn, _validate_targets
from src.services.relay.entity_publish import (
    bump_updated_at_and_publish_with_conn,
    publish_entity_event_with_conn,
)
from src.services.title_validation import validate_title
from src.services.tag_service import (
    validate_and_parse_tags,
    ensure_tag_ids,
    resolve_tag_ids,
    link_tags,
    get_entity_tags,
    get_entity_tags_batch,
    get_available_intents,
)

logger = logging.getLogger(__name__)

# get_activitiesでdescriptionを切り詰める上限文字数
ACTIVITY_DESC_MAX_LEN = 200
from src.config import HEARTBEAT_TIMEOUT_MINUTES, SNOOZE_DURATION_DAYS
# DB格納可能なステータス値
REAL_STATUSES = {"pending", "in_progress", "completed", "snoozed", "shelved"}
# "active"エイリアスが展開されるステータス
ACTIVE_STATUSES = ("in_progress", "pending")
# get_activities用（エイリアス含む）
VALID_STATUSES = REAL_STATUSES | {"active"}


IMPLEMENT_WORKFLOW_GUARD_MESSAGE = (
    "This implement has no decision in its related entities.\n\n"
    "Add a decision recording either:\n"
    "- the agreement reached through discussion, or\n"
    "- the reason for proceeding without design (e.g., \"typo-only\", \"hotfix\").\n\n"
    "Then retry add_activity."
)


def _has_intent_implement(conn, parsed_tags: list[tuple[str, str]]) -> bool:
    """parsed_tags が intent:implement を（aliasを辿った上で）含むかを返す。

    既存タグの canonical_id を SELECT して、エイリアス越しに
    intent:implement に解決されるかを判定する。
    """
    intent_tags = [(ns, name) for ns, name in parsed_tags if ns == "intent"]
    if not intent_tags:
        return False
    if any(name == "implement" for _, name in intent_tags):
        return True
    placeholders = " OR ".join("(t.namespace = ? AND t.name = ?)" for _ in intent_tags)
    flat = [v for pair in intent_tags for v in pair]
    rows = conn.execute(
        f"""
        SELECT ct.namespace AS canonical_ns, ct.name AS canonical_name
        FROM tags t
        LEFT JOIN tags ct ON t.canonical_id = ct.id
        WHERE {placeholders}
        """,
        flat,
    ).fetchall()
    return any(
        row["canonical_ns"] == "intent" and row["canonical_name"] == "implement"
        for row in rows
    )


def _check_implement_workflow_guard(
    conn, parsed_tags: list[tuple[str, str]], related: list[dict] | None
) -> dict | None:
    """intent:implement アクティビティに decision の direct relate を要求する。

    通過条件:
        - intent:implement を含まない、または
        - related に type='decision' で実在する decision_id を1件以上含む

    Returns:
        通過時: None
        ブロック時: {"error": {"code": "IMPLEMENT_WORKFLOW_GUARD", "message": ...}}
    """
    if not _has_intent_implement(conn, parsed_tags):
        return None

    decision_ids = [
        decision_id
        for t in (related or [])
        if t.get("type") == "decision"
        for decision_id in (t.get("ids") or [])
    ]
    if decision_ids:
        placeholders = ",".join("?" * len(decision_ids))
        # retracted_at IS NULL: 取り消し済 decision はガード通過の根拠にしない
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM decisions "
            f"WHERE id IN ({placeholders}) AND retracted_at IS NULL",
            decision_ids,
        ).fetchone()
        if row["cnt"] > 0:
            return None

    return {
        "error": {
            "code": "IMPLEMENT_WORKFLOW_GUARD",
            "message": IMPLEMENT_WORKFLOW_GUARD_MESSAGE,
        }
    }


def _validate_pins(pins: list[dict]) -> dict | None:
    """add_activityのpins引数をバリデーションする。不正な場合はエラーdictを返す。

    呼び出し元は空リスト・Noneをno-opとして扱い、非空の場合のみ本関数を呼ぶ。
    """
    for pin in pins:
        if "type" not in pin or "ref" not in pin:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Each pin must have 'type' and 'ref' fields",
                }
            }
        if pin["type"] not in PIN_ENTITY_TABLE_MAP:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"Invalid pin type: {pin['type']}. Must be one of: {', '.join(sorted(PIN_ENTITY_TABLE_MAP.keys()))}",
                }
            }
    return None


def add_activity(
    title: str,
    description: str,
    tags: list[str],
    related: list[dict] | None = None,
    pins: list[dict] | None = None,
    check_in: bool = True,
    orch_managed: bool = False,
) -> dict:
    """
    アクティビティを作成してIDを返す

    Args:
        title: アクティビティのタイトル（35字以内）
        description: アクティビティの説明
        tags: タグ配列（必須、1個以上）
        related: 関連エンティティ（optional）。
            [{"type": "topic" | "activity" | "material" | "decision" | "log", "ids": [int, ...]}, ...] 形式。
            複数エンティティを配列で同時紐付け可能。
            例: [{"type": "topic", "ids": [1, 2]}, {"type": "decision", "ids": [10]}]
            intent:implement タグを含む場合、related に type='decision' のエントリを
            最低1件含めないと IMPLEMENT_WORKFLOW_GUARD エラーで弾かれる。
        pins: 作成したactivity自身から張るpin（optional）。
            [{"type": "tag" | "activity" | "topic" | "decision" | "log" | "material", "ref": int | str}, ...] 形式。
            source は作成された activity 自身になる。ref は add_pin の target_ref と同じ形式
            （tag のみ namespace:name 文字列を許容、それ以外は整数ID）。
            いずれかの pin が解決できない・存在しない場合、activity 自体の作成も含めて
            全体が失敗する（部分成功はしない）。
        check_in: 作成後にcheck_inを実行するか（デフォルト: True）
        orch_managed: orch が管理する activity か（デフォルト: False）。
            True を指定すると activities.orch_managed = 1 で作成される。
            Stop hook の check-in ブロック・nudge 抑制、SessionStart hook
            の一覧除外、hint 抑制の一次判定に使われる。

    Returns:
        作成されたアクティビティ情報（check_in=Trueの場合はcheck_in_resultを含む）
    """
    # titleのバリデーション
    title_err = validate_title(title)
    if title_err:
        return title_err
    # タグのバリデーション
    parsed_tags = validate_and_parse_tags(tags, required=True)
    if isinstance(parsed_tags, dict):
        return parsed_tags

    # relatedのバリデーション
    if related:
        err = _validate_targets("activity", related)
        if err:
            return err

    # pinsのバリデーション
    if pins:
        err = _validate_pins(pins)
        if err:
            return err

    conn = get_connection()
    try:
        # Workflow guard: intent:implement アクティビティは related に decision を必須とする
        guard_err = _check_implement_workflow_guard(conn, parsed_tags, related)
        if guard_err:
            return guard_err

        # アクティビティをINSERT
        cursor = conn.execute(
            "INSERT INTO activities (title, description, status, orch_managed) "
            "VALUES (?, ?, ?, ?)",
            (title, description, 'pending', 1 if orch_managed else 0),
        )
        activity_id = cursor.lastrowid

        # タグをリンク
        tag_ids = ensure_tag_ids(conn, parsed_tags)
        link_tags(conn, "activity_tags", "activity_id", activity_id, tag_ids)

        # リレーションを追加
        if related:
            _add_relation_with_conn(conn, "activity", activity_id, related)

        publish_entity_event_with_conn(
            conn, entity_type="activity", entity_id=activity_id, event="created"
        )

        # pinを追加（source は作成した activity 自身）。
        # いずれかが失敗したらトランザクション全体を破棄し、activity作成自体も失敗させる。
        # bump+publishはpinごとに個別発火させず、relation_serviceの
        # _bump_and_publish_endpoints_with_connと同様にsourceを1回・targetを
        # 重複排除してループ後にまとめて行う（outbox行の重複増殖を防ぐ）。
        if pins:
            seen_targets: set[tuple[str, int]] = set()
            for pin in pins:
                pin_result = _add_pin_with_conn(
                    conn, "activity", activity_id, pin["type"], pin["ref"], bump=False
                )
                if "error" in pin_result:
                    conn.rollback()
                    return pin_result
                seen_targets.add((pin_result["target_type"], pin_result["target_id"]))

            bump_updated_at_and_publish_with_conn(conn, "activity", activity_id)
            for target_type, target_id in seen_targets:
                bump_updated_at_and_publish_with_conn(conn, target_type, target_id)

        # 生 ID リテラルを {{cite:...}} に変換し、書き換わった本文を DB に書き戻す
        converted = apply_and_writeback_conversions(
            conn,
            entity_type="activity",
            entity_id=activity_id,
            fields_payload={"title": title, "description": description},
            tool_name="add_activity",
            table="activities",
        )
        title = converted["title"]
        description = converted["description"]

        # 本文中の {{cite:X#NNN}} を citations テーブルに保存
        upsert_citations_for_owner_with_conn(
            conn, "activity", activity_id, title=title, description=description
        )

        conn.commit()

        # タグを取得
        tag_strings = get_entity_tags(conn, "activity_tags", "activity_id", activity_id)

        # embedding生成（失敗してもactivity作成には影響しない）
        tag_text = " ".join(tag_strings) if tag_strings else ""
        generate_and_store_embedding("activity", activity_id, build_embedding_text(title, description, tag_text))

        result = {"activity_id": activity_id}
        try:
            result["available_intents"] = get_available_intents()
        except Exception:
            result["available_intents"] = []

    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {
            "error": {
                "code": "CONSTRAINT_VIOLATION",
                "message": str(e),
            }
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

    # check_in実行（connを閉じた後に呼ぶ。checkin_serviceが別connを開くため）
    if check_in:
        from src.services.checkin_service import check_in as do_check_in
        check_in_result = do_check_in(activity_id)
        result["check_in_result"] = check_in_result

    return result


def get_activities(
    tags: list[str] | None = None,
    status: str = "active",
    limit: int = 5,
    since: str | None = None,
    until: str | None = None,
    orch_managed: bool | None = None,
) -> dict:
    """
    アクティビティ一覧を取得（tags/status/orch_managed でフィルタリング）

    呼び出し時、updated_atがSNOOZE_DURATION_DAYS（デフォルト3日）を超過したsnoozed
    アクティビティをpendingへ一括自動復活させてから検索する（lazy evaluation）。

    Args:
        tags: タグ配列（optional。指定時はAND条件でフィルタ、未指定時は全件）
        status: フィルタするステータス（active/pending/in_progress/completed/snoozed/shelved、デフォルト: active）
                "active"はpending+in_progressの両方を返すエイリアス（snoozed/shelvedは含まない）
        limit: 取得件数上限（デフォルト: 5）
        since: ISO日付文字列（例: "2026-03-10"）。この日付以降に更新されたアクティビティのみ返す
        until: ISO日付文字列。この日付以前に更新されたアクティビティのみ返す
        orch_managed: True/False を指定すると activities.orch_managed カラムでフィルタする。
            None（デフォルト）はフィルタなし。

    Returns:
        アクティビティ一覧とtotal_count
    """
    # タグのバリデーション（tags指定時のみ）
    parsed_tags = None
    if tags is not None:
        parsed_tags = validate_and_parse_tags(tags, required=True)
        if isinstance(parsed_tags, dict):
            return parsed_tags

    if limit < 1:
        return {
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"limit must be positive, got {limit}",
            }
        }

    if status not in VALID_STATUSES:
        return {
            "error": {
                "code": "INVALID_STATUS",
                "message": f"Invalid status: {status}. Must be one of {sorted(VALID_STATUSES)}",
            }
        }

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
    if since is not None and not date_pattern.match(since):
        return {
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"since must be ISO date format (YYYY-MM-DD), got '{since}'",
            }
        }
    if until is not None and not date_pattern.match(until):
        return {
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"until must be ISO date format (YYYY-MM-DD), got '{until}'",
            }
        }

    conn = get_connection()
    try:
        # Lazy evaluation: 期限切れsnoozedを自動復活
        conn.execute(
            """UPDATE activities SET status = 'pending', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'snoozed'
                 AND updated_at <= datetime('now', '-' || ? || ' days')""",
            (SNOOZE_DURATION_DAYS,),
        )
        conn.commit()

        # タグフィルタでactivity_idsを絞り込む（tags指定時のみ）
        activity_ids = None
        if parsed_tags is not None:
            tag_ids = resolve_tag_ids(conn, parsed_tags)
            if not tag_ids or len(tag_ids) < len(parsed_tags):
                return {"activities": [], "total_count": 0}
            tag_placeholders = ",".join("?" * len(tag_ids))

            activity_ids_rows = conn.execute(
                f"""
                SELECT activity_id FROM activity_tags
                WHERE tag_id IN ({tag_placeholders})
                GROUP BY activity_id
                HAVING COUNT(DISTINCT tag_id) = ?
                """,
                (*tag_ids, len(tag_ids)),
            ).fetchall()

            activity_ids = [row["activity_id"] for row in activity_ids_rows]

            if not activity_ids:
                return {"activities": [], "total_count": 0}

        # WHERE句・ORDER BY句・パラメータを組み立て
        conditions = []
        where_params = []

        if activity_ids is not None:
            id_placeholders = ",".join("?" * len(activity_ids))
            conditions.append(f"id IN ({id_placeholders})")
            where_params.extend(activity_ids)

        if status == "active":
            status_placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
            conditions.append(f"status IN ({status_placeholders})")
            where_params.extend(ACTIVE_STATUSES)
            order_clause = "CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, updated_at DESC"
        else:
            conditions.append("status = ?")
            where_params.append(status)
            order_clause = "updated_at DESC, id DESC"

        if since is not None:
            conditions.append("updated_at >= ?")
            where_params.append(since)

        if until is not None:
            # 日付のみ指定時は当日を含めるため末尾に時刻を付与
            until_value = until if " " in until else until + " 23:59:59"
            conditions.append("updated_at <= ?")
            where_params.append(until_value)

        if orch_managed is not None:
            conditions.append("orch_managed = ?")
            where_params.append(1 if orch_managed else 0)

        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        else:
            where_clause = ""

        # 1. total_count取得（LIMITなし）
        count_row = conn.execute(
            f"SELECT COUNT(*) as count FROM activities {where_clause}",
            where_params,
        ).fetchone()
        total_count = count_row["count"]

        # 2. LIMIT付きでデータ取得
        rows = conn.execute(
            f"""
            SELECT *,
                   CASE WHEN last_heartbeat_at > datetime('now', '-' || ? || ' minutes') THEN 1 ELSE 0 END AS is_heartbeat_active
            FROM activities
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ?
            """,
            (HEARTBEAT_TIMEOUT_MINUTES, *where_params, limit),
        ).fetchall()

        # バッチでタグ取得
        fetched_ids = [row["id"] for row in rows]
        tags_map = get_entity_tags_batch(conn, "activity_tags", "activity_id", fetched_ids)

        activities = []
        for row in rows:
            activity = row_to_dict(row)
            item = {
                "id": activity["id"],
                "title": activity["title"],
                "description": (activity["description"] or "")[:ACTIVITY_DESC_MAX_LEN],
                "status": activity["status"],
                "tags": tags_map.get(activity["id"], []),
                "created_at": activity["created_at"],
                "updated_at": activity["updated_at"],
                "is_heartbeat_active": bool(activity["is_heartbeat_active"]),
                "orch_managed": bool(activity["orch_managed"]),
            }
            strip_entity_id_inplace(item)
            activities.append(item)

        return {"activities": activities, "total_count": total_count}

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()


def get_active_domains_with_conn(conn) -> list[dict]:
    """アクティブなアクティビティ（in_progress/pending）があるdomain:タグを取得する（conn共有版）。

    Returns:
        [{"tag_id": int, "name": str}, ...]（name順ソート）
    """
    rows = conn.execute(
        """
        SELECT DISTINCT t.id AS tag_id, t.name
        FROM tags t
        JOIN activity_tags at ON t.id = at.tag_id
        JOIN activities a ON at.activity_id = a.id
        WHERE t.namespace = 'domain'
          AND a.status IN ('in_progress', 'pending')
        ORDER BY t.name
        """,
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_active_domains() -> list[dict]:
    """アクティブなアクティビティ（in_progress/pending）があるdomain:タグを取得する。"""
    conn = get_connection()
    try:
        return get_active_domains_with_conn(conn)
    finally:
        conn.close()


def get_active_activities_by_tag_with_conn(conn, tag_id: int) -> list[dict]:
    """domain:タグに紐づくホットアクティビティを取得する（conn共有版）。

    last_heartbeat_session_id は呼び出し側（session_start_hook）が自セッション
    照合に使うため一緒に返す。

    Returns:
        [{"id": int, "title": str, "status": str, "updated_at": str,
          "last_heartbeat_session_id": str | None, "is_heartbeat_active": bool,
          "orch_managed": bool}, ...]
        （in_progress優先、updated_at降順）
    """
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.status, a.updated_at, a.last_heartbeat_session_id,
               a.orch_managed,
               CASE WHEN a.last_heartbeat_at > datetime('now', '-' || ? || ' minutes') THEN 1 ELSE 0 END AS is_heartbeat_active
        FROM activities a
        JOIN activity_tags at ON a.id = at.activity_id
        WHERE at.tag_id = ?
          AND a.status IN ('in_progress', 'pending')
        ORDER BY CASE a.status WHEN 'in_progress' THEN 0 ELSE 1 END,
                 a.updated_at DESC
        """,
        (HEARTBEAT_TIMEOUT_MINUTES, tag_id),
    ).fetchall()
    result = []
    for r in rows:
        d = row_to_dict(r)
        d["is_heartbeat_active"] = bool(d["is_heartbeat_active"])
        d["orch_managed"] = bool(d["orch_managed"])
        result.append(d)
    return result


def get_active_activities_by_tag(tag_id: int) -> list[dict]:
    """domain:タグに紐づくホットアクティビティ（pending + in_progress）を取得する。"""
    conn = get_connection()
    try:
        return get_active_activities_by_tag_with_conn(conn, tag_id)
    finally:
        conn.close()


def get_pinned_active_activities_with_conn(conn) -> list[dict]:
    """pinsテーブルでtargetがactivityになっているactive activitiesを取得する（conn共有版）。

    pinsテーブルを介したpin関係のうち target_type='activity' のものを引き、
    status IN ('in_progress', 'pending') かつ orch_managed=0 の activity を返す。
    複数の source（tag/activity 等）から同じ activity にpinされている場合でも
    DISTINCT で1件に集約する。

    Returns:
        [{"id": int, "title": str, "status": str, "updated_at": str,
          "last_heartbeat_session_id": str | None, "is_heartbeat_active": bool,
          "orch_managed": bool}, ...]
        （updated_at 降順、id を tie-breaker）
    """
    rows = conn.execute(
        """
        SELECT DISTINCT a.id, a.title, a.status, a.updated_at,
               a.last_heartbeat_session_id,
               a.orch_managed,
               CASE WHEN a.last_heartbeat_at > datetime('now', '-' || ? || ' minutes') THEN 1 ELSE 0 END AS is_heartbeat_active
        FROM activities a
        JOIN pins p ON p.target_type = 'activity' AND p.target_id = a.id
        WHERE a.status IN ('in_progress', 'pending')
          AND a.orch_managed = 0
        ORDER BY a.updated_at DESC, a.id DESC
        """,
        (HEARTBEAT_TIMEOUT_MINUTES,),
    ).fetchall()
    result = []
    for r in rows:
        d = row_to_dict(r)
        d["is_heartbeat_active"] = bool(d["is_heartbeat_active"])
        d["orch_managed"] = bool(d["orch_managed"])
        result.append(d)
    return result


def get_pinned_active_activities() -> list[dict]:
    """pinsテーブルでtargetがactivityになっているactive activitiesを取得する。"""
    conn = get_connection()
    try:
        return get_pinned_active_activities_with_conn(conn)
    finally:
        conn.close()


def update_activity(
    activity_id: int,
    status: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    orch_managed: Optional[bool] = None,
) -> dict:
    """
    アクティビティを更新する（ステータス、タイトル、説明、タグ、orch_managed を変更可能）

    snoozed状態のアクティビティに対しstatusを指定せず他フィールドのみ更新すると、
    自動的にstatus="pending"へ復活する。

    Args:
        activity_id: アクティビティID
        status: 新しいステータス（optional）
        title: 新しいタイトル（optional、35字以内）
        description: 新しい説明（optional）
        tags: 新しいタグ配列（optional、指定時は全置換。1個以上必須）
        orch_managed: orch が管理する activity かどうかを切り替える（optional）。
            True/False のみ受け付ける。None なら変更しない。

    Returns:
        更新されたアクティビティ情報
    """
    # 最低1つのオプショナルパラメータが必要
    if (
        status is None
        and title is None
        and description is None
        and tags is None
        and orch_managed is None
    ):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": (
                    "At least one of status, title, description, tags, or "
                    "orch_managed must be provided"
                ),
            }
        }

    # タグのバリデーション（tags指定時のみ）
    parsed_tags = None
    if tags is not None:
        parsed_tags = validate_and_parse_tags(tags, required=True)
        if isinstance(parsed_tags, dict):
            return parsed_tags

    # ステータスバリデーション
    if status is not None and status not in REAL_STATUSES:
        return {
            "error": {
                "code": "INVALID_STATUS",
                "message": f"Invalid status: {status}. Must be one of {sorted(REAL_STATUSES)}",
            }
        }

    # titleのバリデーション
    title_err = validate_title(title)
    if title_err:
        return title_err

    # 空文字バリデーション
    if title is not None and title.strip() == "":
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "title must not be empty",
            }
        }

    if description is not None and description.strip() == "":
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "description must not be empty",
            }
        }

    conn = get_connection()
    try:
        # 現在のアクティビティ情報を取得
        cursor = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,))
        row = cursor.fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Activity with id {activity_id} not found",
                }
            }

        # snoozed中にstatus指定なしでフィールド更新 → 自動復活
        old_status = row["status"]
        if status is None and old_status == "snoozed":
            status = "pending"

        # publish判定: statusはold_status != new_statusの遷移時のみ、
        # 他フィールド（title/description/tags/orch_managed）はno-op含めて無条件でpublish対象。
        # statusはsnoozed自動復活後の値（上のブロック）で判定するため、自動復活も遷移として拾う。
        should_publish = (
            title is not None
            or description is not None
            or parsed_tags is not None
            or orch_managed is not None
            or (status is not None and status != old_status)
        )

        # 動的SQL構築: 指定されたフィールドのみUPDATEする
        set_parts = []
        values = []

        if status is not None:
            set_parts.append("status = ?")
            values.append(status)

        if title is not None:
            set_parts.append("title = ?")
            values.append(title)

        if description is not None:
            set_parts.append("description = ?")
            values.append(description)

        if orch_managed is not None:
            set_parts.append("orch_managed = ?")
            values.append(1 if orch_managed else 0)

        # タグの全置換（tags指定時のみ）
        if parsed_tags is not None:
            conn.execute("DELETE FROM activity_tags WHERE activity_id = ?", (activity_id,))
            tag_ids = ensure_tag_ids(conn, parsed_tags)
            link_tags(conn, "activity_tags", "activity_id", activity_id, tag_ids)

        set_parts.append("updated_at = CURRENT_TIMESTAMP")

        set_clause = ", ".join(set_parts)
        values.append(activity_id)

        conn.execute(
            f"UPDATE activities SET {set_clause} WHERE id = ?",
            tuple(values),
        )

        # 生 ID リテラルを {{cite:...}} に変換し、書き換わった本文を DB に書き戻す。
        # 変換対象は呼び出し引数として明示された field のみ (title/description が
        # None の場合は既存値を触らない)。
        converted = apply_and_writeback_conversions(
            conn,
            entity_type="activity",
            entity_id=activity_id,
            fields_payload={"title": title, "description": description},
            tool_name="update_activity",
            table="activities",
        )

        # citations 全削除→再投入 (本文無変更でも実施)
        new_title = converted["title"] if title is not None else row["title"]
        new_description = converted["description"] if description is not None else row["description"]
        upsert_citations_for_owner_with_conn(
            conn, "activity", activity_id,
            title=new_title, description=new_description,
        )

        if should_publish:
            publish_entity_event_with_conn(
                conn, entity_type="activity", entity_id=activity_id, event="updated"
            )

        conn.commit()

        # 更新後のアクティビティを取得
        cursor = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,))
        row = cursor.fetchone()
        if not row:
            raise Exception("Failed to retrieve updated activity")

        # タグを取得
        tag_strings = get_entity_tags(conn, "activity_tags", "activity_id", activity_id)

        updated = row_to_dict(row)

        # title/description/tagsが変更された場合、embeddingを再生成
        if title is not None or description is not None or parsed_tags is not None:
            tag_text = " ".join(tag_strings) if tag_strings else ""
            generate_and_store_embedding(
                "activity", activity_id,
                build_embedding_text(updated["title"], updated["description"], tag_text),
            )

        return {"activity_id": activity_id, "status": updated["status"]}

    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {
            "error": {
                "code": "CONSTRAINT_VIOLATION",
                "message": str(e),
            }
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
