"""Harness抽象化インターフェース。

cc-memoryの各hookは、これまでClaude Code固有のhookプロトコル
（stdinからのJSON入力、`hookSpecificOutput`各種フィールドへの出力）・
transcriptファイル形式（フラットな`type`/`message.content`のJSONL）・
プロセス識別方式（祖先pid探索）に直接依存していた。Codex CLI対応に
先立ち、ハーネス依存箇所を本インターフェース越しの呼び出しに
置き換えられるよう、対象操作を3系統に整理して定義する。

1. **hook入出力**: `read_hook_input` と `emit_*` 系。ハーネスのhook
   プロトコル（入力の受け取り方・判定結果の返し方）を吸収する
2. **transcript読み書き**: `read_transcript_entries` /
   `read_transcript_entries_from_offset` / `rewrite_transcript_entry`。
   ハーネスごとのファイル形式（Claude Codeのフラット`type`/
   `message.content`、Codexの`{timestamp, ordinal?, type, payload}`
   判別union）を吸収し、共通の中間表現 `TranscriptEntry` を返す
3. **プロセス識別**: `resolve_session_identity`。ハーネスによって実現
   方式が異なる（hook入力からの注入 / プロセス祖先探索など）ことを
   想定し、戻り値の型だけを揃える

Codex側に対応する仕組みが無い操作（`updatedToolOutput`相当の直接
書き換え、`displayContent`相当の発話後処理）は、例外を投げるのではなく
戻り値 `False` で「未サポート」を表現する。呼び出し側は戻り値を見て
代替方針（何もしない・別経路で通知する等）に分岐できる。

## 既存hook・identity.pyと本インターフェースの対応表

| ファイル | ハーネス依存箇所 | 対応メソッド |
|---|---|---|
| session_start_hook.py | stdin JSON (session_id/source/transcript_path) | read_hook_input |
| | hookSpecificOutput.additionalContext | emit_additional_context |
| stop_hook.py | stdin JSON (session_id/transcript_path) | read_hook_input |
| | `{"decision": "approve"/"block", "reason"}` 出力 | emit_approve / emit_block |
| | transcriptバイトオフセット差分読み | read_transcript_entries_from_offset |
| user_prompt_submit_hook.py | stdin JSON (session_id) | read_hook_input |
| | hookSpecificOutput.additionalContext | emit_additional_context |
| | 空JSON `{}` 出力 | emit_empty |
| preblock_hook.py | stdin JSON (tool_name/tool_input) | read_hook_input |
| | hookSpecificOutput.permissionDecision | emit_permission_decision |
| | 空JSON `{}` 出力 | emit_empty |
| sanitize_backfill_hook.py | stdin JSON (session_id/transcript_path/cwd) | read_hook_input |
| | transcript全読み＋atomic書き戻し | read_transcript_entries / rewrite_transcript_entry |
| sanitize_tool_result_hook.py | stdin JSON (tool_name/tool_response/cwd) | read_hook_input |
| | hookSpecificOutput.updatedToolOutput | emit_updated_tool_output（未サポート系統） |
| relay_monitor_watch_hook.py | stdin JSON (session_id/tool_name/tool_input/tool_response) | read_hook_input |
| | 空JSON `{}` 出力 | emit_empty |
| message_display_id_titles.py | stdin JSON (delta/assistant_message) | read_hook_input |
| | hookSpecificOutput.displayContent | emit_display_content（未サポート系統） |
| hook_transcript.py | フラット`type`/`message.content`のJSONL解析 | read_transcript_entries(_from_offset) + TranscriptEntry |
| src/services/relay/identity.py | HTTPヘッダ / 祖先pid探索によるidentity解決 | resolve_session_identity |

現時点ではインターフェース定義とClaude Code実装
（claude_code.ClaudeCodeHarness）の追加のみを行い、既存hookの実装本体は
変更していない。置き換えは後続で行う。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranscriptEntry:
    """transcript 1エントリのハーネス非依存な中間表現。

    Attributes:
        kind: エントリ種別。"user" / "assistant" / "system" / "other" の
            いずれか。ハーネス固有の別名（Claude Codeの"human"等）は
            実装側でこの4値に正規化する
        content: 正規化済みcontent block（dict）のリスト。各blockは
            少なくとも "type" キー（"text" / "tool_use" / "tool_result" 等）
            を持つ。ハーネス側で本文が素の文字列のエントリは
            `[{"type": "text", "text": ...}]` に正規化する
        is_meta: ハーネスが機械注入したエントリ（Claude Codeの
            isMeta=true: スキル内容注入等）ならTrue
        raw: ハーネス固有の元エントリ（無加工）。書き戻し
            （rewrite_transcript_entry）や、中間表現に載せていない
            フィールド（uuid・tool_use_id等）の参照に使う
    """

    kind: str
    content: list[dict]
    is_meta: bool = False
    raw: dict = field(default_factory=dict)


class Harness(ABC):
    """エージェントハーネス固有機構の抽象インターフェース。

    実装クラス（ClaudeCodeHarness、将来のCodexHarness）はhookプロセス
    1回の実行につき1インスタンス生成して使う想定。出力系メソッドは
    いずれもhookプロトコルの応答をstdout（相当）へ書き出す副作用を持ち、
    1プロセスにつき応答は1回だけ出力するのが呼び出し側の責務となる。
    """

    # ------------------------------------------------------------------
    # 1. hook入出力
    # ------------------------------------------------------------------

    @abstractmethod
    def read_hook_input(self) -> dict:
        """hookプロセスへの入力ペイロードを読み取って返す。

        入力が空の場合・トップレベルがdictでない場合は `{}` を返す。
        入力の構文が壊れている場合（不正なJSON等）は例外を送出し、
        フェイルオープン方針（握って続行するか・安全側の応答を返すか）は
        呼び出し側のhookが決める。
        """

    @abstractmethod
    def emit_additional_context(self, text: str) -> None:
        """会話コンテキストへの注入テキストを応答として出力する。

        Claude Codeでは `hookSpecificOutput.additionalContext` 相当。
        SessionStart / UserPromptSubmit系hookが使う。
        """

    @abstractmethod
    def emit_permission_decision(self, decision: str, reason: str) -> None:
        """ツール実行可否の判定を応答として出力する。

        Claude Codeでは `hookSpecificOutput.permissionDecision` /
        `permissionDecisionReason` 相当。PreToolUse系hookが使う。
        decisionはハーネスのプロトコルが定める値（Claude Codeなら
        "allow" / "deny" / "ask"）をそのまま渡す。
        """

    @abstractmethod
    def emit_block(self, reason: str) -> None:
        """エージェントの停止をブロックする判定を応答として出力する。

        Claude Codeでは `{"decision": "block", "reason": ...}` 相当。
        Stop系hookが使う。
        """

    @abstractmethod
    def emit_approve(self, reason: str = "") -> None:
        """エージェントの停止を承認する判定を応答として出力する。

        Claude Codeでは `{"decision": "approve"}` 相当（reasonは省略可）。
        Stop系hookが使う。
        """

    @abstractmethod
    def emit_empty(self) -> None:
        """「何もしない」ことを表す空応答を出力する。

        Claude Codeでは空JSON `{}` の出力相当。判定に該当しなかった
        hookの正常終了経路で使う。
        """

    @abstractmethod
    def emit_updated_tool_output(self, updated_output: Any) -> bool:
        """tool実行結果の書き換えを応答として出力する。

        Claude Codeでは `hookSpecificOutput.updatedToolOutput` 相当
        （PostToolUse）。updated_outputの内部構造はハーネス依存のため
        呼び出し側が組み立てた値をそのまま出力する。

        Returns:
            出力した場合True。ハーネスに対応機構が無い場合は何も出力
            せずFalse（未サポート。Codex側の代替方針は呼び出し側が決める）。
        """

    @abstractmethod
    def emit_display_content(self, text: str) -> bool:
        """エージェント発話の表示専用書き換えを応答として出力する。

        Claude Codeでは `hookSpecificOutput.displayContent` 相当
        （MessageDisplay）。transcriptとモデルコンテキストは変えず、
        ユーザー画面の表示だけを差し替える。

        Returns:
            出力した場合True。ハーネスに対応機構が無い場合は何も出力
            せずFalse（未サポート。Codex側の代替方針は呼び出し側が決める）。
        """

    # ------------------------------------------------------------------
    # 2. transcript読み書き
    # ------------------------------------------------------------------

    @abstractmethod
    def read_transcript_entries(self, path: str) -> list[TranscriptEntry]:
        """transcriptファイル全体を読み、中間表現のリストを返す。

        ファイル不在は空リスト。解析できない行は読み飛ばす
        （append中の書きかけ行を許容するため）。
        """

    @abstractmethod
    def read_transcript_entries_from_offset(
        self, path: str, offset: int
    ) -> tuple[list[TranscriptEntry], int, bool]:
        """transcriptをバイトオフセットから差分読みする。

        offsetがファイルサイズを超えている場合は0にリセットして全読み
        する（transcriptの付け替え・巻き戻りに対する防御）。

        Returns:
            (新規エントリのリスト, 次回読み出し用の新オフセット,
            オフセットリセットが発生したか) のタプル。リセット発生時は
            呼び出し側で差分読みに紐づく状態（turnカウンタ等）も
            リセットすべき。
        """

    @abstractmethod
    def rewrite_transcript_entry(self, path: str, entry: TranscriptEntry) -> bool:
        """transcript中の1エントリを書き戻す。

        entryは read_transcript_entries 系で得た中間表現の `raw` を
        書き換えたもの。どの行を差し替えるかの同定方法（Claude Codeなら
        `uuid` フィールド一致）はハーネス実装が担う。書き込みはatomic
        （一時ファイル + rename）に行う。

        Returns:
            書き戻した場合True。対象エントリを同定できない・ファイルが
            存在しない場合はFalse。ハーネスがtranscript書き換え自体を
            許さない場合も（例外ではなく）Falseを返す。
        """

    # ------------------------------------------------------------------
    # 3. プロセス識別
    # ------------------------------------------------------------------

    @abstractmethod
    def resolve_session_identity(self) -> str | None:
        """このhookプロセスが属するセッションの安定識別子を解決する。

        実現方式はハーネス依存（Claude Code: launcher登録ファイルと
        祖先pidチェーンの交差、Codex: hook入力からの注入等）。解決
        できない場合はNoneを返す（fail-close。推定で候補を返さない）。
        """
