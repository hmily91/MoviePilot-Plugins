"""网上国网 App 通道：模拟官方 App 登录并取数（同步 urllib 版）。

为什么走 App 通道：网页通道的 bearer 只有约 25 分钟命，每轮都要重登，几天就撞上
国网的每日登录额度（RK001），之后当天再也取不到数据。App 网关不查图形验证码，
一次登录给的令牌有效约 15 天（实测 tokenExpireTime=1296000），14 天里无需重登，
从源头避开了限流。

请求信封用 SM2/SM3/SM4（app_crypto.py），设备身份由本地 turing/ 合成并持久化，
不依赖浏览器、短信打码或任何第三方服务。首次登录若被要求新设备验证（4006），
需提供一次短信验证码——这类账号在配置里填一次验证码即可。

接口均为非公开接口，可能随服务端调整或触发风控。参考实现：
bilezhou/state_grid 与 stevenjoezhang/hass-state-grid（均为 MIT）。
"""
from __future__ import annotations

import json
import random
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from . import app_crypto
from .client import ApiError, ConfigError, LoginError

APP_BASE_URL = "https://csc-service.sgcc.com.cn:28630"
LOGIN_PATH = "emss-uia-center-front/member/c2/f01"
DEVICE_SMS_PATH = "emss-uia-center-front/member/c1/f01"
DAILY_PATH = "emss-bia-bill-front/member/c11/f01"
MONTHLY_PATH = "emss-bia-bill-front/member/c51/f04"
BALANCE_PATH = "emss-bia-balance-front/member/c16/f01"

APP_VERSION = "3.2.3"
CHINA = timezone(timedelta(hours=8))
TOKEN_LIFE_SAFETY_S = 86400          # 剩余不足一天就提前重登
TOKEN_CACHE_MS = 14_400_000          # 设备令牌 4 小时缓存（对齐 App 的 Hawk 行为）
TIMEOUT_SECONDS = 30
GAP_MIN_S, GAP_MAX_S = 0.3, 0.5
SESSION_KEY = "app_session"
OK_CODES = {"1", "0000", "000000"}
CHECK_CODE_AES_KEY = b"LNPO+ISJ+QeqNemFK+3OMUdexSn2i6pB"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _end_of_day_ts() -> float:
    now = datetime.now(CHINA)
    return now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def _classify(code: str, text: str) -> str:
    merged = f"{code} {text}"
    if "RK001" in merged or "11401" in merged or "日额度" in merged or "网络连接超时" in merged:
        return "rate_limited"
    if "4006" in merged or "新设备" in merged:
        return "new_device"
    if "验证码" in merged:
        return "invalid_code"
    if "RK007" in merged or "RK008" in merged or "RK1003" in merged:
        return "captcha_required"
    if "密码" in merged or "账号" in merged or code in ("0100", "0101"):
        return "invalid_auth"
    return "unknown"


@dataclass
class AppAccount:
    account_id: str
    cons_no: str          # 密文户号（登录回包原样）
    cons_no_src: str      # 明文户号（取数用它）
    pro_no: str
    org_no: str
    elec_type: str

    @property
    def cons_type(self) -> str:
        if self.elec_type == "04" or (self.elec_type == "02" and self.pro_no == "11102"):
            return "02"
        return "01"

    @classmethod
    def from_api(cls, v: dict[str, Any]) -> "AppAccount":
        def first(*keys: str, default: str = "") -> str:
            for k in keys:
                if v.get(k) not in (None, ""):
                    return str(v[k])
            return default
        cons = first("powerUserNo", "consNo")
        return cls(
            account_id=first("id", "userId", default=cons),
            cons_no=cons,
            cons_no_src=first("powerUserNo_dst", "consNo_dst", default=cons),
            pro_no=first("proNo", "provinceId"),
            org_no=first("orgNo"),
            # 服务端字段拼写就是 constType
            elec_type=first("constType", "elecType", default="01"),
        )

    def as_door(self) -> dict[str, Any]:
        """转成现有 models/client 解析层认识的户号字典（网页形状）。"""
        return {
            "consNo": self.cons_no,
            "consNo_dst": self.cons_no_src,
            "proNo": self.pro_no,
            "provinceId": self.pro_no,
            "orgNo": self.org_no,
            "constType": self.elec_type,
            "elecTypeCode": self.elec_type,
        }


