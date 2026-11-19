"""
订阅自动保种助手（MoviePilot V3 插件）。

一句话目标：用 MP 官方原生订阅追剧时，一旦勾选站点出现「同剧、同季、目标
分辨率」的整季打包种子，就清理该订阅产生的一切（订阅任务、下载器种子及
种子文件、转移记录、媒体库文件），清空媒体库目标路径后再下载整季全集，
让 MP 正常完成转移与刮削 —— 便于长期保种，并保证整季视频规格一致。

配置页只保留 7 项：启用 / Cron / 站点 / 分辨率 / 测试模式 /
包含规则 / 排除规则。其余行为（等待时长、删除范围、通知、先删后下、
多候选择优）全部内置固化，不暴露开关。

包含 / 排除规则对齐 MP 原生订阅：每行一条，普通文字按关键字匹配，
也支持正则；排除优先，包含最后判定。用户规则只能收紧内置安全闸门，
不能放宽。同一订阅同一轮若筛出多份整季全集，只保留做种数最高的一份，
避免重复清理并下载两份全集。

严格遵循 V3 合同：
    - 主类继承 ``app.sdk.plugin._PluginBase``，定义在目录的 __init__.py；
    - 使用稳定 SDK（app.sdk.logging）与公开入口（app.db.oper.* / app.chain.* /
      app.schemas.types / app.schemas.context）；
    - 定时任务由 ``get_service()`` 交给宿主调度器，插件不自建调度线程；
    - 配置走 update_config / get_config，运行状态走 save_data / get_data；
    - 插件被动定时扫描，不接管、不修改 MP 原生订阅搜索与下载流程。

判定链见 core.py，证据链与清理见 cleaner.py，等待队列见 pending_queue.py。
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
except ImportError:  # 兜底：纯本地静态检查
    import logging

    logger = logging.getLogger("SubscribeAutoKeep")


PLUGIN_ID = "SubscribeAutoKeep"

# ---------------------------------------------------------------------------
# 固定行为（内置，不暴露到配置页）
# ---------------------------------------------------------------------------
#: 命中后先等待，期间每分钟复检整包种子是否仍可检索到
WAIT_MINUTES = 15
#: 连续几次检索不到整包种子则取消任务
MISSING_THRESHOLD = 2
#: 清理完成后延迟多久下载整季全集（先删旧，MP 才会重新转移刮削）
REPLACEMENT_DELAY_MINUTES = 10
#: 替换观察窗时长，期间冷却该订阅，防止重复清理
REPLACEMENT_WINDOW_MINUTES = 15

#: 默认配置。测试模式默认开启，避免装好就误删。
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "test_mode": True,
    "cron": "0 4 * * *",
    "sites": [],
    "resolution": "any",
    "include_rules": "",
    "exclude_rules": "",
}

#: 最近一次扫描报告
REPORT_DATA_KEY = "subscribeautokeep_last_report"

#: 规则说明（配置页 hint / README 保持同一套措辞）
RULE_HINT = (
    "每行一条，新增=加一行，删除=删掉该行；# 开头为注释。"
    "普通文字按关键字匹配，也支持正则（忽略大小写）。匹配范围：标题 + 副标题 + 站点标签。"
)


def _text(value: Any) -> str:
    """把枚举/数字/None 安全转成字符串。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


