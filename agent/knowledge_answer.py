"""仅从成功检索的公开知识字段提取答复，不展示原始 JSON。"""
import json


def knowledge_fallback_reply(records):
    for record in reversed(records or []):
        if record.name != 'retrieve_knowledge' or record.status != 'ok':
            continue
        try:
            payload = json.loads(record.result)
        except (ValueError, TypeError):
            continue
        rows = payload.get('results', []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            continue
        facts = []
        for row in rows[:2]:
            if not isinstance(row, dict):
                continue
            for field in ('steps', 'tips', 'rules', 'recommendation', 'note'):
                value = row.get(field, [])
                values = [value] if isinstance(value, str) else value
                if not isinstance(values, list):
                    continue
                for text in values:
                    if isinstance(text, str) and text.strip() and text not in facts:
                        facts.append(text.strip())
                if len(facts) >= 6:
                    break
        if facts:
            return '根据检索资料，可参考以下建议：\n' + '\n'.join(f'{i}. {text}' for i, text in enumerate(facts[:6], 1))
    return '暂时没有检索到足够的相关资料，请补充具体花材或问题。'
