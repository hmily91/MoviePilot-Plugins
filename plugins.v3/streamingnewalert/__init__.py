# -*- coding: utf-8 -*-
"""
MoviePilot V3 插件：影视平台上新提醒（StreamingNewAlert）

功能概述：
    定时抓取爱奇艺 / 优酷 / 腾讯视频三大平台的「新上线正片」（电影、电视剧），
    按「平台 + 内容类型 + 内容名称 + 上映年份」做永久去重，
    通过 MoviePilot 全局消息通道推送提醒（复用用户已配置的微信 / 钉钉 / Telegram 等渠道）。

设计要点：
    1. 主类继承 MoviePilot V3 插件基类（app.sdk.plugin._PluginBase，自动兼容旧路径）。
    2. 定时任务通过 get_service() 注册 CronTrigger，由宿主调度器统一管理，不自建后台调度器。
    3. 「立即执行」通过插件配置开关 + 宿主一次性任务接口（app.sdk.scheduler）实现，并带线程兜底。
    4. 已推送记录持久化在插件数据目录（get_data_path()）下的 pushed_history.json，重启不丢失。
    5. 全部网络与解析逻辑均做异常兜底，单平台失败不影响其他平台，绝不向主程序抛出未捕获异常。
    6. 不引入任何需要手动安装的第三方依赖：优先使用 requests（MoviePilot 宿主自带），
       缺失时自动回退到标准库 urllib。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# 一、宿主依赖导入
# ---------------------------------------------------------------------------
# 设计原则：**这一段绝对不能抛出异常**。
# MoviePilot 的加载器（app/runtime/extensions/plugin/lifecycle.py）在
#   instance = plugin(); instance.init_plugin(...)
# 外层虽然兜了 try/except，但一旦这里抛出 ImportError/AttributeError 等，
# 插件就会被标记为 LOAD_FAILED（前端提示「插件加载失败」）。
# 因此所有宿主依赖都走「安全导入」：拿不到就降级为 None，功能受限但不影响加载。
# ============================================================================

import logging as _std_logging


def _safe_import(loader):
    """
    执行导入回调；任何异常都吞掉并返回 None。

    :param loader: 无参回调，内部完成 import 并返回目标对象
    :return: 导入成功返回对象，失败返回 None
    """
    try:
        return loader()
    except BaseException:  # noqa: BLE001  宿主差异不可预测，必须全部兜住
        return None


# 1.0 先准备一个「最小日志器」，保证后续任何阶段都能记录诊断信息
logger = (
    _safe_import(lambda: __import__("app.sdk.logging", fromlist=["logger"]).logger)
    or _safe_import(lambda: __import__("app.log", fromlist=["logger"]).logger)
    or _std_logging.getLogger("StreamingNewAlert")
)

# 1.1 系统原生日志方法（宿主 logger 优先，缺失时退回标准库）
logger = _safe_import(lambda: __import__("app.sdk.logging", fromlist=["logger"]).logger) \
    or _safe_import(lambda: __import__("app.log", fromlist=["logger"]).logger) \
    or _std_logging.getLogger("StreamingNewAlert")


# 1.2 插件基类：V3 SDK -> 旧版 app.plugins -> 终极兜底基类
#     注意 1：必须绑定到 _PluginBase 这个名字，且主类直接以 _PluginBase 作为父类，
#             加载器与安装扫描都靠这个字面量识别插件类。
#     注意 2：MoviePilot V3 的加载器只接受**小写目录名**
#             （loader.py: targets = [plugin_id.lower()]），
#             所以插件物理目录必须是 streamingnewalert，而 plugin.json 的 id 保持原始大小写。
def _import_plugin_base():
    """按优先级尝试导入宿主插件基类。"""
    for module_path, attr in (
        ("app.sdk.plugin", "_PluginBase"),
        ("app.sdk.plugin", "PluginBase"),
        ("app.sdk.plugin.base", "_PluginBase"),
        ("app.plugins", "_PluginBase"),
        ("app.plugins", "PluginBase"),
    ):
        module = _safe_import(lambda p=module_path: __import__(p, fromlist=[attr]))
        if module is not None and hasattr(module, attr):
            return getattr(module, attr)
    return None


_PluginBase = _import_plugin_base()

if _PluginBase is None:  # pragma: no cover - 仅在宿主结构完全变化时的最后兜底
    # 兜底基类：保证本模块能被导入、插件类能被实例化，不会因基类缺失直接崩溃。
    # 此时插件功能不可用，但会在日志中明确提示原因，便于排查。
    class _PluginBase:  # type: ignore[no-redef]
        plugin_name = ""
        plugin_desc = ""
        plugin_order = 9999

        def __init__(self):
            pass

        def init_plugin(self, config: dict = None):
            pass

        def get_state(self):
            return False

    try:
        logger.error(
            "[影视上新] 未能导入宿主插件基类 _PluginBase，插件已降级为不可用状态，"
            "请检查 MoviePilot 版本是否兼容（要求 >=3.0.0）"
        )
    except Exception:  # noqa: BLE001
        pass

# 兼容别名
PluginBase = _PluginBase

# 1.3 系统配置（读取代理、TMDB Key 等）
settings = _safe_import(lambda: __import__("app.sdk.config", fromlist=["settings"]).settings) \
    or _safe_import(lambda: __import__("app.core.config", fromlist=["settings"]).settings)

# 1.4 消息类型 / 消息渠道（推送用，缺失时退化为字符串/None）
NotificationType = _safe_import(
    lambda: getattr(__import__("app.schemas", fromlist=["NotificationType"]), "NotificationType", None)
) or _safe_import(
    lambda: getattr(__import__("app.schemas.types", fromlist=["NotificationType"]), "NotificationType", None)
)

MessageChannel = _safe_import(
    lambda: getattr(__import__("app.schemas", fromlist=["MessageChannel"]), "MessageChannel", None)
) or _safe_import(
    lambda: getattr(__import__("app.schemas.types", fromlist=["MessageChannel"]), "MessageChannel", None)
)

# 1.5 事件管理器与事件类型（远程命令用）
eventmanager = _safe_import(
    lambda: getattr(__import__("app.sdk.events", fromlist=["eventmanager"]), "eventmanager", None)
) or _safe_import(
    lambda: getattr(__import__("app.core.event", fromlist=["eventmanager"]), "eventmanager", None)
)

EventType = _safe_import(
    lambda: getattr(__import__("app.schemas.types", fromlist=["EventType"]), "EventType", None)
)

# 1.6 定时触发器（缺失时退化为 interval 描述）
CronTrigger = _safe_import(
    lambda: getattr(
        __import__("apscheduler.triggers.cron", fromlist=["CronTrigger"]), "CronTrigger", None
    )
)

# 1.7 HTTP 客户端：优先 requests（宿主一般自带），缺失则用标准库 urllib
requests = _safe_import(lambda: __import__("requests"))

import urllib.error
import urllib.request
from urllib.parse import urlencode


# ============================================================================
# 二、常量定义
# ============================================================================

PLUGIN_ID = "StreamingNewAlert"

# 支持的平台（key 与配置项一一对应）
PLATFORMS: Dict[str, str] = {
    "iqiyi": "爱奇艺",
    "youku": "优酷",
    "tencent": "腾讯视频",
}

# 内容类型（key 与配置项一一对应）
CONTENT_TYPES: Dict[str, str] = {
    "movie": "电影",
    "tv": "电视剧",
}

# 非正片 / 待上线状态的排除关键词（命中即丢弃）
EXCLUDE_KEYWORDS: List[str] = [
    "预告", "预告片", "先行版", "终极预告", "定档", "官宣", "开机", "杀青",
    "花絮", "幕后", "特辑", "彩蛋", "片段", "精彩片段", "cut", "CUT", "剪辑",
    "解说", "解读", "影评", "盘点", "混剪", "reaction", "Reaction",
    "纪录片", "综艺", "短视频", "微短剧", "番外", "衍生", "花絮特辑",
    "待播", "即将上映", "即将上线", "即将播出", "未上映", "未开播", "敬请期待",
    "主题曲", "推广曲", "插曲", "片尾曲", "MV", "OST", "原声",
    "首映礼", "路演", "发布会", "专访", "采访", "预告花絮",
]

# 明确的「未上线」状态词（出现在状态字段中时直接排除）
STATUS_OFFLINE_WORDS: List[str] = [
    "预告", "待播", "即将上映", "即将上线", "即将播出", "未上映", "未开播", "敬请期待", "定档",
]

# 明确的「已上线」状态词（用于状态字段校验，命中之一或状态为空即视为可播）
STATUS_ONLINE_WORDS: List[str] = [
    "上线", "热播", "正在热播", "正在播放", "正片", "独播", "会员", "vip", "VIP",
    "免费", "付费", "已更新", "全集", "播出中", "热映", "正在热映",
]

# 剧集「更新集数」标记：命中说明是每日更新提醒而非首次正式上线，电视剧类直接跳过。
# 注意：这里只匹配「集数」，不匹配「第二季 / 第二部」，因为续作季播属于新的正片内容。
EPISODE_PATTERN = re.compile(
    r"(第\s*\d+\s*集"
    r"|更新至\s*\d+\s*集|更新到\s*\d+\s*集|至\s*\d+\s*集|更新\s*\d+\s*集"
    r"|EP\s*\d+)",
    re.IGNORECASE,
)

# 常见标题字段（用于从 JSON 数据中递归提取条目）
TITLE_KEYS: Tuple[str, ...] = (
    "title", "name", "displayName", "albumName", "tvName", "videoName", "showName",
    "itemName", "playName", "titleCn", "videoTitle", "albumTitle", "subTitle", "titleName",
)

# 常见上线时间字段
DATE_KEYS: Tuple[str, ...] = (
    "publishDate", "onlineDate", "releaseDate", "firstPublishTime", "publishTime",
    "onlineTime", "releaseTime", "publishTimeStamp", "upDate", "updateTime", "showDate",
    "issueDate", "airDate", "launchDate", "date", "time", "datetime", "startTime",
)

# 常见年份字段
YEAR_KEYS: Tuple[str, ...] = ("year", "publishYear", "releaseYear", "showYear", "years")

# 常见类型 / 频道字段
TYPE_KEYS: Tuple[str, ...] = ("type", "channel", "category", "contentType", "albumType", "videoType", "kind")

# 随机 UA 池（伪装成普通浏览器，降低触发反爬的概率）
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1",
]


# ============================================================================
# 三、数据模型
# ============================================================================


@dataclass
class MediaItem:
    """一条候选上新内容。"""

    platform: str                      # 平台 key：iqiyi / youku / tencent
    platform_label: str                # 平台中文名
    mtype: str                         # 内容类型 key：movie / tv
    mtype_label: str                   # 内容类型中文名
    name: str                          # 内容名称
    year: str                          # 上映 / 首播年份（可能为空字符串）
    online_date: str                   # 上线日期，格式 YYYY-MM-DD
    extra: Dict[str, Any] = field(default_factory=dict)  # 原始数据片段，便于排障

    @property
    def dedup_key(self) -> str:
        """去重标识：平台 + 内容类型 + 内容名称 + 上映年份。"""
        return self.build_key(self.platform, self.mtype, self.name, self.year)

    @staticmethod
    def build_key(platform: str, mtype: str, name: str, year: str) -> str:
        """生成去重标识（名称做归一化，避免空格、标点、全半角差异导致重复推送）。"""
        return f"{platform}|{mtype}|{normalize_name(name)}|{year or ''}"


# ============================================================================
# 四、通用工具函数
# ============================================================================


def normalize_name(name: str) -> str:
    """名称归一化：去除空白、标点、全半角差异与剧集更新标记，用于去重比对。"""
    if not name:
        return ""
    text = str(name).strip()
    # 去掉「更新至xx集」这类后缀，避免同一部剧因集数变化被当成新内容
    text = EPISODE_PATTERN.sub("", text)
    # 全角空格转半角（其余标点会由下方正则统一剔除）
    text = text.replace("　", " ")
    # 只保留中英文、数字
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text)
    return text.lower()


def extract_year(*candidates: Any) -> str:
    """从任意候选值中提取 4 位年份。"""
    for value in candidates:
        if value is None:
            continue
        text = str(value)
        match = re.search(r"(19|20)\d{2}", text)
        if match:
            return match.group(0)
    return ""


def parse_online_date(value: Any) -> Optional[datetime]:
    """
    解析上线日期，支持多种常见格式：
        - 10 位 / 13 位时间戳
        - 2026-10-11 / 2026/10/11 / 2026.10.11 / 2026年10月11日
        - 2026-10-11 10:00:00
        - 10月11日（按当前年份补全）
        - 今天 / 昨天 / 前天 / N天前 / N小时前（相对时间）
    无法解析时返回 None。
    """
    if value is None or value == "":
        return None

    # 时间戳
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            number = float(value)
            if number > 1e12:  # 毫秒
                number = number / 1000
            if number > 1e8:
                return datetime.fromtimestamp(number)
        except Exception:
            return None

    text = str(value).strip()
    now = datetime.now()

    # 相对时间
    if text in ("今天", "今日"):
        return now
    if text in ("昨天", "昨日"):
        return now - timedelta(days=1)
    if text in ("前天",):
        return now - timedelta(days=2)
    match = re.match(r"^(\d+)\s*(分钟|小时|天|周)前$", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
            "周": timedelta(weeks=amount),
        }[unit]
        return now - delta

    # 绝对时间
    formats = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y.%m.%d",
        "%Y年%m月%d日", "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 只有月日：按当前年份补全（跨年时回退一年）
    match = re.match(r"^(\d{1,2})月(\d{1,2})日$", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        try:
            candidate = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        if candidate > now + timedelta(days=1):
            candidate = candidate.replace(year=now.year - 1)
        return candidate
    return None


def build_headers(referer: str = "") -> Dict[str, str]:
    """构造随机 UA 的请求头。"""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def http_request(
    url: str,
    method: str = "GET",
    json_body: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    retries: int = 2,
    retry_interval: int = 3,
) -> Optional[str]:
    """
    统一的 HTTP 请求入口。

    :param url: 请求地址
    :param method: GET / POST
    :param json_body: POST 时的 JSON 体
    :param headers: 自定义请求头
    :param timeout: 单次超时秒数
    :param retries: 失败重试次数（默认 2 次）
    :param retry_interval: 重试间隔秒数（默认 3 秒）
    :return: 响应文本，全部失败返回 None
    """
    headers = headers or build_headers()
    proxies = _build_proxies()

    for attempt in range(retries + 1):
        try:
            if requests is not None:
                if method.upper() == "POST":
                    resp = requests.post(
                        url, json=json_body, headers=headers,
                        timeout=timeout, proxies=proxies,
                    )
                else:
                    resp = requests.get(
                        url, headers=headers, timeout=timeout, proxies=proxies,
                    )
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
            # requests 不可用时使用标准库兜底
            data = None
            req_headers = dict(headers)
            if json_body is not None:
                data = json.dumps(json_body).encode("utf-8")
                req_headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            for encoding in ("utf-8", "gbk", "gb18030"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
        except Exception as err:
            if attempt >= retries:
                logger.warn(f"[影视上新] 请求失败（已重试 {retries} 次）：{url} -> {err}")
                return None
            logger.warn(f"[影视上新] 请求异常，{retry_interval} 秒后重试（{attempt + 1}/{retries}）：{err}")
            time.sleep(retry_interval)
    return None


def _build_proxies() -> Optional[Dict[str, str]]:
    """读取 MoviePilot 系统代理配置（用户未配置时返回 None）。"""
    if not settings:
        return None
    proxy = getattr(settings, "PROXY", None)
    if not proxy:
        return None
    return {"http": str(proxy), "https": str(proxy)}


def strip_jsonp(text: str) -> str:
    """去除 JSONP 回调包装，例如 QZOutputJson={...}; -> {...}。"""
    if not text:
        return ""
    stripped = text.strip()
    match = re.match(r"^[A-Za-z_$][\w$]*\s*=\s*(\{.*\}|\[.*\])\s*;?\s*$", stripped, re.S)
    if match:
        return match.group(1)
    match = re.match(r"^[A-Za-z_$][\w$]*\s*\(\s*(\{.*\}|\[.*\])\s*\)\s*;?\s*$", stripped, re.S)
    if match:
        return match.group(1)
    return stripped


def load_json_or_none(text: str) -> Any:
    """尽量把响应文本解析为 JSON，失败返回 None。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return json.loads(strip_jsonp(text))
    except Exception:
        return None


