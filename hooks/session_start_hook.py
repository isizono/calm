"""SessionStart hook: セッションレベル文脈注入

サービス層経由でDBからデータを取得し、セッション開始時のコンテキストを注入する。
- アクティビティ一覧（作業中・優先のみ個別表示。末尾に固定ナビ+未表示件数句）
- 振る舞い（正は~/.claude/rules配下の自動生成ファイル。本hookは投影ファイルの
  鮮度検証と、読み込めていないセッションへの縮退フォールバックのみを担う）
- relay Monitor監視指示（CCM_RELAY_SESSION_AWARE=1のときのみ。identity解決に
  成功した場合は常時、未読N件の報告行のみ未読が実在するときに追加）

コンテキスト取得フローガイドはここでは注入しない（check_in初回呼び出し時に
checkin_service側が埋め込む）。
"""
import functools
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
from hooks.readable_id_format import format_readable_id
from src.services.habit_service import (
    get_active_habit_contents_with_conn,
    list_intelligently_habit_manifest_with_conn,
)
from src.services import habit_projection
from src.services.backup_service import health_check, should_take_snapshot, take_snapshot
from hooks.signal_capture import try_capture_signal

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


def _build_fixed_nav(undisplayed_count: int, pinned_undisplayed_count: int) -> str:
    """一覧セクション末尾の固定ナビを組み立てる。

    check_in（なければactivity-start経由で作成）への導線が唯一の静的文。
    undisplayed_count（表示対象母集団=active全件から実際に表示した件数を
    引いた値）とpinned_undisplayed_count（そのうちpinnedの数）は呼び出し元が
    機械算出した値をそのまま渡す想定（概算・手書き禁止）。
    undisplayed_count が 0 なら件数句ごと省略し前半文のみ、
    pinned_undisplayed_count が 0 なら括弧句のみ省略する。
    """
    base = "作業開始時は該当アクティビティにcheck_in（なければ作成 — activity-start）。"
    if undisplayed_count <= 0:
        return base
    if pinned_undisplayed_count > 0:
        remainder = (
            f"未表示のアクティビティ{undisplayed_count}件"
            f"（pinned {pinned_undisplayed_count}件含む）"
            "や過去の文脈はget_activities・search・get系で取得する。"
        )
    else:
        remainder = (
            f"未表示のアクティビティ{undisplayed_count}件や"
            "過去の文脈はget_activities・search・get系で取得する。"
        )
    return base + remainder


