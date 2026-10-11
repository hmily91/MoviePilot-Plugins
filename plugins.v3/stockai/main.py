# -*- coding: utf-8 -*-
"""
股票AI分析助手（StockAi）—— MoviePilot V3 插件
=============================================

功能概述：
    1. 行情获取：通过东方财富公开行情接口获取沪深A股实时行情与近 5 日 K 线（无需密钥）。
    2. LLM 分析：默认复用 MoviePilot 系统「智能助手」已配置的 LLM 接口
       （LLM_API_KEY / LLM_BASE_URL / LLM_MODEL），也可在配置页自定义 OpenAI 兼容接口；
       对个股做技术面 / 短期走势 / 风险提示的 AI 分析。
    3. 定时任务：内置“早盘预判（默认 09:00）”与“收盘分析（默认 15:30）”两个定时任务，
       执行时间可在配置页自定义；支持配置页“立即执行分析”手动触发。
    4. 消息推送：复用 MoviePilot 全局消息通道（微信 / 钉钉 / Telegram 等）推送分析报告。
    5. 数据持久化：自选股列表（配置）、历史分析记录（插件专属数据目录）重启不丢失。

开发规范遵循：
    - 主类继承 app.sdk.plugin._PluginBase，完全适配 V3 插件生命周期。
    - 仅使用 MoviePilot 内置依赖（requests、apscheduler 等），不引入额外第三方库。
    - 所有外部调用均被 try/except 包裹并输出日志，绝不中断主程序流程。
"""

import json
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger

# MoviePilot V3 稳定 SDK 导入（插件应优先使用 app.sdk 入口）
from app.sdk.config import settings          # 系统配置（含智能助手 LLM 配置）
from app.sdk.logging import logger           # 系统原生日志
from app.sdk.plugin import _PluginBase        # V3 插件基类
from app.sdk import scheduler as scheduler_sdk  # 宿主调度器（用于一次性任务）


# --------------------------------------------------------------------------- #
# 常量定义
# --------------------------------------------------------------------------- #

# 配置项默认值（与 plugin.json 的 config / schema 保持一致）
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,                 # 启用插件
    "onlyonce": False,                # 立即执行一次
    "stocks": "",                     # 自选股票代码（英文逗号分隔）
    "morning_time": "09:00",          # 早盘预判执行时间
    "close_time": "15:30",            # 收盘分析执行时间
    "push_morning": True,             # 推送早盘预判
    "push_close": True,               # 推送收盘分析
    "skip_holiday": True,             # 休市日自动跳过
    "notify_title": "📈 股票AI分析报告",  # 通知标题
    "llm_source": "system",           # LLM 配置来源：system / custom
    "custom_api_key": "",             # 自定义 LLM API Key
    "custom_base_url": "",            # 自定义 LLM 接口地址
    "custom_model": "",               # 自定义模型名称
}

# 东方财富公开行情接口（无需密钥）
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline"

# 单只股票请求间隔（秒），避免触发接口限流
REQUEST_INTERVAL = 1.0

# 请求失败重试次数与间隔（LLM 调用）
LLM_MAX_RETRIES = 2        # 失败后再重试 2 次
LLM_RETRY_INTERVAL = 3.0   # 重试间隔 3 秒

# 历史分析记录最多保留条数
MAX_HISTORY = 30

# 内置“简单法定休市日”示例（仅作演示，请按需补充当年完整休市日）
# 说明：A股休市日以交易所公告为准，这里仅内置少数固定节日作为示例判断。
BUILTIN_HOLIDAYS = {
    "2026-01-01",  # 元旦
    "2026-05-01",  # 劳动节
    "2026-10-01",  # 国庆节
    "2026-10-02",
    "2026-10-03",
}

# 随机 User-Agent 池，降低被风控/封禁的概率
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]


# --------------------------------------------------------------------------- #
# 插件主类
# --------------------------------------------------------------------------- #

