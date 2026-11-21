"""电费日报（MoviePilot V3 插件）。

定时登录国家电网 95598 / 网上国网，读取每日用电量、账户余额以及各阶梯电量
剩余情况，整理后用 MoviePilot 的通知能力发送，不在插件里另建推送渠道。

凭据处理：配置页输入明文密码 -> 立即转换成 95598 登录所需的 MD5 摘要 ->
用宿主 API_TOKEN 派生的密钥加密后写入配置，明文随即从配置中清除。
会话令牌只保存在插件数据区，任何日志与通知都不会输出密码或令牌原文。
"""
from __future__ import annotations

import datetime
import logging
import threading
from typing import Any

try:  # 宿主环境
    from app.sdk.logging import logger
except Exception:  # 本地测试
    logger = logging.getLogger("ElectricityDaily")

try:
    from app.sdk.plugin import _PluginBase
except Exception:  # 本地测试用最小基类

    class _PluginBase:  # type: ignore[no-redef]
        def update_config(self, config: dict, plugin_id: str | None = None) -> bool:
            return True

        def get_config(self, plugin_id: str | None = None) -> Any:
            return None

        def save_data(self, key: str, value: Any, plugin_id: str | None = None) -> None:
            return None

        def get_data(self, key: str | None = None, plugin_id: str | None = None) -> Any:
            return None

        def post_message(self, **kwargs: Any) -> None:
            return None


try:
    from apscheduler.triggers.cron import CronTrigger
except Exception:
    CronTrigger = None  # type: ignore[assignment]

try:
    from app.schemas.types import MessageType

    _PLUGIN_MTYPE = getattr(MessageType, "Plugin", None)
except Exception:
    _PLUGIN_MTYPE = None

from .client import (
    ApiError,
    ConfigError,
    DataError,
    ElectricityError,
    LoginError,
    StateGridClient,
)
from .message import build_combined_text, build_error_text, build_title
from .models import Report, build_report
from . import secure

DEFAULT_CRON = "0 8 * * *"
SERVICE_ID = "ElectricityDaily.DailyReport"


