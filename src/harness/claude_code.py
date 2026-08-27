"""HarnessインターフェースのClaude Code実装。

Claude Codeのhookプロトコル（stdin JSON入力・stdoutへの
`hookSpecificOutput` JSON出力）、transcriptファイル形式（フラットな
`type`/`message.content` のJSONL）、identity解決（HTTPヘッダ / 祖先pid
探索。src/services/relay/identity.py）をHarnessインターフェースに載せる。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO

from src.harness.interface import Harness, TranscriptEntry

# Claude Codeのエントリtype → 中間表現kindの対応。ここに無いtypeは
# "other" に落とす（summary等、hookの判定対象にならないエントリ）。
_KIND_BY_TYPE = {
    "user": "user",
    "human": "user",  # 旧形式transcriptの別名
    "assistant": "assistant",
    "system": "system",
}


class ClaudeCodeHarness(Harness):
    """Claude Code用のHarness実装。

    Args:
        hook_event_name: このhookが処理するイベント名（"SessionStart"等）。
            `hookSpecificOutput` 形式の応答（emit_additional_context /
            emit_permission_decision / emit_updated_tool_output /
            emit_display_content）はClaude Code側がhookEventNameフィールドを
            要求するため、これらを使うhookでは必須。判定系
            （emit_block / emit_approve / emit_empty）しか使わないhookでは
            省略できる
        stdin / stdout: 入出力ストリーム。省略時はsys.stdin / sys.stdout。
            テストからストリームを注入するための引数で、実hookでは
            省略して使う
    """

    def __init__(
        self,
        hook_event_name: str | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self._hook_event_name = hook_event_name
        self._stdin = stdin
        self._stdout = stdout

    # ------------------------------------------------------------------
    # 1. hook入出力
    # ------------------------------------------------------------------

    def read_hook_input(self) -> dict:
        raw = (self._stdin or sys.stdin).read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def _emit(self, payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False), file=self._stdout or sys.stdout)

    def _emit_hook_specific(self, fields: dict) -> None:
        if not self._hook_event_name:
            raise ValueError(
                "hookSpecificOutput形式の応答にはhook_event_nameが必要です。"
                "ClaudeCodeHarness(hook_event_name=...)で生成してください。"
            )
        self._emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": self._hook_event_name,
                    **fields,
                }
            }
        )

    def emit_additional_context(self, text: str) -> None:
        self._emit_hook_specific({"additionalContext": text})

    def emit_permission_decision(self, decision: str, reason: str) -> None:
        self._emit_hook_specific(
            {
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        )

    def emit_block(self, reason: str) -> None:
        self._emit({"decision": "block", "reason": reason})

    def emit_approve(self, reason: str = "") -> None:
        payload: dict = {"decision": "approve"}
        if reason:
            payload["reason"] = reason
        self._emit(payload)

    def emit_empty(self) -> None:
        self._emit({})

    def emit_updated_tool_output(self, updated_output: Any) -> bool:
        self._emit_hook_specific({"updatedToolOutput": updated_output})
        return True

    def emit_display_content(self, text: str) -> bool:
        self._emit_hook_specific({"displayContent": text})
        return True

    # ------------------------------------------------------------------
    # 2. transcript読み書き
    # ------------------------------------------------------------------

    @staticmethod
    def _to_entry(raw: dict) -> TranscriptEntry:
        """Claude Codeのフラット形式エントリを中間表現へ正規化する。"""
        kind = _KIND_BY_TYPE.get(raw.get("type", ""), "other")
        content = raw.get("message", {}).get("content", [])
        if isinstance(content, str):
            blocks: list[dict] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
        else:
            blocks = []
        return TranscriptEntry(
            kind=kind,
            content=blocks,
            is_meta=bool(raw.get("isMeta")),
            raw=raw,
        )

    def read_transcript_entries(self, path: str) -> list[TranscriptEntry]:
        entries, _, _ = self.read_transcript_entries_from_offset(path, 0)
        return entries

    def read_transcript_entries_from_offset(
        self, path: str, offset: int
    ) -> tuple[list[TranscriptEntry], int, bool]:
        p = Path(path).expanduser()
        if not p.exists():
            return [], 0, False

        file_size = p.stat().st_size
        offset_reset = offset > file_size
        if offset_reset:
            offset = 0

        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read()
            new_offset = offset + len(data)

        entries: list[TranscriptEntry] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            entries.append(self._to_entry(raw))

        return entries, new_offset, offset_reset

    def rewrite_transcript_entry(self, path: str, entry: TranscriptEntry) -> bool:
        # Claude Codeのtranscriptエントリはuuidフィールドで同定する。
        uuid = entry.raw.get("uuid")
        if not uuid:
            return False
        p = Path(path).expanduser()
        if not p.exists():
            return False

        original = p.read_text(encoding="utf-8")
        lines = original.splitlines()
        replaced = False
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not replaced and stripped:
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict) and obj.get("uuid") == uuid:
                    out.append(json.dumps(entry.raw, ensure_ascii=False))
                    replaced = True
                    continue
            out.append(line)

        if not replaced:
            return False

        new_text = "\n".join(out)
        if original.endswith("\n"):
            new_text += "\n"

        fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, p)
        except OSError:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        return True

    # ------------------------------------------------------------------
    # 3. プロセス識別
    # ------------------------------------------------------------------

    def resolve_session_identity(self) -> str | None:
        # hookプロセスはMCPリクエストコンテキストを持たないため、
        # get_relay_identity()は通常Noneを返し、祖先pid探索へ
        # フォールバックする（既存hookと同じ解決順）。
        from src.services.relay.identity import (
            get_relay_identity,
            resolve_identity_by_ancestry,
        )

        return get_relay_identity() or resolve_identity_by_ancestry()
