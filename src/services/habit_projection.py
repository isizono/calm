"""habits DBから ~/.claude/rules 配下の自動生成ファイルへの投影サービス。

正はDB。本ファイルはDBから決定論的に本文を組み立て、ハッシュ付きメタデータと
共に原子的にファイルへ書き出す一方向キャッシュを提供する。ファイル側の手動編集は
保護されず、次回の export / verify_and_heal で上書きされる。

不変条件: habitsテーブルをINSERT/UPDATEするサービス層関数は、commit成功後に
export()（または export_and_annotate()）を呼ぶこと。呼び忘れても次のセッション
開始時のリコンサイル（verify_and_heal）が自己修復するため事故にはならないが、
反映が1セッション分遅れる。

注: verify_and_healはこのモジュール内に実装済みだが、本PR時点ではSessionStart
hookからまだ呼び出されていない（接続は別PRで実施予定）。そのためこの docstring
が説明する自己修復は現時点では発生せず、export呼び忘れは次にexportが呼ばれる
（もしくはhook接続後のセッション開始）までそのまま反映されない。
"""
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src import config
from src.db import get_connection
from src.services.habit_service import (
    get_active_habit_contents_with_conn,
    list_intelligently_habit_manifest_with_conn,
)

logger = logging.getLogger(__name__)

HABITS_RULES_FORMAT = 1

_NO_HABITS_LINE = "（現在有効な habits はない）"

_HEADER = (
    "# 振る舞い（cc-memory habits）\n"
    "\n"
    "このファイルは cc-memory が habits DB から自動生成する。手動編集は次回同期で"
    "失われる。変更は MCP ツール add_habit / update_habit で行う。正は cc-memory の"
    "habits DB にある。使い方は man skill を参照。"
)

_DISABLED_PLACEHOLDER_BODY = (
    "# 振る舞い（cc-memory habits）\n"
    "\n"
    "投影は停止中である（CCM_HABITS_RULES_EXPORT=0）。"
    "get_habits で現在の habits を取得すること。\n"
)

_MANIFEST_HEADING = "## 他の振る舞い（オンデマンド）"
_MANIFEST_INTRO = (
    "以下はタイトルのみ。該当しそうな作業では get_habits(habit_id=...) で全文を取得する:"
)

_TMP_FILE_PREFIX = ".cc-memory-habits-"
_TMP_FILE_SUFFIX = ".tmp"
_TMP_STALE_SECONDS = 60

_METADATA_PREFIX = "<!-- cc-memory:habits-projection"
_BODY_MARKER = "-->\n"
_CONTENT_HASH_RE = re.compile(r"content_hash:\s*sha256:([0-9a-fA-F]{64})")


def _normalize_inline(text: str) -> str:
    """箇条書き1行に安全に埋め込めるよう、改行・連続空白をスペース1個へ正規化する。

    content/titleに改行が含まれたまま「- {text}」として出力すると、2行目以降が
    箇条書きプレフィックスなしでそのまま出力され、投影ファイルのMarkdown構造が
    崩れる。add_habit/update_habitのバリデーションは非空チェックのみで改行を
    拒否・除去しないため、レンダリング直前にここで正規化する。
    """
    return " ".join(text.split())


def _fetch_habit_layers(conn) -> tuple[list[str], list[dict]]:
    """habitsの2層データ（always全文・intelligentlyマニフェスト）を1回のクエリ
    セットで取得する。render_body・verify_and_healの共通経路（呼び出し元が
    同じ2クエリを重複実行しないよう、ここに集約する）。
    """
    always_contents = get_active_habit_contents_with_conn(conn)
    manifest = list_intelligently_habit_manifest_with_conn(conn)
    return always_contents, manifest


def _render_body_from_layers(always_contents: list[str], manifest: list[dict]) -> str:
    """_fetch_habit_layersが返す2層データから投影ファイル本文を組み立てる。

    純関数（時刻に依存する述語をここに入れない）。同一入力からは常に同一本文を返す。
    always層は全文、intelligently層はマニフェスト（タイトル+habit_id）で構成する。
    有効なhabitsが0件のときは1行のプレースホルダ本文にする。
    """
    if not always_contents and not manifest:
        return f"{_HEADER}\n\n{_NO_HABITS_LINE}\n"

    sections = [_HEADER]

    if always_contents:
        sections.append(
            "\n".join(f"- {_normalize_inline(content)}" for content in always_contents)
        )

    if manifest:
        sections.append(_render_manifest_section(manifest))

    return "\n\n".join(sections) + "\n"


def render_body(conn) -> str:
    """active habitsから投影ファイル本文（メタデータコメントを除く部分）を組み立てる。

    純関数（時刻に依存する述語をここに入れない）。同一DB状態からは常に同一本文を返す。
    always層は全文、intelligently層はマニフェスト（タイトル+habit_id）で構成する。
    有効なhabitsが0件のときは1行のプレースホルダ本文にする。
    """
    always_contents, manifest = _fetch_habit_layers(conn)
    return _render_body_from_layers(always_contents, manifest)


