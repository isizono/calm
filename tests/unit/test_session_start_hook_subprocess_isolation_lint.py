"""hooks/session_start_hook.py をsubprocess起動するテストファイルが、
tests.helpers の共有ヘルパー（session_start_hook_env / run_session_start_hook /
run_session_start_hook_process）を経由しているかを検証する構造lint。

過去の実障害: あるテストファイルがtests.helpersを経由せず、生の
subprocess.run + 自前env組み立てでsession_start_hook.pyを起動していた。
CALM_HABITS_RULES_PATHの注入が漏れており、hookが実ファイル
（~/.claude/rules/cc-memory-habits.md）へ書き込んだ。tests.helpers側に
隔離ロジックを集約しても、新規テストファイルが生のsubprocess.run+env組み立てで
同じ穴を再現することは（import漏れという凡ミス1つで）構造的に防げない。
本lintはその再発を機械的に検知する。

検出方法: `tests/`配下の各テストファイルについて、ソース文字列に
"session_start_hook"という語と、いずれかのsubprocess起動関数呼び出し
（run/Popen/call/check_call/check_output のいずれか）が両方含まれる
ファイルを抽出し、そのファイルが`tests.helpers`からimportしているかを
確認する。文字列一致ベースの粗い
検出だが、hookの起動をf-string等で組み立てるケース（実例:
TestSessionStartHookRelayInboxViaRealUv、`uv run`経由の起動コマンドは変数
展開で組み立てる）もAST走査より確実に拾える。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"
_HELPERS_FILE = (_TESTS_DIR / "helpers.py").resolve()

_SUBPROCESS_CALL_RE = re.compile(
    r"subprocess\." r"(run|Popen|call|check_call|check_output)\("
)
_HOOK_MENTION_RE = re.compile(r"session_start_hook")
_HELPERS_IMPORT_RE = re.compile(r"\btests\.helpers\b|\bfrom tests import helpers\b")


def _candidate_test_files() -> list[Path]:
    return sorted(
        p for p in _TESTS_DIR.rglob("test_*.py") if p.resolve() != _HELPERS_FILE
    )


def test_session_start_hook_subprocess_launchers_import_shared_helper():
    """session_start_hook.pyをsubprocess起動している気配のあるテストファイルは、
    すべて tests.helpers から共有ヘルパーをimportしていること。

    生のsubprocess.run+自前env組み立てで書かれた新規テストファイルが
    紛れ込むと、CALM_HABITS_RULES_PATHの注入漏れによる実ファイルへの
    書き込み事故（本テストが検知対象とする過去の実障害）が再発しうる。
    """
    violations = []
    for path in _candidate_test_files():
        text = path.read_text(encoding="utf-8")
        if _HOOK_MENTION_RE.search(text) and _SUBPROCESS_CALL_RE.search(text):
            if not _HELPERS_IMPORT_RE.search(text):
                violations.append(str(path.relative_to(_REPO_ROOT)))

    assert violations == [], (
        "session_start_hook.pyをsubprocess起動していながらtests.helpersの"
        f"共有ヘルパーをimportしていないファイル: {violations}\n"
        "生のsubprocess.run+env組み立てをせず、tests.helpers.session_start_hook_env"
        "（またはrun_session_start_hook / run_session_start_hook_process）を使うこと。"
    )


def test_lint_target_scan_actually_finds_the_known_launchers():
    """回帰保護: 走査ロジックが、実際にhookをsubprocess起動している既知の
    2ファイル（test_snapshot.py・test_session_start_hook.py）を正しく候補として
    拾えていること（検出条件の書き間違いで0件のままvacuous passし続ける事故を防ぐ）。
    """
    matched = {
        str(p.relative_to(_REPO_ROOT))
        for p in _candidate_test_files()
        if _HOOK_MENTION_RE.search(p.read_text(encoding="utf-8"))
        and _SUBPROCESS_CALL_RE.search(p.read_text(encoding="utf-8"))
    }
    assert "tests/e2e/test_snapshot.py" in matched
    assert "tests/e2e/test_session_start_hook.py" in matched
