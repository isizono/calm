"""pytest共通フィクスチャ。

ow workerセッション（OW_ROLE=worker）内でテストを実行すると、
hookやサービス層がworkerフロー扱いになり、通常セッション前提のテストが
非決定的に壊れる。テスト実行環境からow関連の環境変数を除去し、
どの環境で実行してもテストが決定論的に振る舞うようにする。
"""
import os

import pytest

_OW_ENV_KEYS = ("OW_ROLE", "OW_ALIAS", "OW_CHANNEL", "OW_TASK_FILE")


@pytest.fixture(autouse=True)
def _clear_ow_env(monkeypatch):
    """ow関連の環境変数をテストごとに除去する。

    OW_ROLE=worker等が実行環境にリークしていても、テストは通常セッション
    として振る舞う。worker挙動を検証するテストは個別にenvを設定すること。
    """
    for key in _OW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