def _render_manifest_section(manifest: list[dict]) -> str:
    """intelligently層マニフェストを、独立予算内に選抜して1セクションにレンダリングする。

    importance_score降順（呼び出し元のSELECTで整列済み）の先頭からmax_items件を列挙し、
    超過分は本文を切断せず「他N件 → get_habits」の件数行1行に縮退する。
    """
    max_items = config.PROJECTION_MANIFEST_MAX_ITEMS
    selected = manifest[:max_items]
    remainder = len(manifest) - len(selected)

    lines = [
        f"- {_normalize_inline(m['title'])} (habit_id={m['habit_id']})" for m in selected
    ]
    if remainder > 0:
        lines.append(f"- 他{remainder}件 → get_habits で確認")

    return f"{_MANIFEST_HEADING}\n\n{_MANIFEST_INTRO}\n\n" + "\n".join(lines)


def compute_hash(body: str) -> str:
    """本文の安定ハッシュ（sha256 hex）を返す。"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_file(body: str, now: datetime) -> str:
    """メタデータコメント（generated_at・content_hash）を付けたファイル全文を組み立てる。

    ハッシュ対象はこの関数が付けるコメントを除く本文部のみ。generated_atはハッシュ
    対象外のため、本文が不変ならgenerated_atだけ違うファイルでも書き換えをスキップできる
    （_write の skip 判定はcontent_hashのみを比較する）。
    """
    content_hash = compute_hash(body)
    header = (
        f"<!-- cc-memory:habits-projection format={HABITS_RULES_FORMAT}\n"
        f"generated_at: {now.isoformat()}\n"
        f"content_hash: sha256:{content_hash}\n"
        "-->\n"
    )
    return header + body


@dataclass
class FileState:
    """read_file_stateの結果。statusは'absent'（不在・破損・メタデータ欠損）か'ok'。"""

    status: str
    body: str = ""
    meta_hash: str | None = None
    body_hash: str | None = None


_ABSENT = FileState(status="absent")


def read_file_state(path) -> FileState:
    """投影ファイルの現在状態を読む。

    ファイル不在・読み取り不能・メタデータコメント欠損はすべて'absent'として扱う
    （破損とみなして再生成する）。'ok'のときはmeta_hash（記録済みハッシュ）と
    body_hash（実本文から再計算したハッシュ）の両方を返し、呼び出し元が比較できる
    ようにする。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return _ABSENT

    if not text.startswith(_METADATA_PREFIX):
        return _ABSENT

    idx = text.find(_BODY_MARKER)
    if idx == -1:
        return _ABSENT

    header = text[: idx + len(_BODY_MARKER)]
    match = _CONTENT_HASH_RE.search(header)
    if not match:
        return _ABSENT

    body = text[idx + len(_BODY_MARKER):]
    return FileState(
        status="ok",
        body=body,
        meta_hash=match.group(1).lower(),
        body_hash=compute_hash(body),
    )


def _write(body: str, *, force: bool) -> dict:
    """bodyを投影ファイルへ原子的に書き込む。

    forceでない場合、既存ファイルの本文ハッシュが新body一致すれば書き込みをスキップする
    （mtimeを不変に保つ）。同一ディレクトリに一時ファイルを書いてからos.replaceで
    差し替えるため、プロセスクラッシュ時も本体は無傷。一時ファイル名にはos.getpid()に
    加えてthreading.get_ident()も含める。本サーバーはFastMCP上でsyncなツール関数
    (add_habit/update_habit/add_decisions)を提供しており、同一プロセス内の複数
    スレッドから並行してexportが呼ばれうるため、pidのみでは一時ファイル名が衝突しうる
    （pidは同一プロセス内の全スレッドで同じ値になる）。例外はすべて捕捉して
    {"status": "failed", "message": ...}を返す（呼び出し元に伝播させない）。
    """
    path = Path(config.HABITS_RULES_PATH)

    if not force:
        current = read_file_state(path)
        if current.status == "ok" and current.body_hash == compute_hash(body):
            return {"status": "skipped"}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        file_content = render_file(body, now)

        tmp_path = (
            path.parent
            / f"{_TMP_FILE_PREFIX}{os.getpid()}-{threading.get_ident()}{_TMP_FILE_SUFFIX}"
        )
        tmp_path.write_text(file_content, encoding="utf-8")
        os.replace(tmp_path, path)

        _cleanup_stale_tmp_files(path.parent)

        logger.info(f"habits rules projection written: {len(body)} chars -> {path}")
        return {"status": "written"}
    except Exception as e:
        return {"status": "failed", "message": str(e)}


