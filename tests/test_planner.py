"""
第三步测试：调研 → 决策 完整链条。
验证决策引擎消费知识包，且行程每条安排能溯源到调研发现。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_settings
from core.llm import LLM
from core.tools import SearchTool
from core.evidence import KnowledgePack
from agents.researcher import ResearchEngine
from agents.planner import PlanEngine


def main():
    s = get_settings()
    llm = LLM(s.llm_model, s.openai_api_key, s.openai_base_url)
    search = SearchTool(api_key=s.tavily_api_key)

    topic = "杭州 3 天人文美食游"
    prefs = {"days": 3, "interests": ["人文", "美食"]}

    # ① 调研
    print("[1] 调研引擎产出知识包 ...")
    pack = ResearchEngine(llm, search).research(topic, prefs)
    print(f"    {len(pack.findings)} 条发现")

    # ② 决策
    print("[2] 决策引擎消费知识包，生成行程 ...")
    plan = PlanEngine(llm).plan(pack)
    print(f"    {len(plan.days)} 天行程")
    for d in plan.days:
        print(f"    第{d.day}天 (依据 {d.references}):")
        for a in d.activities:
            print(f"      - {a}")

    # ③ 渲染带来源的报告
    print("\n[3] 行程报告（Markdown）:")
    print("   " + "-" * 50)
    md = plan.to_markdown(pack)
    print("\n".join("   " + l for l in md.splitlines()))

    # 断言：桥梁真的接上了
    assert isinstance(pack, KnowledgePack)
    assert len(plan.days) >= 2, "行程天数不足"
    total_refs = sum(len(d.references) for d in plan.days)
    assert total_refs >= 1, "行程没有引用任何调研发现——衔接断了"
    print(f"\n✅ 第三步通过：调研→决策链条接通，行程引用了 {total_refs} 处调研证据")


if __name__ == "__main__":
    main()
