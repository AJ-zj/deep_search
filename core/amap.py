"""
高德地图服务：给行程里的地点补真实坐标、地址、周边信息。

为什么需要它？
  纯 LLM 生成的行程只有文字，用户体验差。接高德后：
  1. 每个地点有真实经纬度 → 前端能画地图、点开能导航
  2. 有真实地址 → 用户知道在哪
  3. 可以查周边 → 内容更丰富

成本：高德个人开发者有免费额度（每日次数足够），不烧钱。
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class AmapService:
    BASE = "https://restapi.amap.com/v3"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session = requests.Session()
        self._session.trust_env = True

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def geocode_poi(self, name: str, city: str = "") -> Optional[dict]:
        """地点名 → 坐标/地址。优先 POI 搜索，失败退地理编码。

        额外返回 cityname / pname，供上层校验"这个地点是否真的在目标城市"。
        """
        if not self.enabled:
            return None
        try:
            params = {"keywords": name, "key": self.api_key, "offset": 1}
            if city:
                params["city"] = city
                # citylimit=true：强制只在指定城市内搜索，避免返回外省市同名地点
                params["citylimit"] = "true"
            r = self._session.get(f"{self.BASE}/place/text", params=params, timeout=20)
            d = r.json()
            pois = d.get("pois") or []
            if d.get("status") == "1" and pois:
                p = pois[0]
                loc = p.get("location", "")
                lng, lat = (loc.split(",") + ["", ""])[:2] if loc else ("", "")
                return {
                    "name": p.get("name") or name,
                    "lng": lng, "lat": lat, "location": loc,
                    "address": p.get("address") if isinstance(p.get("address"), str) else "",
                    "type": p.get("type", ""),
                    "cityname": p.get("cityname", ""),
                    "pname": p.get("pname", ""),
                }
        except Exception as e:
            logger.warning(f"高德 POI 查询失败: {name} | {type(e).__name__}: {e}")

        # 退路：地理编码
        try:
            r = self._session.get(
                f"{self.BASE}/geocode/geo",
                params={"address": f"{city}{name}", "key": self.api_key}, timeout=20)
            d = r.json()
            g = (d.get("geocodes") or [])
            if d.get("status") == "1" and g:
                loc = g[0].get("location", "")
                lng, lat = (loc.split(",") + ["", ""])[:2] if loc else ("", "")
                return {"name": name, "lng": lng, "lat": lat, "location": loc,
                        "address": g[0].get("formatted_address", ""), "type": "",
                        "cityname": "", "pname": ""}
        except Exception as e:
            logger.warning(f"高德地理编码失败: {name} | {type(e).__name__}: {e}")
        return None

    def weather(self, city_adcode: str = "", city_name: str = "") -> list:
        """查城市未来几天天气预报（高德天气 API）。

        Args:
            city_adcode: 高德城市 adcode（优先，最准确，如杭州 330100）
            city_name: 城市名（无 adcode 时用）
        Returns: [{date, week, day_weather, day_temp, night_weather, night_temp}]
        """
        if not self.enabled:
            return []
        code = city_adcode
        # 没有 adcode 时，用行政区划接口查询（POI 搜索不返回 adcode，实测无效）
        if not code and city_name:
            try:
                r = self._session.get(f"{self.BASE}/config/district", params={
                    "keywords": city_name, "key": self.api_key, "subdistrict": 0,
                }, timeout=20)
                districts = (r.json() or {}).get("districts") or []
                if districts:
                    code = districts[0].get("adcode", "")
            except Exception as e:
                logger.warning(f"查询城市 adcode 失败: {e}")
        if not code:
            return []
        try:
            r = self._session.get(f"{self.BASE}/weather/weatherInfo", params={
                "city": code, "key": self.api_key, "extensions": "all",
            }, timeout=20)
            d = r.json()
            forecasts = d.get("forecasts") or []
            if not forecasts:
                return []
            out = []
            for cast in (forecasts[0].get("casts") or [])[:4]:
                out.append({
                    "date": cast.get("date", ""),
                    "week": cast.get("week", ""),
                    "day_weather": cast.get("dayweather", ""),
                    "day_temp": cast.get("daytemp", ""),
                    "night_weather": cast.get("nightweather", ""),
                    "night_temp": cast.get("nighttemp", ""),
                })
            return out
        except Exception as e:
            logger.warning(f"高德天气查询失败: {type(e).__name__}: {e}")
            return []

    def nearby(self, location: str, keywords: str = "美食", radius: int = 1000, limit: int = 3) -> list:
        """查某坐标周边（默认美食），丰富行程内容。"""
        if not self.enabled or not location:
            return []
        try:
            r = self._session.get(f"{self.BASE}/place/around", params={
                "location": location, "keywords": keywords, "radius": radius,
                "key": self.api_key, "offset": limit, "sortrule": "weight",
            }, timeout=20)
            d = r.json()
            out = []
            for p in (d.get("pois") or [])[:limit]:
                out.append({
                    "name": p.get("name", ""),
                    "address": p.get("address") if isinstance(p.get("address"), str) else "",
                    "distance": p.get("distance", ""),
                })
            return out
        except Exception as e:
            logger.warning(f"高德周边查询失败: {type(e).__name__}: {e}")
            return []