def walk_json_for_items(data: Any, bucket: List[Dict[str, Any]], depth: int = 0) -> None:
    """
    递归遍历任意 JSON 结构，抽取「像一条影片」的字典节点。

    之所以采用通用遍历：各平台接口字段命名差异极大，且随时可能改版，
    通用遍历可以在平台改版后仍然抽到有效数据，避免插件整体失效。
    """
    if depth > 12:
        return
    if isinstance(data, dict):
        title_value = None
        for key in TITLE_KEYS:
            value = data.get(key)
            if isinstance(value, str) and 1 < len(value.strip()) < 80:
                title_value = value.strip()
                break
        if title_value:
            bucket.append(data)
            # 命中后不再继续下钻，避免把子结构中的同名片段重复收录
            return
        for value in data.values():
            walk_json_for_items(value, bucket, depth + 1)
    elif isinstance(data, list):
        for value in data:
            walk_json_for_items(value, bucket, depth + 1)


def extract_embedded_json(html: str) -> List[Any]:
    """从 HTML 中抽取内嵌的 JSON 数据块（前端渲染站点通常把数据放在这里）。"""
    results: List[Any] = []
    patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>",
        r"window\.__INITIAL_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>",
        r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>",
        r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>",
        r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.S):
            parsed = load_json_or_none(match.group(1))
            if parsed is not None:
                results.append(parsed)
    return results


