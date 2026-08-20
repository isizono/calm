"""バンドル取り込み(dry_run)サービス

export_bundleが書き出したバンドル(manifest.yaml + エンティティ別mdファイル)を読み、
DBを一切変更せず衝突レポートを返す。実際の書き込み(mode="apply")は別途実装する。

衝突判定の骨格は3種類:
  (a) 同一データの再import判定: import_provenance逆引き
      (origin_instance, entity_type, origin_id)でUNIQUE制約と同じキーに照合し、
      content_hashの一致/不一致でunchanged/updatable/upstream_changed_skipを分ける
  (b) ネイティブ重複の疑い: 新規importされるエンティティ(status="new")について
      embeddingベクトル類似検索を行い、閾値超えの類似ローカルエンティティを
      支援情報として返す(機械判定不能・裁定はユーザーに委ねる)
  (c) タグ名前空間の衝突: バンドルが使う全タグをローカルDBの状態
      (存在しない/既存/archived/エイリアス)で4区分する

参照解決(belongs_to/related/supersedes/depends_on・本文中の拡張cite)は
バンドル内→provenance逆引き→自インスタンス出生→解決不能、の優先順で判定する
(dry_run段階では実際の書き換えは行わず、解決可否の集計のみ)。
"""
import difflib
import logging
import os
import re
import sqlite3
from collections import defaultdict

import yaml
from sqlite_vec import serialize_float32

from src.db import get_connection, row_to_dict
from src.services import material_service
from src.services.citations_pure import (
    TYPE_CODE_TO_NAME,
    TYPE_TO_TABLE,
    TYPE_TO_TITLE_EXPR,
    TYPES_WITH_RETRACT,
)
from src.services.export_bundle_service import BUNDLE_FORMAT, _MAIN_FIELD
from src.services.instance_service import get_instance_id_with_conn
from src.services.material_service import _is_within_export_dir
from src.services.tag_service import parse_tag

logger = logging.getLogger(__name__)

_JUNCTION = {
    "topic": ("topic_tags", "topic_id"),
    "activity": ("activity_tags", "activity_id"),
    "material": ("material_tags", "material_id"),
    "decision": ("decision_tags", "decision_id"),
    "log": ("log_tags", "log_id"),
}

# manifest/frontmatterのccm_key・拡張cite形式(`{{cite:<instance_id>:<code><NNN>}}`)を
# パースする正規表現。instance_idの文字集合はinstance_service.INSTANCE_ID_PATTERNと揃える。
_COMPOSITE_KEY_PATTERN = re.compile(r"^([a-z][a-z0-9-]{2,31}):([MDLAT])(\d+)$")
_COMPOSITE_CITE_PATTERN = re.compile(r"\{\{cite:([a-z][a-z0-9-]{2,31}):([MDLAT])(\d+)\}\}")

# 重複疑い検知(ネイティブ重複)のコサイン距離閾値。resolve_tags.MERGE_THRESHOLD(0.15、
# タグ名同士の統合判定)より緩い値にしている。本文全体同士の比較でタグ名ほどの
# 近さは期待できず、閾値が厳しすぎると気づき導線として機能しなくなるため。
DUPLICATE_DISTANCE_THRESHOLD = 0.3
DUPLICATE_SEARCH_LIMIT = 3

SAMPLE_TITLES_LIMIT = 5
DANGLING_SAMPLE_LIMIT = 10

# タグ4区分の判定tiering(review_required)。namespace=='domain'は衝突コストが高い
# ため常にAI一次判定対象、素タグはnotesという判定材料がある場合のみ対象にする。
_REVIEW_REQUIRED_NAMESPACE = "domain"


# --- バンドル読み込み ---


def _load_manifest(bundle_root: str) -> dict | None:
    manifest_path = os.path.join(bundle_root, "manifest.yaml")
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else None