class ElectricityDaily(_PluginBase):
    """国家电网电费日报插件入口。"""

    plugin_name = "电费日报"
    plugin_desc = "每日自动登录网上国网，推送用电量、账户余额与阶梯电量剩余。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.6"
    plugin_author = "hmily"
    author_url = "https://github.com/hmily91/MoviePilot-Plugins"
    plugin_config_prefix = "electricitydaily_"
    plugin_order = 50
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._cron = DEFAULT_CRON
        self._username = ""
        self._alt_account = ""
        self._cons_no = ""
        self._notify_on_error = True
        self._digest = ""
        self._lock = threading.Lock()
        self._last_status = "尚未运行"
        self._last_report: dict[str, Any] = {}
        self._cron_error = ""

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置；若用户新填了密码，先加密摘要再清掉明文。"""
        config = dict(config or {})
        raw_password = str(config.get("password") or "")
        if raw_password:
            config["password_digest_enc"] = secure.seal(secure.password_digest(raw_password))
            config["password"] = ""
            self.update_config(config)
            logger.info("电费日报：密码摘要已更新，明文未写入配置")

        self._enabled = bool(config.get("enabled"))
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip() or DEFAULT_CRON
        self._username = str(config.get("username") or "").strip()
        self._alt_account = str(config.get("alt_account") or "").strip()
        self._cons_no = str(config.get("cons_no") or "").strip()
        self._notify_on_error = bool(config.get("notify_on_error", True))
        self._digest = secure.read_password_digest(config)
        self._cron_error = ""
        if self._cron:
            self._validate_cron(self._cron)

        cached = self.get_data("last_status")
        if isinstance(cached, dict):
            self._last_status = str(cached.get("status") or "尚未运行")
            self._last_report = cached.get("report") if isinstance(cached.get("report"), dict) else {}

        if config.get("onlyonce"):
            config["onlyonce"] = False
            self.update_config(config)
            try:
                from app.sdk import scheduler as scheduler_sdk

                adder = getattr(scheduler_sdk, "add_plugin_once_job", None)
                if adder:
                    adder(
                        self.__class__.__name__,
                        "run_once",
                        self.refresh,
                        "电费日报立即运行一次",
                        delay_seconds=3,
                    )
            except Exception as exc:
                logger.warning("电费日报：一次性任务注册失败：%s", type(exc).__name__)

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """释放一次性任务资源；周期任务由宿主调度器自行管理。"""
        try:
            from app.sdk import scheduler as scheduler_sdk

            remover = getattr(scheduler_sdk, "remove_plugin_once_job", None)
            if remover:
                remover(self.__class__.__name__, "run_once")
        except Exception:
            pass
        self._enabled = False

    @staticmethod
    def get_command() -> list[dict]:
        return []

    def get_api(self) -> list[dict]:
        return []

    def get_service(self) -> list[dict]:
        if not self.get_state() or not self._digest or not self._username:
            return []
        if CronTrigger is None:
            logger.warning("电费日报：当前宿主缺少 apscheduler，无法注册定时任务")
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as exc:
            logger.error("电费日报：Cron 表达式无效（%s），定时任务未注册", type(exc).__name__)
            return []
        return [{
            "id": SERVICE_ID,
            "name": "电费日报每日推送",
            "trigger": trigger,
            "func": self.refresh,
            "kwargs": {},
        }]

    # ── 页面 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _col(*components: dict, cols: int = 12, md: int | None = None) -> dict:
        """把若干控件包进 VCol，避免 VForm 子控件因缺少栅格容器而重叠。"""
        props: dict[str, Any] = {"cols": cols}
        if md is not None:
            props["md"] = md
        return {"component": "VCol", "props": props, "content": list(components)}

    @staticmethod
    def _row(*columns: dict) -> dict:
        return {"component": "VRow", "content": list(columns)}

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        notice = {
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": "密码只用于生成登录摘要，保存后明文立即从配置中清除。"
                        "登录凭据请用 MoviePilot 已配置的通知渠道接收，插件不另建推送通道。",
            },
        }
        enabled = {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
        notify_on_error = {
            "component": "VSwitch",
            "props": {"model": "notify_on_error", "label": "登录或接口失败时推送错误说明"},
        }
        username = {
            "component": "VTextField",
            "props": {
                "model": "username",
                "label": "国网登录账号（手机号）",
                "hint": "与网上国网 App 登录手机号一致",
            },
        }
        password = {
            "component": "VTextField",
            "props": {
                "model": "password",
                "label": "国网登录密码",
                "type": "password",
                "hint": "仅在新填或改密时需要；保存后清空，不会再显示明文",
                "clearable": True,
            },
        }
        alt_account = {
            "component": "VTextField",
            "props": {
                "model": "alt_account",
                "label": "备用登录标识（选填）",
                "hint": "登记在国网的邮箱；主标识被限流时自动尝试一次",
                "clearable": True,
            },
        }
        cons_no = {
            "component": "VTextField",
            "props": {
                "model": "cons_no",
                "label": "只推送指定户号（选填）",
                "hint": "留空则推送账号下全部居民户号",
                "clearable": True,
            },
        }
        cron = {
            "component": "VTextField",
            "props": {
                "model": "cron",
                "label": "定时表达式（5 段 Cron）",
                "hint": "默认 0 8 * * * 表示每天 08:00",
            },
        }
        onlyonce = {
            "component": "VSwitch",
            "props": {"model": "onlyonce", "label": "保存后立即运行一次"},
        }
        return (
            [{
                "component": "VForm",
                "content": [
                    self._row(self._col(notice)),
                    self._row(self._col(enabled, md=6), self._col(notify_on_error, md=6)),
                    self._row(self._col(username, md=6), self._col(password, md=6)),
                    self._row(self._col(alt_account, md=6), self._col(cons_no, md=6)),
                    self._row(self._col(cron)),
                    self._row(self._col(onlyonce)),
                ],
            }],
            {
                "enabled": False,
                "username": "",
                "password": "",
                "alt_account": "",
                "cons_no": "",
                "cron": DEFAULT_CRON,
                "notify_on_error": True,
                "onlyonce": False,
            },
        )

    def get_page(self) -> list[dict]:
        """数据/诊断页：展示最近一次执行结果，便于不翻系统日志就能定位。"""
        config_ready = "是" if (self._username and self._digest) else "否（缺少账号或密码摘要）"
        enabled = "是" if self._enabled else "否"
        digest_len = len(self._digest)
        diag = (
            f"插件状态：{'已启用' if self._enabled else '未启用'}\n"
            f"登录账号：{'已填写' if self._username else '未填写'}\n"
            f"密码摘要：{'已保存（长度 %d）' % digest_len if digest_len == 32 else '未保存（请在配置里重新输入密码并保存）'}\n"
            f"定时任务：{'已注册' if self._cron and not self._cron_error else '未注册' + ('（' + self._cron_error + '）' if self._cron_error else '')}\n"
            f"Cron：{self._cron}\n"
            f"最近执行：{self._last_status}\n"
            f"说明：若“最近执行”仍是尚未运行，请打开配置里的“保存后立即运行一次”，或等待下一个 Cron 时间点。"
        )
        preview = self._last_report.get("text") or "暂无抓取记录"
        preview = preview[:1200]
        return [
            {"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": f"配置检查：启用={enabled}，凭据就绪={config_ready}",
            }},
            {"component": "VPre", "content": diag},
            {"component": "VPre", "content": preview},
        ]

    # ── 任务 ────────────────────────────────────────────────────────────────
    def _validate_cron(self, expression: str) -> None:
        if CronTrigger is None:
            return
        try:
            CronTrigger.from_crontab(expression)
        except Exception as exc:
            self._cron_error = f"Cron 表达式无效：{type(exc).__name__}"

    def _send(self, title: str, text: str) -> None:
        try:
            kwargs: dict[str, Any] = {"title": title, "text": text}
            if _PLUGIN_MTYPE is not None:
                kwargs["mtype"] = _PLUGIN_MTYPE
            self.post_message(**kwargs)
            logger.info("电费日报：通知已提交给 MoviePilot（标题 %s）", title)
        except Exception as exc:
            logger.error("电费日报：调用 MoviePilot 推送失败：%s", type(exc).__name__)

    def _remember(self, status: str, reports: list[Report] | None = None) -> None:
        self._last_status = status
        snapshot: dict[str, Any] = {"status": status}
        if reports:
            report = reports[0]
            snapshot["report"] = {
                "cons_no": report.cons_no,
                "text": build_combined_text(reports),
                "fetched_at": report.fetched_at,
            }
        try:
            self.save_data("last_status", snapshot)
        except Exception as exc:
            logger.debug("电费日报：缓存状态失败：%s", type(exc).__name__)

    def refresh(self) -> None:
        """定时入口：登录 -> 取数 -> 解析 -> 推送。任何异常都在这里收敛。"""
        if not self._lock.acquire(blocking=False):
            logger.debug("电费日报：上一轮尚未结束，跳过本次触发")
            return
        client: StateGridClient | None = None
        try:
            if not self._username or not self._digest:
                raise ConfigError("缺少账号或密码摘要，请在插件配置中重新输入账号密码")
            if self._cron_error:
                raise ConfigError(self._cron_error)

            client = StateGridClient(
                account=self._username,
                password_digest=self._digest,
                data_getter=self.get_data,
                data_setter=self.save_data,
                logger=logger,
                alt_account=self._alt_account,
            )
            client.login()

            doors = client.fetch_doors()
            if self._cons_no:
                doors = [d for d in doors if str(d.get("consNo_dst") or d.get("consNo") or "") == self._cons_no]
            if not doors:
                raise DataError("没有可查询的户号（或户号过滤条件无匹配）")
            logger.info("电费日报：开始抓取 %d 个户号", len(doors))

            today = datetime.date.today()
            start = (today - datetime.timedelta(days=40)).strftime("%Y%m%d")
            end = today.strftime("%Y%m%d")
            year_month = f"{today.year}-{today.month:02d}"
            reports: list[Report] = []

            for door in doors:
                cons_no = str(door.get("consNo_dst") or door.get("consNo") or "")
                parts: dict[str, dict[str, Any]] = {}
                for name, fetch in (
                    ("daily", lambda d=door: client.fetch_daily(d, start, end, today.year)),
                    ("balance", lambda d=door: client.fetch_balance(d)),
                    ("ladder", lambda d=door: client.fetch_ladder(d, year_month)),
                ):
                    try:
                        parts[name] = fetch()
                    except (ApiError, LoginError) as exc:
                        parts[name] = {}
                        logger.warning(
                            "电费日报：户号 %s 的 %s 取数失败（%s），该项按缺数处理",
                            cons_no[-4:] if cons_no else "未知", name,
                            exc.reason if isinstance(exc, LoginError) else type(exc).__name__,
                        )
                reports.append(build_report(
                    cons_no=cons_no,
                    daily_plain=parts.get("daily"),
                    balance_plain=parts.get("balance"),
                    ladder_plain=parts.get("ladder"),
                    today=today,
                ))

            if not reports:
                raise DataError("未生成任何报告")
            title = build_title(reports)
            text = build_combined_text(reports)
            self._send(title, text)
            self._remember(f"成功：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}，已推送 {len(reports)} 个户号", reports)
            logger.info("电费日报：本轮完成，%d 个户号，缺失字段统计 %s",
                        len(reports), [r.missing for r in reports])

        except ConfigError as exc:
            self._handle_failure(exc, "配置错误")
        except LoginError as exc:
            self._handle_failure(exc, "登录失败")
        except ApiError as exc:
            self._handle_failure(exc, "接口异常")
        except DataError as exc:
            self._handle_failure(exc, "数据缺失")
        except Exception as exc:  # 兜底：定时任务不能被未预期异常打断
            logger.exception("电费日报：未预期异常：%s", type(exc).__name__)
            self._handle_failure(exc, "未预期异常", notify=False)
        finally:
            if client is not None:
                client.close()
            self._lock.release()

    def _handle_failure(self, error: BaseException, category: str, notify: bool | None = None) -> None:
        if isinstance(error, LoginError):
            logger.warning("电费日报：%s，原因=%s", category, error.reason)
            detail = f"{category}（{error.reason}）"
        else:
            logger.warning("电费日报：%s：%s", category, str(error))
            detail = f"{category}（{str(error)[:120]}）"
        self._remember(f"{detail}：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
        should_notify = self._notify_on_error if notify is None else notify
        if should_notify:
            self._send("电费日报 | 执行失败", build_error_text(self.plugin_name, error))
