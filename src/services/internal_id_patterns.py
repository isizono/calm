"""内部 ID パターン (`[MDLAT]#NNN` および英語フルワード `log/decision/activity/material/topic #NNN`)
の正規表現定数と type 名 → code mapping を集約する純粋層。

`citations_pure._convert_line_raw_to_cite` (write 経路の自動変換) と PreToolUse hook
(漏出 block) の両方からこの module を import する。DB アクセス・ファイル I/O は持たない。
"""
import re

__all__ = [
    "RAW_CITE_CODE_PATTERN",
    "RAW_CITE_FULLWORD_PATTERN",
    "FULLWORD_TO_CODE",
]

# 大文字 type code 形式 (`M#123`, `D#456`, `L#789`, `A#321`, `T#654`)。
# 前後の word boundary は lookbehind / lookahead で明示する
# (前が英数字 / `_` / `/` なら識別子の一部とみなして非マッチ、
#  後ろが英数字 / `_` なら同様)。
RAW_CITE_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])([MDLAT])#(\d+)(?![A-Za-z0-9_])"
)

# 英語フルワード形式 (`log #123`, `decision #456`, `activity #789`,
# `material #321`, `topic #654`)。case-insensitive、type 名と `#` の間の
# 空白は 0 個または 1 個のみ許容。
RAW_CITE_FULLWORD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])(log|decision|activity|material|topic) ?#(\d+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# fullword 形式の type 名を大文字 code に正規化する mapping。
# キーは小文字。`.lower()` で照会する。
FULLWORD_TO_CODE: dict[str, str] = {
    "log": "L",
    "decision": "D",
    "activity": "A",
    "material": "M",
    "topic": "T",
}
