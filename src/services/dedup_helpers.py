"""dedup（重複記録の集約）で共通に使う正規化・fingerprint生成ヘルパー。

signal_service / ask_service など、同一内容の再記録をoccurrence_count加算で
集約するサービス間で共有する。INSERT ... ON CONFLICT のconflict targetは
各サービスのテーブル固有の部分UNIQUE indexに依存するため、ここでは抽象化しない。
"""
from __future__ import annotations

import hashlib
import re


def normalize_text(s: str) -> str:
    """前後空白除去 + 連続空白畳み込み + 小文字化する。"""
    return re.sub(r"\s+", " ", s.strip()).lower()


def compute_fingerprint16(*parts: str) -> str:
    """任意個の文字列を'|'連結しsha256の先頭16hexを返す。"""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]