def _split_frontmatter(text: str) -> tuple[dict, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    fm_text = text[4:end]
    body = text[end + len("\n---\n") :]
    fm = yaml.safe_load(fm_text)
    if not isinstance(fm, dict):
        return None
    return fm, body


def _strip_leading_heading(text: str, marker: str) -> str:
    """先頭の空行群と`marker`で始まる見出し行を1行だけ取り除く。"""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx < len(lines) and lines[idx].lstrip().startswith(marker):
        idx += 1
    return "\n".join(lines[idx:]).strip("\n")


def _parse_body_fields(etype: str, body: str) -> dict[str, str]:
    """本文からフィールドを抽出する(export_bundle_service._build_body_textの逆変換)。

    frontmatterのtitleを正としh1見出しは無視する。decisionのみ2フィールド
    (`<!-- ccm:field decision -->` / `<!-- ccm:field reason -->`のHTMLコメント区切り)、
    それ以外は単一の主テキストフィールド。
    """
    if etype == "decision":
        d_marker = "<!-- ccm:field decision -->"
        r_marker = "<!-- ccm:field reason -->"
        d_idx = body.find(d_marker)
        r_idx = body.find(r_marker)
        if d_idx == -1 or r_idx == -1 or r_idx < d_idx:
            # 壊れたフォーマット: フィールド分離ができないので全体をdecisionへ落とす
            return {"decision": body.strip("\n"), "reason": ""}
        decision_part = body[d_idx + len(d_marker) : r_idx]
        reason_part = body[r_idx + len(r_marker) :]
        return {
            "decision": _strip_leading_heading(decision_part, "## "),
            "reason": _strip_leading_heading(reason_part, "## "),
        }
    main_field = _MAIN_FIELD.get(etype)
    if main_field is None:
        return {}
    return {main_field: _strip_leading_heading(body, "# ")}


def _parse_composite_key(key: str) -> tuple[str, str, int] | None:
    """複合キー`<instance_id>:<型コード><番号>`を(instance_id, type_name, local_id)に分解する。"""
    if not isinstance(key, str):
        return None
    m = _COMPOSITE_KEY_PATTERN.match(key)
    if not m:
        return None
    instance_id, code, num = m.group(1), m.group(2), m.group(3)
    etype = TYPE_CODE_TO_NAME.get(code)
    if etype is None:
        return None
    return instance_id, etype, int(num)


def _extract_composite_refs(text: str) -> list[str]:
    """本文から`{{cite:<composite_key>}}`形式の参照キーを出現順に抽出する(コードブロック除外)。"""
    keys: list[str] = []
    in_fence = False
    for raw_line in text.split("\n"):
        stripped = raw_line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _COMPOSITE_CITE_PATTERN.finditer(raw_line):
            keys.append(f"{m.group(1)}:{m.group(2)}{m.group(3)}")
    return keys


def _load_bundle_entities(bundle_root: str, entities_meta: list) -> tuple[dict[str, dict], list[dict]]:
    """manifest.entitiesが指すファイルを読み込み、キーごとにfrontmatter/本文フィールドを返す。

    Returns:
        (parsed_entities, load_errors)
        parsed_entities: {ccm_key: {"fm": dict, "fields": dict[str, str], "manifest_entry": dict}}
        load_errors: [{"key": str, "error": str}, ...] (ファイル欠損・frontmatter破損・パス逸脱)
    """
    parsed: dict[str, dict] = {}
    errors: list[dict] = []
    bundle_root_real = os.path.realpath(bundle_root)
    for ent in entities_meta:
        if not isinstance(ent, dict):
            continue
        key = ent.get("key")
        rel_path = ent.get("path")
        if not key or not rel_path:
            errors.append({"key": key, "error": "malformed_manifest_entry"})
            continue
        abs_path = os.path.realpath(os.path.join(bundle_root, rel_path))
        if abs_path != bundle_root_real and not abs_path.startswith(bundle_root_real + os.sep):
            errors.append({"key": key, "error": "path_outside_bundle"})
            continue
        if not os.path.isfile(abs_path):
            errors.append({"key": key, "error": "file_not_found"})
            continue
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
        split = _split_frontmatter(text)
        if split is None:
            errors.append({"key": key, "error": "malformed_frontmatter"})
            continue
        fm, body = split
        etype = fm.get("ccm_type")
        fields = _parse_body_fields(etype, body)
        parsed[key] = {"fm": fm, "fields": fields, "manifest_entry": ent}
    return parsed, errors


# --- provenance逆引き・エンティティ分類 ---


def _fetch_provenance_by_origin_with_conn(conn: sqlite3.Connection) -> dict[tuple[str, str, int], dict]:
    rows = conn.execute(
        "SELECT entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id "
        "FROM import_provenance"
    ).fetchall()
    result: dict[tuple[str, str, int], dict] = {}
    for row in rows:
        d = row_to_dict(row)
        result[(d["origin_instance"], d["entity_type"], d["origin_id"])] = d
    return result


def _classify_entities(
    parsed_entities: dict[str, dict],
    provenance_by_origin: dict[tuple[str, str, int], dict],
    self_instance_id: str,
) -> tuple[dict[str, dict], dict[str, dict[str, int]], list[dict]]:
    """各エンティティを再import判定4状態に分類する。

    Returns:
        (classifications, summary, upstream_changed)
        classifications: {key: {"status", "type", "title", "content_hash", "local_entity_id"?}}
        summary: {type: {"new": n, "unchanged": n, "updatable": n,
                          "upstream_changed_skip": n, "self_origin": n, "invalid_key": n}}
        upstream_changed: decision/logで上流変更が検知された分の警告一覧
    """
    classifications: dict[str, dict] = {}
    summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {"new": 0, "unchanged": 0, "updatable": 0, "upstream_changed_skip": 0, "self_origin": 0}
    )
    upstream_changed: list[dict] = []

    for key, parsed in parsed_entities.items():
        fm = parsed["fm"]
        etype = fm.get("ccm_type")
        manifest_hash = parsed["manifest_entry"].get("content_hash")
        key_parsed = _parse_composite_key(key)
        if key_parsed is None or etype not in TYPE_TO_TABLE:
            classifications[key] = {"status": "invalid_key", "type": etype, "title": fm.get("title")}
            continue
        origin_instance, key_etype, origin_id = key_parsed

        prov = provenance_by_origin.get((origin_instance, etype, origin_id))
        if prov is not None:
            if prov["content_hash"] == manifest_hash:
                status = "unchanged"
            elif etype in ("decision", "log"):
                status = "upstream_changed_skip"
                upstream_changed.append(
                    {
                        "key": key,
                        "type": etype,
                        "title": fm.get("title"),
                        "local_entity_id": prov["entity_id"],
                    }
                )
            else:
                status = "updatable"
        elif origin_instance == self_instance_id:
            status = "self_origin"
        else:
            status = "new"

        classifications[key] = {
            "status": status,
            "type": etype,
            "title": fm.get("title"),
            "content_hash": manifest_hash,
            "local_entity_id": prov["entity_id"] if prov else None,
        }
        summary[etype][status] += 1

    return classifications, dict(summary), upstream_changed


