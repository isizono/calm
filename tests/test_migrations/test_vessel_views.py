"""migration 0076_add_vessel_views のテスト

観測台帳（obs_events・lessons・lesson_entries）から、人間の裏づけ・出自・
守られた知見・現在の本文と条件・採点・未処理の依頼を計算する11本のビューを
検証する。
"""
import json
import os
import tempfile
import uuid

import pytest

from src.db import get_connection, init_database


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        conn = get_connection()
        try:
            yield conn
        finally:
            conn.close()
            os.environ.pop("DISCUSSION_DB_PATH", None)


# ---------------------------------------------------------------------------
# obs_events / lessons / lesson_entries への挿入ヘルパー
# ---------------------------------------------------------------------------


def _obs(conn, kind, session_id="s1", **fields):
    fields = {"session_id": session_id, "kind": kind, **fields}
    cols = ",".join(fields)
    qs = ",".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO obs_events ({cols}) VALUES ({qs})", list(fields.values()))
    return cur.lastrowid


def utterance(conn, text="発話", session_id="s1", prompt_id="p1", flag=None, agent_id=None):
    return _obs(conn, "utterance", session_id=session_id, prompt_id=prompt_id,
                text=text, flag=flag, agent_id=agent_id)


def speaker(conn, ref_id, session_id="s1", turn_origin="human", prompt_source="typed", **extra):
    payload = {"turnOrigin": turn_origin, "promptSource": prompt_source, **extra}
    return _obs(conn, "speaker", session_id=session_id, ref_id=ref_id, text=json.dumps(payload))


def human_utterance(conn, text="発話", session_id="s1", prompt_id="p1", flag=None,
                     turn_origin="human", prompt_source="typed"):
    """許可リストの組で人間の発話を1件作る。"""
    uid = utterance(conn, text=text, session_id=session_id, prompt_id=prompt_id, flag=flag)
    speaker(conn, uid, session_id=session_id, turn_origin=turn_origin, prompt_source=prompt_source)
    return uid


def reply(conn, text="応答", session_id="s1", prompt_id="p1"):
    return _obs(conn, "reply", session_id=session_id, prompt_id=prompt_id, text=text)


def lesson(conn, kind="prevent", handle=None, body="body text",
           deliver_event="tool_call", deliver_spec="{}",
           step_event="tool_call", step_spec="{}", quote=None):
    handle = handle or f"h-{uuid.uuid4().hex[:10]}"
    cur = conn.execute(
        "INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec, "
        "step_event, step_spec, quote) VALUES (?,?,?,?,?,?,?,?)",
        (kind, handle, body, deliver_event, deliver_spec, step_event, step_spec, quote),
    )
    return cur.lastrowid


def tally_lesson(conn, handle=None, body="tally body"):
    return lesson(conn, kind="tally", handle=handle, body=body,
                  deliver_event=None, deliver_spec=None, step_event=None, step_spec=None)


def entry(conn, lesson_id, kind, body=None, note=None, quote=None,
          deliver_event=None, deliver_spec=None, step_event=None, step_spec=None):
    cur = conn.execute(
        "INSERT INTO lesson_entries (lesson_id, kind, body, note, quote, "
        "deliver_event, deliver_spec, step_event, step_spec) VALUES (?,?,?,?,?,?,?,?,?)",
        (lesson_id, kind, body, note, quote, deliver_event, deliver_spec, step_event, step_spec),
    )
    return cur.lastrowid


def bind(conn, session_id, lesson_id, prompt_id=None, agent_id=None, entry_id=None, ref_id=None):
    return _obs(conn, "bind", session_id=session_id, prompt_id=prompt_id, agent_id=agent_id,
                lesson_id=lesson_id, entry_id=entry_id, ref_id=ref_id)


def delivered(conn, session_id, lesson_id, channel="prompt", prompt_id=None, tool_use_id=None):
    return _obs(conn, "delivered", session_id=session_id, prompt_id=prompt_id,
                lesson_id=lesson_id, channel=channel, tool_use_id=tool_use_id)


