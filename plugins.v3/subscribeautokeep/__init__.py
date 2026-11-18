"""
订阅自动保种助手（MoviePilot V3 插件）。

业务目标：使用 MP 官方原生电视剧订阅追剧时，一旦勾选站点出现「同剧、同季、
目标分辨率」的整季打包种子，就按配置清理该订阅产生的一切（订阅任务、下载器
种子、下载文件、转移记录、媒体库文件），清空媒体库目标路径后，再把整季打包
种子交给宿主下载链下载，让 MP 正常完成转移与刮削。

严格遵循 V3 合同：
    - 主类继承 ``app.sdk.plugin._PluginBase``，定义在目录的 __init__.py；
    - 只使用稳定 SDK（app.sdk.logging / app.sdk.events）与公开入口
      （app.db.oper.* / app.chain.* / app.schemas.types / app.scheduler）；
    - 定时任务由 ``get_service()`` 交给宿主调度器，插件不自建调度线程；
    - 配置走 update_config / get_config，运行状态走 save_data / get_data；
    - 插件被动定时扫描，不接管、不修改 MP 原生订阅搜索与下载流程。

判定链见 core.py，证据链与分级清理见 cleaner.py，等待队列见 pending_queue.py。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Optional

from app.sdk.plugin import _PluginBase

from . import core
from .cleaner import CleanupExecutor, EvidenceBuilder
from .pending_queue import QUEUE_DATA_KEY, PendingQueue, PendingTask, make_task_id

try:
    from app.sdk.logging import logger
except ImportError:  # 兜底：极旧宿主或纯本地静态检查
    import logging

    logger = logging.getLogger("SubscribeAutoKeep")


PLUGIN_ID = "SubscribeAutoKeep"

#: 默认配置。所有删除类开关默认取最保守值，测试模式默认开启。
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "test_mode": True,
    "cron": "0 4 * * *",
    "sites": [],
    "resolutions": ["2160p", "1080p"],
    "allow_unknown_resolution": False,
    "include_rules": core.DEFAULT_INCLUDE_RULES,
    "exclude_rules": core.DEFAULT_EXCLUDE_RULES,
    "require_include_rule": True,
    "require_completeness_evidence": False,
    "follow_subscribe_settings": True,
    "verify_episode_count": False,
    "regex_test_text": "",
    "action_delete_subscribe": True,
    "action_delete_torrent": True,
    "action_delete_torrent_files": False,
    "action_delete_local_files": False,
    "action_delete_media_files": False,
    "action_delete_history": True,
    "delete_path_prefixes": "",
    "subscribe_whitelist": "",
    "subscribe_source_blacklist": "",
    "delay_minutes": 15,
    "recheck_missing_threshold": 2,
    "replacement_delay_minutes": 10,
    "replacement_window_minutes": 15,
    "auto_download_replacement": True,
    "notify_on_hit": True,
    "notify_on_queue": True,
    "notify_on_execute": True,
    "notify_on_cancel": True,
    "notify_on_replacement": True,
    "onlyonce": False,
}

#: 最近一次运行/测试报告的持久化键
REPORT_DATA_KEY = "subscribeautokeep_last_report"


def _text(value: Any) -> str:
    """把枚举/数字/None 安全转成字符串。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _lines(value: Any) -> list[str]:
    """把多行文本框内容拆成去空白的字符串列表。"""
    if isinstance(value, (list, tuple)):
        items = [_text(x).strip() for x in value]
    else:
        items = [line.strip() for line in _text(value).splitlines()]
    return [x for x in items if x and not x.startswith("#")]


