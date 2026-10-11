"""仅改变配色或包装颜色的局部改版；保留旧报价和用料。"""
from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger('tools')


def revise_visual_fields(original: dict, feedback: str, *,
                         call_model: Callable[..., Any],
                         effect_prompt: Callable[..., str],
                         copy_text: Callable[[dict], str]) -> dict | None:
    """从当前方案局部修订视觉字段；由入口传入依赖以保留测试替身。"""
    scoped_feedback = re.sub(r'(?:保留|保持)[^，。；,;！!]*?(?=但|只|改|换|调整|[，。；,;！!]|$)', '', feedback)
    scoped_feedback = re.sub(r'(?:预算|花材|数量|支数)(?:都|全部)?不变', '', scoped_feedback)
    clauses = re.split(r'[，。；,;！!]|但是|但|同时', scoped_feedback)
    changes = [c for c in clauses if re.search(r'改|换|调整|变|增加|减少|去掉|不要|便宜|贵', c)
               and not re.search(r'不要.*(?:图|预览)|不改|保持|保留|不变', c)]
    packaging = any('包装' in c or '包材' in c for c in changes)
    colors = any(re.search(r'配色|色系|色调|颜色|色', c) and '包装' not in c and '包材' not in c for c in changes)
    if not (packaging or colors):
        return None
    if packaging and re.search(r'花篮|礼盒|花盒|玻璃|陶瓷|丝绒|改.*材料|换.*材料', scoped_feedback):
        return {'ok': False, 'error': '更换包装材料或形态需要重新估价，请说明是否允许调整预算；原方案已保留'}
    if packaging and not any(re.search(r'粉|白|红|蓝|紫|黄|绿|香槟|颜色|色系|色调', c) for c in changes if '包装' in c or '包材' in c):
        return {'ok': False, 'error': '更换包装材料或形态需要重新估价，请说明要换的材料及是否允许调整预算；原方案已保留'}
    if any(re.search(r'预算|价格|数量|支数|花材|主花|配花|叶材|风格|玫瑰|康乃馨|洋桔梗|洋橘梗|绣球|向日葵', c) for c in changes):
        return None
    allowed = (['color_scheme'] if colors else []) + (['packaging_color'] if packaging else [])
    try:
        response = call_model([
            {'role': 'system', 'content': '只做局部修订，返回 JSON changes 对象，且只包含这些字段：' + ','.join(allowed) + '。color_scheme 为非空颜色字符串数组；packaging_color 仅为颜色名（例如粉色），禁止包含包装材料、形态和做法。不要输出其他字段。'},
            {'role': 'user', 'content': json.dumps({'design': original.get('design'), 'feedback': feedback}, ensure_ascii=False)},
        ], response_format={'type': 'json_object'})
        decoded = json.loads(response.choices[0].message.content)
        patch = decoded.get('changes', decoded) if isinstance(decoded, dict) else None
        if not isinstance(patch, dict) or set(patch) != set(allowed):
            raise ValueError('invalid revision fields')
        for key in allowed:
            value = patch[key]
            if key == 'color_scheme':
                if not isinstance(value, list) or not value or len(value) > 8 or any(not isinstance(x, str) or not x.strip() or len(x) > 30 for x in value):
                    raise ValueError('invalid colors')
            elif not isinstance(value, str) or not value.strip() or len(value) > 20 or re.search(r'纸|纱|盒|篮|袋|塑料|丝绒|缎|皮|玻璃|陶瓷', value):
                raise ValueError('invalid packaging')
        result = copy.deepcopy(original)
        if colors:
            result['design']['color_scheme'] = patch['color_scheme']
        if packaging:
            old_packaging = str(result['design'].get('packaging') or '原包装')
            old_packaging = re.sub(r'；包装颜色：[^；]*', '', old_packaging)
            old_packaging = re.sub(r'(?:香槟|奶白|浅粉|深红|粉|白|红|蓝|紫|黄|绿)色', '', old_packaging)
            result['design']['packaging'] = old_packaging + '；包装颜色：' + patch['packaging_color'].strip()
        result['plan_id'] = 'DIY_' + uuid.uuid4().hex[:12]
        result['version'] = int(original.get('version', 1)) + 1
        result['parent_id'] = original['plan_id']
        for key in ('task_id', 'poll', 'result_url', 'effect_image_url', 'image_url', 'image'):
            result.pop(key, None)
        design = result['design']
        result['effect_prompt'] = effect_prompt(design, result.get('style', ''), design.get('packaging', '花束'))
        result['desc'] = '本版仅调整' + '、'.join('配色' if k == 'color_scheme' else '包装' for k in allowed) + '；花材、支数与原参考报价保持不变。'
        steps = ['按本版用料清单准备花材，保留原方案支数。', '按本版配色搭配花材，完成螺旋扎束。', '按本版包装说明包扎并固定花束。']
        result['diy_steps'] = steps
        design['diy_steps'] = steps
        result['copy_text'] = copy_text(result)
        return result
    except Exception:
        logger.exception('[revise] 局部修订失败，原方案保持不变')
        return {'ok': False, 'error': '本次修改未完成，原方案已保留，请重试'}
