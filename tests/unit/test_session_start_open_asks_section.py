"""hooks/session_start_hook.py の open_asks セクション（_build_open_asks_section）
専用のユニットテスト。

open askと回答済み未捌き（status='answered' AND triage未了）askをkind別
（メタ/非メタ）にタイトル表示するセクションの振る舞いを検証する。E2E経由の
subprocess呼び出し（tests/e2e/test_session_start_hook.py）ではなく、関数を
直接importしテスト用DBのconnをそのまま渡す（他の_build_*_sectionユニット
テストと同じ方式）。
"""
from src import config
from src.db import get_connection
from src.services import ask_service
from src.services.topic_service import add_topic

from hooks.session_start_hook import (
    _OPEN_ASKS_GLOBAL_CTA,
    _OPEN_ASKS_NON_META_DISPLAY_LIMIT,
    _build_open_asks_section,
    _section_text_len,
)


def _seed_activity(conn, title: str, status: str = "pending") -> int:
    cursor = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "desc", status),
    )
    return cursor.lastrowid


def _seed_ask(conn, question: str, *, kind: str = "ask") -> int:
    """askを1件作成しopen状態のまま返す。専用のpending activityを1件作って
    blocks先として紐付ける（add_ask_with_connはblocksが空だとエラーになる）。

    add_ask（embedding生成・近傍検索を伴うMCPツール本体）ではなく
    add_ask_with_connを直接呼ぶことで、embeddingサーバー起動を伴わずに
    conn共有のまま同一トランザクションでcommitする。
    """
    blocking_activity_id = _seed_activity(conn, f"[作業] {question}のblocks先")
    result = ask_service.add_ask_with_conn(
        conn, question, [blocking_activity_id], ["domain:open-asks-test"], kind=kind
    )
    assert "error" not in result, result
    conn.commit()
    return result["id"]


def _answer_ask(conn, ask_id: int, answer_body: str = "回答済み") -> None:
    result = ask_service.answer_ask_with_conn(conn, ask_id, answer_body)
    assert "error" not in result, result
    conn.commit()


class TestEmptyState:
    def test_no_asks_returns_empty(self, temp_db):
        """open askも回答済み未捌きaskも0件のとき、セクションは空文字を返す
        （コンテキスト消費ゼロ）"""
        conn = get_connection()
        try:
            assert _build_open_asks_section(conn) == ""
        finally:
            conn.close()


class TestNonMetaTitleDisplay:
    def test_open_ask_shows_question_title(self, temp_db):
        """非メタのopen askは件数のみでなくタイトル(question)が表示される"""
        conn = get_connection()
        try:
            _seed_ask(conn, "明日の作業はどうする?")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "明日の作業はどうする?" in result
        assert "open ask" in result

    def test_answered_pending_triage_ask_shows_question_title(self, temp_db):
        """非メタの回答済み未捌きaskも件数のみでなくタイトル(question)が
        表示される"""
        conn = get_connection()
        try:
            ask_id = _seed_ask(conn, "昨日決めた件、進めていい?")
            _answer_ask(conn, ask_id, "進めてOK")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "昨日決めた件、進めていい?" in result
        assert "回答済み未捌き" in result

    def test_promoted_ask_not_shown_as_pending(self, temp_db):
        """triage_ask(action="promote")でpromote済み（status='promoted'、
        triage='promote'）になったaskは回答済み未捌きバケットに含まれない"""
        conn = get_connection()
        try:
            ask_id = _seed_ask(conn, "promoteされる質問")
            _answer_ask(conn, ask_id, "進めてOK")
        finally:
            conn.close()

        topic_id = add_topic(
            title="triageテスト用トピック", description="d", tags=["domain:open-asks-test"]
        )["topic_id"]
        result = ask_service.triage_ask(
            ask_id, action="promote", decision="d", reason="r", topic_id=topic_id
        )
        assert "error" not in result, result

        conn = get_connection()
        try:
            out = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "promoteされる質問" not in out

    def test_dismissed_ask_not_shown_as_pending(self, temp_db):
        """triage_ask(action="dismiss")でdismiss済み（status='dismissed'、
        triage='dismiss'）になったaskは回答済み未捌きバケットに含まれない"""
        conn = get_connection()
        try:
            ask_id = _seed_ask(conn, "dismissされる質問")
            _answer_ask(conn, ask_id, "回答内容")
        finally:
            conn.close()

        result = ask_service.triage_ask(ask_id, action="dismiss", dismiss_reason="不要と判断")
        assert "error" not in result, result

        conn = get_connection()
        try:
            out = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "dismissされる質問" not in out

    def test_withdrawn_ask_not_shown_as_pending(self, temp_db):
        """withdrawされたaskは（status='open'でもstatus='answered'でもないため）
        回答済み未捌きバケットに含まれない"""
        conn = get_connection()
        try:
            ask_id = _seed_ask(conn, "withdrawされる質問")
            result = ask_service.withdraw_ask_with_conn(conn, ask_id, "不要になった")
            assert "error" not in result, result
            conn.commit()
            out = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "withdrawされる質問" not in out


