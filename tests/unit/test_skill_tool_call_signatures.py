"""skillsのツール呼び出し例が実際のMCPツール仕様と一致することを検証する導出型整合性lint。

SKILL.md / references/*.md に書かれた「ツール名(引数名=...)」形式の呼び出し例を
`tests.helpers.all_tool_schemas()`（src/main.pyの@mcp.tool定義から導出した実際の
ツール一覧・引数名）と突き合わせ、存在しないツール・存在しない引数名を検出する。
呼び出し例は1行に収まる前提で行単位で走査する。

存在しないツール名・存在しない引数名・括弧や引用符が閉じない呼び出しは違反とする。
"""
import re
from pathlib import Path

from tests.helpers import all_tool_schemas

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS_DIR = _REPO_ROOT / "skills"

_CALL_START_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(")
_KWARG_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")

class Violation:
    __slots__ = ("path", "line_no", "tool", "arg", "snippet")

    def __init__(self, path: str, line_no: int, tool: str, arg: str, snippet: str) -> None:
        self.path = path
        self.line_no = line_no
        self.tool = tool
        self.arg = arg
        self.snippet = snippet

    def __str__(self) -> str:
        return f"{self.path}:{self.line_no} `{self.snippet}`"


def _call_doc_paths() -> list[Path]:
    return sorted(_SKILLS_DIR.glob("*/SKILL.md")) + sorted(_SKILLS_DIR.glob("*/references/*.md"))


def _find_calls_in_line(line: str) -> list[tuple[str, str, bool]]:
    """1行から identifier(...) 形式の呼び出しを抜き出す。

    戻り値は (識別子, 括弧内テキスト, parsed_ok) のリスト。parsed_ok=False は
    行内で括弧・引用符が閉じなかったことを示す（残りの行テキストをそのまま返す）。
    """
    results = []
    for m in _CALL_START_RE.finditer(line):
        name = m.group(1)
        i = m.end()
        depth = 1
        quote = None
        start = i
        while i < len(line) and depth > 0:
            c = line[i]
            if quote:
                if c == quote and line[i - 1] != "\\":
                    quote = None
            elif c in "'\"":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            i += 1
        parsed_ok = depth == 0 and quote is None
        args_text = line[start:i - 1] if parsed_ok else line[start:]
        results.append((name, args_text, parsed_ok))
    return results


def _split_top_level_args(args_text: str) -> list[str]:
    """トップレベル（引用符・括弧の外側）にあるカンマでのみ分割する。"""
    parts = []
    depth = 0
    quote = None
    buf: list[str] = []
    for i, c in enumerate(args_text):
        if quote:
            buf.append(c)
            if c == quote and args_text[i - 1] != "\\":
                quote = None
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c in "([{":
            depth += 1
            buf.append(c)
        elif c in ")]}":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _extract_kwarg_names(args_text: str) -> list[str]:
    names = []
    for part in _split_top_level_args(args_text):
        m = _KWARG_RE.match(part)
        if m:
            names.append(m.group(1))
    return names


def _collect_violations() -> list[Violation]:
    schemas = all_tool_schemas()
    tool_names = set(schemas)
    violations: list[Violation] = []
    for path in _call_doc_paths():
        rel = str(path.relative_to(_REPO_ROOT))
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name, args_text, parsed_ok in _find_calls_in_line(line):
                if not parsed_ok:
                    # 括弧・引用符が行内で閉じなかった呼び出し例。kwarg形式らしき
                    # 痕跡（key=）があるものだけを機械判定不能な違反として扱う。
                    if re.search(r"[A-Za-z_]\w*\s*=(?!=)", args_text):
                        violations.append(Violation(rel, line_no, name, "", f"{name}({args_text}"))
                    continue
                kwargs = _extract_kwarg_names(args_text)
                if not kwargs:
                    continue
                if name not in tool_names:
                    violations.append(Violation(rel, line_no, name, "", f"{name}({args_text})"))
                    continue
                props = set(schemas[name].get("properties", {}))
                for kw in kwargs:
                    if kw not in props:
                        violations.append(Violation(rel, line_no, name, kw, f"{name}({args_text})"))
    return violations


def test_call_examples_use_real_tool_and_argument_names():
    """skillsの呼び出し例に存在しないツール名・引数名が無いこと。"""
    violations = _collect_violations()
    assert not violations, "手順通りに呼ぶとエラーになる呼び出し例が見つかった:\n" + "\n".join(
        str(v) for v in violations
    )
