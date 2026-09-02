"""ハーネス抽象化層。

Claude Code / Codex CLI などのエージェントハーネス固有機構（hookプロトコル・
transcriptファイル形式・セッション識別）を吸収するインターフェースと、
その実装を提供する。詳細は interface モジュールの docstring を参照。
"""
from src.harness.claude_code import ClaudeCodeHarness
from src.harness.codex import CodexHarness
from src.harness.interface import Harness, TranscriptEntry


def select_harness(hook_event_name: str | None = None) -> Harness:
    """環境変数 `CALM_HARNESS` に応じたHarness実装を返す。

    hook登録設定がハーネスごとに分かれている（Claude Code: hooks/hooks.json、
    Codex: .codex/hooks.json）ことを利用し、Codex側の登録コマンドにだけ
    `CALM_HARNESS=codex` を付与して明示的に選択する。ペイロード内容からの
    推定はしない（登録ファイルは自管理下にあり、明示指定の方が決定論的）。
    未設定・未知値はClaude Code（従来挙動）。
    """
    from src.env_compat import env_get

    if env_get("CALM_HARNESS", "").lower() == "codex":
        return CodexHarness(hook_event_name=hook_event_name)
    return ClaudeCodeHarness(hook_event_name=hook_event_name)


__all__ = [
    "Harness",
    "TranscriptEntry",
    "ClaudeCodeHarness",
    "CodexHarness",
    "select_harness",
]
