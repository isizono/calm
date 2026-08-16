"""内部 ID パターン (`[MDLAT]#NNN` および英語フルワード `log/decision/activity/material/topic #NNN`、
`#` を省略した `log/decision/activity/material/topic NNN` も含む) の正規表現定数と
type 名 → code mapping を集約する純粋層。

`#` 省略形式は PreToolUse hook (漏出 block) の `RAW_CITE_FULLWORD_PATTERN` でのみ許容する。
`citations_pure._convert_line_raw_to_cite` (write 経路の自動変換) は `#` 省略形式を
対象にすると自然文中の「type 名+数字」の並び (例: "Activity 1") を誤って書き換える
リスクがあるため、`#` 必須の `RAW_CITE_FULLWORD_HASH_REQUIRED_PATTERN` のみを使う。
DB アクセス・ファイル I/O は持たない。
"""
import re

__all__ = [
    "RAW_CITE_CODE_PATTERN",
    "RAW_CITE_FULLWORD_PATTERN",
    "RAW_CITE_FULLWORD_HASH_REQUIRED_PATTERN",
    "FULLWORD_TO_CODE",
]

# 大文字 type code 形式 (`M#123`, `D#456`, `L#789`, `A#321`, `T#654`)。
# 前後の word boundary は lookbehind / lookahead で明示する
# (前が英数字 / `_` なら識別子の一部とみなして非マッチ、
#  後ろが英数字 / `_` なら同様)。
# 範囲表記 (type#NNN-NNN 形式) の終端 ID は `(?:-(\d+))?` で任意キャプチャする。
# `/` はスラッシュ区切りの複数 ID 列挙 (type#NNN/type#NNN 形式) を独立したトークン
# として認識するため、前方 lookbehind の除外対象から外している。
RAW_CITE_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([MDLAT])#(\d+)(?:-(\d+))?(?![A-Za-z0-9_])"
)

# 英語フルワード形式。対応する type 名は5種 (log, decision, activity, material,
# topic)、case-insensitive。
# `#` ありのときは type 名との間の空白を 0 個または 1 個許容し、
# `#` を省略したときは type 名の直後にスペースをちょうど 1 個要求する
# (詰め書きの誤検知を防ぎ、空白 2 個以上のケースを除外するため)。
# block 用途 (preblock_hook) 専用。自動変換 (citations_pure) には
# RAW_CITE_FULLWORD_HASH_REQUIRED_PATTERN を使うこと。
# RAW_CITE_CODE_PATTERN と同様に、範囲表記の終端 ID を任意キャプチャし、
# `/` 区切りの複数 ID 列挙を独立したトークンとして認識できるよう lookbehind
# の除外対象から `/` を外している。
RAW_CITE_FULLWORD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(log|decision|activity|material|topic)(?: ?#| )(\d+)(?:-(\d+))?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# 英語フルワード形式 (`#` 必須版)。type 名と `#` の間の空白は 0 個または 1 個許容する。
# `#` を省略した「type 名+スペース+数字」の並びは、DB 上に該当 ID が実在すると
# 自然文 (例: "Activity 1 done") まで citation に書き換えてしまう実害があるため、
# 自動変換 (citations_pure._convert_line_raw_to_cite 経由の convert_raw_to_cite)
# はこの `#` 必須パターンのみを使う。
# RAW_CITE_CODE_PATTERN と同様に、範囲表記の終端 ID を任意キャプチャし、
# `/` 区切りの複数 ID 列挙を独立したトークンとして認識できるよう lookbehind
# の除外対象から `/` を外している。
RAW_CITE_FULLWORD_HASH_REQUIRED_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(log|decision|activity|material|topic) ?#(\d+)(?:-(\d+))?(?![A-Za-z0-9_])",
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
