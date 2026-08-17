"""外縁ドキュメントの鮮度チェッカー。

docs 配下（+ 明示指定ファイル）の ccm-doc-sync マーカーを走査し、DB
（decisions / decision_supersedes / decision_tags / topic_tags 継承）と
migrations/ のファイル名を突き合わせて、last-synced 以降に増えた変更を検出する。

ローカル運用専用（recompose / orch 起動時の運用チェックの一部として手動・skill 起動で回す）。
CI には載せない — CI からユーザーの DB は見えない。migration 番号比較だけの
CI 可能な部分は lint_doc_cochange.py が別途担う。

使い方:
    uv run python scripts/check_doc_freshness.py [--json] [--docs-root docs/] [--db <path>] [files...]

マーカー形式（doc先頭に HTML コメントで敷設する）:
    <!-- ccm-doc-sync
    watch-tags: domain:calm
    watch-direction: true
    watch-migrations: true
    last-synced: 2026-07-04
    last-synced-migration: 0048
    -->
"""
import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# プロジェクトルートをパスに追加（src.db等の参照用。pytest経由なら pytest.ini の
# pythonpath=. で既に解決済みだが、CLI単体実行時のために明示的に追加する）
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# tag scope の継承ロジック（直付け decision_tags OR relations.belongs_to 経由の
# 親 topic の topic_tags 継承 / retracted 除外）は hint_service の実装を単一の真実と
# して再利用する。ここで SQL を複製すると将来 hint_service 側が変わったとき checker の
# 判定基準がサイレントに乖離するため import で共有する。
from src.services.hint_service import _count_tag_scope_decisions  # noqa: E402

MIGRATIONS_DIR = _project_root / "migrations"

# 先頭（前置空白のみ許容）に固定するfront-matter形式。ドキュメント本文中のコード例
# として ccm-doc-sync マーカーの書式を引用している箇所（例: doc-sync-convention.md
# 自身の説明文）を実マーカーと誤認しないようにする。
_MARKER_RE = re.compile(r"\A\s*<!--\s*ccm-doc-sync(.*?)-->", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-z-]+):\s*(.*?)\s*$", re.MULTILINE)
_MIGRATION_NUM_RE = re.compile(r"^(\d+)_")


@dataclass
class DocMarker:
    """doc内 ccm-doc-sync マーカーのパース結果。"""

    path: Path
    watch_tags: list[str] = field(default_factory=list)
    watch_direction: bool = False
    watch_migrations: bool = False
    last_synced: str | None = None
    last_synced_migration: str | None = None


def parse_marker(text: str, path: Path) -> DocMarker | None:
    """doc本文から ccm-doc-sync マーカーを抽出する。マーカー無しは None。"""
    m = _MARKER_RE.search(text)
    if m is None:
        return None
    body = m.group(1)
    fields = dict(_FIELD_RE.findall(body))
    watch_tags_raw = fields.get("watch-tags", "")
    watch_tags = [t.strip() for t in watch_tags_raw.split(",") if t.strip()]
    return DocMarker(
        path=path,
        watch_tags=watch_tags,
        watch_direction=fields.get("watch-direction", "false").strip().lower() == "true",
        watch_migrations=fields.get("watch-migrations", "false").strip().lower() == "true",
        last_synced=fields.get("last-synced") or None,
        last_synced_migration=fields.get("last-synced-migration") or None,
    )


def find_marked_docs(docs_root: Path, explicit: list[Path]) -> list[Path]:
    """docs_root配下の *.md + 明示指定ファイルを合わせた候補一覧（重複排除・ソート済み）を返す。"""
    paths: set[Path] = set(p for p in explicit if p.exists())
    if docs_root.exists():
        paths.update(p for p in docs_root.rglob("*.md"))
    return sorted(paths)


def _parse_tag(tag_str: str) -> tuple[str, str]:
    if ":" in tag_str:
        namespace, name = tag_str.split(":", 1)
        return namespace, name
    return "", tag_str


def _tag_id(conn: sqlite3.Connection, namespace: str, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?", (namespace, name)
    ).fetchone()
    return row[0] if row else None


def count_new_tagged_decisions(
    conn: sqlite3.Connection, tag_str: str, after: str | None
) -> int:
    """watch-tags 該当タグの decision（直付け OR 親 topic の topic_tags 継承）のうち、
    after より後に作成された件数を返す。retracted済みは除外する。

    tag 文字列を tag_id に解決したうえで、件数算出そのものは
    hint_service._count_tag_scope_decisions に委譲する（継承規則の単一の真実）。
    タグが存在しなければ 0 を返す。
    """
    namespace, name = _parse_tag(tag_str)
    tag_id = _tag_id(conn, namespace, name)
    if tag_id is None:
        return 0
    return _count_tag_scope_decisions(conn, tag_id, after=after)


