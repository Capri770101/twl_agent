"""识别明确的讨论轮次；不把问题/评价当作执行修改的授权。"""
import re


# 客服/订单语境信号：这类消息里的「怎么样/为什么」是在问订单售后进度，
# 不是在探讨设计方案。误判成讨论轮次会把工具收窄成 CARE_TOOLS，
# query_my_orders / query_my_after_sales 被拦截 → 用户问到退款只能得到
# 「查不到」的兜底话术（2026-10-08 本地联调实测）。
_CUSTOMER_SERVICE_SIGNAL = re.compile(
    r'订单|退款|售后|退了|退货|物流|发货|收货|配送|到货|签收|发货了|送到了|客服|联系商家|联系店家|联系店铺|转人工|人工客服|投诉|催单'
)


def is_plan_discussion(message: str) -> bool:
    text = str(message or '').strip()
    if not text:
        return False
    # 客服/订单语境直接放行：走完整 ReAct（模型自己会选 query_my_orders /
    # query_my_after_sales）。上面那组「怎么样/为什么」讨论信号对这类消息没有区分度。
    if _CUSTOMER_SERVICE_SIGNAL.search(text):
        return False
    # 明确同时要求执行时不拦；去掉“先别改”等否定执行短语再判断。
    remaining = re.sub(r'(?:先)?(?:不要|不用|别|不需要)(?:再)?(?:改方案|修改|改|生成方案|推荐商品)', '', text)
    if re.search(r'(?:请|帮我|直接|就|现在)(?:把)?[^，。？！!?]{0,12}(?:改成|换成|设计|生成|下单|买|推荐)|(?:改成|换成|生成效果图|重新设计)', remaining):
        # “换成白色会不会更好”是在探讨，不是执行；明确“请/直接”仍为执行。
        if not (re.search(r'会不会|是否|怎么样|合适吗|可以吗', remaining)
                and not re.search(r'请|帮我|直接|现在', remaining)):
            return False
    return bool(re.search(r'先聊|先讨论|先别改|不要改|不用改|不需要修改|为什么|为何|区别|怎么(?:算|计算)|怎么算|手工费|贵在哪|贵在哪里|会不会|是否|合适吗|怎么样|有什么讲究|代表什么|是什么意思|这个价格合理吗', text))
