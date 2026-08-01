"""decay_utilsのユニットテスト（純ロジック、DB不要）"""
from datetime import datetime, timedelta, timezone

from src.services.decay_utils import is_decay_eligible


def _ts(days_ago: float) -> str:
    """days_ago日前のタイムスタンプ文字列（DBのTIMESTAMP列と同じ書式）を返す。"""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class TestIsDecayEligible:
    """is_decay_eligibleの判定ロジックのテスト"""

    def test_recent_creation_not_eligible_regardless_of_last_referenced(self):
        """作成が新しい（decay_days以内）場合、last_referenced_atに関わらず非decay"""
        created = _ts(10)
        assert is_decay_eligible(created, None, decay_days=90) is False
        assert is_decay_eligible(created, _ts(200), decay_days=90) is False

    def test_old_creation_recent_reference_not_eligible(self):
        """作成が古くてもlast_referenced_atが新しければ非decay"""
        created = _ts(200)
        last_ref = _ts(5)
        assert is_decay_eligible(created, last_ref, decay_days=90) is False

    def test_old_creation_no_reference_is_eligible(self):
        """作成が古く、参照実績が無ければdecay対象"""
        created = _ts(200)
        assert is_decay_eligible(created, None, decay_days=90) is True

    def test_old_creation_old_reference_is_eligible(self):
        """作成が古く、last_referenced_atも古ければdecay対象"""
        created = _ts(200)
        last_ref = _ts(150)
        assert is_decay_eligible(created, last_ref, decay_days=90) is True

    def test_missing_created_at_not_eligible(self):
        """created_atがNoneの場合は非decay（安全側）"""
        assert is_decay_eligible(None, None, decay_days=90) is False

    def test_invalid_created_at_not_eligible(self):
        """created_atが不正な文字列の場合は非decay（安全側）"""
        assert is_decay_eligible("not-a-timestamp", None, decay_days=90) is False

    def test_boundary_exactly_decay_days_not_eligible(self):
        """作成からちょうどdecay_days日は非decay（境界は厳密な超過>であって>=でない）"""
        created = _ts(90)
        assert is_decay_eligible(created, None, decay_days=90) is False

    def test_boundary_just_over_decay_days_is_eligible(self):
        """作成からdecay_daysを超える（91日）とdecay対象になる"""
        created = _ts(91)
        assert is_decay_eligible(created, None, decay_days=90) is True

    def test_last_referenced_boundary_exactly_decay_days_not_eligible(self):
        """last_referenced_atからちょうどdecay_days日は非decay"""
        created = _ts(200)
        last_ref = _ts(90)
        assert is_decay_eligible(created, last_ref, decay_days=90) is False

    def test_last_referenced_boundary_just_over_is_eligible(self):
        """last_referenced_atからdecay_daysを超えるとdecay対象になる"""
        created = _ts(200)
        last_ref = _ts(91)
        assert is_decay_eligible(created, last_ref, decay_days=90) is True
