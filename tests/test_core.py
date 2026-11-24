"""
第一步测试：共享内核连通性
  验证 config 能读到 key、LLM 能稳定返回中文——这是两个引擎共用地基的证明。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_settings
from core.llm import LLM


def main():
    s = get_settings()
    print("[1] 配置:", s.llm_model, "| key:", s.openai_api_key[:12] + "...")
    assert s.openai_api_key, "没读到 LLM key"

    llm = LLM(model=s.llm_model, api_key=s.openai_api_key, base_url=s.openai_base_url)
    print("[2] 调研引擎视角 —— 让 LLM 识别研究子问题 ...")
    r1 = llm.invoke([
        {"role": "system", "content": "你是旅行研究助手，回答简洁。"},
        {"role": "user", "content": "去杭州玩3天，为调研列出2个关键问题，一句话。"},
    ])
    print("   ", r1.content, f"({r1.latency_ms}ms)")
    assert r1.content and len(r1.content) > 0

    print("[3] 决策引擎视角 —— 让同一个 LLM 排行程 ...")
    r2 = llm.invoke([
        {"role": "system", "content": "你是行程规划助手，回答简洁。"},
        {"role": "user", "content": "杭州3天行程，第一天安排什么？一句话。"},
    ])
    print("   ", r2.content, f"({r2.latency_ms}ms)")
    assert r2.content and len(r2.content) > 0

    print("\n✅ 第一步通过：同一个 LLM 内核同时服务调研与决策两个视角")


if __name__ == "__main__":
    main()
