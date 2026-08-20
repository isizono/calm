"""ハーネス抽象化層（issue #605）。

Claude Code / Codex CLI などのエージェントハーネス固有機構（hookプロトコル・
transcriptファイル形式・セッション識別）を吸収するインターフェースと、
その実装を提供する。詳細は interface モジュールの docstring を参照。
"""
from src.harness.claude_code import ClaudeCodeHarness
from src.harness.interface import Harness, TranscriptEntry

__all__ = [
    "Harness",
    "TranscriptEntry",
    "ClaudeCodeHarness",
]