def extract_html_anchor_items(html: str) -> List[Dict[str, str]]:
    """
    HTML 兜底解析：从带 title / alt 的链接标签中提取标题。

    这是最后一道兜底，只保证「不至于零数据」，无法保证上线日期，
    因此这类条目若拿不到日期会在过滤阶段被丢弃（宁可少推，不可错推）。
    """
    items: List[Dict[str, str]] = []
    seen = set()
    for match in re.finditer(r"<a[^>]+(?:title|alt)=[\"']([^\"']{2,60})[\"'][^>]*>", html):
        title = match.group(1).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        items.append({"title": title})
    return items


def detect_mtype(raw: Dict[str, Any], default: str) -> str:
    """根据原始数据判断内容类型（movie / tv），无法判断时使用抓取通道的默认值。"""
    text = ""
    for key in TYPE_KEYS:
        value = raw.get(key)
        if value is not None:
            text += f" {value}"
    # 名称本身也常带类型线索（例如「XXX 网剧」）
    text += f" {raw.get('title', '') or ''}"
    text = str(text)
    if re.search(r"(电视剧|网剧|剧集|连续剧|tv|teleplay|dianshiju)", text, re.IGNORECASE):
        return "tv"
    if re.search(r"(电影|院线|movie|film|dianying)", text, re.IGNORECASE):
        return "movie"
    return default


def get_first(raw: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    """按优先级取出第一个非空字段值。"""
    for key in keys:
        value = raw.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def find_list_by_key(data: Any, key: str, depth: int = 0) -> List[Any]:
    """在任意 JSON 结构中查找指定 key 对应的列表（用于按频道裁剪榜单）。"""
    if depth > 10:
        return []
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list) and value:
            return value
        for sub in data.values():
            found = find_list_by_key(sub, key, depth + 1)
            if found:
                return found
    elif isinstance(data, list):
        for sub in data:
            found = find_list_by_key(sub, key, depth + 1)
            if found:
                return found
    return []


# ============================================================================
# 五、内容过滤器：状态校验与关键词排除
# ============================================================================


class ContentFilter:
    """负责判断一条候选内容是否属于「已正式上线的正片」。"""

    def __init__(
        self,
        days: int = 7,
        enable_movie: bool = True,
        enable_tv: bool = True,
        strict_dates: bool = False,
    ):
        self._days = max(1, int(days or 7))
        self._enable_movie = bool(enable_movie)
        self._enable_tv = bool(enable_tv)
        # 严格日期模式：必须能解析到「近 N 天内」的上线日期才推送；
        # 宽松模式：平台未公开日期时按本次首见处理（仍受永久去重保护，每条只推一次）
        self._strict_dates = bool(strict_dates)

    def is_type_enabled(self, mtype: str) -> bool:
        """该内容类型是否开启推送。"""
        if mtype == "movie":
            return self._enable_movie
        if mtype == "tv":
            return self._enable_tv
        return False

    @staticmethod
    def hit_exclude_keyword(name: str, raw: Dict[str, Any]) -> Optional[str]:
        """命中排除关键词时返回该关键词，否则返回 None。"""
        corpus = f"{name} {json.dumps(raw, ensure_ascii=False)[:800]}"
        for word in EXCLUDE_KEYWORDS:
            if word and word in corpus:
                return word
        return None

    @staticmethod
    def status_offline(raw: Dict[str, Any]) -> bool:
        """状态字段明确显示「未上线」时返回 True。"""
        # 爱奇艺等接口的 isAdvance 表示超前点映 / 预约，尚未正式上线
        if raw.get("isAdvance") in (1, "1", "true", "True", True):
            return True
        for key in ("status", "state", "tag", "tags", "label", "cornerLabel", "subTitle", "desc"):
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                value = " ".join(str(v) for v in value)
            text = str(value)
            if not text:
                continue
            for word in STATUS_OFFLINE_WORDS:
                if word in text:
                    return True
        return False

    def is_recent(self, online: Optional[datetime]) -> bool:
        """判断上线时间是否落在「近 N 天内」（不含未来时间，未来即视为待播）。"""
        if online is None:
            return False
        now = datetime.now()
        if online > now:
            return False
        return online >= now - timedelta(days=self._days)

    def check(self, item: MediaItem, raw: Dict[str, Any]) -> Tuple[bool, str]:
        """
        综合校验。

        :return: (是否通过, 未通过原因)；通过时原因为空字符串
        """
        if not self.is_type_enabled(item.mtype):
            return False, "内容类型未开启"

        keyword = self.hit_exclude_keyword(item.name, raw)
        if keyword:
            return False, f"命中排除词「{keyword}」"

        if self.status_offline(raw):
            return False, "状态为预告/待播"

        if item.mtype == "tv" and EPISODE_PATTERN.search(item.name):
            # 电视剧：标题带集数说明是「更新提醒」而非「首次上线」
            return False, "电视剧集数更新，非首次上线"

        online = parse_online_date(item.online_date)
        if online is not None:
            # 能解析到日期：必须落在近 N 天内且不是未来时间
            if not self.is_recent(online):
                return False, "上线日期不在时间范围内"
        elif self._strict_dates:
            # 严格模式：拿不到日期视为无法确认上线时间，直接丢弃
            return False, "上线日期缺失（严格模式）"
        # 宽松模式：平台未公开日期，按「本次首见」处理，配合永久去重只推送一次

        return True, ""


# ============================================================================
# 六、平台数据源
# ============================================================================