class TestMetaAlwaysVisible:
    def test_meta_ask_shown_regardless_of_non_meta_display_limit(self, temp_db):
        """kind='meta'のaskは、非メタの表示上限件数に関わらず必ず表示される
        （非メタの上限とは独立）。

        meta askを最初に作成し、その後表示上限件数と同数の非メタaskを新規に
        作成することで、last_seen_at降順の素朴な上位N件クエリでは
        meta askが押し出される状況を作る。それでもmeta askが表示される
        ことを確認する。
        """
        conn = get_connection()
        try:
            _seed_ask(conn, "meta: 同型判断が繰り返されている", kind="meta")
            for i in range(_OPEN_ASKS_NON_META_DISPLAY_LIMIT):
                _seed_ask(conn, f"非メタ質問{i}")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "meta: 同型判断が繰り返されている" in result
        for i in range(_OPEN_ASKS_NON_META_DISPLAY_LIMIT):
            assert f"非メタ質問{i}" in result

    def test_meta_ask_shown_even_when_blocking_activity_completed(self, temp_db):
        """kind='meta'のaskは、blocks先activityの状態(completed等)に関わらず
        表示される（ask_service.get_asks_with_connがblocksでフィルタしないため、
        blocks状態独立性は追加実装なしで自動的に満たされる）。"""
        conn = get_connection()
        try:
            act_id = _seed_activity(conn, "[作業] meta askのblocks先")
            conn.commit()
            result = ask_service.add_ask_with_conn(
                conn,
                "meta: blocks先が完了済みでも表示されるか",
                [act_id],
                ["domain:open-asks-test"],
                kind="meta",
            )
            assert "error" not in result, result
            conn.commit()
            conn.execute(
                "UPDATE activities SET status = 'completed' WHERE id = ?", (act_id,)
            )
            conn.commit()
            out = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "meta: blocks先が完了済みでも表示されるか" in out


class TestNonMetaOverflow:
    def test_non_meta_overflow_shows_remainder_count(self, temp_db):
        """非メタのaskが表示上限件数を超える場合、上位N件は表示しつつ
        「他M件」で残りを省略する"""
        conn = get_connection()
        try:
            for i in range(_OPEN_ASKS_NON_META_DISPLAY_LIMIT + 2):
                _seed_ask(conn, f"open質問{i}")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "他2件" in result

    def test_non_meta_within_limit_shows_no_remainder(self, temp_db):
        """非メタのaskが表示上限件数以内のときは「他N件」を出さない"""
        conn = get_connection()
        try:
            for i in range(_OPEN_ASKS_NON_META_DISPLAY_LIMIT):
                _seed_ask(conn, f"open質問{i}")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "他" not in result


class TestMetaDoesNotStealNonMetaSlots:
    def test_meta_asks_do_not_reduce_non_meta_display_count(self, temp_db):
        """kind='meta'のaskが複数あっても、非メタの表示件数は
        _OPEN_ASKS_NON_META_DISPLAY_LIMIT分そのまま表示される
        （非メタ側の取得がkind="ask"で絞られており、上位N件取得にmetaが
        混在しないため）。meta3件+非メタ6件（表示上限5件）で、表示上限を
        超えた分の残り件数が「他1件」になることを確認する
        （kindで絞らない場合は非メタ2件のみ表示・「他4件」になってしまう）"""
        conn = get_connection()
        try:
            for i in range(3):
                _seed_ask(conn, f"meta質問{i}", kind="meta")
            for i in range(_OPEN_ASKS_NON_META_DISPLAY_LIMIT + 1):
                _seed_ask(conn, f"非メタ質問{i}")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        shown_count = sum(
            1 for i in range(_OPEN_ASKS_NON_META_DISPLAY_LIMIT + 1) if f"非メタ質問{i}" in result
        )
        assert shown_count == _OPEN_ASKS_NON_META_DISPLAY_LIMIT
        assert "他1件" in result
        assert "他4件" not in result


