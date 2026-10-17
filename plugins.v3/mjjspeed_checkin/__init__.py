# -*- coding: utf-8 -*-
"""
MJJSpeed 站点自动签到插件（MoviePilot V3）

能力概览：
    1. 多站点管理：内置 7 个默认站点，支持在配置中手动增删站点域名（所有站点页面结构一致，仅域名不同）
    2. 账号密码登录：无需手工抓取 Cookie，输入邮箱+密码自动完成表单登录，登录态按站点独立保存
    3. 自动签到：注册系统级定时任务，默认 Cron「0 1 * * *」（每日 01:00）自动签到，时间支持任意 Cron 表达式；
       支持命令/API 手动立即签到
    4. 结果通知：签到完成后通过 MoviePilot 系统通知推送成功/失败结果
    5. 异常处理：网络超时、登录失败、今日已签到、站点不可用等场景分别捕获并记录日志
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class mjjspeed_checkin(_PluginBase):
    """
    MJJSpeed 系列站点自动签到插件。

    注意：MoviePilot 以「插件类名」作为插件 ID，因此类名固定为 mjjspeed_checkin，
    插件目录名同为 mjjspeed_checkin，二者必须保持一致。
    """

    # 插件名称
    plugin_name = "MJJSpeed站点自动签到"
    # 插件描述
    plugin_desc = "支持MJJSpeed系列站点账号密码自动登录+每日自动签到，支持自定义域名，无需手动获取Cookie。"
    # 插件图标（仓库中的图标文件名）
    plugin_icon = "mjjspeed-v2.png"
    # 插件版本，需与 package.v3.json 中的 version 保持一致
    plugin_version = "1.2.1"
    # 插件分类
    plugin_label = "工具"
    # 插件作者
    plugin_author = "hmily"
    # 作者主页
    author_url = "https://github.com/hmily91/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "mjjspeed_checkin_"
    # 加载顺序
    plugin_order = 100
    # 可使用的用户级别
    auth_level = 1

    # ---------------- 常量区 ----------------
    # 内置默认站点列表（每行一个，自动补全 https://）
    DEFAULT_SITES: str = (
        "3.mjjspeed.com\n"
        "cn.mjjspeed.com\n"
        "2.mjjspeed.icu\n"
        "1.mjjspeed.top\n"
        "1.mjjclub.top\n"
        "1.mjjcloud.top\n"
        "mjjspeed.us"
    )
    # 默认登录账号（默认留空，避免泄露，请在配置中填写）
    DEFAULT_EMAIL: str = ""
    # 默认登录密码（默认留空，避免泄露，请在配置中填写）
    DEFAULT_PASSWORD: str = ""
    # 默认签到时间（Cron 表达式：分 时 日 月 周，0 1 * * * 即每日 01:00）
    DEFAULT_CRON: str = "0 1 * * *"
    # 兼容旧配置的 HH:MM 默认值
    DEFAULT_CHECKIN_TIME: str = "01:00"
    # 默认登录接口路径
    DEFAULT_LOGIN_PATH: str = "/auth/login"
    # 默认签到接口路径
    DEFAULT_CHECKIN_PATH: str = "/user/checkin"
    # 所有请求统一超时时间（秒）
    REQUEST_TIMEOUT: int = 10
    # 请求UA
    USER_AGENT: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    # 「今日已签到」常见关键字
    ALREADY_KEYWORDS: tuple = ("已经签到", "已签到", "已打卡", "重复签到", "already", "signed", "checked")
    # 「需要重新登录」常见关键字
    RELoginIN_KEYWORDS: tuple = ("登录", "登陆", "login", "unauthorized", "未授权", "请先")

    # ---------------- 运行态属性 ----------------
    _enabled: bool = False
    _notify: bool = True
    _sites: List[str] = []
    _email: str = ""
    _password: str = ""
    _cron: str = DEFAULT_CRON
    _login_path: str = DEFAULT_LOGIN_PATH
    _checkin_path: str = DEFAULT_CHECKIN_PATH
    _retry: int = 3
    # 每个站点独立的登录态与签到记录：{站点: {cookies, last_checkin, last_result, last_success}}
    _states: Dict[str, dict] = {}

    # ---------------- 生命周期 ----------------
    def init_plugin(self, config: dict = None) -> None:
        """
        生效配置信息。

        :param config: 插件配置字典，由系统通过 init_plugin 注入（即 get_config 读到的配置）
        """
        # 先停止已有服务，避免重复注册定时任务
        self.stop_service()
        # 重置运行态
        self._enabled = False
        self._notify = True
        self._sites = []
        self._email = ""
        self._password = ""
        self._cron = self.DEFAULT_CRON
        self._login_path = self.DEFAULT_LOGIN_PATH
        self._checkin_path = self.DEFAULT_CHECKIN_PATH
        self._retry = 3
        self._states = {}
        # 未传入配置时回退到系统原生配置（get_config）
        conf = config or self.get_config() or {}
        # 首次安装没有配置时，把默认值写回系统配置（update_config），保证前端表单有初始值
        if not conf:
            conf = self.__default_config()
            self.update_config(conf)
        # 逐项读取配置
        self._enabled = bool(conf.get("enabled"))
        self._notify = bool(conf.get("notify"))
        self._sites = self.__parse_sites(conf.get("sites"))
        self._email = str(conf.get("email") or self.DEFAULT_EMAIL).strip()
        self._password = str(conf.get("password") or self.DEFAULT_PASSWORD)
        # 签到时间优先取 Cron 表达式，无则兼容旧版 HH:MM 配置
        self._cron = self.__parse_cron(conf.get("cron") or conf.get("checkin_time"))
        self._login_path = self.__parse_path(conf.get("login_path"), self.DEFAULT_LOGIN_PATH)
        self._checkin_path = self.__parse_path(conf.get("checkin_path"), self.DEFAULT_CHECKIN_PATH)
        self._retry = self.__parse_retry(conf.get("retry"))
        # 读取每个站点持久化的登录态与签到记录
        self._states = self.get_data("states") or {}
        if self._enabled:
            logger.info(f"[MJJSpeed] 插件已启用，站点数：{len(self._sites)}，签到 Cron：{self._cron}，"
                        f"失败重试 {self._retry} 次")
        else:
            logger.info("[MJJSpeed] 插件未启用")

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册插件远程命令，用于手动立即签到。"""
        return [
            {
                "cmd": "/mjjspeed_checkin",
                "event": EventType.PluginAction,
                "desc": "MJJSpeed 立即签到",
                "category": "站点",
                "data": {"action": "mjjspeed_checkin"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API，便于外部/前端触发一次立即签到。"""
        return [
            {
                "path": "/checkin",
                "endpoint": self.checkin_now,
                "methods": ["GET", "POST"],
                "summary": "立即签到",
                "description": "立即对所有已配置站点执行一次签到，并返回每个站点的签到结果",
                "auth": "apikey"
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单（Vuetify JSON，前端自动渲染）与默认配置数据。"""
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
                                        "props": {"model": "enabled", "label": "启用插件"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "结果通知"}
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "email", "label": "登录账号", "placeholder": "登录邮箱"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "登录密码",
                                            "type": "password",
                                            "placeholder": "登录密码"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "sites",
                                            "label": "站点列表",
                                            "rows": 8,
                                            "placeholder": "每行一个站点地址，例如：3.mjjspeed.com",
                                            "hint": "每行一个站点地址，留空协议时自动补全 https://"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "签到时间（Cron）",
                                            "placeholder": "0 1 * * *",
                                            "hint": "Cron 表达式（分 时 日 月 周），如 0 1 * * * = 每天 01:00；"
                                                    "0 9 * * 1 = 每周一 09:00；*/30 * * * * = 每 30 分钟"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry",
                                            "label": "失败重试次数",
                                            "type": "number",
                                            "hint": "签到失败后的重试次数，1-10"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "login_path", "label": "登录接口路径"}
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "checkin_path",
                                            "label": "签到接口路径",
                                            "hint": "站点接口路径有差异时可在此调整"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], self.__default_config()

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面，展示各站点最近一次签到情况。"""
        if not self._enabled:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "text": "插件未启用，请先在配置中启用插件。"}
                }
            ]
        if not self._sites:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "warning", "text": "尚未配置任何站点，请在配置中填写站点列表。"}
                }
            ]
        elements: List[dict] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "text": f"共 {len(self._sites)} 个站点，签到 Cron：{self._cron}，"
                            f"失败重试 {self._retry} 次；发送 /mjjspeed_checkin 命令可立即签到。"
                }
            }
        ]
        for site in self._sites:
            state = self._states.get(site) or {}
            last_time = state.get("last_checkin") or "从未签到"
            last_result = state.get("last_result") or "暂无记录"
            if state.get("last_success"):
                alert_type = "success"
            elif state.get("last_result"):
                alert_type = "error"
            else:
                alert_type = "info"
            elements.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": alert_type,
                        "text": f"{site}｜最近签到：{last_time}｜结果：{last_result}"
                    }
                }
            )
        return elements

    def get_service(self) -> List[Dict[str, Any]]:
        """注册系统级定时任务（每日自动签到）。"""
        if not self._enabled:
            return []
        trigger = self.__build_trigger(self._cron)
        if trigger is None:
            logger.error(f"[MJJSpeed] Cron 表达式无效：{self._cron}，定时任务未注册")
            return []
        return [
            {
                "id": "mjjspeed_checkin",
                "name": f"MJJSpeed 自动签到（{self._cron}）",
                "trigger": trigger,
                "func": self.checkin_all,
                "kwargs": {}
            }
        ]

    def stop_service(self) -> None:
        """停止插件服务并释放资源（定时任务由系统调度器统一回收）。"""
        self._states = {}
        logger.info("[MJJSpeed] 插件服务已停止")

    # ---------------- 事件与命令 ----------------
    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event) -> None:
        """响应插件远程命令，执行立即签到。"""
        event_data = event.event_data or {}
        if event_data.get("action") != "mjjspeed_checkin":
            return
        logger.info("[MJJSpeed] 收到立即签到命令")
        self.checkin_now()

    def checkin_now(self) -> Dict[str, Any]:
        """立即对所有站点执行一次签到，并返回结果。"""
        results = self.checkin_all()
        success_count = len([item for item in results if item.get("success")])
        return {
            "success": success_count > 0,
            "total": len(results),
            "success_count": success_count,
            "results": results
        }

    # ---------------- 核心业务 ----------------
    def checkin_all(self) -> List[Dict[str, Any]]:
        """
        对所有已配置站点执行签到并推送结果通知。

        :return: 每个站点的签到结果列表
        """
        if not self._enabled:
            logger.warning("[MJJSpeed] 插件未启用，跳过本次签到")
            return []
        if not self._sites:
            logger.warning("[MJJSpeed] 未配置任何站点，跳过本次签到")
            return []
        if not self._email or not self._password:
            logger.error("[MJJSpeed] 未配置登录账号或密码，无法签到")
            self.__notify("【MJJSpeed 每日签到】", "未配置登录账号或密码，无法签到")
            return []
        results: List[Dict[str, Any]] = []
        for site in self._sites:
            try:
                success, message = self.__checkin_site(site)
            except Exception as err:
                success, message = False, f"签到异常：{err}"
                logger.error(f"[MJJSpeed] {site} 签到异常：{err}")
            results.append({"site": site, "success": success, "message": message})
        self.__send_notification(results)
        return results

    def __checkin_site(self, site: str) -> Tuple[bool, str]:
        """
        对单个站点执行「登录 + 签到」，失败按配置次数重试。

        :param site: 站点地址
        :return: (是否成功, 提示信息)
        """
        state = self._states.get(site) or {}
        last_error = "未知错误"
        for attempt in range(1, self._retry + 1):
            # 无登录态时先登录
            cookies = state.get("cookies") or {}
            if not cookies:
                cookies, login_message = self.__login(site)
                if not cookies:
                    last_error = f"登录失败：{login_message}"
                    logger.warning(f"[MJJSpeed] {site} 第 {attempt}/{self._retry} 次登录失败：{login_message}")
                    if attempt < self._retry:
                        time.sleep(2)
                    continue
                state["cookies"] = cookies
                state["login_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._states[site] = state
                self.__save_states()
                logger.info(f"[MJJSpeed] {site} 登录成功：{login_message}")
            # 携带登录态发起签到
            success, message, need_relogin = self.__do_checkin(site, cookies)
            if success:
                state["last_checkin"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state["last_result"] = message
                state["last_success"] = True
                self._states[site] = state
                self.__save_states()
                logger.info(f"[MJJSpeed] {site} 签到成功：{message}")
                return True, message
            last_error = message
            logger.warning(f"[MJJSpeed] {site} 第 {attempt}/{self._retry} 次签到失败：{message}")
            if need_relogin:
                # 登录态失效，清空后下次循环自动重新登录
                state.pop("cookies", None)
                self._states[site] = state
                self.__save_states()
            if attempt < self._retry:
                time.sleep(2)
        # 全部重试失败，记录失败结果
        state["last_checkin"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_result"] = last_error
        state["last_success"] = False
        self._states[site] = state
        self.__save_states()
        logger.error(f"[MJJSpeed] {site} 签到最终失败：{last_error}")
        return False, last_error

    def __login(self, site: str) -> Tuple[Optional[dict], str]:
        """
        使用账号密码登录站点。

        :param site: 站点地址
        :return: (登录态Cookie字典, 提示信息)，登录失败时 Cookie 为 None
        """
        url = self.__build_url(site, self._login_path)
        if not url:
            return None, "站点地址无效"
        # SSPanel 系站点登录为表单提交：email + passwd；同时附带 password 字段兼容部分变体
        payload = {
            "email": self._email,
            "passwd": self._password,
            "password": self._password,
        }
        try:
            res = self.__request_utils().post_res(url=url, data=payload)
        except Exception as err:
            return None, f"请求异常：{err}"
        if res is None:
            return None, "网络超时或站点不可达"
        if res.status_code >= 400:
            return None, f"站点返回 HTTP {res.status_code}"
        data = self.__safe_json(res)
        # 站点显式返回失败时，直接认定登录失败
        if isinstance(data, dict) and data:
            if not self.__is_success(data):
                return None, self.__extract_message(data) or f"站点返回 HTTP {res.status_code}"
            # ret == 2 表示站点开启了两步验证
            if str(data.get("ret")) == "2":
                return None, "站点开启了两步验证（2FA），无法自动登录"
        cookies = dict(res.cookies.get_dict() or {})
        if not cookies:
            return None, "登录接口未返回会话信息，请检查接口路径"
        return cookies, self.__extract_message(data) or "登录成功"

    def __do_checkin(self, site: str, cookies: dict) -> Tuple[bool, str, bool]:
        """
        调用站点签到接口。

        :param site: 站点地址
        :param cookies: 登录态
        :return: (是否成功, 提示信息, 是否需要重新登录)
        """
        url = self.__build_url(site, self._checkin_path)
        if not url:
            return False, "签到地址无效", False
        try:
            res = self.__request_utils(cookies).post_res(url=url)
        except Exception as err:
            return False, f"请求异常：{err}", False
        if res is None:
            return False, "网络超时或站点不可达", False
        if res.status_code in (401, 403):
            return False, f"登录态已失效，需要重新登录（HTTP {res.status_code}）", True
        if res.status_code >= 500:
            return False, f"站点服务异常（HTTP {res.status_code}）", False
        data = self.__safe_json(res)
        message = self.__extract_message(data) or f"HTTP {res.status_code}"
        text = (res.text or "")[:500]
        # 今日已签到同样视为成功
        for keyword in self.ALREADY_KEYWORDS:
            if keyword.lower() in message.lower() or keyword.lower() in text.lower():
                return True, f"今日已签到：{message}", False
        if isinstance(data, dict) and data and not self.__is_success(data):
            need_relogin = any(keyword.lower() in (message + text).lower() for keyword in self.RELoginIN_KEYWORDS)
            return False, message, need_relogin
        if res.status_code >= 400:
            return False, message, res.status_code in (401, 403)
        return True, message, False

    # ---------------- 工具方法 ----------------
    def __request_utils(self, cookies: dict = None) -> RequestUtils:
        """
        构造请求工具，自动复用系统代理，超时 10 秒。

        :param cookies: 登录态，可为空
        :return: RequestUtils 实例
        """
        return RequestUtils(
            ua=self.USER_AGENT,
            cookies=cookies,
            # SSPanel 系站点使用表单提交，保持默认表单编码即可
            accept_type="application/json, text/plain, */*",
            proxies=settings.PROXY,
            timeout=self.REQUEST_TIMEOUT
        )

    def __save_states(self) -> None:
        """持久化各站点的登录态与签到记录。"""
        try:
            self.save_data("states", self._states)
        except Exception as err:
            logger.warning(f"[MJJSpeed] 签到记录保存失败：{err}")

    def __notify(self, title: str, text: str) -> None:
        """通过 MoviePilot 系统通知推送消息。"""
        if not self._notify:
            return
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:
            logger.warning(f"[MJJSpeed] 通知发送失败：{err}")

    def __send_notification(self, results: List[Dict[str, Any]]) -> None:
        """汇总签到结果并推送通知。"""
        if not results:
            return
        success_list = [item for item in results if item.get("success")]
        fail_list = [item for item in results if not item.get("success")]
        lines = []
        for item in results:
            flag = "成功" if item.get("success") else "失败"
            lines.append(f"[{flag}] {item.get('site')}：{item.get('message')}")
        title = "【MJJSpeed 每日签到】"
        text = (f"共 {len(results)} 个站点，成功 {len(success_list)} 个，失败 {len(fail_list)} 个\n"
                + "\n".join(lines))
        self.__notify(title, text)

    def __default_config(self) -> Dict[str, Any]:
        """返回插件默认配置（与 get_form 的 model 一一对应）。"""
        return {
            "enabled": True,
            "notify": True,
            "sites": self.DEFAULT_SITES,
            "email": "",
            "password": "",
            "cron": self.DEFAULT_CRON,
            "login_path": self.DEFAULT_LOGIN_PATH,
            "checkin_path": self.DEFAULT_CHECKIN_PATH,
            "retry": 3
        }

    @staticmethod
    def __safe_json(res) -> Any:
        """安全解析响应 JSON，解析失败返回 None。"""
        try:
            return res.json()
        except Exception:
            return None

    @staticmethod
    def __extract_message(data: Any) -> str:
        """从接口返回中提取提示信息。"""
        if not isinstance(data, dict):
            return ""
        for key in ("msg", "message", "info", "text", "result", "data"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                sub = mjjspeed_checkin.__extract_message(value)
                if sub:
                    return sub
        return ""

    @staticmethod
    def __is_success(data: dict) -> bool:
        """
        判断接口返回的 JSON 是否表示成功。

        兼容 ret/code/success/status/ok 等多种返回约定；无明确失败标识时视为成功。
        """
        if not isinstance(data, dict) or not data:
            return False
        for key in ("success", "ok"):
            value = data.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                if value.strip().lower() in ("true", "1", "ok", "success"):
                    return True
                if value.strip().lower() in ("false", "0", "error", "fail"):
                    return False
        for key in ("status",):
            value = data.get(key)
            if isinstance(value, str):
                if value.strip().lower() in ("success", "ok", "true", "1"):
                    return True
                if value.strip().lower() in ("error", "fail", "false", "0"):
                    return False
        for key in ("ret", "code", "errcode"):
            value = data.get(key)
            if value is None:
                continue
            try:
                ivalue = int(value)
            except (TypeError, ValueError):
                continue
            if key == "ret":
                return ivalue == 1
            return ivalue in (0, 1, 200)
        # 没有明确失败标识，视为成功
        return True

    @staticmethod
    def __build_url(site: str, path: str) -> str:
        """拼接站点完整 URL，缺省协议时自动补全 https://。"""
        site = (site or "").strip().rstrip("/")
        if not site:
            return ""
        if not site.startswith(("http://", "https://")):
            site = f"https://{site}"
        path = (path or "").strip()
        if path and not path.startswith("/"):
            path = f"/{path}"
        return f"{site}{path}"

    @classmethod
    def __parse_sites(cls, value: Any) -> List[str]:
        """解析多行站点列表，去重并保持顺序。"""
        text = str(value or "")
        if not text.strip():
            text = cls.DEFAULT_SITES
        sites: List[str] = []
        for line in text.splitlines():
            site = line.strip().strip(",").rstrip("/")
            if not site:
                continue
            if site not in sites:
                sites.append(site)
        return sites

    @staticmethod
    def __parse_cron(value: Any) -> str:
        """
        归一化签到时间为 5 段 Cron 表达式（分 时 日 月 周）。

        兼容旧版 HH:MM 配置，例如 01:00 会转换为 0 1 * * *；非法表达式回退默认值。
        """
        text = str(value or "").strip()
        if not text:
            return mjjspeed_checkin.DEFAULT_CRON
        # 旧版 HH:MM 自动转换为 Cron
        if ":" in text and len(text.split()) == 1:
            try:
                hour_str, minute_str = text.split(":")
                hour, minute = int(hour_str), int(minute_str)
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return f"{minute} {hour} * * *"
            except (ValueError, AttributeError):
                return mjjspeed_checkin.DEFAULT_CRON
        # 校验 Cron 表达式是否可被解析
        if mjjspeed_checkin.__build_trigger(text) is None:
            return mjjspeed_checkin.DEFAULT_CRON
        return text

    @staticmethod
    def __build_trigger(value: str) -> Optional[CronTrigger]:
        """
        把 Cron 表达式转换为 APScheduler 触发器，非法时返回 None。

        :param value: 5 段 Cron 表达式
        :return: CronTrigger 实例或 None
        """
        try:
            return CronTrigger.from_crontab(str(value or "").strip())
        except Exception:
            return None

    @staticmethod
    def __parse_path(value: Any, default: str) -> str:
        """校验接口路径，必须以 / 开头。"""
        text = str(value or "").strip()
        if not text:
            return default
        if not text.startswith("/"):
            text = f"/{text}"
        return text

    @staticmethod
    def __parse_retry(value: Any) -> int:
        """校验失败重试次数，限制在 1-10 之间。"""
        try:
            retry = int(value)
        except (TypeError, ValueError):
            return 3
        return max(1, min(10, retry))