# --- 参照解決・dangling refs ---


def _resolve_ref_status(
    key: str,
    bundle_keys: set[str],
    provenance_by_origin: dict[tuple[str, str, int], dict],
    self_instance_id: str,
) -> str:
    """参照キー1件の解決状態を返す(`bundle`/`provenance`/`self_origin`/`unresolved`)。"""
    if key in bundle_keys:
        return "bundle"
    parsed = _parse_composite_key(key)
    if parsed is None:
        return "unresolved"
    origin_instance, etype, origin_id = parsed
    if (origin_instance, etype, origin_id) in provenance_by_origin:
        return "provenance"
    if origin_instance == self_instance_id:
        return "self_origin"
    return "unresolved"


def _collect_dangling_refs(
    parsed_entities: dict[str, dict],
    bundle_keys: set[str],
    provenance_by_origin: dict[tuple[str, str, int], dict],
    self_instance_id: str,
) -> dict:
    """全エンティティのbelongs_to/related/supersedes/depends_on・本文中の拡張cite参照を
    集約し、解決不能(dangling)な参照キーの件数・代表例を返す。"""
    all_ref_keys: set[str] = set()
    for parsed in parsed_entities.values():
        fm = parsed["fm"]
        for k in fm.get("belongs_to") or []:
            all_ref_keys.add(k)
        for k in fm.get("related") or []:
            all_ref_keys.add(k)
        for s in fm.get("supersedes") or []:
            if isinstance(s, dict) and s.get("key"):
                all_ref_keys.add(s["key"])
        for k in fm.get("depends_on") or []:
            all_ref_keys.add(k)
        for field_text in parsed["fields"].values():
            all_ref_keys.update(_extract_composite_refs(field_text))

    dangling = sorted(
        k
        for k in all_ref_keys
        if _resolve_ref_status(k, bundle_keys, provenance_by_origin, self_instance_id) == "unresolved"
    )
    return {"count": len(dangling), "sample": dangling[:DANGLING_SAMPLE_LIMIT]}


