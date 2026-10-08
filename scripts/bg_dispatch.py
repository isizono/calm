#!/usr/bin/env python3
"""claude --bg向けの依頼文を生成する。

宛先activity・goal・作業ツリー・完了条件・やらないこと・報告先・sync-memoryの
範囲を埋めた依頼文をstdoutへ出す。標準ライブラリのみに依存する(DB接続はしない。
条件の詳細はgoal.next/get_goalに委ねる設計のため、activity_id/titleと
goal handle(あれば)だけ分かれば依頼文は組み立てられる)。

goalは依頼の前に発注側がset_goalで作っておく想定。--goal-handleを渡さない
場合は、依頼文の側でgoal未定義ならset_goalで書くよう委譲先に指示する。

## orchが子を振るときの3段の手順

1. add_activityをcheck_in=Falseで呼び、子のアクティビティを作る
2. 子のアクティビティにset_goalでgoalを作る
3. orchのgoalにupdate_goal(op="add")で、子のアクティビティへ束縛した条件を足す

orchは子にcheck_inせず、get_goalで子のgoalを読む(check_inすると子にorchの
heartbeatが付いてしまい、止まっているかどうかの判定が壊れる)。

## 親goalへの結びつけゲートと報告先

--parent-goal-handleと--parent-condition-idは必須の引数で、上記3段目で
orchが作った束縛条件をこの依頼文の受け手に伝える。依頼文には、受けたbgが
着手時に親goalをget_goalで読み、指定した条件のboundが自分のアクティビティを
指しているかを確かめ、外れていたら作業を始めずに親goalのactivitiesの1件目に
あるアクティビティ(orchアクティビティ)へ返す検査が入る。ラッパー自身はDBに
接続しないため、実際に結びついたかどうかまでは検査しない(受け側に確かめさせる
宣言を強制するだけ)。同じ親goalのactivitiesの1件目が、以後の報告先(orch
アクティビティ)にもなる。親を持たない依頼を明示する引数は無い。

## 兄弟との連携

同じ親goalに束縛された他の子(兄弟)の一覧は、依頼文には埋め込まず、受け側が
親goalのconditionsとget_sessionsから、必要になった時点で引く。ラッパーはDBに
接続しないうえ、orchは子を順に振るので、起動時点では後から振る兄弟の宛先が
決まっていないため。依頼文に入るのは、一覧の引き方と、兄弟の担当に関わる事実を
見つけたときに兄弟へも知らせる義務だけで、兄弟への連絡はこの4項目(共有ファイル・
マイグレーション番号・mainの破損・前提の変化)の周知に限る。

## 相談役・観測役

--consultant-activity-idを渡すと、作業役の依頼文に「相談先」の節が入る。相談役は
兄弟ではなく相談先として名指しされ、判断に迷ったときのSendMessageでの相談が
許される(兄弟へは事実の周知しか送れない規則の例外)。渡さない依頼文は従来のまま。
--role consultantは、相談役自身に渡す依頼文の変種に切り替える(実装も調査もせず、
判断の材料と案を返し、報告後も止まらず待つ)。--role observerは、測って数字を出す
だけの観測役の依頼文(決定「orchは5本常駐、反証役は都度thinker」で、生死・文脈の
測り手は相談役から観測役に寄った)。どちらも--consultant-activity-idとは併用できない。

担い手の見張りは観測役が持つ(予告止まりの検知だけ相談役に残る)。観測役は
orch_liveness.pyで生死・固まりを、handoff_trigger.pyで文脈・圧縮・skillの版の
交代契機を判定し、超えたら担い手へ「交代せよ」を送る。DEAD・STUCKは検知して
報告先へ書くところまでで、後継の起動はしない(決定「無人での後継起動は環境の
限界とする」)。--role consultant・--role observerのいずれも、--holder-name・
--holder-session-id・--holder-transcriptで起動時点の担い手を渡す。

--pending-dirは、CALMに書けない内容の退避先を依頼文に埋め込む引数(省略時は
環境変数CALM_PENDING_DIRを見る。どちらも無ければ、退避先を決めずworktree内に
残す指示になる)。

依頼文の書き方・起動形・生死の見分け方など、bgを振るorchの手順はcalm:orch
skillに置く。ここでは依頼文の生成だけを扱う。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hooks.delegate_marker import write_delegate_marker  # noqa: E402

# 「完了したら」節の担い手の生死の規則は、skills/orch/SKILL.mdの窓口の生死の規則と揃える（bgはorch skillを読まずに動くため再掲している）
_TEMPLATE = """あなたはCALMのアクティビティ「{activity_title}」の実装担当のbgセッションです。
指示を出したのはこのアクティビティを束ねるorchです。報告は常にそのorchアクティビティへadd_logsで書いてください(具体的な宛先は下のステップ3・4で確認します)。

