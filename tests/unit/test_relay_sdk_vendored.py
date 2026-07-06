"""vendored relay_sdk がこのリポジトリのテスト環境で import・動作することの確認。

SDK 自体の網羅テストは出自リポジトリ（relay）側にあるため持ち込まない。
ここでは FakeRelay に対する subscribe → publish → receive の往復 1 本だけを固定する。
"""
from __future__ import annotations

import pytest

from src.relay_sdk.client import Event, subscribe
from src.relay_sdk.testing import FakeRelay


@pytest.mark.timeout(10)
def test_subscribe_publish_receive_round_trip():
    """FakeRelay に subscribe し、publish した event が receive() で届く。

    receive() は無応答時に無限再接続するループのため、イベントが届かない
    リグレッションが起きるとテストがハングする。pytest-timeout で打ち切る。
    """
    with FakeRelay() as fake:
        with subscribe(
            relay_base_url=fake.base_url,
            subscriber_identity="vendored-smoke",
            labels=["entity:decision"],
            agent_card_path=fake.fake_agent_card_path(),
        ) as sub:
            publish_id = fake.publish(
                ref_type="decision",
                ref_id=42,
                labels=["entity:decision"],
                title="smoke",
            )
            for event in sub.receive():
                assert isinstance(event, Event)
                assert event.publish_id == publish_id
                assert event.ref_type == "decision"
                assert event.ref_id == 42
                assert event.labels == ["entity:decision"]
                break
