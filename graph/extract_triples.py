"""
三元组抽取 + 图谱构建（mini-GraphRAG 的"离线编译"环节）。

对语料库每篇文档，让 LLM 抽取 (头实体, 关系, 尾实体) 三元组，
合并成一张政策知识图谱，存为 graph/graph.json（NetworkX 可直接读）。

为什么这样设计：
  1. 关系类型用"受控词表"——政策场景就这几种核心关系，
     让模型自由发挥会得到上千种同义关系名，图就废了。
  2. 每篇文档独立抽取，可断点续跑（extracted.json 记录已完成文档）。
  3. 存 JSON + NetworkX 而不是直接上 Neo4j：
     语料才几十篇、三元组预计几百个，内存图完全够；
     验证有效后再迁 Neo4j 只是换一个持久层（接口已按那个方向留）。
"""
import sys
import json
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from core.config import get_settings
from core.llm import LLM

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("extract")

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "graph" / "corpus"
DOCS_DIR = CORPUS_DIR / "docs"
MANIFEST = CORPUS_DIR / "manifest.json"
EXTRACTED = ROOT / "graph" / "extracted.json"     # 断点记录：doc_id -> 三元组
GRAPH_OUT = ROOT / "graph" / "graph.json"

# 受控关系词表（政策领域的核心骨架）
RELATIONS = [
    "发布主体",        # 文件 -> 机构
    "规定",            # 文件 -> 标准/金额/条件
    "适用于",          # 文件 -> 对象（项目类型/人群）
    "修订",            # 文件 -> 文件
    "废止",            # 文件 -> 文件
    "依据",            # 文件 -> 上级文件/法律
    "有效期",          # 文件 -> 时间范围
    "补贴标准",        # 项目类型 -> 金额
    "申报条件",        # 项目类型 -> 条件
]

PROMPT_TMPL = """你是政策知识抽取员。从下面的政策文档摘录中抽取三元组。

硬性要求：
1. 关系只能从这个列表里选：{relations}
2. 只抽取文档中明确出现的事实，禁止推断和编造
3. 实体用文档中的原文名称（如文件用全称或文号，机构用官方名称）
4. 金额、日期、编号必须原样保留
5. 只输出 JSON 数组，形如：[["头实体", "关系", "尾实体"], ...]
6. 抽不到就输出 []

文档摘录（来自 {title} ，主题：{topic}）：
{body}
"""

MAX_BODY = 4000     # 每篇喂给模型的正文上限（取头部，政策要点通常在前部）


def extract_doc(llm: LLM, title: str, topic: str, body: str) -> list:
    prompt = PROMPT_TMPL.format(
        relations="、".join(RELATIONS), title=title, topic=topic,
        body=body[:MAX_BODY],
    )
    resp = llm.invoke([
        {"role": "system", "content": "你是严谨的知识抽取员，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ], max_tokens=1200)

    # 脏 JSON 清洗（项目一贯策略）
    s = resp.content.strip()
    s = __import__("re").sub(r"^```(?:json)?\s*", "", s, flags=__import__("re").IGNORECASE)
    s = __import__("re").sub(r"\s*```$", "", s)
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b <= a:
        return []
    try:
        data = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        logger.warning(f"抽取结果非合法JSON: {title[:30]}")
        return []

    # 校验：必须是三元组、关系必须在词表内（模型偶尔造词，直接丢）
    valid_rel = set(RELATIONS)
    out = []
    for t in data:
        if isinstance(t, list) and len(t) == 3 and str(t[1]).strip() in valid_rel:
            h, r, tail = [str(x).strip() for x in t]
            if h and tail:
                out.append([h, r, tail])
    return out


def main():
    if not MANIFEST.is_file():
        logger.error("语料库不存在，先运行 graph/build_corpus.py")
        sys.exit(1)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    docs = manifest["docs"]
    logger.info(f"语料库共 {len(docs)} 篇文档")

    # 断点续跑
    done = {}
    if EXTRACTED.is_file():
        done = json.loads(EXTRACTED.read_text(encoding="utf-8"))

    s = get_settings()
    llm = LLM(s.llm_model, s.openai_api_key, s.openai_base_url, temperature=0.1)

    todo = [d for d in docs if d["id"] not in done]
    logger.info(f"已抽取 {len(done)} 篇，待抽取 {len(todo)} 篇")

    for i, d in enumerate(todo, 1):
        path = DOCS_DIR / d["file"]
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        # 跳过 front-matter
        body = raw.split("---", 2)[-1] if raw.startswith("---") else raw
        try:
            triples = extract_doc(llm, d["title"], d.get("level", ""), body)
        except Exception as e:
            logger.warning(f"抽取失败 {d['id']}: {type(e).__name__}: {e}")
            triples = []
        done[d["id"]] = triples
        logger.info(f"[{i}/{len(todo)}] {d['title'][:36]} → {len(triples)} 条三元组")
        if i % 3 == 0:
            EXTRACTED.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.5)

    EXTRACTED.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")

    # 汇总合并成图
    all_triples, seen = [], set()
    for doc_id, triples in done.items():
        for h, r, t in triples:
            key = (h, r, t)
            if key not in seen:
                seen.add(key)
                all_triples.append({"h": h, "r": r, "t": t, "docs": [doc_id]})
            else:
                for item in all_triples:
                    if (item["h"], item["r"], item["t"]) == key:
                        if doc_id not in item["docs"]:
                            item["docs"].append(doc_id)
                        break

    entities = set()
    for tr in all_triples:
        entities.add(tr["h"]); entities.add(tr["t"])

    GRAPH_OUT.write_text(json.dumps(
        {"triples": all_triples}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n图谱构建完成: {len(entities)} 个实体, {len(all_triples)} 条关系")
    print(f"输出: {GRAPH_OUT}")


if __name__ == "__main__":
    main()
