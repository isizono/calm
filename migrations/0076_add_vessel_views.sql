-- Migration 0076: 自己改善ループの器 — 知見の判定ビュー
--
-- depends: 0075_add_vessel_tables
--
-- 背景:
--   0075で作った観測台帳（obs_events・lessons・lesson_entries）だけでは、
--   「この知見は人間の裏づけがあるか」「今の本文・条件は何か」「配達すべきか」
--   を判定できない。これらはすべて追記専用の生ログから毎回計算し直す値であり、
--   状態として持たない。11本のビューがその計算を担う。
--
-- 変更内容:
--   utterance_human → lesson_basis → lesson_violated_ok/lesson_contradicted_ok
--   → lesson_origin → lesson_protected → lesson_current → lesson_bad_step
--   → lesson_bad_step_prior → lesson_score → open_requests の順で、依存関係の
--   とおりに11本のビューを作る（後続が参照するビューを必ず先に作る）。
--
-- 不変条件:
--   - 「人間の打鍵か」の判定は utterance_human 1本にだけ書く。他のビューは
--     必ずこのビューを経由して人間の発話を参照し、turnOrigin/promptSourceの
--     照合を書き足さない。
--   - lesson_score に時刻の比較を一切置かない（created_atを選ばず、
--     datetime()/julianday()/date()を使わない）。数はすべて
--     COUNT(DISTINCT session_id)で数える。
--   - 種類の名前（'prevent'/'tally'/'guide'）はビューの条件式に書かない。
--     lesson_kinds.delivers/stepsの値でだけ場合分けする。

-- ============================================================
-- 1. utterance_human: 人間の打鍵と判定する発話（唯一の判定箇所）
-- ============================================================
-- uq_obs_speaker（obs_events(ref_id) WHERE kind='speaker' の部分一意索引）が
-- 発話ごとに speaker 行を高々1件に保証するので、この JOIN で行が増えることはない。
-- created_atは選ばない（下流はすべてidで前後を判定する）。値が欠けていれば
-- json_extractがNULLを返し、比較はすべて偽になるので許可リスト方式が自然に成立する。
CREATE VIEW utterance_human AS
SELECT u.id, u.session_id, u.prompt_id, u.kind, u.flag, u.text
FROM obs_events u
JOIN obs_events sp
  ON sp.kind = 'speaker' AND sp.ref_id = u.id
WHERE u.kind = 'utterance'
  AND u.agent_id IS NULL
  AND json_extract(sp.text, '$.turnOrigin') = 'human'
  AND json_extract(sp.text, '$.promptSource') IN ('typed', 'queued', 'sdk');

-- ============================================================
-- 2. lesson_basis: 知見・追記ごとの人間の裏づけ
-- ============================================================
-- 列は (lesson_id, entry_id, session_id, evidence_id, void) の5つで固定する。
-- 候補が枝を足す場所は末尾のUNION ALLで、その枝はvoid=1を使ってよい
-- （核の枝はvoid=0固定）。
CREATE VIEW lesson_basis AS
SELECT b.lesson_id   AS lesson_id,
       b.entry_id    AS entry_id,
       b.session_id  AS session_id,
       u.id          AS evidence_id,
       0             AS void
FROM obs_events b
JOIN utterance_human u
  ON u.id = b.ref_id AND u.session_id = b.session_id