def suppressed(conn, session_id, lesson_id, channel="prompt", prompt_id=None):
    return _obs(conn, "suppressed", session_id=session_id, prompt_id=prompt_id,
                lesson_id=lesson_id, channel=channel)


def stepped(conn, session_id, lesson_id, prompt_id=None, tool_use_id=None):
    return _obs(conn, "stepped", session_id=session_id, prompt_id=prompt_id,
                lesson_id=lesson_id, tool_use_id=tool_use_id)


def human_withdraw(conn, session_id, lesson_id, ref_id):
    return _obs(conn, "human_withdraw", session_id=session_id, lesson_id=lesson_id, ref_id=ref_id)


def bound_human_entry(conn, lesson_id, entry_id, session_id, prompt_id, text="訂正の発話"):
    """1つの発話が丸ごと1ターンを構成し、そのターンでbindするという、人間の
    裏づけが成立する最小の実在しうる状態を作る。utterance→speaker→bind の順で
    obs_events.idが増えるので lesson_basis の前後判定もそのまま成立する。"""
    uid = human_utterance(conn, text=text, session_id=session_id, prompt_id=prompt_id)
    bid = bind(conn, session_id=session_id, lesson_id=lesson_id, prompt_id=prompt_id,
               entry_id=entry_id, ref_id=uid)
    return uid, bid


def score_row(conn, lesson_id):
    row = conn.execute("SELECT * FROM lesson_score WHERE lesson_id = ?", (lesson_id,)).fetchone()
    assert row is not None
    return row


def current_row(conn, lesson_id):
    row = conn.execute("SELECT * FROM lesson_current WHERE lesson_id = ?", (lesson_id,)).fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# 1. utterance_human
# ---------------------------------------------------------------------------


class TestUtteranceHuman:
    # 実測（preflight_origins.md）に現れた (promptSource, turnOrigin) の組。
    # 許可リストの3組だけを通し、実測に現れた他の組はすべて落とす。
    @pytest.mark.parametrize(
        "prompt_source,turn_origin,expect_human",
        [
            ("typed", "human", True),
            ("queued", "human", True),
            ("sdk", "human", True),
            ("typed", None, False),          # turnOrigin欠落（705件規模で実測）
            (None, "human", False),          # promptSource欠落＝スラッシュコマンドの展開
            ("system", None, False),
            ("system", "task_notification", False),
            ("system", "peer", False),
            ("sdk", "sdk", False),
            (None, None, False),
        ],
    )
    def test_allowed_combinations_only(self, db, prompt_source, turn_origin, expect_human):
        uid = utterance(db, session_id="s1", prompt_id="p1")
        speaker(db, uid, session_id="s1", turn_origin=turn_origin, prompt_source=prompt_source)
        db.commit()
        rows = db.execute("SELECT id FROM utterance_human WHERE id = ?", (uid,)).fetchall()
        assert bool(rows) is expect_human

    def test_no_speaker_row_is_not_human(self, db):
        uid = utterance(db, session_id="s1", prompt_id="p1")
        db.commit()
        rows = db.execute("SELECT id FROM utterance_human WHERE id = ?", (uid,)).fetchall()
        assert rows == []

    def test_view_excludes_created_at_ordering_dependency(self, db):
        """created_atを見ていないことを、同一のcreated_atでも順序に依らず判定
        できることで確かめる（実装は id だけで前後を決める契約）。"""
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        db.commit()
        row = db.execute("SELECT * FROM utterance_human WHERE id = ?", (uid,)).fetchone()
        assert "created_at" not in row.keys()


# ---------------------------------------------------------------------------
# 2. lesson_basis
# ---------------------------------------------------------------------------


