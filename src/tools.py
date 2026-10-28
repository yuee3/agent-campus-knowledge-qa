"""业务工具层（第一板斧：外部能力入口与边界）。

设计边界：
- Tool 只执行原子动作，不承载完整业务流程；流程编排由 agent.py 的 ReAct 循环完成。
- 题目指定两类工具：search_docs（文档检索）、read_doc（片段读取），均为只读。
- 工具只返回文档事实（标题/版本/日期/原文内容），**不判断文档之间是否冲突**——
  冲突查证、是否足以回答、是否需要追问，全部由模型基于观察完成。
- 检索是关键词重合度打分（模拟检索引擎的语义容错有限性）；引用原文必须通过
  read_doc 读取，snippet 仅用于决策。
- 工具失败注入（search_fail_once）在工具层实现，由输入显式声明，可确定性复现。
"""
from __future__ import annotations

import json
import os

DATA_FILES = {"docs": "docs.json"}

# 工具清单（模型可见的能力入口；同时用于生成 API 模式的 tool schemas）
TOOL_SCHEMAS = [
    {
        "name": "search_docs",
        "description": "按关键词检索合成知识库，返回相关文档摘要列表（含 doc_id、标题、版本、snippet）（只读）",
        "params": {"query": "string，必填，检索关键词或问题"},
    },
    {
        "name": "read_doc",
        "description": "按文档 ID 读取文档原文全文（含版本与日期）（只读）",
        "params": {"doc_id": "string，必填，如 D01"},
    },
]


def _read(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _grams(text: str) -> set:
    """中文 2-gram 切分：模拟检索引擎的关键词匹配能力。"""
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i: i + 2] for i in range(len(cleaned) - 1)}


class ToolRegistry:
    """工具注册表：模型通过名称调用；执行、参数校验、故障注入与审计均在本层完成。"""

    def __init__(self, data_dir: str, inject: dict | None = None) -> None:
        self.data_dir = data_dir
        self.inject = inject or {}
        self._search_calls = 0  # 用于实现"注入一次失败"

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, DATA_FILES[name])

    # ------------------------------------------------------------------
    # 工具实现（每个工具返回一个 JSON 观察结果，异常不外抛）
    # ------------------------------------------------------------------

    def search_docs(self, query: str) -> dict:
        # 注入一次失败（工具失败验收场景）：首次调用失败，重试成功
        if self.inject.get("search_fail_once") and self._search_calls == 0:
            self._search_calls += 1
            return {
                "status": "failed",
                "query": query,
                "error": "transient",
                "message": "检索失败：检索服务临时不可用（注入的单次故障）",
            }
        self._search_calls += 1
        docs = _read(self._path("docs")).get("docs", {})
        q_grams = _grams(query)
        results = []
        for doc_id, doc in docs.items():
            hay = doc.get("title", "") + " " + doc.get("content", "")
            hits = sum(1 for g in q_grams if g in hay)
            title_hits = sum(1 for g in q_grams if g in doc.get("title", ""))
            score = hits + 2 * title_hits
            if score > 0:
                snippet = hay[:60]
                results.append(
                    {
                        "doc_id": doc_id,
                        "title": doc.get("title"),
                        "version": doc.get("version"),
                        "date": doc.get("date"),
                        "snippet": snippet,
                        "score": score,
                    }
                )
        results.sort(key=lambda r: (-r["score"], r["doc_id"]))
        return {"status": "ok", "query": query, "results": results[:3]}

    def read_doc(self, doc_id: str) -> dict:
        docs = _read(self._path("docs")).get("docs", {})
        doc = docs.get(doc_id)
        if doc is None:
            return {
                "status": "doc_not_found",
                "doc_id": doc_id,
                "message": f"知识库中不存在文档 {doc_id}",
            }
        return {
            "status": "ok",
            "doc_id": doc_id,
            "doc": {
                "doc_id": doc_id,
                "title": doc.get("title"),
                "version": doc.get("version"),
                "date": doc.get("date"),
                "content": doc.get("content"),
            },
        }

    # ------------------------------------------------------------------
    # 分发与审计入口
    # ------------------------------------------------------------------

    def execute(self, name: str, args: dict) -> dict:
        """按名称执行工具。任何异常都转为观察结果交回模型（错误恢复），不终止循环。"""
        try:
            if name == "search_docs":
                return self.search_docs(args["query"])
            if name == "read_doc":
                return self.read_doc(args["doc_id"])
            return {"status": "unknown_tool", "message": f"未知工具：{name}"}
        except KeyError as e:
            return {"status": "invalid_args", "message": f"工具 {name} 缺少必填参数: {e}"}
        except Exception as e:  # noqa: BLE001 - 观察结果化是 Loop 错误恢复的一部分
            return {"status": "tool_error", "message": f"工具 {name} 执行异常: {e}"}
