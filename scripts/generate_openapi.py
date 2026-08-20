#!/usr/bin/env python3
"""``docs/spec/openapi.yaml`` を ``mcp.list_tools()`` から自動生成する。

一次情報は ``src/main.py`` の ``@mcp.tool()`` 関数群である。手動でopenapi.yamlを
書き換えて追従させる運用は、ツール追加・引数変更のたびに乖離を生む
（cc-memory 仕様書v0乖離監査で実測）。本スクリプトはツール一覧・引数スキーマを
実行時に問い合わせて機械的にYAMLへ変換し、その乖離を構造的に無くす。

使い方:
    uv run python scripts/generate_openapi.py            # docs/spec/openapi.yaml を上書き
    uv run python scripts/generate_openapi.py --check     # 差分があれば exit 1（CI用）
    uv run python scripts/generate_openapi.py --stdout    # 標準出力へ吐くだけ（書き込まない）

自動生成できるのはツール名・引数スキーマ（requestBody）・カテゴリタグ・summaryのみ。
各ツールの返り値の型（response schema）はdocstringからの機械抽出ができないため、
共通のゆるいスキーマ（ErrorResponseとのoneOf）に統一している。返り値の詳細な形は
`docs/spec/mcp-tools.md` の手書き記述、または実装（`src/main.py` / `src/services/`）を
参照すること。
"""
import argparse
import asyncio
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUT_PATH = REPO_ROOT / "docs" / "spec" / "openapi.yaml"

# ツール名 → カテゴリタグ。新規ツール追加時にこの辞書へ追記しないと "misc" に落ちる
# （generate側は落ちるだけで壊れない。乖離検出は他ドキュメント側のlintに任せる）。
TOOL_TAGS: dict[str, str] = {
    "add_topic": "topic",
    "get_topics": "topic",
    "add_logs": "log",
    "get_logs": "log",
    "add_decisions": "decision",
    "get_decisions": "decision",
    "add_activity": "activity",
    "get_activities": "activity",
    "update_activity": "activity",
    "add_material": "material",
    "get_material": "material",
    "update_material": "material",
    "export_material": "material",
    "search": "search",
    "get_by_ids": "search",
    "search_tags": "tag",
    "update_tag": "tag",
    "analyze_tags": "tag",
    "add_relation": "relation",
    "remove_relation": "relation",
    "resolve_destabilization": "relation",
    "suggest_destabilized_candidates": "relation",
    "add_pin": "pin",
    "remove_pin": "pin",
    "add_habit": "habit",
    "update_habit": "habit",
    "get_habits": "habit",
    "get_timeline": "timeline",
    "get_map": "timeline",
    "collect_export_candidates": "export",
    "set_instance_identity": "export",
    "export_bundle": "export",
    "check_in": "checkin",
    "retract": "retract",
    "get_config": "misc",
    "get_signals": "misc",
    "report_signal": "misc",
    "update_signal": "misc",
    "roll_dice": "misc",
    "pull_precedents": "misc",
    "detect_reask_candidates": "misc",
    "add_ask": "ask",
    "get_asks": "ask",
    "answer_ask": "ask",
    "withdraw_ask": "ask",
    "triage_ask": "ask",
    "relay_post": "relay",
    "relay_publish": "relay",
    "relay_subscribe": "relay",
    "relay_receive": "relay",
    "relay_status": "relay",
    "get_sessions": "session",
    "set_session_alias": "session",
}

TAG_DESCRIPTIONS: dict[str, str] = {
    "topic": "トピックの記録・取得",
    "log": "議論ログの記録・取得",
    "decision": "決定事項の記録・取得",
    "activity": "アクティビティの記録・取得・更新",
    "material": "資材の記録・取得・更新",
    "search": "横断検索・タグ検索・ID取得",
    "tag": "タグの更新・分析",
    "relation": "エンティティ間リレーション",
    "pin": "pin の追加・削除",
    "habit": "振る舞いの登録・更新",
    "timeline": "時系列取得",
    "export": "他インスタンスへのexport候補洗い出し・実行",
    "checkin": "アクティビティ check-in",
    "retract": "取り消し（論理削除）",
    "ask": "人間の判断待ちの問いの記録・回答",
    "relay": "セッション間メッセージング",
    "session": "並行セッションの別名管理",
    "misc": "設定取得・ユーティリティ",
}


