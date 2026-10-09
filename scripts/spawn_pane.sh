#!/bin/bash
# 使い方: spawn_pane.sh <分割する元のペインid> <role> <起動文ファイル> [タイトル] [cwd] [model]
# 元のペインを縦に割り、新しいペインで pane_claude.py 経由で claude を起こし、新しいペインidを出力する。
# modelを省くとpane_claude.pyの役ごとの既定に従う。
set -u
src=$1; role=$2; file=$3; title=${4:-"$role (new)"}; cwd=${5:-$HOME/workspace}; model=${6:-}
LAB=$(cd "$(dirname "$0")/.." && pwd)
t(){ tmux -L orch "$@"; }
[ -s "$file" ] || { echo "launch file missing: $file" >&2; exit 1; }
new=$(t split-window -v -t "$src" -P -F '#{pane_id}' -c "$cwd")
[ -n "$new" ] || { echo "split failed (no space?)" >&2; exit 1; }
t select-pane -t "$new" -T "$title"
model_arg=""
[ -n "$model" ] && model_arg="--model $model"
t send-keys -t "$new" "python3 $(printf %q "$LAB")/scripts/pane_claude.py --role $role $model_arg --plugin-dir $(printf %q "$LAB") \"\$(cat $(printf %q "$file"))\"" Enter
echo "$new"
