# Jackpot

ジャックポット（d30で30）が出たときのネタ一覧。
`roll_dice` でこの中から1つを選ぶ。ネタの追加・変更はこのファイルだけで完結する。

| # | ネタ | やり方 |
|---|------|--------|
| 1 | ポエム | セッションの内容を題材にした短い自由詩を書く。真剣に、でも遊び心を持って |
| 2 | 俳句 | セッションの内容を五七五で詠む |
| 3 | ASCII art一コマ | セッション中の任意の単語をpickし、その単語をモチーフにしたASCII art一コマ＋一言コメントを描く。シンプルASCII文字（+, -, \|, /, \\, =, *, o 等）のみ使い、罫線文字（╔═╗等）は使わない |
| 4 | パロディ一言 | セッション中の任意の単語を2つpickし、1つ目を話題、2つ目から連想される人物やモノを主体にして一言喋らせる。主体のキャラを活かした口調で。形式: 〇〇「……」 |
| 5 | 考えさせられる問い | セッション中の任意の単語をpickし、そこから派生した答えのない問いを投げる。説教っぽくならないこと |
| 6 | 文学的一章 | セッション中の任意の単語をpickし、その単語をテーマにオリジナル短編小説の「第一章」を書く。20行程度。実在作品の引用はしない。ジャックポットにふさわしいボリュームで読み手を驚かせる |
| 7 | マルコフ連鎖 | セッション中の単語を3〜5語pickし、単語ごとに2〜3文（計8〜12文程度）の短文をその場で即興生成してコーパスにする。下記「7. マルコフ連鎖の実行スクリプト」の手順でコーパスをファイル化してスクリプトを実行し、出力された文をそのまま提示する |

## 7. マルコフ連鎖の実行スクリプト

ネタ7専用の補足。文字3-gramのマルコフ連鎖で文章を生成する。標準ライブラリの`random`のみを使い、外部ライブラリには依存しない。

手順1・2でセッション中の単語から即興生成した短文は、Writeツールで `/tmp/markov_corpus.txt` に書き出してから、以下のスクリプトを実行する。コーパスの文字列をPythonのソースコードやシェルのheredocに直接埋め込まず、いったんファイルに書き出してから読み込む構成にしているのは、コーパスの中身に `"""` のような文字列リテラルの終端やheredocの区切り文字と偶然一致する文字列が含まれていても、シェルやPythonのソースコードとして解釈されない（＝意図しないコード実行につながらない）ようにするため。

```bash
python3 <<'EOF'
import random

with open("/tmp/markov_corpus.txt", encoding="utf-8") as f:
    corpus = f.read().replace("\n", "")

N = 3  # 文字n-gram


def build_chain(text, n):
    chain = {}
    for i in range(len(text) - n):
        key = text[i:i + n]
        nxt = text[i + n]
        chain.setdefault(key, []).append(nxt)
    return chain


def generate(text, chain, n, length):
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


chain = build_chain(corpus, N)
random.seed()
print(generate(corpus, chain, N, length=random.randint(100, 150)))
EOF

rm -f /tmp/markov_corpus.txt
```

生成された文字列はそのままユーザーに提示する。文法は繋がるが意味は破綻しているのがこのネタの面白さなので、出力に手を加えて意味を通そうとしない。
