"""SessionStart hook: worker terminal ref をファイルキャッシュ。

env (TMUX_PANE) を読んで
`~/.cc-memory/ow/term_refs/<session_id>.json` に書き込む。

ow_service.ow_send が identity event を受信した際、payload.data.term_ref が
未設定なら本 hook が書いたファイルを session_id で参照して補完する。

env 優先順:
- TMUX_PANE: tmux 配下、pane_id (`%N`)

env が取れない場合は何も書かない (補完失敗時は identity event を素通し)。
"""
import json
import os
import sys
from pathlib import Path


def _detect_term_ref() -> str | None:
    tmux_pane = os.environ.get("TMUX_PANE", "").strip()
    if tmux_pane:
        return tmux_pane
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        session_id = payload.get("session_id")
        if not session_id:
            return

        term_ref = _detect_term_ref()
        if not term_ref:
            return

        cache_dir = Path.home() / ".cc-memory" / "ow" / "term_refs"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{session_id}.json"
        out.write_text(
            json.dumps({"term_ref": term_ref}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"term_ref_cache hook error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
