"""
调研引擎：主题 → 带来源的知识包（通用组件）。

它是共享内核的第一个应用：LLM(core) + 搜索(core) + 证据契约(core)。

【这是一个通用组件，不是旅行专用】
  "怎么拆问题/怎么过滤"由 Domain 配置决定，引擎逻辑只有一份：
      research("杭州3天游", domain="travel")
      research("浙江省分布式光伏补贴政策", domain="policy")

【两档调研质量】
  deep=False（默认）：3 个子问题 × 3 条结果，只读摘要，约 17s
  deep=True        ：读整页正文 + 自动追问补搜，约 40-60s，质量高得多
                     （对应"深度研究"，解决摘要信息不够的问题）

【迭代深挖逻辑】
  第一轮跑完后，让 LLM 判断：哪些方面证据不足 / 有矛盾 / 缺关键数字。
  若有缺口，生成 1-2 个追问再搜一轮，补充发现。这是"深度研究"该有的样子。
"""
import re
import logging
from typing import List, Tuple

from core.llm import LLM
from core.tools import SearchTool
from core.evidence import KnowledgePack
from core.domain import Domain, get_domain

logger = logging.getLogger(__name__)

# 无信息量的占位结论（离线降级时模型容易生成这类内容）
_USELESS_PATTERNS = re.compile(
    r"网络(搜索|环境)?(不可用|无法|不支持|受限)"
    r"|无法(获取|查询|检索|找到)"
    r"|没有(找到|获取到)相关"
    r"|建议联网"
    r"|离线资料"
)


