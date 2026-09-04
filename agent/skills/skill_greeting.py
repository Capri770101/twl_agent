"""skills/skill_greeting.py —— 电子贺卡技能（预设祝福语 + 模板合成贺卡图）。

场景定位（2026-09 新增）：
- 买花送人常要配一张贺卡；下单成功后由智能体主动引导，用户也可随时要求生成；
- 祝福语由**内置情景词库**提供候选（收卡人 × 场合 × 语气匹配），不依赖 LLM 现场编
  文案，也支持用户自定义；选定后渲染成**电子贺卡图**（PNG），供前端在订单页/对话流展示。

渲染方式：Pillow 模板合成（无外部生图依赖、文字清晰、秒级同步出图）。
- 画布默认 900×1200（竖版贺卡），内置 5 套模板：warm / blush / green / letter / night；
- 中文字体自动探测（配置 CARD_FONT_PATH 或常见系统路径；Docker 镜像已装 fonts-wqy-microhei）；
- 结果经 backend.storage.object_store.save_generated 写入 data/generated/，
  image_url 形如 /generated/greet_*.png（可配 IMAGE_PUBLIC_BASE_URL 转公网 CDN 前缀）。

契约（与 agent/engine/ui_protocol.py 的 greeting_card 一致）：
- render_greeting_card 返回 {image_url, text, recipient, sender, template, note}，
  渲染器把它原样作为 data 输出 ui="greeting_card"，前端直接展示大图 + 文案，
  并提供「换模板 / 改文案重做」按钮（回传新一轮对话触发再次渲染）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from agent.toolkit import register_tool
from backend.config import settings

logger = logging.getLogger('skills.greeting')


# --------------------------------------------------------------------------- #
# 一、预设祝福语词库（收卡人 × 场合 × 语气）
# --------------------------------------------------------------------------- #
# 条目字段：text 祝福语；r 适用收卡人组；o 适用场合组；s 语气标签。
# 组名采用英文标识，避免中文别名导致的匹配分裂；用户输入中文时先归一化再匹配。

_RECIPIENT_GROUPS = ('mother', 'father', 'lover', 'friend', 'teacher', 'colleague', 'elder', 'kid', 'generic')
_OCCASION_GROUPS = (
    'birthday', 'mothers_day', 'fathers_day', 'valentine', 'qixi', 'new_year', 'mid_autumn',
    'teachers_day', 'thanks', 'apology', 'condolence', 'get_well', 'promotion', 'proposal',
    'wedding', 'graduation', 'generic',
)
_STYLE_GROUPS = ('warm', 'literary', 'playful', 'formal', 'deep')

_RECIPIENT_ALIASES: dict[str, list[str]] = {
    'mother': ['妈妈', '母亲', '妈', '老妈', '娘', '母上', 'mom', 'mother'],
    'father': ['爸爸', '父亲', '爸', '老爸', '爹', 'dad', 'father'],
    'lover': ['女朋友', '男朋友', '女友', '男友', '老婆', '老公', '妻子', '丈夫', '媳妇', '爱人', '对象', '恋人', '亲爱的', '太太', '先生', '老伴'],
    'friend': ['朋友', '闺蜜', '兄弟', '哥们', '姐妹', '好友', '室友', '同学', 'friend'],
    'teacher': ['老师', '导师', 'teacher'],
    'colleague': ['同事', '领导', '上司', '老板', '客户', '经理', '搭档'],
    'elder': ['奶奶', '外婆', '姥姥', '爷爷', '外公', '姥爷', '长辈', '姑妈', '姨妈', '叔叔', '舅舅'],
    'kid': ['孩子', '女儿', '儿子', '侄女', '侄子', '外甥', '小朋友', '宝贝'],
}
_OCCASION_ALIASES: dict[str, list[str]] = {
    'birthday': ['生日', '生辰', 'birthday'],
    'mothers_day': ['母亲节', '妈妈节'],
    'fathers_day': ['父亲节', '爸爸节'],
    'valentine': ['情人节', '七夕', '520', '白色情人节', '纪念日'],
    'qixi': ['七夕', '七姐诞'],
    'new_year': ['新年', '春节', '元旦', '过年', '除夕'],
    'mid_autumn': ['中秋', '中秋节', '月饼'],
    'teachers_day': ['教师节'],
    'thanks': ['感谢', '谢谢', '感恩'],
    'apology': ['道歉', '对不起', '赔罪', '认错'],
    'condolence': ['慰问', '探望', '生病', '住院', '节哀', '丧事', '丧礼'],
    'get_well': ['康复', '早日康复', '出院', '养病'],
    'promotion': ['升职', '升迁', '晋升', '入职', '新工作'],
    'proposal': ['求婚'],
    'wedding': ['结婚', '婚礼', '新婚', '喜宴'],
    'graduation': ['毕业', '毕业典礼'],
}
_STYLE_ALIASES: dict[str, list[str]] = {
    'warm': ['温馨', '温暖', '贴心', '暖'],
    'literary': ['文艺', '诗意', '清新', '淡雅'],
    'playful': ['俏皮', '幽默', '可爱', '逗', '轻松'],
    'formal': ['正式', '庄重', '商务', '得体'],
    'deep': ['深情', '浪漫', '感人', '走心'],
}


def _norm(value: str, aliases: dict[str, list[str]], default: str) -> str:
    """把中文/英文关键词归一化为组名；未命中返回 default。"""
    v = (value or '').strip().lower()
    if not v:
        return default
    for group, keys in aliases.items():
        if v in keys or any(k in v for k in keys):
            return group
    return default


def _norm_recipient(v: str) -> str:
    return _norm(v, _RECIPIENT_ALIASES, 'generic')


def _norm_occasion(v: str) -> str:
    # valentine 与 qixi 关键词重叠：先精确匹配 qixi 再 fallback valentine
    if any(k in (v or '').lower() for k in ('七夕',)):
        return 'qixi'
    return _norm(v, _OCCASION_ALIASES, 'generic')


def _norm_style(v: str) -> str:
    return _norm(v, _STYLE_ALIASES, '')


_GREETING_LIB: list[dict[str, Any]] = [
    # ── 妈妈 / 母亲 ──
    {'r': ['mother'], 'o': ['birthday'], 's': ['warm'],
     'text': '妈妈，生日快乐！愿岁月对您温柔，愿您笑靥常在。这束花像您给我的爱，不惊艳却一直暖到心底。'},
    {'r': ['mother'], 'o': ['birthday'], 's': ['literary'],
     'text': '从前您牵着我的手看花开，如今换我捧花来谢您。妈妈，生辰快乐，安康常伴。'},
    {'r': ['mother'], 'o': ['mothers_day'], 's': ['warm'],
     'text': '妈妈，母亲节快乐！谢谢您用半生辛劳，换我一路繁花。今天换我来宠您。'},
    {'r': ['mother'], 'o': ['mothers_day'], 's': ['deep'],
     'text': '全世界都在催我长大，只有您心疼我的小翅膀。妈妈，我爱您，母亲节快乐。'},
    {'r': ['mother'], 'o': ['generic'], 's': ['warm'],
     'text': '妈妈，愿这束花带去我的牵挂——您在家好好吃饭，别总舍不得。'},
    # ── 爸爸 / 父亲 ──
    {'r': ['father'], 'o': ['birthday'], 's': ['warm'],
     'text': '老爸，生日快乐！您话不多，爱却很满。这束花替我抱抱您。'},
    {'r': ['father'], 'o': ['fathers_day'], 's': ['warm'],
     'text': '爸爸，父亲节快乐！您是我的山，也是我的伞。愿您少操心，多享福。'},
    {'r': ['father'], 'o': ['fathers_day'], 's': ['deep'],
     'text': '小时候骑在您肩上看世界，长大了才懂您的沉默有多重。爸，节日快乐。'},
    {'r': ['father'], 'o': ['generic'], 's': ['literary'],
     'text': '父爱如山，不常言说却一直都在。爸，愿您岁岁平安。'},
    # ── 恋人 / 伴侣 ──
    {'r': ['lover'], 'o': ['valentine'], 's': ['deep'],
     'text': '遇见你之后，日子都沾了花香。七夕快乐，我的余生只想和你慢慢过。'},
    {'r': ['lover'], 'o': ['valentine'], 's': ['playful'],
     'text': '别人晒花晒礼物，我只想晒你。情人节快乐，你比花好看多了！'},
    {'r': ['lover'], 'o': ['birthday'], 's': ['deep'],
     'text': '生日快乐，我的爱人。愿年年岁岁花相似，岁岁年年人相同——身边是你。'},
    {'r': ['lover'], 'o': ['birthday'], 's': ['warm'],
     'text': '宝贝生日快乐！这一束花，替我陪在你身边，替我每天说晚安。'},
    {'r': ['lover'], 'o': ['generic'], 's': ['literary'],
     'text': '世间温柔皆草木，唯有你是人间情诗。'},
    {'r': ['lover'], 'o': ['apology'], 's': ['deep'],
     'text': '惹你生气了，是我不好。这束花替我低头认错——原谅我好吗？'},
    {'r': ['lover'], 'o': ['proposal'], 's': ['deep'],
     'text': '遇见你之前，花是花；遇见你之后，花都是想送给你的话。嫁给我好吗？'},
    {'r': ['lover'], 'o': ['wedding'], 's': ['warm'],
     'text': '新婚快乐！愿你们把日子过成花，把彼此宠成孩子。'},
    # ── 朋友 ──
    {'r': ['friend'], 'o': ['birthday'], 's': ['warm'],
     'text': '生日快乐，我亲爱的朋友！愿你想要的都拥有，得不到的都释怀。'},
    {'r': ['friend'], 'o': ['birthday'], 's': ['playful'],
     'text': '又老一岁啦！不过没关系，你永远是我心里最好看的那个（之一）。生日快乐！'},
    {'r': ['friend'], 'o': ['get_well'], 's': ['warm'],
     'text': '好好养病，别硬扛。等你满血复活，我们火锅撸串走起！'},
    {'r': ['friend'], 'o': ['graduation'], 's': ['warm'],
     'text': '毕业快乐！愿此去前程似锦，再相逢依旧如故。'},
    {'r': ['friend'], 'o': ['new_year'], 's': ['warm'],
     'text': '新年快乐！愿新岁的风都温柔，愿你的笑都发自真心。'},
    {'r': ['friend'], 'o': ['thanks'], 's': ['warm'],
     'text': '谢谢你一直在。这束花很小，装不下我全部的感谢，那就先谢一半，另一半记在心里。'},
    {'r': ['friend'], 'o': ['generic'], 's': ['literary'],
     'text': '山海不足重，重在遇知己。愿你被这个世界温柔以待。'},
    # ── 老师 ──
    {'r': ['teacher'], 'o': ['teachers_day'], 's': ['formal'],
     'text': '老师，教师节快乐！桃李不言，下自成蹊。感谢您的教诲，愿您桃李满天下，身体常安康。'},
    {'r': ['teacher'], 'o': ['teachers_day'], 's': ['warm'],
     'text': '老师辛苦了！您种下的种子，已在他乡开成了花。教师节快乐！'},
    {'r': ['teacher'], 'o': ['thanks'], 's': ['formal'],
     'text': '感谢您当年的提点与包容。师恩难忘，愿您一切都好。'},
    # ── 同事 / 领导 ──
    {'r': ['colleague'], 'o': ['promotion'], 's': ['formal'],
     'text': '祝贺升职！实至名归。愿新岗位大展宏图，前程似锦。'},
    {'r': ['colleague'], 'o': ['birthday'], 's': ['warm'],
     'text': '生日快乐！工作再忙也要记得好好吃饭，愿新的一岁万事顺意。'},
    {'r': ['colleague'], 'o': ['thanks'], 's': ['formal'],
     'text': '这段时间承蒙关照，辛苦了。小小花束聊表谢意，愿合作愉快。'},
    {'r': ['colleague'], 'o': ['condolence'], 's': ['formal'],
     'text': '得知您身体抱恙，甚为挂念。望安心休养，早日康复归来。'},
    # ── 长辈（祖辈/亲属）──
    {'r': ['elder'], 'o': ['birthday'], 's': ['warm'],
     'text': '祝您福如东海，寿比南山！愿您身体硬朗，笑口常开，我们常回来看您。'},
    {'r': ['elder'], 'o': ['new_year'], 's': ['warm'],
     'text': '新年好！愿您岁岁平安，身体康健，儿孙绕膝享清福。'},
    {'r': ['elder'], 'o': ['mid_autumn'], 's': ['warm'],
     'text': '中秋快乐！月圆人圆事事圆。愿您安康喜乐，常享天伦。'},
    {'r': ['elder'], 'o': ['get_well'], 's': ['warm'],
     'text': '您要快快好起来，我们都惦记着您呢。愿早日康复，精神矍铄。'},
    # ── 孩子 ──
    {'r': ['kid'], 'o': ['birthday'], 's': ['playful'],
     'text': '小寿星生日快乐！愿你眼里有光、心中有糖，慢慢长大，一直快乐！'},
    {'r': ['kid'], 'o': ['graduation'], 's': ['warm'],
     'text': '毕业快乐！愿你去更大的世界闯荡，累了记得家里有花有饭有我们。'},
    # ── 泛场景兜底 ──
    {'r': ['generic'], 'o': ['birthday'], 's': ['warm'],
     'text': '生日快乐！愿新的一岁，笑口常开，好事花生（发生）。'},
    {'r': ['generic'], 'o': ['new_year'], 's': ['warm'],
     'text': '新年快乐！愿所求皆如愿，所行皆坦途。'},
    {'r': ['generic'], 'o': ['valentine'], 's': ['deep'],
     'text': '花会凋谢，心意不会。愿这份浪漫，你恰好接住。'},
    {'r': ['generic'], 'o': ['thanks'], 's': ['formal'],
     'text': '千言万语汇成一句感谢。愿这束花带去我最诚挚的谢意。'},
    {'r': ['generic'], 'o': ['apology'], 's': ['warm'],
     'text': '是我考虑不周，抱歉让你难过了。愿这束花替我道一声：对不起。'},
    {'r': ['generic'], 'o': ['get_well'], 's': ['warm'],
     'text': '愿你早日康复。花开了，你也要好起来呀。'},
    {'r': ['generic'], 'o': ['wedding'], 's': ['warm'],
     'text': '新婚快乐，百年好合！愿往后余生，冷暖有相知，喜乐有分享。'},
    {'r': ['generic'], 'o': ['generic'], 's': ['literary'],
     'text': '愿你三冬暖，愿你春不寒，愿你天黑有灯，下雨有伞。'},
    {'r': ['generic'], 'o': ['generic'], 's': ['warm'],
     'text': '花寄心语，见字如面。愿你收下这份美好的祝愿。'},
    {'r': ['generic'], 'o': ['generic'], 's': ['formal'],
     'text': '谨以这束花，致以最诚挚的祝福。愿您诸事顺遂，平安喜乐。'},
]


def _match_greetings(recipient_group: str, occasion_group: str, style: str, count: int = 4) -> list[dict[str, Any]]:
    """按收卡人 × 场合匹配词库；场合未命中时放宽为仅收卡人/兜底，保证必有候选。"""
    if occasion_group != 'generic' and recipient_group != 'generic':
        hits = [g for g in _GREETING_LIB if recipient_group in g['r'] and occasion_group in g['o']]
        if len(hits) >= count:
            return hits[:count]
    rec_hits = [g for g in _GREETING_LIB if recipient_group in g['r']] if recipient_group != 'generic' else []
    occ_hits = [g for g in _GREETING_LIB if occasion_group in g['o']] if occasion_group != 'generic' else []

    def score(g: dict[str, Any]) -> tuple[int, int]:
        s = 0
        if recipient_group in g['r']:
            s += 2
        elif 'generic' not in g['r']:
            s += 1
        if occasion_group in g['o']:
            s += 2
        elif 'generic' not in g['o']:
            s += 1
        style_bonus = 1 if style and style in g['s'] else 0
        return (s, style_bonus)

    pool = rec_hits + occ_hits + [g for g in _GREETING_LIB if g['r'] == ['generic'] and g['o'] == ['generic']]
    dedup: dict[str, dict[str, Any]] = {}
    for g in pool:
        dedup.setdefault(g['text'], g)
    ranked = sorted(dedup.values(), key=score, reverse=True)
    return ranked[:count]


# --------------------------------------------------------------------------- #
# 二、工具 1：suggest_greetings —— 返回候选祝福语
# --------------------------------------------------------------------------- #
_TEMPLATE_HINT = {
    'warm': '温暖奶油风：米金渐变、暖色小花，适合家人/温馨场合',
    'blush': '淡粉花枝风：粉白渐变、花枝点缀，适合恋人/闺蜜',
    'green': '墨绿金边风：深绿金线、稳重雅致，适合长辈/正式',
    'letter': '复古信笺风：米黄纸+横线+火漆印，适合书信体祝福',
    'night': '暮紫深情风：深紫渐变、星点装饰，适合深情告白',
}


@register_tool(name='suggest_greetings',
               description='为电子贺卡推荐预设祝福语：按收卡人×场合×语气返回多条候选（内置情景词库，无需联网/生图）。用户下单成功或说「配张贺卡/写句祝福」时先调它拿候选给用户挑；用户也可直接给自定义文案跳过本工具。',
               parameters={'type': 'object', 'properties': {
                   'recipient': {'type': 'string', 'description': '收卡人，如 妈妈/女朋友/老师/朋友（可空，由场景推断）'},
                   'occasion': {'type': 'string', 'description': '场合，如 生日/情人节/母亲节/道歉/慰问/新年（可空）'},
                   'style': {'type': 'string', 'description': '可选语气：温馨/文艺/俏皮/正式/深情'},
                   'count': {'type': 'integer', 'description': '返回候选条数，默认 4，最多 6'},
               }, 'required': ['recipient', 'occasion']},
               inject_context=True, tags=['greeting', 'card'])
async def suggest_greetings(recipient: str = '', occasion: str = '', style: str = '', count: int = 4, _context: dict | None = None) -> str:
    """返回候选祝福语列表（每条附适用备注），供用户挑选或直接采用。"""
    rg, og = _norm_recipient(recipient), _norm_occasion(occasion)
    sg = _norm_style(style)
    count = max(1, min(int(count or 4), 6))
    cands = _match_greetings(rg, og, sg, count)
    out: list[dict[str, Any]] = []
    for i, c in enumerate(cands, 1):
        note = f"适配「{recipient or '通用'} × {occasion or '通用'}」" if rg != 'generic' or og != 'generic' else '通用祝福'
        out.append({'id': f'g{i}', 'text': c['text'], 'style': '、'.join(c['s']), 'note': note})
    return json.dumps({
        'recipient_group': rg, 'occasion_group': og, 'style_group': sg,
        'recipient_input': recipient, 'occasion_input': occasion,
        'candidates': out,
        'tip': '把候选序号/文案展示给用户挑；用户选定或自定义文案后调用 render_greeting_card 渲染成贺卡图。',
    }, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 三、渲染：Pillow 模板合成电子贺卡
# --------------------------------------------------------------------------- #
_FALLBACK_FONT_PATHS = [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/simhei.ttf',
    'C:/Windows/Fonts/simsun.ttc',
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
]

_TEMPLATES: dict[str, dict[str, Any]] = {
    'warm':   {'bg_top': (253, 246, 236), 'bg_bottom': (246, 224, 187), 'frame': (184, 134, 11), 'ink': (74, 56, 38), 'accent': (226, 154, 154), 'sub': (150, 118, 76)},
    'blush':  {'bg_top': (255, 240, 245), 'bg_bottom': (248, 200, 220), 'frame': (212, 121, 143), 'ink': (110, 52, 70), 'accent': (255, 255, 255), 'sub': (176, 110, 128)},
    'green':  {'bg_top': (15, 43, 34), 'bg_bottom': (30, 74, 58), 'frame': (212, 175, 55), 'ink': (245, 240, 225), 'accent': (212, 175, 55), 'sub': (176, 196, 178)},
    'letter': {'bg_top': (251, 245, 230), 'bg_bottom': (240, 224, 188), 'frame': (160, 82, 45), 'ink': (66, 48, 34), 'accent': (190, 120, 80), 'sub': (148, 118, 86)},
    'night':  {'bg_top': (27, 27, 47), 'bg_bottom': (58, 45, 95), 'frame': (201, 168, 106), 'ink': (236, 231, 245), 'accent': (201, 168, 106), 'sub': (166, 156, 190)},
}

_TEMPLATE_ALIASES = {
    'warm': ['warm', '温暖', '奶油', '暖', '米色'],
    'blush': ['blush', '粉', '粉色', '樱花', '淡粉'],
    'green': ['green', '绿', '墨绿', '金边', '复古绿'],
    'letter': ['letter', '信笺', '复古', '纸', '书信'],
    'night': ['night', '夜', '紫', '暮色', '深情'],
}
# 不同场合的默认模板
_OCCASION_DEFAULT_TEMPLATE = {
    'birthday': 'blush', 'mothers_day': 'warm', 'fathers_day': 'warm', 'valentine': 'night',
    'qixi': 'night', 'thanks': 'letter', 'apology': 'letter', 'condolence': 'green',
    'get_well': 'warm', 'proposal': 'night', 'wedding': 'blush', 'new_year': 'warm',
    'mid_autumn': 'letter', 'teachers_day': 'letter', 'promotion': 'green', 'graduation': 'blush',
}


def _resolve_template(name: str, occasion: str) -> str:
    if not name:
        name = 'warm'
    for tid, keys in _TEMPLATE_ALIASES.items():
        if (name or '').strip().lower() in keys or any(k in (name or '') for k in keys):
            return tid
    og = _norm_occasion(occasion)
    return _OCCASION_DEFAULT_TEMPLATE.get(og, 'warm')


def _find_font_path() -> str:
    """探测可用的中文字体路径；找不到抛出带指引的 RuntimeError。"""
    candidates = []
    cfg = (settings.CARD_FONT_PATH or '').strip() or (os.getenv('CARD_FONT_PATH') or '').strip()
    if cfg:
        candidates.append(cfg)
    candidates += _FALLBACK_FONT_PATHS
    import os as _os
    for p in candidates:
        if p and _os.path.exists(p):
            return p
    raise RuntimeError(
        '未找到中文字体，无法渲染贺卡：请配置 CARD_FONT_PATH 指向系统中文字体文件'
        '（如文泉驿微米黑 / 微软雅黑），Docker 部署已内置 fonts-wqy-microhei。'
    )


def _load_font(size: int) -> Any:
    from PIL import ImageFont
    path = _find_font_path()
    try:
        return ImageFont.truetype(path, size=size)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[greeting] ttc index 0 加载失败，重试 index 0/1: %s', exc)
        for index in (0, 1):
            try:
                return ImageFont.truetype(path, size=size, index=index)
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f'字体加载失败: {path}') from exc


def _wrap_text(text: str, font: Any, max_width: int) -> list[str]:
    """按字符折行（中文逐字；英文单词尽量不断开，超宽再逐字）。"""
    lines: list[str] = []
    for raw in str(text).replace('\r', '').split('\n'):
        if not raw:
            lines.append('')
            continue
        cur = ''
        for ch in raw:
            if ch == ' ' and cur and _text_width(cur + ch, font) > max_width:
                lines.append(cur)
                cur = ''
                continue
            if cur and _text_width(cur + ch, font) > max_width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
    return lines or ['']


def _text_width(text: str, font: Any) -> int:
    box = font.getbbox(text)
    return (box[2] - box[0]) if box else 0


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Any:
    """先生成 1×N 小渐变再放大，避免逐像素填充。"""
    from PIL import Image
    w, h = size
    small = Image.new('RGB', (1, max(2, h)), top)
    for y in range(max(2, h)):
        t = y / max(1, h - 1)
        small.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return small.resize((w, h))


def _draw_blossom(draw: Any, cx: float, cy: float, r: float, petal: tuple[int, int, int], center: tuple[int, int, int] = (255, 224, 130), alpha: int = 200) -> None:
    """简笔五瓣小花（纯几何绘制，不依赖字形）。"""
    import math
    from PIL import ImageDraw
    overlay = ImageDraw.Draw(draw._image, 'RGBA') if hasattr(draw, '_image') else None
    target = overlay or draw
    for i in range(5):
        ang = math.pi * 2 * i / 5 - math.pi / 2
        px = cx + math.cos(ang) * r * 0.62
        py = cy + math.sin(ang) * r * 0.62
        target.ellipse([px - r, py - r * 0.82, px + r, py + r * 0.82], fill=petal + (alpha,))
    target.ellipse([cx - r * 0.34, cy - r * 0.34, cx + r * 0.34, cy + r * 0.34], fill=center + (alpha,))


def _draw_stem_leaf(draw: Any, x: float, y: float, length: float, leaf: tuple[int, int, int], alpha: int = 160) -> None:
    """一条弧茎 + 一片叶（几何近似），用于点缀角落。"""
    import math
    from PIL import ImageDraw
    target = ImageDraw.Draw(draw._image, 'RGBA') if hasattr(draw, '_image') else draw
    pts = []
    for t in range(0, 21):
        tt = t / 20
        px = x + length * tt
        py = y - math.sin(tt * math.pi) * length * 0.18
        pts.append((px, py))
    target.line(pts, fill=(90, 130, 70, alpha), width=3)
    target.ellipse([x + length * 0.55, y - length * 0.24, x + length * 0.95, y - length * 0.02], fill=leaf + (alpha,))


def _fit_and_draw_text(draw: Any, text: str, area: tuple[int, int, int, int], ink: tuple[int, int, int], max_size: int, min_size: int, line_ratio: float, anchor_center: bool = True) -> tuple[str, int, list[str]]:
    """在区域内自适应字号居中绘制多行正文；返回(实际文本,字号,行列表)。"""
    from PIL import ImageDraw
    x0, y0, x1, y1 = area
    max_w = x1 - x0
    max_h = y1 - y0
    size = max_size
    font = _load_font(size)
    lines = _wrap_text(text, font, max_w)
    while size > min_size:
        line_h = int(size * line_ratio)
        if len(lines) * line_h <= max_h:
            break
        size -= 2
        font = _load_font(size)
        lines = _wrap_text(text, font, max_w)
    line_h = int(size * line_ratio)
    if len(lines) * line_h > max_h:
        # 仍然放不下：截断并追加省略号
        ell = '…'
        cut = lines
        while cut and len(cut) * line_h > max_h:
            cut = cut[:-1]
        lines = cut or ['']
        if lines and lines[-1]:
            head = lines[-1]
            while head and _text_width(head + ell, font) > max_w:
                head = head[:-1]
            lines[-1] = head + ell
        text = '\n'.join(lines)
    total_h = len(lines) * line_h
    y = y0 + max(0, (max_h - total_h) // 2)
    for ln in lines:
        w = _text_width(ln, font)
        lx = x0 + (max_w - w) // 2 if anchor_center else x0
        draw.text((lx, y), ln, font=font, fill=ink)
        y += line_h
    return text, size, lines


def _render_card_image(text: str, recipient: str, sender: str, template: str) -> dict[str, Any]:
    """Pillow 渲染贺卡 PNG，返回 bytes 供落盘。"""
    from PIL import Image, ImageDraw

    w, h = settings.CARD_WIDTH or 900, settings.CARD_HEIGHT or 1200
    tpl = _TEMPLATES[template]
    img = Image.new('RGBA', (w, h))
    bg = _vertical_gradient((w, h), tpl['bg_top'], tpl['bg_bottom'])
    img.paste(bg, (0, 0))

    layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # 外框细线
    m = 42
    draw.rounded_rectangle([m, m, w - m, h - m], radius=28, outline=tpl['frame'] + (215,), width=3)
    draw.rounded_rectangle([m + 16, m + 16, w - m - 16, h - m - 16], radius=18, outline=tpl['frame'] + (110,), width=1)

    # 顶部角花 + 花枝点缀（左上 2 朵 + 右下 1 朵）
    _draw_stem_leaf(draw, 92, 118, 96, (90, 130, 70))
    _draw_blossom(draw, 150, 108, 15, tpl['accent'])
    _draw_blossom(draw, 208, 96, 11, tpl['accent'])
    _draw_blossom(draw, w - 140, h - 150, 15, tpl['accent'])

    # 称呼（顶部居中）
    ink = tpl['ink']
    sub = tpl['sub']
    y_cursor = 210
    if recipient:
        r_text = recipient.strip()
        if len(r_text) > 24:
            r_text = r_text[:24]
        r_size = 52 if len(r_text) <= 8 else (44 if len(r_text) <= 14 else 36)
        r_font = _load_font(r_size)
        rw = _text_width(r_text, r_font)
        draw.text(((w - rw) / 2, y_cursor), r_text, font=r_font, fill=ink)
        y_cursor += int(r_size * 1.6)

    # 分隔装饰：细线 + 中央小花
    div_y = y_cursor + 10
    draw.line([(w / 2 - 130, div_y), (w / 2 - 26, div_y)], fill=tpl['frame'] + (150,), width=1)
    draw.line([(w / 2 + 26, div_y), (w / 2 + 130, div_y)], fill=tpl['frame'] + (150,), width=1)
    _draw_blossom(draw, w / 2, div_y, 9, tpl['accent'])

    # 正文区（自适应折行居中）
    body_area = (120, div_y + 40, w - 120, h - 260)
    text_len = len(text)
    body_max = 46 if text_len <= 24 else (40 if text_len <= 60 else (34 if text_len <= 120 else 30))
    body_min = 22
    fitted, used_size, _ = _fit_and_draw_text(draw, text, body_area, ink, body_max, body_min, 1.62)

    # 落款（右下）
    sender_y = h - 150
    if sender:
        s_text = '—— ' + sender.strip()
        if len(s_text) > 30:
            s_text = s_text[:30]
        s_font = _load_font(32 if len(s_text) <= 12 else 26)
        sw = _text_width(s_text, s_font)
        draw.text((w - 110 - sw, sender_y), s_text, font=s_font, fill=ink)

    # 底部水印小字
    foot = '以花传情 · 跳舞兰'
    f_font = _load_font(18)
    fw = _text_width(foot, f_font)
    draw.text(((w - fw) / 2, h - 78), foot, font=f_font, fill=sub + (150,))

    img = Image.alpha_composite(img, layer)
    if img.mode != 'RGB':
        img = img.convert('RGB')

    import io
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return {'bytes': buf.getvalue(), 'fitted_text': fitted, 'font_size': used_size}


# --------------------------------------------------------------------------- #
# 四、工具 2：render_greeting_card —— 渲染电子贺卡图
# --------------------------------------------------------------------------- #
@register_tool(name='render_greeting_card',
               description='把选定/自定义的祝福语渲染成电子贺卡图（模板合成，秒级同步返回 image_url，无需轮询）。用户从 suggest_greetings 挑了文案、或直接给了祝福语后调用。卡片会写入 data/generated/ 并由 /generated 静态挂载访问。',
               parameters={'type': 'object', 'properties': {
                   'text': {'type': 'string', 'description': '祝福语正文（必填；支持换行，建议 ≤200 字，超长自动截断）'},
                   'recipient': {'type': 'string', 'description': '称呼，如 亲爱的妈妈 / To 小明（顶部显示，可空）'},
                   'sender': {'type': 'string', 'description': '落款，如 爱你的女儿（右下显示，可空）'},
                   'template': {'type': 'string', 'description': '模板风格：warm 温暖奶油 / blush 淡粉花枝 / green 墨绿金边 / letter 复古信笺 / night 暮紫深情（可空，按场合推断默认）'},
                   'occasion': {'type': 'string', 'description': '场合（可选，未给 template 时用于选默认模板），如 生日/情人节/道歉'},
               }, 'required': ['text']},
               inject_context=True, tags=['greeting', 'card', 'image'])
async def render_greeting_card(text: str, recipient: str = '', sender: str = '', template: str = '', occasion: str = '', _context: dict | None = None) -> str:
    """渲染电子贺卡图；成功返回 greeting_card 数据，失败返回 {error}（不静默）。"""
    text = (text or '').strip()
    if not text:
        return json.dumps({'error': '祝福语为空：请先让用户从 suggest_greetings 候选里选，或提供自定义文案'}, ensure_ascii=False)

    tid = _resolve_template(template, occasion)
    note = ''
    if len(text) > 400:
        text = text[:400]
        note = '祝福语超过 400 字，已截断展示'
    try:
        rendered = await asyncio.to_thread(_render_card_image, text, recipient.strip(), sender.strip(), tid)
    except RuntimeError as exc:
        logger.warning('[greeting] 渲染失败: %s', exc)
        return json.dumps({'error': str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception('[greeting] 渲染异常')
        return json.dumps({'error': f'贺卡渲染失败: {exc}（请确认已安装 Pillow 且配置中文字体）'}, ensure_ascii=False)

    try:
        from backend.storage.object_store import save_generated
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        fname = f'greet_{ts}_{uuid.uuid4().hex[:6]}.png'
        image_url = save_generated(fname, rendered['bytes'])
    except Exception as exc:  # noqa: BLE001
        logger.exception('[greeting] 落盘失败')
        return json.dumps({'error': f'贺卡图片保存失败: {exc}'}, ensure_ascii=False)

    logger.info('[greeting] 贺卡已生成 user=%s template=%s url=%s', (_context or {}).get('user_id', ''), tid, image_url)
    if note:
        note += '；'
    note += f"模板 {tid}（{_TEMPLATE_HINT[tid]}）"
    return json.dumps({
        'image_url': image_url,
        'text': rendered['fitted_text'],
        'recipient': recipient.strip(),
        'sender': sender.strip(),
        'template': tid,
        'note': note,
    }, ensure_ascii=False)
