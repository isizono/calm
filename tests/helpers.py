"""テスト用互換ヘルパー

add_logs / add_decisions のバッチAPIを単件呼び出し形式でラップする。
旧 add_log / add_decision と同じインターフェースを提供する。

検索 retriever / orchestrator のテストで使う SearchContext のファクトリも
ここに集約する。

hooks/session_start_hook.py をsubprocessで起動するテスト共通のヘルパーも
ここに集約する（run_session_start_hook 以下）。
"""
import asyncio
import contextlib
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import src.config as _config
from src.env_compat import CANONICAL_PREFIX, env_names
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.retract_service import retract
from src.services.search_service import SearchContext

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# config.HABITS_RULES_PATHが CALM_HABITS_RULES_PATH 未設定時に解決する既定値。
# src/config.pyのフォールバック式をここで再定義せず、pytest collection時点
# （このモジュールの初回import時。どのテストのmonkeypatchフィクスチャも
# まだ実行されていないタイミング）で src.config から直接読み取ることで、
# config.py側の既定値が変わってもここが追従するようにする。
REAL_HABITS_RULES_PATH = _config.HABITS_RULES_PATH


@functools.lru_cache(maxsize=1)
def all_tool_descriptions() -> dict[str, str]:
    """全 MCP ツールの name→description を一括取得しキャッシュする（list_tools を 1 回に抑える）。

    ToolSearch/エージェントから見える tool description 文面を検証するテストで共有する。
    """
    from src.main import mcp

    async def _fetch():
        return {t.name: t.description for t in await mcp.list_tools()}

    return asyncio.run(_fetch())


@functools.lru_cache(maxsize=1)
def all_tool_schemas() -> dict[str, dict]:
    """全 MCP ツールの name→input schema を一括取得しキャッシュする（list_tools を 1 回に抑える）。"""
    from src.main import mcp

    async def _fetch():
        return {t.name: t.parameters for t in await mcp.list_tools()}

    return asyncio.run(_fetch())


def make_search_context(**overrides) -> SearchContext:
    """テスト用のデフォルト SearchContext を生成する。

    overrides で必要なフィールドだけ上書きできる。検索 retriever / orchestrator の
    各テストで重複していた _make_ctx を集約したもの。
    """
    defaults = dict(
        keywords=("alpha",),
        fts_keywords=("alpha",),
        original_keyword_count=None,
        tag_ids=None,
        entity_type=None,
        limit=10,
        offset=0,
        fetch_limit=50,
        keyword_mode="and",
        include_details=False,
        date_after=None,
        date_before=None,
        domain=None,
    )
    defaults.update(overrides)
    return SearchContext(**defaults)


def add_log(
    topic_id: int,
    title: Optional[str] = None,
    content: str = "",
    tags: Optional[list[str]] = None,
) -> dict:
    """単件のログ追加（add_logsのラッパー）。旧add_logと同じ戻り値形式を返す。"""
    item = {"topic_id": topic_id, "content": content}
    if title is not None:
        item["title"] = title
    if tags is not None:
        item["tags"] = tags
    result = add_logs([item])
    # バッチAPIのトップレベルエラー（バリデーションエラー等）
    if "error" in result:
        return result
    # アイテムレベルのエラー
    if result["errors"]:
        err = result["errors"][0]["error"]
        return {"error": err}
    # 成功
    return result["created"][0]


def add_decision(
    decision: str,
    reason: str,
    topic_id: int,
    tags: Optional[list[str]] = None,
) -> dict:
    """単件の決定事項追加（add_decisionsのラッパー）。旧add_decisionと同じ戻り値形式を返す。"""
    item = {"topic_id": topic_id, "decision": decision, "reason": reason}
    if tags is not None:
        item["tags"] = tags
    result = add_decisions([item])
    # バッチAPIのトップレベルエラー
    if "error" in result:
        return result
    # アイテムレベルのエラー
    if result["errors"]:
        err = result["errors"][0]["error"]
        return {"error": err}
    # 成功
    return result["created"][0]


def retract_decision(decision_id: int) -> dict:
    """単件のdecision取り消し（retract_serviceのラッパー）。"""
    return retract("decision", [decision_id])


