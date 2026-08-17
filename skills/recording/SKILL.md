---
name: recording
description: 【必須】議論で複数案を比較して採択した、作業中に詰まって解決した、PR レビューで複数 fix した、ドラフト・調査レポート・比較表が出来上がった、バグを観察した、ユーザー指示で方針が変わったなど、セッション中に「経緯」または「成果物」が発生したときに発動。このスキルを経由せずに add_logs / add_material を直接呼んではいけない。記録ガイドが docstring から本スキルに集約されているため、判断基準を skip すると記録漏れが発生する。発動対象は add_logs と add_material のみ。add_decisions / add_topic / add_habit は対象外（別経路で扱う）。
---

# recording

セッション中に発生した「経緯（log）」と「成果物（material）」を記録する。判断基準と発動例をここに集約しているため、`add_logs` / `add_material` を呼ぶ前に本スキルの判定を必ず通す。

## 適用範囲

このスキルは初回 load 時に発動するが、判断基準は本セッション内のすべての後続ターンに適用される。以降のターンで下の例（L1-L5 / M1-M4）に該当する出来事が起きたら、明示的に skill を再発動せずとも `add_logs` / `add_material` を呼ぶこと。

sync-memory による漏れの救済は、ユーザーが `/sync-memory` を明示的に実行した場合に限られる（自動実行はない）。recording skill はトリガー例に基づく検知であり元々「完全網羅」を保証する仕組みではないため役割は「セッション中の見逃し低減」のままだが、事後の自動救済が無くなった分、判断に迷ったら記録する側に倒す姿勢がこれまで以上に重要になる。

## 対象

- `add_logs`（経緯記録）
- `add_material`（成果物保存）

`add_decisions` / `add_topic` / `add_habit` は本スキルの案内対象外。

CALM 自身の故障・使用感不満・既存記録との矛盾は `add_logs` ではなく `report_signal` を使う（下記「report_signal との切り分け」参照）。

## 発動例: add_logs（経緯記録）

| # | トリガー | 量の目安 | 残す内容 |
|---|---|---|---|
| L1 | 議論で 3 案以上比較 → 1 案採択（or 却下） | 議論で N 往復 / 案 3+ | 採択案・却下案・却下理由 |
| L2 | 実装/作業で blocked → 解決経緯 | blocked 1 回でも | 詰まった状況・解決手段・教訓 |
| L3 | PR レビュー対応で複数 fix した経緯 | bot 指摘 2 件以上 | 指摘内容・対応 commit・残課題 |
| L4 | ユーザー明示指示で方針が変わった | 1 回でも | 元方針・新方針・指示理由 |
| L5 | バグ観察（本題作業中の脇道発見） | 観察 1 件 | 現象・再現条件・影響範囲 |

## 発動例: add_material（成果物保存）

| # | トリガー | 量の目安 | 残す内容 |
|---|---|---|---|
| M1 | 設計たたき台 / ドラフト v0 / v1 完成 | 1 ドラフト | 生データそのまま（要約しない） |
| M2 | 調査・試算レポート完成 | 1 レポート | 試算式・結果・条件 |
| M3 | 未コミットのWIP差分・中間成果物 | 1 件 | diff・WIP 状態の説明 |
| M4 | 比較・分析結果（案 N 件の比較表など） | 表 1 つ | 比較項目・スコア・採択理由 |

「など」: 上記に当てはまらなくても、後続セッションが文脈を引き継ぐために要る経緯や成果物が出たら同等に扱う。例: 仕様確定議論の脇で出た代替案メモ、調査の途中で得た一次資料の抜粋、設計レビューで指摘された未解決リスト、外部記事の要旨など。

## report_signal との切り分け（CALM 自身の故障・矛盾）

L5（バグ観察）はユーザーが取り組んでいる対象システムのバグが対象。CALM 自身の故障・使用感不満・既存記録との矛盾を観測したときは `add_logs` ではなく `report_signal` を呼ぶ（合意不要の生の観測データであり、topic 紐付けや文脈タグは不要）。

| kind | 発火例 |
|---|---|
| `machine_error` | ツールエラー・hook 失敗・サーバー異常を観察した |
| `friction` | 検索で引けるべき記録が引けなかった等、CALM の使い勝手への不満・違和感を感じた |
| `contradiction` | 設計・実装中に既存 decision と矛盾する結論に達した / `add_decisions` の `related_decisions` で矛盾に気づいた |

上記3種は頻出例であり、`report_signal` の kind は全7種ある（`precedent_miss` /
`precedent_misapplied` / `boundary_case` / `rollback` を含む）。全種の定義は
`report_signal` ツールのdocstringを正とする。同一内容の再報告は `report_signal`
側で自動集約されるため、迷ったら報告してよい。

より詳しい発動例・判断に迷う場合の追加基準は本スキルディレクトリ内の
`references/taxonomy.md`（1章・2章・4章）に整理してある。本ページの表と
食い違う場合は `references/taxonomy.md` を正とする。

## 優先方針

**「多めに残す > 少なく残す」**。

迷ったら記録する。漏れの最終救済はユーザーが `/sync-memory` を実行した場合の sync-memory が担うが、それは保証された安全網ではない。本スキルの役割は「セッション中の見逃し低減」であり、ここで取りこぼすと `/sync-memory` が実行されない限り文脈は失われたままになる。

## 呼び出し時の注意

- `add_logs` の content / `add_material` の content は要約せず生データを残す
- `add_material` の content 先頭 1-2 文は内容の説明・要約を書く（check-in 時に snippet として表示される）
- `add_material` は `add_decisions` と違って「双方の合意」が不要。成果物が出た時点でユーザーに確認せず呼ぶ
- `add_material` の content に事実と推論が混在する場合は明示的に区別する（例: 「【事実】」「【推論】」見出しを付ける）
- `tags` には `domain:` を必ず付け、内容を表す素タグも追加する。`intent:` namespace の例: `intent:discuss` / `intent:design` / `intent:implement` / `intent:investigate`
- `related` で関連する activity / topic / decision / log / material と紐付ける
- `add_logs` は `topic_id` 必須。tag だけでは紐付かない
