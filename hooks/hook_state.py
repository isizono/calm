"""hook共通: 状態ファイル管理クラス HookState

hookが利用する状態ファイル（block_count, transcript_offset, current_turn,
checked_in_activity）とイベントファイル（events_{session_id}.jsonl）の読み書きを一元管理する。
標準ライブラリのみに依存。
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
    def clear_session(cls, session_id: str) -> None:
        """BASE_DIR内の全状態ファイルとeventsファイルを削除する"""
        session_id_safe = session_id.replace("/", "_")
        if not cls.BASE_DIR.exists():
            return
        for f in cls.BASE_DIR.glob(f"*_{session_id_safe}"):
            f.unlink(missing_ok=True)
        # events.jsonl は命名規則が異なるので個別削除
        events_file = cls.BASE_DIR / f"events_{session_id_safe}.jsonl"
        events_file.unlink(missing_ok=True)


if __name__ == "__main__":
    import json
    import os
    import sys

    if os.environ.get("HOOK_STATE_DIR"):
        HookState.BASE_DIR = Path(os.environ["HOOK_STATE_DIR"])

    if len(sys.argv) >= 2 and sys.argv[1] == "clear":
        data = json.loads(sys.stdin.read())
        session_id = data.get("session_id", "")
        if session_id:
            HookState.clear_session(session_id)
