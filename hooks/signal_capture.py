"""hooks から signal_events への機械エラー捕捉用ラッパ。

signal_service.capture_signal_safe は DB 書き込み・validation の失敗を
握りつぶすが、そのモジュール自体の import 失敗（環境不整合等）までは
保証しない。hooks の top-level except は「絶対にクラッシュしてはいけない」
最終防衛ラインであるため、import から呼び出しまでの経路を丸ごと
try/except で包み、いかなる理由であれ例外を外に漏らさない関数として提供する。
"""
import sys


def try_capture_signal(
    kind: str,
    summary: str,
    *,
    source: str = "agent",
    detail: str | None = None,
) -> None:
    """signal_events への記録を試みる。いかなる例外も外に漏らさない。

    hooks の top-level except ブロックから呼ばれる想定。import 失敗・DB 接続不能を
    含め、ここで起きた例外は全て握りつぶして stderr にのみ出力する。呼び出し元の
    hook はこの関数の成否に関わらず既定のフェイルオープン出力を継続すること。

    Args:
        kind: signal種別（machine_error 等、KNOWN_KINDS のいずれか）
        summary: 1行要約
        source: 発生源。hook からの呼び出しは 'hook:<hook名>' を渡す
        detail: traceback・自由記述
    """
    try:
        from src.services.signal_service import capture_signal_safe

        capture_signal_safe(kind=kind, summary=summary, source=source, detail=detail)
    except Exception as e:
        print(f"signal_capture.py failed: {type(e).__name__}: {e}", file=sys.stderr)


def try_capture_guard_block(
    source: str,
    summary: str,
    *,
    detail: str | None = None,
) -> None:
    """hook が deny 判定を下したときに guard_block signal を記録する。

    deny 判定の経路（全tool呼び出しのうちdenyになった分だけ）から呼ぶ想定。
    get_connection() の busy_timeout=5000・sqlite-vec ロードは deny 判定には
    不要な遅延要因になるため経由せず、短いタイムアウトの独立接続で直接書き込む。
    DB ロック等で書き込みが失敗しても deny 判定自体の速度・結果には影響させない
    （try_capture_signal と同じく、いかなる例外も外に漏らさない）。
    """
    try:
        import sqlite3

        from src.db import get_db_path
        from src.services.signal_service import record_signal

        conn = sqlite3.connect(get_db_path(), timeout=0.5)
        try:
            record_signal("guard_block", summary, source=source, detail=detail, conn=conn)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"signal_capture.py (guard_block) failed: {type(e).__name__}: {e}", file=sys.stderr)
