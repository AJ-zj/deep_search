"""
第二步测试：调研引擎产出带来源的知识包。
验证"共享内核 + 搜索工具 + 证据契约"三者接起来能工作。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_settings
from core.llm import LLM
from core.tools import SearchTool
from core.evidence import KnowledgePack
from agents.researcher import ResearchEngine


def main():
    s = get_settings()
    llm = LLM(s.llm_model, s.openai_api_key, s.openai_base_url)
    # Tavily key：优先读环境变量，否则留空走离线资料（本测试两种都能跑）
    search = SearchTool(api_key=s.tavily_api_key)
    engine = ResearchEngine(llm, search)

    topic = "杭州 3 天人文美食游"
    prefs = {"days": 3, "interests": ["人文", "美食"]}
    print(f"[1] 开始调研: {topic}")
    pack = engine.research(topic, prefs)

    print(f"[2] 知识包: {len(pack.findings)} 条发现")
    for i, f in enumerate(pack.findings, 1):
        print(f"    [{i}] {f.claim}")
        print(f"        来源: {f.source or '（无）'}")

    print("\n[3] 交接物（给决策引擎的 prompt）:")
    print("   " + "-" * 50)
    print("\n".join("   " + l for l in pack.to_prompt().splitlines()))

    assert isinstance(pack, KnowledgePack)
    assert len(pack.findings) >= 2, "发现太少"
    assert all(f.claim for f in pack.findings), "有空发现"
    print("\n✅ 第二步通过：调研引擎产出带来源的知识包")


if __name__ == "__main__":
    main()