# ── 请求体构造 ──────────────────────────────────────────────────────────────
def _java_string_hash(value: str) -> int:
    out = 0
    for ch in value:
        out = (31 * out + ord(ch)) & 0xFFFFFFFF
    return out


def _java_map_order(value: Any) -> Any:
    """App 侧签名按 Java HashMap 桶序展开，顺序错一位 md5 就对不上。"""
    if isinstance(value, dict):
        buckets: dict[int, list[str]] = {}
        for key in value:
            h = _java_string_hash(str(key))
            h ^= h >> 16
            buckets.setdefault(h & 15, []).append(str(key))
        return {k: _java_map_order(value[k]) for b in sorted(buckets) for k in buckets[b]}
    if isinstance(value, list):
        return [_java_map_order(v) for v in value]
    return value


def _login_md5(params: dict[str, Any]) -> str:
    import hashlib
    material = "".join(
        k + _dumps(_java_map_order(params[k])) for k in sorted(params)
    )
    return hashlib.md5(material.encode("utf-8")).hexdigest()


def _login_payload(account: str, password_md5: str, model: str, release: str,
                   code: str = "", code_key: str = "") -> dict[str, Any]:
    # password_md5 是已算好的 32 位小写摘要；App 发小写，网页发大写，同一值
    ts = int(time.time() * 1000)
    push = "000000,000000"
    return {
        "quInfo": {"code": code, "codeKey": code_key, "account": account,
                   "password": password_md5, "optSys": "Android", "pushId": push,
                   "addressCity": "", "addressProvince": "", "addressRegion": ""},
        "uscInfo": {"isEncrypt": "false", "devciceIp": "127.0.0.1", "devciceId": "000000",
                    "tenant": "state_grid", "member": "2202", "optSys": f"Android {release}",
                    "operatorType": "", "devciceName": model, "pushId": push},
        "checkCode": app_crypto.check_code(account, ts),
        "avalonValidCode": "",
    }


def _device_sms_payload(account: str, model: str, release: str) -> dict[str, Any]:
    return {
        "uscInfo": {"tenant": "state_grid", "member": "2202", "devciceId": "000000",
                    "devciceName": model, "devciceIp": "127.0.0.1"},
        "quInfo": {"voiceCodeFlag": False, "account": account, "sendType": 0,
                   "businessType": "logindevice"},
    }


def _daily_payload(a: AppAccount, start: date, end: date) -> dict[str, Any]:
    return {"serviceCode": "BCP_000026", "source": "app", "target": a.pro_no,
            "data": {"acctId": "acctid01", "channelCode": "SGAPP",
                     "consNo": a.cons_no_src, "consNosrc": a.cons_no_src,
                     "endTime": end.isoformat(), "consType": a.cons_type,
                     "funcCode": "ALIPAY_01", "orgNo": a.org_no, "proCode": a.pro_no,
                     "promotCode": "1", "promotType": "1", "serialNo": "", "srvCode": "",
                     "startTime": start.isoformat(), "userName": "acctid01"}}


def _monthly_payload(a: AppAccount, year: int) -> dict[str, Any]:
    return {"serviceCode": "BCP_000026", "source": "app", "target": a.pro_no,
            "data": {"year": year, "consNo": a.cons_no_src, "provinceCode": a.pro_no,
                     "startYm": f"{year}01", "endYm": f"{year}12", "funcCode": "ALIPAY_01"}}


def _balance_payload(a: AppAccount, user_id: str) -> dict[str, Any]:
    return {"serviceCode": "0101143", "source": "app", "target": a.pro_no,
            "data": {"srvCode": "", "serialNo": "", "channelCode": "0902",
                     "funcCode": "A1007200", "acctId": user_id, "userName": "acctid01",
                     "promotType": "1", "promotCode": "1", "userAccountId": user_id,
                     "list": [{"consNoSrc": a.cons_no_src, "proCode": a.pro_no,
                               "sceneType": a.elec_type, "consNo": a.cons_no,
                               "orgNo": a.org_no}]}}


