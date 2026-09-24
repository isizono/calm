"""記録役セッションの目印ファイル(marker)管理。

記録役(別セッションでこのセッションの代わりにlog/materialを記録するプロセス)
が付いているセッションでは、Stop hookの記録催促(record_missing/
follow_up_after_decision/logs_sparse)を抑制する。判定にはセッションIDごとの
目印ファイルを使う。

パスの組み立てと読み書きをここに集約し、記録役プロセスの起動ラッパーや他の
hookからも同じ関数を使えるようにする(起動ラッパー本体は別PRで実装する)。
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


def write_marker(session_id: str, pid: int) -> None:
    """目印ファイルを書く。記録役プロセスの起動時に呼ばれる想定。

    起動時刻は自プロセスの`ps -o lstart=`出力をそのまま保存し、hook側の
    生存判定(is_recorder_attached)がpid再利用を誤って生存と判定しないように
    する。ps呼び出しに失敗した場合はstarted_atがNoneのまま保存され、hook側は
    「不一致」として「付いていない」扱いにする(フェイルセーフ)。
    """
    from src.infra.process_signature import process_start_signature

    marker_dir().mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "started_at": process_start_signature(pid)}
    marker_path(session_id).write_text(json.dumps(data), encoding="utf-8")


def remove_marker(session_id: str) -> None:
    """目印ファイルを削除する。無ければ何もしない。

    記録役プロセスの正常終了時に起動ラッパー側から呼ばれる想定。
    is_recorder_attachedが死亡判定した際の自動削除とは別経路。
    """
    marker_path(session_id).unlink(missing_ok=True)


def is_recorder_attached(session_id: str) -> bool:
    """このセッションに記録役が付いているかを判定する。

    目印ファイルのpidが生存しており、かつ起動時刻(`ps -o lstart=`)が
    目印ファイル記録時と一致する場合のみTrueを返す。

    記録役が死んでいる(pid死亡・別プロセスへの再利用のいずれか)と判定した
    場合は、目印ファイルが溜まり続けないようここで削除する(失敗しても無視
    する)。ファイル欠落・壊れたJSON・想定外の例外(import失敗を含む)の場合は
    「死んでいる」と確定できていないため削除は行わず、単に「付いていない」
    扱いにする(フェイルセーフ: 催促は普段どおり出す側に倒す)。本関数はStop
    hookの毎回の呼び出し経路に乗るため、ここで例外を外に漏らすと記録催促
    そのものが黙って出なくなる。

    # ponytail: 起動時刻はps -o lstart=の秒単位分解能までしか照合しない。
    # 同一秒内でのpid再利用までは防げない。気にするならコマンドライン照合を足す。
    # ponytail: ps呼び出しのタイムアウト等で生死が確定できない場合も
    # 「死んでいる」扱いで削除する。頻発するなら区別を足す。
    """
    path = marker_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        started_at = data["started_at"]

        from src.infra.process_signature import process_start_signature

        current_signature = process_start_signature(pid)
        if current_signature is not None and current_signature == started_at:
            return True

        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    except Exception:
        return False
