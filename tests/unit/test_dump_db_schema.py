"""scripts/dump_db_schema.py のユニットテスト。

migrations/ を実際に全適用した一時DBに対して直接PRAGMAを叩き、
生成結果と突き合わせる（内部関数のmockはしない）。
"""
from scripts.dump_db_schema import (
    OUTPUT_PATH,
    _build_fresh_connection,
    _table_names,
    build_markdown,
)


def test_excludes_yoyo_internal_tables():
    """yoyoのmigration台帳テーブル(_yoyo_*)は出力対象に含めない。"""
    conn = _build_fresh_connection()
    names = _table_names(conn)
    assert not any(n.startswith("_yoyo_") for n in names)
    assert "activities" in names
    assert "decisions" in names


def test_every_table_and_view_gets_a_section():
    """sqlite_masterに存在する全テーブル/ビュー(内部テーブル除く)がMarkdownに1節ずつ出力される。"""
    conn = _build_fresh_connection()
    names = _table_names(conn)
    markdown = build_markdown()

    for name in names:
        assert f"### {name}\n" in markdown, f"missing section for {name}"


def test_activities_columns_match_live_pragma():
    """activitiesテーブルの列挙が実際のPRAGMA table_infoと一致する(型変換ロジックの回帰検出)。"""
    conn = _build_fresh_connection()
    cols = conn.execute("PRAGMA table_info('activities')").fetchall()
    markdown = build_markdown()

    section_start = markdown.index("### activities\n")
    section_end = markdown.index("\n### ", section_start + 1)
    section = markdown[section_start:section_end]

    for c in cols:
        assert c["name"] in section, f"column {c['name']} missing from generated section"
    # NOT NULL 制約のカラムは NULL 列が NO になっている
    for c in cols:
        if c["notnull"] or c["pk"]:
            assert f"| {c['name']} |" in section
            row_line = next(l for l in section.splitlines() if l.startswith(f"| {c['name']} |"))
            assert "| NO |" in row_line, row_line


def test_generated_output_matches_committed_file():
    """docs/spec/db-schema-tables.md は常にこのスクリプトの生成結果と一致していなければならない。

    migrations/ 追加後の再生成忘れ(乖離)を検出する回帰テスト。CIのdoc-gen-driftジョブと同義。
    """
    committed = OUTPUT_PATH.read_text()
    rendered = build_markdown()
    assert committed == rendered