WHERE b.kind = 'bind'
  AND b.agent_id IS NULL
  AND b.prompt_id IS NOT NULL
  AND u.id < b.id
  -- 書き込みのターン自身が人間のターンであること: 同じ session_id・prompt_id の
  -- 発話が1件以上あり、そのすべてが utterance_human に入る。これを満たさない
  -- 限り、割り込み発話の扱いをどう変えてもここだけを直せばよいように、
  -- 独立した副問い合わせのまま保つ（緩める場合もこの2本のEXISTSの置き換えで済む）。
  AND EXISTS (
        SELECT 1 FROM obs_events t
        WHERE t.kind = 'utterance' AND t.agent_id IS NULL
          AND t.session_id = b.session_id AND t.prompt_id = b.prompt_id)
  AND NOT EXISTS (
        SELECT 1 FROM obs_events t
        WHERE t.kind = 'utterance' AND t.agent_id IS NULL
          AND t.session_id = b.session_id AND t.prompt_id = b.prompt_id
          AND t.id NOT IN (SELECT id FROM utterance_human))
  -- U と B の間に、B と違うターンの発話で「人間」または「まだ speaker が無い」
  -- ものが無いこと（U は今のターンか、直前の人間のターンの発話でなければならない）。
  -- v.prompt_id IS NULL OR v.prompt_id <> b.prompt_id は明示的に書く。素の <> は
  -- NULL側でNULLを返し、prompt_idの無い発話を黙って落としてしまう。
  AND NOT EXISTS (
        SELECT 1 FROM obs_events v
        WHERE v.kind = 'utterance' AND v.agent_id IS NULL
          AND v.session_id = b.session_id
          AND v.id > u.id AND v.id < b.id
          AND (v.prompt_id IS NULL OR v.prompt_id <> b.prompt_id)
          AND ( v.id IN (SELECT id FROM utterance_human)
                OR NOT EXISTS (SELECT 1 FROM obs_events s2
                               WHERE s2.kind = 'speaker' AND s2.ref_id = v.id) ));

-- ============================================================
-- 3. lesson_violated_ok: 人間の裏づけがある有効な violated 追記
-- ============================================================
CREATE VIEW lesson_violated_ok AS
SELECT e.lesson_id, e.id AS entry_id, lb.session_id, lb.evidence_id
FROM lesson_entries e
JOIN lesson_basis lb
  ON lb.entry_id = e.id AND lb.lesson_id = e.lesson_id AND lb.void = 0
WHERE e.kind = 'violated';

-- ============================================================
-- 4. lesson_contradicted_ok: 人間の裏づけ + 根拠の発話より前に配達がある
--    有効な contradicted 追記
-- ============================================================
-- 追記のセッションは、その追記のbind行のセッション（lb.session_id）を使う。
-- lesson_entriesにはセッション列が無いので必ずこれを使い、e.created_atは使わない。
CREATE VIEW lesson_contradicted_ok AS
SELECT e.lesson_id, e.id AS entry_id, lb.session_id, lb.evidence_id
FROM lesson_entries e
JOIN lesson_basis lb
  ON lb.entry_id = e.id AND lb.lesson_id = e.lesson_id AND lb.void = 0
WHERE e.kind = 'contradicted'
  AND EXISTS (SELECT 1 FROM obs_events d
              WHERE d.kind = 'delivered' AND d.lesson_id = e.lesson_id
                AND d.session_id = lb.session_id AND d.id < lb.evidence_id);

-- ============================================================
-- 5. lesson_origin: 知見ごとの出自（列としては存在しない計算値）
-- ============================================================
-- 知見の作成そのもの（entry_id IS NULL）に人間の裏づけがあれば人間由来、
-- 無ければAI由来。追記の裏づけの有無はここでは見ない（出自は知見の作成が
-- 人間の発話に結びついたかだけで決まる）。
CREATE VIEW lesson_origin AS
SELECT l.id AS lesson_id,
       CASE WHEN EXISTS (SELECT 1 FROM lesson_basis lb
                         WHERE lb.lesson_id = l.id AND lb.entry_id IS NULL AND lb.void = 0)
            THEN 'human' ELSE 'ai' END AS origin
FROM lessons l;

-- ============================================================
-- 6. lesson_protected: 守られた知見（追記の効き目だけに使う）
-- ============================================================
-- 人間由来の知見、またはAI由来でも有効なviolatedが1件以上ある知見。
-- 出自(lesson_origin)・配達の並び順・採点の引っ込みには配線しない
-- （守られたAI由来の知見も出自はaiのまま、採点でも引っ込む対象になる）。
CREATE VIEW lesson_protected AS
SELECT l.id AS lesson_id
FROM lessons l
WHERE EXISTS (SELECT 1 FROM lesson_origin o
              WHERE o.lesson_id = l.id AND o.origin = 'human')
   OR EXISTS (SELECT 1 FROM lesson_violated_ok v WHERE v.lesson_id = l.id);