# --- タグ4区分レポート ---


def _sample_local_tag_usage_with_conn(
    conn: sqlite3.Connection, tag_id: int, limit: int = SAMPLE_TITLES_LIMIT
) -> tuple[list[str], int]:
    """ローカルDBで指定タグIDを使用中の既存エンティティのtitle上位n件・使用数を返す(5型junction横断)。"""
    titles: list[str] = []
    total = 0
    for etype, (junction, col) in _JUNCTION.items():
        table = TYPE_TO_TABLE[etype]
        title_expr = TYPE_TO_TITLE_EXPR[etype]
        retract_clause = "AND e.retracted_at IS NULL" if etype in TYPES_WITH_RETRACT else ""
        rows = conn.execute(
            f"SELECT {title_expr} AS title FROM {table} e "
            f"JOIN {junction} j ON j.{col} = e.id WHERE j.tag_id = ? {retract_clause}",
            (tag_id,),
        ).fetchall()
        total += len(rows)
        titles.extend(r["title"] for r in rows)
    return titles[:limit], total


def _notes_diff(local_notes: str, incoming_notes: str) -> str:
    """既存notesに対してincoming notesが追加する行(行単位差分の'+'側)を返す。"""
    diff = difflib.unified_diff(local_notes.splitlines(), incoming_notes.splitlines(), lineterm="")
    added = [line[1:] for line in diff if line.startswith("+") and not line.startswith("+++")]
    return "\n".join(added)


