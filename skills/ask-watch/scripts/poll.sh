#!/usr/bin/env bash
# asksテーブルのopen件数・最新last_seen_at・id集合のいずれかが変化した瞬間だけ1行出力する。
# GROUP_CONCAT(id)まで比較に含めているのは、件数が同じでもid構成が入れ替わる変化
# （1件closeして1件openになった等）を取りこぼさないため。
DB="$HOME/.claude/.claude-code-memory/discussion.db"
prev=$(sqlite3 "$DB" "SELECT COUNT(*), MAX(last_seen_at), GROUP_CONCAT(id) FROM asks WHERE status='open';" 2>/dev/null)
while true; do
  sleep 10
  cur=$(sqlite3 "$DB" "SELECT COUNT(*), MAX(last_seen_at), GROUP_CONCAT(id) FROM asks WHERE status='open';" 2>/dev/null)
  if [ "$cur" != "$prev" ]; then
    echo "ask store changed: $cur"
    prev="$cur"
  fi
done