-- ============================================================
-- 7. lesson_current: 現在の本文・条件・最新の補足・撤回の有無
-- ============================================================
-- CASE WHEN ... ELSE ... を使い、COALESCEは使わない（conditions追記が列を
-- NULLにした場合にlessonsの古い値が黙って復活する依存を作らないため）。
-- noteにはlesson_protectedの判定を付けない（noteは出自も守りも問わず効く）。
-- 撤回は2経路（守られていないときだけ効くClaudeのwithdraw追記／出自も守りも
-- 問わない人間のhuman_withdraw行）を1つの列にまとめて表す。id DESCではなく
-- MAX(x.id)を使い、created_atは判定に使わない。状態を持たないビューなので、
-- 後からbindやspeakerが書かれれば遡って値が変わるのが自然に成立する。
CREATE VIEW lesson_current AS
SELECT
  l.id     AS lesson_id,
  l.handle AS handle,
  l.kind   AS kind,
  CASE WHEN be.id IS NOT NULL THEN be.body          ELSE l.body          END AS body,
  CASE WHEN ce.id IS NOT NULL THEN ce.deliver_event ELSE l.deliver_event END AS deliver_event,
  CASE WHEN ce.id IS NOT NULL THEN ce.deliver_spec  ELSE l.deliver_spec  END AS deliver_spec,
  CASE WHEN ce.id IS NOT NULL THEN ce.step_event    ELSE l.step_event    END AS step_event,
  CASE WHEN ce.id IS NOT NULL THEN ce.step_spec     ELSE l.step_spec     END AS step_spec,
  l.quote  AS quote,
  ne.note  AS note,
  CASE WHEN we.id IS NOT NULL OR hw.id IS NOT NULL THEN 1 ELSE 0 END AS retracted
FROM lessons l
LEFT JOIN lesson_protected p ON p.lesson_id = l.id
LEFT JOIN lesson_entries be ON be.id = (
    SELECT MAX(x.id) FROM lesson_entries x
    WHERE x.lesson_id = l.id AND x.kind = 'body'     AND p.lesson_id IS NULL)
LEFT JOIN lesson_entries ce ON ce.id = (
    SELECT MAX(x.id) FROM lesson_entries x
    WHERE x.lesson_id = l.id AND x.kind = 'conditions' AND p.lesson_id IS NULL)
LEFT JOIN lesson_entries ne ON ne.id = (
    SELECT MAX(x.id) FROM lesson_entries x
    WHERE x.lesson_id = l.id AND x.kind = 'note')
LEFT JOIN lesson_entries we ON we.id = (
    SELECT MAX(x.id) FROM lesson_entries x
    WHERE x.lesson_id = l.id AND x.kind = 'withdraw' AND p.lesson_id IS NULL)
LEFT JOIN obs_events hw ON hw.id = (
    SELECT MAX(w.id) FROM obs_events w
    WHERE w.kind = 'human_withdraw' AND w.lesson_id = l.id
      AND w.ref_id IN (SELECT id FROM utterance_human));

-- ============================================================
-- 8. lesson_bad_step: 悪い結果を伴って踏んだ発生ごとに1行
-- ============================================================
-- (a) 有効なviolated。位置は根拠の発話。
-- (b) 踏み跡をtool_failに置いた知見のstepped（失敗そのものが悪い結果）。
--     step_eventは「今の」条件で読む（lesson_currentをJOINしているため、
--     conditions追記でstep_eventがtool_callからtool_failに変わると、それ以前
--     に書かれたstepped行まで遡ってBになる）。lesson_currentが出自・効き目を
--     毎回計算し直すのと同じ考え方であり、stepped行自体に当時の条件を
--     持たせる列は増やさない。
CREATE VIEW lesson_bad_step AS
SELECT v.lesson_id, v.session_id, v.evidence_id AS pos,
       (SELECT MAX(s.id) FROM obs_events s
        WHERE s.kind = 'stepped' AND s.lesson_id = v.lesson_id
          AND s.session_id = v.session_id AND s.id < v.evidence_id) AS step_id
FROM lesson_violated_ok v
UNION ALL
SELECT s.lesson_id, s.session_id, s.id AS pos, s.id AS step_id
FROM obs_events s
JOIN lesson_current lc ON lc.lesson_id = s.lesson_id
WHERE s.kind = 'stepped' AND lc.step_event = 'tool_fail';

