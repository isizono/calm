#!/usr/bin/env python3
"""``docs/spec/db-schema-tables.md`` を実スキーマから自動生成する。

`migrations/` を順に適用した結果として得られる実際の SQLite スキーマ
（テーブル名・カラム名/型/NULL可否/デフォルト値・インデックス）を機械的に
書き出す。手書きの `docs/spec/db-schema.md` は「なぜこの形なのか」（設計判断の
背景・既知の課題）に集中し、「今どういう形なのか」（カラム一覧の現在値）は
本ファイルを正とする。db-schema.md 各テーブル節は本ファイルの該当節を参照する。

使い方:
    uv run python scripts/dump_db_schema.py            # docs/spec/db-schema-tables.md を上書き
    uv run python scripts/dump_db_schema.py --check     # 差分があれば exit 1（CI用）
    uv run python scripts/dump_db_schema.py --stdout    # 標準出力へ吐くだけ

内部テーブル（yoyoのmigration台帳 `_yoyo_*`）は出力対象外。
"""
import argparse
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUT_PATH = REPO_ROOT / "docs" / "spec" / "db-schema-tables.md"
MIGRATIONS_DIR = REPO_ROOT / "migrations"

EXCLUDED_PREFIXES = ("_yoyo_", "sqlite_")

# _build_fresh_connection() が作った一時DBディレクトリの一覧。
# 返す sqlite3.Connection は呼び出し元（build_markdown() やテストコード）が
# 使い終わるまで有効である必要があり、関数を抜けた時点では破棄できない。
# そのため個々の呼び出しごとに即座に削除せず、プロセス終了時にまとめて
# 削除する（atexit）ことでリークを防ぐ。
_TMP_SCHEMA_DUMP_DIRS: list[str] = []


def _cleanup_tmp_schema_dump_dirs() -> None:
    for d in _TMP_SCHEMA_DUMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _TMP_SCHEMA_DUMP_DIRS.clear()


atexit.register(_cleanup_tmp_schema_dump_dirs)


def _latest_migration_number() -> str:
    numbers = []
    for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        numbers.append(f.name[:4])
    return max(numbers) if numbers else "0000"


def _build_fresh_connection() -> sqlite3.Connection:
    """migrations/ を全適用した一時DBへの接続を返す。

    既存の CCM_DB_PATH / DISCUSSION_DB_PATH は変更しない
    （呼び出し元プロセスの他のDB利用に影響させないため、専用の一時パスへ隔離する）。

    一時ディレクトリは、返した接続を呼び出し元が使い終わるまで生存させる
    必要がある（テストコードから直接呼ばれ、戻り値をこの関数のスコープ外で
    使い続けるため）。セットアップ中に例外が起きた場合は即座に削除し、
    成功時は _TMP_SCHEMA_DUMP_DIRS に登録してプロセス終了時（atexit）に
    まとめて削除する。
    """
    tmpdir = tempfile.mkdtemp(prefix="ccm-schema-dump-")
    db_path = os.path.join(tmpdir, "schema-dump.db")
    old_env = {
        k: os.environ.get(k) for k in ("DISCUSSION_DB_PATH", "CCM_DB_PATH")
    }
    os.environ["DISCUSSION_DB_PATH"] = db_path
    os.environ.pop("CCM_DB_PATH", None)
    try:
        from src.db import init_database, get_connection

        init_database()
        conn = get_connection()
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    _TMP_SCHEMA_DUMP_DIRS.append(tmpdir)
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    return [
        r["name"] for r in rows if not any(r["name"].startswith(p) for p in EXCLUDED_PREFIXES)
    ]


def _format_default(value) -> str:
    if value is None:
        return "—"
    return f"`{value}`"


def _render_table(conn: sqlite3.Connection, name: str) -> str:
    kind_row = conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    kind = kind_row["type"]
    create_sql = kind_row["sql"] or ""

    lines = [f"### {name}", ""]
    if kind == "view":
        lines.append("VIEW。定義SQL:")
        lines.append("")
        lines.append("```sql")
        lines.append(create_sql)
        lines.append("```")
        lines.append("")
        return "\n".join(lines)

    cols = conn.execute(f"PRAGMA table_info('{name}')").fetchall()
    if cols:
        lines.append("| カラム名 | 型 | NULL | デフォルト | PK |")
        lines.append("|---|---|---|---|---|")
        for c in cols:
            null_ok = "NO" if c["notnull"] or c["pk"] else "YES"
            pk = "PK" if c["pk"] else "—"
            col_type = c["type"] or "—"
            lines.append(
                f"| {c['name']} | {col_type} | {null_ok} | {_format_default(c['dflt_value'])} | {pk} |"
            )
        lines.append("")
    else:
        lines.append("(仮想テーブル等、PRAGMA table_info では列情報を取得できない)")
        lines.append("")

    indexes = conn.execute(f"PRAGMA index_list('{name}')").fetchall()
    named_indexes = [idx for idx in indexes if not idx["name"].startswith("sqlite_autoindex_")]
    if named_indexes:
        lines.append("インデックス:")
        for idx in named_indexes:
            idx_cols = conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()
            col_list = ", ".join(c["name"] for c in idx_cols)
            unique = " UNIQUE" if idx["unique"] else ""
            lines.append(f"- `{idx['name']}`{unique} ON `{name}`({col_list})")
        lines.append("")
    else:
        lines.append("インデックス: なし（自動生成される主キー索引を除く）")
        lines.append("")

    lines.append("<details><summary>CREATE文（生成元migration）</summary>")
    lines.append("")
    lines.append("```sql")
    lines.append(create_sql)
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def build_markdown() -> str:
    conn = _build_fresh_connection()
    latest = _latest_migration_number()
    names = _table_names(conn)

    header = f"""# cc-memory DB スキーマ自動ダンプ

<!-- 自動生成ファイル。手動編集しないこと。 -->
<!-- 生成元: scripts/dump_db_schema.py（migrations/ 全適用後の実スキーマから生成） -->
<!-- 再生成: uv run python scripts/dump_db_schema.py -->

`migrations/` を通し番号順に全適用した結果として得られる、現在のテーブル/ビュー構造の機械的な写しである。
カラム名・型・NULL可否・デフォルト値・インデックスは常に本ファイルが最新（生成時点で最新migrationは {latest}）。

「なぜこの形なのか」（設計判断の背景・変遷・既知の課題）は `docs/spec/db-schema.md` を参照。
本ファイルは現在値のみを扱い、変遷の経緯（旧カラムの削除理由等）は記載しない。

---

"""
    sections = [_render_table(conn, name) for name in names]
    return header + "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()

    rendered = build_markdown()

    if args.stdout:
        print(rendered, end="")
        return 0

    if args.check:
        current = OUTPUT_PATH.read_text() if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(
                f"differs: {OUTPUT_PATH} is stale relative to migrations/ の現在のスキーマ",
                file=sys.stderr,
            )
            print("再生成: uv run python scripts/dump_db_schema.py", file=sys.stderr)
            return 1
        print(f"ok: {OUTPUT_PATH} is up to date")
        return 0

    OUTPUT_PATH.write_text(rendered)
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