class TestLessonBasis:
    def test_baseline_valid_basis(self, db):
        """3条件をすべて満たす最小の書き込みが人間の裏づけになる。"""
        lid = lesson(db)
        uid, bid = bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        db.commit()
        rows = db.execute(
            "SELECT lesson_id, entry_id, session_id, evidence_id, void FROM lesson_basis "
            "WHERE lesson_id = ?",
            (lid,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["entry_id"] is None
        assert rows[0]["session_id"] == "s1"
        assert rows[0]["evidence_id"] == uid
        assert rows[0]["void"] == 0

    def test_condition_agent_bind_breaks_basis(self, db):
        """bindがサブエージェント由来（agent_id あり）だと裏づけにならない。"""
        lid = lesson(db)
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        bind(db, session_id="s1", lesson_id=lid, prompt_id="p1", agent_id="sub-1", ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM lesson_basis WHERE lesson_id = ?", (lid,)).fetchall()
        assert rows == []

    def test_condition_turn_not_fully_human_breaks_basis(self, db):
        """書き込みのターン自体に人間でない発話が混ざっていると裏づけにならない。

        同じprompt_idに、人間の発話ともう1件の人間でない発話（許可リスト外の
        speaker値）が両方ある状態を作る。
        """
        lid = lesson(db)
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        other_uid = utterance(db, session_id="s1", prompt_id="p1", text="通知の割り込み")
        speaker(db, other_uid, session_id="s1", turn_origin="task_notification", prompt_source="system")
        bind(db, session_id="s1", lesson_id=lid, prompt_id="p1", ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM lesson_basis WHERE lesson_id = ?", (lid,)).fetchall()
        assert rows == []

    def test_condition_bind_without_prompt_id_breaks_basis(self, db):
        """bindのprompt_idがNULLだと、ターン自体が人間かを判定できず裏づけにならない。"""
        lid = lesson(db)
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        bind(db, session_id="s1", lesson_id=lid, prompt_id=None, ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM lesson_basis WHERE lesson_id = ?", (lid,)).fetchall()
        assert rows == []

    def test_condition_intervening_non_human_turn_does_not_block(self, db):
        """UとBの間に、人間でないと分かっている別ターンが挟まっても妨げない。"""
        lid = lesson(db)
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        notif_uid = utterance(db, session_id="s1", prompt_id="p-notif", text="通知")
        speaker(db, notif_uid, session_id="s1", turn_origin="task_notification", prompt_source="system")
        bid = bind(db, session_id="s1", lesson_id=lid, prompt_id="p1", ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM lesson_basis WHERE lesson_id = ?", (lid,)).fetchall()
        assert len(rows) == 1
        assert notif_uid < bid  # 割り込みが確かに間に挟まっていることの前提確認

    def test_condition_intervening_human_turn_breaks_basis(self, db):
        """UとBの間に別の人間のターンが挟まると、Uは直前のターンでなくなり裏づけにならない。

        Bのターン自身（p3）も人間の発話を持たせ、書き込みのターン自身が人間で
        あることは満たしたうえで、間に挟まる別ターン（p2）だけを問題にする。
        """
        lid = lesson(db)
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        human_utterance(db, session_id="s1", prompt_id="p2")  # 間に挟まる別の人間のターン
        human_utterance(db, session_id="s1", prompt_id="p3")  # Bのターン自身
        bind(db, session_id="s1", lesson_id=lid, prompt_id="p3", ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM lesson_basis WHERE lesson_id = ?", (lid,)).fetchall()
        assert rows == []

    def test_condition_intervening_speakerless_turn_breaks_basis(self, db):
        """UとBの間に、まだspeakerが無い（判定未了の）別ターンが挟まると裏づけにならない。"""
        lid = lesson(db)
        uid = human_utterance(db, session_id="s1", prompt_id="p1")
        utterance(db, session_id="s1", prompt_id="p2")  # speakerがまだ無い
        human_utterance(db, session_id="s1", prompt_id="p3")  # Bのターン自身
        bind(db, session_id="s1", lesson_id=lid, prompt_id="p3", ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM lesson_basis WHERE lesson_id = ?", (lid,)).fetchall()
        assert rows == []

    def test_same_turn_write_is_valid_basis(self, db):
        """今のターン（Uと同じprompt_id）でのbindも裏づけになる。"""
        lid = lesson(db)
        uid, _ = bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        db.commit()
        row = db.execute(
            "SELECT evidence_id FROM lesson_basis WHERE lesson_id = ?", (lid,)
        ).fetchone()
        assert row["evidence_id"] == uid


# ---------------------------------------------------------------------------
# 3〜4. lesson_violated_ok / lesson_contradicted_ok
# ---------------------------------------------------------------------------


class TestValidEntries:
    def test_violated_without_basis_is_invalid(self, db):
        lid = lesson(db)
        eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
        db.commit()
        rows = db.execute(
            "SELECT 1 FROM lesson_violated_ok WHERE lesson_id = ? AND entry_id = ?", (lid, eid)
        ).fetchall()
        assert rows == []

    def test_violated_with_basis_is_valid(self, db):
        lid = lesson(db)
        eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
        bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        db.commit()
        rows = db.execute(
            "SELECT session_id FROM lesson_violated_ok WHERE lesson_id = ? AND entry_id = ?",
            (lid, eid),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"

    def test_contradicted_requires_prior_delivery_in_same_session(self, db):
        lid = lesson(db)
        eid = entry(db, lid, "contradicted", quote="これは逐語の引用文です")
        bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        db.commit()
        # 配達が無いので無効
        rows = db.execute(
            "SELECT 1 FROM lesson_contradicted_ok WHERE lesson_id = ? AND entry_id = ?",
            (lid, eid),
        ).fetchall()
        assert rows == []

    def test_contradicted_valid_with_prior_delivery(self, db):
        lid = lesson(db)
        delivered(db, session_id="s1", lesson_id=lid, channel="prompt")
        eid = entry(db, lid, "contradicted", quote="これは逐語の引用文です")
        bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        db.commit()
        rows = db.execute(
            "SELECT 1 FROM lesson_contradicted_ok WHERE lesson_id = ? AND entry_id = ?",
            (lid, eid),
        ).fetchall()
        assert len(rows) == 1

    def test_contradicted_uses_bind_session_not_entry_created_at(self, db):
        """追記の有効性判定はbind行のセッションを使う（lesson_entriesにセッション列は無い）。"""
        lid = lesson(db)
        delivered(db, session_id="s2", lesson_id=lid, channel="prompt")
        eid = entry(db, lid, "contradicted", quote="これは逐語の引用文です")
        # bind自体は別セッション(s2)で行われた体にする
        bound_human_entry(db, lid, entry_id=eid, session_id="s2", prompt_id="p1")
        db.commit()
        row = db.execute(
            "SELECT session_id FROM lesson_contradicted_ok WHERE lesson_id = ? AND entry_id = ?",
            (lid, eid),
        ).fetchone()
        assert row["session_id"] == "s2"


# ---------------------------------------------------------------------------
# 5. lesson_origin
# ---------------------------------------------------------------------------


class TestLessonOrigin:
    def test_no_origin_column_exists(self, db):
        """出自を書き込む列そのものが無い（Claudeが出自を申告できる経路が無い）。"""
        lesson_cols = {r["name"] for r in db.execute("PRAGMA table_info(lessons)").fetchall()}
        entry_cols = {r["name"] for r in db.execute("PRAGMA table_info(lesson_entries)").fetchall()}
        assert "origin" not in lesson_cols
        assert "origin" not in entry_cols

    def test_default_origin_is_ai(self, db):
        lid = lesson(db)
        db.commit()
        row = db.execute("SELECT origin FROM lesson_origin WHERE lesson_id = ?", (lid,)).fetchone()
        assert row["origin"] == "ai"

    def test_origin_is_human_only_via_creation_basis(self, db):
        lid = lesson(db)
        bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        db.commit()
        row = db.execute("SELECT origin FROM lesson_origin WHERE lesson_id = ?", (lid,)).fetchone()
        assert row["origin"] == "human"

    def test_entry_level_basis_does_not_change_origin(self, db):
        """追記（entry_idあり）の裏づけは出自を変えない。出自は作成時の裏づけだけで決まる。"""
        lid = lesson(db)
        eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
        bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        db.commit()
        row = db.execute("SELECT origin FROM lesson_origin WHERE lesson_id = ?", (lid,)).fetchone()
        assert row["origin"] == "ai"


# ---------------------------------------------------------------------------
# 6〜7. lesson_protected / lesson_current
# ---------------------------------------------------------------------------


class TestProtectedAndCurrent:
    def test_unprotected_lesson_all_entries_take_effect(self, db):
        lid = lesson(db, body="original body")
        entry(db, lid, "body", body="updated body")
        entry(db, lid, "conditions", deliver_event="tool_fail", deliver_spec="{}",
              step_event="tool_fail", step_spec="{}")
        entry(db, lid, "withdraw")
        db.commit()
        row = current_row(db, lid)
        assert row["body"] == "updated body"
        assert row["deliver_event"] == "tool_fail"
        assert row["retracted"] == 1

    def test_human_origin_lesson_is_protected(self, db):
        lid = lesson(db, body="original body")
        bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        db.commit()
        protected = db.execute(
            "SELECT 1 FROM lesson_protected WHERE lesson_id = ?", (lid,)
        ).fetchall()
        assert len(protected) == 1

    def test_ai_origin_with_valid_violated_is_protected(self, db):
        lid = lesson(db, body="original body")
        eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
        bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        db.commit()
        origin = db.execute("SELECT origin FROM lesson_origin WHERE lesson_id = ?", (lid,)).fetchone()
        assert origin["origin"] == "ai"
        protected = db.execute(
            "SELECT 1 FROM lesson_protected WHERE lesson_id = ?", (lid,)
        ).fetchall()
        assert len(protected) == 1

    def test_ai_origin_without_valid_violated_is_not_protected(self, db):
        lid = lesson(db, body="original body")
        # 裏づけの無いviolated（発話が無い、単なる自己申告）は守りを作らない
        entry(db, lid, "violated", quote="裏づけの無い引用")
        db.commit()
        protected = db.execute(
            "SELECT 1 FROM lesson_protected WHERE lesson_id = ?", (lid,)
        ).fetchall()
        assert protected == []

    @pytest.mark.parametrize("protect_via", ["human_origin", "ai_violated"])
    def test_protected_lesson_ignores_body_conditions_withdraw(self, db, protect_via):
        lid = lesson(db, body="original body", deliver_spec="{}")
        if protect_via == "human_origin":
            bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        else:
            eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
            bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        before = current_row(db, lid)

        entry(db, lid, "body", body="こっそり書き換えた本文")
        entry(db, lid, "conditions", deliver_event="tool_fail", deliver_spec="{}",
              step_event="tool_fail", step_spec="{}")
        entry(db, lid, "withdraw")
        db.commit()

        after = current_row(db, lid)
        assert after["body"] == before["body"] == "original body"
        assert after["deliver_event"] == before["deliver_event"]
        assert after["retracted"] == 0  # Claudeのwithdrawは守られた知見に効かない

    @pytest.mark.parametrize("protect_via", ["human_origin", "ai_violated"])
    def test_protected_lesson_note_still_takes_effect(self, db, protect_via):
        lid = lesson(db)
        if protect_via == "human_origin":
            bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        else:
            eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
            bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        entry(db, lid, "note", note="この知見について補足")
        db.commit()
        row = current_row(db, lid)
        assert row["note"] == "この知見について補足"


# ---------------------------------------------------------------------------
# 人間の1行の撤回
# ---------------------------------------------------------------------------


class TestHumanWithdraw:
    @pytest.mark.parametrize("scenario", ["human_origin", "ai_unprotected", "ai_protected"])
    def test_retracts_regardless_of_origin_and_protection(self, db, scenario):
        lid = lesson(db)
        if scenario == "human_origin":
            bound_human_entry(db, lid, entry_id=None, session_id="s1", prompt_id="p1")
        elif scenario == "ai_protected":
            eid = entry(db, lid, "violated", quote="これは逐語の引用文です")
            bound_human_entry(db, lid, entry_id=eid, session_id="s1", prompt_id="p1")
        # ai_unprotectedはbindを一切書かない（作成のみ）

        assert current_row(db, lid)["retracted"] == 0

        decl_uid = human_utterance(db, text="知見撤回 " + "h", session_id="s1", prompt_id="p9")
        human_withdraw(db, session_id="s1", lesson_id=lid, ref_id=decl_uid)
        db.commit()
        assert current_row(db, lid)["retracted"] == 1

    def test_declaration_by_non_human_does_not_retract(self, db):
        lid = lesson(db)
        uid = utterance(db, text="知見撤回 x", session_id="s1", prompt_id="p9")
        speaker(db, uid, session_id="s1", turn_origin="task_notification", prompt_source="system")
        human_withdraw(db, session_id="s1", lesson_id=lid, ref_id=uid)
        db.commit()
        assert current_row(db, lid)["retracted"] == 0


# ---------------------------------------------------------------------------
# 8〜10. lesson_bad_step / lesson_bad_step_prior / lesson_score
# ---------------------------------------------------------------------------


class TestLessonScore:
    def test_x_counts_distinct_sessions_with_stepped(self, db):
        lid = lesson(db)
        stepped(db, session_id="s1", lesson_id=lid, prompt_id="p1", tool_use_id="t1")
        stepped(db, session_id="s1", lesson_id=lid, prompt_id="p1", tool_use_id="t1b")
        stepped(db, session_id="s2", lesson_id=lid, prompt_id="p1", tool_use_id="t2")
        db.commit()
        row = score_row(db, lid)
        assert row["x"] == 2  # 同一セッション内の2回は1セッションとして数える

    def test_m_u_u_budget_u_same_split(self, db):
        lid = lesson(db)

        # セッションA: 読んでから踏んだ（M）
        delivered(db, session_id="A", lesson_id=lid, channel="prompt", prompt_id="pA0")
        s1 = stepped(db, session_id="A", lesson_id=lid, prompt_id="pA1", tool_use_id="tA1")
        eA, bA = bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="Aの訂正の内容です"),
                                    session_id="A", prompt_id="pA2")

        # セッションB: 届かずに踏んだ（U、budgetでもsameでもない）
        s2 = stepped(db, session_id="B", lesson_id=lid, prompt_id="pB1", tool_use_id="tB1")
        bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="Bの訂正の内容です"),
                           session_id="B", prompt_id="pB2")

        # セッションC: 上限で落ちて届かずに踏んだ（U_budget）
        suppressed(db, session_id="C", lesson_id=lid, channel="prompt", prompt_id="pC0")
        s3 = stepped(db, session_id="C", lesson_id=lid, prompt_id="pC1", tool_use_id="tC1")
        bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="Cの訂正の内容です"),
                           session_id="C", prompt_id="pC2")

        # セッションD: 同じ呼び出しでだけ届いた（U_same）
        delivered(db, session_id="D", lesson_id=lid, channel="post_tool", tool_use_id="tD1")
        s4 = stepped(db, session_id="D", lesson_id=lid, prompt_id="pD1", tool_use_id="tD1")
        bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="Dの訂正の内容です"),
                           session_id="D", prompt_id="pD2")

        db.commit()
        row = score_row(db, lid)
        assert row["b"] == 4
        assert row["m"] == 1
        assert row["u"] == 3
        assert row["u_budget"] == 1
        assert row["u_same"] == 1
        assert row["s"] == 4  # 4セッションとも有効なviolated（配達する知見なのでS）
        assert row["t"] == 0  # 配達する知見はTに入らない

    def test_contradicted_counts_and_retirement_cycle(self, db):
        lid = lesson(db)  # AI由来のまま（作成時の裏づけを付けない）

        # 支持1件（S=1）
        bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="支持1の訂正内容です"),
                           session_id="S1", prompt_id="p1")
        db.commit()
        row = score_row(db, lid)
        assert row["s"] == 1
        assert row["retired"] == 0

        # 間違い2件（W=C=2） > S(1) → 引っ込む
        for sid in ("W1", "W2"):
            delivered(db, session_id=sid, lesson_id=lid, channel="prompt", prompt_id=f"{sid}-0")
            bound_human_entry(db, lid, entry_id=entry(db, lid, "contradicted", quote=f"{sid}セッションの訂正内容"),
                               session_id=sid, prompt_id=f"{sid}-1")
        db.commit()
        row = score_row(db, lid)
        assert row["c"] == 2
        assert row["w"] == 2
        assert row["retired"] == 1

        # 支持がもう1件増え W(2) > S(2) が崩れる → 戻る
        bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="支持2の訂正内容です"),
                           session_id="S2", prompt_id="p1")
        db.commit()
        row = score_row(db, lid)
        assert row["s"] == 2
        assert row["retired"] == 0

    def test_human_origin_lesson_never_retires(self, db):
        lid = lesson(db)
        bound_human_entry(db, lid, entry_id=None, session_id="s0", prompt_id="p0")
        for sid in ("W1", "W2", "W3"):
            delivered(db, session_id=sid, lesson_id=lid, channel="prompt", prompt_id=f"{sid}-0")
            bound_human_entry(db, lid, entry_id=entry(db, lid, "contradicted", quote=f"{sid}セッションの訂正内容"),
                               session_id=sid, prompt_id=f"{sid}-1")
        db.commit()
        row = score_row(db, lid)
        assert row["c"] >= 2
        assert row["retired"] == 0  # 人間由来は同じ数字でも引っ込めない

    def test_tally_lesson_counts_as_t_not_s(self, db):
        lid = tally_lesson(db)
        bound_human_entry(db, lid, entry_id=entry(db, lid, "violated", quote="判断の癖の訂正内容"),
                           session_id="s1", prompt_id="p1")
        db.commit()
        row = score_row(db, lid)
        assert row["t"] == 1
        assert row["s"] == 0
        assert row["x"] == 0  # 計数型はstepped自体が書かれない