@contextlib.contextmanager
def session_start_hook_env(
    db_path: str,
    *,
    extra_env: Optional[dict] = None,
    env_remove: Optional[list] = None,
    habits_rules_path: Optional[str] = None,
):
    """hooks/session_start_hook.py をsubprocess起動する際のenvを組み立てるcontext manager。

    conftestのautouse fixture（_isolate_habits_rules_projection）はテストプロセス内の
    `config.HABITS_RULES_PATH` しかpatchできず、hookは別プロセスで起動するため
    isolationが伝播しない。ここでCALM_HABITS_RULES_PATHを常に明示注入することで、
    呼び出し側が何も指定しなくても実ファイル（~/.claude/rules/cc-memory-habits.md）へ
    書き込まれない構造にする。hookを起動する経路（[sys.executable, ...]直接起動・
    uv run経由の起動等）によらず、subprocess env を組む箇所はこの関数を必ず通すこと。
    生のsubprocess.run向けにenvを自前組み立てしない。

    habits_rules_path 未指定時は呼び出しごとの使い捨てディレクトリを生成し、with
    blockを抜けるときに削除する。ファイル修復の検証等で複数呼び出しにまたがって
    パスを固定したいテストは明示的に渡すこと（実ファイルパスと一致する場合は
    ValueErrorで拒否する）。
    """
    if habits_rules_path is not None and habits_rules_path == REAL_HABITS_RULES_PATH:
        raise ValueError(
            "habits_rules_path が実ファイルパス（REAL_HABITS_RULES_PATH）と一致しています。"
            "tmp_path配下の使い捨てパスを指定してください。"
        )

    env = {**os.environ, "DISCUSSION_DB_PATH": db_path}
    # runnerのOW_ROLEを継承しない（テストの決定性確保。残存env検証テストはextra_envで明示設定する）
    env.pop("OW_ROLE", None)

    cleanup_dir: Optional[str] = None
    if habits_rules_path is None:
        cleanup_dir = tempfile.mkdtemp(prefix="ccm-habits-rules-")
        habits_rules_path = str(Path(cleanup_dir) / "cc-memory-habits.md")
    env["CALM_HABITS_RULES_PATH"] = habits_rules_path

    if extra_env:
        env.update(extra_env)
    if env_remove:
        for key in env_remove:
            # CALM_ 系は旧名フォールバックが効くため、CALM_ 名だけ消しても
            # 呼び出し元の環境に残った CCM_ / CC_MEMORY_ 名から値が復活する。
            # 新旧まとめて落とす。それ以外の環境変数はその名前だけ落とす。
            names = env_names(key) if key.startswith(CANONICAL_PREFIX) else (key,)
            for name in names:
                env.pop(name, None)

    try:
        yield env
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def run_session_start_hook(
    db_path: str,
    *,
    extra_env: Optional[dict] = None,
    env_remove: Optional[list] = None,
    stdin_payload: Optional[dict] = None,
    habits_rules_path: Optional[str] = None,
) -> dict:
    """session_start_hook.pyを`[sys.executable, ...]`で実行しstdoutのJSONを返す。

    引数の意味はsession_start_hook_envを参照。stdoutが空の場合はstderrを含めて
    assertion messageに出す。
    """
    with session_start_hook_env(
        db_path,
        extra_env=extra_env,
        env_remove=env_remove,
        habits_rules_path=habits_rules_path,
    ) as env:
        payload_str = "{}" if stdin_payload is None else json.dumps(stdin_payload)
        result = subprocess.run(
            [sys.executable, "hooks/session_start_hook.py"],
            input=payload_str,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env=env,
        )
    stdout = result.stdout.strip()
    assert stdout, f"session_start_hook.py produced no output. stderr: {result.stderr}"
    return json.loads(stdout)


def run_session_start_hook_process(
    db_path: str,
    *,
    extra_env: Optional[dict] = None,
    env_remove: Optional[list] = None,
    stdin_payload: Optional[dict] = None,
    habits_rules_path: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """session_start_hook.pyを実行し、生のCompletedProcess（stdout/stderr/returncode）を返す。

    stderrの内容自体を検証したいテスト（クラッシュしないことの確認等）向け。
    JSON化された結果だけで足りる場合はrun_session_start_hookを使うこと。
    """
    with session_start_hook_env(
        db_path,
        extra_env=extra_env,
        env_remove=env_remove,
        habits_rules_path=habits_rules_path,
    ) as env:
        payload_str = "{}" if stdin_payload is None else json.dumps(stdin_payload)
        return subprocess.run(
            [sys.executable, "hooks/session_start_hook.py"],
            input=payload_str,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env=env,
        )