class TestRenderedFormat:
    def test_meta_line_has_marker_and_id_format(self, temp_db):
        """metaの行は`[meta]`マーカーと`(#{id})`形式のID表記を持つ"""
        conn = get_connection()
        try:
            ask_id = _seed_ask(conn, "meta: フォーマット確認", kind="meta")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert f"- [meta] (#{ask_id}) meta: フォーマット確認" in result

    def test_non_meta_line_has_id_format_without_meta_marker(self, temp_db):
        """非メタの行は`[meta]`マーカーを持たず、`(#{id})`形式のID表記のみ"""
        conn = get_connection()
        try:
            ask_id = _seed_ask(conn, "非メタのID表記確認")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert f"- (#{ask_id}) 非メタのID表記確認" in result
        assert "[meta]" not in result

    def test_meta_cta_present_when_meta_exists(self, temp_db):
        """metaが1件以上あるバケットにはメタ向けCTAが付く"""
        conn = get_connection()
        try:
            _seed_ask(conn, "meta: CTA確認", kind="meta")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "→ メタaskはrule-placement skillでの配置検討が必要" in result

    def test_meta_cta_absent_when_no_meta(self, temp_db):
        """非メタのみのバケットにはメタ向けCTAが付かない"""
        conn = get_connection()
        try:
            _seed_ask(conn, "非メタのみ")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "→ メタaskはrule-placement skillでの配置検討が必要" not in result

    def test_global_cta_present(self, temp_db):
        """セクションに何か1件でも表示があれば、全体CTAが末尾に付く"""
        conn = get_connection()
        try:
            _seed_ask(conn, "CTA確認用")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert "→ ask-answer skillまたはget_asksで確認" in result

    def test_open_bucket_appears_before_pending_bucket(self, temp_db):
        """open askバケットの見出しが回答済み未捌きバケットの見出しより
        前に出現する（この順序で表示する仕様）"""
        conn = get_connection()
        try:
            _seed_ask(conn, "open状態の質問")
            pending_ask_id = _seed_ask(conn, "pending状態の質問")
            _answer_ask(conn, pending_ask_id, "回答済み")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert result.index("## open ask") < result.index("## 回答済み未捌き")


class TestBudgetTruncation:
    def test_open_asks_section_fits_budget_and_preserves_meta_and_cta(self, temp_db):
        """openの非メタask5件（各約400字、ask_service.QUESTION_MAX_LEN=500の
        範囲内）+ 回答済み未捌きのmeta ask1件、という構成でセクション全体が
        config.INJECTION_BUDGET_OPEN_ASKS_CHARS以内に収まり、かつ回答済み
        未捌きバケットのmeta ask・見出し・CTAが失われないことを確認する。

        件数のみの上限（_OPEN_ASKS_NON_META_DISPLAY_LIMIT）では質問文の
        可変長（最大500字）に対して文字数予算を守れず、
        injection_compositor._hard_truncateが末尾（2つ目のバケットの
        meta ask・見出し・CTA）を無言で切り詰めてしまう回帰を検証する。
        アサーションは_build_open_asks_section自身の出力に対して行う
        （compose()を経由せず、この関数自身がbudget_chars以内に収める
        設計であることを確認する）。
        """
        conn = get_connection()
        try:
            for i in range(5):
                _seed_ask(conn, f"open質問{i}: " + "あ" * 390)
            pending_meta_id = _seed_ask(conn, "meta: 回答済み未捌きのメタ", kind="meta")
            _answer_ask(conn, pending_meta_id, "回答内容")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        assert len(result) <= config.INJECTION_BUDGET_OPEN_ASKS_CHARS
        assert "meta: 回答済み未捌きのメタ" in result
        assert "## 回答済み未捌き" in result
        assert "→ メタaskはrule-placement skillでの配置検討が必要" in result
        assert "→ ask-answer skillまたはget_asksで確認" in result


