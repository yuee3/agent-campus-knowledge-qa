# 企业知识问答 Agent

小影校园 AI Agent 开发岗笔试 · **题目三：企业知识问答 Agent**。

帮助员工找到**制度或产品问题的可靠答案**：检索知识库 → 读取文档原文 → 依据原文作答并逐字引用；**多篇文档陈述冲突时列出冲突与双方引用、请求确认适用制度，不凭空选一个答案**；知识库没有的资料如实返回缺口，不编造。Agent 基于 ReAct 循环动态决策（补充检索 / 读取原文 / 追问改写 / 冲突查证）。

- 运行方式：CLI（Python 3.10+，零第三方依赖，无需密钥）
- 模型入口：离线确定性 Mock（默认）+ OpenAI 兼容 API（未随仓库验证，边界见下）
- 测试：11 个用例覆盖 6 个必验收点 + 内容替换鲁棒性（审核要点）+ 步数上限 + CLI 边界，全部通过（[examples/test-results.txt](examples/test-results.txt)）

## 快速运行

在仓库根目录执行：

```bash
# 6 个验收场景（输出 JSON 到 stdout）
python -m src.main --input examples/normal.input.json      # 明确答案 -> answered（依据 D03 原文回答年假 5 天，逐字引用）
python -m src.main --input examples/doc-conflict.input.json # 文档冲突 -> needs_confirmation（D01 30天 vs D02 15天，列出冲突与引用请求确认，不擅自选答案）
python -m src.main --input examples/out-of-scope.input.json # 资料外 -> out_of_scope（改写检索 + 原始问句补充检索各一次，如实返回缺口）
python -m src.main --input examples/paraphrase.input.json   # 改写后的相似问题 -> answered（"发票多久之内要交"仍依据 D01 回答 30 天）
python -m src.main --input examples/followup.input.json     # 上下文追问 -> answered（结合上文"年假"话题回答"那事假呢"）
python -m src.main --input examples/tool-failure.input.json # 工具失败 -> answered（注入一次检索失败，重试续跑；失败步骤保留 failed 记录）

# 自动化测试
python -m unittest discover -s tests -v
```

带 Trace 输出（每步含工具参数、观察结果、决策依据）：

```bash
python -m src.main --input examples/doc-conflict.input.json --trace-out examples/my-trace.json
```

约定：程序成功输出 JSON；业务异常（冲突待确认/资料外/工具失败/未完成等）以 JSON 状态返回且退出码 0；输入文件不是合法 JSON 时返回非零退出码。知识库 `data/docs.json` 为只读合成数据，运行不修改它；替换其中的文档内容（如把"30 天"改为"45 天"），答案与动作会随之改变（tests::test_8 验证）。

## 配置

复制 `.env.example` 为 `.env`（可选）。`AGENT_MODEL=mock`（默认）时**无需任何密钥**；`AGENT_MODEL=api` 时需配置 `OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL`。

> **Mock 边界**：Mock 模型是以"任务+历史观察"为输入的确定性策略函数，验证 ReAct 流程与工具协议，**不证明真实模型效果**——检索词改写、补充检索、冲突查证、追问处理与答案引用全部从观察到的文档原文推导，而非按场景硬编码。examples/ 中的全部运行记录均来自 Mock。

## 项目结构

```
├── README.md
├── requirements.txt            # 无第三方依赖声明
├── .env.example                # 变量清单，不含真实密钥
├── src/
│   ├── main.py                 # CLI 入口与输入文件校验
│   ├── agent.py                # ReAct 循环（步数上限/错误恢复/停止条件）
│   ├── model_client.py         # Mock 策略 + OpenAI 兼容 API 双入口
│   ├── tools.py                # 2 个业务工具（文档检索/片段读取，故障注入）
│   ├── prompts.py              # 系统提示词（任务边界/引用纪律/冲突处理/输出契约）
│   └── trace.py                # Trace 记录
├── tests/test_acceptance.py    # 6 验收点 + 红线 + 内容替换鲁棒性 + CLI 边界
├── data/docs.json              # 合成知识库（带文档 ID、版本、日期）
├── examples/                   # 6 组输入/输出/Trace + 测试记录
└── docs/                       # 需求/方案/评估/AI 使用/复盘
```

## 评审导航

| 检查项 | 入口 |
| --- | --- |
| 业务目标与成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 测试预期、实际结果、失败与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助范围与验证方式 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难问题、学习收获、下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 冲突查证与请求确认（参考实例） | [examples/doc-conflict.trace.json](examples/doc-conflict.trace.json) |
| 引用逐字来自原文（红线） | [examples/normal.output.json](examples/normal.output.json) |
| 无依据不编造（资料外） | [examples/out-of-scope.output.json](examples/out-of-scope.output.json) |
| 替换文档内容答案随之改变 | tests/test_acceptance.py::test_8 |

## 提交信息

- 题号：题目三（企业知识问答 Agent）
- 提交物：本仓库全部内容（代码、文档、合成数据、运行记录），不含真实密钥与隐私数据
- 评审可复现：按"快速运行"命令从零执行，结果应与 examples/ 一致（run_id、时间戳除外）
