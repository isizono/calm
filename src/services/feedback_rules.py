"""feedback_entries.condition_json の正規化・検証・評価。

spec = {"tool": str|null, "all": [{"field": str, "op": "regex"|"len_gt", "value": ...}, ...]}
（all は0〜3要素）。フィールドの予約名・ドットパス解決・文字列化規則の実行時
アルゴリズムはこのモジュールが実装の真実源であり、他に仕様書は無い。

標準ライブラリだけで書く（hookからも呼ばれるため、numpy・yoyo等を引き込む
importは避ける）。
"""
from __future__ import annotations

import json
import re
from typing import Optional

MAX_CLAUSES = 3
MAX_REGEX_VALUE_LEN = 200
MAX_EVAL_TEXT_LEN = 20_000

_VALID_OPS = ("regex", "len_gt")
_VALID_TIMINGS = ("utterance", "tool_fail", "pre_tool")


class ConditionError(ValueError):
    """condition_jsonの形式・内容が不正なときに送出する。"""


def parse_condition(condition) -> dict:
    """dictまたはJSON文字列を受け取り、dictを返す。JSON構文エラーはConditionErrorにする。"""
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except json.JSONDecodeError as e:
            raise ConditionError(f"condition のJSONが不正: {e}") from e
    if not isinstance(condition, dict):
        raise ConditionError("condition はオブジェクトである必要がある")
    return condition


def validate_condition(condition: dict, *, strength: str, timing: str) -> dict:
    """condition_jsonの形状 + strength/timingごとの制約を検査し、正規化した形を返す。

    不正な場合は ConditionError を送出する。戻り値は {"tool": str|None, "all": [...]}
    のみのキーに揃えた辞書（そのまま json.dumps して condition_json に保存できる）。
    """
    if timing not in _VALID_TIMINGS:
        raise ConditionError(f"未知のtiming: {timing}")
    extra_keys = set(condition) - {"tool", "all"}
    if extra_keys:
        raise ConditionError(f"condition に不明なキー: {sorted(extra_keys)}")

    tool = condition.get("tool")
    if tool is not None:
        if not isinstance(tool, str) or not tool.strip():
            raise ConditionError("tool は非空文字列またはnull")
    if timing == "utterance" and tool is not None:
        raise ConditionError("timing='utterance' の tool は null 固定")

    raw_clauses = condition.get("all", [])
    if not isinstance(raw_clauses, list) or len(raw_clauses) > MAX_CLAUSES:
        raise ConditionError(f"all は最大{MAX_CLAUSES}要素の配列")

    clauses: list[dict] = []
    for c in raw_clauses:
        if not isinstance(c, dict) or set(c) != {"field", "op", "value"}:
            raise ConditionError("all の各要素は field/op/value の3キーのみを持つオブジェクト")
        field, op, value = c["field"], c["op"], c["value"]
        if not isinstance(field, str) or not field.strip():
            raise ConditionError("field は非空文字列")
        if timing == "utterance" and field != "prompt":
            raise ConditionError("timing='utterance' の field は 'prompt' 固定")
        if op not in _VALID_OPS:
            raise ConditionError(f"op は {_VALID_OPS} のいずれか")
        if op == "regex":
            if not isinstance(value, str) or len(value) > MAX_REGEX_VALUE_LEN:
                raise ConditionError(f"regexのvalueは{MAX_REGEX_VALUE_LEN}字までの文字列")
            try:
                re.compile(value)
            except re.error as e:
                raise ConditionError(f"regexが不正: {e}") from e
        else:  # len_gt
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConditionError("len_gtのvalueは数値")
        clauses.append({"field": field, "op": op, "value": value})

    if strength == "block" and tool is None and not clauses:
        raise ConditionError(
            "strength='block' で tool=null かつ all=[] は書き込めない"
            "（無条件で全ツール呼び出しを止められてしまうため）"
        )

    return {"tool": tool, "all": clauses}


def _resolve_dot_path(data, path: str) -> Optional[str]:
    """dictキーのみを辿ってpathを解決し、文字列化して返す。

    途中でlist等（非dict）に当たった場合・キーが存在しない場合はNone
    （「不一致」を表す。例外にはしない）。最終的に解決された値は、辞書・
    リストを含めどんな型でもstr()で文字列化して返す。
    """
    if not isinstance(data, dict):
        return None
    current = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return str(current)


def _resolve_field_text(
    field: str,
    *,
    timing: str,
    tool_input: Optional[dict],
    error_text: Optional[str],
    prompt_text: Optional[str],
) -> Optional[str]:
    """フィールド名を実際の評価対象テキストへ解決する。該当なしはNone。"""
    if timing == "utterance":
        return prompt_text if field == "prompt" else None
    if timing == "tool_fail" and field == "error":
        return error_text
    return _resolve_dot_path(tool_input, field)


def _evaluate_clause(
    clause: dict,
    *,
    timing: str,
    tool_input: Optional[dict],
    error_text: Optional[str],
    prompt_text: Optional[str],
) -> bool:
    text = _resolve_field_text(
        clause["field"],
        timing=timing,
        tool_input=tool_input,
        error_text=error_text,
        prompt_text=prompt_text,
    )
    if text is None:
        return False
    text = text[:MAX_EVAL_TEXT_LEN]
    if clause["op"] == "regex":
        # ponytail: タイムアウト・破局的バックトラック対策なし。strength='block'
        # (timing='pre_tool'固定)はPreToolUseの同期パスでこの評価結果を待つため、
        # 悪い正規表現を書けば該当セッションのツール実行が止まりうる。エントリを
        # 書けるのはClaude自身のみで外部入力ではないため最小形では許容する。
        # 悪化したら書き込み時の複雑度検査 or signal / re2 等への切り替えを検討する。
        return re.search(clause["value"], text) is not None
    return len(text) > clause["value"]  # len_gt


def evaluate_condition(
    condition: dict,
    *,
    timing: str,
    tool_name: Optional[str] = None,
    tool_input: Optional[dict] = None,
    error_text: Optional[str] = None,
    prompt_text: Optional[str] = None,
) -> bool:
    """正規化済みcondition（validate_conditionの戻り値と同じ形状）を実データに対して評価する。

    tool条件・all の全clauseを満たせばTrue。all が空なら（tool条件だけ満たせば）True。

    保存済みデータの正規表現が壊れている場合はre.errorを送出する（呼び出し側が
    エントリ単位でtry/exceptし、そのエントリだけスキップする設計を前提にしている
    ため、ここでは握りつぶさない）。
    """
    tool = condition.get("tool")
    if tool is not None:
        if timing == "utterance" or tool_name != tool:
            return False

    for clause in condition.get("all", []):
        if not _evaluate_clause(
            clause,
            timing=timing,
            tool_input=tool_input,
            error_text=error_text,
            prompt_text=prompt_text,
        ):
            return False
    return True
