"""skills/sync-memory/scripts/markov.py の単体テスト。

ディレクトリ名にハイフンを含む(sync-memory)ため通常のdotted importができず、
importlib.util.spec_from_file_locationでファイルパスから直接読み込む。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MARKOV_PATH = Path(__file__).resolve().parents[2] / "skills" / "sync-memory" / "scripts" / "markov.py"


def _load_markov_module():
    spec = importlib.util.spec_from_file_location("sync_memory_markov", _MARKOV_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_chain_maps_ngram_to_next_char():
    markov = _load_markov_module()

    chain = markov.build_chain("abcabc", 3)

    assert chain == {"abc": ["a"], "bca": ["b"], "cab": ["c"]}


def test_generate_returns_corpus_as_is_for_empty_chain():
    """n-gramを1つも作れない短いコーパス(chainが空)では、生成を諦めて
    コーパスそのものを返す(空文字列ではなく、入力をそのまま出力する)。
    """
    markov = _load_markov_module()

    assert markov.generate("ab", {}, 3, length=50) == "ab"


def test_generate_produces_requested_length_from_cyclic_corpus():
    """循環コーパスではどのキーからも必ず次の候補が見つかるため、
    生成文字列の長さは常に n + length になる(候補なしでキーを
    再抽選する分岐を通らない)。
    """
    markov = _load_markov_module()
    corpus = "abcabcabcabc"
    chain = markov.build_chain(corpus, 3)

    result = markov.generate(corpus, chain, 3, length=50)

    assert len(result) == 3 + 50
    assert set(result) <= {"a", "b", "c"}


def test_main_prints_generated_text_and_deletes_corpus_file(tmp_path, monkeypatch, capsys):
    markov = _load_markov_module()
    corpus_path = tmp_path / "markov_corpus.txt"
    corpus_path.write_text("abcabcabcabc", encoding="utf-8")
    monkeypatch.setattr(markov.sys, "argv", ["markov.py", str(corpus_path)])
    monkeypatch.setattr(markov.random, "randint", lambda a, b: 50)

    markov.main()

    out = capsys.readouterr().out.strip()
    assert len(out) == 3 + 50
    assert set(out) <= {"a", "b", "c"}
    assert not corpus_path.exists()


def test_main_deletes_corpus_file_even_on_empty_corpus(tmp_path, monkeypatch, capsys):
    """コーパスがn-gramを作れないほど短くても例外にせず、ファイルは消す
    (finally節での削除がgenerateの結果に関わらず実行されることを確かめる)。
    """
    markov = _load_markov_module()
    corpus_path = tmp_path / "markov_corpus.txt"
    corpus_path.write_text("ab", encoding="utf-8")
    monkeypatch.setattr(markov.sys, "argv", ["markov.py", str(corpus_path)])

    markov.main()

    assert capsys.readouterr().out.strip() == "ab"
    assert not corpus_path.exists()


def test_main_deletes_corpus_file_even_when_read_raises(tmp_path, monkeypatch):
    """コーパスファイルの読み込み自体が例外を出しても、finally節で削除する
    (正常終了後の削除ではなく、try/finally構造そのものを確かめる)。

    UTF-8としてデコードできないバイト列を書き込み、read_text(encoding="utf-8")
    自身に本物のUnicodeDecodeErrorを起こさせる(内部関数のmockは使わない)。
    """
    markov = _load_markov_module()
    corpus_path = tmp_path / "markov_corpus.txt"
    corpus_path.write_bytes(b"\xff\xfe")
    monkeypatch.setattr(markov.sys, "argv", ["markov.py", str(corpus_path)])

    with pytest.raises(UnicodeDecodeError):
        markov.main()

    assert not corpus_path.exists()


def test_main_reconfigures_stdout_to_utf8(tmp_path, monkeypatch):
    """Windows既定のANSIコードページ(cp932等)では、コーパスに含まれうる
    em dash・絵文字等でstdoutがUnicodeEncodeErrorになりうるため、UTF-8へ揃える。
    """
    markov = _load_markov_module()
    corpus_path = tmp_path / "markov_corpus.txt"
    corpus_path.write_text("abcabcabcabc", encoding="utf-8")
    monkeypatch.setattr(markov.sys, "argv", ["markov.py", str(corpus_path)])

    calls = []

    class FakeStdout:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

        def write(self, *a, **kw):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(markov.sys, "stdout", FakeStdout())

    markov.main()

    assert calls == [{"encoding": "utf-8"}]