class StockAi(_PluginBase):
    """股票AI分析助手插件主类。"""

    # ---------------------- 插件元数据（V3 规范必填） ---------------------- #
    plugin_name = "股票AI分析助手"
    plugin_desc = "获取沪深A股行情，调用LLM进行技术面/走势/风险提示分析，并定时推送早盘预判与收盘报告；支持历史分析前端页面。"
    plugin_icon = "StockAi.png"
    plugin_version = "1.1.3"
    plugin_author = "hmily"
    author_url = "https://github.com/hmily91/MoviePilot-Plugins"
    plugin_config_prefix = "stockai_"      # 配置命名空间前缀
    plugin_order = 50                       # 加载优先级
    auth_level = 1                          # 所需用户等级

    # ---------------------- 运行时状态 ---------------------- #
    _enabled: bool = False
    _config: Dict[str, Any] = {}

    # ===================================================================== #
    # 1. 生命周期方法
    # ===================================================================== #
    def init_plugin(self, config: Optional[dict] = None) -> None:
        """
        初始化插件：读取并合并配置，建立本次运行状态。
        该方法会被宿主重复调用（保存配置 / 重载插件时），必须可重入。
        """
        try:
            self._config = self._merge_config(config or {})
            self._enabled = bool(self._config.get("enabled"))

            # “立即执行分析”处理：保存配置后延迟 3 秒运行一次，并自动关闭开关
            if self._config.get("onlyonce"):
                self._schedule_once_job()
                # 重置开关并回写配置，避免每次重载都触发
                self._config["onlyonce"] = False
                try:
                    self.update_config(self._config)
                except Exception:  # noqa: BLE001
                    logger.error("重置 onlyonce 配置失败：%s", traceback.format_exc())
            logger.info("股票AI分析助手初始化完成，启用状态：%s", self._enabled)
        except Exception:  # noqa: BLE001
            logger.error("插件初始化异常：%s", traceback.format_exc())

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """插件停用 / 重载时释放后台资源（一次性任务）。"""
        self._enabled = False
        try:
            remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
            if remove_once:
                remove_once(self.__class__.__name__, "StockAi.Once")
        except Exception:  # noqa: BLE001
            logger.error("清理一次性任务异常：%s", traceback.format_exc())

    # ===================================================================== #
    # 2. 配置表单（V3：返回 Vuetify 组件树 + 默认数据）
    # ===================================================================== #
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回配置页面 UI 定义与默认配置。
        第一项为表单组件树（Vuetify JSON），第二项为默认/建议数据。
        """
        form = [
            {
                "component": "VForm",
                "content": [
                    # 第一行：启用开关 + 立即执行（每个字段都必须包裹在 VRow/VCol 中，
                    # 否则 V3 前端渲染时会与相邻字段重叠错位）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即执行分析（保存后立即运行一次）"}},
                            ]},
                        ],
                    },
                    # 第二行：自选股（独占整行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "stocks",
                                    "label": "自选股票代码",
                                    "placeholder": "600519,000001,300750",
                                    "rows": 3,
                                    "hint": "沪深A股代码，多只用英文逗号分隔",
                                    "persistent-hint": True,
                                }},
                            ]},
                        ],
                    },
                    # 第三行：两个执行时间（各占半行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "morning_time", "label": "早盘预判执行时间(HH:MM)", "placeholder": "09:00"}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "close_time", "label": "收盘分析执行时间(HH:MM)", "placeholder": "15:30"}},
                            ]},
                        ],
                    },
                    # 第四行：三个推送/跳过开关（各占三分之一行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "push_morning", "label": "推送早盘预判"}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "push_close", "label": "推送收盘分析"}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "skip_holiday", "label": "休市日自动跳过"}},
                            ]},
                        ],
                    },
                    # 第五行：通知标题（独占整行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "notify_title", "label": "通知标题", "placeholder": "📈 股票AI分析报告",
                                }},
                            ]},
                        ],
                    },
                    # 第六行：LLM 配置来源（独占整行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VSelect", "props": {
                                    "model": "llm_source",
                                    "label": "LLM配置来源",
                                    "items": [
                                        {"title": "跟随系统智能助手", "value": "system"},
                                        {"title": "自定义配置", "value": "custom"},
                                    ],
                                }},
                            ]},
                        ],
                    },
                    # 第七行：自定义 LLM 的 Key 与接口地址（各占半行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "custom_api_key", "label": "自定义LLM API Key", "type": "password"}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "custom_base_url", "label": "自定义LLM接口地址", "placeholder": "https://api.openai.com/v1"}},
                            ]},
                        ],
                    },
                    # 第八行：自定义模型名称（独占整行）
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "custom_model", "label": "自定义模型名称", "placeholder": "gpt-4o",
                                }},
                            ]},
                        ],
                    },
                ],
            }
        ]
        return form, dict(DEFAULT_CONFIG)

    # ===================================================================== #
    # 3. 详情页（历史分析前端页面：列表展示 + 筛选 + 立即分析 / 清空）
    # ===================================================================== #
    def get_page(self) -> List[dict]:
        """
        返回插件详情页——历史分析前端页面。

        页面采用 MoviePilot V3 标准的 Vuetify JSON 组件树描述，具备：
          1) 顶部统计卡：总条数 / 当前筛选展示条数；
          2) 筛选按钮（全部 / 早盘预判 / 收盘分析），通过 events.api 调用
             /set_filter 并自动刷新本页；
          3) 操作按钮「立即分析」「清空历史」，分别调用 /run、/clear；
          4) 历史记录卡片列表（按时间倒序），每张卡片展示该次分析的
             时间、类型、各股票名称/代码与 AI 分析结论。

        说明：页面内容为服务端根据持久化数据实时渲染，调用 API 后会自动刷新。
        """
        try:
            history = self._load_history()
            mode_filter = self._get_filter()
        except Exception:  # noqa: BLE001
            history = []
            mode_filter = "all"

        # 按筛选条件过滤展示内容
        if mode_filter and mode_filter != "all":
            shown = [h for h in history if h.get("mode") == mode_filter]
        else:
            shown = history

        components: List[dict] = []

        # ---- 顶部统计 + 筛选 + 操作 卡片 ----
        components.append({
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-3"},
            "content": [
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        # 注意：显示文字必须放在组件顶层 "text" 键（渲染为插槽内容），
                        # 放进 props 会绑定成属性导致文字不显示。
                        {"component": "VCol", "props": {"cols": 12, "md": 7}, "content": [
                            {"component": "div", "props": {"class": "text-h6"},
                             "text": f"📈 历史分析记录（共 {len(history)} 条，当前展示 {len(shown)} 条）"},
                        ]},
                        {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                            {"component": "VBtn", "props": {
                                "size": "small", "variant": "text", "class": "mr-1",
                                "color": "primary" if mode_filter == "all" else "default",
                            },
                             "text": "全部",
                             "events": {"click": {"api": "plugin/StockAi/set_filter",
                                                  "method": "get", "params": {"mode": "all"}}}},
                            {"component": "VBtn", "props": {
                                "size": "small", "variant": "text", "class": "mr-1",
                                "color": "primary" if mode_filter == "morning" else "default",
                            },
                             "text": "早盘预判",
                             "events": {"click": {"api": "plugin/StockAi/set_filter",
                                                  "method": "get", "params": {"mode": "morning"}}}},
                            {"component": "VBtn", "props": {
                                "size": "small", "variant": "text",
                                "color": "primary" if mode_filter == "close" else "default",
                            },
                             "text": "收盘分析",
                             "events": {"click": {"api": "plugin/StockAi/set_filter",
                                                  "method": "get", "params": {"mode": "close"}}}},
                        ]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VBtn", "props": {
                                "color": "primary", "variant": "flat", "prepend-icon": "mdi-play",
                            },
                             "text": "立即分析",
                             "events": {"click": {"api": "plugin/StockAi/run", "method": "post"}}},
                        ]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VBtn", "props": {
                                "color": "error", "variant": "outlined", "prepend-icon": "mdi-delete",
                            },
                             "text": "清空历史",
                             "events": {"click": {"api": "plugin/StockAi/clear", "method": "post"}}},
                        ]},
                    ]},
                ]},
            ],
        })

        # 无记录时给出友好提示
        if not shown:
            components.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal",
                          "text": "暂无分析记录。启用插件并保存配置后，每日 09:00 / 15:30 将自动生成；"
                                  "也可点击上方「立即分析」手动生成。"},
            })
            return components

        # ---- 历史记录卡片列表（时间倒序）----
        cards: List[dict] = []
        for rec in shown:
            mode = rec.get("mode")
            stocks = rec.get("stocks") or []
            stock_blocks: List[dict] = []
            for i, s in enumerate(stocks):
                if i > 0:
                    stock_blocks.append({"component": "VDivider", "props": {"class": "my-2"}})
                stock_blocks.append({
                    "component": "div", "props": {"class": "text-subtitle-1 font-weight-bold mb-1"},
                    "text": f"{s.get('name')}（{s.get('code')}）",
                })
                # 分析结论文本由 Vue 插槽渲染（自动转义），按行拆分还原换行排版
                analysis_lines = str(s.get("analysis") or "（无分析结论）").split("\n")
                for line in analysis_lines:
                    if line.strip():
                        stock_blocks.append({
                            "component": "div", "props": {"class": "text-body-2"},
                            "text": line,
                        })
            cards.append({
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "text-h6 mb-2"},
                         "text": f"🕒 {rec.get('time')} ｜ {self._mode_label(mode)} ｜ {len(stocks)} 只"},
                    ] + stock_blocks},
                ],
            })

        components.append({
            "component": "div",
            "props": {"class": "mt-2"},
            "content": cards,
        })
        return components

    # ===================================================================== #
    # 4. 插件 API（V3：get_api 返回路由声明，前缀 /api/v1/plugin/StockAi）
    #    页面通过 events.api 调用，鉴权方式 bear，调用后自动刷新数据页。
    # ===================================================================== #
    def get_api(self) -> List[dict]:
        """注册历史分析相关接口。"""
        return [
            {
                "path": "/history",
                "endpoint": self.api_get_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询历史分析记录（支持 mode=all|morning|close 筛选）",
            },
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动立即执行一次分析（早盘+收盘），并写入历史",
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空全部历史分析记录",
            },
            {
                "path": "/set_filter",
                "endpoint": self.api_set_filter,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "设置历史页筛选条件（mode=all|morning|close）",
            },
        ]

    # ---- 4.1 API 端点实现（**kwargs 接收 query/body 参数，返回 JSON）----
    def api_get_history(self, **kwargs: Any) -> Dict[str, Any]:
        """GET /history：返回历史分析记录（按 mode 筛选）。"""
        mode = (kwargs.get("mode") or "").strip()
        history = self._load_history()
        if mode and mode != "all":
            history = [h for h in history if h.get("mode") == mode]
        return {"code": 0, "total": len(history), "data": history}

    def api_run(self, **kwargs: Any) -> Dict[str, Any]:
        """POST /run：手动立即分析（不受休市跳过限制），并写入历史。"""
        try:
            self._manual_run()
            return {"code": 0, "message": "已触发分析，稍候刷新本页即可查看最新记录"}
        except Exception:  # noqa: BLE001
            logger.error("手动立即分析异常：%s", traceback.format_exc())
            return {"code": 1, "message": "分析执行失败，详见日志"}

    def api_clear(self, **kwargs: Any) -> Dict[str, Any]:
        """POST /clear：清空历史分析记录。"""
        try:
            self.save_data("analysis_history", [])
            return {"code": 0, "message": "历史记录已清空"}
        except Exception:  # noqa: BLE001
            logger.error("清空历史记录异常：%s", traceback.format_exc())
            return {"code": 1, "message": "清空失败，详见日志"}

    def api_set_filter(self, **kwargs: Any) -> Dict[str, Any]:
        """GET /set_filter?mode=xxx：设置历史页筛选条件。"""
        mode = (kwargs.get("mode") or "all").strip()
        if mode not in ("all", "morning", "close"):
            mode = "all"
        try:
            self.save_data("history_filter", mode)
            return {"code": 0, "message": "筛选条件已更新", "mode": mode}
        except Exception:  # noqa: BLE001
            logger.error("设置筛选条件异常：%s", traceback.format_exc())
            return {"code": 1, "message": "设置失败，详见日志"}

    # V3 要求但本插件未使用的扩展点，返回空即可
    def get_command(self) -> List[dict]:
        return []

    # ===================================================================== #
    # 4. 定时服务（V3：get_service 返回服务定义，由宿主调度器托管）
    # ===================================================================== #
    def get_service(self) -> List[dict]:
        """
        注册两个固定定时任务：早盘预判、收盘分析。
        时间来自配置项，并做格式校验；cron 设为每天执行，由 _should_skip 控制休市跳过。
        """
        if not self.get_state():
            return []

        services: List[dict] = []
        try:
            m_h, m_min = self._parse_time(self._config.get("morning_time", "09:00"))
            services.append({
                "id": "StockAi.Morning",
                "name": "股票AI早盘预判",
                "trigger": CronTrigger.from_crontab(f"{m_min} {m_h} * * *"),
                "func": self._scheduled_morning,
                "kwargs": {},
            })
        except Exception:  # noqa: BLE001
            logger.error("早盘预判定时任务注册失败：%s", traceback.format_exc())

        try:
            c_h, c_min = self._parse_time(self._config.get("close_time", "15:30"))
            services.append({
                "id": "StockAi.Close",
                "name": "股票AI收盘分析",
                "trigger": CronTrigger.from_crontab(f"{c_min} {c_h} * * *"),
                "func": self._scheduled_close,
                "kwargs": {},
            })
        except Exception:  # noqa: BLE001
            logger.error("收盘分析定时任务注册失败：%s", traceback.format_exc())

        return services

    # ===================================================================== #
    # 5. 定时任务入口（被调度器调用）
    # ===================================================================== #
    def _scheduled_morning(self) -> None:
        """早盘预判定时入口（受开关与休市跳过控制）。"""
        self._do_analysis("morning", manual=False)

    def _scheduled_close(self) -> None:
        """收盘分析定时入口（受开关与休市跳过控制）。"""
        self._do_analysis("close", manual=False)

    def _do_analysis(self, mode: str, manual: bool = False) -> None:
        """
        统一执行分析流程：
            mode   = "morning" 早盘预判 / "close" 收盘分析
            manual = True 表示手动触发（立即执行），不跳过休市日，便于测试
        """
        try:
            if not self.get_state():
                return
            # 按推送开关分流：未开启对应推送则跳过（避免无意义消耗 LLM 额度）
            if mode == "morning" and not self._config.get("push_morning"):
                logger.info("未开启“推送早盘预判”，跳过本次早盘分析")
                return
            if mode == "close" and not self._config.get("push_close"):
                logger.info("未开启“推送收盘分析”，跳过本次收盘分析")
                return
            # 休市日跳过（手动触发时不跳过）
            if not manual and self._should_skip():
                logger.info("当前为休市日且已开启跳过，取消 %s 分析", self._mode_label(mode))
                return

            results = self._analyze_stocks(mode)
            if not results:
                logger.info("本次 %s 无可用分析结果，不推送通知", self._mode_label(mode))
                return

            # 持久化历史记录
            self._save_history(results, mode)
            # 推送通知
            self._push_results(results, mode)
        except Exception:  # noqa: BLE001
            logger.error("%s 分析流程异常：%s", self._mode_label(mode), traceback.format_exc())

    def _schedule_once_job(self) -> None:
        """通过宿主调度器注册一次性任务，实现“保存后立即运行一次”。"""
        try:
            add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
            if add_once:
                add_once(
                    self.__class__.__name__,   # 插件ID
                    "StockAi.Once",            # 任务ID
                    self._run_once,            # 执行函数
                    "股票AI分析立即执行",         # 任务名称
                    delay_seconds=3,           # 延迟 3 秒执行
                )
                logger.info("已注册“立即执行分析”一次性任务")
            else:
                # 兼容未提供该接口的旧版本：直接同步执行
                logger.warning("当前主程序不支持 add_plugin_once_job，改为同步执行")
                self._run_once()
        except Exception:  # noqa: BLE001
            logger.error("注册立即执行任务异常：%s", traceback.format_exc())

    def _run_once(self) -> None:
        """
        手动“立即执行分析”：按各自推送开关分别运行早盘/收盘；
        若两个推送开关都关闭，则仍做一次收盘分析并推送，便于测试效果。
        """
        did = False
        if self._config.get("push_morning"):
            self._do_analysis("morning", manual=True)
            did = True
        if self._config.get("push_close"):
            self._do_analysis("close", manual=True)
            did = True
        if not did:
            results = self._analyze_stocks("close")
            if results:
                self._save_history(results, "close")
                self._push_results(results, "close")

    def _manual_run(self) -> None:
        """
        手动「立即分析」按钮（/run）触发：对早盘与收盘分别分析并写入历史；
        推送仍遵循各自的推送开关。此入口不受休市跳过限制，便于测试与即时查看。
        """
        for mode in ("morning", "close"):
            try:
                results = self._analyze_stocks(mode)
                if not results:
                    logger.info("手动分析 %s 无可用结果", self._mode_label(mode))
                    continue
                self._save_history(results, mode)
                if self._config.get(f"push_{mode}"):
                    self._push_results(results, mode)
            except Exception:  # noqa: BLE001
                logger.error("手动分析 %s 异常：%s", self._mode_label(mode), traceback.format_exc())

    # ===================================================================== #
    # 6. 核心分析流程
    # ===================================================================== #
    def _analyze_stocks(self, mode: str) -> List[Dict[str, Any]]:
        """
        对全部自选股执行：获取行情 -> 获取K线 -> 构造Prompt -> 调用LLM -> 收集结果。
        任意单只股票失败都跳过并继续，不影响其它股票。
        """
        results: List[Dict[str, Any]] = []
        stocks = self._parse_stocks()
        if not stocks:
            logger.warning("未配置有效的自选股票代码，跳过分析")
            return results

        for market, code in stocks:
            try:
                quote = self._fetch_quote(market, code)
                if not quote:
                    logger.warning("股票 %s 行情获取失败，跳过该股票", code)
                    continue
                klines = self._fetch_kline(market, code)
                prompt = self._build_prompt(quote, klines, mode)
                analysis = self._call_llm(prompt)
                results.append({
                    "code": quote["code"],
                    "name": quote["name"],
                    "price": quote["price"],
                    "pct": quote["pct"],
                    "change": quote["change"],
                    "open": quote["open"],
                    "high": quote["high"],
                    "low": quote["low"],
                    "prev_close": quote["prev_close"],
                    "amplitude": quote["amplitude"],
                    "volume": quote["volume"],
                    "amount": quote["amount"],
                    "analysis": analysis,
                })
            except Exception:  # noqa: BLE001
                logger.error("分析股票 %s 时发生异常，跳过：%s", code, traceback.format_exc())
            # 控制请求频率：单只股票之间间隔 1 秒
            time.sleep(REQUEST_INTERVAL)

        logger.info("本次 %s 分析完成，成功 %d / 共 %d 只", self._mode_label(mode), len(results), len(stocks))
        return results

    # ===================================================================== #
    # 7. 行情数据获取（东方财富公开接口）
    # ===================================================================== #
    def _fetch_quote(self, market: str, code: str) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。
        字段（fltt=2 返回数值）：
            f43 最新价, f44 最高, f45 最低, f46 今开, f47 成交量(手), f48 成交额(元),
            f57 代码, f58 名称, f60 昨收, f168 涨跌幅(%), f169 涨跌额, f170 振幅(%)
        """
        secid = f"{market}.{code}"
        url = (f"{QUOTE_URL}?secid={secid}"
               "&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170"
               "&fltt=2&invt=2")
        try:
            resp = requests.get(url, headers=self._random_headers(), timeout=10)
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            if not data or data.get("f58") is None:
                logger.warning("行情接口返回空数据，代码=%s", code)
                return None
            return {
                "code": code,
                "name": data.get("f58"),
                "price": data.get("f43"),
                "open": data.get("f46"),
                "high": data.get("f44"),
                "low": data.get("f45"),
                "prev_close": data.get("f60"),
                "pct": data.get("f168"),
                "change": data.get("f169"),
                "amplitude": data.get("f170"),
                "volume": data.get("f47"),
                "amount": data.get("f48"),
            }
        except Exception:  # noqa: BLE001
            logger.error("获取股票 %s 行情失败：%s", code, traceback.format_exc())
            return None

    def _fetch_kline(self, market: str, code: str) -> List[str]:
        """
        获取近 5 日日K线（klt=101 日线，lmt=5 最近5条）。
        返回字符串列表，每条格式：
            日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
        """
        secid = f"{market}.{code}"
        url = (f"{KLINE_URL}?secid={secid}&klt=101&fqt=0&lmt=5&end=20500101"
               "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58")
        try:
            resp = requests.get(url, headers=self._random_headers(), timeout=10)
            resp.raise_for_status()
            klines = ((resp.json() or {}).get("data") or {}).get("klines") or []
            return list(klines)
        except Exception:  # noqa: BLE001
            logger.error("获取股票 %s K线失败：%s", code, traceback.format_exc())
            return []

    # ===================================================================== #
    # 8. LLM 智能分析引擎
    # ===================================================================== #
    def _get_llm_config(self) -> Optional[Dict[str, str]]:
        """
        获取 LLM 配置：
            1) 若配置来源为“自定义”且三项齐全，优先使用自定义配置；
            2) 否则复用系统「智能助手」配置（LLM_API_KEY / LLM_BASE_URL / LLM_MODEL）。
        返回 None 表示无可用配置。
        """
        source = self._config.get("llm_source", "system")
        if source == "custom":
            key = (self._config.get("custom_api_key") or "").strip()
            base = (self._config.get("custom_base_url") or "").strip()
            model = (self._config.get("custom_model") or "").strip()
            if key and base and model:
                return {"api_key": key, "base_url": base, "model": model}
            logger.warning("LLM配置来源为“自定义”，但存在未填写项，回退到系统智能助手配置")

        # 复用系统「智能助手」配置
        key = getattr(settings, "LLM_API_KEY", None)
        base = getattr(settings, "LLM_BASE_URL", None)
        model = getattr(settings, "LLM_MODEL", None)
        if key and base and model:
            return {"api_key": key, "base_url": base, "model": model}

        logger.warning("未找到可用的 LLM 配置（系统智能助手或自定义均未完整配置）")
        return None

    def _call_llm(self, prompt: str) -> str:
        """
        调用 OpenAI 兼容接口进行股票分析。
        失败自动重试 2 次（间隔 3 秒）；仍失败返回“分析接口暂时不可用”。
        """
        cfg = self._get_llm_config()
        if not cfg:
            return "未配置可用的LLM接口（系统智能助手或自定义）。"

        base = cfg["base_url"].rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 400,
        }

        last_err = None
        # 第 1 次 + 重试 2 次 = 共 3 次尝试
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json() or {}
                choices = data.get("choices") or []
                if choices:
                    return (choices[0].get("message", {}) or {}).get("content", "").strip()
                logger.warning("LLM 返回结果为空：%s", data)
                return "分析接口暂时不可用"
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.error("LLM 调用失败（第 %d 次）：%s", attempt + 1, traceback.format_exc())
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(LLM_RETRY_INTERVAL)

        logger.error("LLM 调用在重试 %d 次后仍失败：%s", LLM_MAX_RETRIES, last_err)
        return "分析接口暂时不可用"

    # ===================================================================== #
    # 9. 消息推送
    # ===================================================================== #
    def _push_results(self, results: List[Dict[str, Any]], mode: str) -> None:
        """
        按股票分组推送分析报告。
        所有股票均失败时（results 为空）不发送空通知。
        """
        if not results:
            logger.info("无可推送结果，跳过通知")
            return

        title = self._config.get("notify_title") or "📈 股票AI分析报告"
        blocks: List[str] = [f"【{self._mode_label(mode)}】"]
        for r in results:
            line = (
                f"📊 {r['name']}({r['code']})\n"
                f"现价：{self._fmt(r['price'])}　涨跌幅：{self._fmt(r['pct'])}%\n"
                f"今开：{self._fmt(r['open'])}　最高：{self._fmt(r['high'])}　"
                f"最低：{self._fmt(r['low'])}　昨收：{self._fmt(r['prev_close'])}\n"
                f"振幅：{self._fmt(r['amplitude'])}%　成交量：{self._humanize(r['volume'])}手　"
                f"成交额：{self._humanize(r['amount'])}\n"
                f"AI分析：{r['analysis']}"
            )
            blocks.append(line)

        text = "\n\n".join(blocks)
        try:
            # 复用 MoviePilot 全局消息通道（微信 / 钉钉 / Telegram 等）
            self.post_message(title=title, text=text)
            logger.info("已推送 %s 分析报告（%d 只）", self._mode_label(mode), len(results))
        except Exception:  # noqa: BLE001
            logger.error("推送分析报告失败：%s", traceback.format_exc())

    # ===================================================================== #
    # 10. 持久化：历史分析记录（插件专属数据目录，使用系统原生 save_data/get_data）
    # ===================================================================== #
    def _save_history(self, results: List[Dict[str, Any]], mode: str) -> None:
        """
        将本次分析结果写入插件专属数据目录（通过系统原生 save_data 持久化）。
        使用 list 结构，最新记录置于最前，并裁剪到 MAX_HISTORY 条。
        """
        try:
            history = self._load_history()
            record = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "stocks": [
                    {"code": r["code"], "name": r["name"], "analysis": r["analysis"]}
                    for r in results
                ],
            }
            history.insert(0, record)
            history = history[:MAX_HISTORY]
            self.save_data("analysis_history", history)
        except Exception:  # noqa: BLE001
            logger.error("保存历史分析记录失败：%s", traceback.format_exc())

    def _load_history(self) -> List[dict]:
        """读取历史分析记录（供详情页与 /history 接口使用）。"""
        try:
            data = self.get_data("analysis_history")
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            logger.error("读取历史分析记录失败：%s", traceback.format_exc())
        return []

    def _get_filter(self) -> str:
        """读取历史页当前筛选条件（默认 all）。"""
        try:
            return self.get_data("history_filter") or "all"
        except Exception:  # noqa: BLE001
            return "all"

    def _set_filter(self, mode: str) -> None:
        """保存历史页筛选条件。"""
        try:
            self.save_data("history_filter", mode or "all")
        except Exception:  # noqa: BLE001
            logger.error("设置历史筛选条件失败：%s", traceback.format_exc())

    # ===================================================================== #
    # 11. 工具方法
    # ===================================================================== #
    def _merge_config(self, config: dict) -> Dict[str, Any]:
        """合并默认值与用户配置，保证所有键都有值。"""
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in (config or {}).items() if v is not None})
        # 校验时间格式，非法则回退默认
        for key in ("morning_time", "close_time"):
            if not self._is_valid_time(merged.get(key)):
                logger.warning("配置项 %s 时间格式非法，回退默认值", key)
                merged[key] = DEFAULT_CONFIG[key]
        return merged

    @staticmethod
    def _is_valid_time(value: Any) -> bool:
        """校验 HH:MM 格式且范围合法。"""
        if not isinstance(value, str):
            return False
        parts = value.strip().split(":")
        if len(parts) != 2:
            return False
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            return False
        return 0 <= h <= 23 and 0 <= m <= 59

    @staticmethod
    def _parse_time(value: str) -> Tuple[int, int]:
        """将 HH:MM 解析为 (hour, minute)。"""
        h, m = value.strip().split(":")
        return int(h), int(m)

    def _parse_stocks(self) -> List[Tuple[str, str]]:
        """
        解析配置中的自选股代码，返回 [(market, code), ...]。
        market: 沪市=1，深市=0。自动校验格式，非法代码仅记日志并忽略。
        """
        raw = (self._config.get("stocks") or "").strip()
        if not raw:
            return []
        out: List[Tuple[str, str]] = []
        for token in raw.replace("\n", ",").replace("，", ",").split(","):
            token = token.strip().upper()
            if not token:
                continue
            norm = self._normalize_code(token)
            if norm:
                out.append(norm)
            else:
                logger.warning("股票代码格式不合法，已忽略：%s", token)
        return out

    @staticmethod
    def _normalize_code(token: str) -> Optional[Tuple[str, str]]:
        """将股票代码规范化为 (market, code)。支持 sh/sz 前缀与纯6位数字。"""
        if token.startswith("SH"):
            return "1", token[2:]
        if token.startswith("SZ"):
            return "0", token[2:]
        if len(token) == 6 and token.isdigit():
            if token[0] == "6":
                return "1", token          # 沪市
            if token[0] in ("0", "3"):
                return "0", token          # 深市（含创业板 3 开头）
        return None

    def _should_skip(self) -> bool:
        """
        判断是否应跳过执行（休市日）。
        周末（周六/周日）或内置休市日 -> 跳过。仅当开启 skip_holiday 时生效。
        """
        if not self._config.get("skip_holiday"):
            return False
        now = datetime.now()
        if now.weekday() >= 5:  # 5=周六, 6=周日
            return True
        if now.strftime("%Y-%m-%d") in BUILTIN_HOLIDAYS:
            return True
        return False

    @staticmethod
    def _mode_label(mode: str) -> str:
        return "早盘预判" if mode == "morning" else "收盘分析"

    @staticmethod
    def _system_prompt() -> str:
        """LLM 角色设定：专业A股证券分析师。"""
        return (
            "你是一位专业的A股证券分析师，风格客观理性，严格遵守合规要求，"
            "不做任何绝对化的收益承诺或买卖指令。请基于用户提供的行情与K线数据，"
            "从技术面（K线、均线、量能）、短期走势判断、核心风险提示三个维度进行分析，"
            "条理清晰、重点突出。"
        )

    def _build_prompt(self, quote: Dict[str, Any], klines: List[str], mode: str) -> str:
        """构造发送给 LLM 的结构化分析 Prompt。"""
        # 整理近 5 日 K 线文本
        kline_lines: List[str] = []
        for k in klines:
            p = k.split(",")
            # p: 日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手率
            if len(p) >= 9:
                kline_lines.append(
                    f"{p[0]} 开:{p[1]} 收:{p[2]} 高:{p[3]} 低:{p[4]} 涨跌幅:{p[8]}%"
                )
        kline_text = "\n".join(kline_lines) if kline_lines else "（无K线数据）"

        # 行情文本
        data_text = (
            f"股票：{quote['name']}({quote['code']})\n"
            f"现价：{self._fmt(quote['price'])}　涨跌幅：{self._fmt(quote['pct'])}%　"
            f"涨跌额：{self._fmt(quote['change'])}\n"
            f"今开：{self._fmt(quote['open'])}　最高：{self._fmt(quote['high'])}　"
            f"最低：{self._fmt(quote['low'])}　昨收：{self._fmt(quote['prev_close'])}　"
            f"振幅：{self._fmt(quote['amplitude'])}%\n"
            f"成交量：{self._humanize(quote['volume'])}手　成交额：{self._humanize(quote['amount'])}\n"
            f"近5日K线：\n{kline_text}"
        )

        if mode == "morning":
            task = ("请在 200 字以内给出当日走势预判与注意点，条理清晰、重点突出，"
                    "并明确标注「仅为技术分析参考，不构成投资建议」。")
        else:
            task = ("请在 200 字以内做收盘总结与后市展望，条理清晰、重点突出，"
                    "并明确标注「仅为技术分析参考，不构成投资建议」。")

        return f"{data_text}\n\n任务：{task}"

    def _random_headers(self) -> Dict[str, str]:
        """构造带随机 User-Agent 的请求头，降低被封禁风险。"""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        }

    @staticmethod
    def _fmt(value: Any) -> str:
        """格式化数值，None 显示为 '--'。"""
        if value is None:
            return "--"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _humanize(num: Any) -> str:
        """将大数值转换为 万 / 亿 便于阅读。"""
        try:
            n = float(num)
        except (TypeError, ValueError):
            return str(num)
        if abs(n) >= 1e8:
            return f"{n / 1e8:.2f}亿"
        if abs(n) >= 1e4:
            return f"{n / 1e4:.2f}万"
        return f"{n:.2f}"

    @staticmethod
    def _escape_html(text: Any) -> str:
        """
        转义 HTML 特殊字符并把换行转为 <br>（当前页面已改为按行拆分渲染，
        保留此工具方法供其它展示场景使用）。
        """
        if text is None:
            return ""
        s = str(text)
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = s.replace("\n", "<br>")
        return s
