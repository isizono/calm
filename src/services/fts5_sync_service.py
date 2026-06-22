"""FTS5 同期トリガーの単一真実源 (single source of truth)。

migrations/ 内に手書きで散在していた `trg_search_<entity>_{insert,update,delete}`
の DDL を、`Fts5SyncSpec` から決定論的に生成する。

新しい src_table を FTS5 検索対象に追加するときの規約:
1. `FTS5_SPECS` に `Fts5SyncSpec(...)` を追加する。
2. その変更を投入する migration で `install(conn, spec)` を呼ぶ。

src_table の列名や display title 式が変わるときの規約:
1. `FTS5_SPECS` の該当 spec を更新する。
2. その変更を投入する migration で `install(conn, spec)` を呼ぶ。

Expand-Contract:
- Expand 段階 (本モジュール導入時): 既存 migration のトリガー記述は不変。
  本モジュールから生成される DDL は既存最終形と機能的に等価。
- Contract 段階 (後続 PR): 別 migration で `install_all(conn)` を呼んで
  既存トリガーを spec ベースで全量再構築する。
"""

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

# CREATE/DROP TRIGGER の DDL に直接埋め込まれる識別子は、SQL 文字列補間で
# bind parameter を使えないため、事前に文字種を制限してインジェクション余地を消す。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# display_title_expr に許可する形式:
# - "NEW.<ident>" 単独
# - "COALESCE(NEW.<ident>, NEW.<ident>[, NEW.<ident>...])"
# それ以外の任意 SQL 式は受け付けない (DDL 直接補間されるため)。
_DISPLAY_TITLE_NEW_RE = re.compile(r"^NEW\.[A-Za-z_][A-Za-z0-9_]*$")
_DISPLAY_TITLE_COALESCE_RE = re.compile(
    r"^COALESCE\(\s*NEW\.[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\s*,\s*NEW\.[A-Za-z_][A-Za-z0-9_]*)+\s*\)$"
)


@dataclass(frozen=True)
class Fts5SyncSpec:
    """1 src_table に対する FTS5 同期ルール。

    Attributes:
        source_type: `search_index.source_type` に格納する短い literal ('topic'/'decision'/'activity'/'log'/'material')
        src_table: トリガー対象の SQLite テーブル名 (例: 'discussion_topics')
        trigger_basename: トリガー名の中央部分 (`trg_search_<basename>_{insert,update,delete}` の `<basename>`)。
            既存 migration での命名 (topics/decisions/activities/logs/materials) を踏襲する。
            英語複数形は規則的とは限らない (activity→activities) ため source_type/src_table から自動導出しない。
        fts_title_column: NEW.<fts_title_column> を `search_index_fts.title` に投入する列名
        fts_body_column: NEW.<fts_body_column> を `search_index_fts.body` に投入する列名
        display_title_expr: `search_index.title` (display 用) に投入する SQL 式
            (例: "NEW.title" / "COALESCE(NEW.title, NEW.decision)")
            None のときは "NEW.<fts_title_column>" として扱う
    """

    source_type: str
    src_table: str
    trigger_basename: str
    fts_title_column: str
    fts_body_column: str
    display_title_expr: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_type",
            "src_table",
            "trigger_basename",
            "fts_title_column",
            "fts_body_column",
        ):
            value = getattr(self, field_name)
            if not _IDENT_RE.match(value):
                raise ValueError(
                    f"Fts5SyncSpec.{field_name}={value!r} は識別子として不正 "
                    f"(英数字・アンダースコアのみ、先頭は英字 or _)"
                )
        if self.display_title_expr is not None:
            expr = self.display_title_expr
            if not (
                _DISPLAY_TITLE_NEW_RE.match(expr)
                or _DISPLAY_TITLE_COALESCE_RE.match(expr)
            ):
                raise ValueError(
                    f"Fts5SyncSpec.display_title_expr={expr!r} は許可されない式 "
                    f"('NEW.<ident>' または 'COALESCE(NEW.<ident>, NEW.<ident>[, ...])' のみ)"
                )

    @property
    def trigger_insert_name(self) -> str:
        return f"trg_search_{self.trigger_basename}_insert"

    @property
    def trigger_update_name(self) -> str:
        return f"trg_search_{self.trigger_basename}_update"

    @property
    def trigger_delete_name(self) -> str:
        return f"trg_search_{self.trigger_basename}_delete"

    @property
    def effective_display_title_expr(self) -> str:
        return self.display_title_expr or f"NEW.{self.fts_title_column}"


# === FTS5 同期対象の最終形レジストリ ===
# search_index.title (display) と search_index_fts.title/body (検索用) の列マッピングを
# 一元宣言する。新しい src_table を加えるとき or 列マッピングを変えるときはここを編集する。
FTS5_SPECS: tuple[Fts5SyncSpec, ...] = (
    Fts5SyncSpec(
        source_type="topic",
        src_table="discussion_topics",
        trigger_basename="topics",
        fts_title_column="title",
        fts_body_column="description",
    ),
    Fts5SyncSpec(
        source_type="decision",
        src_table="decisions",
        trigger_basename="decisions",
        fts_title_column="decision",
        fts_body_column="reason",
        display_title_expr="COALESCE(NEW.title, NEW.decision)",
    ),
    Fts5SyncSpec(
        source_type="activity",
        src_table="activities",
        trigger_basename="activities",
        fts_title_column="title",
        fts_body_column="description",
    ),
    Fts5SyncSpec(
        source_type="log",
        src_table="discussion_logs",
        trigger_basename="logs",
        fts_title_column="title",
        fts_body_column="content",
    ),
    Fts5SyncSpec(
        source_type="material",
        src_table="materials",
        trigger_basename="materials",
        fts_title_column="title",
        fts_body_column="content",
    ),
)