def count_new_direction_events(conn: sqlite3.Connection, after: str | None) -> int:
    """layer:direction decision の新規追加・supersede イベント数（after より後）を返す。

    タグ layer:direction 自体が存在しない（未導入・0件）場合は 0 を返す。
    """
    tag_id = _tag_id(conn, "layer", "direction")
    if tag_id is None:
        return 0

    added_sql = """
        SELECT COUNT(*) FROM decisions d
        JOIN decision_tags dt ON dt.decision_id = d.id AND dt.tag_id = ?
        WHERE d.retracted_at IS NULL
    """
    added_params: list = [tag_id]
    if after is not None:
        added_sql += " AND d.created_at > ?"
        added_params.append(after)
    added = conn.execute(added_sql, tuple(added_params)).fetchone()[0]

    supersede_sql = """
        SELECT COUNT(*) FROM decision_supersedes ds
        WHERE (
          EXISTS (SELECT 1 FROM decision_tags dt WHERE dt.decision_id = ds.source_id AND dt.tag_id = ?)
          OR EXISTS (SELECT 1 FROM decision_tags dt WHERE dt.decision_id = ds.target_id AND dt.tag_id = ?)
        )
    """
    supersede_params: list = [tag_id, tag_id]
    if after is not None:
        supersede_sql += " AND ds.created_at > ?"
        supersede_params.append(after)
    superseded = conn.execute(supersede_sql, tuple(supersede_params)).fetchone()[0]

    return added + superseded


def max_migration_number(migrations_dir: Path) -> int | None:
    """migrations/ 配下のファイル名から最大の migration 番号を返す（無ければ None）。

    番号重複ファイル（0005/0015/0039系の既知の重複）があっても max() で吸収される。
    """
    nums = []
    if not migrations_dir.exists():
        return None
    for p in migrations_dir.glob("*.sql"):
        m = _MIGRATION_NUM_RE.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def check_doc(
    conn: sqlite3.Connection, migrations_dir: Path, marker: DocMarker
) -> list[str]:
    """マーカー1件分の stale 判定を行い、stale 理由の一覧を返す（空なら fresh）。"""
    reasons: list[str] = []

    for tag_str in marker.watch_tags:
        n = count_new_tagged_decisions(conn, tag_str, marker.last_synced)
        if n > 0:
            reasons.append(f"watch-tags '{tag_str}' 該当の decision が {n} 件増加")

    if marker.watch_direction:
        n = count_new_direction_events(conn, marker.last_synced)
        if n > 0:
            reasons.append(f"layer:direction の追加・supersede が {n} 件発生")

    if marker.watch_migrations:
        latest = max_migration_number(migrations_dir)
        synced = int(marker.last_synced_migration) if marker.last_synced_migration else None
        if latest is not None and (synced is None or latest > synced):
            start = (synced + 1) if synced is not None else 0
            reasons.append(f"migration {start:04d}..{latest:04d} 未反映")

    return reasons


def run(docs_root: Path, explicit_files: list[Path], db_path: Path) -> dict:
    """チェックを実行し、--json 出力と同じ形の dict を返す。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        docs = find_marked_docs(docs_root, explicit_files)
        results = []
        any_stale = False
        for doc_path in docs:
            text = doc_path.read_text(encoding="utf-8")
            marker = parse_marker(text, doc_path)
            if marker is None:
                continue  # マーカー無し doc はスキップ
            reasons = check_doc(conn, MIGRATIONS_DIR, marker)
            stale = len(reasons) > 0
            any_stale = any_stale or stale
            results.append(
                {
                    "doc": str(doc_path),
                    "stale": stale,
                    "reasons": reasons,
                    "last_synced": marker.last_synced,
                }
            )
        return {"stale": any_stale, "docs": results}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "files", nargs="*", type=Path, help="明示的にチェックするファイル（--docs-root走査に加えて対象にする）"
    )
    parser.add_argument("--docs-root", type=Path, default=Path("docs"))
    parser.add_argument("--db", type=Path, default=None, help="省略時は src.db.get_db_path() の既定パス")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    db_path = args.db
    if db_path is None:
        from src.db import get_db_path

        db_path = Path(get_db_path())

    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    result = run(args.docs_root, args.files, db_path)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result["docs"]:
            print("ccm-doc-sync マーカー付きドキュメントが見つかりません。")
        for r in result["docs"]:
            status = "STALE" if r["stale"] else "OK"
            print(f"[{status}] {r['doc']} (last-synced: {r['last_synced']})")
            for reason in r["reasons"]:
                print(f"  - {reason}")

    return 1 if result["stale"] else 0


if __name__ == "__main__":
    sys.exit(main())
