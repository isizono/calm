"""バンドル書き出しサービス

collect_export_candidatesで確定した候補リストから、他インスタンスへ渡すための
バンドル(manifest.yaml + エンティティ別mdファイル)を書き出す。instance_meta基盤
(複合キー生成)を最初に利用するツール。

ディレクトリ構造:
    ~/cc-memory-export/bundles/<bundle-name>/
    ├── manifest.yaml
    ├── topics/T-<番号>-<slug>.md
    ├── decisions/D-<番号>-<slug>.md
    ├── logs/L-<番号>-<slug>.md
    ├── activities/A-<番号>-<slug>.md
    └── materials/M-<番号>-<slug>.md

複合キーは`<instance_id>:<型コード><ローカルID>`(例: team-a:M12)。import_provenance
テーブルはまだ無いため、本ツールが対象にするのは常に自インスタンス発のエンティティのみで、
複合キーはinstance_id + 自身のローカルIDから直接組み立てる(provenance逆引きは行わない)。
"""
import hashlib
import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import yaml

from src.db import get_connection, row_to_dict
from src.services.citations_pure import (
    OWNER_TEXT_FIELDS,
    TYPE_CODE_TO_NAME,
    TYPE_NAME_TO_CODE,
    TYPE_TO_TABLE,
    TYPE_TO_TITLE_EXPR,
    TYPES_WITH_RETRACT,
    _CITE_PATTERN,
    check_target_exists,
    convert_raw_to_cite,
)
from src.services.export_candidate_service import ALL_CATALOG_TYPES, _fetch_rows_for_type_with_conn
from src.services.instance_service import get_instance_id_with_conn
from src.services.internal_id_patterns import (
    RAW_CITE_CODE_PATTERN,
    RAW_CITE_FULLWORD_PATTERN,
)
from src.services.material_service import DEFAULT_EXPORT_DIR, _is_within_export_dir, _slugify_title
from src.services.tag_service import get_entity_tags_batch

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "ccm-bundle/1"
UNRESOLVED_MASK = "(解決不能な内部参照)"

# manifestのentities順・ディレクトリ作成順に使う固定順序
TYPE_ORDER = ["topic", "decision", "log", "activity", "material"]

_JUNCTION = {
    "topic": ("topic_tags", "topic_id"),
    "activity": ("activity_tags", "activity_id"),
    "material": ("material_tags", "material_id"),
    "decision": ("decision_tags", "decision_id"),
    "log": ("log_tags", "log_id"),
}

_DIR_NAME = {
    "topic": "topics",
    "decision": "decisions",
    "log": "logs",
    "activity": "activities",
    "material": "materials",
}

# 型別の本文メインフィールド(frontmatter下のh1直後に置く。decisionのみ2フィールド)
_MAIN_FIELD = {"material": "content", "log": "content", "activity": "description", "topic": "description"}


def _composite_key(instance_id: str, etype: str, local_id: int) -> str:
    return f"{instance_id}:{TYPE_NAME_TO_CODE[etype]}{local_id}"


def _validate_items(items) -> dict | None:
    if not isinstance(items, list) or not items:
        return {"error": {"code": "VALIDATION_ERROR", "message": "items must be a non-empty list"}}
    for item in items:
        if not isinstance(item, dict) or "type" not in item or "ids" not in item:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Each item must have 'type' and 'ids' fields",
                }
            }
        if item["type"] not in ALL_CATALOG_TYPES:
            return {
                "error": {
                    "code": "INVALID_ENTITY_TYPE",
                    "message": f"Invalid entity type: '{item['type']}'. Must be one of {sorted(ALL_CATALOG_TYPES)}",
                }
            }
        ids = item["ids"]
        if not isinstance(ids, list) or not ids:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"'ids' for type '{item['type']}' must be a non-empty list",
                }
            }
        for i in ids:
            if not isinstance(i, int) or isinstance(i, bool) or i <= 0:
                return {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": f"ids for type '{item['type']}' must be positive integers",
                    }
                }
    return None


