"""レンダー時decay述語の共通ヘルパー。

「一定期間参照が無ければ自動注入対象から外す」という判定を、バッチジョブやcronで
事前計算するのではなく、呼び出しの都度（レンダー時）評価するための純関数を提供する。
判定結果はDBに書き戻さない。対象はSessionStartマニフェスト等の自動注入経路のみで、
一覧・検索系のAPI（get_habits・search_tags等）の返却対象からは除外しない。
"""
from datetime import datetime, timezone


def _parse_utc(ts: str | None) -> datetime | None:
    """DBのTIMESTAMP文字列（例: "2026-07-22 16:13:33"）をUTC awareなdatetimeへ変換する。

    欠損・パース不能な値はNoneを返す（呼び出し側で安全側に倒す判断に使う）。
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_decay_eligible(
    created_at: str | None, last_referenced_at: str | None, decay_days: int
) -> bool:
    """作成からdecay_days超過 かつ (参照実績が無いかdecay_days超過) ならTrueを返す。

    created_atが欠損・不正な場合はFalse（decay対象にしない、安全側）。
    比較は厳密な「超過」（>）。ちょうどdecay_days日はdecay対象にしない
    （作成直後・参照直後のエンティティを境界値で誤ってdecayさせないため）。

    Args:
        created_at: エンティティの作成日時（DB TIMESTAMP文字列）
        last_referenced_at: 最終参照実績日時（DB TIMESTAMP文字列、未参照ならNone）
        decay_days: decay判定の閾値日数

    Returns:
        decay対象（=自動注入から外すべき）ならTrue
    """
    created = _parse_utc(created_at)
    if created is None:
        return False
    now = datetime.now(timezone.utc)
    if (now - created).days <= decay_days:
        return False
    last_ref = _parse_utc(last_referenced_at)
    if last_ref is None:
        return True
    return (now - last_ref).days > decay_days