class TestBudgetPreservesRequiredElements:
    """2回目レビューで指摘されたCritical-1再発（メタask2〜3件存在時の
    メタ常時表示不変条件違反）およびMajor3件（全体CTA消失・残り件数行の
    無言欠落・見出しだけのバケット）の回帰テスト。

    いずれも根は同じで、旧実装は見出し・meta行・両CTAを無条件追記し
    非メタ側の行だけを1行ずつ予算検査していたため、無条件追記側の合計が
    budget_charsを超えるとinjection_compositor._hard_truncateが末尾を
    無言で切り詰めていた。新実装は見出し・meta行・meta向けCTA・全体CTAを
    「必須要素」として先に確定し、非メタ・残り件数行だけをその残り予算に
    収める設計に変えている。
    """

    def test_meta_survives_when_combined_with_non_meta_pushes_past_naive_budget(self, temp_db):
        """openの非メタ3件（各約350字）＋回答済み未捌きのmeta2件（各約110字）
        という構成。旧実装ではこれらを無条件に連結した生テキストが1200字を
        超え、compose()のハード切り詰めでpending側のmeta 1件・全体CTAが
        無言で消える（session_start_hook.pyの旧_render_open_asks_sectionを
        使って別途確認済み）。新実装では_build_open_asks_section自身の
        出力がbudget_chars以内に収まり、両方のmeta・見出し・両CTAが
        揃って表示されることを確認する。"""
        conn = get_connection()
        try:
            for i in range(3):
                _seed_ask(conn, f"open質問{i}: " + "あ" * 340)
            meta_a_id = _seed_ask(conn, "meta_a: " + "あ" * 100, kind="meta")
            meta_b_id = _seed_ask(conn, "meta_b: " + "あ" * 100, kind="meta")
            _answer_ask(conn, meta_a_id, "回答a")
            _answer_ask(conn, meta_b_id, "回答b")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        assert len(result) <= config.INJECTION_BUDGET_OPEN_ASKS_CHARS
        assert "meta_a" in result
        assert "meta_b" in result
        assert "## open ask" in result
        assert "## 回答済み未捌き" in result
        assert "→ メタaskはrule-placement skillでの配置検討が必要" in result
        assert "→ ask-answer skillまたはget_asksで確認" in result

    def test_global_cta_survives_when_non_meta_total_length_barely_exceeds_budget(self, temp_db):
        """メタ0件・非メタ5件（各約220字）という構成。旧実装ではこの5件を
        連結した生テキストが全体CTA（改行込み約33字）の分だけbudget_charsを
        わずかに超え、compose()のハード切り詰めで全体CTAが無言で消える
        （前回レビューのMajor再現条件）。新実装では_build_open_asks_section
        自身の出力がbudget_chars以内に収まり、全体CTAが失われないことを
        確認する。"""
        conn = get_connection()
        try:
            for i in range(5):
                _seed_ask(conn, f"q{i}: " + "あ" * 220)
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        assert len(result) <= config.INJECTION_BUDGET_OPEN_ASKS_CHARS
        assert "→ ask-answer skillまたはget_asksで確認" in result

    def test_remainder_line_reflects_actually_hidden_non_meta_count(self, temp_db):
        """openの非メタ5件（各約400字）＋回答済み未捌きのmeta1件という構成。
        非メタが表示上限内に収まらず一部のみ表示される場合でも、実際に
        表示できなかった件数どおりの残り件数行（「他N件」）が出ることを
        確認する（前回レビューのMajor再現条件。旧実装では非メタ本文の
        1行目が予算に収まらなかった時点でhas_budgetの一方向ラッチにより
        残り件数行も無条件で捨てられていた）。"""
        conn = get_connection()
        try:
            for i in range(5):
                _seed_ask(conn, f"open質問{i}: " + "あ" * 390)
            pending_meta_id = _seed_ask(conn, "meta: 回答済み未捌きのメタ", kind="meta")
            _answer_ask(conn, pending_meta_id, "回答")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        shown = sum(1 for i in range(5) if f"open質問{i}" in result)
        hidden = 5 - shown
        assert len(result) <= config.INJECTION_BUDGET_OPEN_ASKS_CHARS
        assert hidden > 0, "この構成は非メタが一部欠落する想定だが全件表示された"
        assert f"他{hidden}件" in result

    def test_pending_bucket_not_left_as_dangling_heading(self, temp_db):
        """openの非メタ5件（各約400字）＋回答済み未捌きの非メタ1件（短い）
        という構成。1つ目のバケット（open）で残り予算をほぼ使い切っても、
        2つ目のバケット（回答済み未捌き）の短い非メタ行が表示されることを
        確認する（前回レビューのMajor再現条件。旧実装のhas_budget
        一方向ラッチは「文字数は単調増加」という誤った前提に立ち、
        bucket1の非メタが1つ収まらなかった時点でbucket2の非メタ行も
        試行せず捨てていた）。"""
        conn = get_connection()
        try:
            for i in range(5):
                _seed_ask(conn, f"open質問{i}: " + "あ" * 390)
            pending_id = _seed_ask(conn, "PENDING_NONMETA_SENTINEL")
            _answer_ask(conn, pending_id, "回答")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        has_heading = "## 回答済み未捌き" in result
        has_body = "PENDING_NONMETA_SENTINEL" in result
        assert has_heading
        assert has_body, "見出しだけで中身が0行のバケットになっている"

    def test_pending_remainder_line_survives_when_open_bucket_leaves_little_budget(self, temp_db):
        """openの非メタ5件（各273字）＋回答済み未捌きの非メタ1件（460字超で
        確実に表示できない長さ）という構成（3回目レビューv3で186通り中2通り
        再現した「見出しだけのバケット」をids 1-6の決定論的な構成に写した
        もの）。

        旧実装は残り件数行の予約をバケット単位で行っていた（そのバケットに
        入ってから予約する）ため、openバケット自身の残り件数行を出した
        あとに残るavailableが、回答済み未捌きバケット自身の予約コストにも
        届かないことがあった。この場合予約が-の値になるだけで予約自体が
        不成立となり、非メタ・残り件数行のどちらも入らず見出しだけが残った
        （読み手には0件と読めるが実際には1件ある）。

        修正後は全バケット分の残り件数行の最悪コストを非メタ走査の前に
        まとめて予約するため、openバケットの走査が回答済み未捌きバケット
        自身の予約コストまで侵食できず、残り件数行（「他1件」）が表示される。"""
        conn = get_connection()
        try:
            for i in range(5):
                _seed_ask(conn, f"o{i}: " + "あ" * 273)
            pending_id = _seed_ask(conn, "PENDING_LONG_SENTINEL: " + "い" * 460)
            _answer_ask(conn, pending_id, "回答")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        assert len(result) <= config.INJECTION_BUDGET_OPEN_ASKS_CHARS
        assert "## 回答済み未捌き" in result
        tail = result[result.index("## 回答済み未捌き"):]
        body_lines = [
            ln for ln in tail.splitlines()[1:]
            if ln and ln != _OPEN_ASKS_GLOBAL_CTA
        ]
        assert body_lines, "見出しだけで中身が0行のバケットになっている（残り件数行も出ていない）"
        assert "他1件" in result

    def test_reservation_keeps_remainder_line_from_being_silently_dropped(self, temp_db):
        """openの非メタ6件（各219字）のみ（回答済み未捌きは0件）という構成。
        非メタの表示上限（_OPEN_ASKS_NON_META_DISPLAY_LIMIT=5）により候補は
        常に上位5件までしかDBから取得されないが、この5件だけで残り予算を
        使い切れる長さを選んでいる。

        残り件数行の予約を行わない実装は、この5件の走査で残り予算を
        ぎりぎりまで使い切り、非表示が2件（6件中4件しか表示できない）
        あるにもかかわらず残り件数行自体が入らなくなる（無言の欠落）。
        予約ありの現行実装は最後の非メタ行の代わりに残り件数行
        （「他N件」）を出すことで、非表示が発生していることを常に読み手に
        伝える。本テストは残り件数行の予約ロジックそのものを無効化する
        変更（reservedを常に0にする等）を検知する（mutation testで既存
        24件が1件も検知できなかったギャップの回帰テスト）。"""
        conn = get_connection()
        try:
            for i in range(6):
                _seed_ask(conn, f"o{i}: " + "あ" * 219)
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        shown = sum(1 for i in range(6) if f"o{i}: " in result)
        hidden = 6 - shown
        assert len(result) <= config.INJECTION_BUDGET_OPEN_ASKS_CHARS
        assert hidden > 0, "この構成は一部が非表示になる想定だが全件表示された"
        assert f"他{hidden}件" in result, (
            "非表示件数があるのに残り件数行が出ていない"
            "(予約が無効化され残り予算を使い切った可能性がある)"
        )

    def test_build_open_asks_section_itself_never_drops_meta_even_when_result_exceeds_budget(self, temp_db):
        """openにmeta3件（各約400字）＋回答済み未捌きにmeta1件という、
        meta本文だけで必須要素の合計がconfig.INJECTION_BUDGET_OPEN_ASKS_CHARS
        （1200字）を超える構成（前回レビューのCritical再現条件そのもの。
        3件×400字+1件で約1400字になり、非メタが0件でも必須要素だけで
        budget_charsを超える）。

        本テストが検証するのは_build_open_asks_section（レンダラ）自身の
        振る舞いに限られる。compose()は経由しない。meta常時表示の不変条件に
        より、_build_open_asks_section自身はこの場合でも全てのmeta本文・
        見出し・両CTAを欠落させずに構築し、返り値そのものがbudget_charsを
        超えることを許容する（必須要素だけで予算を超える極端な構成では、
        非メタ・残り件数行にゼロを割り当てても解決できないため）。

        ただしこの「メタは失われない」保証はレンダラの出力段までであり、
        後段のcompose()（injection_compositor._hard_truncate）がこの
        budget_chars超過分を末尾から機械的に切り詰めるため、実際にユーザー
        へ注入される最終テキストではmeta本文が失われることがある
        （設計上受容されている残余リスク。compose()を経由した場合の実際の
        挙動はこのテストでは検証しない）。"""
        conn = get_connection()
        try:
            for i in range(3):
                _seed_ask(conn, f"meta{i}: " + "あ" * 390, kind="meta")
            pending_meta_id = _seed_ask(conn, "PENDING_META_SENTINEL", kind="meta")
            _answer_ask(conn, pending_meta_id, "回答")
            result = _build_open_asks_section(conn)
        finally:
            conn.close()

        assert "meta0: " in result
        assert "meta1: " in result
        assert "meta2: " in result
        assert "PENDING_META_SENTINEL" in result
        assert "## 回答済み未捌き" in result
        assert result.count("→ メタaskはrule-placement skillでの配置検討が必要") == 2
        assert "→ ask-answer skillまたはget_asksで確認" in result
        # 必須要素だけでbudget_charsを超える極端な構成のため、
        # _render_open_asks_section自身の返り値がbudget_charsを超えることを
        # 明示的に許容する（受容している残余リスクの境界を固定するテスト）。
        assert len(result) > config.INJECTION_BUDGET_OPEN_ASKS_CHARS