# --- 本文パイプライン (生リテラル正規化 → 複合キー化 → 最終スイープ) ---


def _rewrite_composite_keys(text: str, key_of) -> tuple[str, list[tuple[str, int, str]]]:
    """本文中の`{{cite:X#NNN}}`を`{{cite:<composite_key>}}`に書き換える(パイプライン2段目)。

    key_of(target_type, target_id)が複合キー文字列を返せばそれで置換し、Noneを返せば
    (target がDB不在) `[deleted X#NNN]`に書き換える(citations_serviceのdangling処理と
    同じ表現)。フェンスコードブロック・インラインバッククォート・`\\{{cite:...}}`
    エスケープはスキップし元のまま残す(citations_pureの抽出ロジックと同じ境界判定)。

    Returns:
        (書き換え後テキスト, [(target_type, target_id, composite_key), ...] 出現順。
         dangling判定でNoneが返ったものはこのリストに含めない)
    """
    out_lines: list[str] = []
    resolved: list[tuple[str, int, str]] = []
    in_fence = False
    for raw_line in text.split("\n"):
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(raw_line)
            continue
        if in_fence:
            out_lines.append(raw_line)
            continue
        out_lines.append(_rewrite_cite_line(raw_line, key_of, resolved))
    return "\n".join(out_lines), resolved


def _rewrite_cite_line(line: str, key_of, resolved: list) -> str:
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            close = line.find("`", i + 1)
            if close == -1:
                out.append(line[i:])
                i = n
                continue
            out.append(line[i : close + 1])
            i = close + 1
            continue
        if ch == "\\" and line[i + 1 : i + 3] == "{{":
            end = line.find("}}", i + 1)
            if end == -1:
                out.append(ch)
                i += 1
                continue
            out.append(line[i : end + 2])
            i = end + 2
            continue
        m = _CITE_PATTERN.match(line, i)
        if m:
            code = m.group(1)
            target_id = int(m.group(2))
            target_type = TYPE_CODE_TO_NAME[code]
            key = key_of(target_type, target_id)
            if key is None:
                out.append(f"[deleted {code}#{target_id}]")
            else:
                out.append("{{cite:" + key + "}}")
                resolved.append((target_type, target_id, key))
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _mask_residual_raw_literals(text: str) -> tuple[str, int]:
    """内部ID漏洩判定と同じ広い正規表現(#省略のフルワード形式含む)で残存する生リテラルを
    検出しUNRESOLVED_MASKに置換する(パイプライン3段目、最終スイープ)。

    パイプライン1段目(convert_raw_to_cite)は自然文中の「type名+数字」の誤変換を避ける
    ため`#`必須パターンのみを対象にしており、`#`を省略したフルワード形式は素通りする。
    ここではその素通り分も含め、安全に複合キー化できなかった残存リテラルを一括で
    マスクする(DB上の実在確認は行わない。1段目で実在確認済みの形式は既に処理済みの
    ため、ここに残るのは「安全に変換できるか判断できない」形式のみと扱う)。
    """
    out_lines: list[str] = []
    in_fence = False
    total = 0
    for raw_line in text.split("\n"):
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(raw_line)
            continue
        if in_fence:
            out_lines.append(raw_line)
            continue
        masked_line, count = _mask_line(raw_line)
        total += count
        out_lines.append(masked_line)
    return "\n".join(out_lines), total