def _cleanup_stale_tmp_files(directory: Path) -> None:
    """os.replace後に残った一時ファイル（クラッシュの取り残し）を掃除する。

    並行exportが書き込み中の一時ファイルを消さないよう、更新から
    _TMP_STALE_SECONDS を過ぎたものだけを対象にする。書き込み中のtmpは
    直前に作られたばかりで必ず閾値未満に収まり、クラッシュの取り残しは
    時間経過で必ず閾値を超える。
    """
    now = time.time()
    for tmp_file in directory.glob(f"{_TMP_FILE_PREFIX}*{_TMP_FILE_SUFFIX}"):
        try:
            if now - tmp_file.stat().st_mtime < _TMP_STALE_SECONDS:
                continue
            tmp_file.unlink()
        except OSError:
            pass


def export(*, force: bool = False) -> dict:
    """habits DBから投影ファイルを再生成する。

    自前でDB接続を確保・解放する自己完結API（呼び出し元connに依存せず、hookからも
    直接呼べる）。kill switch（config.HABITS_RULES_EXPORT_ENABLED=False、環境変数
    CCM_HABITS_RULES_EXPORT=0）が立っているときはDBに触れず、プレースホルダ本文で
    ファイルを上書きしてから停止する（stale化したファイルが以後注入され続けるのを防ぐ）。
    例外はすべて捕捉して{"status": "failed", "message": ...}を返す。raiseしない。
    """
    if not config.HABITS_RULES_EXPORT_ENABLED:
        return _write(_DISABLED_PLACEHOLDER_BODY, force=force)

    try:
        conn = get_connection()
    except Exception as e:
        return {"status": "failed", "message": str(e)}

    try:
        body = render_body(conn)
    except Exception as e:
        return {"status": "failed", "message": str(e)}
    finally:
        conn.close()

    return _write(body, force=force)


def export_and_annotate(result: dict) -> dict:
    """DBコミット成功後のexport共通呼び出し口。

    add_habit / update_habit / add_decisions の habit propagation から呼ぶ。
    export失敗時のみresultに"rules_projection"キーを付与し、logger.warningに残す
    （resultの他のキー・呼び出し元の成否には影響させない）。戻り値は常にexportの
    結果dict。
    """
    try:
        proj = export()
    except Exception as e:
        proj = {"status": "failed", "message": str(e)}

    if proj.get("status") == "failed":
        logger.warning(f"habits rules export failed: {proj.get('message')}")
        result["rules_projection"] = proj

    return proj


def verify_and_heal(conn) -> dict:
    """投影ファイルの鮮度をDBと照合し、不一致なら自己修復する（SessionStart hook専用）。

    3値比較（ファイル本文の実ハッシュ・ファイルに記録されたハッシュ・DBから今
    レンダリングした本文のハッシュ）で fresh / stale / absent を判定する。手動編集
    （ファイル本文とメタデータの不一致）とDB側の変更取り残し（メタデータとファイルは
    一致するがDBとは不一致）は、どちらも「DB内容で上書き」という同じ処置になるため
    stale系として1つに扱う。

    kill switchが立っているときはexportを呼ばず、フォールバック注入も行わない
    （"disabled"を返す。完全停止スイッチの意味論を守る）。この場合DBに触れない
    ため、always_contents/manifestはNone（未取得）で返す。呼び出し元
    （hook側のフォールバック注入）は、必要ならそこで自前にクエリすること。

    Returns:
        {"status": "disabled"|"fresh"|"healed_absent"|"healed_stale"
                    |"failed_absent"|"failed_stale",
         "body": <レンダリング済み本文>,
         "always_contents": <always層の全文リスト> | None,
         "manifest": <intelligently層マニフェスト> | None}
        # body/always_contents/manifestはhookがフォールバック注入時にDBクエリを
        # 再実行せず再利用するためのもの（disabled以外は常にリストで返る）。
    """
    if not config.HABITS_RULES_EXPORT_ENABLED:
        return {"status": "disabled", "body": "", "always_contents": None, "manifest": None}

    always_contents, manifest = _fetch_habit_layers(conn)
    body = _render_body_from_layers(always_contents, manifest)
    h_db = compute_hash(body)
    state = read_file_state(config.HABITS_RULES_PATH)

    if state.status == "absent":
        result = _write(body, force=True)
        status = "healed_absent" if result["status"] != "failed" else "failed_absent"
        return {"status": status, "body": body, "always_contents": always_contents, "manifest": manifest}

    if state.body_hash == h_db and state.meta_hash == h_db:
        return {"status": "fresh", "body": body, "always_contents": always_contents, "manifest": manifest}

    if state.meta_hash != state.body_hash:
        logger.warning(
            "habits rules projection file appears manually edited; overwriting with DB content"
        )

    result = _write(body, force=True)
    status = "healed_stale" if result["status"] != "failed" else "failed_stale"
    return {"status": status, "body": body, "always_contents": always_contents, "manifest": manifest}
