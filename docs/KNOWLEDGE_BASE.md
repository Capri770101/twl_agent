# 知识库维护说明

## 当前域

| 域 | 文件 | 当前条目 | 用途 |
|---|---|---:|---|
| flower | `flowers.json` | 42 | 花材、别名、花语、颜色和养护关联 |
| style | `styles.json` | 12 | 风格、适用场景和视觉特征 |
| scene | `scenes.json` | 24 | 送花场景、对象、语气和推荐约束 |
| pairing | `pairings.json` | 24 | 色彩、结构、花材和包装搭配规则 |
| budget | `budget.json` | 6 | 预算档和花材/包装分配 |
| packaging | `packaging.json` | 7 | 包装风格和适用场景 |
| care | `care.json` | 8 | 醒花、剪根、换水、保鲜和常见问题 |
| colors | `colors.json` | 41 | 色系和颜色语义 |

检索实现：关键词命中保底 + 字符 n-gram TF-IDF；可选 embedding 默认关闭。`knowledge_manifest.json` 是域清单，不是外部数据自动下载器。

## 添加知识

1. 修改对应 JSON，保持 UTF-8、数组顶层和稳定 `id`。
2. 新实体至少有 `id` 和可检索名称/条件；搭配域使用 `condition` / `recommendation`。
3. 补充别名时更新 `synonyms.json`，不要把具体花名随意加入通用同义词组。
4. 运行：

```bash
python scripts/validate_knowledge.py
python scripts/eval_retrieval.py --verbose
pytest -q tests/test_knowledge_retrieval.py tests/test_knowledge_flowers.py tests/test_knowledge_scenes_pairings.py
```

5. 在 `CHANGELOG.md` 记录知识域和评测变化，再合并发布。

## 内容质量要求

- 不把未经核实的价格、库存、营业时间写入知识库；这些必须来自平台实时查询。
- 花语是文化语义，不应写成医学、功效或效果保证。
- 养护建议避免绝对化；涉及有毒植物、过敏和宠物时必须保守表述。
- 每条知识尽量包含适用场景、限制条件和可执行建议，避免只有营销形容词。
