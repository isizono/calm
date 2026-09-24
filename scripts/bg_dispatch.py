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

## 親goalへの結びつけゲート

--parent-goal-handleと--parent-condition-idは、上記3段目でorchが作った
束縛条件をこの依頼文の受け手に伝える引数で、セットで指定する。渡すと依頼文に
「受けたbgは着手時に親goalをget_goalで読み、指定した条件が自分のアクティビティに
束縛されているかを確かめ、外れていたら作業を始めずに振り主へ返す」検査が入る。
ラッパー自身はDBに接続しないため、実際に結びついたかどうかまでは検査しない
(受け側に確かめさせる宣言を強制するだけ)。親を持たない依頼には--no-parentを
明示する。どちらも指定しない、または両方同時に指定する呼び出しはエラーで
止まる。

出力したテキストをそのままclaude --bgに渡す起動形は未確認のため、ここでは
規定しない。出力を確認してから貼り付ける、またはファイルに保存して使うこと。
"""
from __future__ import annotations

import argparse

_TEMPLATE = """あなたはCALMのアクティビティ「{activity_title}」の実装担当のbgセッションです。
指示を出したのは{report_to}のセッションで、報告もそこへSendMessageで返してください。

## 作業場所
- worktree: {worktree}{branch_line}
- このworktreeの外のファイルは編集しない。Bashでは毎回、絶対パスでcdすること

## 最初にやること
1. CALMでアクティビティ「{activity_title}」(activity_id={activity_id})を探してcheck_inする
2. {goal_instruction}{parent_check_step}

## 完了条件
- goal.nextに従い、満たした条件はupdate_goalでsatisfiedにする{completion_block}

## やらないこと
- マージ、--delete-branchの使用
- サーバー再起動
- マイグレーション番号を勝手に取ること(必要になったら{report_to}へ宣言して返事を待つ){dont_block}

## 記録
- 経緯はadd_logsで記録する。完了の合図(update_goalのsatisfiedかSendMessage)があるのに
  check_in以降にadd_logsが無いと、Stop hookが1回blockする
- 止める前に `{sync_memory_scope}` でsync-memoryを実行する

## 完了したら
{report_to}へSendMessageで報告する。書く内容は、PR番号・CIの状態・自分で判断したこと・残っていること。
報告のあとは次の指示を待ち、自分から終わらない。
"""


def build_request(
    *,
    activity_id: int,
    activity_title: str,
    worktree: str,
    report_to: str,
    branch: str | None = None,
    goal_handle: str | None = None,
    completion: list[str] | None = None,
    dont: list[str] | None = None,
    sync_memory_scope: str = "sync-memory --minimal",
    parent_goal_handle: str | None = None,
    parent_condition_id: int | None = None,
) -> str:
    if (parent_goal_handle is None) != (parent_condition_id is None):
        raise ValueError(
            "parent_goal_handleとparent_condition_idは両方指定するか、両方省略する"
        )
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
    if parent_goal_handle is not None:
        parent_check_step = (
            f"\n3. get_goal(handle=\"{parent_goal_handle}\")で親goalを読み、"
            f"条件id={parent_condition_id}が自分のアクティビティ(activity_id={activity_id})に"
            "束縛されているかを確かめる。外れていたら作業を始めず、"
            f"{report_to}へSendMessageで理由とともに返す"
        )
    else:
        parent_check_step = ""
    return _TEMPLATE.format(
        activity_title=activity_title,
        activity_id=activity_id,
        worktree=worktree,
        branch_line=(f"、ブランチ {branch}" if branch else ""),
        report_to=report_to,
        goal_instruction=goal_instruction,
        parent_check_step=parent_check_step,
        completion_block=completion_block,
        dont_block=dont_block,
        sync_memory_scope=sync_memory_scope,
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
    parser.add_argument("--report-to", required=True, help="完了報告を送るセッション名")
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
        "--parent-goal-handle", default=None,
        help=(
            "親orchのgoal handle。--parent-condition-idとセットで指定する"
            "(--no-parentと排他)"
        ),
    )
    parser.add_argument(
        "--parent-condition-id", type=int, default=None,
        help="親goalの束縛条件id。--parent-goal-handleとセットで指定する",
    )
    parser.add_argument(
        "--no-parent", action="store_true",
        help="親を持たない依頼であることを明示する。--parent-goal-handle/--parent-condition-idと排他",
    )
    args = parser.parse_args(argv)
    has_parent_handle = args.parent_goal_handle is not None
    has_parent_condition = args.parent_condition_id is not None
    if args.no_parent and (has_parent_handle or has_parent_condition):
        parser.error(
            "--no-parentと--parent-goal-handle/--parent-condition-idは同時に指定できない"
        )
    if not args.no_parent and not (has_parent_handle and has_parent_condition):
        parser.error(
            "--parent-goal-handleと--parent-condition-idの両方、"
            "または--no-parentのどちらかが必須"
        )
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    print(build_request(
        activity_id=args.activity_id,
        activity_title=args.activity_title,
        worktree=args.worktree,
        report_to=args.report_to,
        branch=args.branch,
        goal_handle=args.goal_handle,
        completion=args.completion,
        dont=args.dont,
        sync_memory_scope=args.sync_memory_scope,
        parent_goal_handle=args.parent_goal_handle,
        parent_condition_id=args.parent_condition_id,
    ), end="")


if __name__ == "__main__":
    main()