-- ============================================================
-- 9. lesson_bad_step_prior: 事前配達・上限落ち・同じ呼び出しを発生ごとに判定
-- ============================================================
-- 「別の呼び出し」の判定が肝である。両方の行にtool_use_idがあり等しいとき
-- だけを同じ呼び出しとする。d.tool_use_id <> s.tool_use_id だけを書くと、
-- どちらかがNULLのとき比較結果がNULLになり、session・prompt・pullの配達
-- （tool_use_idがNULL）が黙って「事前配達なし」に落ちる。3項のORを必ず書く。
CREATE VIEW lesson_bad_step_prior AS
SELECT bs.lesson_id, bs.session_id, bs.pos, bs.step_id,
  CASE WHEN bs.step_id IS NOT NULL THEN (
        SELECT COUNT(*) FROM obs_events d JOIN obs_events s ON s.id = bs.step_id
        WHERE d.kind = 'delivered' AND d.lesson_id = bs.lesson_id
          AND d.session_id = bs.session_id AND d.id < bs.step_id
          AND (d.tool_use_id IS NULL OR s.tool_use_id IS NULL
               OR d.tool_use_id <> s.tool_use_id))
       ELSE (
        SELECT COUNT(*) FROM obs_events d
        WHERE d.kind = 'delivered' AND d.lesson_id = bs.lesson_id
          AND d.session_id = bs.session_id AND d.id < bs.pos)
  END AS prior_cnt,
  (SELECT COUNT(*) FROM obs_events q
   WHERE q.kind = 'suppressed' AND q.lesson_id = bs.lesson_id
     AND q.session_id = bs.session_id AND q.id < bs.pos) AS supp_cnt,
  CASE WHEN bs.step_id IS NULL THEN 0 ELSE (
        SELECT COUNT(*) FROM obs_events d JOIN obs_events s ON s.id = bs.step_id
        WHERE d.kind = 'delivered' AND d.lesson_id = bs.lesson_id
          AND d.session_id = bs.session_id
          AND d.tool_use_id IS NOT NULL AND s.tool_use_id IS NOT NULL
          AND d.tool_use_id = s.tool_use_id)
  END AS same_call_cnt
FROM lesson_bad_step bs;

-- ============================================================
-- 10. lesson_score: 採点の集計
-- ============================================================
-- すべてCOUNT(DISTINCT session_id)で数える（COUNT(*)を書いた時点で間違い）。
-- MとUは重なりうる（1つのセッションに事前配達ありのBと無しのBが両方あれば
-- 両方に入る。M + U = Bは成り立たない）。retiredはAI由来の配達する知見だけ
-- （人間由来は同じ値を見せるが引っ込めない）。時刻の比較は使わず、種類の
-- 名前も書かない。
CREATE VIEW lesson_score AS
SELECT
  l.id AS lesson_id,
  (SELECT COUNT(DISTINCT s.session_id) FROM obs_events s
   WHERE s.kind = 'stepped' AND s.lesson_id = l.id)                              AS x,
  (SELECT COUNT(DISTINCT b.session_id) FROM lesson_bad_step_prior b
   WHERE b.lesson_id = l.id)                                                     AS b,
  (SELECT COUNT(DISTINCT b.session_id) FROM lesson_bad_step_prior b
   WHERE b.lesson_id = l.id AND b.prior_cnt > 0)                                 AS m,
  (SELECT COUNT(DISTINCT b.session_id) FROM lesson_bad_step_prior b
   WHERE b.lesson_id = l.id AND b.prior_cnt = 0)                                 AS u,
  (SELECT COUNT(DISTINCT b.session_id) FROM lesson_bad_step_prior b
   WHERE b.lesson_id = l.id AND b.prior_cnt = 0 AND b.supp_cnt > 0)              AS u_budget,
  (SELECT COUNT(DISTINCT b.session_id) FROM lesson_bad_step_prior b
   WHERE b.lesson_id = l.id AND b.prior_cnt = 0 AND b.same_call_cnt > 0)         AS u_same,
  CASE WHEN k.delivers = 1 THEN
    (SELECT COUNT(DISTINCT v.session_id) FROM lesson_violated_ok v WHERE v.lesson_id = l.id)
  ELSE 0 END                                                                     AS s,
  (SELECT COUNT(DISTINCT c.session_id) FROM lesson_contradicted_ok c
   WHERE c.lesson_id = l.id)                                                     AS c,
  (SELECT COUNT(DISTINCT c.session_id) FROM lesson_contradicted_ok c
   WHERE c.lesson_id = l.id)                                                     AS w,
  CASE WHEN k.delivers = 0 THEN
    (SELECT COUNT(DISTINCT v.session_id) FROM lesson_violated_ok v WHERE v.lesson_id = l.id)
  ELSE 0 END                                                                     AS t,
  CASE WHEN o.origin = 'ai' AND k.delivers = 1
        AND (SELECT COUNT(DISTINCT c.session_id) FROM lesson_contradicted_ok c
             WHERE c.lesson_id = l.id) >= 2
        AND (SELECT COUNT(DISTINCT c.session_id) FROM lesson_contradicted_ok c
             WHERE c.lesson_id = l.id)
          > (SELECT COUNT(DISTINCT v.session_id) FROM lesson_violated_ok v
             WHERE v.lesson_id = l.id)
       THEN 1 ELSE 0 END                                                         AS retired
