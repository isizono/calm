"""entityイベント購読の構造的不成立バグの修正を、実write経路から一気通貫で検証する
integration test。

対象は2つの独立した修正の組み合わせ:

1. `relay_subscribe` の購読labelsへの自handle自動付与を廃止（非空labelsはそのまま
   購読され、entity publish（購読者handleを含まない）とマッチできるようになった）
2. `entity_publish`のself label付与をask限定から全entity種別へ拡張し、
   `_TAG_JUNCTION`にaskを追加（ask個体label・domainタグがpublish labelsに載る）

どちらか片方だけでは「askの回答通知が届かない」バグは解消しない。本テストは
add_ask/answer_ask（実write経路）→ relay_outbox → 実HTTP POST /publish（FakeRelay）
→ 実subset matching → 実intake → session inbox、まで実コードパスを通して検証する
（dispatcherの常駐polling loop自体はSDK側の既存実装でありスコープ外のため、
1回分のdispatchはpoll()/post_publish()で明示的に行う）。
"""
from __future__ import annotations

import threading
import time

import pytest

from relay_sdk.http import post_publish
from relay_sdk.http.auth import make_client
from relay_sdk.outbox import mark_delivered, poll
from relay_sdk.testing import FakeRelay
from src.db import get_connection
from src.services import ask_service as ak
from src.services.activity_service import add_activity
from src.services.relay import config, intake, service


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("RELAY_IDENTITY", "cc-memory")


def _run_intake_briefly(stop_event: threading.Event, seconds: float) -> None:
    reconfigure = threading.Event()

    def stopper():
        time.sleep(seconds)
        stop_event.set()

    t = threading.Thread(target=stopper, daemon=True)
    t.start()
    intake.run(
        stop_event,
        reconfigure,
        rescan_interval_seconds=0.2,
        reconnect_backoff_initial=0.1,
        reconnect_backoff_cap=0.5,
    )


def _dispatch_pending_rows_to_fake(fake: FakeRelay) -> list[dict]:
    """relay_outboxのpending行を実HTTP POST /publishでFakeRelayへ配達する（1回分）。

    dispatcherの常駐polling loopは起動せず、poll()で取得した行をpost_publish()で
    そのまま送る（デバッグ用APIだが本番と同じpublisher側関数を通る）。配達成功行は
    mark_delivered()で処理済みにする。返り値はFakeRelay側の応答一覧。
    """
    conn = get_connection()
    try:
        rows = poll(conn)
        assert rows, "relay_outboxにpending行が無い"
        responses = []
        with make_client(config.get_base_url(), bearer_token=config.get_token()) as client:
            for row in rows:
                response = post_publish(
                    client,
                    ref={"type": row["ref_type"], "id": row["ref_id"]},
                    labels=row["labels"],
                    title=row["title"],
                    idempotency_key=row["idempotency_key"],
                )
                responses.append(response)
        mark_delivered(conn, [row["id"] for row in rows])
        conn.commit()
        return responses
    finally:
        conn.close()


def test_ask_self_label_subscription_receives_answer_notification(
    monkeypatch, temp_db, disable_embedding
):
    """askの個体label購読が、回答（event:updated）を実際に受信できることを検証する。

    修正前は次のいずれかが欠けており構造的に届かなかった:
    - handle自動付与により、購読条件に実publishが持たない自handleが混入し
      subset判定が恒久的に不成立（relay_subscribeの修正対象）
    - askのcreated以外のイベントでもself labelとown_tagsは元々ask限定で付いて
      いたため実は影響しないが、他entity種別では individual label 購読が
      一切成立しなかった（entity_publishの修正対象、他テストで別途検証）
    """
    with FakeRelay() as fake:
        monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

        activity = add_activity(
            title="a", description="d", tags=["domain:test"], check_in=False
        )
        ask = ak.add_ask("質問", tags=["domain:test"], blocks=[activity["activity_id"]])
        ask_id = ask["id"]

        sub_result = service.relay_subscribe(
            [f"ask:{ask_id}"], caller_session_id="sess-1"
        )
        assert "error" not in sub_result, sub_result
        subscription_id = sub_result["subscription_id"]

        ak.answer_ask(ask_id, "回答本文")

        matched_before = fake.outbox_size(subscription_id)
        responses = _dispatch_pending_rows_to_fake(fake)
        assert any(r.get("matched_subscriptions", 0) >= 1 for r in responses), responses
        matched_after = fake.outbox_size(subscription_id)
        assert matched_after > matched_before

        stop = threading.Event()
        _run_intake_briefly(stop, seconds=2.5)

        received = service.relay_receive(caller_session_id="sess-1")
        assert received["count"] >= 1, received
        matching = [m for m in received["messages"] if m["ref"] == {"type": "ask", "id": str(ask_id)}]
        assert matching, received


def test_entity_type_individual_subscription_receives_own_update(
    monkeypatch, temp_db, disable_embedding
):
    """ask以外のentity種別（activity）でも、個体label購読が自身のevent:updatedに
    マッチすることを検証する（entity_publishのself label全種別拡張の効果）。"""
    with FakeRelay() as fake:
        monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

        from src.services.activity_service import update_activity

        activity = add_activity(
            title="a", description="d", tags=["domain:test"], check_in=False
        )
        activity_id = activity["activity_id"]

        sub_result = service.relay_subscribe(
            [f"activity:{activity_id}"], caller_session_id="sess-1"
        )
        assert "error" not in sub_result, sub_result

        update_activity(activity_id, status="in_progress")

        _dispatch_pending_rows_to_fake(fake)

        stop = threading.Event()
        _run_intake_briefly(stop, seconds=2.5)

        received = service.relay_receive(caller_session_id="sess-1")
        assert received["count"] >= 1, received
        matching = [
            m for m in received["messages"]
            if m["ref"] == {"type": "activity", "id": str(activity_id)}
        ]
        assert matching, received