def _build_tag_report_with_conn(
    conn: sqlite3.Connection,
    parsed_entities: dict[str, dict],
    tag_definitions: list,
) -> dict:
    """バンドルが使う全タグをローカルDBの状態で4区分する。

    merge(既存合流)・create(新規作成)・alias_hit(エイリアス該当)のエントリには、
    タグ同名異義のAI一次判定材料としてreview_required・local/incomingのsample_titles・
    使用数を付す。alias_hitの同名異義判定は解決先canonicalタグに対して行う。
    """
    bundle_tag_usage: dict[str, list[dict]] = defaultdict(list)
    for key, parsed in parsed_entities.items():
        fm = parsed["fm"]
        title = fm.get("title") or key
        for tag_str in fm.get("tags") or []:
            bundle_tag_usage[tag_str].append({"key": key, "title": title})

    tag_notes_incoming: dict[str, str] = {
        d["tag"]: d["notes"] for d in (tag_definitions or []) if isinstance(d, dict) and d.get("tag")
    }

    if not bundle_tag_usage:
        return {"merge": [], "create": [], "archived_hit": [], "alias_hit": []}

    parsed_map = {t: parse_tag(t) for t in bundle_tag_usage}
    placeholders = " OR ".join("(namespace = ? AND name = ?)" for _ in parsed_map)
    params = [v for pair in parsed_map.values() for v in pair]
    rows = conn.execute(
        f"SELECT id, namespace, name, notes, canonical_id, archived_at, archived_reason "
        f"FROM tags WHERE {placeholders}",
        params,
    ).fetchall()
    local_by_key: dict[tuple[str, str], dict] = {(r["namespace"], r["name"]): row_to_dict(r) for r in rows}

    canonical_ids = {r["canonical_id"] for r in local_by_key.values() if r["canonical_id"] is not None}
    canonical_by_id: dict[int, dict] = {}
    if canonical_ids:
        ph = ",".join("?" * len(canonical_ids))
        crows = conn.execute(
            f"SELECT id, namespace, name, notes FROM tags WHERE id IN ({ph})",
            list(canonical_ids),
        ).fetchall()
        canonical_by_id = {r["id"]: row_to_dict(r) for r in crows}

    merge: list[dict] = []
    create: list[dict] = []
    archived_hit: list[dict] = []
    alias_hit: list[dict] = []

    for tag_str in sorted(bundle_tag_usage.keys()):
        ns, name = parsed_map[tag_str]
        usage = bundle_tag_usage[tag_str]
        incoming = {
            "sample_titles": [u["title"] for u in usage[:SAMPLE_TITLES_LIMIT]],
            "count": len(usage),
        }
        incoming_notes = tag_notes_incoming.get(tag_str)
        local = local_by_key.get((ns, name))

        if local is None:
            review_required = ns == _REVIEW_REQUIRED_NAMESPACE or bool(incoming_notes)
            create.append(
                {
                    "tag": tag_str,
                    "review_required": review_required,
                    "notes": incoming_notes,
                    "incoming": incoming,
                }
            )
            continue

        if local["canonical_id"] is not None:
            canonical = canonical_by_id.get(local["canonical_id"])
            resolved_to = None
            canonical_notes = None
            if canonical:
                resolved_to = (
                    f"{canonical['namespace']}:{canonical['name']}" if canonical["namespace"] else canonical["name"]
                )
                canonical_notes = canonical["notes"]
            local_titles, local_count = _sample_local_tag_usage_with_conn(conn, local["canonical_id"])
            review_required = ns == _REVIEW_REQUIRED_NAMESPACE or bool(incoming_notes) or bool(canonical_notes)
            alias_hit.append(
                {
                    "tag": tag_str,
                    "resolved_to": resolved_to,
                    "review_required": review_required,
                    "local": {"sample_titles": local_titles, "count": local_count},
                    "incoming": incoming,
                }
            )
            continue

        if local["archived_at"] is not None:
            archived_hit.append(
                {
                    "tag": tag_str,
                    "archived_reason": local["archived_reason"],
                    "incoming": incoming,
                }
            )
            continue

        local_titles, local_count = _sample_local_tag_usage_with_conn(conn, local["id"])
        local_notes = local.get("notes")
        review_required = ns == _REVIEW_REQUIRED_NAMESPACE or bool(incoming_notes) or bool(local_notes)
        if incoming_notes and local_notes:
            notes_diff = _notes_diff(local_notes, incoming_notes)
        elif incoming_notes:
            notes_diff = incoming_notes
        else:
            notes_diff = None
        merge.append(
            {
                "tag": tag_str,
                "review_required": review_required,
                "notes_diff": notes_diff,
                "local": {"sample_titles": local_titles, "count": local_count},
                "incoming": incoming,
            }
        )

    return {"merge": merge, "create": create, "archived_hit": archived_hit, "alias_hit": alias_hit}


# --- ネイティブ重複疑い検知 ---


def _build_query_text(etype: str, title: str | None, fields: dict[str, str]) -> str:
    from src.services.embedding_service import build_embedding_text

    if etype == "decision":
        return build_embedding_text(fields.get("decision"), fields.get("reason"))
    main_field = _MAIN_FIELD.get(etype)
    return build_embedding_text(title, fields.get(main_field) if main_field else None)