class TestSectionTextLenContract:
    """_section_text_lenのdocstringが主張する契約（"\\n".join(lines) + "\\n"
    と連結した場合の文字数と一致する）を直接固定する。この関数はbudget_chars
    との差分（available）の起点であり、1字のずれが_hard_truncateの発火判定を
    左右する（mutation testで_section_text_lenを1字過小に変更しても既存24件が
    1件も検知できなかったギャップの回帰テスト）。"""

    def test_matches_actual_joined_length(self):
        lines = ["ab", "cde", "f"]
        assert _section_text_len(lines) == len("\n".join(lines) + "\n")

    def test_empty_lines_is_zero(self):
        assert _section_text_len([]) == 0


class TestErrorFallback:
    def test_ask_service_error_yields_empty_section(self, temp_db, monkeypatch):
        """ask_service.get_asks_with_connが失敗（{"error": ...}）を返した場合、
        例外を投げず空文字にフォールバックする（section単位try/exceptに
        依存しない、本セクション自身の防御）"""
        conn = get_connection()
        try:
            _seed_ask(conn, "エラー時にも落ちないことを確認する質問")

            def boom(*args, **kwargs):
                return {"error": {"code": "DATABASE_ERROR", "message": "boom"}}

            monkeypatch.setattr(ask_service, "get_asks_with_conn", boom)
            result = _build_open_asks_section(conn)
        finally:
            conn.close()
        assert result == ""
