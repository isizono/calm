"""SessionStart hook: セッションレベル文脈注入

サービス層経由でDBからデータを取得し、セッション開始時のコンテキストを注入する。
- アクティビティ一覧（active = in_progress + pending）
- 振る舞い（active=1）
- コンテキスト取得フロー・補助ツール認知（静的テキスト）
- relay確認を促す静的ガイド（静的テキスト）
- relay inbox未読件数（identity解決に成功した場合のみ、動的）
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
    get_pinned_active_activities_with_conn,
)
from src.services.readable_id import format_readable_id
from src.services.habit_service import get_active_habit_contents_with_conn
from src.services.topic_service import get_activity_topics_batch
from src.services.backup_service import health_check, should_take_snapshot, take_snapshot
from hooks.signal_capture import try_capture_signal

_TIER4_STALE_DAYS = 30
_RECENT_CREATED_HOURS = 24
_TIER2_MAX_ITEMS = 5
_PIN_MARK = "\U0001f4cc"
_NEW_MARK = "\U0001f195"


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


def _is_recent_created(created_at_str: str, hours: int = _RECENT_CREATED_HOURS) -> bool:
    """created_atが指定時間以内かを判定する。"""
    try:
        created = datetime.fromisoformat(created_at_str).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - created).total_seconds() < hours * 3600
    except (ValueError, TypeError):
        return False


_DETERMINISTIC_RENDER_NOTICE = (
    "この一覧は cc-memory hook が決定論的に組み立てた表示用 markdown です。"
    "再フォーマットや優先順の再評価をせず、必要時はそのまま提示してください。"
)

_TOPICLESS_GROUP_LABEL = "その他"


def _shorten_topic_title(title: str) -> str:
    """topic見出しは「 — 」(em dash) 以降を除去した短縮版を使う。"""
    if " — " in title:
        return title.split(" — ", 1)[0].strip()
    return title


def _build_activities_section(conn, session_id: str | None = None) -> str:
    """アクティビティ一覧を 4 階層ダッシュボードで組み立てる。

    階層 1「作業中（別セッション）」: heartbeat 中で自セッションでないもの。
    階層 2「優先」: 階層 1 に入らなかった in_progress または pinned を集約し、
        pinned 先頭 → updated_at 降順で上位 5 件（flat、topic 別グルーピングなし）。
    階層 3「直近作成（24h以内）」: 上位階層で消費されなかった created_at 24h 以内、
        created_at 降順の flat リスト。0 件時はセクション自体省略。
    階層 4「その他」: 残り active のうち updated_at 30 日以内、または pinned。
        topic 別グルーピングでタイトル一行のみ（番号・status マーカー・meta 行なし）。

    行フォーマット:
        - 階層 1: `- 📌 タイトル (#id) (Nd)`（📌 は pinned 時のみ）
        - 階層 2/3: 番号 + status マーカー (●/○) + 📌（pinned 時）+ タイトル (#id)
          + (Nd) + 🆕（24h 以内作成時）。blocked_by 未解決依存があるときのみ
          meta 行 1 行を続ける。
        - 階層 4: `- 📌 タイトル (#id) (Nd) 🆕`（📌/🆕 は該当時のみ）

    重複排除: 上位階層に採用された activity は下位階層から除外する。

    orch_managed=1 のアクティビティは全階層で除外する。
    """
    domains = get_active_domains_with_conn(conn)

    seen_collect: set[int] = set()
    all_active: list[dict] = []
    for domain in domains:
        for a in get_active_activities_by_tag_with_conn(conn, domain["tag_id"]):
            if a["id"] in seen_collect:
                continue
            if a.get("orch_managed"):
                continue
            seen_collect.add(a["id"])
            all_active.append(a)

    # pinned は active domain の有無と独立して存在しうるため、
    # domain が 0 件でも早期 return せず必ず pinned を引く。
    pinned_all = get_pinned_active_activities_with_conn(conn)
    pinned_ids = {a["id"] for a in pinned_all}
    for a in pinned_all:
        if a["id"] in seen_collect:
            continue
        if a.get("orch_managed"):
            continue
        seen_collect.add(a["id"])
        all_active.append(a)

    if not all_active:
        return ""

    seen_ids: set[int] = set()
    parts: list[str] = ["# アクティビティ一覧", ""]

    tier1: list[dict] = []
    for a in all_active:
        is_own_session = (
            session_id is not None
            and a.get("last_heartbeat_session_id") == session_id
        )
        if a.get("is_heartbeat_active") and not is_own_session:
            tier1.append(a)

    if tier1:
        tier1.sort(key=lambda a: (a["updated_at"], a["id"]), reverse=True)
        parts.append("## 作業中（別セッション）")
        for a in tier1:
            seen_ids.add(a["id"])
            days = _calc_elapsed_days(a["updated_at"])
            pin_mark = f"{_PIN_MARK} " if a["id"] in pinned_ids else ""
            display = format_readable_id("activity", a["id"], a["title"])
            parts.append(f"- {pin_mark}{display} ({days}d)")
        parts.append("")

    # 階層 1 は updated_at と pin だけで描画し created_at / 依存 / topic を参照しない。
    # 階層 1 で消費済みの id はバッチ取得対象から外す。
    lower_ids = [a["id"] for a in all_active if a["id"] not in seen_ids]
    unresolved_deps = _get_unresolved_deps(conn, lower_ids)
    created_ats = _get_created_ats(conn, lower_ids)
    activity_topics = get_activity_topics_batch(conn, lower_ids)

    tier2_pool = [
        a
        for a in all_active
        if a["id"] not in seen_ids
        and (a["status"] == "in_progress" or a["id"] in pinned_ids)
    ]
    tier2_pool.sort(key=lambda a: (a["updated_at"], a["id"]), reverse=True)
    tier2_pool.sort(key=lambda a: 0 if a["id"] in pinned_ids else 1)
    tier2 = tier2_pool[:_TIER2_MAX_ITEMS]

    if tier2:
        parts.append("## 優先")
        for idx, a in enumerate(tier2, start=1):
            seen_ids.add(a["id"])
            parts.extend(
                _render_numbered_line(a, idx, pinned_ids, created_ats, unresolved_deps)
            )
        parts.append("")

    tier3_pool = [
        a
        for a in all_active
        if a["id"] not in seen_ids
        and _is_recent_created(created_ats.get(a["id"], ""))
    ]
    tier3_pool.sort(
        key=lambda a: (created_ats.get(a["id"], ""), a["id"]), reverse=True
    )

    if tier3_pool:
        parts.append("## 直近作成（24h以内）")
        for idx, a in enumerate(tier3_pool, start=1):
            seen_ids.add(a["id"])
            parts.extend(
                _render_numbered_line(a, idx, pinned_ids, created_ats, unresolved_deps)
            )
        parts.append("")

    # pinned は staleness で脱落させない。上位階層の件数上限で溢れた pinned が
    # 30 日フィルタでも除外されると、どの階層にも出ずダッシュボードから消えるため。
    tier4_pool = [
        a
        for a in all_active
        if a["id"] not in seen_ids
        and (
            a["id"] in pinned_ids
            or _calc_elapsed_days(a["updated_at"]) <= _TIER4_STALE_DAYS
        )
    ]

    if tier4_pool:
        groups: dict[int | None, dict] = {}
        for a in tier4_pool:
            topics = activity_topics.get(a["id"], [])
            if topics:
                primary = topics[0]
                primary_id: int | None = primary["id"]
                primary_title = _shorten_topic_title(primary["title"])
            else:
                primary_id = None
                primary_title = _TOPICLESS_GROUP_LABEL
            group = groups.setdefault(
                primary_id, {"title": primary_title, "activities": []}
            )
            group["activities"].append(a)

        topic_ids_sorted: list[int | None] = sorted(
            tid for tid in groups.keys() if tid is not None
        )
        if None in groups:
            topic_ids_sorted.append(None)

        for tid in topic_ids_sorted:
            group = groups[tid]
            group["activities"].sort(
                key=lambda a: (a["updated_at"], a["id"]), reverse=True
            )
            parts.append(f"## {group['title']}")
            for a in group["activities"]:
                seen_ids.add(a["id"])
                parts.append(_render_tier4_line(a, pinned_ids, created_ats))
            parts.append("")

    parts.append(_DETERMINISTIC_RENDER_NOTICE)
    parts.append("")

    return "\n".join(parts) + "\n"


def _render_numbered_line(
    a: dict,
    idx: int,
    pinned_ids: set[int],
    created_ats: dict[int, str],
    unresolved_deps: dict[int, list[dict]],
) -> list[str]:
    """階層 2/3 用の 1 activity 分の行群を返す。

    タイトル行 1 行と、blocked_by 未解決依存があるとき meta 行 1 行を続ける。
    """
    aid = a["id"]
    days = _calc_elapsed_days(a["updated_at"])
    status_mark = "●" if a["status"] == "in_progress" else "○"
    pin_mark = f"{_PIN_MARK} " if aid in pinned_ids else ""
    created_at_str = created_ats.get(aid, "")
    new_marker = (
        f" {_NEW_MARK}"
        if created_at_str and _is_recent_created(created_at_str)
        else ""
    )
    display = format_readable_id("activity", aid, a["title"])
    lines = [f"{idx}. {status_mark} {pin_mark}{display} ({days}d){new_marker}"]

    deps = unresolved_deps.get(aid, [])
    if deps:
        dep_titles = [f"{d['title']}({d['status']})" for d in deps]
        lines.append(f"   blocked_by: {', '.join(dep_titles)}")
    return lines


def _render_tier4_line(
    a: dict,
    pinned_ids: set[int],
    created_ats: dict[int, str],
) -> str:
    """階層 4 用のタイトル一行のみを返す。番号・status マーカー・meta 行なし。"""
    aid = a["id"]
    days = _calc_elapsed_days(a["updated_at"])
    pin_mark = f"{_PIN_MARK} " if aid in pinned_ids else ""
    created_at_str = created_ats.get(aid, "")
    new_marker = (
        f" {_NEW_MARK}"
        if created_at_str and _is_recent_created(created_at_str)
        else ""
    )
    display = format_readable_id("activity", aid, a["title"])
    return f"- {pin_mark}{display} ({days}d){new_marker}"


def _build_habits_section(conn, session_id: str | None = None) -> str:  # conn, session_id: buildersループの統一シグネチャ
    """振る舞い一覧を組み立てる。"""
    contents = get_active_habit_contents_with_conn(conn)

    if not contents:
        return ""

    lines = ["# 振る舞い"]
    for content in contents:
        lines.append(f"- {content}")

    return "\n".join(lines) + "\n"


def _build_sync_policy_section(conn, session_id: str | None = None) -> str:  # conn, session_id: buildersループの統一シグネチャ
    """sync_policyが設定されていれば注入する。未設定時はコンテキスト消費ゼロ。"""
    if not config.SYNC_POLICY:
        return ""
    return f"# sync_policy\n{config.SYNC_POLICY}\n"


def _build_signals_section(conn, session_id: str | None = None) -> str:  # conn, session_id: buildersループの統一シグネチャ
    """未トリアージ(status='new')のシグナル件数をkind内訳付きで1行表示する。

    0件時はコンテキスト消費ゼロ（空文字を返す）。signal_events テーブルが
    存在しない場合は例外が呼び出し元のsection単位try/exceptで握られ、
    セクション非表示にフォールバックする。
    """
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS c FROM signal_events WHERE status = 'new' GROUP BY kind"
    ).fetchall()
    if not rows:
        return ""

    total = sum(row["c"] for row in rows)
    breakdown = " / ".join(f"{row['kind']} {row['c']}" for row in rows)
    return f"未トリアージのシグナル: {total}件 ({breakdown}) → get_signals で確認\n"


def _build_relay_inbox_section(conn, session_id: str | None = None) -> str:  # conn, session_id: buildersループの統一シグネチャ
    """relay inboxの未読件数 + Monitor監視の指示を表示する。

    relay未構成（token未設定）ならidentity解決を試みる前に打ち切る。
    本hookはSessionStart（Claude Code起動をブロックする経路）で毎回実行される
    ため、identity解決の前にコストの小さいtokenチェックを行い、無駄な
    プロセスspawnを避ける。

    relay構成済みの場合、identity解決はまずsrc.services.relay.identity.
    get_relay_identity()（MCPリクエストのHTTPヘッダ経由）を試す。本hookは
    Claude Code CLIが起動する独立プロセスでMCPリクエストコンテキストを
    持たないため、この経路は常にNoneを返す。その場合はresolve_identity_by_
    ancestry()（祖先pidチェーンの一致でlauncherプロセスを特定する経路、
    ps最大5回spawn）にフォールバックする。

    identityが解決できてもinbox file未作成（このidentity宛のrelay
    メッセージが一度も無い）の場合は、そこで打ち切ってコンテキスト消費
    ゼロを維持する。
    """
    from src.services.relay import config as relay_config

    if not relay_config.get_token():
        return ""

    from src.services.relay.identity import get_relay_identity, resolve_identity_by_ancestry

    identity = get_relay_identity() or resolve_identity_by_ancestry()
    if not identity:
        return ""

    from src.services.relay.inbox import count_unread, inbox_path

    path = inbox_path(identity)
    if not path.exists():
        return ""

    count = count_unread(identity)
    return (
        f"relay inbox 未読: {count}件\n"
        f"relay通知の受信待機: Monitorツールで {path} を監視し、変更を検知したら"
        " relay_receive で新着を読んでください。未読がある場合は先に"
        " relay_receive で消化してください。\n"
    )


def _build_snapshot_section(conn, session_id: str | None = None) -> str:  # conn, session_id: buildersループの統一シグネチャ
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
            "\u26a1 db-recovery スキルを発動して自律復旧を進めてください(手動手順は cc-memory:guide を参照)。",
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

_RELAY_CHECK_GUIDE = """\
# relay

セッション開始時に `relay_receive` で他セッションからの未読メッセージがないか確認してください。
まず `peek=True` で呼び、内容を確認して必要なら add_logs/add_material 等で保存してください。
保存できたことを確認してから、同じ呼び出しを `peek=False`（既定）で呼び直して既読化して
ください。既定の `peek=False` は consume（既読化）のため、保存前に処理が中断すると
その内容は再取得できません。
"""


def _build_session_context(session_id: str | None = None) -> str:
    """サービス層経由でセッション開始時のコンテキストを組み立てる。

    session_id は session_start_hook の stdin payload に含まれる Claude Code 提供の
    識別子。アクティビティ一覧の「自セッション heartbeat」照合に使う。

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
            _build_signals_section,
            _build_relay_inbox_section,
        ]
        for builder in builders:
            try:
                result = builder(conn, session_id)
                if result:
                    sections.append(result)
            except Exception:
                # セクション単位で失敗を許容し、残りのセクションは返す
                pass

        # 静的セクション（DB不要）
        sections.append(_CONTEXT_FLOW_GUIDE)
        sections.append(_RELAY_CHECK_GUIDE)

        context = "\n".join(sections)
        return context

    finally:
        conn.close()


def main() -> None:
    try:
        raw = sys.stdin.read()
        session_id: str | None = None
        if raw:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    sid = payload.get("session_id")
                    if isinstance(sid, str) and sid:
                        session_id = sid
            except json.JSONDecodeError:
                # session_id 取得失敗時は従来挙動（self 照合なし）にフォールバック
                # 初期値 None のまま継続するため再代入不要
                pass

        context = _build_session_context(session_id)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        print(json.dumps(output, ensure_ascii=False))
    except Exception as e:
        print(f"session_start_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:session_start", summary=str(e)[:200])
        print("{}")


if __name__ == "__main__":
    main()
