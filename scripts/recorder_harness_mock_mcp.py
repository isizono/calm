"""記録役の回帰ハーネス用のmock CALM MCPサーバー（stdio）。

記録役に許可されているツールのうち、片の処理に要るもの（search・add_logs・
add_material・add_relation）だけを本物と同じ名前・引数で持ち、DBには触れない。
呼び出しは1行1件のJSONで、第1引数のログファイルへ追記する。

searchは常に固定の1件（FIXTURE_LOG）を返す。ハーネスの3つ目の片はこの記録と
同じ経緯なので、記録役は `SKIP 重複 log:<id>` で飛ばすことが期待される。
"""
import json
import sys
import time
from itertools import count
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

FIXTURE_LOG = {
    "type": "log",
    "id_raw": 4242,
    "title": "見張りの片番号ずれバグを直した経緯",
    "snippet": "見張りが片番号を1つ飛ばして渡すバグを観察し、cursorの確定順を直して解決した。",
}

_LOG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else None
_ids = count(9000)
mcp = FastMCP("calm")


def _log(tool: str, args: dict) -> None:
    if _LOG_PATH is None:
        return
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "tool": tool, "args": args}, ensure_ascii=False) + "\n")


@mcp.tool()
def search(
    keyword: str | list[str],
    tags: list[str] | None = None,
    entity_type: Literal["topic", "decision", "activity", "log", "material"] | None = None,
    limit: int = 10,
) -> dict:
    """記録を検索する。"""
    _log("search", {"keyword": keyword, "tags": tags, "entity_type": entity_type, "limit": limit})
    return {"results": [FIXTURE_LOG], "total_count": 1}


@mcp.tool()
def add_logs(items: list[dict]) -> dict:
    """ログを記録する。"""
    _log("add_logs", {"items": items})
    return {"created": [{"log_id": next(_ids), "topic_id": i.get("topic_id")} for i in items], "errors": []}


@mcp.tool()
def add_material(title: str, content: str, tags: list[str], source: str, related: list[dict] | None = None) -> dict:
    """資材を記録する。"""
    _log("add_material", {"title": title, "tags": tags, "source": source, "related": related})
    return {"material_id": next(_ids), "title": title}


@mcp.tool()
def add_relation(source_type: str, source_id: int, targets: list[dict], relation_type: str = "related") -> dict:
    """記録同士をつなげる。"""
    _log("add_relation", {"source_type": source_type, "source_id": source_id, "targets": targets})
    return {"created": len(targets)}


if __name__ == "__main__":
    mcp.run()
