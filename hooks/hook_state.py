"""hook共通: 状態ファイル管理クラス HookState

hookが利用する状態ファイル（block_count, transcript_offset, current_turn,
checked_in_activity, monitor_started, relay_identity）とイベントファイル
（events_{session_id}.jsonl）の読み書きを一元管理する。標準ライブラリのみに依存。
"""
import json
from pathlib import Path


class HookState:
    BASE_DIR = Path.home() / ".claude" / ".claude-code-memory" / "state"

    def __init__(self, session_id: str):
        self._session_id_safe = session_id.replace("/", "_")
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)

    # --- private helpers ---

    def _path(self, prefix: str) -> Path:
        return self.BASE_DIR / f"{prefix}_{self._session_id_safe}"

    def _read_int(self, path: Path, default: int = 0) -> int:
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return default

    def _read_str(self, path: Path) -> str | None:
        try:
            value = path.read_text().strip()
            return value if value else None
        except FileNotFoundError:
            return None

    def _write(self, path: Path, value: str) -> None:
        path.write_text(value)

    def _delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    # --- block_count ---

    def get_block_count(self) -> int:
        """state/block_count_{session_id_safe} を読む。
        ファイルなし or 内容が不正 -> 0"""
        return self._read_int(self._path("block_count"), 0)

    def increment_block_count(self) -> int:
        """インクリメントして書き込み、新しい値を返す"""
        new_val = self.get_block_count() + 1
        self._write(self._path("block_count"), str(new_val))
        return new_val

    def reset_block_count(self) -> None:
        """ファイル削除（missing_ok=True）"""
        self._delete(self._path("block_count"))

    # --- id_leak_count ---

    def get_id_leak_count(self) -> int:
        """assistant 発話に出現した cc-memory 内部 ID リテラル件数の累積。
        未設定 -> 0。MessageDisplay hook が観測時に increment し、
        UserPromptSubmit hook が次ターンで参照して system-reminder を注入
        した後に reset する想定。"""
        return self._read_int(self._path("id_leak_count"), 0)

    def increment_id_leak_count(self, by: int = 1) -> int:
        """count を by 加算して書き込み、新しい値を返す"""
        new_val = self.get_id_leak_count() + by
        self._write(self._path("id_leak_count"), str(new_val))
        return new_val

    def reset_id_leak_count(self) -> None:
        """ファイル削除（missing_ok=True）"""
        self._delete(self._path("id_leak_count"))

    # --- transcript_offset ---

    def get_transcript_offset(self) -> int:
        """transcript差分読みのバイトオフセットを取得。未設定 -> 0"""
        return self._read_int(self._path("transcript_offset"), 0)

    def set_transcript_offset(self, offset: int) -> None:
        """transcript差分読みのバイトオフセットを保存"""
        self._write(self._path("transcript_offset"), str(offset))

    # --- sanitize_offset ---

    def get_sanitize_offset(self) -> int:
        """sanitize 用 transcript 差分読みバイトオフセットを取得。未設定 -> 0。

        stop_hook の transcript_offset とは別管理。SessionStart backfill hook が
        過去 transcript の差分 sanitize を冪等に行うために独立した offset を持つ。
        """
        return self._read_int(self._path("sanitize_offset"), 0)

    def set_sanitize_offset(self, offset: int) -> None:
        """sanitize 用 transcript 差分読みバイトオフセットを保存"""
        self._write(self._path("sanitize_offset"), str(offset))

    # --- sanitize_failure_count ---

    def get_sanitize_failure_count(self) -> int:
        """SessionStart backfill hook の連続失敗回数を取得。未設定 -> 0。

        書き戻し失敗 (harness_race / io_error / rename_failed) や scan/sanitize 例外で
        インクリメントされ、成功時にリセットされる。閾値 (hook 側で N=3) に達したら
        その session の backfill は以降スキップする (ループ防止)。
        """
        return self._read_int(self._path("sanitize_failure_count"), 0)

    def set_sanitize_failure_count(self, count: int) -> None:
        """SessionStart backfill hook の連続失敗回数を保存"""
        self._write(self._path("sanitize_failure_count"), str(count))

    # --- current_turn ---

    def get_current_turn(self) -> int:
        """現在のturn番号を取得。未設定 -> 0"""
        return self._read_int(self._path("current_turn"), 0)

    def set_current_turn(self, turn: int) -> None:
        """現在のturn番号を保存"""
        self._write(self._path("current_turn"), str(turn))

    # --- checked_in_activity ---

    def get_checked_in_activity(self) -> int | None:
        """checked_in_activity_{session_id} を読む"""
        path = self._path("checked_in_activity")
        try:
            content = path.read_text().strip()
            return int(content) if content else None
        except (FileNotFoundError, ValueError):
            return None

    def set_checked_in_activity(self, activity_id: int) -> None:
        """checked_in_activity_{session_id} に書く"""
        self._write(self._path("checked_in_activity"), str(activity_id))

    # --- monitor_started (relay inbox監視) ---

    def get_monitor_started(self) -> bool:
        """このセッションでrelay inbox監視用のMonitorが起動済みかを取得。
        未設定（ファイルなし） -> False。"""
        return self._path("monitor_started").exists()

    def set_monitor_started(self) -> None:
        """monitor_started_{session_id} マーカーファイルを作成する（冪等）。"""
        self._write(self._path("monitor_started"), "1")

    # --- relay_identity (resolve_identity_by_ancestryの解決結果キャッシュ) ---

    def get_cached_relay_identity(self) -> str | None:
        """セッション単位でキャッシュ済みのrelay identityを取得。
        未設定（ファイルなし） -> None。

        resolve_identity_by_ancestryはps最大5回spawn（各2秒timeout）を伴う
        コストのある解決経路のため、一度成功した結果はセッション内で使い回す。
        """
        return self._read_str(self._path("relay_identity"))

    def set_cached_relay_identity(self, identity: str) -> None:
        """relay identityをセッション単位でキャッシュする（ps spawn回避）。"""
        self._write(self._path("relay_identity"), identity)

    # --- events.jsonl ---

    @property
    def events_path(self) -> Path:
        """events_{session_id_safe}.jsonl のパスを返す"""
        return self.BASE_DIR / f"events_{self._session_id_safe}.jsonl"

    def append_events(self, events: list[dict]) -> None:
        """events.jsonl にイベントを追記する"""
        if not events:
            return
        with open(self.events_path, "a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_events(self) -> list[dict]:
        """events.jsonl から全イベントを読み込む。ファイルなし -> 空リスト"""
        if not self.events_path.exists():
            return []
        events = []
        try:
            with open(self.events_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        return events

    # --- clear_session ---

    @classmethod
    def clear_session(cls, session_id: str, *, preserve: set[str] | None = None) -> None:
        """BASE_DIR内の全状態ファイルとeventsファイルを削除する。

        preserveにprefix名（例: "monitor_started"）を指定すると、そのファイルは
        削除対象から除外する。compact（セッションを継続したまま発火するイベント）
        では、生存中のMonitor watchや解決済みidentityはクリアすべきでない
        （watch自体はcompactで終了せず、launcherプロセスもcompactをまたいで
        生存するため）。
        """
        session_id_safe = session_id.replace("/", "_")
        if not cls.BASE_DIR.exists():
            return
        preserve = preserve or set()
        suffix = f"_{session_id_safe}"
        for f in cls.BASE_DIR.glob(f"*{suffix}"):
            prefix = f.name[: -len(suffix)]
            if prefix in preserve:
                continue
            f.unlink(missing_ok=True)
        # events.jsonl は命名規則が異なるので個別削除
        if "events" not in preserve:
            events_file = cls.BASE_DIR / f"events_{session_id_safe}.jsonl"
            events_file.unlink(missing_ok=True)


if __name__ == "__main__":
    import json
    import os
    import sys

    if os.environ.get("HOOK_STATE_DIR"):
        HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

    # compact時にクリア対象から除外するprefix。生存中のMonitor watch（
    # monitor_started）と解決済みidentity（relay_identity）はcompactで
    # 消える情報ではないため保持する。
    _COMPACT_PRESERVE = {"monitor_started", "relay_identity"}

    if len(sys.argv) >= 2 and sys.argv[1] == "clear":
        data = json.loads(sys.stdin.read())
        session_id = data.get("session_id", "")
        source = data.get("source")
        if session_id:
            preserve = _COMPACT_PRESERVE if source == "compact" else None
            HookState.clear_session(session_id, preserve=preserve)
