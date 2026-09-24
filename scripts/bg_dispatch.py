#!/usr/bin/env python3
"""claude --bg向けの依頼文を生成する。

宛先activity・goal・作業ツリー・完了条件・やらないこと・報告先・sync-memoryの
範囲を埋めた依頼文をstdoutへ出す。標準ライブラリのみに依存する(DB接続はしない。
条件の詳細はgoal.next/get_goalに委ねる設計のため、activity_id/titleと
goal handle(あれば)だけ分かれば依頼文は組み立てられる)。

goalは依頼の前に発注側がset_goalで作っておく想定。--goal-handleを渡さない
場合は、依頼文の側でgoal未定義ならset_goalで書くよう委譲先に指示する。

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
2. {goal_instruction}

## 完了条件
- goal.nextに従い、満たした条件はupdate_goalでsatisfiedにする{completion_block}

## やらないこと
- マージ、--delete-branchの使用
- サーバー再起動
- マイグレーション番号を勝手に取ること(必要になったら{report_to}へ宣言して返事を待つ){dont_block}

## 記録
- 経緯はadd_logsで記録する。完了の合図(judge_goal呼び出し)があるのに
  記録が無いと、Stop hookが1回blockする
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
    return _TEMPLATE.format(
        activity_title=activity_title,
        activity_id=activity_id,
        worktree=worktree,
        branch_line=(f"、ブランチ {branch}" if branch else ""),
        report_to=report_to,
        goal_instruction=goal_instruction,
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
    return parser.parse_args(argv)


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
    ), end="")


if __name__ == "__main__":
    main()
