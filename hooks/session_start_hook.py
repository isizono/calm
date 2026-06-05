"""SessionStart hook: セッションレベル文脈注入

サービス層経由でDBからデータを取得し、セッション開始時のコンテキストを注入する。
- アクティビティ一覧（active = in_progress + pending）
- 振る舞い（active=1）
- コンテキスト取得フロー・補助ツール認知（静的テキスト）
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# プロジェクトルートをパスに追加（src.db等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import config
from src.db import get_connection, get_db_path
from src.services.activity_service import (
    get_active_domains_with_conn,
    get_active_activities_by_tag_with_conn,
)
from src.services.habit_service import get_active_habit_contents_with_conn
from src.services.tag_service import get_entity_tags_batch
from scripts.snapshot import health_check, should_take_snapshot, take_snapshot

# description先頭の切り出し文字数
_DESCRIPTION_SNIPPET_LENGTH = 100



def _calc_elapsed_days(updated_at_str: str) -> int:
    """updated_atからの経過日数を計算する。"""
    try:
        updated = datetime.fromisoformat(updated_at_str).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - updated).days
    except (ValueError, TypeError):
        return 0


def _get_unresolved_deps(conn, activity_ids: list[int]) -> dict[int, list[dict]]:
    """アクティビティIDリストに対し、未完了の依存先を一括取得する。

    Returns:
        {dependent_id: [{"id": int, "title": str, "status": str}, ...], ...}
    """
    if not activity_ids:
        return {}
    placeholders = ",".join("?" * len(activity_ids))
    rows = conn.execute(
        f"""SELECT ad.dependent_id, a.id, a.title, a.status
            FROM activity_dependencies ad
            JOIN activities a ON a.id = ad.dependency_id
            WHERE ad.dependent_id IN ({placeholders})
              AND a.status != 'completed'""",
        tuple(activity_ids),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        dep_id = r["dependent_id"]
        if dep_id not in result:
            result[dep_id] = []
        result[dep_id].append({"id": r["id"], "title": r["title"], "status": r["status"]})
    return result


def _get_descriptions(conn, activity_ids: list[int]) -> dict[int, str]:
    """アクティビティIDリストに対し、descriptionを一括取得する。

    Returns:
        {activity_id: description_snippet, ...}
    """
    if not activity_ids:
        return {}
    placeholders = ",".join("?" * len(activity_ids))
    rows = conn.execute(
        f"SELECT id, description FROM activities WHERE id IN ({placeholders})",
        tuple(activity_ids),
    ).fetchall()
    result: dict[int, str] = {}
    for r in rows:
        desc = r["description"] or ""
        result[r["id"]] = desc[:_DESCRIPTION_SNIPPET_LENGTH]
    return result


def _get_created_ats(conn, activity_ids: list[int]) -> dict[int, str]:
    """アクティビティIDリストに対し、created_atを一括取得する。

    Returns:
        {activity_id: created_at, ...}
    """
    if not activity_ids:
        return {}
    placeholders = ",".join("?" * len(activity_ids))
    rows = conn.execute(
        f"SELECT id, created_at FROM activities WHERE id IN ({placeholders})",
        tuple(activity_ids),
    ).fetchall()
    return {r["id"]: r["created_at"] for r in rows}


def _is_recent_created(created_at_str: str, hours: int = 24) -> bool:
    """created_atが指定時間以内かを判定する。"""
    try:
        created = datetime.fromisoformat(created_at_str).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - created).total_seconds() < hours * 3600
    except (ValueError, TypeError):
        return False


_SCORING_INSTRUCTIONS = """\
# スコアリング指示
上記アクティビティから優先度の高い上位5件を選び、番号付きで表示してください。
判断基準:
- depends_on未完了 → 大幅減点（折りたたみ推奨）
- 締め切りが近い（descriptionから判断）→ 加点
- 自分がブロッカーになっている → 加点
- 鮮度が高い（最近更新） → やや加点（更新頻度が高いものは継続中の作業）
"""


def _build_activities_section(conn) -> str:
    """スコアリング用にアクティビティ一覧とメタデータを組み立てる。

    heartbeat中は別セクション表示。非heartbeatは番号付きフラットリストで出力し、
    AIスコアリング指示を末尾に追加する。
    """
    domains = get_active_domains_with_conn(conn)

    if not domains:
        return ""

    # 全アクティブアクティビティを収集（重複排除）
    seen_ids: set[int] = set()
    heartbeat_activities: list[dict] = []
    normal_activities: list[dict] = []

    for domain in domains:
        tag_id = domain["tag_id"]
        activities = get_active_activities_by_tag_with_conn(conn, tag_id)
        for a in activities:
            if a["id"] in seen_ids:
                continue
            seen_ids.add(a["id"])
            if a.get("is_heartbeat_active"):
                heartbeat_activities.append(a)
            else:
                normal_activities.append(a)

    if not heartbeat_activities and not normal_activities:
        return ""

    # メタデータ一括取得
    all_ids = [a["id"] for a in normal_activities]
    tags_map = get_entity_tags_batch(conn, "activity_tags", "activity_id", all_ids)
    unresolved_deps = _get_unresolved_deps(conn, all_ids)
    descriptions = _get_descriptions(conn, all_ids)
    created_ats = _get_created_ats(conn, all_ids)

    # normal_activitiesを「直近作成(24h以内)」と「それ以外」に分割
    recent_activities: list[dict] = []
    rest_activities: list[dict] = []
    for a in normal_activities:
        created_at_str = created_ats.get(a["id"], "")
        if created_at_str and _is_recent_created(created_at_str):
            recent_activities.append(a)
        else:
            rest_activities.append(a)
    # 直近作成セクションはcreated_at降順（新しい順）で並べる
    recent_activities.sort(
        key=lambda a: created_ats.get(a["id"], ""), reverse=True
    )

    def _render_activity_meta(aid: int, days: int) -> list[str]:
        meta_parts = [f"updated: {days}d ago"]
        tags = tags_map.get(aid, [])
        deps = unresolved_deps.get(aid, [])
        desc_snippet = descriptions.get(aid, "")
        if tags:
            meta_parts.append(f"tags: {', '.join(tags)}")
        if deps:
            dep_titles = [f"{d['title']}({d['status']})" for d in deps]
            meta_parts.append(f"blocked_by: {', '.join(dep_titles)}")
        if desc_snippet:
            meta_parts.append(f"desc: {desc_snippet}")
        return meta_parts

    parts = ["# アクティビティ一覧", ""]

    # heartbeat中は別セクション
    if heartbeat_activities:
        parts.append("## 作業中（別セッション）")
        for a in heartbeat_activities:
            days = _calc_elapsed_days(a["updated_at"])
            parts.append(f"- [{a['id']}] {a['title']} ({days}d)")
        parts.append("")

    # 直近作成（24h以内）: スコアリング対象の上に独立セクションとして配置
    if recent_activities:
        parts.append("## \U0001f195 直近作成（24h以内）")
        for idx, a in enumerate(recent_activities, 1):
            aid = a["id"]
            days = _calc_elapsed_days(a["updated_at"])
            status_mark = "●" if a["status"] == "in_progress" else "○"
            line = f"{idx}. {status_mark} [{aid}] {a['title']}"
            meta_parts = _render_activity_meta(aid, days)
            parts.append(line)
            parts.append(f"   {' | '.join(meta_parts)}")
        parts.append("")

    # 非heartbeat: 番号付きフラットリスト（直近作成に該当したものは除外）
    if rest_activities:
        parts.append("## スコアリング対象")
        for idx, a in enumerate(rest_activities, 1):
            aid = a["id"]
            days = _calc_elapsed_days(a["updated_at"])
            status_mark = "●" if a["status"] == "in_progress" else "○"
            line = f"{idx}. {status_mark} [{aid}] {a['title']}"
            meta_parts = _render_activity_meta(aid, days)
            parts.append(line)
            parts.append(f"   {' | '.join(meta_parts)}")

        total = len(rest_activities)
        parts.append(f"\n全{total}件")

        parts.append("")
        parts.append(_SCORING_INSTRUCTIONS)

    return "\n".join(parts) + "\n"


def _build_habits_section(conn) -> str:
    """振る舞い一覧を組み立てる。"""
    contents = get_active_habit_contents_with_conn(conn)

    if not contents:
        return ""

    lines = ["# 振る舞い"]
    for content in contents:
        lines.append(f"- {content}")

    return "\n".join(lines) + "\n"


def _build_sync_policy_section(conn) -> str:  # conn: buildersループの統一シグネチャ
    """sync_policyが設定されていれば注入する。未設定時はコンテキスト消費ゼロ。"""
    if not config.SYNC_POLICY:
        return ""
    return f"# sync_policy\n{config.SYNC_POLICY}\n"


def _build_snapshot_section(conn) -> str:
    """スナップショット取得＋ヘルスチェック。異常検知時のみ警告を返す。

    connは引数として受け取るが、snapshot.pyはdb_pathベースで動作するため
    内部でget_db_path()を使用する。
    """
    db_path = get_db_path()
    snapshot_dir = Path(db_path).parent / "snapshots"

    # ヘルスチェック
    result = health_check(db_path, snapshot_dir)

    if not result.is_healthy:
        lines = [
            "\U0001f6a8\U0001f6a8\U0001f6a8 【緊急】DBデータ異常減少を検知 \U0001f6a8\U0001f6a8\U0001f6a8",
            "",
            "前回スナップショットと比較して以下のテーブルで大幅なデータ減少を確認:",
        ]
        lines.extend(result.warnings)
        lines.extend([
            "",
            "\u26a1 データ消失インシデントの可能性があります。",
            "\u26a1 スナップショットからの復元が可能です。",
            "\u26a1 ユーザーに即座に状況を報告し、復元するか確認してください。",
            "\u26a1 復元手順は cc-memory:guide を参照してください。",
        ])
        return "\n".join(lines) + "\n"

    # ヘルスチェックOKの場合のみスナップショット取得判定
    if should_take_snapshot(snapshot_dir, db_path=db_path):
        try:
            take_snapshot(db_path, snapshot_dir)
        except Exception as e:
            print(f"snapshot error: {e}", file=sys.stderr)

    return ""


_CONTEXT_FLOW_GUIDE = """\
# コンテキスト取得フロー

