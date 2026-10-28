# 03 - 评估（Evaluation）

评分方式：**确定性评分器**（unittest 断言）——本任务输出为结构化 JSON 且行为可枚举，笔试范围内不需要 LLM-as-judge（留作下一步，见 05）。

## 测试执行方式

```bash
python -m unittest discover -s tests -v
```

实际运行记录：[examples/test-results.txt](../examples/test-results.txt)（11 个用例全部通过）。

## 用例、预期与实际结果

6 个必须验收点 + 步数边界 + 内容替换鲁棒性 + 缺问题边界 + 2 个 CLI 边界，全部使用隔离临时数据目录，不污染仓库数据：

| # | 验收点 | 输入 | 预期行为 | 实际结果 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | 明确答案 | "年假有几天？" | 检索→读取 D03 原文→依据原文回答 5 天并逐字引用 | 通过：`answered`，quote ∈ content | [normal.output.json](../examples/normal.output.json) / [normal.trace.json](../examples/normal.trace.json) |
| 2 | 文档冲突（参考实例） | "报销期限多久？" | D01 30天 vs D02 15天 → 列出冲突与双方引用，请求确认，**不擅自选答案** | 通过：`needs_confirmation`，answer 为空，conflicts 含双方 stated_value 与逐字引用 | [doc-conflict.output.json](../examples/doc-conflict.output.json) / [doc-conflict.trace.json](../examples/doc-conflict.trace.json) |
| 3 | 资料外问题 | "公司年会什么时候开？" | 改写检索 + 原始问句补充检索各一次，均未命中 → 如实返回缺口 | 通过：`out_of_scope`，恰好 2 次检索，gaps 非空，answer 为空 | [out-of-scope.output.json](../examples/out-of-scope.output.json) / [out-of-scope.trace.json](../examples/out-of-scope.trace.json) |
| 4 | 改写后的相似问题 | "发票多久之内要交？" | 与"报销期限"不同问法，仍命中 D01 → 依据原文回答 30 天 | 通过：`answered`，引用 D01 | [paraphrase.output.json](../examples/paraphrase.output.json) / [paraphrase.trace.json](../examples/paraphrase.trace.json) |
| 5 | 上下文追问 | "那事假呢？"（history 含年假问答） | 结合上文改写检索词 → 依据 D04 回答事假规定 | 通过：`answered`，引用 D04，决策依据写明"结合上文" | [followup.output.json](../examples/followup.output.json) / [followup.trace.json](../examples/followup.trace.json) |
| 6 | 工具失败 | 注入 `search_fail_once=true` | 首次检索 failed → 重试 success → 正常回答；失败步骤保留 failed | 通过：steps 为 failed/success/success | [tool-failure.output.json](../examples/tool-failure.output.json) / [tool-failure.trace.json](../examples/tool-failure.trace.json) |
| 7 | 步数上限 | max_steps=2 | 返回明确 `incomplete` + 上限说明 | 通过 | tests::test_7 |
| 8 | 内容替换鲁棒性（审核要点） | 临时库把 D01 改 45 天并删 D02 | 相似问题改答 45 天；原冲突问题只检索到 D01 → 依据 D01 回答 | 通过：答案与动作随文档内容改变，无硬编码 | tests::test_8 |
| 9 | 缺问题本体 | 无 question | 不调用工具，返回 `missing_input` + 缺口 | 通过 | tests::test_9 |
| 10-11 | CLI 边界 | 合法输入 / 非法 JSON | 退出码 0 + Trace 落盘 / 非零退出码 | 通过 | tests::CliTests |

## 红线断言（所有场景统一检查）

`tests/test_acceptance.py::assert_red_lines` 对每个场景断言：

1. **回答必须有依据**：`answered` 时 citations 非空，且每条 `quote` 逐字属于对应文档的 `content`——无依据回答或错误引用是关键缺陷；
2. **冲突不擅自选答案**：`needs_confirmation` 时 answer 为空、conflicts ≥ 2、每条冲突引用逐字属于原文、请求确认文案非空；
3. **无依据不编造**：`out_of_scope/failed/incomplete/missing_input` 时 answer 为空、citations 为空、gaps 非空；
4. **失败步骤按实际观察记录**（`success/failed`，失败不被改写）；
5. 问题原样回显，未被更改。

## 两条不同执行路径的运行记录

- **路径 A（单文档直答）**：[normal.trace.json](../examples/normal.trace.json) —— 检索只命中 D03 → 读取原文 → 决策依据"依据 D03 原文中的直接陈述回答，无文档冲突"。
- **路径 B（冲突查证）**：[doc-conflict.trace.json](../examples/doc-conflict.trace.json) —— 检索命中 D01+D02 → 逐一读取原文 → 决策依据"多篇文档对同一问题给出不同数值陈述，依据边界列出冲突与引用并请求确认"。

同一 Mock 策略在不同观察下走出不同路径，体现"根据检索结果选择补充检索、读取原文、追问或停止"的动态决策。

## 已知限制

1. **Mock 边界**：examples/ 全部结果来自离线确定性 Mock 模型，只验证 ReAct 流程与工具协议，**不证明真实模型效果**；`AGENT_MODEL=api` 路径（OpenAI 兼容）未随本仓库验证。
2. 检索为关键词 2-gram 重合度打分，无语义向量召回；改写词如果与文档用词完全不同（如纯口语俚语）可能漏检——此时走补充检索并如实返回资料外，不会编造。
3. 冲突判定基于相关句中"数值+单位"陈述的比较；纯文字性冲突（表述矛盾但无数值）不会触发 `needs_confirmation`，会以多篇引用综合回答。
4. 读取原文上限 2 篇（按相关度），超过 2 篇相关文档时的冲突排查覆盖有限。
5. 评分器为确定性检查，缺少 LLM-as-judge 对回答表述质量的评估。