def _mask_line(line: str) -> tuple[str, int]:
    out: list[str] = []
    i = 0
    n = len(line)
    count = 0
    while i < n:
        ch = line[i]
        if ch == "`":
            close = line.find("`", i + 1)
            if close == -1:
                out.append(line[i:])
                i = n
                continue
            out.append(line[i : close + 1])
            i = close + 1
            continue
        if line[i : i + 7] == "{{cite:":
            end = line.find("}}", i + 7)
            if end == -1:
                out.append(ch)
                i += 1
                continue
            out.append(line[i : end + 2])
            i = end + 2
            continue
        if line[i : i + 9] == "[deleted ":
            end = line.find("]", i + 9)
            if end == -1:
                out.append(ch)
                i += 1
                continue
            out.append(line[i : end + 1])
            i = end + 1
            continue
        if ch == "\\":
            m = RAW_CITE_CODE_PATTERN.match(line, i + 1)
            if m and m.start() == i + 1:
                out.append(line[i : m.end()])
                i = m.end()
                continue
            m_fw = RAW_CITE_FULLWORD_PATTERN.match(line, i + 1)
            if m_fw and m_fw.start() == i + 1:
                out.append(line[i : m_fw.end()])
                i = m_fw.end()
                continue
            out.append(ch)
            i += 1
            continue
        m = RAW_CITE_CODE_PATTERN.match(line, i)
        if m:
            out.append(UNRESOLVED_MASK)
            count += 1
            i = m.end()
            continue
        m_fw = RAW_CITE_FULLWORD_PATTERN.match(line, i)
        if m_fw:
            out.append(UNRESOLVED_MASK)
            count += 1
            i = m_fw.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out), count


def _export_body_pipeline(
    conn: sqlite3.Connection, text: str, instance_id: str
) -> tuple[str, list[tuple[str, int, str]], int]:
    """本文をexport用の3段パイプラインで変換する。

    1. 生リテラル正規化: 生の`X#NNN`参照を`{{cite:X#NNN}}`へ正規化する(既存の書き込み
       経路と同じdangling判定。DB不在targetは`[deleted X#NNN]`に確定書き換え)
    2. 複合キー化: `{{cite:X#NNN}}`の参照先を複合キー形式`{{cite:<instance_id>:X<NNN>}}`
       へ書き換える。参照先が選択集合外でも書き換えは行う(importでの自己解決可能性を
       残すため)。DB不在targetは`[deleted X#NNN]`に書き換える
    3. 最終スイープ: 1段目が対象にしない形式等の残存生リテラルをマスクする

    Returns:
        (変換後テキスト, [(target_type, target_id, composite_key), ...] 複合キー化できた
         参照の出現順リスト, マスクした残存リテラル件数)
    """

    def _exists(target_type: str, target_id: int) -> bool:
        return check_target_exists(conn, target_type, target_id)

    step1, _counters = convert_raw_to_cite(text, target_validator=_exists)

    def _key_of(target_type: str, target_id: int) -> str | None:
        if not _exists(target_type, target_id):
            return None
        return _composite_key(instance_id, target_type, target_id)

    step2, refs = _rewrite_composite_keys(step1, _key_of)
    step3, masked_count = _mask_residual_raw_literals(step2)
    return step3, refs, masked_count


# --- リレーション取得ヘルパー ---


def _fetch_belongs_to_ids_with_conn(
    conn: sqlite3.Connection, etype: str, ids: list[int]
) -> dict[int, list[int]]:
    """子(decision/log/material/activity)→topicのbelongs_to先topic_idを一括取得する。"""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT source_id AS entity_id, target_id AS topic_id FROM relations "
        "WHERE relation_type = 'belongs_to' AND source_type = ? AND target_type = 'topic' "
        f"AND source_id IN ({placeholders})",
        (etype, *ids),
    ).fetchall()
    result: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        result[row["entity_id"]].append(row["topic_id"])
    return result


def _fetch_related_ids_with_conn(
    conn: sqlite3.Connection, etype: str, ids: list[int]
) -> dict[int, list[tuple[str, int]]]:
    """related(相互リンク)先を型を問わず一括取得する。"""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT source_id AS entity_id, target_type, target_id FROM relations_view "
        f"WHERE relation_type = 'related' AND source_type = ? AND source_id IN ({placeholders})",
        (etype, *ids),
    ).fetchall()
    result: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for row in rows:
        result[row["entity_id"]].append((row["target_type"], row["target_id"]))
    return result