def render_insert_trigger(spec: Fts5SyncSpec) -> str:
    """INSERT トリガーの CREATE 文を生成する。"""
    return (
        f"CREATE TRIGGER IF NOT EXISTS {spec.trigger_insert_name}\n"
        f"AFTER INSERT ON {spec.src_table}\n"
        f"BEGIN\n"
        f"  INSERT INTO search_index (source_type, source_id, title, created_at)\n"
        f"  VALUES ('{spec.source_type}', NEW.id, {spec.effective_display_title_expr}, NEW.created_at);\n"
        f"  INSERT INTO search_index_fts (rowid, title, body)\n"
        f"  VALUES (last_insert_rowid(), NEW.{spec.fts_title_column}, NEW.{spec.fts_body_column});\n"
        f"END"
    )


def render_update_trigger(spec: Fts5SyncSpec) -> str:
    """UPDATE トリガーの CREATE 文を生成する。

    contentless FTS5 の 'delete' コマンドは INSERT 時と同じ title/body を渡す必要があり、
    OLD.<fts_title_column> / OLD.<fts_body_column> で対応する。
    """
    return (
        f"CREATE TRIGGER IF NOT EXISTS {spec.trigger_update_name}\n"
        f"AFTER UPDATE ON {spec.src_table}\n"
        f"BEGIN\n"
        f"  INSERT INTO search_index_fts (search_index_fts, rowid, title, body)\n"
        f"  VALUES ('delete',\n"
        f"    (SELECT id FROM search_index WHERE source_type = '{spec.source_type}' AND source_id = OLD.id),\n"
        f"    OLD.{spec.fts_title_column}, OLD.{spec.fts_body_column});\n"
        f"  UPDATE search_index\n"
        f"  SET title = {spec.effective_display_title_expr}\n"
        f"  WHERE source_type = '{spec.source_type}' AND source_id = NEW.id;\n"
        f"  INSERT INTO search_index_fts (rowid, title, body)\n"
        f"  VALUES (\n"
        f"    (SELECT id FROM search_index WHERE source_type = '{spec.source_type}' AND source_id = NEW.id),\n"
        f"    NEW.{spec.fts_title_column}, NEW.{spec.fts_body_column});\n"
        f"END"
    )


def render_delete_trigger(spec: Fts5SyncSpec) -> str:
    """DELETE トリガーの CREATE 文を生成する。"""
    return (
        f"CREATE TRIGGER IF NOT EXISTS {spec.trigger_delete_name}\n"
        f"AFTER DELETE ON {spec.src_table}\n"
        f"BEGIN\n"
        f"  INSERT INTO search_index_fts (search_index_fts, rowid, title, body)\n"
        f"  VALUES ('delete',\n"
        f"    (SELECT id FROM search_index WHERE source_type = '{spec.source_type}' AND source_id = OLD.id),\n"
        f"    OLD.{spec.fts_title_column}, OLD.{spec.fts_body_column});\n"
        f"  DELETE FROM search_index WHERE source_type = '{spec.source_type}' AND source_id = OLD.id;\n"
        f"END"
    )


def render_create_triggers(spec: Fts5SyncSpec) -> tuple[str, str, str]:
    """insert/update/delete の CREATE TRIGGER SQL を順に返す。"""
    return (
        render_insert_trigger(spec),
        render_update_trigger(spec),
        render_delete_trigger(spec),
    )


def render_drop_triggers(spec: Fts5SyncSpec) -> tuple[str, str, str]:
    """insert/update/delete の DROP TRIGGER SQL を順に返す。"""
    return (
        f"DROP TRIGGER IF EXISTS {spec.trigger_insert_name}",
        f"DROP TRIGGER IF EXISTS {spec.trigger_update_name}",
        f"DROP TRIGGER IF EXISTS {spec.trigger_delete_name}",
    )


def install(conn: sqlite3.Connection, spec: Fts5SyncSpec) -> None:
    """1 spec を DB に適用する。既存同名トリガーを drop してから create する。

    注意: Python 標準 ``sqlite3`` モジュールは default の ``isolation_level=""``
    では DDL (CREATE/DROP TRIGGER 等) の実行直前に pending transaction を
    暗黙 commit する。このため本関数の drop→create 系列は原子的ではなく、
    途中失敗時には trigger が消えたまま残る可能性がある。
    呼び出し側でこの非原子性が問題になる場合は、``isolation_level=None`` で
    接続して BEGIN/COMMIT を明示制御するか、`DROP TRIGGER` / `CREATE TRIGGER`
    の各 statement が独立に成功することを前提に設計すること。
    """
    for drop_sql in render_drop_triggers(spec):
        conn.execute(drop_sql)
    for create_sql in render_create_triggers(spec):
        conn.execute(create_sql)


def install_all(
    conn: sqlite3.Connection, specs: Iterable[Fts5SyncSpec] = FTS5_SPECS
) -> None:
    """全 spec をまとめて適用する。Contract migration から呼ぶことを想定。

    原子性については `install` の docstring 参照 (Python sqlite3 の DDL
    auto-commit 挙動により本関数も原子的ではない)。
    """
    for spec in specs:
        install(conn, spec)
