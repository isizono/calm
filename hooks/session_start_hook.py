"""SessionStart hook: セッションレベル文脈注入

サービス層経由でDBからデータを取得し、セッション開始時のコンテキストを注入する。
- アクティビティ一覧（作業中・優先を個別表示、goal束縛の親には未完了の子を
  ツリーでぶら下げる。末尾にdomain別内訳の未表示節+凡例+固定ナビ）
- 振る舞い（正は~/.claude/rules配下の自動生成ファイル。本hookは投影ファイルの
  鮮度検証と、読み込めていないセッションへの縮退フォールバックのみを担う）
- open ask・回答済み未捌きaskの件数とタイトル一覧（メタaskは表示上限に関わらず
  常時全件表示、非メタは上限件数超過時のみ残り件数へ縮退。両バケットとも
  0件時は非表示）

コンテキスト取得フローガイドはここでは注入しない（check_in初回呼び出し時に
checkin_tier_service側が埋め込む）。
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# プロジェクトルートをパスに追加（src.db等の参照用）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import config
from src.db import get_connection, get_db_path
from src.harness import select_harness
from src.services.activity_service import (
    get_active_domains_with_conn,
    get_active_activities_by_tag_with_conn,
    get_pinned_active_activities_with_conn,
)
from hooks.hook_state import HookState
from hooks.readable_id_format import format_readable_id
from src.services import ask_service
from src.services.habit_service import (
    get_active_habit_contents_with_conn,
    list_intelligently_habit_manifest_with_conn,
)
from src.services import habit_projection
from src.services.backup_service import health_check, should_take_snapshot, take_snapshot
from src.services.injection_compositor import Section, compose
from src.services import session_registry_service
from hooks.signal_capture import try_capture_signal

_RECENT_CREATED_HOURS = 24
_PIN_MARK = "\U0001f4cc"
_NEW_MARK = "\U0001f195"
_CHILD_MARK_FAILED = "✕"  # ✕
_CHILD_MARK_WAITING = "◷"  # ◷
_CHILD_MARK_READY = "▷"  # ▷
_CHILD_MARK_ACHIEVED = "✓"  # ✓
_LEGEND_LINE = (
    f"{_CHILD_MARK_ACHIEVED}達成 {_CHILD_MARK_READY}着手できる "
    f"{_CHILD_MARK_WAITING}待ち {_CHILD_MARK_FAILED}失敗"
)


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


def _fetch_children_by_parent(conn, parent_ids: list[int]) -> dict[int, list[dict]]:
    """activity束縛のgoal条件から、親activity_idごとの子の一覧を一括取得する。

    親子はgoal_conditionsのbound_type='activity' / bound_idで結ばれる
    （goalはparent_ids側のactivityに紐づくものだけを見る）。子自身の
    status（completed/snoozed/shelved等）は問わない（子の条件がopenな限り
    未完了として扱う）ため、all_active経由ではなくactivitiesを直接引く。

    Returns:
        {parent_id: [{"parent_id", "condition_id", "state", "child_id",
                      "child_title"}, ...]（condition_id昇順）, ...}
    """
    if not parent_ids:
        return {}
    placeholders = ",".join("?" * len(parent_ids))
    rows = conn.execute(
        f"""
        SELECT ga.activity_id AS parent_id, gc.id AS condition_id, gc.state AS state,
               gc.bound_id AS child_id, c.title AS child_title
        FROM goal_activities ga
        JOIN goal_conditions gc ON gc.goal_id = ga.goal_id AND gc.bound_type = 'activity'
        JOIN activities c ON c.id = gc.bound_id
        WHERE ga.activity_id IN ({placeholders})
        ORDER BY gc.id
        """,
        tuple(parent_ids),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        result.setdefault(r["parent_id"], []).append(dict(r))
    return result


def _classify_children(
    conn, child_ids: list[int], unresolved_deps: dict[int, list[dict]]
) -> dict[int, str | None]:
    """未完了（open状態）の子1件ごとに、一覧表示用の状態記号を決める。

    決定事項「止まっている子を『待ち』と『着手できる』に分ける」の判定を
    そのまま実装する（check_inのattention判定・_stalled_hintは変更しない、
    別物として並存させる）。子自身のgoal（あれば）の判定結果・open ask・
    heartbeatの3種を子ごとに1回ずつのバッチ問い合わせでまとめて読む。
    depends_onの未完了はunresolved_depsを呼び出し元（blocked_by meta行の
    組み立てと共有するids_for_metaのバッチ問い合わせ）からそのまま受け取り、
    ここで再度問い合わせない。

    Returns:
        {child_id: "✕"|"◷"|"▷"|None, ...}（Noneは無印＝動いている）
    """
    if not child_ids:
        return {}
    placeholders = ",".join("?" * len(child_ids))
    rows = conn.execute(
        f"""
        SELECT a.id AS id,
               a.last_heartbeat_at AS last_heartbeat_at,
               CAST(
                   (julianday('now') - julianday(MAX(a.updated_at, COALESCE(a.last_heartbeat_at, ''))))
                   * 24 * 60 AS INTEGER
               ) AS minutes_since,
               (
                   SELECT 1 FROM ask_blocks ab
                   JOIN asks ak ON ak.id = ab.ask_id
                   WHERE ab.activity_id = a.id AND ak.status = 'open'
                   LIMIT 1
               ) AS has_open_ask,
               ga.goal_id AS own_goal_id,
               g.closed AS own_goal_closed,
               g.verdict AS own_goal_verdict
        FROM activities a
        LEFT JOIN goal_activities ga ON ga.activity_id = a.id
        LEFT JOIN goals g ON g.id = ga.goal_id
        WHERE a.id IN ({placeholders})
        """,
        tuple(child_ids),
    ).fetchall()

    own_goal_ids = sorted({r["own_goal_id"] for r in rows if r["own_goal_id"] is not None})
    human_wait_goal_ids: set[int] = set()
    if own_goal_ids:
        goal_placeholders = ",".join("?" * len(own_goal_ids))
        wait_rows = conn.execute(
            f"""
            SELECT DISTINCT goal_id FROM goal_conditions
            WHERE goal_id IN ({goal_placeholders}) AND actor IN ('human', 'external') AND state = 'open'
            """,
            tuple(own_goal_ids),
        ).fetchall()
        human_wait_goal_ids = {r["goal_id"] for r in wait_rows}

    marks: dict[int, str | None] = {}
    for r in rows:
        if r["own_goal_closed"] == 1 and r["own_goal_verdict"] == "failed":
            marks[r["id"]] = _CHILD_MARK_FAILED
            continue
        waiting = (
            bool(r["has_open_ask"])
            or (r["own_goal_id"] is not None and r["own_goal_id"] in human_wait_goal_ids)
            or r["id"] in unresolved_deps
        )
        if waiting:
            marks[r["id"]] = _CHILD_MARK_WAITING
            continue
        never_active = r["last_heartbeat_at"] is None
        stalled = (
            r["minutes_since"] is not None
            and r["minutes_since"] > config.HEARTBEAT_TIMEOUT_MINUTES
        )
        marks[r["id"]] = _CHILD_MARK_READY if (never_active or stalled) else None
    return marks


_DETERMINISTIC_RENDER_NOTICE = (
    "この一覧は CALM hook が決定論的に組み立てた表示用 markdown です。"
    "再フォーマットや優先順の再評価をせず、必要時はそのまま提示してください。"
)


def _build_fixed_nav() -> str:
    """一覧セクション末尾の固定ナビを組み立てる。

    check_in（なければactivity-start経由で作成）への導線と、未表示の内訳
    （ドメイン別件数と例示）・過去の文脈の取得経路を案内する静的文。
    未表示の実際の内訳は`## 未表示`節（_build_undisplayed_lines）が別途出す
    ため、ここでは固定文言のみを返す。
    """
    return (
        "作業開始時は該当アクティビティにcheck_in（なければ作成 — activity-start）。"
        "未表示や過去の文脈はget_activities・search・get系で取得する。"
    )


_UNDISPLAYED_EXAMPLE_DOMAINS = 3


def _build_undisplayed_lines(
    undisplayed: list[dict], domains: list[dict], domain_pool: dict[int, list[dict]]
) -> list[str]:
    """末尾『未表示』節の行群を組み立てる。

    domainごとの件数と、そのdomainで最近更新された順に2件のタイトルを
    例示する。未表示のいるdomainが `_UNDISPLAYED_EXAMPLE_DOMAINS + 1` 件を
    超えるとき（まとめ行が2 domain以上を受け持つとき）だけ畳み、件数上位
    `_UNDISPLAYED_EXAMPLE_DOMAINS` 件は例示付きで出し、残りは名前と件数だけを
    件数降順で1行にまとめる（省略せず全domain分を載せる）。
    domainタグを持たない未表示activity（pin経由のみ）は、そのタグを持つ
    domainが無いため、どの内訳行にも現れない（見出しの総数には数えるが、
    domain単位の内訳の対象外という受容済みの隙間）。
    """
    if not undisplayed:
        return []
    undisplayed_ids = {a["id"] for a in undisplayed}
    groups: list[tuple[str, list[dict]]] = []
    for domain in domains:
        members = [a for a in domain_pool.get(domain["tag_id"], []) if a["id"] in undisplayed_ids]
        if members:
            groups.append((domain["name"], members))
    groups.sort(key=lambda g: len(g[1]), reverse=True)

    fold = len(groups) > _UNDISPLAYED_EXAMPLE_DOMAINS + 1
    shown_groups = groups[:_UNDISPLAYED_EXAMPLE_DOMAINS] if fold else groups

    lines = [f"## 未表示 {len(undisplayed)}件"]
    for name, members in shown_groups:
        ordered = sorted(members, key=lambda a: a["updated_at"], reverse=True)
        examples = [a["title"] for a in ordered[:2]]
        suffix = " など" if len(members) > len(examples) else ""
        lines.append(f"- {name} {len(members)}件：{'、'.join(examples)}{suffix}")
    if fold:
        rest = groups[_UNDISPLAYED_EXAMPLE_DOMAINS:]
        summary = "、".join(f"{name} {len(members)}件" for name, members in rest)
        lines.append(f"- ほか{len(rest)} domain：{summary}")
    return lines


def _build_activities_section(conn, session_id: str | None = None, source: str | None = None, **_kwargs) -> str:  # source, **_kwargs: 全セクション共通シグネチャ（本セクションは未使用）
    """アクティビティ一覧を組み立てる。

    階層 1「作業中（別セッション）」: heartbeat 中で自セッションでなく、
        打刻主セッションの生存が確認できるもの（session_registry_service.
        is_session_alive）。生存確認できない場合（別名ファイルにエントリが
        無い、プロセスが死んでいる等）は死亡側に倒し、階層 1 には出さない。
    階層 2「優先」: 階層 1 に入らなかった activity のうち、
        (in_progress かつ updated_at が config.TIER2_MAX_AGE_DAYS 日以内) または
        (pinned かつ updated_at が config.PIN_SURFACE_DECAY_DAYS 日以内) を集約し、
        pinned 先頭 → updated_at 降順で上位 `config.TIER2_MAX_ITEMS` 件（既定5、
        flat、topic 別グルーピングなし）。
        pinned が decay 日数を超えると階層 2 から外れる（pin 自体は残り、
        activity を touch すれば updated_at 更新により自動復帰する）。

    行フォーマット: `#id タイトル`（📌 は pinned 時のみ先頭、🆕 は階層 2 で
    24h 以内作成時のみ末尾）。blocked_by 未解決依存があるときのみ meta 行
    1 行を続ける（階層 2 のみ）。goal条件がactivity束縛の親には、未完了
    （open状態）の子を `|` `├-` `└-` で行の下にぶら下げ、行の末尾に子の
    内訳（✓達成数 ▷着手できる数 ◷待ち数 ✕失敗数のうち0件でないもの）を
    付ける。openな子は必ず親の下にのみ出すため階層 1・2 の候補プールから
    除外する。束縛条件がsatisfied/waivedになった子（✓の内訳に数える分）は
    条件の充足と子自身のactivityの終了が別操作であるため除外しない
    （子が非completedのまま残っていれば通常のactivityと同じ基準で候補
    プールに残り、選ばれなければ未表示に数える）。

    末尾には『未表示』節（表示された親の下に出たopenな子は含まない）と
    凡例・固定文・固定ナビを付ける。active な activity が 1 件も無いとき
    は固定ナビだけを返す。

    重複排除: 上位階層に採用された activity は下位階層（および統計対象）から除外する。
    """
    domains = get_active_domains_with_conn(conn)

    seen_collect: set[int] = set()
    all_active: list[dict] = []
    domain_pool: dict[int, list[dict]] = {}
    for domain in domains:
        members = get_active_activities_by_tag_with_conn(conn, domain["tag_id"])
        domain_pool[domain["tag_id"]] = members
        for a in members:
            if a["id"] in seen_collect:
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
        seen_collect.add(a["id"])
        all_active.append(a)

    if not all_active:
        return _build_fixed_nav()

    all_active_ids = [a["id"] for a in all_active]
    children_by_parent = _fetch_children_by_parent(conn, all_active_ids)
    open_child_ids = sorted(
        {
            row["child_id"]
            for rows in children_by_parent.values()
            for row in rows
            if row["state"] == "open"
        }
    )
    # 除外するのはopenな子だけ（openな子は必ず親の下にツリー表示されるため、
    # 優先枠と二重に競合させない）。束縛条件がsatisfied/waivedになった子は、
    # 条件の充足と子自身のactivityの終了は別操作であるため状態問わず除外
    # しない。子自身が非completedのまま残っていれば、通常のactivityと同じ
    # 基準で候補プールに残り、選ばれなければ未表示に数える（状態を問わず
    # 除外すると、まだ動いている作業が一覧から消えてしまう）。
    child_ids_excluded_from_pool = set(open_child_ids)

    seen_ids: set[int] = set()

    # is_session_aliveはプロセス確認でpsサブプロセスを起動しうるため、同じ
    # last_heartbeat_session_idを持つ候補が複数あっても呼び出しは1回に抑える
    # （自セッション・鮮度切れの行は判定不要なので候補集合にも入れない）。
    # 子は自分の親の下にのみ出すため、heartbeat中でも階層1の候補から外す
    # （外さないと親の下と階層1に二重表示されうる）。
    tier1_candidates = [
        a
        for a in all_active
        if a["id"] not in child_ids_excluded_from_pool
        and a.get("is_heartbeat_active")
        and not (
            session_id is not None
            and a.get("last_heartbeat_session_id") == session_id
        )
    ]
    candidate_session_ids = {
        sid
        for a in tier1_candidates
        if (sid := a.get("last_heartbeat_session_id"))
    }
    alive_by_session_id = {
        sid: session_registry_service.is_session_alive(sid)
        for sid in candidate_session_ids
    }

    tier1: list[dict] = [
        a
        for a in tier1_candidates
        if alive_by_session_id.get(a.get("last_heartbeat_session_id"), False)
    ]
    for a in tier1:
        seen_ids.add(a["id"])

    # 階層 1 で消費済みの id はバッチ取得対象から外す。子（未完了分）の
    # blocked_by・🆕判定に使う分もここでまとめて引く。open_child_idsはこの
    # 集合の部分集合なので、_classify_childrenへ渡して問い合わせの重複を避ける。
    lower_ids = [a["id"] for a in all_active if a["id"] not in seen_ids]
    ids_for_meta = sorted(set(lower_ids) | set(open_child_ids))
    unresolved_deps = _get_unresolved_deps(conn, ids_for_meta)
    created_ats = _get_created_ats(conn, ids_for_meta)
    child_marks = _classify_children(conn, open_child_ids, unresolved_deps)

    tier2_pool = [
        a
        for a in all_active
        if a["id"] not in seen_ids
        and a["id"] not in child_ids_excluded_from_pool
        and (
            (
                a["status"] == "in_progress"
                and _calc_elapsed_days(a["updated_at"]) <= config.TIER2_MAX_AGE_DAYS
            )
            or (
                a["id"] in pinned_ids
                and _calc_elapsed_days(a["updated_at"]) <= config.PIN_SURFACE_DECAY_DAYS
            )
        )
    ]
    tier2_pool.sort(key=lambda a: (a["updated_at"], a["id"]), reverse=True)
    tier2_pool.sort(key=lambda a: 0 if a["id"] in pinned_ids else 1)
    tier2 = tier2_pool[:config.TIER2_MAX_ITEMS]
    for a in tier2:
        seen_ids.add(a["id"])

    displayed_ids = {a["id"] for a in tier1} | {a["id"] for a in tier2}
    # 未表示に数えなくてよいのは、実際にツリー行として出るopenな子だけ。
    # satisfied/waivedの子は行として出ないため、ここでの除外対象に含めない
    # （子自身が非completedのまま候補プールから漏れた場合は未表示に数える）。
    rendered_child_ids = {
        row["child_id"]
        for parent_id in displayed_ids
        for row in children_by_parent.get(parent_id, [])
        if row["state"] == "open"
    }

    undisplayed = [
        a for a in all_active if a["id"] not in seen_ids and a["id"] not in rendered_child_ids
    ]

    parts: list[str] = ["# アクティビティ一覧", ""]

    if tier1:
        tier1.sort(key=lambda a: (a["updated_at"], a["id"]), reverse=True)
        parts.append("## 作業中（別セッション）")
        for a in tier1:
            parts.extend(
                _render_activity_block(
                    a,
                    pinned_ids=pinned_ids,
                    created_ats=created_ats,
                    unresolved_deps=unresolved_deps,
                    children_by_parent=children_by_parent,
                    child_marks=child_marks,
                    include_meta=False,
                )
            )
        parts.append("")

    if tier2:
        parts.append("## 優先")
        for a in tier2:
            parts.extend(
                _render_activity_block(
                    a,
                    pinned_ids=pinned_ids,
                    created_ats=created_ats,
                    unresolved_deps=unresolved_deps,
                    children_by_parent=children_by_parent,
                    child_marks=child_marks,
                    include_meta=True,
                )
            )
        parts.append("")

    parts.extend(_build_undisplayed_lines(undisplayed, domains, domain_pool))
    if undisplayed:
        parts.append("")

    if tier1 or tier2:
        parts.append(_LEGEND_LINE)
        parts.append("")

    parts.append(_DETERMINISTIC_RENDER_NOTICE)
    parts.append("")
    parts.append(_build_fixed_nav())

    return "\n".join(parts) + "\n"


def _children_suffix(children: list[dict], child_marks: dict[int, str | None]) -> str:
    """親の行の末尾に付ける子の内訳（✓達成数 ▷着手できる数 ◷待ち数 ✕失敗数）を返す。

    並び順は_LEGEND_LINEと揃える（決定事項「一覧の状態記号は ✓ ▷ ◷ ✕ にする」）。
    0件のカテゴリは省く。子が1件も無い、またはどのカテゴリも0件のときは空文字列。
    """
    achieved = sum(1 for c in children if c["state"] in ("satisfied", "waived"))
    counts = {_CHILD_MARK_ACHIEVED: achieved, _CHILD_MARK_WAITING: 0, _CHILD_MARK_READY: 0, _CHILD_MARK_FAILED: 0}
    for c in children:
        if c["state"] != "open":
            continue
        mark = child_marks.get(c["child_id"])
        if mark in counts:
            counts[mark] += 1
    order = (_CHILD_MARK_ACHIEVED, _CHILD_MARK_READY, _CHILD_MARK_WAITING, _CHILD_MARK_FAILED)
    parts = [f"{sym}{n}" for sym in order if (n := counts[sym]) > 0]
    return ("  " + " ".join(parts)) if parts else ""


def _render_activity_block(
    a: dict,
    *,
    pinned_ids: set[int],
    created_ats: dict[int, str],
    unresolved_deps: dict[int, list[dict]],
    children_by_parent: dict[int, list[dict]],
    child_marks: dict[int, str | None],
    include_meta: bool,
) -> list[str]:
    """1 activity 分の行群（本体行 + 子のツリー）を返す。

    include_meta=True（階層 2）のときのみ blocked_by meta 行・🆕 マーカーを
    出す（階層 1 は従来どおり本体行のみ）。子のツリーは階層の別を問わず、
    未完了（open状態）の子だけを `|` `├-` `└-` で本体行の下にぶら下げる
    （子どうしのネストはしない＝子が親であっても孫は展開しない）。
    """
    aid = a["id"]
    pin_mark = f"{_PIN_MARK} " if aid in pinned_ids else ""
    display = format_readable_id(aid, a["title"])
    children = children_by_parent.get(aid, [])
    suffix = _children_suffix(children, child_marks)

    if include_meta:
        created_at_str = created_ats.get(aid, "")
        new_marker = (
            f" {_NEW_MARK}"
            if created_at_str and _is_recent_created(created_at_str)
            else ""
        )
        lines = [f"- {pin_mark}{display}{suffix}{new_marker}"]
        deps = unresolved_deps.get(aid, [])
        if deps:
            dep_titles = [f"{d['title']}({d['status']})" for d in deps]
            lines.append(f"   blocked_by: {', '.join(dep_titles)}")
    else:
        lines = [f"- {pin_mark}{display}{suffix}"]

    open_children = [c for c in children if c["state"] == "open"]
    if open_children:
        lines.append("  |")
        for i, c in enumerate(open_children):
            connector = "└-" if i == len(open_children) - 1 else "├-"  # └- / ├-
            mark = child_marks.get(c["child_id"])
            mark_prefix = f"{mark} " if mark else ""
            child_pin = f"{_PIN_MARK} " if c["child_id"] in pinned_ids else ""
            child_display = format_readable_id(c["child_id"], c["child_title"])
            lines.append(f"  {connector} {mark_prefix}{child_pin}{child_display}")
            child_deps = unresolved_deps.get(c["child_id"], [])
            if child_deps:
                dep_titles = [f"{d['title']}({d['status']})" for d in child_deps]
                lines.append(f"     blocked_by: {', '.join(dep_titles)}")
    return lines


_HABITS_STALE_NOTICE = (
    "habits rulesファイルが古かったため最新化した。今回のセッションには未反映のため、"
    "反映は次回セッション起動から。\n"
)

_HABITS_STALE_HEAL_FAILED_NOTICE = (
    "habits rulesファイルが古かったが、修復（書き込み）に失敗した。"
    "ファイルの内容は更新されていない可能性がある。最新のhabits内容を以下に直接表示する。\n"
)


def _build_degraded_habits_fallback(
    conn,
    *,
    always_contents: list[str] | None = None,
    manifest: list[dict] | None = None,
) -> str:
    """habits rules投影ファイルが当該セッションに効いていない前提の縮退注入。

    verify_and_healのabsent系（不在・破損・修復失敗を含む）・failed_stale
    （staleを検知したが修復書き込みに失敗し、最新内容が読めていない可能性がある
    ケース）・kill switch（CALM_HABITS_RULES_EXPORT=0）・SessionStart(source=
    compact)（rulesファイル内容がcompact後も保持されるかの実機検証が未了のため
    安全側に倒し無条件で呼ばれる）から呼ばれる。always層は全文、intelligently層は
    タイトルを列挙せず件数1行にとどめる（全文9,500字級の注入はpersisted-output
    退避を再発させるため行わない。詳細はget_habits）。

    always_contents/manifestを呼び出し元が既に取得済み（verify_and_healの戻り値）
    なら渡すことで、DBクエリの再実行を避ける。未指定（None）の場合のみ自前で
    クエリする（disabled statusなどverify_and_healがDBに触れていないケース向け）。
    """
    if always_contents is None or manifest is None:
        always_contents = get_active_habit_contents_with_conn(conn)
        manifest = list_intelligently_habit_manifest_with_conn(conn)

    if not always_contents and not manifest:
        return ""

    lines = ["# 振る舞い"]
    for content in always_contents:
        lines.append(f"- {content}")

    if manifest:
        lines.append(f"他の振る舞い: {len(manifest)}件 → get_habits で確認")

    return "\n".join(lines) + "\n"


def _build_habits_section(conn, session_id: str | None = None, source: str | None = None, **_kwargs) -> str:  # conn, session_id, **_kwargs: 全セクション共通シグネチャ
    """振る舞いセクションを組み立てる。

    正はhabits DBで、通常の配信は~/.claude/rules配下の自動生成ファイル
    （habit_projection.export、書き込み経路のcommit直後に実行）が担う。
    本セクションは当該セッションがその投影ファイルを読み込めているかを
    verify_and_healで検証するだけの縮退面であり、fresh（投影ファイルが
    最新で読み込み済み）なら何も注入しない。stale（投影ファイルは存在するが
    読み込んだ内容が古い可能性がある）で修復に成功した（healed_stale）場合は
    1行の通知のみ、修復（書き込み）に失敗した（failed_stale）場合はファイルが
    実際には更新されていないため、成功したかのような通知は返さず失敗が分かる
    通知＋_build_degraded_habits_fallbackによるalways層全文フォールバックを
    行う。absent（投影ファイルが存在しない・読めない。修復失敗を含む）または
    kill switch中も同様に_build_degraded_habits_fallbackへ委譲する。

    source == "compact" のときは、compact後にrulesファイルの内容が
    コンテキストに保持されるかが実機未検証のため、鮮度判定の結果に関わらず
    安全側に倒してalways層全文フォールバックを注入する（verify_and_healによる
    ファイル修復自体は通常通り行う）。
    """
    result = habit_projection.verify_and_heal(conn)
    status = result["status"]
    always_contents = result.get("always_contents")
    manifest = result.get("manifest")

    if source == "compact":
        return _build_degraded_habits_fallback(
            conn, always_contents=always_contents, manifest=manifest
        )

    if status == "fresh":
        return ""
    if status == "healed_stale":
        return _HABITS_STALE_NOTICE
    if status == "failed_stale":
        return _HABITS_STALE_HEAL_FAILED_NOTICE + _build_degraded_habits_fallback(
            conn, always_contents=always_contents, manifest=manifest
        )
    return _build_degraded_habits_fallback(
        conn, always_contents=always_contents, manifest=manifest
    )


def _build_signals_section(conn, session_id: str | None = None, source: str | None = None, **_kwargs) -> str:  # conn, session_id, source, **_kwargs: 全セクション共通シグネチャ
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


# 非メタaskの表示上限（件数）。budget_chars（config.INJECTION_BUDGET_OPEN_ASKS_CHARS）内に
# 収める必要から実装時に決めた固定値で、decisionでは具体的な件数は定められていない。
_OPEN_ASKS_NON_META_DISPLAY_LIMIT = 5

# ask_service.get_asks_with_connのlimit上限(_MAX_LIMIT)と揃えたページサイズ。
# metaaskは表示上限を持たず常時全件表示するため、_fetch_asks_with_guaranteed_meta
# はこの単位でoffsetをずらしながら全件になるまでページングする
# （1回のget_asks_with_conn呼び出しではlimitがこの値にクランプされ、
# 同一バケットのmetaaskがこの値を超えると取得漏れが起きるため）。
_ASK_SERVICE_MAX_LIMIT = 100


def _fetch_asks_with_guaranteed_meta(conn, *, base_kwargs: dict, non_meta_limit: int) -> dict:
    """base_kwargs（statusまたはtriage_pending_only等）に合致するaskを取得し、
    kind="meta"のものは非メタの表示上限に関わらず必ず含める。

    非メタ側の取得はkind="ask"で明示的に絞るため、上位non_meta_limit件の中に
    metaaskが混ざることはない（VALID_KINDS = {"ask", "meta"}でkindは排他的な
    ため）。non_meta_total_countもkind="ask"で絞った後の母集団件数であり、
    _render_open_asks_sectionはそこから表示済みnon_meta件数だけを引いて
    残り件数を算出する。

    meta側はget_asks_with_connのlimitが_ASK_SERVICE_MAX_LIMIT
    （=ask_service._MAX_LIMIT）にクランプされるため、1回の呼び出しでは
    同一バケットに101件以上あると取得漏れが起きる。offsetを
    _ASK_SERVICE_MAX_LIMIT刻みでずらし、total_countに達するまでページングして
    全件を組み立てることでこれを避ける。

    Returns:
        {"meta": [...], "non_meta": [...], "non_meta_total_count": int}
        取得失敗時: ask_service.get_asks_with_connの{"error": {...}}をそのまま返す
    """
    non_meta_result = ask_service.get_asks_with_conn(
        conn, kind="ask", limit=non_meta_limit, **base_kwargs
    )
    if "error" in non_meta_result:
        return non_meta_result

    meta_asks: list[dict] = []
    offset = 0
    while True:
        meta_page = ask_service.get_asks_with_conn(
            conn, kind="meta", limit=_ASK_SERVICE_MAX_LIMIT, offset=offset, **base_kwargs
        )
        if "error" in meta_page:
            return meta_page
        meta_asks.extend(meta_page["asks"])
        offset += _ASK_SERVICE_MAX_LIMIT
        if offset >= meta_page["total_count"]:
            break

    return {
        "meta": meta_asks,
        "non_meta": non_meta_result["asks"],
        "non_meta_total_count": non_meta_result["total_count"],
    }


_OPEN_ASKS_META_CTA = "→ メタaskはrule-placement skillでの配置検討が必要"
_OPEN_ASKS_GLOBAL_CTA = "→ ask-answer skillまたはget_asksで確認"


def _section_text_len(lines: list[str]) -> int:
    """linesを"\n".join(lines) + "\n"で連結した場合の文字数。

    連結後の文字数はsum(len(line) for line in lines) + len(lines)と等しく、
    lines内の順序に依存しない（結合で増える改行の本数は行数分だけであり、
    どの位置に挿入されるかは総文字数を変えないため）。これにより、最終的な
    行の並び順を確定させる前に「この行集合を採用した場合の総文字数」を
    計算できる。
    """
    return sum(len(line) for line in lines) + len(lines)


def _render_open_asks_section(open_result: dict, pending_result: dict, budget_chars: int) -> str:
    """open_result/pending_resultから、セクション全体のテキストを組み立てる。

    見出し（内容が1件以上あるバケットのみ）・meta行（バケットごとに全件、
    kind='meta'は常時表示するという不変条件を非メタの表示より優先する）・
    meta向けCTA（meta1件以上のバケットのみ）・末尾の全体CTAを「必須要素」
    とし、これらは budget_chars検査を一切行わずに必ず含める。必須要素
    全体の文字数を先に確定し、budget_charsとの差分を非メタ用の「残り予算」
    とする。

    非メタの行と残り件数行（「他N件」）は「調整可能要素」であり、この残り
    予算に収まる範囲でのみ1行ずつ追加する（budget_charsが小さい・meta件数が
    多い場合は非メタが0件になってもよい）。候補行の長さは可変であり、1行が
    残り予算に収まらなくても後続の候補（より短い残り件数行等）が収まる余地は
    残るため、収まらなかった候補があっても走査を打ち切らない（一方向ラッチは
    使わない）。残り件数行は、そのバケットの非メタ行を追加し終えて実際の
    表示件数が確定した後に組み立てて予算検査する。この際、残り件数行の
    最悪長（非メタが1件も入らなかった場合の「他{全件数}件」の長さ）を
    「全バケット分まとめて」非メタ走査の前に一括予約しておき、各バケットの
    非メタ走査が終わった時点でそのバケット自身の予約分だけを解放して実際の
    残り件数行の長さで判定し直す。予約をバケット単位（そのバケットに入って
    から予約する）にすると、先行バケットの非メタ走査は「自分より後のバケット
    が残り件数行を出すのに必要な予算」を知らずに残り予算を使い切れてしまい、
    後続バケットの入口でavailableが自分の予約コストにすら届かず、予約自体が
    成立しないまま見出しだけが残ることがある。全バケット分を先頭で確保して
    おけば、どのバケットの非メタ走査も他バケットの残り件数行の予算を侵食
    できない。

    必須要素だけでbudget_charsを超える場合（meta askの絶対量が非常に多い場合）
    は、非メタ・残り件数行は自然に0件となるが、それでも返り値がbudget_charsを
    超えることがある。この場合は呼び出し元（injection_compositor._hard_truncate
    経由のcompose()）のハード切り詰めに委ねる。必須要素はバケット順（見出し→
    meta行→meta向けCTA、これをバケット数分繰り返す）で連結し全体CTAを最後に
    置くため、末尾から切り詰められるのはまず全体CTA、次に最後のバケット
    （回答済み未捌き）の必須要素である。バケットが2つ以上あり後方のバケットに
    metaが含まれる場合、そのmeta本文は末尾に近い位置になり切り詰め対象になり
    うる（先頭バケット側のmetaより先に失われる）。全バケットのmetaを同時に
    守ることはbudget_chars自体を超える入力では原理的に不可能なため、これは
    受容する残余リスクであり、meta常時表示の不変条件を壊さない範囲での最善
    である。
    """
    buckets = [
        (label, bucket)
        for label, bucket in (("open ask", open_result), ("回答済み未捌き", pending_result))
        if bucket["meta"] or bucket["non_meta"]
    ]
    if not buckets:
        return ""

    # 必須要素（見出し・meta行・meta向けCTA）をバケットごとに確定する。
    required_by_bucket: list[list[str]] = []
    for label, bucket in buckets:
        bucket_lines = [f"## {label}"]
        for a in bucket["meta"]:
            bucket_lines.append(f"- [meta] (#{a['id_raw']}) {a['question']}")
        if bucket["meta"]:
            bucket_lines.append(_OPEN_ASKS_META_CTA)
        required_by_bucket.append(bucket_lines)

    all_required_lines = [line for bucket_lines in required_by_bucket for line in bucket_lines]
    all_required_lines.append(_OPEN_ASKS_GLOBAL_CTA)
    available = budget_chars - _section_text_len(all_required_lines)

    # 残り件数行の最悪長（バケットごとの「他{total_count}件」の長さ）を
    # 全バケット分まとめて先頭で一括予約する。バケット単位の予約だと、
    # 先行バケットの非メタ走査がこの予約を知らずにavailableを使い切り、
    # 後続バケットの入口でその自分の予約すら成立しなくなる（後述の
    # ループ内で個別に解放する予約と役割が異なる）。
    reserved_by_bucket = [
        (len(f"他{bucket['non_meta_total_count']}件") + 1)
        if bucket["non_meta_total_count"] > 0
        else 0
        for _, bucket in buckets
    ]
    available -= sum(reserved_by_bucket)

    # 非メタ行・残り件数行（調整可能要素）を、残り予算(available)の範囲内で
    # バケット順に1行ずつ追加する。
    optional_by_bucket: list[list[str]] = []
    for (label, bucket), reserved in zip(buckets, reserved_by_bucket):
        bucket_optional: list[str] = []
        non_meta = bucket["non_meta"]
        total_count = bucket["non_meta_total_count"]

        shown = 0
        for a in non_meta:
            candidate = f"- (#{a['id_raw']}) {a['question']}"
            cost = len(candidate) + 1
            if cost <= available:
                bucket_optional.append(candidate)
                available -= cost
                shown += 1
            # 収まらなくても走査は継続する（後続の非メタ行が短ければ収まりうる）

        available += reserved  # このバケット自身の予約だけを解放し、実際の残り件数行で判定し直す
        remainder = total_count - shown
        if remainder > 0:
            candidate = f"他{remainder}件"
            cost = len(candidate) + 1
            if cost <= available:
                bucket_optional.append(candidate)
                available -= cost

        optional_by_bucket.append(bucket_optional)

    lines: list[str] = []
    for bucket_required, bucket_optional in zip(required_by_bucket, optional_by_bucket):
        lines.extend(bucket_required)
        lines.extend(bucket_optional)
    lines.append(_OPEN_ASKS_GLOBAL_CTA)

    return "\n".join(lines) + "\n"


def _build_open_asks_section(conn, session_id: str | None = None, source: str | None = None, **_kwargs) -> str:  # session_id, source, **_kwargs: 全セクション共通シグネチャ（本セクションは未使用）
    """open askと回答済み未捌き（status='answered' AND triage未了）askを
    kind別（メタ/非メタ）にタイトル表示する。

    メタaskはblocks先activityの状態や非メタの表示上限に関わらず常時全件表示する
    （ask_service.get_asks_with_connがblocksでフィルタしないため、blocks状態
    独立性は追加実装なしで自動的に満たされる）。非メタは
    _OPEN_ASKS_NON_META_DISPLAY_LIMIT件を上限としつつ、実際の表示件数は
    config.INJECTION_BUDGET_OPEN_ASKS_CHARSの範囲内に収まる件数まで縮退する
    （_render_open_asks_section参照。meta・見出し・CTAはこの予算検査より
    優先して確保される）。open・回答済み未捌きの両バケットとも0件時は
    コンテキスト消費ゼロ（空文字を返す）。取得失敗時（ask_service側が
    {"error": ...}を返す場合）も非表示にフォールバックする。
    """
    open_result = _fetch_asks_with_guaranteed_meta(
        conn, base_kwargs={"status": "open"}, non_meta_limit=_OPEN_ASKS_NON_META_DISPLAY_LIMIT
    )
    if "error" in open_result:
        return ""

    pending_result = _fetch_asks_with_guaranteed_meta(
        conn, base_kwargs={"triage_pending_only": True}, non_meta_limit=_OPEN_ASKS_NON_META_DISPLAY_LIMIT
    )
    if "error" in pending_result:
        return ""

    if not (open_result["meta"] or open_result["non_meta"] or pending_result["meta"] or pending_result["non_meta"]):
        return ""

    return _render_open_asks_section(open_result, pending_result, config.INJECTION_BUDGET_OPEN_ASKS_CHARS)


def _build_ask_notify_section(conn, session_id: str | None = None, source: str | None = None, **_kwargs) -> str:  # source, **_kwargs: 全セクション共通シグネチャ
    """add_askし通知待ちで追跡中のask（HookState.tracked_ask_ids）を
    get_asksで直接照会し、解決済み（open以外）になっていれば表示して
    追跡対象から外す（hooks/ask_notify_section.build_ask_notify_lines）。

    add_ask後の回答待ちhookが無いハーネス（Codex）や、待機が途切れた場合の
    二重網。identity解決（resolve_identity_by_ancestry等）には一切触れない。
    追跡登録自体はStop hook（hook_transcript.extract_ask_registrations）が担う。

    他のセクションビルダーと同様、本hookで共有されるconnをそのまま渡す
    （自前で別コネクションを開かない）。

    本セクションはcompose()経由でconfig.INJECTION_BUDGET_ASK_NOTIFY_CHARS
    以内にハード切り詰めされうる（injection_compositor._hard_truncate）。
    build_ask_notify_linesにbudget_charsを渡し、切り詰めで表示が欠落する
    行のask_idを消費済みにしてしまわないようにする（欠落したaskは
    UserPromptSubmit hook側の二重網が予算制約なしで拾う）。
    """
    from hooks.ask_notify_section import build_ask_notify_lines

    lines = build_ask_notify_lines(
        session_id, conn=conn, budget_chars=config.INJECTION_BUDGET_ASK_NOTIFY_CHARS
    )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _build_transcript_path_section(
    conn, session_id: str | None = None, source: str | None = None, transcript_path: str | None = None
) -> str:  # conn, session_id, source: 全セクション共通シグネチャ（本セクションは未使用）
    """このセッションのtranscript pathを1行注入する。

    MCPサーバーのサブプロセスにはtranscript_pathが渡らないため（session_id同様、
    Claude Code側にIPC経路が無い）、SessionStart hookのstdinで受け取った値を
    ここで会話コンテキストに載せ、Claudeが明示引数として`detect_reask_candidates`等の
    tool呼び出しに転記する方式を取る。用途はsync-memoryステップ9（聞き返しの後追い検出）。
    """
    if not transcript_path:
        return ""
    return f"このセッションのtranscript path: {transcript_path}\n"


def _build_snapshot_section(conn, session_id: str | None = None, source: str | None = None, **_kwargs) -> str:  # conn, session_id, source, **_kwargs: 全セクション共通シグネチャ
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
            "\u26a1 db-recovery スキルを発動して自律復旧を進めてください(手動手順は calm:man を参照)。",
        ])
        return "\n".join(lines) + "\n"

    # ヘルスチェックOKの場合のみスナップショット取得判定
    if should_take_snapshot(snapshot_dir, db_path=db_path):
        try:
            take_snapshot(db_path, snapshot_dir)
        except Exception as e:
            print(f"snapshot error: {e}", file=sys.stderr)

    return ""


# セクション登録レジストリ。priorityは既存builders順（出力順）をそのまま踏襲する。
# budget_charsは各セクションの宣言予算（文字数）で、実出力がこれを超えた場合
# compose()側でハード切り詰めされる（詳細はinjection_compositor.pyのdocstring参照）。
_SECTIONS: list[Section] = [
    Section("snapshot", _build_snapshot_section, config.INJECTION_BUDGET_SNAPSHOT_CHARS, priority=0),
    Section("activities", _build_activities_section, config.INJECTION_BUDGET_ACTIVITIES_CHARS, priority=10),
    Section("habits", _build_habits_section, config.INJECTION_BUDGET_HABITS_CHARS, priority=20),
    Section("signals", _build_signals_section, config.INJECTION_BUDGET_SIGNALS_CHARS, priority=40),
    Section("open_asks", _build_open_asks_section, config.INJECTION_BUDGET_OPEN_ASKS_CHARS, priority=41),
    Section("ask_notify", _build_ask_notify_section, config.INJECTION_BUDGET_ASK_NOTIFY_CHARS, priority=45),
    Section("transcript_path", _build_transcript_path_section, config.INJECTION_BUDGET_TRANSCRIPT_PATH_CHARS, priority=60),
]


def _build_session_context(
    session_id: str | None = None, source: str | None = None, transcript_path: str | None = None
) -> str:
    """サービス層経由でセッション開始時のコンテキストを組み立てる。

    session_id は session_start_hook の stdin payload に含まれる Claude Code 提供の
    識別子。アクティビティ一覧の「自セッション heartbeat」照合に使う。
    source は同 payload の "source"（startup|resume|clear|compact）。現状
    _build_habits_section のみが参照し、compact後にrulesファイル内容が
    コンテキストに保持されるかの実機未検証を安全側に倒すため使う。
    transcript_path は同payloadの"transcript_path"。_build_transcript_path_section
    のみが参照する。

    各セクションの組み立て・予算管理はinjection_compositor.composeへ委譲する
    （セクション単位try/except・宣言予算超過時の縮退はcompose側の責務）。
    """
    conn = get_connection()
    try:
        return compose(conn, session_id, source, transcript_path, _SECTIONS)
    finally:
        conn.close()


def main() -> None:
    harness = select_harness(hook_event_name="SessionStart")
    try:
        # 環境変数によるテスト用オーバーライド（stop_hook.py/user_prompt_submit_hook.py
        # と同じ規約。_build_ask_notify_sectionがHookStateを使うため、他hookと同様に
        # 本番既定パスへの書き込みをテストから隔離できるようにする）
        if os.environ.get("HOOK_STATE_DIR"):
            HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

        session_id: str | None = None
        source: str | None = None
        transcript_path: str | None = None
        try:
            payload = harness.read_hook_input()
        except json.JSONDecodeError:
            # session_id/source/transcript_path 取得失敗時は従来挙動（self 照合なし・
            # compact判定なし・transcript path注入なし）にフォールバック。初期値 None
            # のまま継続するため再代入不要
            payload = {}
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid
        src = payload.get("source")
        if isinstance(src, str) and src:
            source = src
        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp:
            transcript_path = tp

        context = _build_session_context(session_id, source, transcript_path)
        harness.emit_additional_context(context)
    except Exception as e:
        print(f"session_start_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:session_start", summary=str(e)[:200])
        harness.emit_empty()


if __name__ == "__main__":
    main()