def _fetch_supersedes_with_conn(conn: sqlite3.Connection, decision_ids: list[int]) -> dict[int, list[int]]:
    """decisionが supersede(kind='replaces') する先のdecision_idを一括取得する。"""
    if not decision_ids:
        return {}
    placeholders = ",".join("?" * len(decision_ids))
    rows = conn.execute(
        "SELECT source_id, target_id FROM decision_supersedes "
        f"WHERE kind = 'replaces' AND source_id IN ({placeholders})",
        decision_ids,
    ).fetchall()
    result: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        result[row["source_id"]].append(row["target_id"])
    return result


def _fetch_depends_on_with_conn(conn: sqlite3.Connection, activity_ids: list[int]) -> dict[int, list[int]]:
    if not activity_ids:
        return {}
    placeholders = ",".join("?" * len(activity_ids))
    rows = conn.execute(
        "SELECT dependent_id, dependency_id FROM activity_dependencies "
        f"WHERE dependent_id IN ({placeholders})",
        activity_ids,
    ).fetchall()
    result: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        result[row["dependent_id"]].append(row["dependency_id"])
    return result


def _fetch_unresolved_info_with_conn(
    conn: sqlite3.Connection, targets: set[tuple[str, int]]
) -> dict[tuple[str, int], dict]:
    """選択集合外の参照先について、title・domainタグを一括取得する。"""
    result: dict[tuple[str, int], dict] = {}
    by_type: dict[str, list[int]] = defaultdict(list)
    for etype, eid in targets:
        by_type[etype].append(eid)
    for etype, ids in by_type.items():
        table = TYPE_TO_TABLE[etype]
        title_expr = TYPE_TO_TITLE_EXPR[etype]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, {title_expr} AS title FROM {table} WHERE id IN ({placeholders})", ids
        ).fetchall()
        titles = {row["id"]: row["title"] for row in rows}
        junction, col = _JUNCTION[etype]
        tags_map = get_entity_tags_batch(conn, junction, col, ids)
        for eid in ids:
            domain_tags = [t for t in tags_map.get(eid, []) if t.startswith("domain:")]
            result[(etype, eid)] = {
                "title": titles.get(eid) or f"{etype}#{eid}",
                "domain_tags": domain_tags,
            }
    return result


# --- frontmatter / body / content_hash 構築 ---


def _build_frontmatter(
    etype: str,
    composite_key: str,
    title: str,
    tags: list[str],
    created_at: str,
    updated_at: str | None,
    retracted_at: str | None,
    belongs_to_keys: list[str],
    related_keys: list[str],
    supersedes: list[dict] | None,
    depends_on_keys: list[str] | None,
    source: str | None,
    status: str | None,
) -> str:
    data: dict = {
        "ccm_format": 1,
        "ccm_type": etype,
        "ccm_key": composite_key,
        "title": title,
        "tags": list(tags),
        "created_at": created_at,
    }
    if updated_at is not None:
        data["updated_at"] = updated_at
    if etype in TYPES_WITH_RETRACT:
        data["retracted_at"] = retracted_at
    if etype != "topic":
        data["belongs_to"] = belongs_to_keys
    data["related"] = related_keys
    if etype == "decision":
        data["supersedes"] = supersedes or []
    if etype == "activity":
        data["depends_on"] = depends_on_keys or []
    if etype == "material":
        data["source"] = source
    if etype == "activity":
        data["status"] = status
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{body}---\n"


def _build_body_text(etype: str, converted_title: str, converted_fields: dict[str, str]) -> str:
    if etype == "decision":
        return (
            f"# {converted_title}\n\n"
            "<!-- ccm:field decision -->\n"
            "## 決定\n"
            f"{converted_fields.get('decision', '')}\n\n"
            "<!-- ccm:field reason -->\n"
            "## 理由\n"
            f"{converted_fields.get('reason', '')}\n"
        )
    main_field = _MAIN_FIELD[etype]
    return f"# {converted_title}\n\n{converted_fields.get(main_field, '')}\n"