1. ユーザーの発言やアクティビティ一覧から該当するものを判定し、`check_in`で作業コンテキストを取得する
   - ぴったりなアクティビティがなければ`add_activity`で作成してからcheck-inする
   - check-inするとtag_notes・資材・関連decisionsが一括で返り、statusも自動更新される
   - 返ってきたsummaryフィールドはそのまま出力すること
2. 特定エンティティの深掘りには`get_decisions`・`get_logs`を使う
   （議論の詳細な経緯はlogsに入っていることが多い）
3. キーワードベースの探索には`search`を使う。結果の詳細は`get_by_ids`でピンポイント取得する
4. `get_by_ids`の典型ユースケース:
   - search結果からのチェリーピック（関連する上位N件をまとめて詳細取得）
   - ログ・decision内の参照先をまとめて取得
   - ユーザーがIDで「これ何？」と聞いたとき

# 補助ツール・概念

- `update_pin`: 重要なエンティティをピン留めする。check-in時に必ず返されるので、長期にわたって参照され続けるエンティティにはピン留めを自律的に行うこと
  - 例: ユビキタス言語を定義したmaterial、方針を決定づけるdecision
- `get_map`: トピックやアクティビティの関連構造を俯瞰できる。全体像の把握や探索に有用
- `get_timeline`: エンティティの時系列変遷を追える。経緯を辿りたいときに有用
- リレーションタイプ `supersedes`・`depends_on`（`add_relation`で設定）: 差し替えられたdecisionやブロッカーのあるアクティビティの管理に使う
"""


def _build_session_context() -> str:
    """サービス層経由でセッション開始時のコンテキストを組み立てる。

    各セクションは独立してtry/exceptで保護し、
    一部のセクションが失敗しても残りは返す。
    """
    conn = get_connection()
    try:
        sections = []
        builders = [
            _build_snapshot_section,
            _build_activities_section,
            _build_habits_section,
            _build_sync_policy_section,
        ]
        for builder in builders:
            try:
                result = builder(conn)
                if result:
                    sections.append(result)
            except Exception:
                # セクション単位で失敗を許容し、残りのセクションは返す
                pass

        # 静的セクション（DB不要）
        sections.append(_CONTEXT_FLOW_GUIDE)

        context = "\n".join(sections)
        return context

    finally:
        conn.close()


def main() -> None:
    try:
        sys.stdin.read()  # stdinを消費（session_id等が渡されるが今は不使用）

        context = _build_session_context()

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        print(json.dumps(output, ensure_ascii=False))
    except Exception as e:
        print(f"session_start_hook.py error: {e}", file=sys.stderr)
        print("{}")


if __name__ == "__main__":
    main()
