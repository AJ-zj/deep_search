"""
图谱查询（mini-GraphRAG 的"在线便宜查询"环节）。

流程：问题 → 定位实体（图上别名/子串匹配）→ 沿关系 1-2 跳扩展
     → 把命中的三元组渲染成上下文块，供回答/注入 prompt 用。

为什么不用 LLM 做实体链接？
    语料才几十篇、实体几百个，子串匹配足够且零成本零延迟；
    这是"规模决定方案"的又一次体现。实体变多再上 LLM 消歧。

用法：
    python graph/graph_query.py "浙政发〔2013〕49号规定了什么"
    或在代码里: context_for_question("...") -> 拼给 LLM 的图上下文
"""
import sys
import json
import re
import argparse
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
GRAPH_FILE = ROOT / "graph" / "graph.json"


def load_graph(path: Path = GRAPH_FILE) -> dict:
    """读图并建立双向邻接表。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    adj = defaultdict(list)          # entity -> [(rel, other, triple)]
    entities = set()
    for tr in data["triples"]:
        h, r, t = tr["h"], tr["r"], tr["t"]
        entities.add(h); entities.add(t)
        adj[h].append((r, t, tr))
        adj[t].append(("←" + r, h, tr))   # 反向也能走
    return {"triples": data["triples"], "adj": adj, "entities": entities}


def split_terms(question: str) -> list:
    """问题切出候选词：连续中文段 + 文号模式 + 英文词。"""
    terms = []
    terms += re.findall(r"[\u4e00-\u9fa5]{2,}", question)
    terms += re.findall(r"[\u4e00-\u9fa5]{1,8}[〔\[（]\d{4}[〕\]）]\d+号", question)
    terms += re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", question)
    # 长中文段再打碎成 2-4 字词，方便子串命中（"补贴标准"→也能用"补贴"匹配）
    frag = []
    for t in terms:
        if len(t) >= 5 and re.fullmatch(r"[\u4e00-\u9fa5]+", t):
            for n in (4, 3, 2):
                for i in range(0, len(t) - n + 1):
                    frag.append(t[i:i + n])
    return terms + frag


def find_entities(question: str, graph: dict, top_k: int = 3) -> list:
    """问题 → 图中实体（按命中词长度打分，优先长词=更具体）。

    同分时【短实体名优先】：实测打分并列时按名字长排序，
    会让"碳排放权交易活动由重点排放单位对其……"这种长句尾实体
    排在《碳排放权交易管理暂行条例》这种规范实体前面。
    """
    terms = split_terms(question)
    scores = defaultdict(int)
    for ent in graph["entities"]:
        for term in set(terms):
            if len(term) >= 2 and (term in ent or ent in term):
                scores[ent] = max(scores[ent], len(term))
    ranked = sorted(scores.items(), key=lambda x: (-x[1], len(x[0])))
    return [e for e, _ in ranked[:top_k]]


def neighborhood(seeds: list, graph: dict, hops: int = 2) -> list:
    """从种子实体做 1~hops 跳 BFS，返回途经三元组（按文档溯源保留）。"""
    seen_keys = set()
    seen_triples, visited = [], set(seeds)
    frontier = set(seeds)
    for _ in range(hops):
        nxt = set()
        for ent in frontier:
            for rel, other, tr in graph["adj"].get(ent, []):
                key = (tr["h"], tr["r"], tr["t"])
                if key not in seen_keys:
                    seen_keys.add(key)
                    seen_triples.append(tr)
                if other not in visited:
                    nxt.add(other); visited.add(other)
        frontier = nxt
        if not frontier:
            break
    return seen_triples


def render_context(triples: list, max_items: int = 12) -> str:
    """三元组 → 给 LLM 的上下文块（带"来自图谱"标注，与搜索结果区分）。"""
    if not triples:
        return ""
    lines = ["【政策知识图谱（结构化关系，可信度：中，来自固定语料抽取）】"]
    for tr in triples[:max_items]:
        lines.append(f"- ({tr['h']}) -[{tr['r']}]-> ({tr['t']})")
    return "\n".join(lines)


def context_for_question(question: str, graph: dict = None) -> str:
    """一步到位：问题 → 图上下文（图谱没建/没命中就返回空串）。"""
    if graph is None:
        if not GRAPH_FILE.is_file():
            return ""
        graph = load_graph()
    seeds = find_entities(question, graph)
    if not seeds:
        return ""
    trs = neighborhood(seeds, graph, hops=2)
    if not trs:
        return ""
    return render_context(trs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default="浙政发〔2013〕49号规定了什么")
    args = ap.parse_args()

    if not GRAPH_FILE.is_file():
        print("图谱不存在。先跑: build_corpus.py → extract_triples.py")
        sys.exit(1)

    g = load_graph()
    seeds = find_entities(args.question, g)
    print(f"问题: {args.question}")
    print(f"命中实体: {seeds}")
    trs = neighborhood(seeds, g, hops=2)
    print(f"\n2 跳邻域共 {len(trs)} 条关系:")
    print(render_context(trs, max_items=30))


if __name__ == "__main__":
    main()
