"""
编排层：把"调研 → 决策"两个引擎连成一条流水线。

这是整个体系的"总装"——上层 API 和前端只需要调这一个函数，
不用关心下面有几个引擎、怎么衔接。

为什么单独抽一层？
  1. 引擎各自只做一件事（调研/决策），编排逻辑单独放，职责清晰；
  2. 以后加新引擎（比如"预算估算"），只改这里，API 和前端不动；
  3. 事件回调 event_sink 让上层（SSE）能实时看到每一步进展。
"""
import logging
from typing import Callable, Dict, Any, Optional

from core.llm import LLM
from core.tools import SearchTool
from core.evidence import KnowledgePack, TravelPlan
from agents.researcher import ResearchEngine
from agents.planner import PlanEngine

logger = logging.getLogger(__name__)


class TripResearchPipeline:
    def __init__(self, llm: LLM, search: SearchTool,
                 event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
                 amap=None, photos=None):
        self._llm = llm
        self.researcher = ResearchEngine(llm, search, event_sink=event_sink)
        self.planner = PlanEngine(llm, amap=amap, photos=photos)
        self._sink = event_sink

    def _emit(self, event: Dict[str, Any]):
        if self._sink:
            try:
                self._sink(event)
            except Exception as e:
                logger.warning(f"事件回调异常（不影响主流程）: {e}")

    def run(self, topic: str, preferences: dict = None,
            domain: str = "", deep: bool = False):
        """执行完整流程，返回 (知识包, 行程, 政策卡片)。

        Args:
            domain: 调研领域（travel/policy/ecommerce/general），默认按旅行。
            deep: True 时读全文 + 迭代追问（更慢但更深入）。
        """
        preferences = preferences or {}

        # ---- 阶段 1：调研 ----
        mode = "深度调研（读全文+追问）" if deep else "快速调研"
        self._emit({"type": "stage", "stage": "research",
                    "message": f"正在{mode}：{topic}"})
        pack = self.researcher.research(topic, preferences, domain=domain, deep=deep)
        self._emit({"type": "findings",
                    "count": len(pack.findings),
                    "message": f"调研完成，得到 {len(pack.findings)} 条带来源的发现"})

        # ---- 阶段 2：决策（只有旅行领域才排行程）----
        plan = TravelPlan(topic=topic)
        card = None
        if domain in ("", "travel"):
            self._emit({"type": "stage", "stage": "plan",
                        "message": "正在生成行程..."})
            plan = self.planner.plan(pack)
            self._emit({"type": "plan",
                        "days": len(plan.days),
                        "places": len(plan.all_places()),
                        "message": f"行程生成完成，共 {len(plan.days)} 天 {len(plan.all_places())} 个地点"})
        elif domain == "policy":
            # 政策领域：把发现整理成结构化卡片（文号/主体/补贴/期限）
            self._emit({"type": "stage", "stage": "plan",
                        "message": "正在整理政策要点..."})
            # GraphRAG 接入点：先查固定语料图谱拿多跳关系链
            # （纯 RAG 的搜索摘要串不起"条例→依据→试行办法"这类链，
            #  评测中该信息缺失导致两个用例 50%~100% 波动）
            # 图谱不存在（用户没跑过 build_corpus/extract）→ 静默回退纯 RAG
            graph_context = ""
            try:
                from graph.graph_query import context_for_question, GRAPH_FILE
                if GRAPH_FILE.is_file():
                    graph_context = context_for_question(topic, restrict_topic=topic)
                    if graph_context:
                        self._emit({"type": "progress",
                                    "text": "已注入政策知识图谱上下文（2跳关系链）"})
            except Exception as e:
                logger.warning(f"图谱查询失败，回退纯 RAG: {type(e).__name__}: {e}")

            from core.policy import build_card
            card = build_card(pack, llm=self._llm, graph_context=graph_context)
            self._emit({"type": "policy_card", "card": card.to_dict(),
                        "message": f"已抽取 {card.filled}/6 个关键字段"})

        return pack, plan, card