class SubscribeAutoKeep(_PluginBase):
    """订阅自动保种助手主类。"""

    plugin_name = "订阅自动保种助手"
    plugin_desc = "发现同季同分辨率的整季全集时，清理旧订阅及其种子/文件/记录，再下载整包以便保种与刮削"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "3.2.1"
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
        self._global_include_rules: list[core.Rule] = []
        self._global_exclude_rules: list[core.Rule] = []
        self._queue = PendingQueue()
        self._lock = threading.Lock()
        self._last_report: dict[str, Any] = {}
        self._evidence = EvidenceBuilder(self._log)
        self._cleaner = CleanupExecutor(self._log)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[dict[str, Any]] = None) -> None:
        """读取配置、校验 Cron 与规则合法性、恢复队列；允许重复调用。"""
        merged = dict(DEFAULTS)
        merged.update(config or {})
        self._cfg = merged
        self._enabled = bool(merged.get("enabled"))

        self._config_errors = []
        cron_ok, cron_msg = core.validate_cron(_text(merged.get("cron")))
        if not cron_ok:
            self._config_errors.append(f"Cron 表达式非法：{cron_msg}")

        # 编译全局规则。任何一行非法都暂停扫描：宁可不干活，也不能因为
        # 「写错了被忽略」导致按更宽松的规则删除订阅与文件。
        include_rules, include_errors = core.parse_rules(
            _text(merged.get("include_rules")), "include"
        )
        exclude_rules, exclude_errors = core.parse_rules(
            _text(merged.get("exclude_rules")), "exclude"
        )
        self._global_include_rules = include_rules if not include_errors else []
        self._global_exclude_rules = exclude_rules if not exclude_errors else []
        self._config_errors.extend(include_errors + exclude_errors)

        for error in self._config_errors:
            logger.error(f"【{self.plugin_name}】配置校验失败：{error}")
        if self._config_errors:
            self._notify(
                "配置有误，已暂停扫描",
                "请修正后重新保存配置：\n" + "\n".join(self._config_errors),
            )

        self._queue = PendingQueue.from_list(self.get_data(QUEUE_DATA_KEY))
        self._last_report = self.get_data(REPORT_DATA_KEY) or {}

        logger.info(
            f"【{self.plugin_name}】已加载：启用={self._enabled} "
            f"测试模式={merged.get('test_mode')} Cron={merged.get('cron')} "
            f"站点={len(merged.get('sites') or [])} 分辨率={merged.get('resolution')} "
            f"包含规则={len(self._global_include_rules)} 排除规则={len(self._global_exclude_rules)} "
            f"配置错误={len(self._config_errors)} 等待任务={len(self._queue.waiting())}"
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """注册 Cron 扫描与每分钟队列复检，全部交给宿主调度器。"""
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
                "name": "订阅自动保种 · 定时检索整季全集",
                "trigger": CronTrigger.from_crontab(_text(self._cfg.get("cron")) or "0 4 * * *"),
                "func": self.scan,
                "kwargs": {},
            })
        except Exception as error:
            logger.error(f"【{self.plugin_name}】Cron 表达式无法注册：{error}")
        services.append({
            "id": f"{plugin_id}.Watch",
            "name": "订阅自动保种 · 等待复检与先删后下",
            "trigger": CronTrigger.from_crontab("* * * * *"),
            "func": self.watch_queue,
            "kwargs": {},
        })
        return services

    def stop_service(self) -> None:
        """保存队列并释放状态；后台任务由宿主调度器统一清理。"""
        self._enabled = False
        try:
            self.save_data(QUEUE_DATA_KEY, self._queue.to_list())
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】保存等待队列失败：{error}")
        logger.info(f"【{self.plugin_name}】已停止")

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------

    def _site_options(self) -> list[dict[str, Any]]:
        """从宿主站点表生成多选选项。"""
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
        """分辨率四档，与 MP 原生订阅的下拉一致。"""
        return [{"title": label, "value": key} for key, label in core.RESOLUTION_CHOICES]

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
        # 配置页由 FormRender.vue 渲染：它只识别 props 里以 on 开头的「字符串函数」，
        # 不识别顶层 events（events 只被详情页 PageRender.vue 处理）。因此按钮必须
        # 写成 props.onclick + window.MoviePilotAPI.post，与官方 V3 插件保持一致。
        def button(text: str, path: str) -> dict[str, Any]:
            onclick = (
                "function(e) { "
                f"window.MoviePilotAPI.post('plugin/{PLUGIN_ID}/{path}', {{}})"
                ".then(function(r) { "
                "var d = (r && r.data) ? r.data : r; "
                "alert(d && d.message ? d.message : '操作已完成，请查看插件日志'); "
                "})"
                ".catch(function(err) { "
                "console.error(err); "
                "alert('调用失败，请查看浏览器控制台与插件日志'); "
                "}) }"
            )
            return {
                "component": "VBtn",
                "props": {
                    "color": "primary",
                    "variant": "flat",
                    "size": "small",
                    "class": "mr-2 mb-2",
                    "onclick": onclick,
                },
                "text": text,
            }

        form = [
            {
                "component": "VForm",
                "content": [
                    row((12, [alert(
                        "命中后本插件会删除订阅、种子及种子文件、转移记录、媒体库文件，不可回滚。"
                        "请务必先用【测试模式】跑一轮，核对日志里的证据链，再关闭测试模式。",
                        "warning",
                    )])),
                    row(
                        (6, [field("enabled", "VSwitch", label="启用插件")]),
                        (6, [field("test_mode", "VSwitch", label="测试模式（只记日志，不删除）")]),
                    ),
                    row(
                        (6, [field("cron", "VTextField", label="Cron 定时表达式",
                                    placeholder="0 4 * * *",
                                    **{"persistent-hint": True,
                                       "hint": "5 段：分 时 日 月 周，例如每天 04:00 为 0 4 * * *"})]),
                        (6, [field("resolution", "VSelect", label="分辨率",
                                    items=self._resolution_options(),
                                    **{"item-title": "title", "item-value": "value",
                                       "persistent-hint": True,
                                       "hint": "选一档可保证新旧资源规格一致；「全部」= 不限制"})]),
                    ),
                    row((12, [field("sites", "VSelect", label="监控站点（多选，留空 = 全部站点）",
                                    items=self._site_options(), multiple=True, chips=True,
                                    **{"closable-chips": True, "item-title": "title",
                                       "item-value": "value", "persistent-hint": True,
                                       "hint": "只检索勾选站点的整季全集"})])),
                    row(
                        (6, [field("include_rules", "VTextarea",
                                    label="包含规则（关键字、正则式）",
                                    rows=4, clearable=True,
                                    placeholder="HDR|HDR10|DV|杜比视界",
                                    **{"persistent-hint": True,
                                       "hint": "命中任意一条才处理；留空 = 不限制。" + RULE_HINT
                                               + " 插件规则优先于订阅自带规则。"})]),
                        (6, [field("exclude_rules", "VTextarea",
                                    label="排除规则（关键字、正则式）",
                                    rows=4, clearable=True,
                                    placeholder="HDR|HDR10|DV|杜比视界",
                                    **{"persistent-hint": True,
                                       "hint": "命中任意一条直接跳过，且优先于包含规则。" + RULE_HINT
                                               + " 会与该订阅自带的排除规则叠加。"})]),
                    ),
                    row((12, [alert(
                        "同一站点出现两个 2160p 全集时：只要不带 HDR 的那份 → 【排除】填 "
                        "HDR|HDR10|DV|杜比视界；只要带 HDR 的那份 → 【包含】填 HDR|HDR10|DV。"
                        "规则会自动叠加该订阅自带的包含/排除规则；保存时校验正则，非法会暂停扫描并推送通知。"
                        "若筛完仍有多份，按做种数 → 体积自动选一份，不会重复清理或下载两份。",
                        "info",
                    )])),
                    row((12, [{"component": "VCol", "props": {"cols": 12},
                               "content": [button("立即扫描一次", "run"),
                                           button("校验规则", "validate")]}])),
                    row((12, [alert(
                        "固定流程：命中同季整季全集 → 等待 15 分钟并持续复检种子是否还在（消失则取消）"
                        " → 删除订阅/种子及种子文件/转移记录/媒体库文件 → 10 分钟后自动下载整季全集。"
                        "先删旧再下新，MP 才会重新转移刮削。",
                        "info",
                    )])),
                ],
            },
        ]
        return form, dict(DEFAULTS)

    def get_page(self) -> list[dict[str, Any]]:
        """不提供额外详情页，运行结论直接看插件日志与 MP 通知。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """暴露「立即扫描」与「校验规则」两个接口。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["POST"],
                "summary": "立即扫描一次（仍受测试模式约束）",
                # V3 动态路由按字符串判断：仅 "bear" 走登录 token 校验，
                # 其它值（含布尔 True）都会落到 apikey 校验，导致配置页按钮 401。
                "auth": "bear",
            },
            {
                "path": "/validate",
                "endpoint": self.api_validate,
                "methods": ["POST"],
                "summary": "校验 Cron 与包含/排除规则，并回显上轮被规则筛掉的真实标题",
                # V3 动态路由按字符串判断：仅 "bear" 走登录 token 校验，
                # 其它值（含布尔 True）都会落到 apikey 校验，导致配置页按钮 401。
                "auth": "bear",
            },
        ]

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        logger.info(f"【{self.plugin_name}】{message}")

    def _notify(self, title: str, text: str) -> None:
        """通过 MP 原生通知通道推送（微信 / Telegram 等由宿主路由）。"""
        try:
            from app.schemas.types import MessageType

            self.post_message(mtype=MessageType.Plugin, title=f"订阅自动保种 · {title}", text=text)
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】通知发送失败：{error}")

    def _save_state(self) -> None:
        try:
            self.save_data(QUEUE_DATA_KEY, self._queue.to_list())
            self.save_data(REPORT_DATA_KEY, self._last_report)
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】状态保存失败：{error}")

    def _site_ids(self) -> set[int]:
        result: set[int] = set()
        for item in (self._cfg.get("sites") or []):
            try:
                result.add(int(item))
            except (TypeError, ValueError):
                continue
        return result

    def _rules_for(self, subscribe: Any) -> tuple[list[core.Rule], list[core.Rule], str]:
        """
        合并插件全局规则与该订阅自带的 ``include`` / ``exclude``。

        - 排除：两者取并集，任一命中即跳过（只收紧，不放宽）；
        - 包含：插件全局规则优先（用户手动指定），未配置时才跟随订阅自带规则；
        - 订阅自带规则非法时只记日志并忽略，不影响其它订阅。

        :return: (包含规则, 排除规则, 可读的来源说明)
        """
        sub_include, include_errors = core.parse_rules(
            _text(getattr(subscribe, "include", "")), "subscribe.include"
        )
        sub_exclude, exclude_errors = core.parse_rules(
            _text(getattr(subscribe, "exclude", "")), "subscribe.exclude"
        )
        errors = include_errors + exclude_errors
        if errors:
            self._log(
                f"订阅 {_text(getattr(subscribe, 'name', ''))} 自带规则非法，已忽略："
                + "；".join(errors)
            )

        include = list(self._global_include_rules) if self._global_include_rules else sub_include
        exclude = list(self._global_exclude_rules) + sub_exclude
        include_source = (
            "插件全局（优先）" if self._global_include_rules
            else ("订阅自带" if sub_include else "未配置")
        )
        note = (
            f"包含 {len(include)} 条（{include_source}），"
            f"排除 {len(exclude)} 条"
            f"（全局 {len(self._global_exclude_rules)} + 订阅 {len(sub_exclude)}）"
        )
        return include, exclude, note

    def _list_native_subscribes(self) -> list[Any]:
        """只读取 MP 原生电视剧订阅；手动添加的种子不在订阅表，天然排除。"""
        try:
            from app.db.oper.subscribe import SubscribeOper

            rows = SubscribeOper().list() or []
        except Exception as error:
            logger.error(f"【{self.plugin_name}】读取订阅失败：{error}")
            return []
        return [s for s in rows if _text(getattr(s, "type", "")) in ("电视剧", "TV")]

    def _search_contexts(self, subscribe: Any) -> list[Any]:
        """在勾选站点检索订阅关键字，返回宿主 Context 列表（只读）。"""
        keyword = _text(getattr(subscribe, "keyword", "")) or _text(getattr(subscribe, "name", ""))
        if not keyword:
            return []
        try:
            from app.chain.search import SearchChain
            from app.schemas.types import MediaType

            sites = sorted(self._site_ids()) or None
            contexts = SearchChain().search_by_title(
                title=keyword, sites=sites, mtype=MediaType.TV,
            )
            return list(contexts or [])
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】站点检索失败（{keyword}）：{error}")
            return []

    @staticmethod
    def _context_payload(context: Any) -> dict[str, Any]:
        """把宿主 Context 序列化，供清理后重建并提交下载。"""
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
        """Cron 主扫描：遍历原生订阅 → 站点检索 → 规则+整季全集判定 → 候选收敛 → 入队。"""
        if not self._enabled:
            # 手动按钮也会走到这里；必须留日志与可读回执，否则用户会以为按钮没反应。
            self._log("【立即扫描】被拒绝：插件未启用。请打开「启用插件」并保存后再点按钮。")
            return {
                "ok": False,
                "reason": "插件未启用",
                "message": "插件未启用：请先打开「启用插件」并保存配置，再点「立即扫描一次」。",
            }
        if self._config_errors:
            self._log("Cron 或包含/排除规则非法，本轮扫描已跳过")
            return {
                "ok": False, "reason": "配置校验未通过", "errors": self._config_errors,
                "message": "配置校验未通过：" + "；".join(self._config_errors),
            }
        if not self._lock.acquire(blocking=False):
            self._log("上一轮扫描尚未结束，跳过本次触发")
            return {
                "ok": False, "reason": "扫描正在进行中",
                "message": "上一轮扫描尚未结束，请稍后再试。",
            }

        test_mode = bool(self._cfg.get("test_mode"))
        resolution = _text(self._cfg.get("resolution")) or "any"
        site_ids = self._site_ids()
        summary: dict[str, Any] = {
            "ok": True, "manual": manual, "subscribes": 0, "torrents": 0,
            "hits": 0, "queued": 0, "test_mode": test_mode,
            "resolution": resolution,
            "rule_excluded": 0, "include_rejected": 0, "multi_candidate": 0,
            "excluded_samples": [], "picked": [],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            busy = {
                (t.subscribe_id, t.season)
                for t in self._queue.all() if t.status in {"waiting", "cooling"}
            }
            subscribes = self._list_native_subscribes()
            summary["subscribes"] = len(subscribes)
            self._log(
                f"开始扫描：原生电视剧订阅 {len(subscribes)} 条 | "
                f"站点={sorted(site_ids) or '全部'} | 分辨率={resolution} | 测试模式={test_mode} | "
                f"全局包含 {len(self._global_include_rules)} 条 / 排除 {len(self._global_exclude_rules)} 条"
            )

            for sub in subscribes:
                sub_id = getattr(sub, "id", None)
                sub_name = _text(getattr(sub, "name", "")) or f"#{sub_id}"
                season = getattr(sub, "season", None)
                if sub_id is None:
                    continue
                if (int(sub_id), season) in busy:
                    self._log(f"订阅 {sub_name} S{season or '?'} 已在等待/替换流程中，跳过")
                    continue

                include_rules, exclude_rules, rule_note = self._rules_for(sub)
                self._log(f"订阅 {sub_name} S{season or '?'} 生效规则：{rule_note}")
                contexts = self._search_contexts(sub)
                self._log(f"订阅 {sub_name} S{season or '?'} 检索到 {len(contexts)} 条资源")

                # 先收集该订阅的全部合格候选，再收敛为唯一一份。若同一订阅
                # 有多份整季全集都直接入队，会重复清理并下载两份全集。
                candidates: list[tuple[Any, Any, Any, tuple[int, float, str, str]]] = []
                for context in contexts:
                    summary["torrents"] += 1
                    torrent = getattr(context, "torrent_info", None)
                    if torrent is None:
                        continue
                    snapshot = core.build_snapshot_from_torrent(torrent, context)
                    verdict = core.evaluate(
                        subscribe_season=int(season) if season is not None else None,
                        torrent=snapshot,
                        allowed_site_ids=site_ids or None,
                        resolution=resolution,
                        exclude_rules=exclude_rules,
                        include_rules=include_rules,
                    )
                    self._log(
                        f"[{sub_name} S{season or '?'}] {snapshot.site_name or '未知站点'} | "
                        f"{snapshot.title[:70]} | 副标题={snapshot.subtitle[:50]} | "
                        f"分辨率={','.join(verdict.resolutions) or '未标注'} | "
                        f"整季全集={verdict.is_full_pack} | "
                        f"结论={'命中' if verdict.passed else '跳过'} ({verdict.stage}: {verdict.reason})"
                    )
                    if verdict.stage == "排除规则":
                        summary["rule_excluded"] += 1
                        if len(summary["excluded_samples"]) < 8:
                            summary["excluded_samples"].append({
                                "title": snapshot.title[:120],
                                "rule": verdict.matched_exclude,
                            })
                    elif verdict.stage == "包含规则":
                        summary["include_rejected"] += 1
                    for stage, ok, detail in verdict.checks:
                        self._log(f"    · {stage} {'通过' if ok else '未过'}：{detail}")
                    if not verdict.passed:
                        continue

                    try:
                        seeders = int(getattr(torrent, "seeders", 0) or 0)
                    except (TypeError, ValueError):
                        seeders = 0
                    try:
                        size = float(getattr(torrent, "size", 0) or 0)
                    except (TypeError, ValueError):
                        size = 0.0
                    candidates.append((
                        context, snapshot, verdict,
                        (seeders, size, _text(getattr(torrent, "pubdate", "")), snapshot.title),
                    ))

                if not candidates:
                    continue

                # 候选收敛：做种数 → 体积 → 发布时间 → 标题，保证跨轮次稳定
                summary["hits"] += 1
                if len(candidates) > 1:
                    summary["multi_candidate"] += 1
                chosen = core.best_index([item[3] for item in candidates])
                context, snapshot, verdict, _ = candidates[chosen]
                others = [c[1].title for i, c in enumerate(candidates) if i != chosen]
                self._log(
                    f"候选收敛 {sub_name} S{season or '?'}：{len(candidates)} 份符合 → "
                    f"选用 [{snapshot.site_name}] {snapshot.title[:80]}，"
                    f"其余 {len(others)} 份略过"
                    + ("（如需精确指定请填包含/排除规则）" if others else "")
                )
                for title in others[:5]:
                    self._log(f"    · 略过候选：{title[:100]}")
                summary["picked"].append({
                    "subscribe": sub_name,
                    "season": season,
                    "title": snapshot.title[:120],
                    "site": snapshot.site_name,
                    "include": verdict.matched_include,
                    "candidates": len(candidates),
                })
                if others:
                    self._notify(
                        "检测到多份整季全集",
                        f"{sub_name} S{season or '?'} 有 {len(candidates)} 份符合，"
                        f"已按做种数/体积选用：\n{snapshot.title}\n"
                        "若不是你要的那份，请在包含/排除规则里指定（例如 HDR）。",
                    )

                evidence = self._evidence.build(sub)
                if test_mode:
                    # 测试模式也打印一遍「本来会删什么」，方便核对证据链
                    self._cleaner.run(evidence, test_mode=True)
                    self._log(f"【测试模式】{sub_name} 已命中，仅记录，不入队 / 不删除 / 不下载")
                    self._notify(
                        "测试模式命中",
                        f"{sub_name} S{season or '?'} 发现同季整季全集，未删除任何内容。\n"
                        f"站点：{snapshot.site_name}\n标题：{snapshot.title}\n"
                        f"命中包含规则：{verdict.matched_include or '未配置'}\n"
                        f"分辨率：{','.join(verdict.resolutions) or '未标注'}\n"
                        f"证据链：种子 {len(evidence['hashes'])} 个 / "
                        f"下载侧文件 {len(evidence['download_paths'])} 个 / "
                        f"媒体库文件 {len(evidence['media_paths'])} 个",
                    )
                    continue

                evidence["replacement_context"] = self._context_payload(context)
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
                    verdict_detail=verdict.as_dict(),
                    evidence=evidence,
                    created_ts=time.time(),
                    execute_ts=time.time() + WAIT_MINUTES * 60,
                    last_check_ts=time.time(),
                    status="waiting",
                )
                created = self._queue.upsert(task)
                if created:
                    summary["queued"] += 1
                    self._notify(
                        "进入等待队列",
                        f"订阅：{sub_name} S{season or '?'}\n整季全集：{snapshot.title}\n"
                        f"命中包含规则：{verdict.matched_include or '未配置'}\n"
                        f"等待 {WAIT_MINUTES} 分钟后执行清理，期间持续复检种子是否还在。",
                    )
                self._log(
                    f"{'已入队' if created else '刷新复核'}：{sub_name} S{season or '?'} → "
                    f"{task.task_id}，倒计时 {WAIT_MINUTES} 分钟"
                )

            self._queue.purge_terminal()
            summary["summary"] = (
                f"扫描订阅 {summary['subscribes']} 条 / 资源 {summary['torrents']} 条 / "
                f"命中订阅 {summary['hits']} 条 / 新入队 {summary['queued']} 条 / "
                f"排除规则筛掉 {summary['rule_excluded']} 条 / "
                f"包含规则拒绝 {summary['include_rejected']} 条 / "
                f"多候选订阅 {summary['multi_candidate']} 条"
            )
            summary["message"] = summary["summary"] + (
                "（测试模式：仅记录，不删除、不下载）" if test_mode else ""
            )
            self._last_report = summary
            self._save_state()
            self._log(f"扫描完成：{summary['summary']}")
            return summary
        except Exception as error:
            logger.error(f"【{self.plugin_name}】扫描异常：{error}\n{traceback.format_exc()}")
            return {"ok": False, "reason": str(error), "message": f"扫描异常：{error}"}
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # 等待复检 → 先删旧 → 后下新
    # ------------------------------------------------------------------

    def _torrent_still_searchable(self, task: PendingTask) -> bool:
        """复检选中的那一份整季全集是否仍能在勾选站点检索到。"""
        # 扫描用订阅的 keyword 检索，复检必须用同一个关键字，否则可能
        # 因为订阅 name ≠ keyword 而误判为「种子消失」，白白取消清理。
        keyword = task.subscribe_name
        try:
            from app.db.oper.subscribe import SubscribeOper

            subscribe = SubscribeOper().get(int(task.subscribe_id))
            if subscribe is not None:
                keyword = _text(getattr(subscribe, "keyword", "")) or keyword
        except Exception as error:
            self._log(f"复检读取订阅关键字失败，回退订阅名称：{error}")
        try:
            from app.chain.search import SearchChain
            from app.schemas.types import MediaType

            sites = sorted(self._site_ids()) or None
            contexts = SearchChain().search_by_title(
                title=keyword, sites=sites, mtype=MediaType.TV,
            ) or []
            expected = (task.torrent_title or "").strip()
            for context in contexts:
                torrent = getattr(context, "torrent_info", None)
                title = _text(getattr(torrent, "title", "")).strip() if torrent else ""
                # 站点偶尔会给标题追加 [热门] 等标签，双向包含更稳
                if title and expected and (
                    title == expected or expected in title or title in expected
                ):
                    return True
            return False
        except Exception as error:
            self._log(f"复检 {task.task_id} 检索失败（保守视为仍存在）：{error}")
            return True

    def _execute_cleanup(self, task: PendingTask) -> None:
        """等待到期：重新读取订阅、收集证据链、执行全套清理、转入先删后下流程。"""
        try:
            from app.db.oper.subscribe import SubscribeOper

            subscribe = SubscribeOper().get(int(task.subscribe_id))
        except Exception as error:
            self._log(f"读取订阅 #{task.subscribe_id} 失败：{error}")
            subscribe = None
        if subscribe is None:
            self._queue.mark(task.task_id, "cancelled", "订阅已不存在，取消清理")
            self._notify("任务取消", f"{task.subscribe_name}：订阅在等待期内已被删除，取消本次清理。")
            return

        evidence = self._evidence.build(subscribe)
        old_context = (task.evidence or {}).get("replacement_context")
        if old_context:
            evidence["replacement_context"] = old_context

        result = self._cleaner.run(evidence, test_mode=False)
        self._log(
            f"清理结果 {task.subscribe_name}：成功={result['ok']} "
            f"步骤={len(result['steps'])} 错误={result['errors'] or '无'}"
        )
        self._notify(
            "已执行清理",
            f"订阅：{task.subscribe_name} S{task.season or '?'}\n"
            f"结果：{'全部成功' if result['ok'] else '存在失败，请查看日志'}\n"
            + "\n".join(f"· {s['step']}：{s['detail']}" for s in result["steps"])
            + (f"\n失败：{'; '.join(result['errors'])}" if result["errors"] else ""),
        )

        # 旧资源已清空，此时下载整季全集 MP 才会正常转移刮削
        task.evidence = evidence
        task.status = "cooling"
        task.execute_ts = time.time() + REPLACEMENT_WINDOW_MINUTES * 60
        task.created_ts = time.time()
        task.check_count = 0
        task.ready_confirmed = False
        task.last_reason = "旧资源已清空，等待下载整季全集以便 MP 正常刮削"
        task.verdict_detail = {
            **(task.verdict_detail or {}),
            "download_new_at": time.time() + REPLACEMENT_DELAY_MINUTES * 60,
            "cleanup": {"ok": result["ok"], "errors": result["errors"]},
        }
        self._log(
            f"进入替换观察窗：{task.subscribe_name} 共 {REPLACEMENT_WINDOW_MINUTES} 分钟，"
            f"{REPLACEMENT_DELAY_MINUTES} 分钟后自动下载整季全集"
        )

    def _download_replacement(self, task: PendingTask) -> None:
        """把命中的整季全集 Context 交给宿主下载链（不自己拼 HTTP / Cookie）。"""
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
                "已下载整季全集",
                f"{task.subscribe_name} S{task.season or '?'}：{task.torrent_title}\n"
                "旧资源已清空，MP 将在下载完成后正常转移与刮削。",
            )
        except Exception as error:
            self._log(f"自动下载整季全集失败（请手动下载）：{error}")
            self._notify(
                "自动下载失败，请手动下载",
                f"{task.subscribe_name}：{error}\n标题：{task.torrent_title}\n"
                f"页面：{task.torrent_page_url or '无'}",
            )

    def watch_queue(self) -> dict[str, Any]:
        """每分钟由宿主调度：复检等待任务、执行清理、推进先删后下。"""
        if not self._enabled or not self._lock.acquire(blocking=False):
            return {"ok": False, "reason": "未启用或扫描进行中"}
        try:
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
                        f"整季全集{'仍在' if exists else '已检索不到'}，"
                        f"剩余 {round(task.remaining_seconds / 60, 1)} 分钟，"
                        f"连续缺失 {task.consecutive_missing}/{MISSING_THRESHOLD}"
                    )
                    if task.consecutive_missing >= MISSING_THRESHOLD:
                        self._queue.mark(task.task_id, "cancelled", "整季全集种子在等待期内消失")
                        stats["cancelled"] += 1
                        self._notify(
                            "任务取消",
                            f"{task.subscribe_name} S{task.season or '?'}：整季全集种子在 "
                            f"{WAIT_MINUTES} 分钟等待期内消失，已取消清理，未删除任何内容。",
                        )
                        continue
                    if task.execute_ts <= now:
                        self._queue.mark(task.task_id, "executing", "等待完成，执行清理")
                        self._execute_cleanup(task)
                        stats["executed"] += 1

                elif task.status == "cooling":
                    download_at = (task.verdict_detail or {}).get("download_new_at", 0)
                    if (
                        not task.ready_confirmed
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
    # API
    # ------------------------------------------------------------------

    def api_run(self, kwargs: Any = None) -> dict[str, Any]:
        """API：立即执行一次扫描（仍受测试模式约束）。

        kwargs 是 V3 插件 API 的通用占位参数：宿主/前端可能附带，声明后可安全忽略。
        """
        self._log("【立即扫描一次】按钮已触发，启用=%s 测试模式=%s" % (
            self._enabled, self._cfg.get("test_mode"),
        ))
        return self.scan(manual=True)

    def api_validate(self, kwargs: Any = None) -> dict[str, Any]:
        """API：校验 Cron 与规则，并回显上一轮真实标题的筛选结果。"""
        self._log("【校验规则】按钮已触发，开始校验 Cron 与包含/排除规则")
        cron_value = _text(self._cfg.get("cron"))
        cron_ok, cron_msg = core.validate_cron(cron_value)
        include_rules, include_errors = core.parse_rules(
            _text(self._cfg.get("include_rules")), "include"
        )
        exclude_rules, exclude_errors = core.parse_rules(
            _text(self._cfg.get("exclude_rules")), "exclude"
        )
        errors = ([] if cron_ok else [f"Cron 表达式非法：{cron_msg}"]) \
            + include_errors + exclude_errors
        self._log(
            f"【校验规则】Cron={'合法' if cron_ok else '非法'} | "
            f"包含 {len(include_rules)} 条 / 排除 {len(exclude_rules)} 条 | "
            f"错误 {len(errors)} 项" + ("：" + "；".join(errors) if errors else "")
        )
        report = self._last_report or {}
        return {
            "ok": not errors,
            "message": (
                f"校验通过：Cron 合法，包含 {len(include_rules)} 条 / 排除 {len(exclude_rules)} 条。"
                if not errors else "校验失败：" + "；".join(errors)
            ),
            "cron": {"value": cron_value, "valid": cron_ok, "detail": cron_msg},
            "include_rules": {"count": len(include_rules), "items": [r.raw for r in include_rules]},
            "exclude_rules": {"count": len(exclude_rules), "items": [r.raw for r in exclude_rules]},
            "errors": errors,
            "last_scan": {
                "at": report.get("started_at"),
                "rule_excluded": report.get("rule_excluded", 0),
                "include_rejected": report.get("include_rejected", 0),
                "multi_candidate": report.get("multi_candidate", 0),
                "excluded_samples": report.get("excluded_samples", []),
                "picked": report.get("picked", []),
            },
            "hint": (
                "校验通过后请保持测试模式跑一次「立即扫描一次」，"
                "用 last_scan.excluded_samples / picked 核对规则是否筛对了资源。"
            ),
        }