## 作業場所
- worktree: {worktree}{branch_line}
- このworktreeの外のファイルは編集しない。Bashでは毎回、絶対パスでcdすること

{first_steps}

## 完了条件
- goal.nextに従い、満たした条件はupdate_goalでsatisfiedにする{completion_block}

## 越えない線
- 外向きの操作、~/.claude配下の変更、既決を見直すことになる判断、auto modeや権限に止められた操作は、進めずに報告先(親のorchアクティビティ)へ返す。止められたら迂回しない
- 人間宛てのaskは起票しない。人の判断が要るときは報告先へ返す
- EnterWorktreeは使わない

## 待たずに進む
- 上の線の内側の実装上の判断は、返事を待たずに最も妥当な方針で進め、判断をPR本文と報告に書く
- マイグレーション番号が必要なときは、origin/mainとopen PRの番号の最大値+1を取る。宣言して返事を待たない

## 兄弟との連携
兄弟とは、3で読んだ親goalのconditionsのうち、boundが自分以外のアクティビティを指す条件のアクティビティ(完了済みは除く)。一覧は着手時に作らず、下の事実を見つけたときに引く。
- 兄弟の宛先: get_sessionsでそのactivity_idの行を探し、`claude agents --json`でその行のcli_session_idと一致するsessionIdでpidがある行を探す。あれば、その行の今のnameが宛先。行が無い・死んでいるときは空席として扱う
- 兄弟の担当に関わる事実(共有ファイル・マイグレーション番号・mainの破損・前提の変化)を見つけたら、報告先へのadd_logsに加えて、その兄弟へもSendMessageで事実を知らせる。空席・送れないときは省く
- 兄弟へ送るのは上の事実の周知だけ。作業の依頼・質問・進捗の共有は送らない{consultant_block}

{dont}

## 記録
- 経緯はadd_logsで記録する。完了の合図(update_goalのsatisfiedかSendMessage)があるのに
  check_in以降にadd_logsが無いと、Stop hookが1回blockする(この依頼文の宛先activityにcheck-inしたセッションだけが対象)
{record_tail}

## 完了したら
3・4で控えた親のorchアクティビティへadd_logsで報告を書く。書く内容は、PR番号・CIの状態・自分で判断したこと・残っていること。
そのあとget_by_idsでそのアクティビティの説明の担い手欄(sessionId)を読み、`claude agents --json`でそのsessionIdが一致しpidがある行を探す（名前は照合に使わない。複数あればstartedAtが最も新しい行）。あれば、その行の今のnameへSendMessageで「orchのログに報告を書いた」と要旨を知らせる。空席・死んでいる・送れないときは知らせを省く。
報告のあとは次の指示を待ち、自分から終わらない。
"""


# 作業役と相談役の依頼文で共通の節。片方だけ直して食い違わないよう、ここに1つだけ持つ
_FIRST_STEPS = """## 最初にやること
1. CALMでアクティビティ「{activity_title}」(activity_id={activity_id})を探してcheck_inする
2. {goal_instruction}{parent_check_step}

goalのstatementをこの仕事の目的として読む。成果物ができたかでなく、statementが指す状態に近づいたかで判断する。"""

_DONT = """## やらないこと
- マージ、--delete-branchの使用{dont_block}"""

_RECORD_TAIL = """- CALMに書き込めない内容は{pending_line}
- 最後の報告の前に `{sync_memory_scope}` でsync-memoryを実行する"""

_CONSULTANT_BLOCK = """

## 相談先
アクティビティ(activity_id={consultant_activity_id})の相談役は、兄弟ではなく相談先。上の兄弟の規則(質問を送らない)は相談役には当てはめない。
- 宛先: 兄弟と同じ引き方(get_sessionsでそのactivity_idの行を探し、`claude agents --json`でpidのある行の今のnameを使う)
- SendMessageで相談してよい。場面は、方針に迷うとき、手の結果が目的に近づいたか判断できないとき、人に返す前の分類(返す3項目に当たるか)
- 送る中身: 次の手と、その手に何を期待するか、結果。調べた事実はそのまま付ける
- 返事は助言。結論と責任は自分とorchが持つ。宛先が空席・死んでいる・返事が無いときは待たず、自分で判断して進め、判断を報告に書く"""

_CONSULTANT_TEMPLATE = """あなたはCALMのアクティビティ「{activity_title}」の相談役のbgセッションです。
指示を出したのはこのアクティビティを束ねるorchです。相談役は実装も調査も実測もしません。判断の材料と案を返すだけで、結論と責任はorchが持ちます。

