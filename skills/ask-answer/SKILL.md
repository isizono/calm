---
name: ask-answer
description: 【必須】open askを一覧して1件ずつ人間に提示し、回答が得られたらanswer_askで記録する最小フロー。get_asksを1回呼び、順に提示してanswer_askを呼ぶだけの直線的な手順を踏む。「/ask-answer」「askに答える」「open ask消化して」「溜まってるask片付けたい」「判断待ちに回答する」などで発動。継続的なopen ask滞留の監視はask-watch、起票前のquestion/context構成はask-compose、起票後の同型メタask起票はask-distillの担当のため発動しない。このスキルを経由せずにanswer_askを直接呼んではいけない。
---

# ask-answer

open askを一覧し、1件ずつ人間に提示して回答を得たら`answer_ask`で記録する最小skill。トリアージ判定・同型判定・監視ループは持たず、「聞く→答える→記録する」の直線フローだけを担当する。

## 手順

1. `get_asks()`を1回だけ呼ぶ（`status`の既定値は`"open"`なので省略してよい）。ユーザーが明示的にkind/tags等を指定しない限り、追加のフィルタ引数は渡さない。`limit`はデフォルト（20件）のままでよく、ページネーションのために追加呼び出しを重ねる必要はない。
2. `total_count`が0件なら「open askはありません」と提示し、`answer_ask`を呼ばずにここで終了する。
3. 返り値の`asks`件数が`total_count`より少ない場合は「上位N件のみ表示（全M件）」と明記してから、1件ずつ順に人間へ提示する。提示は`question`（概要）を中心に、必要なら`context`も見せる。内部ID（`id_raw`）は表示せず、タイトル・要約ベースで「これは何のaskか」が伝わる形にする。
4. 人間から回答が得られたら、その発言をそのまま、または意図を汲んだ文面で`answer_ask(ask_id=<該当askのid_raw>, answer_body=<回答本文>)`を呼ぶ。回答本文は人間の判断そのものであり、AIが代わりに考えて埋めない。`A`/`はい`のような一言回答も、そのまま`answer_body`に渡してよい（`ask-compose`のテンプレートが一言回答を許容しているため）。
5. `answer_ask`が成功し、返り値の`next_step`にトリアージへの誘導が含まれていても、それはこのskillでは実行しない（次のaskへ進む）。対象がすでにanswered/promoted/dismissed等で`answer_ask`がエラーを返した場合は、エラーをそのまま提示して次のaskへ進む。
6. 残りのaskについて3〜5を繰り返し、最後まで進んだら完了を短く報告する。

## 注意

- 回答本文（`answer_body`）は人間の発言・意図をそのまま反映したものであり、AIが自分の判断で作文して埋めてはならない
- 1つのaskに対して`answer_ask`が成功するのは1回だけ。既にanswered/promoted/dismissed等になったaskへの再回答は拒否される
- `triage_ask`（promote/dismiss）・`withdraw_ask`はこのskillの手順に含めない。答えた後の裁定（一般化ルールとして発効させるか、見送るか）は別経路（次回のcheck-in・`get_asks(triage_pending_only=true)`を使った非メタask自走裁定等）に委ねる。理由: このskillのスコープは「順に答えるだけ」であり、答えたその場でのtriageまでは含まない設計判断のため
- `answer_ask`はaskがblockしているactivityのブロックを解除しない（解除するのは`triage_ask`のpromote/dismiss、または`withdraw_ask`のみ）。このskillで回答した後もblockは残った状態のままであることを、完了報告時に人間へ伝える
- 提示は常にタイトル・question・context等の内容ベースで行い、内部ID（`id_raw`）をユーザーに見せない
- `kind="meta"`のaskも通常のaskと同じ手順（一覧→提示→`answer_ask`）で処理できる（`answer_ask`のシグネチャは`kind`に依存しない）。メタask固有の配置作業（`rule-placement` skillへの誘導）はこのskillの手順に含めない

## 関連skillとの境界

- `ask-compose`: `add_ask`を呼ぶ**前**にquestion/contextを構成する。ask-answerは既に起票済みのaskに答える側であり、起票前の作業は担当しない
- `ask-distill`: `add_ask`呼び出し**後**、`similar_asks`から同型askの反復に気づいたときにメタaskを起票する。ask-answerは回答フローに専念し、同型判定・メタask起票は行わない
- `ask-watch`: open askの滞留をMonitorでイベント駆動監視し続けるスキル。ask-answerは1回限りの一覧+回答フローであり、継続監視は行わない
