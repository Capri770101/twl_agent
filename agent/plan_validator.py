"""DIY 方案的确定性校验；模型只生成候选，业务约束由这里复核。"""
from __future__ import annotations

from typing import Any


def validate_plan(plan: dict[str, Any], requirement: Any = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ['方案不是对象']
    design = plan.get('design') if isinstance(plan.get('design'), dict) else {}
    main = [x for x in (design.get('main_flowers') or []) if isinstance(x, dict) and x.get('name')]
    fillers = [x for x in (design.get('fillers') or []) if isinstance(x, dict) and x.get('name')]
    foliage = [x for x in (design.get('foliage') or []) if isinstance(x, dict) and x.get('name')]
    if not main:
        errors.append('缺少主花')
    all_names = [str(x.get('name')) for x in main + fillers + foliage]
    single = getattr(requirement, 'single_flower', None) if requirement is not None else None
    if single and any(single not in name for name in all_names):
        errors.append(f'违反单一花材约束：{single}')
    excluded = set(getattr(requirement, 'excluded_flowers', []) or []) if requirement is not None else set()
    excluded.update(str(x) for x in (plan.get('exclude_flowers') or []))
    for name in all_names:
        if any(bad and bad in name for bad in excluded):
            errors.append(f'包含排除花材：{name}')
    expected_stems = getattr(requirement, 'stem_count', None) if requirement is not None else None
    if expected_stems is not None and main:
        actual = sum(int(x.get('qty') or 0) for x in main)
        if actual != int(expected_stems):
            errors.append(f'主花支数不一致：期望{expected_stems}，实际{actual}')
    budget = getattr(requirement, 'budget_num', None) if requirement is not None else None
    price = plan.get('budget_num') or plan.get('price') or plan.get('estimated_price_num')
    if budget is not None and isinstance(price, (int, float)) and price > float(budget) * 1.15:
        errors.append(f'预算超出：方案{price}，预算{budget}')
    return list(dict.fromkeys(errors))


def annotate_plan_validation(plan: dict[str, Any], requirement: Any = None) -> dict[str, Any]:
    errors = validate_plan(plan, requirement)
    if errors:
        plan['validation_errors'] = errors
        plan['validation_status'] = 'needs_review'
    else:
        plan.pop('validation_errors', None)
        plan['validation_status'] = 'ok'
    return plan


def repair_plan(plan: dict[str, Any], requirement: Any = None) -> dict[str, Any]:
    """对明确、无歧义的硬约束做确定性修正，无法安全修正的预算问题保持阻断。"""
    if not isinstance(plan, dict):
        return plan
    design = plan.get('design') if isinstance(plan.get('design'), dict) else None
    if design is None:
        return annotate_plan_validation(plan, requirement)
    excluded = set(str(x) for x in (plan.get('exclude_flowers') or []) if x)
    excluded.update(str(x) for x in (getattr(requirement, 'excluded_flowers', []) or []) if x)

    def allowed(item: Any) -> bool:
        return isinstance(item, dict) and item.get('name') and not any(bad in str(item['name']) for bad in excluded)

    for key in ('main_flowers', 'fillers', 'foliage'):
        design[key] = [item for item in (design.get(key) or []) if allowed(item)]

    single = getattr(requirement, 'single_flower', None) if requirement is not None else None
    if single:
        matching = [item for item in design['main_flowers'] if single in str(item.get('name') or '')]
        if matching:
            design['main_flowers'] = matching[:1]
        design['fillers'] = []
        design['foliage'] = []

    expected = getattr(requirement, 'stem_count', None) if requirement is not None else None
    if expected is not None and design['main_flowers']:
        # 明确单花取全部支数；多主花无法知道用户想如何分配，不擅自改动。
        if len(design['main_flowers']) == 1:
            design['main_flowers'][0]['qty'] = int(expected)

    errors = validate_plan(plan, requirement)
    plan['validation_errors'] = errors
    plan['validation_status'] = 'blocked' if errors else 'ok'
    if not errors:
        plan.pop('validation_errors', None)
    return plan
