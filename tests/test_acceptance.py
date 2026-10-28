"""验收测试：题目三「企业知识问答 Agent」的必须验收点 + 红线。

运行：python -m unittest discover -s tests -v

覆盖：明确答案 / 文档冲突（参考实例 D01 30天 vs D02 15天，不擅自选答案）/
资料外问题 / 改写后的相似问题 / 上下文追问 / 工具失败（续跑）/
步数上限 / CLI 边界 / 替换文档内容后答案与动作随之改变（审核要点鲁棒性）。
红线（对应试卷审核要点）在 assert_red_lines 统一断言：
1. 回答必须有依据：answered 时 citations 非空且每条 quote 逐字属于对应文档原文；
2. 冲突不擅自选答案：needs_confirmation 时 answer 为空且列出双方冲突引用；
3. 无依据不编造：资料外/失败/未完成时 answer 为空且给出资料缺口；
4. 失败步骤不得记为成功；问题原样回显未被更改。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from src.agent import run_agent  # noqa: E402
from src.model_client import MockModelClient  # noqa: E402
from src.tools import ToolRegistry  # noqa: E402


def make_data_dir(tmp_dir: str) -> str:
    """构造隔离数据目录：合成知识库复制。"""
    data_dir = os.path.join(tmp_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    shutil.copy(os.path.join(REPO_ROOT, "data", "docs.json"), os.path.join(data_dir, "docs.json"))
    return data_dir


def load_docs(data_dir: str) -> dict:
    with open(os.path.join(data_dir, "docs.json"), encoding="utf-8") as f:
        return json.load(f)["docs"]


def run_task(task: dict, data_dir: str, max_steps: int = 12):
    registry = ToolRegistry(data_dir, inject=task.get("inject") or {})
    return run_agent(task, MockModelClient(), registry, max_steps=max_steps)


def base_task(**kw) -> dict:
    task = {"question": "年假有几天？"}
    task.update(kw)
    return task


def assert_red_lines(test: unittest.TestCase, result: dict, task: dict, data_dir: str) -> None:
    docs = load_docs(data_dir)
    status = result["status"]
    # 红线 1：回答必须有依据，引用逐字来自原文（不错误引用）
    if status == "answered":
        test.assertTrue(result["answer"])
        test.assertTrue(result["citations"])
        for c in result["citations"]:
            test.assertIn(c["doc_id"], docs)
            test.assertIn(c["quote"], docs[c["doc_id"]]["content"])
    # 红线 2：冲突不擅自选答案
    if status == "needs_confirmation":
        test.assertIsNone(result["answer"])
        test.assertGreaterEqual(len(result["conflicts"]), 2)
        test.assertTrue(result["pending_confirmation"])
        for c in result["conflicts"]:
            test.assertIn(c["quote"], docs[c["doc_id"]]["content"])
    # 红线 3：无依据不编造——非回答状态一律无 answer 且给出缺口
    if status in ("out_of_scope", "failed", "incomplete", "missing_input"):
        test.assertIsNone(result["answer"])
        test.assertFalse(result["citations"])
        test.assertTrue(result["gaps"])
    # 红线 4：步骤状态按实际观察记录，失败不被改写
    for step in result["steps"]:
        test.assertIn(step["status"], ("success", "failed"))
    # 问题原样回显
    test.assertEqual(result["input_summary"]["question"], task.get("question"))


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = make_data_dir(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_1_normal_answered(self):
        """验收点：明确答案——依据 D03 原文回答年假天数并逐字引用。"""
        task = base_task()
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "answered")
        self.assertIn("5 天", result["answer"])
        self.assertEqual(result["citations"][0]["doc_id"], "D03")
        tools = [(s["tool"], s["status"]) for s in result["steps"]]
        self.assertEqual(
            tools, [("search_docs", "success"), ("read_doc", "success")]
        )

    def test_2_doc_conflict_needs_confirmation(self):
        """验收点：文档冲突（参考实例）——列出 D01 30天 与 D02 15天 冲突与引用，
        请求确认适用制度，不得擅自回答 15 天或 30 天。"""
        task = base_task(question="报销期限多久？")
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertIsNone(result["answer"])
        conflicts = {c["doc_id"]: c for c in result["conflicts"]}
        self.assertIn("D01", conflicts)
        self.assertIn("D02", conflicts)
        self.assertEqual(conflicts["D01"]["stated_value"], "30 天")
        self.assertEqual(conflicts["D02"]["stated_value"], "15 天")
        self.assertIn("确认", result["pending_confirmation"])
        self.assertEqual(len(result["citations"]), 2)

    def test_3_out_of_scope(self):
        """验收点：资料外问题——补充检索一次后如实返回缺口，不编造。"""
        task = base_task(question="公司年会什么时候开？")
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "out_of_scope")
        search_calls = [s for s in result["steps"] if s["tool"] == "search_docs"]
        self.assertEqual(len(search_calls), 2)  # 改写检索 + 原始问句补充检索
        self.assertTrue(all(s["status"] == "success" for s in search_calls))
        self.assertTrue(result["gaps"])

    def test_4_paraphrase(self):
        """验收点：改写后的相似问题——『发票多久之内要交』同样依据 D01 回答 30 天。"""
        task = base_task(question="发票多久之内要交？")
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "answered")
        self.assertIn("30 天", result["answer"])
        self.assertEqual(result["citations"][0]["doc_id"], "D01")

    def test_5_context_followup(self):
        """验收点：上下文追问——结合上文『年假』话题回答省略式追问『那事假呢』。"""
        task = base_task(
            question="那事假呢？",
            history=[
                {
                    "question": "年假有几天？",
                    "answer": "根据《考勤与假期制度》（D03 v3.0）：正式员工每年享有 5 天带薪年假。",
                }
            ],
        )
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "answered")
        self.assertIn("3 天", result["answer"])
        self.assertEqual(result["citations"][0]["doc_id"], "D04")
        # 检索词结合上文做了改写（决策依据可审计）
        search_step = next(
            s for s in trace.to_dict()["steps"] if s["tool"] == "search_docs"
        )
        self.assertIn("上文", search_step["decision_basis"])

    def test_6_tool_failure_retry(self):
        """验收点：工具失败——注入一次检索失败，重试成功后正常回答；
        失败步骤保留 failed 记录。"""
        task = base_task(inject={"search_fail_once": True})
        result, _ = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "answered")
        tools = [(s["tool"], s["status"]) for s in result["steps"]]
        self.assertEqual(
            tools,
            [
                ("search_docs", "failed"),
                ("search_docs", "success"),
                ("read_doc", "success"),
            ],
        )
        self.assertIn("5 天", result["answer"])

    def test_7_max_steps_incomplete(self):
        """边界：达到步数上限时返回明确的未完成状态。"""
        task = base_task()
        result, _ = run_task(task, self.data_dir, max_steps=2)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("上限", result["message"])
        self.assertEqual(result["steps_used"], 2)

    def test_8_doc_content_change_alters_answer(self):
        """审核要点鲁棒性：替换文档内容后，答案与动作随之改变——
        把 D01 的 30 天改为 45 天、删除冲突源 D02 后：
        相似问题依据新原文回答 45 天；原冲突问题只检索到 D01 时依据 D01 回答。"""
        with open(os.path.join(self.data_dir, "docs.json"), encoding="utf-8") as f:
            docs = json.load(f)
        docs["docs"]["D01"]["content"] = (
            "员工费用报销期限为 45 天，自发票开具日期起算。"
            "超过期限提交的报销单据将被退回，需重新发起申请。"
        )
        del docs["docs"]["D02"]
        with open(os.path.join(self.data_dir, "docs.json"), "w", encoding="utf-8") as f:
            json.dump(docs, f, ensure_ascii=False, indent=2)

        result, _ = run_task(base_task(question="发票多久之内要交？"), self.data_dir)
        assert_red_lines(self, result, {"question": "发票多久之内要交？"}, self.data_dir)
        self.assertEqual(result["status"], "answered")
        self.assertIn("45 天", result["answer"])

        result2, _ = run_task(base_task(question="报销期限多久？"), self.data_dir)
        assert_red_lines(self, result2, {"question": "报销期限多久？"}, self.data_dir)
        # 只检索到 D01 且无冲突 -> 依据 D01 回答（参考实例第二句）
        self.assertEqual(result2["status"], "answered")
        self.assertEqual(result2["citations"][0]["doc_id"], "D01")
        self.assertIn("45 天", result2["answer"])

    def test_9_missing_question(self):
        """边界：缺问题本体 -> 不调用工具，返回缺口。"""
        task = {"history": []}
        result, trace = run_task(task, self.data_dir)
        assert_red_lines(self, result, task, self.data_dir)
        self.assertEqual(result["status"], "missing_input")
        self.assertEqual(trace.to_dict()["steps"], [])


class CliTests(unittest.TestCase):
    """CLI 运行方式与输入边界。"""

    def test_cli_normal_exit_zero_and_trace(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            data_dir = make_data_dir(tmp.name)
            trace_out = os.path.join(tmp.name, "out.trace.json")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--input",
                    os.path.join(REPO_ROOT, "examples", "normal.input.json"),
                    "--data-dir",
                    data_dir,
                    "--trace-out",
                    trace_out,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "answered")
            with open(trace_out, encoding="utf-8") as f:
                trace = json.load(f)
            self.assertTrue(trace["steps"])
        finally:
            tmp.cleanup()

    def test_cli_invalid_input_nonzero(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            bad_json = os.path.join(tmp.name, "bad.json")
            with open(bad_json, "w", encoding="utf-8") as f:
                f.write("{not json")
            proc = subprocess.run(
                [sys.executable, "-m", "src.main", "--input", bad_json],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