## 作業場所
- worktree: {worktree}{branch_line}
- ファイルは編集しない(読むだけ)。Bashでは毎回、絶対パスでcdすること

{first_steps}

## 受け取るもの
orchや作業役のbgから、SendMessageかorchアクティビティのログで次が届く。
- 目的節と現在地節
- 次の手と、その手に何を期待するか
- 手の結果

## 返すもの
- その手・結果が目的に近づいたか、その根拠。外部の観測、反証を試みて生き残った仮説、未確定事項の減少のどれに当たるか
- 見落としや別の読み
- 方向を変える数字の検算の要否と、そのやり方
- 人に出そうとしている問いが、人しか持たない事実・本人の価値判断・orchの「ユーザーに上げるもの」のどれにも当たらないか

## 書き先
- SendMessageで来た相談には、送ってきた相手へSendMessageで返す
- 指摘の要点は、報告先(親のorchアクティビティ)へadd_logsでも書く
- 同じ型の誤りの指摘は、そう明記する(フィードバックの材料になる)

## 担い手の見張り(観測役と分担)
相談役に残るのは、担い手への`notify_when_idle`の購読と、知らせのたびの予告止まりの検査、押し上げ、経緯の要る検査。生死・固まりの判定、担い手の文脈の大きさの測定、「交代せよ」の送信は観測役が持つ(相談役は生死・文脈の定期のcronを持たない)。
起動時点の担い手は {holder_name}(sessionId {holder_session_id}、transcript {holder_transcript})。担い手は世代交代で替わるので、見るたびに報告先(親のorchアクティビティ)の説明の担い手欄からsessionIdを読み直し、transcriptはそのsessionIdから引く(`~/.claude/projects/*/<sessionId>.jsonl`)。宛先は、`claude agents --json`でそのsessionIdが一致しpidがある行(複数あればstartedAtが最も新しい行)の今のname。

1. 予告止まりの検査: 担い手へSendMessageの`notify_when_idle`で購読する。1回で切れるので、知らせが来るたびに今の担い手へ張り直す。知らせが来たら、transcriptの最後の返答と状態節の次の一手を突き合わせ、予告した手が打たれずに止まっていれば担い手へSendMessageで起こす
2. 観測役がいなければ、担い手に`bg_dispatch.py --role observer`で立てるよう報告先へ書く(観測役を立てるのは担い手の仕事で、相談役は自分で立てない)

## 越えない線
- 外向きの操作、~/.claude配下の変更、既決を見直すことになる判断、auto modeや権限に止められた操作は、進めずに報告先(親のorchアクティビティ)へ返す
- 人間宛てのaskは起票しない

{dont}

## 記録
- 経緯はadd_logsで記録する
{record_tail}

## 待ち方
報告のあとも自分から終わらない。次の相談が届くまで待つ。orchが止めるまで続ける。
"""

_OBSERVER_TEMPLATE = """あなたはCALMのアクティビティ「{activity_title}」の観測役のbgセッションです。測って数字を出すだけで、判断も提案もしません。実装も調査もしません。

## 作業場所
- worktree: {worktree}{branch_line}
- ファイルは編集しない(読むだけ)。Bashでは毎回、絶対パスでcdすること

{first_steps}

## 担い手の見張り(常設の仕事)
起動時点の担い手は {holder_name}(sessionId {holder_session_id}、transcript {holder_transcript})。担い手は世代交代で替わるので、見るたびに報告先(親のorchアクティビティ)の説明の担い手欄からsessionIdを読み直し、transcriptはそのsessionIdから引く(`~/.claude/projects/*/<sessionId>.jsonl`)。宛先は、`claude agents --json`でそのsessionIdが一致しpidがある行(複数あればstartedAtが最も新しい行)の今のname。

自分のCronCreate(recurring、30分ごと、:00と:30は避ける)で、担い手欄のsessionIdを次の2本の判定スクリプトに通す。

1. 生死・固まり: `python3 {liveness_script} --session-id <sessionId>` → 出力のverdictは次のとおり
   - DEAD: そのsessionIdが一致しpidがある行が無い
   - STUCK: その行のstatusがbusyのまま、transcriptが25分以上更新されていない
   - OK: それ以外
   - 担い手欄が空席なら判定しない(空席は異常ではない)