def _int(value: Any, default: int, low: int, high: int) -> int:
    """把配置值约束为 [low, high] 内的整数。"""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class SubscribeAutoKeep(_PluginBase):
    """订阅自动保种助手主类。"""

    plugin_name = "订阅自动保种助手"
    plugin_desc = "识别同季整季打包资源，自动清理旧分集订阅及其种子/文件/记录，再下载整包以便正常刮削"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "3.0.0"
    plugin_author = "hmily91"
    author_url = "https://github.com/hmily91/MoviePilot-Plugins"
    plugin_config_prefix = "subscribeautokeep_"
    plugin_order = 50
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = dict(DEFAULTS)
        self._enabled = False
        self._config_errors: list[str] = []
        self._queue = PendingQueue()
        self._lock = threading.Lock()
        self._last_report: dict[str, Any] = {}
        self._evidence = EvidenceBuilder(self._log)
        self._cleaner = CleanupExecutor(self._log)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[dict[str, Any]] = None) -> None:
        """读取配置、校验、恢复队列；必须允许重复调用。"""
        merged = dict(DEFAULTS)
        merged.update(config or {})
        self._cfg = merged
        self._enabled = bool(merged.get("enabled"))
        self._config_errors = []

        # 保存时校验正则合法性：非法正则直接提示用户，并阻止本轮扫描
        include = core.parse_rules(merged.get("include_rules"))
        exclude = core.parse_rules(merged.get("exclude_rules"))
        errors = core.validate_rules(include + exclude)
        cron_ok, cron_msg = core.validate_cron(_text(merged.get("cron")))
        if not cron_ok:
            errors.append(f"Cron 表达式非法：{cron_msg}")
        # 季集硬闸门使用内置正则，不参与用户校验；此处只校验用户可编辑部分
        self._config_errors = errors
        for error in errors:
            logger.error(f"【{self.plugin_name}】配置校验失败：{error}")
        if errors:
            self._notify(
                "notify_on_cancel",
                "订阅自动保种助手 · 配置有误",
                "存在非法正则或 Cron，插件已暂停扫描，请修正配置：\n" + "\n".join(errors),
            )

        self._include_rules = include
        self._exclude_rules = exclude
        self._queue = PendingQueue.from_list(self.get_data(QUEUE_DATA_KEY))
        self._last_report = self.get_data(REPORT_DATA_KEY) or {}

        if merged.get("onlyonce"):
            self._schedule_once()
            merged["onlyonce"] = False
            self._cfg["onlyonce"] = False
            try:
                self.update_config({**merged})
            except Exception as error:
                logger.warn(f"【{self.plugin_name}】复位 onlyonce 失败：{error}")

        logger.info(
            f"【{self.plugin_name}】已加载：启用={self._enabled} 测试模式={merged.get('test_mode')} "
            f"Cron={merged.get('cron')} 站点={len(merged.get('sites') or [])} "
            f"分辨率={merged.get('resolutions')} 等待任务={len(self._queue.waiting())}"
        )

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """不注册远程命令，避免与宿主消息入口产生额外副作用。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """注册 Cron 扫描任务与每分钟队列复检任务，全部交给宿主调度器。"""
        if not self.get_state():
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】CronTrigger 不可用，未注册服务：{error}")
            return []

        plugin_id = self.__class__.__name__
        services: list[dict[str, Any]] = []
        try:
            services.append({
                "id": f"{plugin_id}.Scan",
                "name": "订阅自动保种 · 定时检索整季打包",
                "trigger": CronTrigger.from_crontab(_text(self._cfg.get("cron")) or "0 4 * * *"),
                "func": self.scan,
                "kwargs": {},
            })
        except Exception as error:
            logger.error(f"【{self.plugin_name}】Cron 表达式无法注册：{error}")
        services.append({
            "id": f"{plugin_id}.Watch",
            "name": "订阅自动保种 · 等待队列复检与替换观察窗",
            "trigger": CronTrigger.from_crontab("* * * * *"),
            "func": self.watch_queue,
            "kwargs": {},
        })
        return services

    def stop_service(self) -> None:
        """保存队列、取消一次性任务并释放状态；可重复调用。"""
        self._enabled = False
        try:
            self.save_data(QUEUE_DATA_KEY, self._queue.to_list())
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】保存等待队列失败：{error}")
        try:
            from app.sdk import scheduler as scheduler_sdk

            remove = getattr(scheduler_sdk, "remove_plugin_once_job", None)
            if callable(remove):
                remove(self.__class__.__name__, "subscribeautokeep_once")
        except Exception:
            pass
        logger.info(f"【{self.plugin_name}】已停止，后台任务由宿主调度器统一清理")

    def _schedule_once(self) -> None:
        """配置页勾选「立即执行一次」时，交给宿主调度器延迟触发。"""
        try:
            from app.sdk import scheduler as scheduler_sdk

            add = getattr(scheduler_sdk, "add_plugin_once_job", None)
            if callable(add):
                add(
                    self.__class__.__name__,
                    "subscribeautokeep_once",
                    self.scan,
                    "订阅自动保种 · 立即执行一次",
                    delay_seconds=3,
                )
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】一次性任务注册失败：{error}")

    # ------------------------------------------------------------------
    # 配置页 / 详情页
    # ------------------------------------------------------------------

    def _site_options(self) -> list[dict[str, Any]]:
        """从宿主站点表生成多选选项；失败时返回空列表并告警。"""
        try:
            from app.db.oper.site import SiteOper

            rows = SiteOper().list_active() or []
            options = []
            for site in rows:
                site_id = getattr(site, "id", None)
                if site_id is None:
                    continue
                options.append({
                    "title": _text(getattr(site, "name", "")) or f"站点 {site_id}",
                    "value": int(site_id),
                })
            return options
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】读取站点列表失败：{error}")
            return []

    @staticmethod
    def _resolution_options() -> list[dict[str, str]]:
        """分辨率多选选项。"""
        return [{"title": label, "value": key} for key, label in core.RESOLUTION_OPTIONS]

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回 Vuetify JSON 配置页与默认配置模型。"""
        field = lambda model, component, **props: {  # noqa: E731
            "component": component,
            "props": {"model": model, **props},
        }
        row = lambda *cols: {  # noqa: E731
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": width}, "content": items}
                for width, items in cols
            ],
        }
        alert = lambda text, kind="info": {  # noqa: E731
            "component": "VAlert",
            "props": {"type": kind, "variant": "tonal", "density": "compact"},
            "text": text,
        }
        button = lambda text, path, method="post", color="primary": {  # noqa: E731
            "component": "VBtn",
            "props": {"color": color, "variant": "flat", "size": "small", "class": "mr-2 mb-2"},
            "text": text,
            "events": {"click": {"api": f"plugin/{PLUGIN_ID}/{path}", "method": method, "params": {}}},
        }

        form = [
            {
                "component": "VForm",
                "content": [
                    row((12, [alert(
                        "本插件会删除订阅、种子、转移记录与文件，且不可回滚。请务必先用【测试模式】跑一轮，"
                        "核对日志里的证据链；确认无误后再关闭测试模式。",
                        "warning",
                    )])),
                    row(
                        (6, [field("enabled", "VSwitch", label="启用插件")]),
                        (6, [field("test_mode", "VSwitch", label="测试模式（只记日志/通知，不删除、不入队、不下载）")]),
                    ),
                    row(
                        (6, [field("cron", "VTextField", label="Cron 定时表达式", placeholder="0 4 * * *",
                                    hint="5 段：分 时 日 月 周，例如每天 04:00 为 0 4 * * *", **{"persistent-hint": True})]),
                        (6, [field("delay_minutes", "VTextField", label="删前稳定性等待（分钟）",
                                    placeholder="15", hint="命中后持续复检整包种子，期间消失则取消清理",
                                    **{"persistent-hint": True})]),
                    ),
                    row(
                        (12, [field("sites", "VSelect", label="监控站点（多选，留空=全部站点）",
                                    items=self._site_options(), multiple=True, chips=True,
                                    **{"closable-chips": True, "item-title": "title", "item-value": "value",
                                       "persistent-hint": True, "hint": "只检索勾选站点的资源"})]),
                    ),
                    row(
                        (8, [field("resolutions", "VSelect", label="目标分辨率（多选）",
                                   items=self._resolution_options(), multiple=True, chips=True,
                                   **{"closable-chips": True, "item-title": "title", "item-value": "value"})]),
                        (4, [field("allow_unknown_resolution", "VSwitch", label="放行未标注分辨率")]),
                    ),
                    row((12, [alert(
                        "规则格式：每行一条「字段::正则」，字段可选 title / subtitle / release_group / note / any，"
                        "省略字段默认 title；以 # 开头为注释。新增=加一行，删除=删掉该行。保存后可用下方按钮测试。",
                        "info",
                    )])),
                    row(
                        (6, [field("include_rules", "VTextarea", label="包含正则列表（满足任意一条即命中）",
                                   rows=6, **{"auto-grow": True, "persistent-hint": True,
                                              "hint": "内置示例：Complete|Season\\s*\\d+|全集 / 全\\s*\\d+\\s*集"})]),
                        (6, [field("exclude_rules", "VTextarea", label="排除正则列表（命中任意一条即跳过，优先级最高）",
                                   rows=6, **{"auto-grow": True, "persistent-hint": True,
                                              "hint": "先判排除，未命中再判包含"})]),
                    ),
                    row(
                        (6, [field("require_include_rule", "VSwitch", label="必须命中至少一条包含正则")]),
                        (6, [field("require_completeness_evidence", "VSwitch",
                                   label="额外要求全N集/Complete 等整包关键词")]),
                    ),
                    row(
                        (6, [field("follow_subscribe_settings", "VSwitch",
                                   label="跟随 MP 订阅自带设置（站点/分辨率/排除/总集数）")]),
                        (6, [field("verify_episode_count", "VSwitch",
                                   label="校验「全N集」与订阅总集数一致")]),
                    ),
                    row(
                        (12, [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                button("测试正则与季集判定", "test_rules"),
                                button("立即扫描一次", "run"),
                                button("查看等待队列", "queue", "get"),
                                button("清空等待队列", "queue/clear", "post", "error"),
                                button("校验当前配置", "validate", "get"),
                            ]},
                        ]),
                    ),
                    row((12, [alert("执行动作（分级，均可通过开关关闭）", "info")])),
                    row(
                        (6, [field("action_delete_subscribe", "VSwitch", label="L0 删除 MP 原生订阅")]),
                        (6, [field("action_delete_torrent", "VSwitch", label="L1 删除下载器种子（qB/TR）")]),
                    ),
                    row(
                        (6, [field("action_delete_torrent_files", "VSwitch",
                                   label="L1 让下载器同时删种子文件")]),
                        (6, [field("action_delete_local_files", "VSwitch",
                                   label="L1b 删除下载目录具体文件（默认关）")]),
                    ),
                    row(
                        (6, [field("action_delete_media_files", "VSwitch",
                                   label="L3 删除媒体库文件（高危，默认关）")]),
                        (6, [field("action_delete_history", "VSwitch",
                                   label="L2 删除转移记录与下载历史")]),
                    ),
                    row(
                        (12, [field("delete_path_prefixes", "VTextarea", label="删除文件路径白名单（每行一个前缀，留空=不限）",
                                    rows=2, **{"persistent-hint": True,
                                               "hint": "强烈建议填写，例如 /media /downloads；只删证据链登记的具体文件，绝不删目录"})]),
                    ),
                    row((12, [alert(
                        "清理完成后进入替换观察窗：媒体库目标路径已清空，此时下载整季全集，MP 才会正常转移刮削。",
                        "info",
                    )])),
                    row(
                        (6, [field("auto_download_replacement", "VSwitch",
                                   label="延迟后自动下载整季全集（走 MP 下载链）")]),
                        (6, [field("replacement_delay_minutes", "VTextField",
                                   label="清理后延迟下载（分钟，建议 10~15）", placeholder="10")]),
                    ),
                    row(
                        (6, [field("replacement_window_minutes", "VTextField",
                                   label="替换观察窗时长（分钟，期间冷却防重复清理）", placeholder="15")]),
                        (6, [field("recheck_missing_threshold", "VTextField",
                                   label="整包种子连续检索不到几次则取消", placeholder="2")]),
                    ),
                    row(
                        (6, [field("subscribe_whitelist", "VTextarea", label="订阅白名单（每行：订阅ID / 剧名 / 媒体ID）",
                                   rows=3, **{"persistent-hint": True, "hint": "白名单内订阅永不清理"})]),
                        (6, [field("subscribe_source_blacklist", "VTextarea", label="订阅来源黑名单（每行一个 username）",
                                   rows=3, **{"persistent-hint": True,
                                              "hint": "用于排除其他插件写入的订阅；留空则只按「电视剧」类型过滤"})]),
                    ),
                    row((12, [alert("通知开关（沿用 MP 原生通知通道：微信 / Telegram / 等）", "info")])),
                    row(
                        (4, [field("notify_on_hit", "VSwitch", label="命中规则")]),
                        (4, [field("notify_on_queue", "VSwitch", label="进入等待队列")]),
                        (4, [field("notify_on_execute", "VSwitch", label="执行清理")]),
                    ),
                    row(
                        (4, [field("notify_on_cancel", "VSwitch", label="任务取消")]),
                        (4, [field("notify_on_replacement", "VSwitch", label="替换/下载新全集")]),
                        (4, [field("onlyonce", "VSwitch", label="保存后立即扫描一次")]),
                    ),
                ],
            },
        ]
        return form, dict(DEFAULTS)

    def get_page(self) -> list[dict[str, Any]]:
        """详情页：配置校验、规则列表、等待队列与最近一次报告。"""
        errors = self._config_errors or []
        include_rules = core.parse_rules(self._cfg.get("include_rules"))
        exclude_rules = core.parse_rules(self._cfg.get("exclude_rules"))
        rule_rows = [
            {"类别": "包含", "字段": core.FIELD_LABELS.get(r.field, r.field), "正则": r.pattern,
             "合法性": "合法" if not core.validate_rules([r]) else "非法"}
            for r in include_rules
        ] + [
            {"类别": "排除", "字段": core.FIELD_LABELS.get(r.field, r.field), "正则": r.pattern,
             "合法性": "合法" if not core.validate_rules([r]) else "非法"}
            for r in exclude_rules
        ]
        queue_rows = [
            {"订阅": t.subscribe_name, "季": f"S{t.season or '?'}", "状态": t.status,
             "标题": t.torrent_title[:48], "剩余(分)": round(t.remaining_seconds / 60, 1),
             "复核": t.check_count, "说明": t.last_reason or "-"}
            for t in self._queue.all()
        ]
        report = self._last_report or {}
        return [
            {"component": "VAlert", "props": {
                "type": "error" if errors else "success", "variant": "tonal", "density": "compact",
            }, "text": "配置错误：" + "；".join(errors) if errors else "配置校验通过，正则与 Cron 均合法"},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "density": "compact"},
             "text": f"当前模式：{'测试模式（不会删除/下载）' if self._cfg.get('test_mode') else '正式模式'} "
                     f"| 等待任务 {len(self._queue.waiting())} 条 | 观察窗 {len([t for t in self._queue.all() if t.status == 'cooling'])} 条"},
            {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "div", "props": {"class": "text-h6"}, "text": "规则列表（配置页可新增/删除行）"},
                {"component": "VDataTable", "props": {
                    "headers": [
                        {"title": "类别", "key": "类别"}, {"title": "字段", "key": "字段"},
                        {"title": "正则", "key": "正则"}, {"title": "合法性", "key": "合法性"},
                    ],
                    "items": rule_rows, "density": "compact", "items-per-page": 20,
                }},
            ]}]},
            {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "div", "props": {"class": "text-h6"}, "text": "等待队列 / 替换观察窗"},
                {"component": "VDataTable", "props": {
                    "headers": [
                        {"title": "订阅", "key": "订阅"}, {"title": "季", "key": "季"},
                        {"title": "状态", "key": "状态"}, {"title": "标题", "key": "标题"},
                        {"title": "剩余(分)", "key": "剩余(分)"}, {"title": "复核", "key": "复核"},
                        {"title": "说明", "key": "说明"},
                    ],
                    "items": queue_rows, "density": "compact", "items-per-page": 20,
                }},
            ]}]},
            {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "text": f"最近报告：{report.get('summary') or '尚无运行记录'}"},
            ]}]},
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """注册插件动态 API（配置页按钮与手动运维入口）。"""
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即执行一次扫描"},
            {"path": "/test_rules", "endpoint": self.api_test_rules, "methods": ["POST"],
             "auth": "bear", "summary": "用内置真实站点标题测试正则与季集判定"},
            {"path": "/queue", "endpoint": self.api_queue, "methods": ["GET"],
             "auth": "bear", "summary": "查看等待队列"},
            {"path": "/queue/clear", "endpoint": self.api_queue_clear, "methods": ["POST"],
             "auth": "bear", "summary": "清空等待队列"},
            {"path": "/validate", "endpoint": self.api_validate, "methods": ["GET"],
             "auth": "bear", "summary": "校验当前配置"},
            {"path": "/report", "endpoint": self.api_report, "methods": ["GET"],
             "auth": "bear", "summary": "查看最近一次运行报告"},
        ]

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """统一日志输出。"""
        logger.info(f"【{self.plugin_name}】{message}")

    def _notify(self, flag: str, title: str, text: str) -> None:
        """按开关通过 MP 原生通知通道推送（微信 / Telegram 等由宿主路由）。"""
        if not self._cfg.get(flag, True):
            return
        try:
            from app.schemas.types import MessageType

            self.post_message(mtype=MessageType.Plugin, title=f"订阅自动保种 · {title}", text=text)
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】通知发送失败：{error}")

    def _save_state(self) -> None:
        """持久化队列与报告；失败只告警，不影响主流程。"""
        try:
            self.save_data(QUEUE_DATA_KEY, self._queue.to_list())
            self.save_data(REPORT_DATA_KEY, self._last_report)
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】状态保存失败：{error}")

    def _actions(self) -> dict[str, Any]:
        """组装分级清理动作开关。"""
        return {
            "delete_subscribe": bool(self._cfg.get("action_delete_subscribe")),
            "delete_torrent": bool(self._cfg.get("action_delete_torrent")),
            "delete_torrent_files": bool(self._cfg.get("action_delete_torrent_files")),
            "delete_local_files": bool(self._cfg.get("action_delete_local_files")),
            "delete_media_files": bool(self._cfg.get("action_delete_media_files")),
            "delete_history": bool(self._cfg.get("action_delete_history")),
            "path_prefixes": _lines(self._cfg.get("delete_path_prefixes")),
        }

    @staticmethod
    def _id_set(value: Any) -> set[int]:
        """把订阅的「订阅站点」字段（列表 / 逗号串 / JSON 串）转成站点 ID 集合。"""
        if not value:
            return set()
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    import json

                    value = json.loads(stripped)
                except Exception:
                    value = stripped.strip("[]").split(",")
            else:
                value = stripped.split(",")
        if not isinstance(value, (list, tuple, set)):
            return set()
        result: set[int] = set()
        for item in value:
            try:
                result.add(int(item))
            except (TypeError, ValueError):
                continue
        return result

    def _effective_settings(
        self, subscribe: Any
    ) -> tuple[set[int], set[str], list[Any], list[Any], Optional[int]]:
        """
        合并插件配置与 MP 原生订阅自带设置（编辑订阅页的字段）。

        - 站点：插件勾选 ∩ 订阅站点；插件未勾选时跟随订阅站点；
        - 分辨率：合并订阅「分辨率」（例如 4k → 2160p）与插件勾选；
        - 排除规则：叠加订阅「排除」正则（收紧判定，安全）；
        - 包含规则：**不并入**订阅「包含」，否则会放宽命中条件，与「宁可漏判」冲突；
        - 返回订阅「总集数」，供可选的「全N集」一致性校验使用。
        """
        sites = {int(x) for x in (self._cfg.get("sites") or [])}
        resolutions = set(self._cfg.get("resolutions") or [])
        include = list(self._include_rules)
        exclude = list(self._exclude_rules)
        total_episode: Optional[int] = None
        if not self._cfg.get("follow_subscribe_settings"):
            return sites, resolutions, include, exclude, total_episode

        raw_sites = getattr(subscribe, "sites", None)
        if raw_sites is None:
            raw_sites = getattr(subscribe, "site_ids", None)
        sub_sites = self._id_set(raw_sites)
        if sub_sites:
            sites = (sites & sub_sites) if sites else sub_sites
        mapped = core.map_resolution(_text(getattr(subscribe, "resolution", "")))
        if mapped:
            resolutions.add(mapped)
        sub_exclude = _text(getattr(subscribe, "exclude", "")).strip()
        if sub_exclude:
            exclude.append(core.Rule(
                pattern=sub_exclude, field="any", raw=f"订阅排除::{sub_exclude}"
            ))
        try:
            total_episode = int(getattr(subscribe, "total_episode", None) or 0) or None
        except (TypeError, ValueError):
            total_episode = None
        return sites, resolutions, include, exclude, total_episode

    def _is_whitelisted(self, subscribe: Any) -> bool:
        """白名单支持订阅 ID、剧名（包含匹配）、媒体 ID 三种写法。"""
        whitelist = _lines(self._cfg.get("subscribe_whitelist"))
        if not whitelist:
            return False
        values = [
            _text(getattr(subscribe, "id", "")),
            _text(getattr(subscribe, "name", "")),
            _text(getattr(subscribe, "media_id", "")),
        ]
        for entry in whitelist:
            for value in values:
                if value and (entry == value or (len(entry) >= 2 and entry in value)):
                    return True
        return False

    def _list_native_subscribes(self) -> list[Any]:
        """只读取 MP 原生电视剧订阅；手动种子不在订阅表，天然排除。"""
        try:
            from app.db.oper.subscribe import SubscribeOper

            rows = SubscribeOper().list() or []
        except Exception as error:
            logger.error(f"【{self.plugin_name}】读取订阅失败：{error}")
            return []
        blacklist = set(_lines(self._cfg.get("subscribe_source_blacklist")))
        result = []
        for sub in rows:
            if _text(getattr(sub, "type", "")) not in ("电视剧", "TV"):
                continue
            username = _text(getattr(sub, "username", ""))
            if username and username in blacklist:
                continue
            result.append(sub)
        return result

    def _search_contexts(
        self, subscribe: Any, site_ids: Optional[set[int]] = None
    ) -> list[Any]:
        """在有效站点检索订阅关键字，返回宿主 Context 列表（只读，不改宿主流程）。"""
        keyword = _text(getattr(subscribe, "keyword", "")) or _text(getattr(subscribe, "name", ""))
        if not keyword:
            return []
        try:
            from app.chain.search import SearchChain
            from app.schemas.types import MediaType

            sites = sorted(site_ids) if site_ids else None
            contexts = SearchChain().search_by_title(
                title=keyword,
                sites=sites,
                mtype=MediaType.TV,
            )
            return list(contexts or [])
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】站点检索失败（{keyword}）：{error}")
            return []

    @staticmethod
    def _context_payload(context: Any) -> dict[str, Any]:
        """把宿主 Context 序列化，供等待期结束后重建并提交下载。"""
        try:
            return context.model_dump(mode="json")  # Pydantic v2
        except Exception:
            try:
                return context.dict()
            except Exception:
                return {}

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    def scan(self, manual: bool = False) -> dict[str, Any]:
        """
        Cron 主扫描：遍历原生电视剧订阅 → 站点检索 → 五元组判定 → 命中入队。

        测试模式下只打印日志与推送预警，不入队、不删除、不下载。
        """
        if not self._enabled:
            return {"ok": False, "reason": "插件未启用"}
        if self._config_errors:
            self._log("配置存在非法正则或 Cron，本轮扫描已跳过")
            return {"ok": False, "reason": "配置校验未通过", "errors": self._config_errors}
        if not self._lock.acquire(blocking=False):
            self._log("上一轮扫描尚未结束，跳过本次触发")
            return {"ok": False, "reason": "扫描正在进行中"}

        summary: dict[str, Any] = {
            "ok": True, "manual": manual, "subscribes": 0, "torrents": 0,
            "hits": 0, "queued": 0, "test_mode": bool(self._cfg.get("test_mode")),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            cooling_keys = {
                (t.subscribe_id, t.season)
                for t in self._queue.all() if t.status in {"waiting", "cooling"}
            }
            subscribes = self._list_native_subscribes()
            summary["subscribes"] = len(subscribes)
            self._log(
                f"开始扫描：原生电视剧订阅 {len(subscribes)} 条，"
                f"跟随订阅设置={bool(self._cfg.get('follow_subscribe_settings'))}，"
                f"集数一致性校验={bool(self._cfg.get('verify_episode_count'))}"
            )

            for sub in subscribes:
                sub_id = getattr(sub, "id", None)
                sub_name = _text(getattr(sub, "name", "")) or f"#{sub_id}"
                season = getattr(sub, "season", None)
                if sub_id is None:
                    continue
                if self._is_whitelisted(sub):
                    self._log(f"订阅 {sub_name} S{season or '?'} 命中白名单，永久跳过")
                    continue
                if (int(sub_id), season) in cooling_keys:
                    self._log(f"订阅 {sub_name} S{season or '?'} 处于等待队列/替换观察窗，跳过避免重复清理")
                    continue

                eff_sites, eff_res, eff_include, eff_exclude, total_episode = \
                    self._effective_settings(sub)
                self._log(
                    f"订阅 {sub_name} S{season or '?'} 有效设置："
                    f"站点={sorted(eff_sites) or '全部'} 分辨率={sorted(eff_res) or '不限'} "
                    f"总集数={total_episode or '未填'} 指定季={season or '?'} "
                    f"订阅包含={_text(getattr(sub, 'include', '')) or '无'} "
                    f"订阅排除={_text(getattr(sub, 'exclude', '')) or '无'} "
                    f"洗版={getattr(sub, 'best_version', None) or getattr(sub, 'wash', None) or '未知'}"
                )
                contexts = self._search_contexts(sub, eff_sites)
                self._log(f"订阅 {sub_name} S{season or '?'} 检索到 {len(contexts)} 条资源")
                for context in contexts:
                    summary["torrents"] += 1
                    torrent = getattr(context, "torrent_info", None)
                    if torrent is None:
                        continue
                    snapshot = core.build_snapshot_from_torrent(torrent, context)
                    verdict = core.evaluate(
                        subscribe_name=sub_name,
                        subscribe_season=int(season) if season is not None else None,
                        torrent=snapshot,
                        allowed_site_ids=eff_sites or None,
                        allowed_resolutions=eff_res or None,
                        allow_unknown_resolution=bool(self._cfg.get("allow_unknown_resolution")),
                        include_rules=eff_include,
                        exclude_rules=eff_exclude,
                        require_include_rule=bool(self._cfg.get("require_include_rule")),
                        require_completeness_evidence=bool(self._cfg.get("require_completeness_evidence")),
                        subscribe_total_episode=total_episode,
                        verify_episode_count=bool(self._cfg.get("verify_episode_count")),
                    )
                    self._log(
                        f"[{sub_name} S{season or '?'}] {snapshot.site_name or '未知站点'} | {snapshot.title[:70]} | "
                        f"副标题={snapshot.subtitle[:50]} | 识别整包={verdict.is_full_pack} | "
                        f"命中包含={verdict.rule_match.included_by or '无'} | "
                        f"命中排除={verdict.rule_match.excluded_by or '无'} | "
                        f"结论={'命中' if verdict.passed else '跳过'} ({verdict.stage}: {verdict.reason})"
                    )
                    for stage, ok, detail in verdict.checks:
                        self._log(f"    · {stage} {'通过' if ok else '未过'}：{detail}")
                    if not verdict.passed:
                        continue

                    summary["hits"] += 1
                    self._notify(
                        "notify_on_hit", "命中同季整季打包",
                        f"订阅：{sub_name} S{season or '?'}\n"
                        f"站点：{snapshot.site_name}\n标题：{snapshot.title}\n"
                        f"副标题：{snapshot.subtitle}\n分辨率：{','.join(verdict.resolutions) or '未标注'}\n"
                        f"命中包含正则：{', '.join(verdict.rule_match.included_by) or '无'}\n"
                        f"同季校验：S{verdict.seasons[0]:02d} == 订阅 S{season}",
                    )
                    if self._cfg.get("test_mode"):
                        self._log(f"【测试模式】{sub_name} 已命中，仅记录，不入队/不删除/不下载")
                        self._notify(
                            "notify_on_hit", "测试模式预警",
                            f"【测试模式】{sub_name} S{season or '?'} 命中整季打包，"
                            f"未进入等待队列、未删除任何内容。\n{snapshot.title}",
                        )
                        continue

                    evidence = self._evidence.build(sub)
                    evidence["replacement_context"] = self._context_payload(context)
                    delay = _int(self._cfg.get("delay_minutes"), 15, 1, 1440)
                    task = PendingTask(
                        task_id=make_task_id(int(sub_id), snapshot.site_id, snapshot.title),
                        subscribe_id=int(sub_id),
                        subscribe_name=sub_name,
                        season=int(season) if season is not None else None,
                        media_source=_text(getattr(sub, "media_source", "")),
                        media_id=_text(getattr(sub, "media_id", "")),
                        site_id=snapshot.site_id,
                        site_name=snapshot.site_name,
                        torrent_title=snapshot.title,
                        torrent_page_url=snapshot.page_url,
                        torrent_enclosure=snapshot.enclosure,
                        resolutions=verdict.resolutions,
                        matched_include=verdict.rule_match.included_by,
                        verdict_detail=verdict.as_dict(),
                        evidence=evidence,
                        created_ts=time.time(),
                        execute_ts=time.time() + delay * 60,
                        last_check_ts=time.time(),
                        status="waiting",
                    )
                    created = self._queue.upsert(task)
                    if created:
                        summary["queued"] += 1
                        self._notify(
                            "notify_on_queue", "进入删前等待队列",
                            f"订阅：{sub_name} S{season or '?'}\n"
                            f"整包种子：{snapshot.title}\n"
                            f"等待 {delay} 分钟后执行清理，期间持续复检种子是否仍可检索到。",
                        )
                    self._log(
                        f"{'已入队' if created else '刷新复核'}：{sub_name} S{season or '?'} → "
                        f"{task.task_id}，倒计时 {delay} 分钟"
                    )

            self._queue.purge_terminal()
            summary["summary"] = (
                f"扫描订阅 {summary['subscribes']} 条 / 资源 {summary['torrents']} 条 / "
                f"命中 {summary['hits']} 条 / 新入队 {summary['queued']} 条"
            )
            self._last_report = summary
            self._save_state()
            self._log(f"扫描完成：{summary['summary']}")
            return summary
        except Exception as error:
            logger.error(f"【{self.plugin_name}】扫描异常：{error}\n{traceback.format_exc()}")
            return {"ok": False, "reason": str(error)}
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # 等待队列复检与替换观察窗
    # ------------------------------------------------------------------

    def _torrent_still_searchable(self, task: PendingTask) -> bool:
        """复检删前等待任务：整季打包种子是否仍能在勾选站点检索到。"""
        try:
            from app.chain.search import SearchChain
            from app.schemas.types import MediaType

            sites = [int(x) for x in (self._cfg.get("sites") or [])]
            contexts = SearchChain().search_by_title(
                title=task.subscribe_name,
                sites=sites or None,
                mtype=MediaType.TV,
            ) or []
            for context in contexts:
                torrent = getattr(context, "torrent_info", None)
                if torrent and _text(getattr(torrent, "title", "")) == task.torrent_title:
                    return True
                if torrent and task.torrent_title and task.torrent_title in _text(getattr(torrent, "title", "")):
                    return True
            return False
        except Exception as error:
            self._log(f"复检 {task.task_id} 检索失败（保守视为仍存在）：{error}")
            return True

    def _execute_cleanup(self, task: PendingTask) -> None:
        """删前等待到期：重新读取订阅、收集证据链、执行分级清理、转入替换观察窗。"""
        try:
            from app.db.oper.subscribe import SubscribeOper

            subscribe = SubscribeOper().get(int(task.subscribe_id))
        except Exception as error:
            self._log(f"读取订阅 #{task.subscribe_id} 失败：{error}")
            subscribe = None
        if subscribe is None:
            self._queue.mark(task.task_id, "cancelled", "订阅已不存在，取消清理")
            self._notify("notify_on_cancel", "任务取消",
                         f"{task.subscribe_name}：订阅在等待期内已被删除，取消本次清理。")
            return

        evidence = self._evidence.build(subscribe)
        # 保留扫描时保存的宿主 Context，供后续自动下载新全集使用
        old_context = (task.evidence or {}).get("replacement_context")
        if old_context:
            evidence["replacement_context"] = old_context

        result = self._cleaner.run(evidence, self._actions(), test_mode=False)
        self._log(
            f"清理结果 {task.subscribe_name}：成功={result['ok']} "
            f"步骤={len(result['steps'])} 错误={result['errors'] or '无'}"
        )
        self._notify(
            "notify_on_execute", "等待完成 · 已执行清理",
            f"订阅：{task.subscribe_name} S{task.season or '?'}\n"
            f"结果：{'全部成功' if result['ok'] else '存在失败，请查看日志'}\n"
            + "\n".join(f"· {s['step']}：{s['detail']}" for s in result["steps"])
            + (f"\n失败：{'; '.join(result['errors'])}" if result["errors"] else ""),
        )

        # 进入替换观察窗：旧资源已清空，此时下载整季全集 MP 才会正常刮削
        window = _int(self._cfg.get("replacement_window_minutes"), 15, 1, 1440)
        delay = _int(self._cfg.get("replacement_delay_minutes"), 10, 0, 1440)
        task.evidence = evidence
        task.status = "cooling"
        task.execute_ts = time.time() + window * 60
        task.created_ts = time.time()
        task.check_count = 0
        task.ready_confirmed = False
        task.last_reason = "旧资源已清空，等待下载整季全集以便 MP 正常刮削"
        task.verdict_detail = {
            **(task.verdict_detail or {}),
            "download_new_at": time.time() + delay * 60,
            "cleanup": {"ok": result["ok"], "errors": result["errors"]},
        }
        self._log(
            f"进入替换观察窗：{task.subscribe_name} 共 {window} 分钟，"
            f"{delay} 分钟后{'自动下载新全集' if self._cfg.get('auto_download_replacement') else '推送下载链接'}"
        )
        self._notify(
            "notify_on_replacement", "旧资源已清空 · 可下载整季全集",
            f"订阅：{task.subscribe_name} S{task.season or '?'}\n"
            f"站点：{task.site_name}\n标题：{task.torrent_title}\n"
            f"页面：{task.torrent_page_url or '无'}\n"
            f"下载：{task.torrent_enclosure or '无'}\n"
            f"媒体库目标路径已清空，此时下载整季全集，MP 才会执行转移与刮削。",
        )

    def _download_replacement(self, task: PendingTask) -> None:
        """把命中的整季打包 Context 交给宿主下载链（不自己拼 HTTP/Cookie）。"""
        payload = (task.evidence or {}).get("replacement_context")
        if not payload:
            self._log(f"{task.subscribe_name} 无可用检索上下文，跳过自动下载，请手动下载")
            return
        try:
            from app.chain.download import DownloadChain
            from app.schemas.context import Context

            context = Context.model_validate(payload)
            DownloadChain().download_single(
                context=context,
                source="plugin",
                username=self.__class__.__name__,
            )
            task.ready_confirmed = True
            task.last_reason = "已提交整季全集下载，交由 MP 完成转移刮削"
            self._log(f"已提交整季全集下载：{task.torrent_title}")
            self._notify(
                "notify_on_replacement", "已下载整季全集",
                f"{task.subscribe_name} S{task.season or '?'}：{task.torrent_title}\n"
                "旧资源已清空，MP 将在下载完成后正常转移与刮削。",
            )
        except Exception as error:
            self._log(f"自动下载整季全集失败（请手动下载）：{error}")
            self._notify(
                "notify_on_replacement", "自动下载失败，请手动下载",
                f"{task.subscribe_name}：{error}\n标题：{task.torrent_title}\n"
                f"页面：{task.torrent_page_url or '无'}",
            )

    def watch_queue(self) -> dict[str, Any]:
        """每分钟由宿主调度：复检等待任务、执行清理、推进替换观察窗。"""
        if not self._enabled or not self._lock.acquire(blocking=False):
            return {"ok": False, "reason": "未启用或扫描进行中"}
        try:
            threshold = _int(self._cfg.get("recheck_missing_threshold"), 2, 1, 20)
            now = time.time()
            stats = {"checked": 0, "executed": 0, "cancelled": 0, "downloaded": 0, "closed": 0}
            for task in list(self._queue.all()):
                if task.status == "waiting":
                    stats["checked"] += 1
                    exists = self._torrent_still_searchable(task)
                    task.last_check_ts = now
                    task.check_count += 1
                    if exists:
                        task.consecutive_missing = 0
                    else:
                        task.consecutive_missing += 1
                    self._log(
                        f"等待复核 {task.task_id}（{task.subscribe_name}）："
                        f"整包种子{'仍在' if exists else '已检索不到'}，"
                        f"剩余 {round(task.remaining_seconds / 60, 1)} 分钟，"
                        f"连续缺失 {task.consecutive_missing}/{threshold}"
                    )
                    if task.consecutive_missing >= threshold:
                        self._queue.mark(task.task_id, "cancelled", "整季打包种子在等待期内消失/检索不到")
                        stats["cancelled"] += 1
                        self._notify(
                            "notify_on_cancel", "任务取消",
                            f"{task.subscribe_name} S{task.season or '?'}：整季打包种子在 "
                            f"{_int(self._cfg.get('delay_minutes'), 15, 1, 1440)} 分钟等待期内消失，"
                            "已取消清理，未删除任何内容。",
                        )
                        continue
                    if task.execute_ts <= now:
                        self._queue.mark(task.task_id, "executing", "等待完成，执行清理")
                        self._execute_cleanup(task)
                        stats["executed"] += 1

                elif task.status == "cooling":
                    download_at = (task.verdict_detail or {}).get("download_new_at", 0)
                    if (
                        self._cfg.get("auto_download_replacement")
                        and not task.ready_confirmed
                        and download_at
                        and now >= float(download_at)
                    ):
                        self._download_replacement(task)
                        stats["downloaded"] += 1
                    if task.execute_ts <= now:
                        self._queue.mark(task.task_id, "done", "替换观察窗结束")
                        stats["closed"] += 1
                        self._log(
                            f"替换观察窗结束：{task.subscribe_name}，"
                            f"新全集{'已提交下载' if task.ready_confirmed else '未自动下载（需人工处理）'}"
                        )

            self._queue.purge_terminal()
            self._save_state()
            return {"ok": True, **stats}
        except Exception as error:
            logger.error(f"【{self.plugin_name}】队列复检异常：{error}\n{traceback.format_exc()}")
            return {"ok": False, "reason": str(error)}
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # API 实现
    # ------------------------------------------------------------------

    def api_run(self) -> dict[str, Any]:
        """API：立即执行一次扫描（仍受测试模式约束）。"""
        return self.scan(manual=True)

    def api_test_rules(self) -> dict[str, Any]:
        """API：用内置真实 PT 站标题逐条测试正则与季集判定，并推送报告。"""
        failures = []
        rows = []
        for title, subtitle, season, expected in core.SAMPLE_CASES:
            torrent = core.TorrentSnapshot(
                title=title, subtitle=subtitle, site_id=1, site_name="内置样例",
                release_group=core.extract_release_group(title),
            )
            verdict = core.evaluate(
                subscribe_name="内置样例", subscribe_season=season, torrent=torrent,
                include_rules=self._include_rules, exclude_rules=self._exclude_rules,
                require_include_rule=bool(self._cfg.get("require_include_rule")),
            )
            rows.append({
                "title": title[:60], "subtitle": subtitle[:40],
                "expected": expected, "passed": verdict.passed,
                "stage": verdict.stage, "reason": verdict.reason,
                "include": verdict.rule_match.included_by,
            })
            if verdict.passed != expected:
                failures.append(f"{title[:50]} 期望={expected} 实际={verdict.passed} ({verdict.stage})")
        errors = core.validate_rules(self._include_rules + self._exclude_rules)
        report = {
            "ok": not failures and not errors,
            "total": len(rows), "failures": failures, "regex_errors": errors,
            "rows": rows, "summary": f"测试 {len(rows)} 条真实站点标题，失败 {len(failures)} 条",
            "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._last_report = {"summary": report["summary"], **report}
        self._save_state()
        self._notify(
            "notify_on_hit", "正则与季集判定测试报告",
            f"{report['summary']}\n非法正则：{len(errors)} 条\n"
            + ("\n".join(failures[:8]) if failures else "全部通过：整季打包与单集/分段包/跨季均判定正确。"),
        )
        return report

    def api_queue(self) -> dict[str, Any]:
        """API：查看等待队列与替换观察窗。"""
        return {
            "total": len(self._queue.all()),
            "waiting": [t.to_dict() for t in self._queue.waiting()],
            "cooling": [t.to_dict() for t in self._queue.all() if t.status == "cooling"],
            "recent": [t.to_dict() for t in self._queue.all() if t.status in {"done", "cancelled"}][-10:],
        }

    def api_queue_clear(self) -> dict[str, Any]:
        """API：清空队列（不会回滚已执行的清理）。"""
        count = self._queue.clear()
        self._save_state()
        self._log(f"等待队列已清空，移除 {count} 条任务")
        return {"ok": True, "cleared": count}

    def api_validate(self) -> dict[str, Any]:
        """API：校验当前配置中的正则与 Cron。"""
        errors = core.validate_rules(self._include_rules + self._exclude_rules)
        cron_ok, cron_msg = core.validate_cron(_text(self._cfg.get("cron")))
        return {
            "ok": not errors and cron_ok,
            "regex_errors": errors, "cron_ok": cron_ok, "cron_message": cron_msg,
            "include_rules": len(self._include_rules), "exclude_rules": len(self._exclude_rules),
            "test_mode": bool(self._cfg.get("test_mode")),
            "enabled": self._enabled,
        }

    def api_report(self) -> dict[str, Any]:
        """API：查看最近一次运行/测试报告。"""
        return self._last_report or {"summary": "尚无运行记录"}
