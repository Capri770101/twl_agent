"""花艺方案的 DIY 步骤、养护与寄语，均只依赖已确定的方案数据。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger('tools')
_DIY_STEPS_MAX = 10


def _cap_steps(steps: Any) -> list[str]:
    """清洗模型给出的 DIY 步骤，限制最多十条。"""
    if not isinstance(steps, list):
        return []
    out = [str(s).strip() for s in steps if str(s).strip()]
    if len(out) > _DIY_STEPS_MAX:
        logger.warning('[tools] DIY 步骤 %d 条超出上限 %d，已截断', len(out), _DIY_STEPS_MAX)
        out = out[:_DIY_STEPS_MAX]
    return out


def _build_diy_steps(main: list[dict], fillers: list[dict], foliage: list[dict], color_scheme: list[str], packaging: dict | None) -> list[str]:
    """生成基于本方案真实花材和包装的分步插花指引。"""
    m = [f['name'] for f in main if f]
    f1 = [f['name'] for f in fillers if f]
    f2 = [f['name'] for f in foliage if f]
    pk_name = packaging['name'] if packaging else '花束'
    pk_desc = packaging.get('description', '') if packaging else ''
    colors = '/'.join(color_scheme) or '自然色系'
    trim_m = '；'.join(f'{n}斜剪 45° 并去下半叶' for n in m) or '玫瑰斜剪 45° 并去下半叶'
    trim_f = '；'.join(f'{n}短剪保留 1/2 长度' for n in f1) or '满天星短剪成簇'
    trim_g = '；'.join(f'{n}留长 2/3 做托底勾边' for n in f2) or '尤加利留长做托底'
    if not f1 and not f2:
        step3 = '填充层次：本束为单一花材，不额外搭配配材与叶材；各支主花交错分布、花头朝向一致并留出空隙，即可形成饱满层次。'
        step4 = f'配色比例：单一花材按色系 {colors} 统一色调，可同色系深浅过渡，避免拼入杂色。'
        outer = '整理外轮廓'
    else:
        step3 = f'填充层次：{trim_f} 填补空隙，{trim_g} 勾边制造空气感，形成前低后高、疏密有致。'
        step4 = f'配色比例：按色系 {colors} 控制主花:配材 ≈ 7:3，避免头重脚轻或色彩打架。'
        outer = '整理外层叶材外扩'
    return [f"1. 备材处理（修剪）：{trim_m}。其中{('玫瑰需去刺' if any('玫瑰' in n for n in m) else '无刺花材无需去刺')}，{('百合需摘除雄蕊防染色' if any('百合' in n for n in m) else '无需特殊处理')}。", '2. 定高构图：以主花为视觉重心，整体高度约为花束/花器的 1.5 倍；先插主花确定骨架与朝向，各主花交错分布、花头朝向一致。', step3, step4, f'5. 包装收尾：用「{pk_name}」（{pk_desc}）螺旋扎制并{outer}，丝带/韩素纸收尾，如有贺卡随花附赠。', '6. 醒花养护：完成后深水醒花 2-4 小时再摆放，详见「养护建议」。']


def _build_care_tips(main: list[dict]) -> str:
    """生成通用养护建议及主花特例。"""
    names = {f['name'] for f in main if f}
    tips = ['收到后斜剪根部 45°，深水醒花 2-4 小时再入瓶；', '每日换水并清洗花茎切口，花瓶水位保持 2/3；', '远离空调出风口与阳光直射，可延长花期 3-7 天。']
    if '百合' in names:
        tips.append('百合：摘除雄蕊避免花粉染色衣物，花蕊变褐及时剪去。')
    if '绣球' in names:
        tips.append('绣球：喜水，可整支浸入水中 1-2 小时急救脱水；花头可轻柔喷水。')
    if '向日葵' in names:
        tips.append('向日葵：花头重，建议浅水位并支托花茎，防止垂头。')
    return ''.join(tips)


def _build_card_message(recipient: str, occasion_phrase: str, style_label: str, tone: str, short_meaning: str) -> str:
    """生成可复用的贺卡寄语（场景基调优先）。"""
    base = f'致{recipient}：{occasion_phrase}之际，送上这束{style_label}花束，'
    if tone:
        return base + f'愿它替我传递「{tone}」。'
    return base + f'愿它替我传递{short_meaning}。'
