"""自己改善ループの器: hookとMCPサーバーの両方が使う共有の規則。

標準ライブラリだけで書く。src.db や src.services 配下の他モジュールを import
しない（それらは numpy・yoyo・sqlite_vec を引き込み、hookの起動コストを
押し上げるため）。dedup_helpers は hashlib・re しか import しない薄いモジュール
であることを確認済みなので、ここから使ってよい。
"""
from __future__ import annotations

import re

from src.services.dedup_helpers import normalize_text

# ---------------------------------------------------------------------------
# 地の文（`>` で始まる行とコードブロックを除いた部分）
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")


def plain_text(s: str) -> str:
    """地の文を返す。`>` で始まる行と、フェンスで囲まれたコードブロックを除く。

    フェンスが閉じずに終わった場合は、開いた以降が全部落ちる（安全側）。
    """
    out: list[str] = []
    in_fence = False
    for line in (s or "").replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 語彙（強い語・弱い語）
# ---------------------------------------------------------------------------

STRONG_VOCAB_PATTERN = re.compile(
    r"前にも|何回|何度|言った(よね|じゃん|でしょ)|またやって|また同じ|そうじゃない|違うって|やめてって"
)
WEAK_VOCAB_PATTERN = re.compile(r"違う|ちがう|じゃなくて|古い|間違|おかしい|戻して|なんで")


def compute_flag(text: str) -> str | None:
    """発話の地の文が語彙に当たったかを返す（'strong'|'weak'|None）。強い語を優先する。

    人間の発話かどうかに関係なく計算する（依頼になるかどうかは別の判定が見る）。
    """
    body = plain_text(text)
    if STRONG_VOCAB_PATTERN.search(body):
        return "strong"
    if WEAK_VOCAB_PATTERN.search(body):
        return "weak"
    return None


# ---------------------------------------------------------------------------
# 人間の打鍵の判別: 許可リストと本文の比較
# ---------------------------------------------------------------------------

# 人間の打鍵として扱う promptSource の値（許可リスト方式。一覧に無い値・欠けた
# 値はすべて人間でない扱いにする）。
ALLOWED_HUMAN_PROMPT_SOURCES = frozenset({"typed", "queued", "sdk"})


def is_human_speaker(turn_origin: object, prompt_source: object) -> bool:
    """speaker行のturnOrigin/promptSourceから人間の打鍵かどうかを判定する。

    許可リスト方式: turnOrigin='human' かつ promptSource が許可リストの値の
    いずれかであるときだけ True。値が欠けている・一覧に無い値であるとき、
    どちらも人間でない扱いにする（安全側）。
    """
    return turn_origin == "human" and prompt_source in ALLOWED_HUMAN_PROMPT_SOURCES


def transcript_body(content: object) -> str:
    """transcriptのユーザー行のmessage.contentから地の本文を1本の文字列にする。

    文字列部分だけをつなぐ（imageブロック等は落とす）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def bodies_match(hook_prompt: str, transcript_content: object) -> bool:
    """hook入力のpromptとtranscript行の本文が「同じ発話」かを判定する。

    前後空白の除去・連続空白の畳み込み・小文字化のうえで比較する
    （dedup_helpers.normalize_textをそのまま使う。改行は連続空白として畳まれる）。
    """
    return normalize_text(hook_prompt) == normalize_text(transcript_body(transcript_content))


# ---------------------------------------------------------------------------
# 上限・区切りの定数
# ---------------------------------------------------------------------------

TOOL_SUMMARY_MAX_CHARS = 300
TOOL_CALLS_PER_TURN_MAX = 40
TOOL_FAIL_MAX_CHARS = 1000
