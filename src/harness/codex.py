"""HarnessインターフェースのCodex CLI実装。

Codexのhookプロトコルは意図的にClaude Code互換で設計されている
（stdin JSON入力・`hookSpecificOutput`各種フィールドの受理。wireスキーマは
codex-rs/hooks/src/schema.rs）。このためhook入出力はClaudeCodeHarnessを
継承し、Codexに存在しない機構だけを上書きする:

- Stop応答の `decision` はCodexでは `"block"` のみ有効で、`"approve"` は
  `deny_unknown_fields` によりパースエラーになる。承認（続行許可）は
  decisionフィールドの省略＝空応答で表現する（emit_approve）
- `updatedToolOutput` / `displayContent` に相当するwireフィールドが無い
  （未サポート系統。False返却）
- transcriptはrolloutファイル（`{timestamp, ordinal, type, payload}` の
  判別union JSONL）で、Claude Codeのフラット形式と全く異なる

## rollout正規化の方針

rolloutには同じ発話が複数の形で記録される（モデル生の `response_item` と、
Codexが整形した `event_msg`/`item_completed` のitemストリーム）。正規化は
**item_completedのみ**を情報源とし、それ以外の行は kind="other" に落とす:

- `response_item` のuser roleには実発話と機械注入（`<environment_context>`
  等）が混在し、行単体では区別できない。item側の `UserMessage` は実発話
  のみが対象になるため、turn境界の誤検出（Claude Codeの isMeta 相当の
  問題）を構造的に回避できる
- `McpToolCall` itemはserver/tool/arguments/resultを構造化して持つ。
  モデル生の記録はツール呼び出しがJSコード文字列に埋まる形（unified
  exec）になることがあり、名前・引数の機械抽出に適さない

item → 中間表現の対応:

| item.type | kind | content |
|---|---|---|
| UserMessage | user | text block列 |
| AgentMessage | assistant | text block列 |
| McpToolCall | assistant | tool_use block + tool_result block |

McpToolCallは `mcp__{server}__{tool}` 形式のツール名でtool_use blockを
合成する。呼び出し側（hook_transcriptのcalmツール判定 `calm__` マーカー・
activity_id抽出）がClaude Code形式と同じコードパスで処理できる。
"""
from __future__ import annotations

import json
from typing import Any

from src.harness.claude_code import ClaudeCodeHarness
from src.harness.interface import TranscriptEntry


def _text_blocks(content: Any) -> list[dict]:
    """item.contentのtext系blockを中間表現のtext blockへ変換する。"""
    blocks: list[dict] = []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    for b in content:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            blocks.append({"type": "text", "text": b["text"]})
    return blocks


def _parse_arguments(arguments: Any) -> dict:
    """McpToolCallのarguments（dictまたはJSON文字列）をdictへ寄せる。"""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _result_content(result: Any) -> list[dict]:
    """McpToolCallのresultをtool_result blockのcontent形式へ寄せる。

    MCPのtool結果は `{"content": [{"type": "text", "text": ...}]}` 形式で、
    Claude Codeのtool_result.contentと同構造。dict以外・content欠落は
    文字列化してtext block 1個に落とす。
    """
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return [b for b in result["content"] if isinstance(b, dict)]
    if result is None:
        return []
    return [{"type": "text", "text": str(result)}]


class CodexHarness(ClaudeCodeHarness):
    """Codex CLI用のHarness実装。

    hookプロトコルの共通部分（stdin JSON読み・hookSpecificOutput系出力・
    block判定・空応答）はClaudeCodeHarnessをそのまま継承し、Codexに
    存在しない機構だけを上書きする。選択はhook登録側の環境変数
    `CALM_HARNESS=codex`（.codex/hooks.jsonのコマンドに付与）で行う。
    """

    # ------------------------------------------------------------------
    # 1. hook入出力（差分のみ上書き）
    # ------------------------------------------------------------------

    def emit_approve(self, reason: str = "") -> None:
        """停止承認を空応答で出力する。

        CodexのStop応答wireは `decision: "block"` のみを受理し、
        `"approve"` はパースエラーになるため、承認はdecisionフィールドの
        省略で表現する。reasonは載せ先が無いため出力しない（診断用の
        文字列であり、動作には影響しない）。
        """
        self._emit({})

    def emit_updated_tool_output(self, updated_output: Any) -> bool:
        """Codexには相当機構が無いため、何も出力せずFalseを返す。"""
        return False

    def emit_display_content(self, text: str) -> bool:
        """Codexには相当機構（表示専用書き換え）が無いため、何も出力せず
        Falseを返す。対応イベント（MessageDisplay相当）自体も存在しない。
        """
        return False

    # ------------------------------------------------------------------
    # 2. transcript読み書き
    # ------------------------------------------------------------------

    @staticmethod
    def to_entry(raw: dict) -> TranscriptEntry:
        """rollout 1行を中間表現へ正規化する（方針はモジュールdocstring）。"""
        payload = raw.get("payload", {})
        if (
            raw.get("type") != "event_msg"
            or not isinstance(payload, dict)
            or payload.get("type") != "item_completed"
        ):
            return TranscriptEntry(kind="other", content=[], raw=raw)

        item = payload.get("item", {})
        if not isinstance(item, dict):
            return TranscriptEntry(kind="other", content=[], raw=raw)
        item_type = item.get("type")

        if item_type == "UserMessage":
            return TranscriptEntry(
                kind="user", content=_text_blocks(item.get("content")), raw=raw
            )
        if item_type == "AgentMessage":
            return TranscriptEntry(
                kind="assistant", content=_text_blocks(item.get("content")), raw=raw
            )
        if item_type == "McpToolCall":
            call_id = item.get("id", "")
            name = f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
            content = [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": _parse_arguments(item.get("arguments")),
                },
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _result_content(item.get("result")),
                },
            ]
            return TranscriptEntry(kind="assistant", content=content, raw=raw)

        return TranscriptEntry(kind="other", content=[], raw=raw)

    def rewrite_transcript_entry(self, path: str, entry: TranscriptEntry) -> bool:
        """rolloutの書き戻しは未実装のためFalse（未サポート扱い）。

        Codexがセッション中のrollout外部書き換えを許容するかは未検証で、
        sanitize backfill相当のCodex対応で扱う。それまでは安全側に倒す。
        """
        return False

    # ------------------------------------------------------------------
    # 3. プロセス識別
    # ------------------------------------------------------------------

    def resolve_session_identity(self) -> str | None:
        """CodexセッションのIdentity解決は未実装のためNone（fail-close）。

        祖先pid探索の対象になるlauncher登録がCodexセッションでどう成立
        するかの検証を含め、identity相当のCodex対応で扱う。
        """
        return None
