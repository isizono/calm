"""check_in応答の全体予算切り詰めモジュール。

入力は応答dictと方針(BudgetPolicy)の2つだけであり、DBにもcheck_inの組み立てにも
触れない。書き直し前後どちらのcheck_in応答形にも、対応するpolicyを渡すことで
同じ手順を適用できる（policy自体は形を持つ側=checkin_serviceが定数として持つ）。

字数の数え方はlen(json.dumps(response, ensure_ascii=False))で統一する
（config.PRECEDENT_RESPONSE_CHARS_MAXが使っている「JSON文字列化後の実測文字数」
という約束に揃えたもの）。FastMCPの実際のシリアライズとは区切り文字などで
数%ずれると推測されるため、値は近似として扱う。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Callable, Literal

CutMode = Literal["tail_list", "stub_dict"]


def measure_chars(obj: object) -> int:
    """objのJSON文字列化後の実測文字数を返す。"""
    return len(json.dumps(obj, ensure_ascii=False))


@dataclass(frozen=True)
class CutStep:
    """予算超過時に応答の1パスを削る手順。

    path: response直下のキー名。
    mode: "tail_list"（リストを末尾から1件ずつ削る）| "stub_dict"
        （dictを{id_raw, title, chars, next}のスタブに置き換える）。
    coverage_key: env.coverage（現状はresponse["coverage"]）の対応する分子名。
        Noneならcoverageの書き換え対象にしない。
    pointer: 削った後に"next"へ添えるポインタ一覧を組み立てる関数。response全体を
        受け取り [{"tool": ..., "args": ...}, ...] を返す。Noneなら付けない。
    """
    path: str
    mode: CutMode
    coverage_key: str | None = None
    pointer: Callable[[dict], list[dict]] | None = None


@dataclass(frozen=True)
class CappedSection:
    """全体予算には数えないが、独自の天井を持つ枠（例: 制御信号・tag_notes）。

    paths: 合算して字数を測るresponse直下のキー名の集合。全体予算(budget_chars)には
        数えない。
    cap_chars: この枠だけの天井。
    fold: 天井超過時に呼ぶ畳み込み関数。response全体を受け取りin-placeで畳む。
        Noneなら畳まずtruncatedにoverフラグを立てるだけにする。
    """
    name: str
    paths: tuple[str, ...]
    cap_chars: int
    fold: Callable[[dict], None] | None = None


@dataclass(frozen=True)
class PinnedPolicy:
    """pinned枠の縮小方針。

    path: response直下のpinnedキー名。
    slot_chars: pinned専用の枠（予算全体10,000字の内側で確保される分）。
    content_field: 子キー（decisions/logs/materials等）ごとの本文フィールド名。
        枠をまたぐ要素の先頭を残して切るときに使う。無ければ丸ごとスタブにする。
    pointer: (response, child_key, id_raw) -> ポインタ一覧を組み立てる関数。
    """
    path: str
    slot_chars: int
    content_field: dict[str, str]
    pointer: Callable[[dict, str, int | None], list[dict]] | None = None


@dataclass(frozen=True)
class BudgetPolicy:
    budget_chars: int
    hard_max_chars: int
    protected_paths: frozenset[str]
    capped_sections: tuple[CappedSection, ...]
    pinned: PinnedPolicy | None
    cut_steps: tuple[CutStep, ...]
    hard_max_pointer: Callable[[dict], list[dict]] | None = None


def _uncounted_paths(policy: BudgetPolicy) -> set[str]:
    paths: set[str] = set()
    for section in policy.capped_sections:
        paths.update(section.paths)
    return paths


def _total_chars(response: dict, uncounted: set[str]) -> int:
    counted = {k: v for k, v in response.items() if k not in uncounted}
    return measure_chars(counted)


def _rewrite_coverage_numerator(response: dict, key: str, new_numerator: int) -> None:
    coverage = response.get("coverage")
    if not isinstance(coverage, dict):
        return
    value = coverage.get(key)
    if not isinstance(value, str) or "/" not in value:
        return
    _, _, denom = value.partition("/")
    coverage[key] = f"{new_numerator}/{denom}"


def _item_id(item: dict) -> int | None:
    return item.get("id_raw") if "id_raw" in item else item.get("id")


def _apply_cut_step(response: dict, step: CutStep, budget_chars: int, uncounted: set[str]) -> dict | None:
    value = response.get(step.path)

    if step.mode == "tail_list":
        if not isinstance(value, list) or not value:
            return None
        original_len = len(value)
        while value and _total_chars(response, uncounted) > budget_chars:
            value.pop()
        cut = original_len - len(value)
        if cut == 0:
            return None
        if step.coverage_key is not None:
            _rewrite_coverage_numerator(response, step.coverage_key, len(value))
        cut_info = {"section": step.path, "kept": len(value), "cut": cut}
        if step.pointer is not None:
            cut_info["next"] = step.pointer(response)
        return cut_info

    if step.mode == "stub_dict":
        if not isinstance(value, dict) or not value:
            return None
        if _total_chars(response, uncounted) <= budget_chars:
            return None
        original_chars = measure_chars(value)
        stub = {"id_raw": _item_id(value), "title": value.get("title"), "chars": original_chars}
        if step.pointer is not None:
            stub["next"] = step.pointer(response)
        response[step.path] = stub
        if step.coverage_key is not None:
            _rewrite_coverage_numerator(response, step.coverage_key, 0)
        return {"section": step.path, "kept": 0, "cut": 1}

    return None


def _shrink_pinned(response: dict, pinned_policy: PinnedPolicy, slot_chars: int) -> dict | None:
    """pinnedを指定字数まで縮める。小さい要素から順に丸ごと残し、枠をまたぐ要素は
    先頭を残して切り、それ以降はスタブにする（種別をまたいだ小さい順）。
    """
    pinned = response.get(pinned_policy.path)
    if not isinstance(pinned, dict) or not pinned:
        return None
    if measure_chars(pinned) <= slot_chars:
        return None

    flat: list[tuple[str, int, dict, int]] = []
    for child_key, items in pinned.items():
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                flat.append((child_key, idx, item, measure_chars(item)))
    if not flat:
        return None
    flat.sort(key=lambda t: t[3])

    kept_keys: set[tuple[str, int]] = set()
    used = 0
    crossing_key: tuple[str, int] | None = None
    for child_key, idx, _item, isize in flat:
        if used + isize <= slot_chars:
            kept_keys.add((child_key, idx))
            used += isize
        elif crossing_key is None:
            crossing_key = (child_key, idx)
            break
        else:
            break

    new_pinned: dict[str, list] = {}
    for child_key, idx, item, isize in flat:
        key = (child_key, idx)
        if key in kept_keys:
            new_pinned.setdefault(child_key, []).append(item)
        elif key == crossing_key:
            field = pinned_policy.content_field.get(child_key)
            stub = dict(item)
            if field and isinstance(stub.get(field), str):
                remaining = max(slot_chars - used, 0)
                stub[field] = stub[field][:remaining]
                stub["content_truncated"] = True
            if pinned_policy.pointer is not None:
                stub["next"] = pinned_policy.pointer(response, child_key, _item_id(item))
            new_pinned.setdefault(child_key, []).append(stub)
        else:
            stub = {"id_raw": _item_id(item), "title": item.get("title"), "chars": isize}
            if pinned_policy.pointer is not None:
                stub["next"] = pinned_policy.pointer(response, child_key, _item_id(item))
            new_pinned.setdefault(child_key, []).append(stub)

    response[pinned_policy.path] = new_pinned
    kept_count = len(kept_keys)
    return {"section": pinned_policy.path, "kept": kept_count, "cut": len(flat) - kept_count}


def _apply_hard_max(response: dict, policy: BudgetPolicy) -> bool:
    """ハード上限超過時の最後の手段: activity.descriptionの先頭を残して切る。"""
    activity = response.get("activity")
    if not isinstance(activity, dict):
        return False
    desc = activity.get("description")
    if not isinstance(desc, str) or not desc:
        return False
    overflow = measure_chars(response) - policy.hard_max_chars
    keep = max(len(desc) - overflow - 200, 200)
    if keep >= len(desc):
        return False
    activity["description"] = desc[:keep]
    activity["description_truncated"] = True
    if policy.hard_max_pointer is not None:
        activity["description_next"] = policy.hard_max_pointer(response)
    return True


def apply_budget(response: dict, policy: BudgetPolicy) -> dict:
    """policyに従いresponseを予算内へ切り詰める（non-mutating、新しいdictを返す）。

    応答がエラーなら何もしない。総字数（予算に数えないパスを除く）が
    budget_chars以下なら何もしない（truncatedキーも付けない）。
    """
    if not isinstance(response, dict) or "error" in response:
        return response

    response = copy.deepcopy(response)
    uncounted = _uncounted_paths(policy)

    control_flags: dict[str, bool] = {}
    for section in policy.capped_sections:
        size = measure_chars({p: response[p] for p in section.paths if p in response})
        if size > section.cap_chars:
            control_flags[f"{section.name}_over"] = True
            if section.fold is not None:
                section.fold(response)

    before = _total_chars(response, uncounted)
    if before <= policy.budget_chars:
        if control_flags:
            response["truncated"] = {
                "budget": policy.budget_chars,
                "before": before,
                "after": before,
                "over_budget": False,
                **control_flags,
            }
        return response

    cuts: list[dict] = []

    if policy.pinned is not None:
        cut = _shrink_pinned(response, policy.pinned, policy.pinned.slot_chars)
        if cut:
            cuts.append(cut)

    for step in policy.cut_steps:
        if _total_chars(response, uncounted) <= policy.budget_chars:
            break
        cut = _apply_cut_step(response, step, policy.budget_chars, uncounted)
        if cut:
            cuts.append(cut)

    if policy.pinned is not None and _total_chars(response, uncounted) > policy.budget_chars:
        cut = _shrink_pinned(response, policy.pinned, 0)
        if cut:
            cuts.append(cut)

    after = _total_chars(response, uncounted)
    over_budget = after > policy.budget_chars

    hard_over = False
    if measure_chars(response) > policy.hard_max_chars:
        hard_over = _apply_hard_max(response, policy)

    truncated = {
        "budget": policy.budget_chars,
        "before": before,
        "after": after,
        "over_budget": over_budget,
        **control_flags,
    }
    if cuts:
        truncated["cuts"] = cuts
    if hard_over:
        truncated["hard_max"] = True

    response["truncated"] = truncated
    return response
