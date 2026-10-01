"""hooks/配下のフックが、標準入力をsys.stdin経由で直接読んでいないことを
確かめる構造lint。

Windows既定のANSIコードページ(cp932等)ではテキストモードのsys.stdinが
ロケールエンコーディングになり、日本語を含むJSON入力が化けたりデコード
できなかったりする。src.harness.claude_code.read_stdin_text()はbuffer経由で
UTF-8として読むことでこれを避けており、hooks/配下の全フックはこれを経由する
よう揃えた。sys.stdin.read()に限らずreadline()やfor文での反復、
json.load(sys.stdin)等も同じ問題を踏むため、sys.stdinへの言及自体を検知する
(read_stdin_text自身の実装(src/harness/claude_code.py)は対象外。hooks/配下
のみを走査する)。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _REPO_ROOT / "hooks"

_RAW_STDIN_RE = re.compile(r"\bsys\.stdin\b")


def test_hooks_do_not_read_stdin_directly():
    violations = []
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _RAW_STDIN_RE.search(text):
            violations.append(str(path.relative_to(_REPO_ROOT)))

    assert violations == [], (
        f"sys.stdinを直接参照しているhook: {violations}\n"
        "src.harness.claude_code.read_stdin_text()を経由すること"
        "(Windows既定のANSIコードページ下でのデコード崩れを避けるため)。"
    )


def test_scan_target_is_not_empty():
    """回帰保護: hooks/の場所の変更等で走査対象が0件になり、上のテストが
    vacuous passし続ける事故を防ぐ(既知のフックが候補に含まれることを確認する)。
    """
    scanned = {p.name for p in _HOOKS_DIR.glob("*.py")}
    assert "session_start_hook.py" in scanned