2. 文脈・圧縮・skillの版: `python3 {handoff_script} --session-id <sessionId> --role holder` → `trigger`がtrueなら、まだ送っていなければ担い手へSendMessageで「交代せよ」を送る(同じ相手に二重に送らない。送ったことをログに書く)

DEADかSTUCKが出たら、まず報告先へadd_logsで判定スクリプトの出力をそのまま書く。後継を起こすのは担い手(または次の担い手)の仕事で、観測役は検知して報告するところまで(決定「無人での後継起動は環境の限界とする」)。

CronCreateがauto modeに止められたら(許可が無い環境)、迂回も言い換えての再試行もしない。止められたことを報告先へ書くだけにし、1・2の判定は手動で続ける。

## 書き先
数字は報告先(親のorchアクティビティ)へadd_logs、まとまった計数は資材。担い手には変化があったときだけ要約1行

{dont}

## 記録
- 経緯はadd_logsで記録する
{record_tail}

## 待ち方
報告のあとも自分から終わらない。次の見張りの周期まで待つ。orchが止めるまで続ける。
"""


def build_request(
    *,
    activity_id: int,
    activity_title: str,
    worktree: str,
    parent_goal_handle: str,
    parent_condition_id: int,
    branch: str | None = None,
    goal_handle: str | None = None,
    completion: list[str] | None = None,
    dont: list[str] | None = None,
    sync_memory_scope: str = "sync-memory --minimal",
    pending_dir: str | None = None,
    role: str = "worker",
    consultant_activity_id: int | None = None,
    holder_name: str | None = None,
    holder_session_id: str | None = None,
    holder_transcript: str | None = None,
) -> str:
    if role not in ("worker", "consultant", "observer"):
        raise ValueError(f"unknown role: {role}")
    if role != "worker" and consultant_activity_id is not None:
        raise ValueError(f"consultant_activity_id is for worker requests, not role={role}")
    holder = (holder_name, holder_session_id, holder_transcript)
    if role in ("consultant", "observer") and not all(holder):
        raise ValueError(f"role={role} requires holder_name, holder_session_id, holder_transcript")
    if role == "worker" and any(holder):
        raise ValueError("holder_* is for role=consultant or role=observer")
    completion_block = _bullet_block(completion)
    dont_block = _bullet_block(dont)
    if goal_handle:
        goal_instruction = (
            f"get_goal(handle=\"{goal_handle}\")で条件を確かめ、goal.nextに従う。"
            "満たした条件はupdate_goalでsatisfiedにする"
        )
    else:
        goal_instruction = (
            "check_inの応答のgoal.nextに従う。goalが未定義なら、スコープを条件としてset_goalで書く"
        )
    parent_check_step = (
        f'\n3. get_goal(handle="{parent_goal_handle}")で親goalを読み、'
        f"conditionsのid_raw={parent_condition_id}の条件のboundが"
        f'{{"type": "activity", "id_raw": {activity_id}}}(=自分のアクティビティ)を'
        "指しているかを確かめる。外れていたら作業を始めず、"
        "その親goalのactivitiesの1件目にあるアクティビティへadd_logsで理由を書いて止める"
        "\n4. 3で読んだ親goalのactivitiesの1件目にあるアクティビティを、"
        "以降の報告先(親のorchアクティビティ)として控える"
    )
    resolved_pending_dir = pending_dir or os.environ.get("CALM_PENDING_DIR")
    if resolved_pending_dir:
        pending_line = f"`{resolved_pending_dir}` へファイルとして退避し、報告に書く"
    else:
        pending_line = "分かる形でworktree内に残し、報告に書く(退避先の設定なし)"
    consultant_block = (
        _CONSULTANT_BLOCK.format(consultant_activity_id=consultant_activity_id)
        if consultant_activity_id is not None
        else ""
    )
    template = {"consultant": _CONSULTANT_TEMPLATE, "observer": _OBSERVER_TEMPLATE}.get(role, _TEMPLATE)
    for name, part in (
        ("first_steps", _FIRST_STEPS), ("dont", _DONT), ("record_tail", _RECORD_TAIL),
    ):
        template = template.replace("{" + name + "}", part)
    return template.format(
        consultant_block=consultant_block,
        activity_title=activity_title,
        activity_id=activity_id,
        worktree=worktree,
        branch_line=(f"、ブランチ {branch}" if branch else ""),
        goal_instruction=goal_instruction,
        parent_check_step=parent_check_step,
        completion_block=completion_block,
        dont_block=dont_block,
        sync_memory_scope=sync_memory_scope,
        pending_line=pending_line,
        holder_name=holder_name,
        holder_session_id=holder_session_id,
        holder_transcript=holder_transcript,
        liveness_script=Path(__file__).resolve().parent / "orch_liveness.py",
        handoff_script=Path(__file__).resolve().parent / "handoff_trigger.py",
    )


def _bullet_block(items: list[str] | None) -> str:
    if not items:
        return ""
    return "\n" + "\n".join(f"- {item}" for item in items)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--activity-id", type=int, required=True, help="宛先activityのID")
    parser.add_argument("--activity-title", required=True, help="宛先activityのタイトル")
    parser.add_argument("--worktree", required=True, help="作業ツリーの絶対パス")
    parser.add_argument("--branch", default=None, help="worktreeのブランチ名(表示用、省略可)")
    parser.add_argument(
        "--goal-handle", default=None,
        help=(
            "依頼前にset_goalで作っておいたgoalのhandle。英小文字・数字・ハイフンのみ、"
            "40字以内(goal_serviceの制約と同じ、ここではバリデーションしない)。"
            "省略時はgoal未定義ならset_goalで書くよう指示する"
        ),
    )
    parser.add_argument(
        "--parent-goal-handle", required=True,
        help="親orchのgoal handle。orchが子を束縛条件で結びつけたgoalを指す",
    )
    parser.add_argument(
        "--parent-condition-id", type=int, required=True,
        help="親goalの束縛条件id(update_goalのop=addで足した条件のid_raw)",
    )
    parser.add_argument(
        "--completion", action="append", default=None,
        help="完了条件に追加する項目(複数指定可)",
    )
    parser.add_argument(
        "--dont", action="append", default=None,
        help="やらないことに追加する項目(複数指定可)",
    )
    parser.add_argument(
        "--sync-memory-scope", default="sync-memory --minimal",
        help="止める前に実行するsync-memoryの範囲(既定: sync-memory --minimal)",
    )
    parser.add_argument(
        "--pending-dir", default=None,
        help="CALMに書けないときの退避先ディレクトリ(省略時は環境変数CALM_PENDING_DIRを見る)",
    )
    parser.add_argument(
        "--role", choices=["worker", "consultant", "observer"], default="worker",
        help="依頼文の種類。consultantは実装も調査もしない相談役用、observerは生死・文脈を測って報告するだけの観測役用(既定: worker)",
    )
    parser.add_argument(
        "--consultant-activity-id", type=int, default=None,
        help="相談役のactivityのID。渡すと作業役の依頼文に相談先の節が入る(--role consultant・--role observerとは同時に指定できない)",
    )
    for flag, label in (
        ("--holder-name", "担い手の名前"),
        ("--holder-session-id", "担い手のsessionId"),
        ("--holder-transcript", "担い手のtranscriptのパス"),
    ):
        parser.add_argument(
            flag, default=None,
            help=f"{label}。--role consultant・--role observerのとき必須(起動時点の値。見るたびに担い手欄から読み直す)",
        )
    args = parser.parse_args(argv)
    if args.role != "worker" and args.consultant_activity_id is not None:
        parser.error(f"--consultant-activity-idは--role {args.role}と同時に指定できない")
    holder = (args.holder_name, args.holder_session_id, args.holder_transcript)
    if args.role in ("consultant", "observer") and not all(holder):
        parser.error(f"--role {args.role}には--holder-name・--holder-session-id・--holder-transcriptが要る")
    if args.role == "worker" and any(holder):
        parser.error("--holder-*は--role consultantまたは--role observerのときだけ指定できる")
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    # 依頼文を出す宛先activityを委譲先として記録する(Stop hookの記録義務block用)。
    # 依頼文の生成そのものはbuild_requestに閉じ、ファイルを書くのはCLI実行時だけ。
    write_delegate_marker(args.activity_id)
    print(build_request(
        activity_id=args.activity_id,
        activity_title=args.activity_title,
        worktree=args.worktree,
        parent_goal_handle=args.parent_goal_handle,
        parent_condition_id=args.parent_condition_id,
        branch=args.branch,
        goal_handle=args.goal_handle,
        completion=args.completion,
        dont=args.dont,
        sync_memory_scope=args.sync_memory_scope,
        pending_dir=args.pending_dir,
        role=args.role,
        consultant_activity_id=args.consultant_activity_id,
        holder_name=args.holder_name,
        holder_session_id=args.holder_session_id,
        holder_transcript=args.holder_transcript,
    ), end="")


if __name__ == "__main__":
    main()