def _build_activities_section(conn, session_id: str | None = None, source: str | None = None) -> str:  # source: buildersループの統一シグネチャ（本セクションは未使用）
    """アクティビティ一覧を組み立てる。

    階層 1「作業中（別セッション）」: heartbeat 中で自セッションでないもの。
    階層 2「優先」: 階層 1 に入らなかった activity のうち、
        (in_progress かつ updated_at が config.TIER2_MAX_AGE_DAYS 日以内) または
        (pinned かつ updated_at が config.PIN_SURFACE_DECAY_DAYS 日以内) を集約し、
        pinned 先頭 → updated_at 降順で上位 5 件（flat、topic 別グルーピングなし）。
        pinned が decay 日数を超えると階層 2 から外れる（pin 自体は残り、
        activity を touch すれば updated_at 更新により自動復帰する）。

    行フォーマット:
        - 階層 1: `- 📌 タイトル (#id) (Nd)`（📌 は pinned 時のみ）
        - 階層 2: 番号 + status マーカー (●/○) + 📌（pinned 時）+ タイトル (#id)
          + (Nd) + 🆕（24h 以内作成時）。blocked_by 未解決依存があるときのみ
          meta 行 1 行を続ける。

    末尾には固定ナビ（_build_fixed_nav）を必ず付与する。階層 1・2 がともに
    0 件（activity が 1 件も無い場合を含む）のときは、ヘッダ・末尾固定文の
    空殻を出さず固定ナビだけを返す。

    重複排除: 上位階層に採用された activity は下位階層（および統計対象）から除外する。

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

    seen_ids: set[int] = set()

    tier1: list[dict] = []
    for a in all_active:
        is_own_session = (
            session_id is not None
            and a.get("last_heartbeat_session_id") == session_id
        )
        if a.get("is_heartbeat_active") and not is_own_session:
            tier1.append(a)
    for a in tier1:
        seen_ids.add(a["id"])

    # 階層 1 で消費済みの id はバッチ取得対象から外す。
    lower_ids = [a["id"] for a in all_active if a["id"] not in seen_ids]
    unresolved_deps = _get_unresolved_deps(conn, lower_ids)
    created_ats = _get_created_ats(conn, lower_ids)

    tier2_pool = [
        a
        for a in all_active
        if a["id"] not in seen_ids
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
    tier2 = tier2_pool[:_TIER2_MAX_ITEMS]
    for a in tier2:
        seen_ids.add(a["id"])

    undisplayed = [a for a in all_active if a["id"] not in seen_ids]
    nav = _build_fixed_nav(
        len(undisplayed),
        sum(1 for a in undisplayed if a["id"] in pinned_ids),
    )

    if not tier1 and not tier2:
        return nav

    parts: list[str] = ["# アクティビティ一覧", ""]

    if tier1:
        tier1.sort(key=lambda a: (a["updated_at"], a["id"]), reverse=True)
        parts.append("## 作業中（別セッション）")
        for a in tier1:
            days = _calc_elapsed_days(a["updated_at"])
            pin_mark = f"{_PIN_MARK} " if a["id"] in pinned_ids else ""
            display = format_readable_id(a["id"], a["title"])
            parts.append(f"- {pin_mark}{display} ({days}d)")
        parts.append("")

    if tier2:
        parts.append("## 優先")
        for idx, a in enumerate(tier2, start=1):
            parts.extend(
                _render_numbered_line(a, idx, pinned_ids, created_ats, unresolved_deps)
            )
        parts.append("")

    parts.append(_DETERMINISTIC_RENDER_NOTICE)
    parts.append("")
    parts.append(nav)

    return "\n".join(parts) + "\n"


def _render_numbered_line(
    a: dict,
    idx: int,
    pinned_ids: set[int],
    created_ats: dict[int, str],
    unresolved_deps: dict[int, list[dict]],
) -> list[str]:
    """階層 2 用の 1 activity 分の行群を返す。

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
    display = format_readable_id(aid, a["title"])
    lines = [f"{idx}. {status_mark} {pin_mark}{display} ({days}d){new_marker}"]

    deps = unresolved_deps.get(aid, [])
    if deps:
        dep_titles = [f"{d['title']}({d['status']})" for d in deps]
        lines.append(f"   blocked_by: {', '.join(dep_titles)}")
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
    ケース）・kill switch（CCM_HABITS_RULES_EXPORT=0）・SessionStart(source=
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


def _build_habits_section(conn, session_id: str | None = None, source: str | None = None) -> str:  # conn, session_id: buildersループの統一シグネチャ
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


def _build_sync_policy_section(conn, session_id: str | None = None, source: str | None = None) -> str:  # conn, session_id, source: buildersループの統一シグネチャ
    """sync_policyが設定されていれば注入する。未設定時はコンテキスト消費ゼロ。"""
    if not config.SYNC_POLICY:
        return ""
    return f"# sync_policy\n{config.SYNC_POLICY}\n"


def _build_signals_section(conn, session_id: str | None = None, source: str | None = None) -> str:  # conn, session_id, source: buildersループの統一シグネチャ
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


def _build_relay_inbox_section(conn, session_id: str | None = None, source: str | None = None) -> str:  # conn, session_id, source: buildersループの統一シグネチャ
    """identityが解決できる限りMonitor監視指示を常時出す。未読件数の表示のみ0件時は省く。

    CCM_RELAY_SESSION_AWARE（デフォルトOFF）のkill switch。OFF時はtokenチェック・
    identity解決を一切試みず空文字を返す。relayを使わないユーザー・セッションに
    関連コンテキストを注入しないための入口ゲート。

    ON時、relay未構成（token未設定）ならidentity解決を試みる前に打ち切る。
    本hookはSessionStart（Claude Code起動をブロックする経路）で毎回実行される
    ため、identity解決の前にコストの小さいtokenチェックを行い、無駄な
    プロセスspawnを避ける。

    relay構成済みの場合、identity解決はまずsrc.services.relay.identity.
    get_relay_identity()（MCPリクエストのHTTPヘッダ経由）を試す。本hookは
    Claude Code CLIが起動する独立プロセスでMCPリクエストコンテキストを
    持たないため、この経路は常にNoneを返す。その場合はresolve_identity_by_
    ancestry()（祖先pidチェーンの一致でlauncherプロセスを特定する経路、
    ps最大5回spawn）にフォールバックする。

    Monitor監視指示はセッション作業中に届く新着を取りこぼさないための
    ものなので、既存の未読・inbox file有無に関わらずidentity解決できた
    時点で常に出す（inbox_path/count_unreadはファイル不在でも安全に動作する）。
    未読N件の報告行のみ、未読が実在するときに追加する。

    inbox fileはメッセージが1件もappendされるまで実体が無く、指示通りに
    `tail -f`する等のツールはfile不在だと即座に失敗する。表示前に
    ensure_inbox_file()でfileを先行生成し、この失敗を防ぐ。
    """
    if not config.RELAY_SESSION_AWARE_ENABLED:
        return ""

    from src.services.relay import config as relay_config

    if not relay_config.get_token():
        return ""

    from src.services.relay.identity import get_relay_identity, resolve_identity_by_ancestry

    identity = get_relay_identity() or resolve_identity_by_ancestry()
    if not identity:
        return ""

    from src.services.relay.inbox import count_unread, ensure_inbox_file

    path = ensure_inbox_file(identity)
    count = count_unread(identity)

    lines = []
    if count > 0:
        lines.append(f"relay inbox 未読: {count}件 → relay_receiveで消化")
    lines.append(f"新着の待ち受けはMonitorツールで {path} を監視してください。")
    return "\n".join(lines) + "\n"


def _build_transcript_path_section(
    conn, session_id: str | None = None, source: str | None = None, transcript_path: str | None = None
) -> str:  # conn, session_id, source: buildersループの統一シグネチャ
    """このセッションのtranscript pathを1行注入する。

    MCPサーバーのサブプロセスにはtranscript_pathが渡らないため（session_id同様、
    Claude Code側にIPC経路が無い）、SessionStart hookのstdinで受け取った値を
    ここで会話コンテキストに載せ、Claudeが明示引数として`detect_reask_candidates`等の
    tool呼び出しに転記する方式を取る。用途はsync-memoryステップ9（聞き返しの後追い検出）。
    """
    if not transcript_path:
        return ""
    return f"このセッションのtranscript path: {transcript_path}\n"


def _build_snapshot_section(conn, session_id: str | None = None, source: str | None = None) -> str:  # conn, session_id, source: buildersループの統一シグネチャ
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
            functools.partial(_build_transcript_path_section, transcript_path=transcript_path),
        ]
        for builder in builders:
            try:
                result = builder(conn, session_id, source)
                if result:
                    sections.append(result)
            except Exception:
                # セクション単位で失敗を許容し、残りのセクションは返す
                pass

        context = "\n".join(sections)
        return context

    finally:
        conn.close()


def main() -> None:
    try:
        raw = sys.stdin.read()
        session_id: str | None = None
        source: str | None = None
        transcript_path: str | None = None
        if raw:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    sid = payload.get("session_id")
                    if isinstance(sid, str) and sid:
                        session_id = sid
                    src = payload.get("source")
                    if isinstance(src, str) and src:
                        source = src
                    tp = payload.get("transcript_path")
                    if isinstance(tp, str) and tp:
                        transcript_path = tp
            except json.JSONDecodeError:
                # session_id/source/transcript_path 取得失敗時は従来挙動（self 照合なし・
                # compact判定なし・transcript path注入なし）にフォールバック。初期値 None
                # のまま継続するため再代入不要
                pass

        context = _build_session_context(session_id, source, transcript_path)

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
