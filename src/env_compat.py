"""環境変数の新旧名フォールバック解決。

CALM への改名にあたり、環境変数の接頭辞を ``CALM_`` に統一した。改名前は
``CCM_`` 系と ``CC_MEMORY_`` 系の2系統に割れており、同じ「DBのパス」を
``CCM_DB_PATH``（サーバー本体）と ``CC_MEMORY_DB_PATH``（hook 群）が別名で
読んでいたため、片方だけ設定するとサーバーと hook が別DBを見る不整合があった。
接頭辞の統一はこの不整合の解消も兼ねる。

旧名を渡している既存デプロイ（リモートサーバーの launchd plist 等）を落とさない
ため、ハードリネームにはせず新名優先のフォールバックを設けている。解決順は
``CALM_<SUFFIX>`` → ``CCM_<SUFFIX>`` → ``CC_MEMORY_<SUFFIX>`` で、先に見つかった
名前の値を返す。値が空文字でも「設定済み」として扱い、後続の旧名は見ない。

各APIは現行名（``CALM_`` 始まり）を受け取り、旧名は内部で導出する。呼び出し側に
現行名がそのまま書かれるため、``CALM_DB_PATH`` のような grep で利用箇所を辿れる。

旧名フォールバックの撤去は別タスクで行う。
"""
from __future__ import annotations

import os
from typing import overload

# 現行名の接頭辞。新規に環境変数を書き込むときはこれを使う。
CANONICAL_PREFIX = "CALM_"

# フォールバック先の旧接頭辞。現行名が未設定のときこの順に探す。
LEGACY_PREFIXES: tuple[str, ...] = ("CCM_", "CC_MEMORY_")


def env_names(name: str) -> tuple[str, ...]:
    """現行名から、解決順に並んだ環境変数名のタプルを返す。

    例: ``env_names("CALM_DB_PATH")`` →
    ``("CALM_DB_PATH", "CCM_DB_PATH", "CC_MEMORY_DB_PATH")``
    """
    if not name.startswith(CANONICAL_PREFIX):
        raise ValueError(f"環境変数名は {CANONICAL_PREFIX} で始まる必要があります: {name!r}")
    suffix = name[len(CANONICAL_PREFIX) :]
    return (name, *(prefix + suffix for prefix in LEGACY_PREFIXES))


@overload
def env_get(name: str) -> str | None: ...


@overload
def env_get(name: str, default: str) -> str: ...


def env_get(name: str, default: str | None = None) -> str | None:
    """現行名を優先し、未設定なら旧名の順に読む。すべて未設定なら ``default``。"""
    for candidate in env_names(name):
        if candidate in os.environ:
            return os.environ[candidate]
    return default


def env_pop(name: str) -> None:
    """その環境変数を現行名・旧名すべて削除する。

    現行名だけ削除すると旧名から値が復活してしまうため、無効化したいときは
    必ずこちらを使う。
    """
    for candidate in env_names(name):
        os.environ.pop(candidate, None)


def env_set(name: str, value: str) -> None:
    """現行名に値を設定し、対応する旧名は削除する。

    旧名を残したまま現行名を設定しても解決順により現行名が勝つが、あとで
    現行名だけ ``pop`` された際に旧名の値が復活するため、書き込み時に旧名ごと
    畳んでおく。
    """
    env_pop(name)
    os.environ[name] = value


def env_snapshot(name: str) -> dict[str, str | None]:
    """その環境変数（現行名・旧名すべて）の現在値を控える。

    未設定の名前は値 ``None`` で保持する。``env_restore`` と対で使う。
    """
    return {candidate: os.environ.get(candidate) for candidate in env_names(name)}


def env_restore(snapshot: dict[str, str | None]) -> None:
    """``env_snapshot`` で控えた状態へ環境変数を戻す。"""
    for candidate, value in snapshot.items():
        if value is None:
            os.environ.pop(candidate, None)
        else:
            os.environ[candidate] = value