class TestNoTimeComparisonAndNoKindNames:
    """migrations/0076が実際に登録したビューの定義文（sqlite_master.sql）を検査する。

    コメント込みの生ファイルではなく、SQLiteが解釈した各CREATE VIEW文そのものを
    見ることで、コメントの言い回しに引きずられず判定条件の中身だけを検査する。
    """

    def _view_sql_texts(self, db):
        rows = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view'"
        ).fetchall()
        return {r["name"]: r["sql"] for r in rows}

    def test_views_have_no_time_comparison(self, db):
        views = self._view_sql_texts(db)
        for name, sql in views.items():
            for forbidden in ("datetime(", "julianday(", "date("):
                assert forbidden not in sql, f"{name} に {forbidden} が含まれている"

    def test_views_do_not_name_lesson_kinds(self, db):
        views = self._view_sql_texts(db)
        for name, sql in views.items():
            for kind_name in ("'prevent'", "'tally'", "'guide'"):
                assert kind_name not in sql, f"{name} に種類の名前 {kind_name} が現れている"

    def test_lesson_score_does_not_select_created_at(self, db):
        lid = lesson(db)
        db.commit()
        row = score_row(db, lid)
        assert "created_at" not in row.keys()


# ---------------------------------------------------------------------------
# 11. open_requests
# ---------------------------------------------------------------------------


