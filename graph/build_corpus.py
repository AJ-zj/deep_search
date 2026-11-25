"""
语料固化：把调研搜到的政策全文存成本地固定语料库。

为什么是 GraphRAG 的第一步？
    GraphRAG 的公式是"离线编译一次，在线便宜查询"。
    "离线编译"的对象必须是一个【固定的语料库】——文档存下来、不再变，
    之后抽的三元组、建的图才有意义。
    现在项目是"每次在线搜、用完即弃"，没有沉淀，所以先固化。

产出目录结构：
    graph/corpus/
    ├── manifest.json        # 每篇文档的 id/url/title/topic/文件名
    └── docs/
        ├── 001_灵隐....md    # 全文正文（Markdown 纯文本）
        └── ...

用法：
    python -m graph.build_corpus            # 跑全部主题，生成语料
    python graph/build_corpus --dry-run     # 只列计划不联网
"""
import sys
import json
import time
import hashlib
import argparse
import logging
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from core.config import get_settings
from core.tools import SearchTool
from core.authority import rank
from agents.researcher import ResearchEngine
from core.domain import get_domain

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("build_corpus")

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "graph" / "corpus"
DOCS_DIR = CORPUS_DIR / "docs"
MANIFEST = CORPUS_DIR / "manifest.json"

MIN_CHARS = 600          # 太短的全文没有抽取价值，丢弃
MIN_AUTHORITY = ("S", "A", "B")   # 只收 B 级以上的来源（政策场景 D 级没意义）


def doc_relevant(title: str, body: str, topic: str, min_hits: int = 2) -> int:
    """文档级相关性判断：返回命中的主题实体词数量（0=完全不沾边）。

    为什么需要它？实测语料混进过高校"2026年高层次人才招聘启事"——
    它含"补贴/人才"等词通过了搜索，但不是政策文件，被抽出了 14 条
    垃圾三元组。搜索不可全信，入库前要按主题实体词再过一遍。

    为什么分级使用（写入 min_hits=2，汇总 min_hits=1）？
        第一版汇总也用 >=2 一刀切，结果误杀真文档：
        《杭州市数字经济发展"十四五"规划》相对主题"浙江省数字经济发展规划政策"
        只命中 1 个词（市≠省、规划≠政策），被剔除了——但它恰恰是主题的核心文档。
        原则：能搜到的文档大多相关，强过滤只用于【入库】；
        【图汇总】只排除完全无关（0 命中）的，模糊的保留。
    """
    from agents.researcher import ResearchEngine
    from core.domain import get_domain
    kws = ResearchEngine._precheck_keywords(topic, get_domain("policy"))
    if not kws:
        return 99
    probe = (title or "") + " " + (body or "")[:1000]
    return sum(1 for k in kws if k in probe)


def topic_list():
    """从评测集取主题（保证语料和评测同源，之后好对比）。"""
    eval_set = json.loads((ROOT / "tests" / "eval_set.json").read_text(encoding="utf-8"))
    return [c for c in eval_set["cases"] if not c.get("is_negative")]


def slugify(text: str, maxlen: int = 40) -> str:
    """清洗文件名：只保留汉字/字母/数字/下划线/短横。

    为什么不能只替换常见标点？
        实测政策标题里混着 Windows 非法字符（反斜杠、全角感叹、引号等），
        黑名单永远列不全。反过来用白名单：不匹配的一律换成下划线，
        从根上杜绝 FileNotFoundError。
    """
    text = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", str(text or ""))
    text = re.sub(r"_+", "_", text).strip("_. -")     # 去首尾的点/下划线（Windows 忌讳）
    return text[:maxlen].strip("_. -") or "doc"


def build(deep: bool = True, per_topic_max_docs: int = 6):
    s = get_settings()
    if not s.tavily_api_key:
        logger.error("缺少 TAVILY_API_KEY，无法抓全文")
        sys.exit(1)

    search = SearchTool(api_key=s.tavily_api_key, fulltext=True)
    llm_engine = ResearchEngine(
        __import__("core.llm", fromlist=["LLM"]).LLM(
            s.llm_model, s.openai_api_key, s.openai_base_url), search)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # 已有 manifest 则续用（增量：同 URL 不重复抓）
    manifest = {"docs": [], "topics": {}}
    if MANIFEST.is_file():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    known_urls = {d["url"] for d in manifest["docs"]}

    for case in topic_list():
        topic = case["topic"]
        logger.info(f"固化主题: {topic}")
        # 用拆解出的子问题搜（覆盖面比单查主题好）
        dom = get_domain("policy")
        questions = llm_engine._decompose(topic, {}, dom)
        collected = []
        for q in questions:
            results = rank(search.search(q, max_results=5))
            for r in results:
                if r.get("level") not in MIN_AUTHORITY:
                    continue
                body = (r.get("fulltext") or "").strip()
                if len(body) < MIN_CHARS:
                    continue
                # 文档级相关性过滤（入库从严：≥2 个实体词命中）
                if doc_relevant(r.get("title", ""), body, topic) < 2:
                    logger.info(f"  过滤非主题文档: {r.get('title','')[:36]}")
                    continue
                if r["url"] in known_urls:
                    continue
                known_urls.add(r["url"])
                doc_id = f"{len(manifest['docs']) + 1:03d}"
                fname = f"{doc_id}_{slugify(r.get('title') or r['url'])}.md"
                header = (f"---\ntitle: {r.get('title','')}\nurl: {r['url']}\n"
                          f"level: {r.get('level','')}\ntopic: {topic}\n"
                          f"found_by: {q}\n---\n\n")
                (DOCS_DIR / fname).write_text(header + body, encoding="utf-8")
                manifest["docs"].append({
                    "id": doc_id, "file": fname, "title": r.get("title", ""),
                    "url": r["url"], "level": r.get("level", ""),
                    "chars": len(body),
                })
                collected.append(fname)
                if len(collected) >= per_topic_max_docs:
                    break
            if len(collected) >= per_topic_max_docs:
                break
        manifest["topics"][topic] = collected
        logger.info(f"  主题 [{topic}] 入库 {len(collected)} 篇")
        time.sleep(1)   # 温柔对待 API

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    total = len(manifest["docs"])
    chars = sum(d["chars"] for d in manifest["docs"])
    print(f"\n语料库: {total} 篇文档, 共 {chars} 字 (~{chars//2000} 次LLM抽取量)")
    print(f"位置: {CORPUS_DIR}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列计划不联网")
    args = ap.parse_args()

    if args.dry_run:
        for c in topic_list():
            print(f"计划固化: {c['topic']}")
        print(f"共 {len(topic_list())} 个主题 → 预计 40-60 篇文档")
        return
    build()


if __name__ == "__main__":
    main()
