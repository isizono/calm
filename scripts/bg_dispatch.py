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

--pending-dirは、CALMに書けない内容の退避先を依頼文に埋め込む引数(省略時は
環境変数CALM_PENDING_DIRを見る。どちらも無ければ、退避先を決めずworktree内に
残す指示になる)。

依頼文の書き方・起動形・生死の見分け方など、bgを振るorchの手順はcalm:orch
skillに置く。ここでは依頼文の生成だけを扱う。
"""
from __future__ import annotations

import argparse
import os

_TEMPLATE = """あなたはCALMのアクティビティ「{activity_title}」の実装担当のbgセッションです。
指示を出したのはこのアクティビティを束ねるorchです。報告は常にそのorchアクティビティへadd_logsで書いてください(具体的な宛先は下のステップ3・4で確認します)。

## 作業場所
- worktree: {worktree}{branch_line}
- このworktreeの外のファイルは編集しない。Bashでは毎回、絶対パスでcdすること

## 最初にやること
1. CALMでアクティビティ「{activity_title}」(activity_id={activity_id})を探してcheck_inする
2. {goal_instruction}{parent_check_step}

## 完了条件
- goal.nextに従い、満たした条件はupdate_goalでsatisfiedにする{completion_block}

## 越えない線
- 外向きの操作、~/.claude配下の変更、既決を見直すことになる判断、auto modeや権限に止められた操作は、進めずに報告先(親のorchアクティビティ)へ返す。止められたら迂回しない
- 人間宛てのaskは起票しない。人の判断が要るときは報告先へ返す
- EnterWorktreeは使わない

## 待たずに進む
- 上の線の内側の実装上の判断は、返事を待たずに最も妥当な方針で進め、判断をPR本文と報告に書く
- マイグレーション番号が必要なときは、origin/mainとopen PRの番号の最大値+1を取る。宣言して返事を待たない

## やらないこと
- マージ、--delete-branchの使用{dont_block}

## 記録
- 経緯はadd_logsで記録する。完了の合図(update_goalのsatisfiedかSendMessage)があるのに
  check_in以降にadd_logsが無いと、Stop hookが1回blockする
- CALMに書き込めない内容は{pending_line}
- 最後の報告の前に `{sync_memory_scope}` でsync-memoryを実行する

## 完了したら
3・4で控えた親のorchアクティビティへadd_logsで報告を書く。書く内容は、PR番号・CIの状態・自分で判断したこと・残っていること。
そのあとget_by_idsでそのアクティビティの説明の担い手欄を読み、`claude agents --json`でそのsessionIdの行にpidがあれば、その行のnameへSendMessageで「orchのログに報告を書いた」と要旨を知らせる。空席・死んでいる・送れないときは知らせを省く。
報告のあとは次の指示を待ち、自分から終わらない。
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
) -> str:
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
    return _TEMPLATE.format(
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
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
    ), end="")


if __name__ == "__main__":
    main()