FROM lessons l
JOIN lesson_kinds k  ON k.kind = l.kind
JOIN lesson_origin o ON o.lesson_id = l.id;

-- ============================================================
-- 11. open_requests: Stopが差し戻す未処理の依頼
-- ============================================================
-- 列は (session_id, prompt_id, kind, ref_id) の4つで固定する。候補が枝を
-- 足す場所は末尾のUNION ALL。
-- 弱い語の枝でd.prompt_id IS NOT NULLを落とすと、常時配達（session口。
-- prompt_idを持たない）がある限りあらゆる弱い語が依頼になり毎ターン差し戻す
-- ことになる。u.prompt_id IS NOT NULLが無いと、prompt_idの無い発話から立つ
-- 依頼が永久に開いたままになる。
-- 「行頭」の判定は4枝要る（文字列の先頭・改行の直後 × 半角・全角コロン）。
-- LIKE '%知見にしない:%' にすると本文の途中で触れただけで閉じてしまう。
CREATE VIEW open_requests AS
SELECT u.session_id       AS session_id,
       u.prompt_id        AS prompt_id,
       'correction'       AS kind,
       u.id               AS ref_id
FROM utterance_human u
WHERE u.prompt_id IS NOT NULL
  -- 強い語、または「直前の人間のターンで配達があった」弱い語
  AND ( u.flag = 'strong'
        OR ( u.flag = 'weak'
             AND EXISTS (
                 SELECT 1 FROM obs_events d
                 WHERE d.kind = 'delivered' AND d.agent_id IS NULL
                   AND d.session_id = u.session_id
                   AND d.prompt_id IS NOT NULL
                   AND d.prompt_id = (
                       SELECT p.prompt_id FROM utterance_human p
                       WHERE p.session_id = u.session_id
                         AND p.id < u.id
                         AND p.prompt_id IS NOT NULL
                         AND p.prompt_id <> u.prompt_id
                       ORDER BY p.id DESC LIMIT 1) ) ) )
  -- 同じターンに「人間の裏づけになり、note以外の」bindが無いこと。
  -- (lesson_id, entry_id)の組ごとにbindが高々1行しか作られない前提に依る
  -- （作成のbindは1回に1行・entry_idはNULL、追記のbindは1回に1行・entry_id
  -- は一意）。この前提を壊す変更を足すときは、条件がここで壊れないか確かめる。
  AND NOT EXISTS (
        SELECT 1 FROM obs_events b
        LEFT JOIN lesson_entries e ON e.id = b.entry_id
        WHERE b.kind = 'bind'
          AND b.session_id = u.session_id AND b.prompt_id = u.prompt_id
          AND (b.entry_id IS NULL OR e.kind <> 'note')
          AND EXISTS (
              SELECT 1 FROM lesson_basis lb
              WHERE lb.void = 0
                AND lb.session_id = b.session_id
                AND lb.lesson_id  = b.lesson_id
                AND ( (b.entry_id IS NULL AND lb.entry_id IS NULL)
                      OR lb.entry_id = b.entry_id ) ) )
  -- 同じターンのreplyに行頭「知見にしない:」の行が無いこと
  AND NOT EXISTS (
        SELECT 1 FROM obs_events r
        WHERE r.kind = 'reply'
          AND r.session_id = u.session_id AND r.prompt_id = u.prompt_id
          AND ( r.text LIKE '知見にしない:%'
             OR r.text LIKE '知見にしない：%'
             OR r.text LIKE '%' || char(10) || '知見にしない:%'
             OR r.text LIKE '%' || char(10) || '知見にしない：%' ) );
