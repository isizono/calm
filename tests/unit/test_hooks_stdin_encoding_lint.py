"""hooks/配下のフックが、標準入力を生のsys.stdin.read()で読んでいないことを
確かめる構造lint。

Windows既定のANSIコードページ(cp932等)ではテキストモードのsys.stdinが
ロケールエンコーディングになり、日本語を含むJSON入力が化けたりデコード
できなかったりする。src.harness.claude_code.read_stdin_text()はbuffer経由で
UTF-8として読むことでこれを避けており、hooks/配下の全フックはこれを経由する
よう揃えた。生のsys.stdin.read()を直接呼ぶ新規フックが紛れ込むとこの問題が
再発するため、本lintは機械的に検知する。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _REPO_ROOT / "hooks"

_RAW_STDIN_READ_RE = re.compile(r"sys\.stdin\.read\(")


def test_hooks_do_not_read_stdin_directly():
    violations = []
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _RAW_STDIN_READ_RE.search(text):
            violations.append(str(path.relative_to(_REPO_ROOT)))

    assert violations == [], (
        f"sys.stdin.read()を直接呼んでいるhook: {violations}\n"
        "src.harness.claude_code.read_stdin_text()を経由すること"
        "(Windows既定のANSIコードページ下でのデコード崩れを避けるため)。"
    )
