"""記録役セッションの目印ファイル(marker)管理。

記録役(別セッションでこのセッションの代わりにlog/materialを記録するプロセス)
が付いているセッションでは、Stop hookの記録催促(record_missing/
follow_up_after_decision/logs_sparse)を抑制する。判定にはセッションIDごとの
目印ファイルを使う。

パスの組み立てをここに集約し、記録役プロセスの起動ラッパーや他のhookからも
同じ関数を使えるようにする。
"""
import json
from pathlib import Path

from hooks.hook_state import HookState


def marker_dir() -> Path:
    """目印ファイルの置き場所を返す。HookStateの状態ディレクトリ配下。"""
    return HookState.BASE_DIR / "recorder"


def marker_path(session_id: str) -> Path:
    """セッションIDに対応する目印ファイルのパスを返す。"""
    safe = session_id.replace("/", "_")
    return marker_dir() / f"{safe}.json"


def is_recorder_attached(session_id: str) -> bool:
    """このセッションに記録役が付いているかを判定する。

    目印ファイルのpidが生存しており、かつ起動時刻(`ps -o lstart=`)が
    目印ファイル記録時と一致する場合のみTrueを返す。ファイル欠落・壊れた
    JSON・pid死亡・起動時刻不一致・ps呼び出し失敗に加え、import失敗を含む
    予期しない例外もすべて「付いていない」扱いにする(フェイルセーフ: 催促は
    普段どおり出す側に倒す)。本関数はStop hookの毎回の呼び出し経路に乗るため、
    ここで例外を外に漏らすと記録催促そのものが黙って出なくなる。

    # ponytail: 起動時刻はps -o lstart=の秒単位分解能までしか照合しない。
    # 同一秒内でのpid再利用までは防げない。気にするならコマンドライン照合を足す。
    """
    try:
        data = json.loads(marker_path(session_id).read_text(encoding="utf-8"))
        pid = int(data["pid"])
        started_at = data["started_at"]

        from src.infra.process_signature import process_start_signature

        current_signature = process_start_signature(pid)
        return current_signature is not None and current_signature == started_at
    except Exception:
        return False