class ResearchEngine:
    def __init__(self, llm: LLM, search: SearchTool,
                 event_sink=None):
        self.llm = llm
        self.search = search
        self._sink = event_sink

    def _emit(self, event: dict):
        if self._sink:
            try:
                self._sink(event)
            except Exception:
                pass

    def research(self, topic: str, preferences: dict = None,
                 domain: str = "", deep: bool = False) -> KnowledgePack:
        """调研主题，产出知识包。

        Args:
            topic: 研究主题
            preferences: 额外偏好
            domain: 领域 key（travel/policy/ecommerce/general）
            deep: True 时读全文 + 迭代追问（更慢但更深）
        """
        dom: Domain = get_domain(domain)
        preferences = preferences or {}
        pack = KnowledgePack(topic=topic, preferences=preferences)

        # ① 拆解
        questions = self._decompose(topic, preferences, dom)
        logger.info(f"[{dom.name}] 拆解出 {len(questions)} 个子问题: {questions}")
        self._emit({"type": "progress", "text": f"拆解为 {len(questions)} 个调研问题"})

        keywords = self._topic_keywords(topic, dom)
        scope = keywords[0] if (keywords and dom.scope_by_place) else ""
        all_findings: List[Tuple[str, str]] = []

        # ② 第一轮：逐问题搜索 + 提炼
        for i, q in enumerate(questions, 1):
            self._emit({"type": "progress",
                        "text": f"正在调研 {i}/{len(questions)}：{q[:24]}"})
            all_findings.extend(self._research_one(q, scope, dom, deep))

        # ③ 迭代深挖：找出证据缺口，追问补搜
        if deep and all_findings:
            gaps = self._find_gaps(topic, all_findings, dom)
            logger.info(f"缺口判断结果: {gaps}（deep={deep}, 已有 {len(all_findings)} 条）")
            if gaps:
                logger.info(f"发现 {len(gaps)} 个证据缺口，进入第二轮: {gaps}")
                self._emit({"type": "progress", "text": f"发现 {len(gaps)} 处证据缺口，深挖中..."})
                for g in gaps[:2]:
                    all_findings.extend(self._research_one(g, scope, dom, deep))

        # ④ 过滤 + 去重（同一结论只保留一条）
        kept = [(c, s) for c, s in all_findings
                if not keywords or any(k in c for k in keywords)]
        if len(kept) < 2:
            kept = all_findings
        seen = set()
        for claim, source in kept:
            # 丢弃"网络不可用"这类无信息量的占位结论（离线降级时模型会生成）
            if _USELESS_PATTERNS.search(claim):
                logger.warning(f"丢弃无信息量结论: {claim[:40]}")
                continue
            key = claim[:30]        # 用前缀去重，避免同一结论重复入库
            if key in seen:
                continue
            seen.add(key)
            pack.add(claim, source)

        return pack

    def _research_one(self, question: str, scope: str, dom: Domain,
                      deep: bool) -> List[Tuple[str, str]]:
        """单个问题的调研：搜索 → 提炼 → 绑定来源。"""
        scoped_q = f"{scope} {question}" if scope and scope not in question else question
        results = self.search.search(scoped_q, max_results=3)
        if not results:
            return []

        # 组装喂给模型的材料。deep 模式优先用整页正文，
        # 但正文可能为空（抓取失败/离线降级），此时必须回退到摘要，
        # 否则模型会拿到空材料、凭空编造"网络不可用"之类的内容。
        blocks = []
        for i, r in enumerate(results, 1):
            body = ""
            if deep:
                body = (r.get("fulltext") or "").strip()
            if not body:
                body = (r.get("snippet") or "").strip()
            if not body:
                continue
            blocks.append(f"[{i}] {r.get('title','')}\n{body}")
        if not blocks:
            logger.warning(f"搜索结果无有效正文，跳过：{question[:30]}")
            return []
        material = "\n\n".join(blocks)

        findings = self._extract(question, material, len(results), dom)
        out = []
        for claim, idx in findings:
            # 按模型标注的编号取来源，而非一律用第一条（防错误归因）
            source = results[idx - 1].get("url", "") if idx else ""
            out.append((claim, source))
        return out

    def _find_gaps(self, topic: str, findings: List[Tuple[str, str]],
                   dom: Domain) -> List[str]:
        """让 LLM 判断还缺什么，生成追问。没有缺口就返回空列表。"""
        existing = "\n".join(f"- {c}" for c, _ in findings[:10])
        prompt = (
            f"研究主题：{topic}\n已获得的发现：\n{existing}\n\n"
            f"请判断要交付一份可靠的研究结论，还缺哪些关键信息"
            f"（重点关注{dom.name}领域的关键事实）。\n"
            "如果需要补充，列出 1 个最关键的补充搜索问题（只一行，不要编号）。\n"
            "如果已经足够充分，只输出：无需补充"
        )
        try:
            resp = self.llm.invoke([
                {"role": "system", "content": "你是严谨的研究审校员，只输出一行结论。"},
                {"role": "user", "content": prompt},
            ], max_tokens=200)
            text = resp.content.strip()
            if "无需补充" in text or not text:
                return []
            line = re.sub(r"^\d+[\.、)]\s*", "", text.splitlines()[0]).strip()
            return [line] if len(line) > 5 else []
        except Exception as e:
            logger.warning(f"缺口判断失败（不影响主流程）: {type(e).__name__}: {e}")
            return []

    @staticmethod
    def _topic_keywords(topic: str, dom: Domain) -> List[str]:
        """从主题提取有区分度的关键词（按领域停用词过滤）。

        为什么不能直接用 re.findall(r'[\\u4e00-\\u9fa5]{2,}')？
            它会贪婪吞掉整句（"浙江省分布式光伏补贴政策" 变一个词），
            导致关键词比主题还长，过滤时永远匹配不上。
        做法：先按停用词切分，再从切出的片段里取前 2-4 字作为关键词。
        """
        stop = sorted(dom.stop_words, key=len, reverse=True)

        # 用领域停用词作为分隔符，把长句切成短片段
        pattern = "|".join(re.escape(w) for w in stop) if stop else None
        segments = re.split(pattern, topic) if pattern else [topic]

        keywords: List[str] = []
        for seg in segments:
            seg = seg.strip()
            seg = re.sub(r"[\d\s\W_]+", "", seg)     # 去数字、标点
            if len(seg) < 2:
                continue
            # 片段可能偏长（如"浙江省分布式光伏"），提取行政区/主体前缀
            keywords.extend(ResearchEngine._split_subject(seg))

        # 英文词单独提取
        keywords.extend(re.findall(r"[A-Za-z]{3,}", topic))
        # 去重且保持顺序
        seen, out = set(), []
        for k in keywords:
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out

    # 常见行政区/主体后缀（用于切出"浙江省""杭州市"这类核心主体）
    _GEO_RE = re.compile(
        r"^(.{2,3}?(?:省|市|县|区|州|自治区|特别行政区))"
    )

    @staticmethod
    def _split_subject(seg: str) -> List[str]:
        """从长片段里切出核心主体。"""
        out = []
        m = ResearchEngine._GEO_RE.match(seg)
        if m:
            out.append(m.group(1))          # 如"浙江省"
            rest = seg[m.end():]
            if len(rest) >= 2:
                out.append(rest[:6])        # 剩余部分取前 6 字作领域词
        else:
            out.append(seg[:6])
        return out

    def _decompose(self, topic: str, prefs: dict, dom: Domain) -> List[str]:
        """LLM 按领域维度拆成 3 个子问题；解析失败兜底用主题本身。"""
        prompt = (
            f"研究主题：{topic}\n用户偏好：{prefs}\n"
            f"请列出 3 个最值得调研的具体问题（每行一个，不要编号、不要解释），"
            f"{dom.dimensions_hint}。"
        )
        resp = self.llm.invoke([
            {"role": "system", "content": f"你是{dom.name}研究助手，只列问题，简洁。"},
            {"role": "user", "content": prompt},
        ], max_tokens=400)
        lines = [re.sub(r"^\d+[\.、)]\s*", "", l.strip())
                 for l in resp.content.splitlines() if l.strip()]
        lines = [l for l in lines if len(l) > 4][:3]
        return lines or [topic]

    def _extract(self, question: str, material: str, n_results: int,
                 dom: Domain) -> List[Tuple[str, int]]:
        """从资料提炼最多 3 条结论，返回 [(结论, 来源编号)]。

        两段式：先严格提炼（允许判定"无关"），失败则放宽再试一次。
        为什么需要放宽？
            实测严格模式下，模型会因为"资料没精确回答这个具体问题"
            而输出"无关"，导致整轮调研 0 条结果。但资料本身是相关的
            （如问文号，资料给了补贴标准），不该整条丢弃。
        """
        if not material:
            return []

        strict = self._extract_once(question, material, n_results, dom, strict=True)
        if strict:
            return strict
        logger.info(f"严格提炼无结果，放宽重试：{question[:30]}")
        return self._extract_once(question, material, n_results, dom, strict=False)

    def _extract_once(self, question: str, material: str, n_results: int,
                      dom: Domain, strict: bool) -> List[Tuple[str, int]]:
        hint = f"。{dom.extract_hint}" if dom.extract_hint else ""
        if strict:
            system_prompt = (
                "你是研究助手。仔细阅读资料，输出结论，每条一行，格式为：结论内容||来源编号。"
                "来源编号是该结论依据的资料序号（如 1、2、3）。"
                "只依据资料、不要编造；如果资料与问题主题完全无关，直接输出“无关”" + hint
            )
            ask = "提炼最多 3 条关键结论（格式：结论||编号）："
        else:
            # 放宽：不再允许说"无关"，只要资料里有可用信息就提炼出来
            system_prompt = (
                "你是研究助手。资料可能与问题不完全对应，但只要能提取到相关的"
                "事实、数字、规定或背景，就要提炼出来。输出结论，每条一行，"
                "格式为：结论内容||来源编号。来源编号是资料序号。" + hint
            )
            ask = "尽量提炼最多 3 条与主题相关的事实或结论（格式：结论||编号）："

        resp = self.llm.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"问题：{question}\n资料：\n{material}\n\n{ask}"},
        ], max_tokens=600)

        out = []
        for line in resp.content.splitlines():
            line = re.sub(r"^\d+[\.、)]\s*", "", line.strip())
            if len(line) <= 5 or "无关" in line:
                continue
            claim, _, idx = line.partition("||")
            claim = claim.strip()
            if not claim:
                continue
            try:
                n = int(re.sub(r"\D", "", idx) or 0)
            except ValueError:
                n = 0
            if not (1 <= n <= n_results):
                n = 0
            out.append((claim, n))
        return out[:3]