def _find_similar_local_entities_with_conn(
    conn: sqlite3.Connection, query_embedding: list[float], limit: int
) -> list[dict]:
    blob = serialize_float32(query_embedding)
    vec_rows = conn.execute(
        "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
        (blob, limit * 5),
    ).fetchall()
    if not vec_rows:
        return []
    dist_by_rowid = {r["rowid"]: r["distance"] for r in vec_rows}
    rowids = list(dist_by_rowid.keys())
    placeholders = ",".join("?" * len(rowids))
    si_rows = conn.execute(
        f"SELECT id, source_type, source_id FROM search_index WHERE id IN ({placeholders})",
        rowids,
    ).fetchall()
    results = []
    for r in si_rows:
        etype = r["source_type"]
        if etype not in TYPE_TO_TABLE:
            continue
        distance = dist_by_rowid[r["id"]]
        if distance > DUPLICATE_DISTANCE_THRESHOLD:
            continue
        table = TYPE_TO_TABLE[etype]
        title_expr = TYPE_TO_TITLE_EXPR[etype]
        retract_clause = "AND retracted_at IS NULL" if etype in TYPES_WITH_RETRACT else ""
        row = conn.execute(
            f"SELECT {title_expr} AS title FROM {table} WHERE id = ? {retract_clause}",
            (r["source_id"],),
        ).fetchone()
        if row is None:
            continue
        results.append(
            {
                "type": etype,
                "id_raw": r["source_id"],
                "title": row["title"],
                "score": round(1.0 - distance, 4),
            }
        )
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def _check_duplicates_with_conn(
    conn: sqlite3.Connection,
    new_entities: list[tuple[str, dict]],
) -> tuple[list[dict], bool]:
    """新規importエンティティ(status="new")について類似ローカルエンティティを検索する。

    embeddingサーバー未起動時は各候補でNoneが返り続けるためdegraded=Trueになるが、
    クラッシュはしない(fable原案5.2節のdegraded表示方針と同じ)。
    """
    from src.services import embedding_service

    duplicates: list[dict] = []
    degraded = False

    candidates: list[tuple[str, dict]] = []
    query_texts: list[str] = []
    for key, info in new_entities:
        query_text = _build_query_text(info["type"], info["title"], info["fields"])
        if not query_text:
            continue
        candidates.append((key, info))
        query_texts.append(query_text)

    if not query_texts:
        return duplicates, degraded

    query_embeddings = embedding_service.encode_queries(query_texts)
    if query_embeddings is None:
        return duplicates, True

    for (key, info), query_embedding in zip(candidates, query_embeddings):
        similar = _find_similar_local_entities_with_conn(conn, query_embedding, DUPLICATE_SEARCH_LIMIT)
        if similar:
            duplicates.append({"key": key, "title": info["title"], "similar": similar})
    return duplicates, degraded


# --- メインエントリ ---