class TestOpenRequests:
    def test_strong_word_opens_unconditionally(self, db):
        uid = human_utterance(db, text="前にも言ったよね", session_id="s1", prompt_id="p1", flag="strong")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert len(rows) == 1

    def test_weak_word_without_prior_delivery_does_not_open(self, db):
        human_utterance(db, session_id="s1", prompt_id="p0")
        uid = human_utterance(db, text="それは古い", session_id="s1", prompt_id="p1", flag="weak")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert rows == []

    def test_weak_word_opens_when_prior_human_turn_had_delivery(self, db):
        lid = lesson(db)
        human_utterance(db, session_id="s1", prompt_id="p0")
        delivered(db, session_id="s1", lesson_id=lid, channel="prompt", prompt_id="p0")
        uid = human_utterance(db, text="それは古い", session_id="s1", prompt_id="p1", flag="weak")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert len(rows) == 1

    def test_weak_word_not_opened_by_delivery_before_immediate_prior_turn(self, db):
        """直前ターン(p1)に配達が無ければ、より前(p0)の配達があってもopen_requestsに入らない。"""
        lid = lesson(db)
        human_utterance(db, session_id="s1", prompt_id="p0")
        delivered(db, session_id="s1", lesson_id=lid, channel="prompt", prompt_id="p0")
        human_utterance(db, session_id="s1", prompt_id="p1")
        uid = human_utterance(db, text="それは古い", session_id="s1", prompt_id="p2", flag="weak")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert rows == []

    def test_weak_word_not_opened_by_session_channel_delivery(self, db):
        """session口の配達はprompt_idを持たないので、直前ターンの配達として数えない。"""
        lid = lesson(db)
        human_utterance(db, session_id="s1", prompt_id="p0")
        delivered(db, session_id="s1", lesson_id=lid, channel="session", prompt_id=None)
        uid = human_utterance(db, text="それは古い", session_id="s1", prompt_id="p1", flag="weak")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert rows == []

    def test_request_without_prompt_id_never_opens(self, db):
        """u.prompt_id IS NOT NULL が無いと、prompt_idの無い発話が永久に開いたままになる。"""
        uid = utterance(db, text="前にも言ったよね", session_id="s1", prompt_id=None)
        speaker(db, uid, session_id="s1", turn_origin="human", prompt_source="typed")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert rows == []

    def test_closed_by_effective_bind_in_same_turn(self, db):
        lid = lesson(db)
        uid = human_utterance(db, text="前にも言ったよね", session_id="s1", prompt_id="p1", flag="strong")
        bind(db, session_id="s1", lesson_id=lid, prompt_id="p1", ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert rows == []

    def test_note_only_bind_does_not_close(self, db):
        lid = lesson(db)
        eid = entry(db, lid, "note", note="補足だけ")
        uid = human_utterance(db, text="前にも言ったよね", session_id="s1", prompt_id="p1", flag="strong")
        bind(db, session_id="s1", lesson_id=lid, prompt_id="p1", entry_id=eid, ref_id=uid)
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert len(rows) == 1  # noteだけでは閉じない

    @pytest.mark.parametrize(
        "reply_text,should_close",
        [
            ("知見にしない: その場限りだった", True),
            ("知見にしない：その場限りだった", True),
            ("前置き\n知見にしない: その場限りだった", True),
            ("前置き\n知見にしない：その場限りだった", True),
            ("途中に知見にしない: と書いただけ", False),
            ("知見にしないつもりだった", False),
        ],
    )
    def test_not_a_lesson_line_variants(self, db, reply_text, should_close):
        uid = human_utterance(db, text="前にも言ったよね", session_id="s1", prompt_id="p1", flag="strong")
        reply(db, text=reply_text, session_id="s1", prompt_id="p1")
        db.commit()
        rows = db.execute("SELECT 1 FROM open_requests WHERE ref_id = ?", (uid,)).fetchall()
        assert (rows == []) is should_close


# ---------------------------------------------------------------------------
# migrationがDBの写しに適用できることの確認（実データ想定）
# ---------------------------------------------------------------------------


class TestMigrationApplies:
    def test_views_created_after_migration(self, db):
        expected = {
            "utterance_human", "lesson_basis", "lesson_violated_ok", "lesson_contradicted_ok",
            "lesson_origin", "lesson_protected", "lesson_current", "lesson_bad_step",
            "lesson_bad_step_prior", "lesson_score", "open_requests",
        }
        rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()
        names = {r["name"] for r in rows}
        assert expected <= names