class AppChannel:
    """App 网关的登录与取数；令牌约 15 天有效，会话用插件 save_data 持久化。"""

    def __init__(self, account: str, password_digest: str,
                 data_getter: Callable[[str], Any],
                 data_setter: Callable[[str, Any], Any],
                 logger: Any, sms_code: str = "") -> None:
        self.account = (account or "").strip()
        if len(password_digest or "") != 32:
            raise ConfigError("密码摘要缺失或格式不正确，请在插件配置中重新输入密码")
        self.password_digest = password_digest.lower()
        self._get_data = data_getter
        self._set_data = data_setter
        self.log = logger
        self.sms_code = (sms_code or "").strip()

        self.token = ""
        self.user_id = ""
        self.province = ""
        self.accounts: list[AppAccount] = []
        self.raw_accounts: list[dict[str, Any]] = []
        self.expires_at = 0.0
        self.cooldown_until = 0.0
        self._authenticated = False
        self._device: Any = None
        self._profile: Any = None
        self._load_session()
        self._load_device()

    # ---------- 会话与设备持久化 ----------
    def _load_device(self) -> None:
        """恢复或首次创建合成设备的种子。种子即设备身份，必须跨次不变。"""
        import base64
        import secrets

        from .turing.feature_profile import StableProfile

        try:
            doc = self._get_data("app_device") or {}
        except Exception:
            doc = {}
        if not isinstance(doc, dict) or not doc.get("seed_b64"):
            doc = {"seed_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                   "boot_epoch_ms": int(time.time() * 1000) - random.randrange(86_400_000 * 13)}
            self._set_data("app_device", doc)
        try:
            self._profile = StableProfile(
                base64.b64decode(doc["seed_b64"]),
                boot_epoch_ms=int(doc.get("boot_epoch_ms") or 0),
            )
        except Exception as exc:
            raise ConfigError(f"合成设备画像损坏：{type(exc).__name__}")
        self._device = doc

    def _device_token(self) -> tuple[str, str]:
        """4 小时内的旧令牌直接用，过期则重新生成并落盘。"""
        from .turing.device_token import generate_device_token

        cached = str(self._device.get("token") or "")
        stamp = int(self._device.get("token_ms") or 0)
        now_ms = int(time.time() * 1000)
        if cached and 0 <= now_ms - stamp <= TOKEN_CACHE_MS:
            return cached, str(stamp)[:10]
        gen = generate_device_token(self._profile)
        self._device["token"] = gen.token
        self._device["token_ms"] = gen.timestamp_ms
        self._set_data("app_device", self._device)
        self.log.info("App 通道：本地设备令牌已生成（%d 字符）", len(gen.token))
        return gen.token, gen.token_time

    @property
    def _identity(self) -> tuple[str, str]:
        ident = self._profile.identity()
        return str(ident.model), str(ident.release)

    @property
    def _app_guid(self) -> str:
        import string as _string
        guid = self._device.get("app_guid")
        if guid:
            return str(guid)
        alphabet = _string.ascii_letters + _string.digits
        guid = "".join(alphabet[v % len(alphabet)] for v in self._profile.bytes("app-guid", 60))
        self._device["app_guid"] = guid
        self._set_data("app_device", self._device)
        return guid

    def _load_session(self) -> None:
        try:
            data = self._get_data(SESSION_KEY) or {}
        except Exception as exc:
            self.log.warning("读取 App 会话失败，改为重新登录：%s", type(exc).__name__)
            return
        if not isinstance(data, dict):
            return
        used = str(data.get("account") or "")
        if used and used != self.account:
            return  # 换账号后旧会话不可复用
        self.token = str(data.get("token") or "")
        self.user_id = str(data.get("user_id") or "")
        self.province = str(data.get("province") or "")
        self.expires_at = float(data.get("expires_at") or 0)
        self.cooldown_until = float(data.get("cooldown_until") or 0)
        self.raw_accounts = [v for v in (data.get("accounts") or []) if isinstance(v, dict)]
        self.accounts = [AppAccount.from_api(v) for v in self.raw_accounts]
        left = self.expires_at - time.time()
        self._authenticated = bool(self.token) and left > TOKEN_LIFE_SAFETY_S
        if self.token and not self._authenticated:
            self.log.info("App 会话剩余 %.1f 天，本轮提前重登", max(left, 0) / 86400)

    def _save_session(self) -> None:
        self._set_data(SESSION_KEY, {
            "account": self.account,
            "token": self.token,
            "user_id": self.user_id,
            "province": self.province,
            "expires_at": self.expires_at,
            "cooldown_until": self.cooldown_until,
            "accounts": self.raw_accounts,
            "saved_at": int(time.time()),
        })

    # ---------- 底层信封与请求 ----------
    def _post(self, path: str, payload: Any, *, login_params: dict | None = None,
              session_token: str = "") -> dict[str, Any] | None:
        time.sleep(random.uniform(GAP_MIN_S, GAP_MAX_S))
        now = datetime.now(CHINA)
        model, release = self._identity
        device_token, device_time = self._device_token()
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "timeStamp": (now.strftime("%Y%m%d%H%M%S") if login_params is not None
                          else now.strftime("%Y%m%d%H%M%S%f")[:17]
                          + "".join(str(random.randrange(10)) for _ in range(6))),
            "t": session_token,
            "userid": self.user_id if session_token else "0",
            "AppGuid": self._app_guid,
            "AppGuidNew": ("".join(random.choice(string.ascii_letters + string.digits)
                                   for _ in range(40))
                           + now.strftime("%Y%m%d%H%M%S%f")[:17]
                           + str(random.randrange(100, 1000))),
            "security": "android", "appcode": "WSGW-SG1001-APP", "datacenter": "99",
            "AccessMethod": "App", "deviceTokenTX": device_token,
            "deviceTokenTXTime": device_time,
            "province": self.province if session_token else "",
            "version": APP_VERSION, "wsgwType": "android", "ip": "127.0.0.1", "os": "android",
            "User-Agent": "okhttp/3.14.9",
        }
        if login_params is not None:
            headers["md5"] = _login_md5(login_params)
        body = _dumps(app_crypto.build_envelope(payload)).encode("utf-8")
        req = urllib.request.Request(APP_BASE_URL + "/" + path, data=body,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                outer = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.log.warning("App 通道 %s HTTP 错误 %s", path, exc.code)
            return None
        except Exception as exc:
            self.log.warning("App 通道 %s 请求失败：%s", path, type(exc).__name__)
            return None
        if not isinstance(outer, dict) or "respKey" not in outer:
            self.log.warning("App 通道 %s 回包不是加密信封", path)
            return None
        try:
            return app_crypto.parse_envelope(outer)
        except Exception as exc:
            self.log.warning("App 通道 %s 响应解不开：%s", path, type(exc).__name__)
            return None

    # ---------- 登录 ----------
    def _do_login(self, code: str = "", code_key: str = "") -> bool:
        model, release = self._identity
        params = _login_payload(self.account, self.password_digest, model, release,
                                code=code, code_key=code_key)
        plain = self._post(LOGIN_PATH, params, login_params=params)
        biz = ((plain or {}).get("data") or {}).get("bizrt") or {}
        ui = biz.get("userInfo") or {}
        if isinstance(ui, list):
            ui = ui[0] if ui else {}
        if not biz.get("token"):
            srv = ((plain or {}).get("data") or {}).get("srvrt") or {}
            code_out = str((plain or {}).get("code") or "")
            result_code = str(srv.get("resultCode") or "")
            text = str(srv.get("resultMessage") or (plain or {}).get("message") or "")
            reason = _classify(code_out or result_code, text)
            self.log.warning("App 登录未完成：code=%s resultCode=%s 分类=%s",
                             code_out or "?", result_code or "?", reason)
            self.last_error = reason
            if reason == "rate_limited":
                self.cooldown_until = _end_of_day_ts()
                self._save_session()
            if reason == "new_device":
                self.needs_device_sms = True
            raise LoginError(reason)
        self.token = str(biz["token"])
        self.user_id = str(ui.get("userId") or "")
        self.province = str(ui.get("addressProvince") or "")
        life = int(biz.get("tokenExpireTime") or 1296000)
        self.expires_at = time.time() + life
        raw = ui.get("powerUserList") or []
        self.raw_accounts = [v for v in raw if isinstance(v, dict)]
        self.accounts = [AppAccount.from_api(v) for v in self.raw_accounts]
        self._authenticated = True
        self.cooldown_until = 0.0
        self.last_error = ""
        self._save_session()
        self.log.info("App 登录成功：令牌 %d 字符、有效期 %d 天、户号 %d 块",
                      len(self.token), life // 86400, len(self.accounts))
        return True

    def request_device_sms(self) -> str:
        """让服务端给账号发一条新设备验证短信，返回 codeKey（有效期很短）。"""
        model, release = self._identity
        plain = self._post(DEVICE_SMS_PATH, _device_sms_payload(self.account, model, release))
        biz = (((plain or {}).get("data") or {}).get("bizrt") or {})
        key = str(biz.get("codeKey") or "") if isinstance(biz, dict) else ""
        self.log.info("新设备短信已请求：codeKey 长度 %d", len(key))
        return key

    def login(self, force: bool = False) -> None:
        if self.cooldown_until > time.time():
            raise LoginError("rate_limited", "当天已被限流，冷却到 24 点")
        if self._authenticated and not force:
            return
        self.needs_device_sms = False
        self.last_error = ""
        # 若配置了短信验证码，带上重新登录；否则走纯密码
        if self.sms_code:
            self._do_login(code=self.sms_code, code_key=self._last_code_key())
        else:
            self._do_login()

    def _last_code_key(self) -> str:
        try:
            data = self._get_data("app_code_key") or {}
            return str(data.get("codeKey") or "") if isinstance(data, dict) else ""
        except Exception:
            return ""

    def save_code_key(self, code_key: str) -> None:
        self._set_data("app_code_key", {"codeKey": code_key, "saved_at": int(time.time())})

    # ---------- 取数 ----------
    def _session_dead(self, plain: Any) -> bool:
        if not isinstance(plain, dict):
            return False
        code = str(plain.get("code") or "")
        srv = ((plain.get("data") or {}).get("srvrt") or {})
        text = str(srv.get("resultMessage") or plain.get("message") or "")
        return code in ("-200", "-201") or "登录状态已失效" in text

    def _authed(self, path: str, payload: Any) -> dict[str, Any] | None:
        self.login()
        plain = self._post(path, payload, session_token=self.token)
        if self._session_dead(plain):
            # 服务端提前作废会话：清掉本地那份重登一次再试（上游 issue #2 实测过）
            self._authenticated = False
            self.token = ""
            self.login(force=True)
            plain = self._post(path, payload, session_token=self.token)
        return plain

    def _wrap(self, plain: dict[str, Any] | None) -> dict[str, Any]:
        """把 App 回包包装成现有 models.py 认识的网页形状（data.bizrt.list）。"""
        if not isinstance(plain, dict):
            return {}
        data = plain.get("data")
        biz = data.get("bizrt") if isinstance(data, dict) else None
        payload = biz if isinstance(biz, dict) else (data if isinstance(data, dict) else {})
        result = {"code": str(plain.get("code") or "1")}
        if isinstance(payload, dict):
            result["data"] = {"bizrt": payload}
        else:
            result["data"] = {}
        return result

    def fetch_daily(self, door: dict[str, Any], start: str, end: str, year: int) -> dict[str, Any]:
        a = AppAccount.from_api(door)
        digits = lambda s: str(s or "").replace("-", "").replace("/", "")
        d1, d2 = digits(start), digits(end)
        iso = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else str(s)
        plain = self._authed(DAILY_PATH, _daily_payload(a, date.fromisoformat(iso(d1)),
                                                         date.fromisoformat(iso(d2))))
        return self._wrap(plain)

    def fetch_balance(self, door: dict[str, Any]) -> dict[str, Any]:
        a = AppAccount.from_api(door)
        plain = self._authed(BALANCE_PATH, _balance_payload(a, self.user_id))
        return self._wrap(plain)

    def fetch_ladder(self, door: dict[str, Any], year_month: str) -> dict[str, Any]:
        """App 网关没有阶梯档位接口——实测 c04/f03、c05/f01、c21/f01 均 404，
        c51/f04 月度账单里的 levelStdCode/secondRefPq 恒为 0，不含档位电量。
        返回空让上层明确走网页兜底，避免月账单被误判成"第0档、额度0"的假阶梯。
        """
        self.log.info("App 通道不提供阶梯档位，交由网页通道补齐")
        return {}