class TmdbSource:
    """
    TMDB 数据源（推荐）。

    TMDB 提供「观看平台（Watch Providers）」数据，可以按
    「地区 CN + 平台（爱奇艺 / 优酷 / 腾讯视频）+ 上线日期区间」精确查询，
    并且每条都有明确的 release_date / first_air_date —— 这正好补上了
    三大平台公开接口「不返回上线日期」的短板。

    实现要点：
        1. provider_id 不硬编码：先调用 /watch/providers 列表按名称匹配，平台改名也不失效；
        2. provider_id 结果做进程内缓存，避免每次任务重复请求；
        3. 电视剧只取 first_air_date（首播日期），天然满足「仅首次上线提醒一次」。
    """

    # 地区：中国大陆
    REGION = "CN"

    # 平台在 TMDB 上的名称别名（小写匹配）
    PROVIDER_ALIASES: Dict[str, Tuple[str, ...]] = {
        "iqiyi": ("iqiyi", "爱奇艺"),
        "youku": ("youku", "优酷"),
        "tencent": ("tencent video", "腾讯视频", "tencent"),
    }

    # 需要排除的 TMDB 类型 ID：99=纪录片、10764=真人秀/综艺、10767=脱口秀
    EXCLUDE_GENRES: Dict[str, set] = {
        "movie": {99},
        "tv": {10764, 10767},
    }

    def __init__(self):
        self._provider_cache: Dict[str, Dict[str, Optional[int]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 基础

    @staticmethod
    def _base_url() -> str:
        """TMDB API 根地址（优先使用 MoviePilot 系统设置里的镜像域名）。"""
        domain = "api.themoviedb.org"
        if settings is not None:
            domain = str(getattr(settings, "TMDB_API_DOMAIN", "") or domain).strip() or domain
        domain = domain.replace("https://", "").replace("http://", "").rstrip("/")
        return f"https://{domain}/3"

    @staticmethod
    def resolve_api_key(config_key: str = "") -> str:
        """
        解析 TMDB API Key：优先插件配置，其次 MoviePilot 系统设置。

        MoviePilot 本身就依赖 TMDB，绝大多数用户已在系统设置里填过 Key，
        因此这里留空即可自动复用。
        """
        if config_key:
            return str(config_key).strip()
        if settings is not None:
            return str(getattr(settings, "TMDB_API_KEY", "") or "").strip()
        return ""

    # ------------------------------------------------------------------ 平台ID

    def _provider_id(self, kind: str, platform_key: str, api_key: str) -> Optional[int]:
        """查询某平台在 TMDB 上的 provider_id（带缓存）。"""
        cache_key = f"{kind}"
        with self._lock:
            cached = self._provider_cache.get(cache_key)
        if cached is None:
            cached = self._load_providers(kind, api_key)
            with self._lock:
                self._provider_cache[cache_key] = cached
        return cached.get(platform_key)

    def _load_providers(self, kind: str, api_key: str) -> Dict[str, Optional[int]]:
        """拉取 watch providers 列表并按别名匹配三大平台。"""
        result: Dict[str, Optional[int]] = {key: None for key in self.PROVIDER_ALIASES}
        url = f"{self._base_url()}/watch/providers/{kind}" \
              f"?api_key={api_key}&watch_region={self.REGION}"
        try:
            text = http_request(url, headers=build_headers())
            data = load_json_or_none(text or "")
            for provider in (data or {}).get("results", []):
                name = str(provider.get("provider_name") or "").lower()
                for key, aliases in self.PROVIDER_ALIASES.items():
                    if result.get(key):
                        continue
                    for alias in aliases:
                        if alias in name:
                            result[key] = provider.get("provider_id")
                            break
        except Exception as err:
            logger.warn(f"[影视上新] TMDB 观看平台列表获取失败：{err}")
        logger.debug(f"[影视上新] TMDB provider 解析结果：{result}")
        return result

    # ------------------------------------------------------------------ 查询

    def fetch(
        self,
        platform_key: str,
        platform_label: str,
        mtype: str,
        days: int,
        api_key: str,
    ) -> List[MediaItem]:
        """
        查询「近 N 天内、在中国大陆该平台上线的」电影 / 剧集。

        :param platform_key: iqiyi / youku / tencent
        :param platform_label: 平台中文名
        :param mtype: movie / tv
        :param days: 近 N 天
        :param api_key: TMDB API Key
        """
        kind = "movie" if mtype == "movie" else "tv"
        provider_id = self._provider_id(kind, platform_key, api_key)
        if not provider_id:
            logger.warn(
                f"[影视上新] TMDB 未找到 {platform_label} 的观看平台 ID，"
                f"该平台本次改用内置数据源"
            )
            return []

        today = datetime.now().date()
        start = today - timedelta(days=max(1, int(days)) - 1)
        date_from, date_to = start.isoformat(), today.isoformat()

        params = {
            "api_key": api_key,
            "language": "zh-CN",
            "sort_by": "release_date.desc" if kind == "movie" else "first_air_date.desc",
            "include_adult": "false",
            "page": 1,
            "watch_region": self.REGION,
            "with_watch_providers": provider_id,
        }
        if kind == "movie":
            params["release_date.gte"] = date_from
            params["release_date.lte"] = date_to
        else:
            params["first_air_date.gte"] = date_from
            params["first_air_date.lte"] = date_to

        url = f"{self._base_url()}/discover/{kind}?{urlencode(params)}"
        text = http_request(url, headers=build_headers())
        data = load_json_or_none(text or "")
        if not isinstance(data, dict):
            return []

        exclude = self.EXCLUDE_GENRES.get(kind, set())
        items: List[MediaItem] = []
        for raw in data.get("results", []) or []:
            name = (raw.get("title") or raw.get("name")
                    or raw.get("original_title") or raw.get("original_name") or "")
            if not name:
                continue
            # 排除纪录片 / 综艺 / 脱口秀
            if set(raw.get("genre_ids") or []) & exclude:
                continue
            online_date = raw.get("release_date") or raw.get("first_air_date") or ""
            items.append(
                MediaItem(
                    platform=platform_key,
                    platform_label=platform_label,
                    mtype=mtype,
                    mtype_label=CONTENT_TYPES.get(mtype, "影视"),
                    name=str(name).strip(),
                    year=extract_year(online_date),
                    online_date=str(online_date),
                    extra={"tmdb_id": raw.get("id"), "genre_ids": raw.get("genre_ids")},
                )
            )
        return items


_TMDB_SOURCE: Optional[TmdbSource] = None
_TMDB_LOCK = threading.Lock()


def get_tmdb_source() -> TmdbSource:
    """获取 TMDB 数据源单例（provider_id 缓存需要跨任务复用）。"""
    global _TMDB_SOURCE
    with _TMDB_LOCK:
        if _TMDB_SOURCE is None:
            _TMDB_SOURCE = TmdbSource()
        return _TMDB_SOURCE


class PlatformFetcher:
    """
    平台抓取器基类。

    每个平台按顺序尝试若干候选数据源（TMDB / 聚合 API 优先，平台内置接口与 HTML 兜底），
    任一条通道成功即可返回数据；全部失败时返回空列表，由上层记录日志。
    """

    platform_key: str = ""
    platform_label: str = ""

    # 候选数据源：{内容类型: [ {url, method, json, referer, kind} ... ]}
    ENDPOINTS: Dict[str, List[Dict[str, Any]]] = {}

    def fetch(
        self,
        mtype: str,
        api_base: str = "",
        api_token: str = "",
        tmdb_key: str = "",
        days: int = 7,
    ) -> List[MediaItem]:
        """
        抓取指定内容类型的新上线内容。

        数据源优先级：
            1. TMDB（有精确上线日期，优先）
            2. 用户自定义的第三方影视聚合 API
            3. 平台内置公开接口 / HTML 页面兜底

        :param mtype: movie / tv
        :param api_base: 用户自定义的第三方影视聚合 API 地址，支持 {platform}/{type} 占位符
        :param api_token: 自定义 API 的 Bearer Token（可选）
        :param tmdb_key: TMDB API Key，为空表示不使用 TMDB
        :param days: 只取近 N 天内上线的内容
        """
        items: List[MediaItem] = []

        # 6.0 优先使用 TMDB：按「观看平台 + 地区 + 上线日期区间」精确查询
        if tmdb_key:
            try:
                items = get_tmdb_source().fetch(self.platform_key, self.platform_label,
                                                mtype, days, tmdb_key)
            except Exception as err:
                logger.warn(f"[影视上新] TMDB 查询 {self.platform_label} 失败：{err}")
            if items:
                logger.info(
                    f"[影视上新] {self.platform_label}·{CONTENT_TYPES.get(mtype)} "
                    f"TMDB 返回 {len(items)} 条候选"
                )
                return items

        # 6.1 其次使用用户配置的第三方聚合 API
        if api_base:
            items = self._fetch_from_custom_api(mtype, api_base, api_token)
            if items:
                logger.info(
                    f"[影视上新] {self.platform_label}·{CONTENT_TYPES.get(mtype)} "
                    f"自定义聚合API 返回 {len(items)} 条候选"
                )
                return items

        # 6.2 逐个尝试内置候选数据源
        for endpoint in self.ENDPOINTS.get(mtype, []):
            try:
                text = http_request(
                    url=endpoint["url"],
                    method=endpoint.get("method", "GET"),
                    json_body=endpoint.get("json"),
                    headers=build_headers(endpoint.get("referer", "")),
                )
                if not text:
                    continue
                raw_items = self._parse_response(text, endpoint.get("kind", "auto"), mtype)
                for raw in raw_items:
                    item = self._to_media_item(raw, mtype)
                    if item:
                        items.append(item)
                if items:
                    logger.info(
                        f"[影视上新] {self.platform_label}·{CONTENT_TYPES.get(mtype)} "
                        f"数据源 {endpoint['url']} 解析到 {len(items)} 条候选"
                    )
                    break
            except Exception as err:
                logger.warn(
                    f"[影视上新] {self.platform_label} 数据源 {endpoint.get('url')} 解析失败：{err}"
                )
                logger.debug(traceback.format_exc())
        return items

    # ---------------------------------------------------------------- 内部方法

    def _fetch_from_custom_api(self, mtype: str, api_base: str, api_token: str) -> List[MediaItem]:
        """调用用户自定义的影视聚合 API（需返回 JSON，字段由通用解析器适配）。"""
        url = str(api_base).replace("{platform}", self.platform_key).replace("{type}", mtype)
        headers = build_headers()
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        try:
            text = http_request(url, headers=headers)
            if not text:
                return []
            raw_items = self._parse_response(text, "json", mtype)
            return [item for item in (self._to_media_item(raw, mtype) for raw in raw_items) if item]
        except Exception as err:
            logger.warn(f"[影视上新] 自定义聚合API 调用失败：{err}")
            return []

    def _select_subtree(self, data: Any, mtype: str) -> Any:
        """
        按内容类型裁剪数据结构（钩子方法，供具体平台重写）。

        默认返回原始数据；腾讯视频等多频道接口需要按频道筛掉综艺、动漫等无关榜单。
        """
        return data

    def _parse_response(self, text: str, kind: str = "auto", mtype: str = "movie") -> List[Dict[str, Any]]:
        """把响应文本解析为原始条目的字典列表。"""
        bucket: List[Dict[str, Any]] = []

        if kind in ("auto", "json"):
            data = load_json_or_none(text)
            if data is not None:
                walk_json_for_items(self._select_subtree(data, mtype), bucket)

        # JSON 解析无结果时，尝试从 HTML 内嵌数据 / 链接标签中提取
        if not bucket and kind in ("auto", "html"):
            for embedded in extract_embedded_json(text):
                walk_json_for_items(self._select_subtree(embedded, mtype), bucket)
        if not bucket:
            bucket.extend(extract_html_anchor_items(text))

        # 去重（同一条数据可能被多个字段重复收录）
        unique: List[Dict[str, Any]] = []
        seen = set()
        for raw in bucket:
            title = str(raw.get("title") or raw.get("name") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            unique.append(raw)
        return unique

    def _to_media_item(self, raw: Dict[str, Any], default_mtype: str) -> Optional[MediaItem]:
        """把一条原始数据转换为 MediaItem（无法识别标题时返回 None）。"""
        name = str(raw.get("title") or raw.get("name") or "").strip()
        if not name:
            return None

        mtype = detect_mtype(raw, default_mtype)
        date_value = get_first(raw, DATE_KEYS)
        online_dt = parse_online_date(date_value)
        year = str(get_first(raw, YEAR_KEYS) or "")
        if not year:
            year = extract_year(name, online_dt.year if online_dt else "")

        return MediaItem(
            platform=self.platform_key,
            platform_label=self.platform_label,
            mtype=mtype,
            mtype_label=CONTENT_TYPES.get(mtype, "影视"),
            name=name,
            year=year,
            online_date=online_dt.strftime("%Y-%m-%d") if online_dt else "",
            extra={k: raw.get(k) for k in list(raw.keys())[:12]},
        )


class IQiyiFetcher(PlatformFetcher):
    """爱奇艺数据源。"""

    platform_key = "iqiyi"
    platform_label = "爱奇艺"

    # 说明：爱奇艺公开的「上新榜」接口已下线，这里使用仍可用的片库推荐接口
    # （channel_id=1 电影 / channel_id=2 电视剧）作为主通道，页面作为兜底。
    ENDPOINTS = {
        "movie": [
            {
                "url": "https://pcw-api.iqiyi.com/search/recommend/list?channel_id=1&data_type=1"
                       "&mode=11&page_id=1&ret_num=48&session=1",
                "method": "GET", "kind": "auto", "referer": "https://www.iqiyi.com/dianying/",
            },
            {
                "url": "https://www.iqiyi.com/dianying/",
                "method": "GET", "kind": "html", "referer": "https://www.iqiyi.com/",
            },
        ],
        "tv": [
            {
                "url": "https://pcw-api.iqiyi.com/search/recommend/list?channel_id=2&data_type=1"
                       "&mode=11&page_id=1&ret_num=48&session=1",
                "method": "GET", "kind": "auto", "referer": "https://www.iqiyi.com/dianshiju/",
            },
            {
                "url": "https://www.iqiyi.com/dianshiju/",
                "method": "GET", "kind": "html", "referer": "https://www.iqiyi.com/",
            },
        ],
    }


class YoukuFetcher(PlatformFetcher):
    """优酷数据源。"""

    platform_key = "youku"
    platform_label = "优酷"

    ENDPOINTS = {
        "movie": [
            {
                "url": "https://www.youku.com/category/show/c_96_s_1_d_1.html",
                "method": "GET", "kind": "auto", "referer": "https://www.youku.com/",
            },
            {
                "url": "https://www.youku.com/category/show/c_96.html",
                "method": "GET", "kind": "auto", "referer": "https://www.youku.com/",
            },
            {
                "url": "https://list.youku.com/category/show/c_96.html",
                "method": "GET", "kind": "html", "referer": "https://www.youku.com/",
            },
        ],
        "tv": [
            {
                "url": "https://www.youku.com/category/show/c_97_s_1_d_1.html",
                "method": "GET", "kind": "auto", "referer": "https://www.youku.com/",
            },
            {
                "url": "https://www.youku.com/category/show/c_97.html",
                "method": "GET", "kind": "auto", "referer": "https://www.youku.com/",
            },
            {
                "url": "https://list.youku.com/category/show/c_97.html",
                "method": "GET", "kind": "html", "referer": "https://www.youku.com/",
            },
        ],
    }


class TencentFetcher(PlatformFetcher):
    """腾讯视频数据源。"""

    platform_key = "tencent"
    platform_label = "腾讯视频"

    URL = "https://pbaccess.video.qq.com/trpc.videosearch.hot_rank.HotRankServantHttp/HotRankHttp"

    # 腾讯视频榜单：navItemList 中 tabId=1 电影、tabId=2 电视剧
    TAB_IDS = {"movie": {"1"}, "tv": {"2"}}

    ENDPOINTS = {
        "movie": [
            {
                "url": URL,
                "method": "POST",
                "json": {"pageNum": 0, "pageSize": 50, "hotRankType": 1},
                "kind": "auto",
                "referer": "https://v.qq.com/channel/movie",
            },
            {
                "url": "https://v.qq.com/channel/movie",
                "method": "GET", "kind": "html", "referer": "https://v.qq.com/",
            },
        ],
        "tv": [
            {
                "url": URL,
                "method": "POST",
                "json": {"pageNum": 0, "pageSize": 50, "hotRankType": 1},
                "kind": "auto",
                "referer": "https://v.qq.com/channel/tv",
            },
            {
                "url": "https://v.qq.com/channel/tv",
                "method": "GET", "kind": "html", "referer": "https://v.qq.com/",
            },
        ],
    }

    def _select_subtree(self, data: Any, mtype: str) -> Any:
        """只保留指定频道的榜单，避免把综艺、动漫、纪录片榜的内容混进来。"""
        nav = find_list_by_key(data, "navItemList")
        if not nav:
            return data
        wanted = self.TAB_IDS.get(mtype, set())
        selected = [item for item in nav if isinstance(item, dict) and str(item.get("tabId")) in wanted]
        return selected or data


FETCHERS: Dict[str, PlatformFetcher] = {
    "iqiyi": IQiyiFetcher(),
    "youku": YoukuFetcher(),
    "tencent": TencentFetcher(),
}


# ============================================================================
# 七、去重库（插件数据目录下的 JSON 文件）
# ============================================================================


class PushHistory:
    """
    已推送记录持久化存储。

    数据落盘在插件数据目录（get_data_path()）下的 pushed_history.json，
    重启插件 / 重启容器均不丢失。
    """

    FILE_NAME = "pushed_history.json"

    def __init__(self, data_path: Path):
        self._file: Path = Path(data_path) / self.FILE_NAME
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"version": 1, "updated_at": "", "items": {}}
        self._load()

    # ------------------------------------------------------------------ 读写

    def _load(self) -> None:
        """从磁盘加载记录（文件损坏时自动备份并重建，避免插件不可用）。"""
        try:
            if self._file.exists():
                with open(self._file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
                    self._data = loaded
                    logger.info(
                        f"[影视上新] 已加载去重库：{len(self._data.get('items', {}))} 条记录"
                    )
                    return
                logger.warn("[影视上新] 去重库格式异常，已备份并重建")
                self._file.rename(self._file.with_suffix(".json.bak"))
        except Exception as err:
            logger.warn(f"[影视上新] 读取去重库失败，将重建：{err}")
        self._data = {"version": 1, "updated_at": "", "items": {}}

    def _flush(self) -> None:
        """写入磁盘（先写临时文件再替换，避免写到一半导致文件损坏）。"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self._file.with_suffix(".json.tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp_file.replace(self._file)
        except Exception as err:
            logger.error(f"[影视上新] 写入去重库失败：{err}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ 接口

    def contains(self, key: str) -> bool:
        """该内容是否已推送过。"""
        with self._lock:
            return key in self._data.get("items", {})

    def add(self, item: MediaItem) -> None:
        """记录一条已推送内容。"""
        with self._lock:
            self._data.setdefault("items", {})[item.dedup_key] = {
                "platform": item.platform,
                "platform_label": item.platform_label,
                "type": item.mtype,
                "type_label": item.mtype_label,
                "name": item.name,
                "year": item.year,
                "online_date": item.online_date,
                "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._flush()

    def count(self) -> int:
        """已推送记录总数。"""
        with self._lock:
            return len(self._data.get("items", {}))

    def recent(self, limit: int = 30) -> List[Dict[str, Any]]:
        """按推送时间倒序返回最近的记录。"""
        with self._lock:
            items = list(self._data.get("items", {}).values())
        items.sort(key=lambda x: x.get("pushed_at", ""), reverse=True)
        return items[:limit]

    def clear(self) -> int:
        """清空去重库（便于用户重新接收一次全量提醒）。"""
        with self._lock:
            total = len(self._data.get("items", {}))
            self._data = {"version": 1, "updated_at": "", "items": {}}
            self._flush()
        return total


# ============================================================================
# 八、插件主类
# ============================================================================


def _plugin_action(func):
    """条件注册事件装饰器：宿主不支持事件系统时安全跳过。"""
    if eventmanager is None or EventType is None:
        return func
    try:
        return eventmanager.register(getattr(EventType, "PluginAction"))(func)
    except Exception:
        return func


class StreamingNewAlert(_PluginBase):
    """影视平台上新提醒。"""

    # ---------------------------- 插件元信息 ----------------------------
    plugin_name = "影视平台上新提醒"
    plugin_desc = "监控爱奇艺 / 优酷 / 腾讯视频的电影与电视剧上新，按平台分组推送提醒，永久去重不重复打扰。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.1.2"
    plugin_author = "MoviePilot Plugin"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "streamingnew_"
    plugin_order = 60
    auth_level = 1

    # ---------------------------- 运行时状态 ----------------------------
    def __init__(self):
        super().__init__()
        self._enabled = False
        self._platforms: Dict[str, bool] = {k: True for k in PLATFORMS}
        self._types: Dict[str, bool] = {k: True for k in CONTENT_TYPES}
        self._cron = "10:00"
        self._days = 7
        self._notify_title = "🎬 影视平台上新提醒"
        self._api_base = ""
        self._api_token = ""
        self._use_tmdb = True
        self._tmdb_key = ""
        self._strict_dates = False
        self._onlyonce = False
        self._history: Optional[PushHistory] = None
        self._timer: Optional[threading.Timer] = None
        self._last_run: str = ""
        self._last_result: str = ""

    # ---------------------------- 生命周期 ----------------------------

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并建立运行状态（可重复调用）。"""
        config = config or {}
        try:
            self._enabled = bool(config.get("enabled"))
            for key in PLATFORMS:
                self._platforms[key] = bool(config.get(key, True))
            for key in CONTENT_TYPES:
                self._types[key] = bool(config.get(key, True))
            self._cron = self._normalize_cron(config.get("cron") or "10:00")
            self._days = self._safe_int(config.get("days"), 7, 1, 365)
            self._notify_title = str(config.get("notify_title") or "🎬 影视平台上新提醒").strip()
            self._api_base = str(config.get("api_base") or "").strip()
            self._api_token = str(config.get("api_token") or "").strip()
            # TMDB：默认开启，Key 留空则自动复用 MoviePilot 系统设置里的 TMDB API Key
            self._use_tmdb = bool(config.get("use_tmdb", True))
            self._tmdb_key = TmdbSource.resolve_api_key(config.get("tmdb_api_key") or "")
            # strict / loose：拿不到上线日期时是否仍然推送
            self._strict_dates = str(config.get("date_mode") or "loose").lower() == "strict"
            self._onlyonce = bool(config.get("onlyonce"))
            self._last_result = str(config.get("last_result") or "")

            # 去重库：放在插件数据目录，重启不丢失
            if self._history is None:
                self._history = PushHistory(self.get_data_path())

            if not self._enabled:
                logger.info("[影视上新] 插件未启用，跳过初始化后续步骤")
                return

            logger.info(
                f"[影视上新] 插件已启用：平台={self._enabled_platforms()} "
                f"类型={self._enabled_types()} 时间={self._cron} 范围={self._days} 天 "
                f"TMDB={'已启用' if (self._use_tmdb and self._tmdb_key) else '未启用'}"
            )

            # 配置页勾选「立即执行一次」：交给宿主调度器延迟 3 秒执行，避免阻塞保存流程
            if self._onlyonce:
                self._schedule_once()
                # 立即复位开关，避免每次保存都重复触发
                config["onlyonce"] = False
                self._onlyonce = False
                self.update_config(config)
        except Exception as err:
            logger.error(f"[影视上新] 初始化失败：{err}\n{traceback.format_exc()}")

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """停止插件：取消未执行的一次性任务与定时器。"""
        try:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            scheduler_sdk = self._get_scheduler_sdk()
            if scheduler_sdk and hasattr(scheduler_sdk, "remove_plugin_once_job"):
                scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, "streamingnew_once")
        except Exception as err:
            logger.warn(f"[影视上新] 停止服务时出现异常：{err}")
        self._enabled = False

    # ---------------------------- 配置页面 ----------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置页面（Vuetify JSON）+ 默认值。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即执行一次",
                                            "hint": "保存后立即抓取并推送一次（执行后自动复位）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "iqiyi", "label": "监控爱奇艺"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "youku", "label": "监控优酷"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "tencent", "label": "监控腾讯视频"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "movie", "label": "推送电影"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "tv", "label": "推送电视剧"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "days",
                                        "label": "上新时间范围",
                                        "type": "number",
                                        "hint": "只推送近 N 天内上线的内容（单位：天）",
                                        "persistent-hint": True,
                                    },
                                }]},
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "date_mode",
                                        "label": "日期策略",
                                        "items": [
                                            {"title": "宽松：平台未公开日期时按本次首见推送", "value": "loose"},
                                            {"title": "严格：必须解析到近 N 天上线日期才推送", "value": "strict"},
                                        ],
                                        "hint": "三大平台公开接口多数不返回上线日期，严格模式可能导致无内容可推",
                                        "persistent-hint": True,
                                    },
                                }]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "每日执行时间",
                                        "placeholder": "10:00",
                                        "hint": "格式 HH:MM，多个时间用英文逗号分隔，如 10:00,22:00",
                                        "persistent-hint": True,
                                    },
                                }]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "notify_title",
                                        "label": "通知标题",
                                        "placeholder": "🎬 影视平台上新提醒",
                                    },
                                }]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_base",
                                        "label": "自定义聚合API（可选）",
                                        "placeholder": "https://your-api.com/new?platform={platform}&type={type}",
                                        "hint": "填写后优先使用该接口；{platform} 会替换为 iqiyi/youku/tencent，{type} 替换为 movie/tv",
                                        "persistent-hint": True,
                                    },
                                }]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_token",
                                        "label": "API Token（可选）",
                                        "placeholder": "Bearer Token",
                                    },
                                }]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "use_tmdb",
                                        "label": "使用TMDB数据源",
                                        "hint": "按「平台+地区+上线日期」精确查询，推荐开启",
                                        "persistent-hint": True,
                                    },
                                }]},
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "tmdb_api_key",
                                        "label": "TMDB API Key（可选）",
                                        "placeholder": "留空则复用 MoviePilot 系统设置中的 TMDB API Key",
                                    },
                                }]},
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "提示：修改「每日执行时间」后需重载/重启插件生效；"
                                    "已推送内容会永久去重，不会再重复通知。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "iqiyi": True,
            "youku": True,
            "tencent": True,
            "movie": True,
            "tv": True,
            "cron": "10:00",
            "days": 7,
            "notify_title": "🎬 影视平台上新提醒",
            "api_base": "",
            "api_token": "",
            "use_tmdb": True,
            "tmdb_api_key": "",
            "date_mode": "loose",
        }

    def get_page(self) -> List[dict]:
        """插件详情页面：展示运行状态与最近推送记录。"""
        if not self._history:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                                      "text": "插件尚未初始化，请先启用并保存配置。"}}]

        records = self._history.recent(50)
        rows = [
            {"平台": r.get("platform_label", ""), "类型": r.get("type_label", ""),
             "名称": r.get("name", ""), "上线日期": r.get("online_date", ""),
             "推送时间": r.get("pushed_at", "")}
            for r in records
        ]
        return [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VAlert", "props": {
                            "type": "success", "variant": "tonal",
                            "text": f"已推送记录：{self._history.count()} 条"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VAlert", "props": {
                            "type": "info", "variant": "tonal",
                            "text": f"上次运行：{self._last_run or '尚未运行'}"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VAlert", "props": {
                            "type": "info", "variant": "tonal",
                            "text": f"监控范围：{self._enabled_platforms()} / {self._enabled_types()}"}}]},
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VAlert", "props": {
                            "type": "warning", "variant": "tonal",
                            "text": self._last_result or "尚无运行记录"}}]},
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VDataTable", "props": {
                            "headers": [
                                {"title": "平台", "key": "平台"},
                                {"title": "类型", "key": "类型"},
                                {"title": "名称", "key": "名称"},
                                {"title": "上线日期", "key": "上线日期"},
                                {"title": "推送时间", "key": "推送时间"},
                            ],
                            "items": rows,
                            "density": "compact",
                            "items-per-page": 20,
                        }}]},
                ],
            },
        ]

    # ---------------------------- 定时服务 ----------------------------

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时任务（由 MoviePilot 宿主调度器统一管理）。"""
        if not self.get_state():
            return []
        services: List[Dict[str, Any]] = []
        for index, (hour, minute) in enumerate(self._parse_cron_list(self._cron)):
            if CronTrigger is not None:
                trigger = CronTrigger.from_crontab(f"{minute} {hour} * * *")
            else:
                # 极端兜底：每 24 小时执行一次
                trigger = "interval"
            service = {
                "id": f"{self.__class__.__name__}.Check{index}",
                "name": f"影视平台上新检查 {hour}:{minute:02d}",
                "trigger": trigger,
                "func": self.check_now,
                "kwargs": {},
            }
            if CronTrigger is None:
                service["kwargs"] = {"hours": 24}
            services.append(service)
        return services

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令：在聊天渠道发送 /ptnew 立即执行一次。"""
        if EventType is None:
            return []
        return [
            {
                "cmd": "/ptnew",
                "event": getattr(EventType, "PluginAction"),
                "desc": "立即执行一次影视上新检查",
                "category": "插件命令",
                "data": {"action": "streamingnew_run"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：立即执行 / 连通性自检 / 清空去重库。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行一次影视上新检查",
            },
            {
                "path": "/selftest",
                "endpoint": self.api_selftest,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "数据源连通性自检",
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空去重库",
            },
        ]

    # ---------------------------- API 处理 ----------------------------

    def api_run(self) -> Dict[str, Any]:
        """API：立即执行一次检查。"""
        try:
            result = self.check_now()
            return {"success": True, "message": "执行完成", "data": result}
        except Exception as err:
            logger.error(f"[影视上新] 手动执行失败：{err}\n{traceback.format_exc()}")
            return {"success": False, "message": str(err)}

    def api_selftest(self) -> Dict[str, Any]:
        """API：逐个平台做连通性自检，返回各平台解析到的候选条数。"""
        report: Dict[str, Any] = {}
        for key, label in PLATFORMS.items():
            if not self._platforms.get(key):
                report[label] = "未启用"
                continue
            fetcher = FETCHERS.get(key)
            if not fetcher:
                continue
            counts: Dict[str, int] = {}
            for mtype in CONTENT_TYPES:
                if not self._types.get(mtype):
                    continue
                try:
                    items = fetcher.fetch(
                        mtype=mtype,
                        api_base=self._api_base,
                        api_token=self._api_token,
                        tmdb_key=self._tmdb_key if self._use_tmdb else "",
                        days=self._days,
                    )
                    counts[CONTENT_TYPES[mtype]] = len(items)
                except Exception as err:
                    counts[CONTENT_TYPES[mtype]] = f"失败：{err}"
                time.sleep(2)
            report[label] = counts

        # TMDB 连通性诊断：是否能拿到三大平台的观看平台 ID
        tmdb_key = self._tmdb_key if self._use_tmdb else ""
        if not tmdb_key:
            report["TMDB"] = "未启用（未配置 TMDB API Key）"
        else:
            tmdb = get_tmdb_source()
            providers = {}
            for kind in ("movie", "tv"):
                try:
                    providers[kind] = tmdb._load_providers(kind, tmdb_key)
                except Exception as err:
                    providers[kind] = f"失败：{err}"
            report["TMDB"] = providers
        return {"success": True, "data": report}

    def api_clear(self) -> Dict[str, Any]:
        """API：清空去重库。"""
        if not self._history:
            return {"success": False, "message": "插件未初始化"}
        total = self._history.clear()
        return {"success": True, "message": f"已清空 {total} 条去重记录"}

    # ---------------------------- 事件处理 ----------------------------

    @_plugin_action
    def handle_command(self, event: Any) -> None:
        """处理远程命令 /ptnew。"""
        try:
            if not self.get_state():
                return
            data = getattr(event, "event_data", None) or {}
            if data.get("action") != "streamingnew_run":
                return
            logger.info("[影视上新] 收到远程命令，立即执行一次检查")
            self.check_now()
        except Exception as err:
            logger.error(f"[影视上新] 处理远程命令失败：{err}\n{traceback.format_exc()}")

    # ---------------------------- 核心业务 ----------------------------

    def check_now(self) -> Dict[str, Any]:
        """
        执行一次完整检查：抓取 -> 过滤 -> 去重 -> 推送。

        :return: 运行统计字典
        """
        if not self.get_state():
            logger.info("[影视上新] 插件未启用，跳过本次检查")
            return {"pushed": 0, "skipped": "插件未启用"}

        if self._history is None:
            self._history = PushHistory(self.get_data_path())

        started = datetime.now()
        logger.info("[影视上新] ===== 开始检查三大平台上新 =====")
        summary: Dict[str, Any] = {
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "platforms": {},
            "pushed": 0,
            "candidates": 0,
            "filtered": 0,
            "duplicated": 0,
        }

        content_filter = ContentFilter(
            days=self._days,
            enable_movie=self._types.get("movie", True),
            enable_tv=self._types.get("tv", True),
            strict_dates=self._strict_dates,
        )

        new_items: List[MediaItem] = []
        platform_index = 0

        for platform_key, platform_label in PLATFORMS.items():
            if not self._platforms.get(platform_key):
                continue

            # 平台之间的请求间隔至少 2 秒，避免频繁请求触发反爬
            if platform_index > 0:
                time.sleep(2 + random.uniform(0, 0.8))
            platform_index += 1

            fetcher = FETCHERS.get(platform_key)
            if not fetcher:
                continue

            try:
                platform_candidates: List[MediaItem] = []
                for mtype in ("movie", "tv"):
                    if not self._types.get(mtype):
                        continue
                    # 同一平台内两类内容之间也做短暂间隔
                    time.sleep(2)
                    platform_candidates.extend(
                        fetcher.fetch(
                            mtype=mtype,
                            api_base=self._api_base,
                            api_token=self._api_token,
                            tmdb_key=self._tmdb_key if self._use_tmdb else "",
                            days=self._days,
                        )
                    )

                summary["platforms"][platform_label] = {"候选": len(platform_candidates)}
                summary["candidates"] += len(platform_candidates)

                kept: List[MediaItem] = []
                for item in platform_candidates:
                    ok, reason = content_filter.check(item, item.extra)
                    if not ok:
                        summary["filtered"] += 1
                        logger.debug(f"[影视上新] 过滤《{item.name}》：{reason}")
                        continue
                    if self._history.contains(item.dedup_key):
                        summary["duplicated"] += 1
                        continue
                    kept.append(item)

                # 同一批次内也可能出现重复（多数据源叠加），再做一次批内去重
                batch_seen = set()
                for item in kept:
                    if item.dedup_key in batch_seen:
                        continue
                    batch_seen.add(item.dedup_key)
                    new_items.append(item)
                summary["platforms"][platform_label]["新增"] = len(batch_seen)

            except Exception as err:
                # 单个平台异常不影响其他平台
                logger.error(
                    f"[影视上新] {platform_label} 抓取失败，已跳过：{err}\n{traceback.format_exc()}"
                )
                summary["platforms"][platform_label] = {"error": str(err)}
                continue

        # 推送
        if not new_items:
            logger.info("[影视上新] 本次未发现新上线内容，不发送通知")
            summary["pushed"] = 0
        else:
            self._push(new_items)
            for item in new_items:
                self._history.add(item)
            summary["pushed"] = len(new_items)

        finished = datetime.now()
        self._last_run = finished.strftime("%Y-%m-%d %H:%M:%S")
        summary["finished_at"] = self._last_run
        summary["seconds"] = round((finished - started).total_seconds(), 1)
        self._last_result = (
            f"上次运行 {self._last_run}：候选 {summary['candidates']} 条，"
            f"过滤 {summary['filtered']} 条，重复 {summary['duplicated']} 条，"
            f"实际推送 {summary['pushed']} 条"
        )
        logger.info(f"[影视上新] ===== 检查结束：{self._last_result} =====")
        return summary

    def _push(self, items: List[MediaItem]) -> None:
        """按平台分组构造通知内容，并通过系统全局消息通道发送。"""
        try:
            grouped: Dict[str, List[MediaItem]] = {}
            for item in items:
                grouped.setdefault(item.platform_label, []).append(item)

            lines: List[str] = []
            for platform_label in PLATFORMS.values():
                group = grouped.get(platform_label)
                if not group:
                    continue
                group.sort(key=lambda x: (x.online_date, x.name), reverse=True)
                lines.append(f"【{platform_label}】")
                for item in group:
                    date_text = item.online_date or "近期上线"
                    year_text = f"（{item.year}）" if item.year else ""
                    lines.append(f"· {item.name}{year_text} —— {item.mtype_label} / {date_text}")
                lines.append("")

            text = "\n".join(lines).strip()
            title = self._notify_title or "🎬 影视平台上新提醒"
            self.post_message(
                channel=None,  # None 表示走用户已配置的全部通知渠道
                mtype=self._notify_mtype(),
                title=title,
                text=text,
            )
            logger.info(f"[影视上新] 已推送 {len(items)} 条上新提醒")
        except Exception as err:
            logger.error(f"[影视上新] 推送通知失败：{err}\n{traceback.format_exc()}")

    # ---------------------------- 辅助方法 ----------------------------

    @staticmethod
    def _notify_mtype():
        """获取插件消息类型（兼容缺少 Plugin 成员的旧版本）。"""
        if NotificationType is None:
            return None
        return getattr(NotificationType, "Plugin", None) or getattr(NotificationType, "Manual", None)

    @staticmethod
    def _get_scheduler_sdk():
        """获取宿主一次性任务调度器 SDK（不存在时返回 None）。"""
        try:
            from app.sdk import scheduler as scheduler_sdk  # type: ignore
            return scheduler_sdk
        except Exception:
            return None

    def _schedule_once(self) -> None:
        """安排一次「立即执行」：优先使用宿主调度器，否则用线程兜底。"""
        scheduler_sdk = self._get_scheduler_sdk()
        adder = getattr(scheduler_sdk, "add_plugin_once_job", None) if scheduler_sdk else None
        if callable(adder):
            try:
                ok = adder(
                    self.__class__.__name__,
                    "streamingnew_once",
                    self.check_now,
                    "影视上新立即执行一次",
                    3,
                )
                if ok is not False:
                    logger.info("[影视上新] 已向宿主调度器登记一次性执行任务（3 秒后执行）")
                    return
            except Exception as err:
                logger.warn(f"[影视上新] 登记宿主一次性任务失败，改用线程兜底：{err}")
        # 兜底：延迟 3 秒后台执行，避免阻塞配置保存
        self._timer = threading.Timer(3, self._safe_run)
        self._timer.daemon = True
        self._timer.start()
        logger.info("[影视上新] 已安排 3 秒后执行一次（线程兜底）")

    def _safe_run(self) -> None:
        """线程兜底执行入口（吞掉所有异常，绝不崩主程序）。"""
        try:
            self.check_now()
        except Exception as err:
            logger.error(f"[影视上新] 立即执行失败：{err}\n{traceback.format_exc()}")

    def _enabled_platforms(self) -> str:
        """已启用平台的中文描述。"""
        labels = [label for key, label in PLATFORMS.items() if self._platforms.get(key)]
        return "、".join(labels) or "无"

    def _enabled_types(self) -> str:
        """已启用内容类型的中文描述。"""
        labels = [label for key, label in CONTENT_TYPES.items() if self._types.get(key)]
        return "、".join(labels) or "无"

    @staticmethod
    def _safe_int(value: Any, default: int, min_value: int, max_value: int) -> int:
        """安全的整数转换。"""
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, number))

    @staticmethod
    def _normalize_cron(value: str) -> str:
        """规范化时间配置：仅保留合法 HH:MM，非法时回退 10:00。"""
        parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
        valid = [p for p in parts if re.match(r"^([01]?\d|2[0-3]):([0-5]?\d)$", p)]
        if not valid:
            return "10:00"
        normalized = []
        for p in valid:
            hour, minute = p.split(":")
            normalized.append(f"{int(hour):02d}:{int(minute):02d}")
        return ",".join(dict.fromkeys(normalized))

    @staticmethod
    def _parse_cron_list(value: str) -> List[Tuple[int, int]]:
        """把 '10:00,22:00' 解析为 [(10, 0), (22, 0)]。"""
        result: List[Tuple[int, int]] = []
        for part in str(value or "").split(","):
            part = part.strip()
            match = re.match(r"^([01]?\d|2[0-3]):([0-5]?\d)$", part)
            if match:
                result.append((int(match.group(1)), int(match.group(2))))
        return result or [(10, 0)]

# ============================================================================
# 加载自检：模块执行到这里说明完整落盘且可解析。若宿主日志中看不到本行，
# 说明安装到运行目录的 __init__.py 不完整（下载被截断/污染），请改用
# GitHub Release 渠道重新安装（Release 资产带 ZIP 校验，不会静默损坏）。
# ============================================================================
try:
    logger.info(
        "[影视上新] 插件模块加载完成 v%s：插件类 StreamingNewAlert 已定义，等待宿主实例化",
        plugin_version,
    )
except Exception:  # noqa: BLE001  诊断日志绝不影响加载
    pass