def _compute_content_hash(
    etype: str,
    converted_title: str,
    converted_fields: dict[str, str],
    tags: list[str],
    belongs_to_keys: list[str],
    related_keys: list[str],
    supersedes: list[dict] | None,
    depends_on_keys: list[str] | None,
    source: str | None,
    retracted_at: str | None,
) -> str:
    """プロトコル対象フィールドのみを固定順で連結したJSONのsha256を返す。

    created_at/updated_at/status(activity)は含めない。created_at/updated_atは
    「いつ書かれたか」というメタ情報でありコンテンツの同一性判定には不要、statusは
    ローカル運用状態(受け側で変更されうる)であり、これらを含めると無関係な変更で
    ハッシュが変わってしまう。tags/本文/関係エッジの変更は既にそれ自体が該当フィールド
    経由でハッシュに反映されるため、タイムスタンプでの二重シグナルは不要。
    """
    payload: dict = {"ccm_type": etype, "title": converted_title}
    for field in OWNER_TEXT_FIELDS[etype]:
        if field == "title":
            continue
        payload[field] = converted_fields.get(field, "")
    payload["tags"] = sorted(tags)
    if etype != "topic":
        payload["belongs_to"] = sorted(belongs_to_keys)
    payload["related"] = sorted(related_keys)
    if etype == "decision":
        payload["supersedes"] = sorted(f"{s['key']}|{s['kind']}" for s in (supersedes or []))
    if etype == "activity":
        payload["depends_on"] = sorted(depends_on_keys or [])
    if etype == "material":
        payload["source"] = source or ""
    if etype in TYPES_WITH_RETRACT:
        payload["retracted_at"] = retracted_at
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _default_bundle_name(instance_id: str, rows_by_key: dict, items: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    first_item = items[0]
    first_key = (first_item["type"], first_item["ids"][0])
    row_d = rows_by_key.get(first_key, {})
    title = row_d.get("resolved_title") or row_d.get("title") or ""
    slug = _slugify_title(title)
    if slug and slug != "untitled":
        return f"{instance_id}-{ts}-{slug}"
    return f"{instance_id}-{ts}"


def export_bundle(
    items: list[dict],
    bundle_name: str | None = None,
    include_supersede_targets: bool = False,
    selection: dict | None = None,
) -> dict:
    """確定した候補リストからバンドル(manifest.yaml + エンティティ別mdファイル)を書き出す。

    Args:
        items: 確定選択。[{"type": "topic"|"activity"|"material"|"decision"|"log", "ids": [int, ...]}, ...]
        bundle_name: バンドルディレクトリ名(省略時は`<instance_id>-<日時>-<起点slug>`)。
            出力先はDEFAULT_EXPORT_DIR配下に限定される(パスガード)
        include_supersede_targets: Trueのとき、選択decisionのsupersede先が選択集合外でも
            実体をバンドルに同梱する(デフォルトFalse。既定はエッジ情報のみ運ぶ)
        selection: collect_export_candidatesへの入力をverbatimで記録するための任意dict。
            manifest.yamlのselectionフィールドにそのまま書き込まれる(再exportの追跡用)

    親topicの自動同梱: 選択されたdecision/logのbelongs_to先topicは必ずバンドルに含まれる
    (機械規則、ユーザー裁定を経ない)。activityには適用しない(activityは常に明示選択のみ)。

    Returns:
        成功時: {"path": str, "bundle_id": str, "counts": {type: n}, "auto_included": [...],
            "unresolved_refs": [...], "masked_literals": int, "warnings": [...]}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    err = _validate_items(items)
    if err:
        return err

    conn = get_connection(load_vec=False)
    try:
        instance_id = get_instance_id_with_conn(conn)
        if instance_id is None:
            return {
                "error": {
                    "code": "INSTANCE_ID_NOT_SET",
                    "message": "instance_id is not set. Call set_instance_identity first.",
                }
            }

        requested: dict[str, set[int]] = defaultdict(set)
        for item in items:
            requested[item["type"]].update(item["ids"])

        rows_by_key: dict[tuple[str, int], dict] = {}
        missing: list[str] = []
        for etype, ids in requested.items():
            rows = _fetch_rows_for_type_with_conn(conn, etype, list(ids))
            found_ids: set[int] = set()
            for row in rows:
                row_d = row_to_dict(row)
                rows_by_key[(etype, row_d["id"])] = row_d
                found_ids.add(row_d["id"])
            for missing_id in sorted(ids - found_ids):
                missing.append(f"{etype}#{missing_id}")
        if missing:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"The following items do not exist: {', '.join(missing)}",
                }
            }

        selected: set[tuple[str, int]] = set(rows_by_key.keys())
        auto_included: list[dict] = []
        warnings: list[dict] = []

        # supersede先の扱い(decisionのみ)
        decision_ids = [eid for (etype, eid) in selected if etype == "decision"]
        supersede_map = _fetch_supersedes_with_conn(conn, decision_ids)
        external_supersedes: list[tuple[int, int]] = [
            (source_id, target_id)
            for source_id, target_ids in supersede_map.items()
            for target_id in target_ids
            if ("decision", target_id) not in selected
        ]

        if include_supersede_targets and external_supersedes:
            target_ids_to_add = sorted({t for _, t in external_supersedes})
            rows = _fetch_rows_for_type_with_conn(conn, "decision", target_ids_to_add)
            for row in rows:
                row_d = row_to_dict(row)
                key = ("decision", row_d["id"])
                rows_by_key[key] = row_d
                selected.add(key)
                auto_included.append({"type": "decision", "id_raw": row_d["id"], "reason": "supersede_target"})
            external_supersedes = [
                (s, t) for (s, t) in external_supersedes if ("decision", t) not in selected
            ]

        for source_id, target_id in external_supersedes:
            src_row = rows_by_key[("decision", source_id)]
            warnings.append(
                {
                    "kind": "supersede_target_outside",
                    "from_title": src_row.get("resolved_title") or src_row.get("title") or f"decision#{source_id}",
                    "target": {"type": "decision", "id_raw": target_id},
                }
            )

        # 親topic自動同梱(decision/logのみ、強制規則)
        new_topic_ids: set[int] = set()
        parent_map_by_etype: dict[str, dict[int, list[int]]] = {}
        for etype in ("decision", "log"):
            ids = [eid for (t, eid) in selected if t == etype]
            if not ids:
                continue
            parent_map = _fetch_belongs_to_ids_with_conn(conn, etype, ids)
            parent_map_by_etype[etype] = parent_map
            for topic_ids in parent_map.values():
                for tid in topic_ids:
                    if ("topic", tid) not in selected:
                        new_topic_ids.add(tid)
        if new_topic_ids:
            rows = _fetch_rows_for_type_with_conn(conn, "topic", list(new_topic_ids))
            for row in rows:
                row_d = row_to_dict(row)
                key = ("topic", row_d["id"])
                rows_by_key[key] = row_d
                selected.add(key)
                auto_included.append({"type": "topic", "id_raw": row_d["id"], "reason": "parent_topic"})

        # 型別ID一覧(タグ・リレーション一括取得用、最終確定済みselected基準)
        ids_by_type: dict[str, list[int]] = defaultdict(list)
        for etype, eid in selected:
            ids_by_type[etype].append(eid)

        tags_by_key: dict[tuple[str, int], list[str]] = {}
        for etype, ids in ids_by_type.items():
            junction, col = _JUNCTION[etype]
            batch = get_entity_tags_batch(conn, junction, col, ids)
            for eid, tags in batch.items():
                tags_by_key[(etype, eid)] = tags

        belongs_to_by_key: dict[tuple[str, int], list[int]] = {}
        for etype in ("activity", "material", "decision", "log"):
            if etype in parent_map_by_etype:
                m = parent_map_by_etype[etype]
            else:
                m = _fetch_belongs_to_ids_with_conn(conn, etype, ids_by_type.get(etype, []))
            for eid, topic_ids in m.items():
                belongs_to_by_key[(etype, eid)] = topic_ids

        related_by_key: dict[tuple[str, int], list[tuple[str, int]]] = {}
        for etype, ids in ids_by_type.items():
            m = _fetch_related_ids_with_conn(conn, etype, ids)
            for eid, targets in m.items():
                related_by_key[(etype, eid)] = targets

        supersedes_by_key = _fetch_supersedes_with_conn(conn, ids_by_type.get("decision", []))
        depends_on_by_key = _fetch_depends_on_with_conn(conn, ids_by_type.get("activity", []))

        # 出力先ディレクトリの決定(パスガード)
        raw_bundle_name = bundle_name or _default_bundle_name(instance_id, rows_by_key, items)
        safe_name = _slugify_title(raw_bundle_name) or "bundle"
        bundle_root = os.path.join(os.path.expanduser(DEFAULT_EXPORT_DIR), "bundles", safe_name)
        if not _is_within_export_dir(bundle_root):
            allowed = os.path.expanduser(DEFAULT_EXPORT_DIR)
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"bundle_name must resolve to a location within {allowed}. resolved path: {bundle_root}",
                }
            }

        entities_manifest: list[dict] = []
        counts: dict[str, int] = defaultdict(int)
        all_refs: list[tuple[str, int, tuple[str, int]]] = []
        total_masked = 0
        files_to_write: list[tuple[str, str]] = []

        ordered_selected = sorted(selected, key=lambda k: (TYPE_ORDER.index(k[0]), k[1]))
        for etype, eid in ordered_selected:
            row_d = rows_by_key[(etype, eid)]
            composite_key = _composite_key(instance_id, etype, eid)
            title_raw = row_d.get("resolved_title") or row_d.get("title") or ""

            converted_fields: dict[str, str] = {}
            entity_refs: list[tuple[str, int, str]] = []
            for field_name in OWNER_TEXT_FIELDS[etype]:
                text = row_d.get(field_name) or ""
                if not text:
                    converted_fields[field_name] = ""
                    continue
                converted, refs, masked = _export_body_pipeline(conn, text, instance_id)
                converted_fields[field_name] = converted
                entity_refs.extend(refs)
                total_masked += masked

            converted_title = converted_fields.get("title", title_raw)

            for target_type, target_id, _key in entity_refs:
                if (target_type, target_id) not in selected:
                    all_refs.append((target_type, target_id, (etype, eid)))

            belongs_to_ids = belongs_to_by_key.get((etype, eid), []) if etype != "topic" else []
            belongs_to_keys = [_composite_key(instance_id, "topic", tid) for tid in belongs_to_ids]
            related_targets = related_by_key.get((etype, eid), [])
            related_keys = [_composite_key(instance_id, t, i) for (t, i) in related_targets]

            supersedes_entries = None
            if etype == "decision":
                supersedes_entries = [
                    {"key": _composite_key(instance_id, "decision", tid), "kind": "replaces"}
                    for tid in supersedes_by_key.get(eid, [])
                ]
            depends_on_keys = None
            if etype == "activity":
                depends_on_keys = [
                    _composite_key(instance_id, "activity", did) for did in depends_on_by_key.get(eid, [])
                ]

            tags = tags_by_key.get((etype, eid), [])
            created_at = row_d.get("created_at")
            updated_at = row_d.get("updated_at")
            retracted_at = row_d.get("retracted_at")
            source = row_d.get("source")
            status = row_d.get("status")

            frontmatter = _build_frontmatter(
                etype=etype,
                composite_key=composite_key,
                title=converted_title,
                tags=tags,
                created_at=created_at,
                updated_at=updated_at,
                retracted_at=retracted_at,
                belongs_to_keys=belongs_to_keys,
                related_keys=related_keys,
                supersedes=supersedes_entries,
                depends_on_keys=depends_on_keys,
                source=source,
                status=status,
            )
            body_text = _build_body_text(etype, converted_title, converted_fields)
            file_content = frontmatter + "\n" + body_text
            if not file_content.endswith("\n"):
                file_content += "\n"

            content_hash = _compute_content_hash(
                etype=etype,
                converted_title=converted_title,
                converted_fields=converted_fields,
                tags=tags,
                belongs_to_keys=belongs_to_keys,
                related_keys=related_keys,
                supersedes=supersedes_entries,
                depends_on_keys=depends_on_keys,
                source=source,
                retracted_at=retracted_at,
            )

            slug = _slugify_title(title_raw)
            code = TYPE_NAME_TO_CODE[etype]
            filename = f"{code}-{eid}-{slug}.md"
            rel_path = os.path.join(_DIR_NAME[etype], filename)
            files_to_write.append((os.path.join(bundle_root, rel_path), file_content))

            entities_manifest.append(
                {
                    "key": composite_key,
                    "type": etype,
                    "title": converted_title,
                    "content_hash": content_hash,
                    "path": rel_path,
                }
            )
            counts[etype] += 1

        # unresolved_refs: 本文citation由来(選択集合外) + supersede由来(選択集合外)
        unresolved_targets: set[tuple[str, int]] = {(t, i) for (t, i, _src) in all_refs}
        for _source_id, target_id in external_supersedes:
            unresolved_targets.add(("decision", target_id))
        info_by_target = _fetch_unresolved_info_with_conn(conn, unresolved_targets)

        referenced_by: dict[tuple[str, int], set[str]] = defaultdict(set)
        for target_type, target_id, (src_etype, src_eid) in all_refs:
            referenced_by[(target_type, target_id)].add(_composite_key(instance_id, src_etype, src_eid))
        for source_id, target_id in external_supersedes:
            referenced_by[("decision", target_id)].add(_composite_key(instance_id, "decision", source_id))

        unresolved_refs = []
        for target in sorted(unresolved_targets, key=lambda k: (TYPE_ORDER.index(k[0]), k[1])):
            info = info_by_target.get(target, {"title": f"{target[0]}#{target[1]}", "domain_tags": []})
            unresolved_refs.append(
                {
                    "key": _composite_key(instance_id, target[0], target[1]),
                    "type": target[0],
                    "title": info["title"],
                    "domain_tags": info["domain_tags"],
                    "referenced_by": sorted(referenced_by.get(target, [])),
                }
            )

        try:
            os.makedirs(bundle_root, exist_ok=True)
            for etype, _eid in selected:
                os.makedirs(os.path.join(bundle_root, _DIR_NAME[etype]), exist_ok=True)
            for abs_path, content in files_to_write:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(content)
        except OSError as e:
            return {"error": {"code": "IO_ERROR", "message": str(e)}}

        bundle_id = f"{instance_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        manifest = {
            "format": BUNDLE_FORMAT,
            "bundle_id": bundle_id,
            "source_instance": instance_id,
            "exported_at": exported_at,
            "selection": selection if selection is not None else {"items": items},
            "entities": entities_manifest,
            "unresolved_refs": unresolved_refs,
        }
        manifest_path = os.path.join(bundle_root, "manifest.yaml")
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        except OSError as e:
            return {"error": {"code": "IO_ERROR", "message": str(e)}}

        return {
            "path": bundle_root,
            "bundle_id": bundle_id,
            "counts": dict(counts),
            "auto_included": auto_included,
            "unresolved_refs": unresolved_refs,
            "masked_literals": total_masked,
            "warnings": warnings,
        }
    except Exception as e:
        logger.error(f"export_bundle failed: {e}")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()
