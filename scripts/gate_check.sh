#!/bin/sh
# 正規のローカル呼び出し経路: origin/main 版の検出器を取り出して実行する。
#
# PR branch 上の gate_check.py はブランチ側で改変され得るため、判定は常に
# origin/main 版で行う。これにより検出器自身への変更が判定を迂回できない。
set -eu

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# fetch 失敗(ネットワーク不通・origin 未設定など)でスクリプトごと落とさない。
# set -e 配下でも下の分岐へ必ず進み、ローカルに既存の origin/main があればそれで、
# 無ければ worktree 版へフォールバックして必ず verdict を返す。
git fetch -q origin main 2>/dev/null || true

if git show origin/main:scripts/gate_check.py > "$tmp/gate_check.py" 2>/dev/null; then
  exec python3 "$tmp/gate_check.py" "$@"
else
  # 検出器がまだ main に未マージの導入初期のみ: worktree 版で代用し、
  # その旨を verdict に残す(detector_source=worktree)。
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  exec python3 "$script_dir/gate_check.py" --detector-source worktree "$@"
fi