def import_bundle(
    bundle_path: str,
    mode: str = "dry_run",
    resolutions: dict | None = None,
    skip_duplicate_check: bool = False,
) -> dict:
    """バンドルを読み、衝突検知レポートを返す(mode="dry_run")。DBへの書き込みは行わない。

    Args:
        bundle_path: `export_bundle`が書き出したバンドルディレクトリのパス
            (`manifest.yaml`を直下に持つディレクトリ)。DEFAULT_EXPORT_DIR配下に
            限定される(exportと対称のパスガード)
        mode: "dry_run"のみサポート(既定)。"apply"は未実装でNOT_IMPLEMENTEDを返す
        resolutions: mode="apply"向けの裁定結果。dry_runでは無視する
        skip_duplicate_check: Trueのときネイティブ重複疑い検知(embedding類似検索)を
            スキップする(デフォルトFalse)。domain規模の初回importで対象エンティティ数が
            多い場合の速度対策

    Returns:
        成功時: {"format_version_ok": bool, "bundle_id": str, "source_instance": str,
            "summary": {type: {"new", "unchanged", "updatable", "upstream_changed_skip",
                "self_origin"}, ...},
            "upstream_changed": [{"key", "type", "title", "local_entity_id"}, ...],
            "tag_report": {"merge": [...], "create": [...], "archived_hit": [...],
                "alias_hit": [...]},
            "duplicates_suspected": [{"key", "title", "similar": [...]}, ...],
            "dangling_refs": {"count": int, "sample": [...]},
            "degraded": bool, "load_errors": [...]}
        失敗時: {"error": {"code": "VALIDATION_ERROR" | "NOT_FOUND" |
            "INSTANCE_ID_NOT_SET" | "NOT_IMPLEMENTED" | "DATABASE_ERROR", "message": str}}
    """
    if mode not in ("dry_run", "apply"):
        return {"error": {"code": "VALIDATION_ERROR", "message": "mode must be 'dry_run' or 'apply'"}}
    if mode == "apply":
        return {
            "error": {
                "code": "NOT_IMPLEMENTED",
                "message": "mode='apply' is not yet implemented.",
            }
        }

    if not bundle_path or not isinstance(bundle_path, str):
        return {"error": {"code": "VALIDATION_ERROR", "message": "bundle_path must be a non-empty string"}}

    expanded = os.path.expanduser(bundle_path)
    if not _is_within_export_dir(expanded):
        allowed = os.path.expanduser(material_service.DEFAULT_EXPORT_DIR)
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"bundle_path must resolve to a location within {allowed}. resolved path: {expanded}",
            }
        }

    manifest = _load_manifest(expanded)
    if manifest is None:
        return {"error": {"code": "NOT_FOUND", "message": f"manifest.yaml not found under {expanded}"}}

    format_version_ok = manifest.get("format") == BUNDLE_FORMAT
    source_instance = manifest.get("source_instance")
    bundle_id = manifest.get("bundle_id")
    entities_meta = manifest.get("entities") or []
    tag_definitions = manifest.get("tag_definitions") or []

    if not isinstance(entities_meta, list):
        return {"error": {"code": "VALIDATION_ERROR", "message": "manifest.entities must be a list"}}

    # 重複疑い検知(_check_duplicates_with_conn)がvec_index仮想テーブルへクエリするため、
    # load_vec=True(既定)でsqlite-vec拡張をロードした接続を使う。
    conn = get_connection()
    try:
        self_instance_id = get_instance_id_with_conn(conn)
        if self_instance_id is None:
            return {
                "error": {
                    "code": "INSTANCE_ID_NOT_SET",
                    "message": "instance_id is not set. Call set_instance_identity first.",
                }
            }

        parsed_entities, load_errors = _load_bundle_entities(expanded, entities_meta)
        bundle_keys = set(parsed_entities.keys())

        provenance_by_origin = _fetch_provenance_by_origin_with_conn(conn)

        classifications, summary, upstream_changed = _classify_entities(
            parsed_entities, provenance_by_origin, self_instance_id
        )

        dangling_refs = _collect_dangling_refs(
            parsed_entities, bundle_keys, provenance_by_origin, self_instance_id
        )

        tag_report = _build_tag_report_with_conn(conn, parsed_entities, tag_definitions)

        duplicates_suspected: list[dict] = []
        degraded = False
        if not skip_duplicate_check:
            new_entities = [
                (
                    key,
                    {
                        "type": classifications[key]["type"],
                        "title": classifications[key]["title"],
                        "fields": parsed_entities[key]["fields"],
                    },
                )
                for key, info in classifications.items()
                if info["status"] == "new" and key in parsed_entities
            ]
            duplicates_suspected, degraded = _check_duplicates_with_conn(conn, new_entities)

        return {
            "format_version_ok": format_version_ok,
            "bundle_id": bundle_id,
            "source_instance": source_instance,
            "summary": summary,
            "upstream_changed": upstream_changed,
            "tag_report": tag_report,
            "duplicates_suspected": duplicates_suspected,
            "dangling_refs": dangling_refs,
            "degraded": degraded,
            "load_errors": load_errors,
        }
    except Exception as e:
        logger.error(f"import_bundle dry_run failed: {e}")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()