def _summary_from_description(description: str | None) -> str:
    """docstring先頭行をsummaryとして使う。空なら空文字。"""
    if not description:
        return ""
    first_line = description.strip().splitlines()[0].strip()
    # docstring先頭が「〜する。」のような句点区切りのことが多いので、最初の文だけ使う
    if "。" in first_line:
        first_line = first_line.split("。", 1)[0] + "。"
    return first_line


async def _fetch_tools() -> list:
    from src.main import mcp

    return await mcp.list_tools()


def build_openapi_doc() -> dict:
    tools = asyncio.run(_fetch_tools())

    used_tags = sorted({TOOL_TAGS.get(t.name, "misc") for t in tools})
    tags = [
        {"name": tag, "description": TAG_DESCRIPTIONS.get(tag, tag)}
        for tag in used_tags
    ]

    paths: dict = {}
    for tool in sorted(tools, key=lambda t: t.name):
        tag = TOOL_TAGS.get(tool.name, "misc")
        request_schema = dict(tool.parameters) if tool.parameters else {"type": "object"}
        paths[f"/tools/{tool.name}"] = {
            "post": {
                "tags": [tag],
                "summary": _summary_from_description(tool.description),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": request_schema}
                    },
                },
                "responses": {
                    "200": {
                        "description": "ツール実行結果。正確な形状は該当ツールのdocstring（src/main.py）または docs/spec/mcp-tools.md を参照",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {"type": "object"},
                                        {"$ref": "#/components/schemas/ErrorResponse"},
                                    ]
                                }
                            }
                        },
                    }
                },
            }
        }

    doc = {
        "openapi": "3.0.3",
        "info": {
            "title": "cc-memory MCP server",
            "version": "0",
            "description": _LiteralStr(
                "cc-memory が提供する MCP ツール群の機械可読仕様。\n"
                "MCP は本来 JSON-RPC ベースだが、ここでは OpenAPI 3.0 形式で\n"
                "1 ツール = 1 path (POST) として整理する。\n"
                "本ファイルは scripts/generate_openapi.py が mcp.list_tools() から自動生成する。\n"
                "手動編集は次回生成で失われる。requestBody は実際の引数スキーマそのものだが、\n"
                "responses は自動抽出できないため簡略化してある（詳細は docs/spec/mcp-tools.md）。\n"
            ),
        },
        "servers": [
            {
                "url": "mcp://cc-memory",
                "description": "ローカル/HTTP/StreamableHTTP の任意のトランスポートを抽象化した仮想 base URL",
            }
        ],
        "tags": tags,
        "paths": paths,
        "components": {
            "schemas": {
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        }
                    },
                }
            }
        },
    }
    return doc


class _LiteralStr(str):
    """複数行文字列をYAMLのliteral block scalar (`|`) で出力させるためのマーカー型。"""


class _NoAliasDumper(yaml.SafeDumper):
    """anyOf等で同一dictの参照が発生してもYAMLアンカー(&id001等)を出さない。"""

    def ignore_aliases(self, data):
        return True


def _literal_str_representer(dumper: yaml.Dumper, data: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


_NoAliasDumper.add_representer(_LiteralStr, _literal_str_representer)


def render_yaml(doc: dict) -> str:
    header = (
        "# 自動生成ファイル。手動編集しないこと。\n"
        "# 生成元: scripts/generate_openapi.py（mcp.list_tools() から生成）\n"
        "# 再生成: uv run python scripts/generate_openapi.py\n"
    )
    body = yaml.dump(
        doc,
        Dumper=_NoAliasDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    return header + body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="生成結果と現ファイルを比較し、差分があれば exit 1（書き込みはしない）",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="標準出力へ書くだけでファイルは変更しない",
    )
    args = parser.parse_args()

    rendered = render_yaml(build_openapi_doc())

    if args.stdout:
        print(rendered, end="")
        return 0

    if args.check:
        current = OUTPUT_PATH.read_text() if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(f"differs: {OUTPUT_PATH} is stale relative to src/main.py の tool 定義", file=sys.stderr)
            print("再生成: uv run python scripts/generate_openapi.py", file=sys.stderr)
            return 1
        print(f"ok: {OUTPUT_PATH} is up to date")
        return 0

    OUTPUT_PATH.write_text(rendered)
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
