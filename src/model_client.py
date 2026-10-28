"""模型接入层：离线确定性 Mock 与 OpenAI 兼容 API 双入口。

边界声明（试卷要求标明）：
- Mock 模型以"任务 + 历史观察"为输入的策略函数模拟模型决策，只验证 ReAct
  流程与工具协议，不证明真实模型效果；它不是按场景硬编码的脚本——检索词改写、
  补充检索、冲突查证、追问改写、答案与引用全部从观察到的文档原文推导，
  替换文档内容后答案与动作会随之改变。
- API 模式走 OpenAI 兼容 /chat/completions（标准库 urllib 实现），与 Mock
  共用同一 ReAct 循环。本仓库交付时未配置真实 Key，该路径未经过真实模型验证。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS

_MAX_DOCS_TO_READ = 2  # 按相关度最多读取的原文篇数（控制步数）

_PUNCT = "，。！？、：；,.!?:; \n\t（）()《》\"'“”‘’？"
_STOPWORDS = {"那", "呢", "吗", "啊", "的", "了", "有", "和", "还", "想", "问", "请", "请问", "一下", "什么", "怎么", "为什么", "多久", "几天", "多少", "时候"}
_SENT_SPLIT = "。！？；;!?."


def _core_words(text: str) -> str:
    """检索词改写：去标点与停用词，保留连续核心片段（模拟查询改写）。"""
    segments = []
    cur = []
    for ch in text:
        if ch in _PUNCT or ch in _STOPWORDS:
            if cur:
                segments.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        segments.append("".join(cur))
    return " ".join(s for s in segments if s) if segments else text.strip()


def _last(history: list, tool: str):
    for h in reversed(history):
        if h["action"].get("tool") == tool:
            return h["observation"]
    return None


def _last_action(history: list, tool: str):
    for h in reversed(history):
        if h["action"].get("tool") == tool:
            return h["action"]
    return None


def _count(history: list, tool: str) -> int:
    return sum(1 for h in history if h["action"].get("tool") == tool)


def _steps_from_history(history: list) -> list:
    """步骤状态按实际观察记录：成功即 success，失败即 failed，重试后成功保留两条记录。"""
    steps = []
    for h in history:
        obs = h["observation"]
        steps.append(
            {
                "tool": h["action"].get("tool"),
                "query": (h["action"].get("args") or {}).get("query"),
                "doc_id": (h["action"].get("args") or {}).get("doc_id"),
                "status": "success" if obs.get("status") == "ok" else "failed",
            }
        )
    return steps


def _relevant_sentence(content: str, query: str) -> str | None:
    """从原文中提取与查询最相关的句子（引用与答案的事实来源）。"""
    q_grams = _grams_set(query)
    best, best_score = None, 0
    buf = []
    sentences = []
    for ch in content:
        if ch in _SENT_SPLIT:
            if buf:
                sentences.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        sentences.append("".join(buf))
    for sentence in sentences:
        hay = sentence
        score = sum(1 for g in q_grams if g in hay)
        if score > best_score:
            best, best_score = sentence.strip(), score
    return best if best_score > 0 else None


def _grams_set(text: str) -> set:
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i: i + 2] for i in range(len(cleaned) - 1)}


_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([个]?(?:天|日|周|月|年|次|元|小时|分钟|工作日))")


def _stated_value(sentence: str) -> str | None:
    """提取相关句中的“数值+单位”陈述（如 30 天），用于冲突比较。"""
    m = _VALUE_RE.search(sentence)
    if not m:
        return None
    return f"{m.group(1)} {m.group(2).lstrip('个')}"


def _citation(doc: dict, quote: str) -> dict:
    return {
        "doc_id": doc["doc_id"],
        "title": doc["title"],
        "version": doc["version"],
        "date": doc["date"],
        "quote": quote,
    }


class MockModelClient:
    """离线确定性 Mock 模型（AGENT_MODEL=mock，默认）。"""

    def decide(self, task: dict, history: list) -> dict:
        question = (task.get("question") or "").strip()
        raw_history = task.get("history") or []

        # 1) 信息缺失：无问题直接返回缺口，不调用工具
        if not question:
            return {
                "type": "finish",
                "payload": {
                    "status": "missing_input",
                    "message": "缺少必填信息：问题 question，无法开始问答",
                    "answer": None,
                    "citations": [],
                    "conflicts": None,
                    "pending_confirmation": None,
                    "gaps": ["缺少问题 question"],
                    "steps": _steps_from_history(history),
                },
                "basis": "任务缺少问题本体，直接返回未完成与缺口",
            }

        # 2) 工具失败处理：重试一次；仍失败如实终止（保留已成功步骤）
        for tool in ("search_docs", "read_doc"):
            last = _last(history, tool)
            if last is not None and last.get("status") != "ok":
                if last.get("status") == "doc_not_found":
                    return self._finish_incomplete(
                        history, last.get("message", "文档不存在"),
                        f"{last.get('doc_id')} 在知识库中不存在，无法提供依据",
                    )
                if _count(history, tool) < 2:
                    prev = _last_action(history, tool)
                    return {
                        "type": "tool_call",
                        "tool": tool,
                        "args": prev.get("args") or {},
                        "basis": f"{tool} 上次返回失败（{last.get('message')}），"
                        "按失败处理策略重试一次，保留已成功步骤",
                    }
                return {
                    "type": "finish",
                    "payload": {
                        "status": "failed",
                        "message": f"{tool} 重试后仍失败，任务终止",
                        "answer": None,
                        "citations": [],
                        "conflicts": None,
                        "pending_confirmation": None,
                        "gaps": [f"{tool} 连续两次失败，暂无法回答"],
                        "steps": _steps_from_history(history),
                    },
                    "basis": "重试仍失败，按边界终止并如实报告",
                }

        # 3) 未检索：构造检索词（省略式追问结合上文改写）
        search = _last(history, "search_docs")
        if search is None:
            query = self._build_query(question, raw_history)
            return {
                "type": "tool_call",
                "tool": "search_docs",
                "args": {"query": query},
                "basis": self._query_basis(question, raw_history, query),
            }

        # 4) 检索为空：改写补充检索一次；两轮都空则如实返回资料外
        if not search.get("results"):
            if _count(history, "search_docs") < 2:
                return {
                    "type": "tool_call",
                    "tool": "search_docs",
                    "args": {"query": question},
                    "basis": "改写后的检索词未命中，改用原始问句补充检索一次",
                }
            return {
                "type": "finish",
                "payload": {
                    "status": "out_of_scope",
                    "message": "知识库中未找到与问题相关的文档，无法回答",
                    "answer": None,
                    "citations": [],
                    "conflicts": None,
                    "pending_confirmation": None,
                    "gaps": [
                        "知识库中未检索到与该问题相关的制度或 FAQ 文档"
                        "（已尝试改写检索词与原始问句各一次）"
                    ],
                    "steps": _steps_from_history(history),
                },
                "basis": "两轮检索均未命中，如实返回资料外与缺口，不编造答案",
            }

        # 5) 逐篇读取原文（引用必须基于 read_doc 的原文，而非检索摘要）
        results = search.get("results") or []
        to_read = [r["doc_id"] for r in results[:_MAX_DOCS_TO_READ]]
        read_ok = {
            h["observation"]["doc_id"]: h["observation"]["doc"]
            for h in history
            if h["action"].get("tool") == "read_doc" and h["observation"].get("status") == "ok"
        }
        pending_read = next((d for d in to_read if d not in read_ok), None)
        if pending_read is not None:
            rank = to_read.index(pending_read) + 1
            return {
                "type": "tool_call",
                "tool": "read_doc",
                "args": {"doc_id": pending_read},
                "basis": f"按相关度第 {rank} 位读取文档 {pending_read} 原文，"
                "引用与冲突查证必须基于原文而非检索摘要",
            }

        # 6) 原文读取完毕：综合判断——冲突查证，不凭空选答案
        return self._synthesize(question, history, results, read_ok)

    # ------------------------------------------------------------------
    # 合成阶段：答案与冲突判定全部从读取的原文推导（替换文档内容即随之改变）
    # ------------------------------------------------------------------

    def _synthesize(self, question: str, history: list, results: list, read_ok: dict) -> dict:
        query = _last_action(history, "search_docs").get("args", {}).get("query", question)
        analyzed = []
        for doc_id, doc in read_ok.items():
            sentence = _relevant_sentence(doc.get("content", ""), query)
            if sentence:
                analyzed.append(
                    {"doc": doc, "quote": sentence, "value": _stated_value(sentence)}
                )
        if not analyzed:
            return self._finish_incomplete(
                history,
                "相关文档原文中未找到与问题直接相关的陈述",
                f"已读取的文档原文中缺少支持回答「{question}」的依据",
            )

        values = [a["value"] for a in analyzed]
        if len(analyzed) >= 2 and all(v is not None for v in values) and len(set(values)) > 1:
            # 冲突：列出双方原文引用与陈述值，请求确认，不擅自选答案
            conflicts = [
                {
                    "doc_id": a["doc"]["doc_id"],
                    "title": a["doc"]["title"],
                    "version": a["doc"]["version"],
                    "date": a["doc"]["date"],
                    "quote": a["quote"],
                    "stated_value": a["value"],
                }
                for a in analyzed
            ]
            ids = "、".join(c["doc_id"] for c in conflicts)
            no_scope = all(
                "适用" not in c["quote"] and "优先" not in c["quote"] for c in conflicts
            )
            extra = "，且两份文档均未说明适用范围与优先级" if no_scope else ""
            return {
                "type": "finish",
                "payload": {
                    "status": "needs_confirmation",
                    "message": f"检测到文档冲突：{ids} 对该问题给出不同陈述，需用户确认适用制度",
                    "answer": None,
                    "citations": [_citation(a["doc"], a["quote"]) for a in analyzed],
                    "conflicts": conflicts,
                    "pending_confirmation": (
                        f"检测到 {ids} 对「{question}」分别陈述为 "
                        f"{' 与 '.join(c['stated_value'] for c in conflicts)}{extra}，"
                        "请确认适用的制度后我再作答，不擅自选择。"
                    ),
                    "gaps": ["文档间陈述冲突，缺少适用范围或优先级说明"],
                    "steps": _steps_from_history(history),
                },
                "basis": "多篇文档对同一问题给出不同数值陈述，依据边界列出冲突与引用并请求确认",
            }

        # 无冲突：依据最相关文档的原文回答（引用逐字原文）
        top = analyzed[0]
        citations = [_citation(a["doc"], a["quote"]) for a in analyzed]
        answer = (
            f"根据《{top['doc']['title']}》（{top['doc']['doc_id']} "
            f"{top['doc']['version']}，{top['doc']['date']}）：{top['quote']}"
        )
        return {
            "type": "finish",
            "payload": {
                "status": "answered",
                "message": "已依据知识库原文回答",
                "answer": answer,
                "citations": citations,
                "conflicts": None,
                "pending_confirmation": None,
                "gaps": [],
                "steps": _steps_from_history(history),
            },
            "basis": f"依据 {top['doc']['doc_id']} 原文中的直接陈述回答，无文档冲突",
        }

    def _finish_incomplete(self, history: list, message: str, gap: str) -> dict:
        return {
            "type": "finish",
            "payload": {
                "status": "incomplete",
                "message": message,
                "answer": None,
                "citations": [],
                "conflicts": None,
                "pending_confirmation": None,
                "gaps": [gap],
                "steps": _steps_from_history(history),
            },
            "basis": "无法获得可靠依据，按边界终止并如实报告",
        }

    def _build_query(self, question: str, dialog_history: list) -> str:
        """构造检索词：省略式追问结合上文改写，否则用问句核心词。"""
        if dialog_history and self._is_followup(question):
            last_q = dialog_history[-1].get("question", "")
            return _core_words(question) or _core_words(last_q)
        return _core_words(question) or question

    @staticmethod
    def _is_followup(question: str) -> bool:
        return question.startswith("那") or "呢" in question or "还有" in question or len(question) <= 5

    def _query_basis(self, question: str, dialog_history: list, query: str) -> str:
        if dialog_history and self._is_followup(question):
            return (
                f"当前问题「{question}」是省略式追问，结合上文话题改写检索词为「{query}」"
            )
        return f"尚未检索，将问句改写为检索词「{query}」发起检索"


class ApiModelClient:
    """OpenAI 兼容 /chat/completions 客户端（标准库实现；未随本仓库验证）。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _chat(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def decide(self, task: dict, history: list) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"task": task, "tool_schemas": TOOL_SCHEMAS}, ensure_ascii=False
                ),
            },
        ]
        for i, h in enumerate(history):
            a = h["action"]
            messages.append(
                {
                    "role": "assistant",
                    "content": a.get("basis", ""),
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": a["tool"],
                                "arguments": json.dumps(
                                    a.get("args") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": json.dumps(h["observation"], ensure_ascii=False),
                }
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "description": s["description"],
                        "parameters": {"type": "object", "properties": s["params"]},
                    },
                }
                for s in TOOL_SCHEMAS
            ],
            "tool_choice": "auto",
        }
        resp = self._chat(payload)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if calls:
            fn = calls[0]["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            return {
                "type": "tool_call",
                "tool": fn["name"],
                "args": args,
                "basis": msg.get("content") or "模型发起工具调用",
            }
        content = msg.get("content") or ""
        try:
            payload_out = json.loads(content)
            if not isinstance(payload_out, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            payload_out = {"status": "incomplete", "message": content or "模型未返回可解析结果"}
        return {"type": "finish", "payload": payload_out, "basis": "模型直接给出最终结论"}


def build_client():
    """按 AGENT_MODEL 环境变量构建模型客户端，默认 mock。"""
    mode = os.environ.get("AGENT_MODEL", "mock").strip().lower()
    if mode == "mock":
        return MockModelClient()
    if mode == "api":
        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL")
        if not (base and key and model):
            raise RuntimeError(
                "AGENT_MODEL=api 需要设置 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL"
                "（参见 .env.example）"
            )
        return ApiModelClient(base, key, model)
    raise ValueError(f"未知 AGENT_MODEL: {mode}（可选 mock / api）")
