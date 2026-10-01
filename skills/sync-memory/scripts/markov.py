"""文字3-gramのマルコフ連鎖で文章を生成する(jackpotネタ「マルコフ連鎖」専用)。

標準ライブラリのrandomのみを使い、外部ライブラリに依存しない。コーパスを
Pythonソースやシェルのheredocに直接埋め込まず、いったんファイルへ書き出して
から読み込む構成にしているのは、コーパスの中身に区切り文字と偶然一致する
文字列が含まれていても、コードとして解釈されない(=意図しないコード実行に
つながらない)ようにするため。読み込み後はコーパスファイル自身を削除する。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

N = 3  # 文字n-gram


def build_chain(text: str, n: int) -> dict[str, list[str]]:
    chain: dict[str, list[str]] = {}
    for i in range(len(text) - n):
        key = text[i:i + n]
        nxt = text[i + n]
        chain.setdefault(key, []).append(nxt)
    return chain


def generate(text: str, chain: dict[str, list[str]], n: int, length: int) -> str:
    if not chain:
        return text
    key = random.choice(list(chain.keys()))
    result = key
    for _ in range(length):
        candidates = chain.get(key)
        if not candidates:
            key = random.choice(list(chain.keys()))
            continue
        nxt = random.choice(candidates)
        result += nxt
        key = result[-n:]
    return result


def main() -> None:
    # Windows既定のANSIコードページ(cp932等)ではstdoutが非UTF-8になり、
    # 生成文字列にcp932へ変換できない文字(em dash・絵文字等)が混じると
    # UnicodeEncodeErrorで落ちる。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    corpus_path = Path(sys.argv[1])
    try:
        corpus = corpus_path.read_text(encoding="utf-8").replace("\n", "")
        chain = build_chain(corpus, N)
        print(generate(corpus, chain, N, length=random.randint(100, 150)))
    finally:
        corpus_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
