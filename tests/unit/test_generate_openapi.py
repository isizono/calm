"""scripts/generate_openapi.py のユニットテスト。

mcp.list_tools() は実際にサーバーへ登録された全ツールを返すため、
生成結果がその実データと一致するかを検証する（内部関数のmockはしない）。
"""
import asyncio

import yaml

from scripts.generate_openapi import TOOL_TAGS, build_openapi_doc, render_yaml


def _live_tools():
    from src.main import mcp

    async def _fetch():
        return await mcp.list_tools()

    return asyncio.run(_fetch())


def test_paths_cover_every_registered_tool():
    """生成された paths が mcp.list_tools() の全ツールと過不足なく一致する。"""
    tools = _live_tools()
    doc = build_openapi_doc()

    expected_paths = {f"/tools/{t.name}" for t in tools}
    assert set(doc["paths"].keys()) == expected_paths


def test_every_registered_tool_has_an_explicit_tag():
    """全ての登録済みツールがTOOL_TAGSに明示登録されていること。

    未登録のまま放置すると"misc"タグへ黙って落ちてしまい(generate_openapi.py内
    のコメント参照)、タグ分類ミスがCIで検出されない。ツール追加時にTOOL_TAGSへの
    追記漏れを機械的に検出する。
    """
    tools = _live_tools()
    tool_names = {t.name for t in tools}
    missing = tool_names - TOOL_TAGS.keys()
    assert not missing, f"TOOL_TAGS に未登録のツールがある: {sorted(missing)}"


def test_request_body_schema_matches_tool_parameters():
    """各ツールのrequestBodyスキーマが実際のtool.parametersそのものである(改変されていない)。"""
    tools = {t.name: t for t in _live_tools()}
    doc = build_openapi_doc()

    for path, item in doc["paths"].items():
        tool_name = path.removeprefix("/tools/")
        expected_schema = dict(tools[tool_name].parameters)
        actual_schema = item["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert actual_schema == expected_schema, tool_name


def test_every_path_is_tagged_with_a_declared_tag():
    """全pathのtagsが info.tags で宣言済みのタグ名であること(未知タグへの参照ミスを検出)。"""
    doc = build_openapi_doc()
    declared = {t["name"] for t in doc["tags"]}
    for path, item in doc["paths"].items():
        for tag in item["post"]["tags"]:
            assert tag in declared, f"{path} references undeclared tag {tag}"


def test_render_yaml_round_trips_to_same_structure():
    """レンダリングしたYAMLをパースし直すと元のdoc構造と一致する(手書きコメント行を除く)。"""
    doc = build_openapi_doc()
    rendered = render_yaml(doc)
    parsed = yaml.safe_load(rendered)
    assert parsed == doc


def test_generated_output_matches_committed_file():
    """docs/spec/openapi.yaml は常にこのスクリプトの生成結果と一致していなければならない。

    手動編集や、ツール追加後の再生成忘れ(乖離)を検出する回帰テスト。
    CIの doc-gen-drift ジョブと同じチェックをユニットテストとしても持たせておく。
    """
    from scripts.generate_openapi import OUTPUT_PATH

    committed = OUTPUT_PATH.read_text()
    rendered = render_yaml(build_openapi_doc())
    assert committed == rendered
