"""
决策引擎：知识包 → 逐日行程（结构化地点 + 地图 + 图片）。

三个层次：
  1. LLM 生成结构化行程（每天若干地点，每个地点带时间/描述/引用编号）
  2. 高德补地理信息（坐标、地址、周边推荐）
  3. Unsplash 补图片

这就是"调研→决策"之后再加"落地"——把文字方案变成能导航、能看图的产品。
成本可控：高德/Unsplash 都有免费额度。
"""
import re
import json
import logging
from typing import List

from core.llm import LLM
from core.evidence import KnowledgePack, TravelPlan, DayPlan, Place

logger = logging.getLogger(__name__)


class PlanEngine:
    def __init__(self, llm: LLM, amap=None, photos=None):
        self.llm = llm
        self.amap = amap
        self.photos = photos

    def plan(self, pack: KnowledgePack) -> TravelPlan:
        """消费知识包，生成逐日行程（含地理与图片信息）。"""
        days = int(pack.preferences.get("days", 3) or 3)
        city = self._extract_city(pack.topic)

        resp = self.llm.invoke([
            {"role": "system", "content":
             "你是行程规划助手。只依据给定调研发现安排行程，不编造发现之外的信息。"
             "地点要是真实存在的具体场所（寺庙/街道/餐厅/博物馆等），不要写“自由活动”。"
             "只输出 JSON，不要解释文字。"},
            {"role": "user", "content": self._build_prompt(pack, days)},
        ], max_tokens=2000)

        data = self._parse(resp.content)
        plan = TravelPlan(topic=pack.topic)
        for item in data:
            try:
                day_no = int(item.get("day", len(plan.days) + 1))
            except (ValueError, TypeError):
                day_no = len(plan.days) + 1

            places = []
            for p in (item.get("places") or []):
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name") or "").strip()
                if not name:
                    continue
                ref = self._safe_ref(p.get("ref"), len(pack.findings))
                place = Place(
                    name=name,
                    time=str(p.get("time") or "").strip(),
                    desc=str(p.get("desc") or "").strip(),
                    ref=ref,
                )
                # ② 补地理信息（并校验是否真在目标城市）
                if self.amap is not None and getattr(self.amap, "enabled", False):
                    geo = self.amap.geocode_poi(name, city)
                    if geo:
                        # 城市校验：POI 返回的 cityname 与目标城市不符 → 丢弃
                        # （实测"雨山湖公园"会被匹配到安徽马鞍山的同名地点）
                        poi_city = (geo.get("cityname") or "").rstrip("市")
                        target_city = (city or "").rstrip("市")
                        if target_city and poi_city and target_city not in poi_city:
                            logger.warning(
                                f"丢弃异地地点: {name}（高德匹配到 {geo.get('pname')}{poi_city}）")
                            continue
                        place.lng, place.lat = geo.get("lng", ""), geo.get("lat", "")
                        place.location = geo.get("location", "")
                        place.address = geo.get("address", "") or place.address
                        place.nearby = self.amap.nearby(place.location, "美食", 1200, 3)
                # ③ 补图片（Unsplash 对中文支持差，用城市英文名 + 通用词提高命中）
                if self.photos is not None and getattr(self.photos, "enabled", False):
                    photo = self._find_photo(place.name, city)
                    if photo:
                        place.photo = photo.get("url", "")
                        place.photo_thumb = photo.get("thumb", "")
                        place.photo_credit = photo.get("credit", "")
                places.append(place)

            refs = sorted({p.ref for p in places if p.ref})
            plan.days.append(DayPlan(day=day_no, activities=[], references=refs, places=places))

        # 城市校验可能过滤掉部分地点：剔除空白天，并重新连续编号
        plan.days = [d for d in plan.days if d.places]
        for i, d in enumerate(plan.days, 1):
            d.day = i

        # 天气（用城市的 adcode 最准确）
        if self.amap is not None and getattr(self.amap, "enabled", False) and city:
            plan.weather = self.amap.weather(city_name=city)

        total_places = len(plan.all_places())
        plan.summary = (f"根据 {len(pack.findings)} 条调研发现，为你规划 "
                        f"{len(plan.days)} 天、共 {total_places} 个地点。")
        return plan

    @staticmethod
    def _extract_city(topic: str) -> str:
        """粗提取城市名（简单但够用）。"""
        for c in ["杭州", "上海", "北京", "成都", "西安", "南京", "苏州", "厦门", "重庆", "广州", "深圳", "武汉", "长沙", "青岛", "大理", "丽江"]:
            if c in topic:
                return c
        m = re.search(r"([\u4e00-\u9fa5]{2,4})", topic)
        return m.group(1) if m else ""

    # 城市中文 → 英文（Unsplash 用英文检索命中率高得多）
    _CITY_EN = {
        "杭州": "Hangzhou", "上海": "Shanghai", "北京": "Beijing", "成都": "Chengdu",
        "西安": "Xian", "南京": "Nanjing", "苏州": "Suzhou", "厦门": "Xiamen",
        "重庆": "Chongqing", "广州": "Guangzhou", "深圳": "Shenzhen", "武汉": "Wuhan",
        "长沙": "Changsha", "青岛": "Qingdao", "大理": "Dali", "丽江": "Lijiang",
    }

    # 常见地点中文 → 英文（只覆盖高频词，其余走通用策略）
    _PLACE_EN = {
        "灵隐寺": "Lingyin Temple", "西湖": "West Lake", "雷峰塔": "Leifeng Pagoda",
        "岳王庙": "Yue Fei Temple", "河坊街": "Hefang Street", "南宋御街": "Southern Song Imperial Street",
        "浙江省博物馆": "Zhejiang Museum", "良渚古城遗址": "Liangzhu Ancient City",
        "京杭大运河": "Grand Canal", "西溪湿地": "Xixi Wetland", "宋城": "Songcheng Park",
        "奎元馆": "noodle restaurant", "楼外楼": "Chinese restaurant",
        "知味观": "dim sum restaurant", "外婆家": "Chinese restaurant",
        "灵隐": "Lingyin Temple", "苏堤": "Su Causeway", "断桥": "Broken Bridge",
        "千岛湖": "Qiandao Lake", "六和塔": "Liuhe Pagoda", "虎跑": "Hupao Spring",
    }

    def _find_photo(self, name: str, city: str):
        """多策略找图：地名英文 → 城市英文+通用。"""
        if self.photos is None or not getattr(self.photos, "enabled", False):
            return None
        city_en = self._CITY_EN.get(city, city)
        # 策略 1：优先用已知地名的英文映射（最精准）
        place_en = ""
        for cn, en in self._PLACE_EN.items():
            if cn in name:
                place_en = en
                break
        queries = []
        if place_en:
            queries.append(f"{city_en} {place_en}")
        queries.append(f"{city_en} {name}")
        # 兜底：按地点类别选更贴切的英文词，避免所有地点都用同一张城市风景图
        if any(k in name for k in ("寺", "庙", "塔", "观", "禅")):
            queries.append(f"{city_en} temple")
        elif any(k in name for k in ("博物馆", "遗址", "美术馆", "纪念馆")):
            queries.append(f"{city_en} museum")
        elif any(k in name for k in ("街", "路", "巷", "坊", "广场")):
            queries.append(f"{city_en} street")
        elif any(k in name for k in ("湖", "湿地", "公园", "山", "堤", "溪", "景区")):
            queries.append(f"{city_en} lake park")
        elif any(k in name for k in ("餐厅", "菜馆", "酒家", "楼", "面", "饭店",
                                     "虾", "馆", "食", "味", "轩", "居", "园")):
            # 餐馆类：搜美食图，别搜城市风景
            queries.append("Chinese noodles food")
            queries.append("Chinese cuisine dish")
        else:
            queries.append(f"{city_en} China")
        # 最终兜底：通用城市图，保证每个地点都有图
        if not place_en:
            queries.append(f"{city_en} travel")
        for q in queries:
            r = self.photos.search_one(q)
            if r and r.get("url"):
                return r
        return None

    @staticmethod
    def _safe_ref(v, max_n: int) -> int:
        try:
            n = int(v)
            return n if 1 <= n <= max_n else 0
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _build_prompt(pack: KnowledgePack, days: int) -> str:
        return (
            f"{pack.to_prompt()}\n\n"
            f"请安排 {days} 天行程。输出严格 JSON 数组，格式：\n"
            '[{"day":1,"places":['
            '{"time":"上午","name":"灵隐寺","desc":"一句话介绍","ref":1},'
            '{"time":"下午","name":"浙江省博物馆","desc":"...","ref":2}'
            ']},...]\n'
            "要求：每天 2-3 个具体地点；name 必须是真实场所全名（便于地图检索）；"
            "ref 填依据的调研发现编号。"
        )

    @staticmethod
    def _parse(text: str) -> list:
        """脏 JSON 清洗：剥围栏、截数组、去尾逗号、双大括号。"""
        s = (text or "").strip()
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
        start, end = s.find("["), s.rfind("]")
        if start != -1 and end > start:
            s = s[start:end + 1]
        s = s.replace("{{", "{").replace("}}", "}")
        s = re.sub(r",\s*([}\]])", r"\1", s)
        try:
            data = json.loads(s)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError as e:
            logger.warning(f"行程 JSON 解析失败: {e} | 原文: {text[:300]!r}")
            return []
