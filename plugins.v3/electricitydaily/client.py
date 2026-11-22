"""国家电网 95598 / 网上国网网页通道的最小取数客户端。

只做三件事：登录并维持会话、发四个业务请求、把回包原样交回解析层。
不写宿主数据库、不读插件配置、不发通知——这些都由插件入口负责。

协议要点（来自 bilezhou/state_grid，MIT）：
  * 握手 /oauth2/outer/c02/f02：本地生成 SM4 密钥，用内置 SM2 公钥封给网关；
  * 登录 /osg-web0004/open/c44/f06：先空手发一次拿 RK1003，再补
    complexSliderRet=0 / complexSliderType=clickImg 发第二次；
  * 授权 /oauth2/oauth/authorize + /oauth2/outer/getWebToken：换约 25 分钟有效的 bearer；
  * 业务请求的信封内层令牌取令牌后半段，请求头取前半段，不能写反。

接口均为非公开接口，可能随服务端调整或触发风控；进程内做了冷却和失败退避。
"""
from __future__ import annotations

import http.cookiejar
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from . import sm_crypto

BASE_API = "https://www.95598.cn/api"
KEY_API = "/oauth2/outer/c02/f02"
AUTHORIZE_API = "/oauth2/oauth/authorize"
WEB_TOKEN_API = "/oauth2/outer/getWebToken"
LOGIN_API = "/osg-web0004/open/c44/f06"

API_DOOR_LIST = "/osg-open-uc0001/member/c9/f02"
API_BALANCE = "/osg-open-bc0001/member/c05/f01"
API_LADDER = "/osg-open-bc0001/member/c04/f03"
API_DAILY = "/osg-web0004/member/c24/f01"
AUTH_APIS = (API_DOOR_LIST, API_BALANCE, API_LADDER, API_DAILY)

APP_KEY = "0329843199564c55809c77959792b558"
APP_SECRET = "4c1974786ee54d3bb4fb82c1ec5cd1a8"
SERVER_PUBLIC_KEY = (
    "04461932EDC916BFEF2EA324056296214E8281FDF9F962C82E28D59C7B98BB5ED"
    "479801B8AB8F86E933B73A136A431D40E0FF769A7209E63E67C8B9326F277A058"
)
TENANT = "state_grid"
MEMBER = "0902"
SESSION_KEY = "web_session"
OK_CODES = {"1", "0000", "000000"}
JSON_TYPE = "application/json;charset=UTF-8"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 30
GAP_MIN_S = 0.3
GAP_MAX_S = 0.5
BEARER_LIFE_SAFETY_S = 300
TOKEN_FALLBACK_S = 15 * 86400
RETRY_LOCK_S = 1800
RETRY_LOCK_MAX_S = 12 * 3600


class ElectricityError(Exception):
    """插件可预期的业务异常，消息中不含任何凭据。"""


class ConfigError(ElectricityError):
    """配置缺失或非法。"""


class LoginError(ElectricityError):
    """登录失败，reason 为稳定分类键。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"登录失败（{reason}）")


class ApiError(ElectricityError):
    """业务接口不可用或返回错误码。"""


class DataError(ElectricityError):
    """接口成功但没有解析出需要的数据。"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _payload(plain: Any) -> dict[str, Any]:
    if not isinstance(plain, dict):
        return {}
    data = plain.get("data")
    if isinstance(data, dict):
        biz = data.get("bizrt")
        return biz if isinstance(biz, dict) else data
    return plain


def _mask_digits(text: str) -> str:
    """把 6 位以上连续数字（户号/手机号/金额）打码，避免诊断信息泄漏隐私。"""
    import re

    return re.sub(r"\d{6,}", lambda m: "*" * len(m.group()), str(text))


# 这些字段是服务端状态码/提示，本身不是隐私，诊断时保留真实值最有价值。
# resultMessage 仍会经过 _mask_digits 打码，避免其中夹带户号或手机号。
DIAG_FIELDS = {"code", "message", "resultCode", "resultMessage", "errmsg"}


def shape(plain: Any, max_depth: int = 6) -> str:
    """回包脱敏结构：默认只留字段名/类型/数组长度；诊断字段留明文（已打码）。

    这样既能定位真实回包形态，又不会把户号、姓名、地址等隐私写进日志和通知。
    """
    def walk(node: Any, depth: int, key: str = "") -> str:
        if depth > max_depth:
            return "..."
        if isinstance(node, dict):
            parts = []
            for k in list(node.keys())[:24]:
                if k in DIAG_FIELDS and not isinstance(node[k], (dict, list)):
                    parts.append(f"{k}={_mask_digits(node[k])[:60]!r}")
                else:
                    parts.append(f"{k}:{walk(node[k], depth + 1, k)}")
            return "{" + ", ".join(parts) + "}"
        if isinstance(node, list):
            inner = walk(node[0], depth + 1) if node else ""
            return f"list[{len(node)}]{'<' + inner + '>' if inner else ''}"
        if isinstance(node, str):
            return f"str[{len(node)}]"
        if isinstance(node, bool):
            return "bool"
        if isinstance(node, (int, float)):
            return type(node).__name__
        return "null"
    return walk(plain, 0)


def business_error(plain: dict[str, Any]) -> str:
    """若回包业务码非成功，返回包含分类与服务端可读消息的简短说明；否则空串。

    国网的顶层 code 与 data.srvrt.resultCode 是两套独立状态码：顶层成功只代表
    HTTP/网关层通过，服务层失败时 data 里只有 srvrt、没有 bizrt。两套都必须查，
    否则服务层错误会被误判成"数据缺失"。
    """
    if not isinstance(plain, dict):
        return ""
    srv = _srvrt(plain)
    result_code = str(srv.get("resultCode") or "")
    text = str(srv.get("resultMessage") or plain.get("message") or "")
    bits: list[str] = []

    code = str(plain.get("code") or "")
    if code and code not in OK_CODES:
        bits.append(f"code={code}")
    # 关键：只要 srvrt.resultCode 存在且非成功码，就是服务层报错
    if result_code and result_code not in OK_CODES:
        bits.append(f"resultCode={result_code}")
    if not bits:
        return ""
    # 优先用真正报错的那套码来分类：顶层成功但 srvrt 失败时要用 resultCode
    err_code = result_code if (result_code and result_code not in OK_CODES) else code
    reason = classify_error(err_code, plain)
    bits.append(f"分类={reason}")
    # 服务端消息可能含账号/姓名，先打码再截断
    if text:
        bits.append("message=" + _mask_digits(text)[:60])
    return "，".join(bits)


def _srvrt(plain: Any) -> dict[str, Any]:
    data = plain.get("data") if isinstance(plain, dict) else None
    srv = data.get("srvrt") if isinstance(data, dict) else None
    return srv if isinstance(srv, dict) else {}


def _decrypt(plain: Any, key: str) -> dict[str, Any]:
    if not isinstance(plain, dict):
        return {}
    blob = plain.get("encryptData")
    if not blob and isinstance(plain.get("data"), str):
        blob = plain["data"]
    if blob and key:
        try:
            inner = json.loads(sm_crypto.sm4_decrypt(str(blob), key))
            if isinstance(inner, dict):
                return inner
        except Exception:
            pass
    return plain


def _end_of_day_ts() -> float:
    import datetime

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def classify_error(code: Any, plain: Optional[dict[str, Any]] = None) -> str:
    """把站点错误收敛成一组稳定键，供日志和通知使用。"""
    text = ""
    result_code = ""
    if plain:
        srv = _srvrt(plain)
        text = str(srv.get("resultMessage") or plain.get("message") or "")
        result_code = str(srv.get("resultCode") or "")
    code = str(code or "")
    if code in ("11401", "RK001") or result_code in ("11401", "RK001") or "RK001" in text:
        return "rate_limited"
    if code in ("RK007", "RK008", "RK1003"):
        return "captcha_required"
    if "验证码" in text:
        return "invalid_code"
    if result_code in ("0100", "0101") or "密码" in text or "账号" in text:
        return "invalid_auth"
    if "频繁" in text or "次数" in text or "限流" in text:
        return "rate_limited"
    if not plain:
        return "cannot_connect"
    return "unknown"


class StateGridClient:
    """一份网页会话。线程不安全，插件层用锁保证同一时刻只有一个任务在跑。"""

    def __init__(
        self,
        account: str,
        password_digest: str,
        data_getter: Callable[[str], Any],
        data_setter: Callable[[str, Any], Any],
        logger: Any,
        alt_account: str = "",
    ) -> None:
        self.account = (account or "").strip()
        self.primary_account = self.account
        self.alt_account = (alt_account or "").strip()
        if len(password_digest or "") != 32:
            raise ConfigError("密码摘要缺失或格式不正确，请在插件配置中重新输入密码")
        self.password_digest = password_digest
        self._get_data = data_getter
        self._set_data = data_setter
        self.log = logger

        self.key_code = ""
        self.public_key = SERVER_PUBLIC_KEY
        self.token = ""
        self.user_info: dict[str, Any] = {}
        self.access_token = ""
        self.bearer_deadline = 0.0
        self.expires_at = 0
        # 明文户号缓存：登录回包的 powerUserList 有时只给密文 consNo（无
        # consNo_dst），且国网每天登录次数有限、被限流后当天无法重登。
        # 一旦某次成功拿到明文就缓存下来，后续密文时按 userId 回填，避免
        # 把密文当户号去查业务接口（那样会静默拿到空数据、日报缺字段）。
        self.doors_cache: list[dict[str, Any]] = []
        self.cooldown_until = 0.0
        self.locked_until = 0.0
        self.fail_streak = 0
        self.last_error = ""
        self._authenticated = False

        cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        self._load_session()

    # ---------- 会话持久化（只保存令牌，不保存密码） ----------
    def _load_session(self) -> None:
        try:
            data = self._get_data(SESSION_KEY) or {}
        except Exception as exc:
            self.log.warning("读取国网会话失败，改为重新登录：%s", type(exc).__name__)
            return
        if not isinstance(data, dict):
            return
        self.key_code = str(data.get("keyCode") or "")
        self.public_key = str(data.get("publicKey") or "") or SERVER_PUBLIC_KEY
        self.token = str(data.get("token") or "")
        info = data.get("userInfo")
        self.user_info = info if isinstance(info, dict) else {}
        self.access_token = str(data.get("accessToken") or "")
        self.bearer_deadline = float(data.get("bearerDeadline") or 0)
        self.expires_at = int(data.get("expiresAt") or 0)
        cached = data.get("doorsCache")
        self.doors_cache = [x for x in cached if isinstance(x, dict)] if isinstance(cached, list) else []
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        used = str(data.get("account") or "")
        if used and used not in (self.account, self.alt_account):
            # 换账号后旧会话不可用，避免把别人的数据发出来
            self.token = ""
            self.access_token = ""
            return
        if used:
            self.account = used
        self._authenticated = bool(self.token) and self.expires_at > time.time() + 86400

    def _save_session(self) -> None:
        payload = {
            "keyCode": self.key_code,
            "publicKey": self.public_key,
            "token": self.token,
            "userInfo": self.user_info,
            "accessToken": self.access_token,
            "bearerDeadline": self.bearer_deadline,
            "expiresAt": self.expires_at,
            "account": self.account,
            "cooldownUntil": self.cooldown_until,
            "doorsCache": self.doors_cache,
            "savedAt": int(time.time()),
        }
        try:
            self._set_data(SESSION_KEY, payload)
        except Exception as exc:
            self.log.warning("保存国网会话失败：%s", type(exc).__name__)

    def close(self) -> None:
        """释放连接资源；可重复调用。"""
        try:
            self._opener.close()
        except Exception:
            pass

    # ---------- 底层信封 ----------
    def _raw(self, url: str, body: Any, headers: dict[str, str]) -> dict[str, Any]:
        time.sleep(random.uniform(GAP_MIN_S, GAP_MAX_S))
        if isinstance(body, dict):
            data = _dumps(body).encode("utf-8")
        else:
            data = str(body).encode("utf-8")
        headers = dict(headers)
        headers.setdefault("User-Agent", USER_AGENT)
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self.last_error = f"http_{exc.code}"
            self.log.warning("国网接口 HTTP 错误：%s", exc.code)
            return {}
        except Exception as exc:
            self.last_error = "cannot_connect"
            self.log.warning("国网接口请求失败：%s", type(exc).__name__)
            return {}
        if not text.lstrip().startswith("{"):
            self.last_error = "cannot_connect"
            self.log.warning("国网接口回包不是 JSON（%d 字节）", len(text))
            return {}
        try:
            plain = json.loads(text)
        except Exception:
            self.last_error = "invalid_json"
            return {}
        return plain if isinstance(plain, dict) else {}

    def _envelope(self, payload: Any, ts: int, wrap: bool = True) -> dict[str, str]:
        inner = payload if not wrap else {
            "_access_token": self.access_token[len(self.access_token) // 2:]
            if self.access_token else "",
            "_t": self.token[len(self.token) // 2:] if self.token else "",
            "_data": payload,
            "_s": ts,
        }
        cipher = sm_crypto.sm4_encrypt(_dumps(inner), self.key_code)
        return {
            "data": cipher + sm_crypto.sm3_hash(cipher + str(ts)),
            "skey": sm_crypto.sm2_encrypt_with_public_key(self.key_code, self.public_key),
            "timestamp": str(ts),
        }

    def _headers(self, api: str, ts: int, with_key: bool = True) -> dict[str, str]:
        headers = {
            "Accept": JSON_TYPE,
            "Content-Type": JSON_TYPE,
            "version": "1.0",
            "source": "0901",
            "timestamp": str(ts),
            "wsgwType": "web",
            "appKey": APP_KEY,
        }
        if with_key:
            headers["keyCode"] = self.key_code
        if api in AUTH_APIS and self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token[:len(self.access_token) // 2]
            headers["t"] = self.token[:len(self.token) // 2]
        return headers

    def _post(self, api: str, payload: Any, ts: int = 0, wrap: bool = True) -> dict[str, Any]:
        ts = ts or int(time.time() * 1000)
        plain = self._raw(
            BASE_API + api,
            self._envelope(payload, ts, wrap),
            self._headers(api, ts),
        )
        return _decrypt(plain, self.key_code)

    def _post_form(self, api: str, payload: dict[str, Any], key: str, ts: int = 0) -> dict[str, Any]:
        ts = ts or int(time.time() * 1000)
        headers = self._headers(api, ts, with_key=False)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(payload)
        return _decrypt(self._raw(BASE_API + api, body, headers), key)

    # ---------- 登录 ----------
    def _handshake(self) -> bool:
        local_key = sm_crypto.random_key(32, 16, 2)
        ts = int(time.time() * 1000)
        cipher = sm_crypto.sm4_encrypt(
            _dumps({"client_id": APP_KEY, "client_secret": APP_SECRET}), local_key
        )
        body = {
            "data": cipher + sm_crypto.sm3_hash(cipher + str(ts)),
            "skey": sm_crypto.sm2_encrypt_with_public_key(local_key, SERVER_PUBLIC_KEY),
            "client_id": APP_KEY,
            "timestamp": str(ts),
        }
        headers = {
            "Accept": JSON_TYPE,
            "Content-Type": JSON_TYPE,
            "version": "1.0",
            "source": "0901",
            "timestamp": str(ts),
            "wsgwType": "web",
            "appKey": APP_KEY,
        }
        plain = self._raw(BASE_API + KEY_API, body, headers)
        biz = _payload(_decrypt(plain, local_key))
        self.key_code = str(biz.get("keyCode") or "")
        self.public_key = str(biz.get("publicKey") or "") or SERVER_PUBLIC_KEY
        if not self.key_code:
            self.last_error = classify_error(plain.get("code") if plain else "", plain)
            self.log.warning("国网握手失败：%s", self.last_error)
            return False
        return True

    def _login_body(self, with_answer: bool) -> dict[str, Any]:
        body = {
            "params": {
                "uscInfo": {
                    "devciceIp": "",
                    "tenant": TENANT,
                    "member": MEMBER,
                    "devciceId": "",
                },
                "quInfo": {
                    "optSys": "android",
                    "pushId": "000000",
                    "addressProvince": "110100",
                    "password": self.password_digest.upper(),
                    "addressRegion": "110101",
                    "account": self.account,
                    "addressCity": "330100",
                },
            }
        }
        if with_answer:
            body["complexSliderRet"] = 0
            body["complexSliderType"] = "clickImg"
        return body

    def _attempt_login(self) -> bool:
        if not self._handshake():
            return False
        ts = int(time.time() * 1000)
        plain = self._post(LOGIN_API, self._login_body(False), ts)
        code = str(plain.get("code") or "")
        biz = _payload(plain)
        if not biz.get("token"):
            if code not in ("RK1003", "RK007", "RK008"):
                self.last_error = classify_error(code, plain)
                self.log.warning("国网登录未取得题目分支：%s", self.last_error)
                return False
            plain = self._post(LOGIN_API, self._login_body(True), ts + 1)
            code = str(plain.get("code") or "")
            biz = _payload(plain)
        if not biz.get("token"):
            self.last_error = classify_error(code, plain)
            self.log.warning("国网登录未拿到令牌：%s", self.last_error)
            return False
        self.token = str(biz["token"])
        info = biz.get("userInfo")
        self.user_info = info[0] if isinstance(info, list) and info else (info or {})
        self.expires_at = int(time.time()) + TOKEN_FALLBACK_S
        self._authenticated = True
        self.log.info("国网登录成功，令牌长度 %d", len(self.token))
        return True

    def login(self, force: bool = False) -> None:
        """登录或复用有效会话；失败时抛出分类明确的 LoginError。"""
        if self.cooldown_until > time.time():
            raise LoginError("rate_limited", "当天已被限流，冷却到 24 点")
        if self._authenticated and not force:
            return
        identifiers = [x for x in (self.account, self.alt_account) if x]
        # 去重但保持顺序
        identifiers = list(dict.fromkeys(identifiers))
        for account in identifiers:
            self.account = account
            if self._attempt_login():
                self._save_session()
                self._ensure_bearer()
                return
            if self.last_error != "rate_limited":
                break
        reason = self.last_error or "unknown"
        if reason == "rate_limited":
            self.cooldown_until = _end_of_day_ts()
            self._save_session()
        raise LoginError(reason)

    def _ensure_bearer(self) -> bool:
        if self.access_token and time.time() < self.bearer_deadline:
            return True
        if not self.token:
            return False
        ts = int(time.time() * 1000)
        auth = self._post_form(AUTHORIZE_API, {
            "client_id": APP_KEY,
            "response_type": "code",
            "redirect_url": "/test",
            "timestamp": ts,
            "rsi": self.token,
        }, self.token, ts)
        url = str(_payload(auth).get("redirect_url") or "")
        at = url.find("code=")
        if at < 0:
            self.last_error = classify_error(auth.get("code") if auth else "", auth)
            return False
        code32 = url[at + 5:at + 37]
        tok = self._post(WEB_TOKEN_API, {
            "grant_type": "authorization_code",
            "sign": sm_crypto.sm3_hash(APP_KEY + str(ts)),
            "client_secret": APP_SECRET,
            "state": "464606a4-184c-4beb-b442-2ab7761d0796",
            "key_code": self.key_code,
            "client_id": APP_KEY,
            "timestamp": ts,
            "code": code32,
        }, ts, wrap=False)
        biz = _payload(tok)
        self.access_token = str(biz.get("access_token") or "")
        life = int(biz.get("expiresIn") or 1800)
        self.bearer_deadline = time.time() + max(60, life - BEARER_LIFE_SAFETY_S)
        if not self.access_token:
            self.last_error = classify_error(tok.get("code") if tok else "", tok)
            return False
        self.last_error = ""
        self._save_session()
        return True

    # ---------- 业务请求 ----------
    def _locked(self) -> bool:
        return time.time() < self.locked_until

    def _lock(self) -> None:
        self.fail_streak += 1
        hold = min(RETRY_LOCK_S << (self.fail_streak - 1), RETRY_LOCK_MAX_S)
        self.locked_until = time.time() + hold
        self.log.warning(
            "国网通道暂停 %d 分钟（连续第 %d 次失败，原因=%s）",
            hold // 60, self.fail_streak, self.last_error or "unknown",
        )

    def _session_dead(self, plain: dict[str, Any]) -> bool:
        code = str(plain.get("code") or "")
        text = str(_srvrt(plain).get("resultMessage") or plain.get("message") or "")
        return code in ("-200", "-201") or "登录状态已失效" in text

    def call(self, api: str, payload: Any) -> dict[str, Any]:
        """发一个业务请求。会话失效自动重登一次，仍失败则抛出 ApiError。"""
        if self._locked():
            raise ApiError(f"接口暂时限流，{int(self.locked_until - time.time()) // 60} 分钟后重试")
        try:
            self.login()
        except LoginError:
            raise
        if not self._ensure_bearer():
            try:
                self.login(force=True)
            except LoginError:
                raise
            if not self._ensure_bearer():
                self._lock()
                raise ApiError("无法获取访问令牌")
        api_name = api.rsplit("/", 2)[-1]
        plain = self._post(api, payload)
        if self._session_dead(plain):
            try:
                self.login(force=True)
            except LoginError:
                self._lock()
                raise
            plain = self._post(api, payload)
            if self._session_dead(plain):
                self._lock()
                raise ApiError("会话失效且重新登录无效")
        if not plain:
            self.last_error = classify_error("", {})
            self._lock()
            raise ApiError(f"接口 {api_name} 无有效回包")
        # 关键：先校验业务码，避免把限流/验证码等错误回包送进解析层，
        # 造成"户号列表缺失"这类含糊报错。
        biz_err = business_error(plain)
        if biz_err:
            self.last_error = classify_error(str(plain.get("code") or ""), plain)
            self.log.warning("国网接口 %s 返回业务错误：%s", api_name, biz_err)
            if self.last_error == "rate_limited":
                self.cooldown_until = _end_of_day_ts()
                self._save_session()
            raise ApiError(f"接口 {api_name} 业务错误：{biz_err}")
        self.fail_streak = 0
        return plain

    # ---------- 四个业务接口 ----------
    @staticmethod
    def _is_plain_cons(cons_no: str) -> bool:
        """国网户号是纯数字串；带冒号/字母的是加密串（osg-uc 密文），不能用于取数。"""
        value = str(cons_no or "").strip()
        return value.isdigit()

    def _restore_plain_cons(self, doors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """用缓存的明文户号回填本次回包里只有密文 consNo 的户号。

        登录回包有时只下发密文 consNo（无 consNo_dst）。密文用于业务接口
        会静默拿到空数据。按 userId 优先、其次按位置顺序，把缓存明文补回。
        """
        plain_pool = [
            c for c in self.doors_cache
            if self._is_plain_cons(str(c.get("consNo_dst") or c.get("consNo") or ""))
        ]
        if not plain_pool:
            return doors
        by_user = {
            str(c.get("userId") or ""): c for c in plain_pool if c.get("userId")
        }
        used: set[int] = set()
        out: list[dict[str, Any]] = []
        for door in doors:
            if not isinstance(door, dict):
                out.append(door)
                continue
            cur = str(door.get("consNo_dst") or door.get("consNo") or "")
            if self._is_plain_cons(cur):
                out.append(door)
                continue
            match = by_user.get(str(door.get("userId") or ""))
            idx = None
            if match is None:
                for i, cached in enumerate(plain_pool):
                    if i not in used:
                        idx = i
                        match = cached
                        break
            if match is None:
                out.append(door)
                continue
            if idx is None:
                idx = plain_pool.index(match)
            used.add(idx)
            fixed = dict(door)
            plain = str(match.get("consNo_dst") or match.get("consNo") or "")
            fixed["consNo_dst"] = plain
            for field in ("proNo", "orgNo", "provinceId", "constType", "consSortCode"):
                if not fixed.get(field) and match.get(field):
                    fixed[field] = match[field]
            self.log.info("户号 %s 为密文，已用缓存明文回填", str(cur)[:12])
            out.append(fixed)
        return out

    def _door_identity(self, door: dict[str, Any]) -> dict[str, str]:
        cons_no = str(door.get("consNo_dst") or door.get("consNo") or "")
        return {
            "consNo": cons_no,
            "proNo": str(door.get("proNo") or door.get("provinceId") or ""),
            "orgNo": str(door.get("orgNo") or ""),
            "consType": str(door.get("constType") or door.get("elecTypeCode") or ""),
            "sceneType": str(door.get("consSortCode") or door.get("elecTypeCode") or ""),
        }

    def _usc_info(self) -> dict[str, str]:
        return {
            "member": MEMBER,
            "devciceIp": "",
            "devciceId": "",
            "tenant": TENANT,
        }

    def fetch_doors(self) -> list[dict[str, Any]]:
        user_id = str(self.user_info.get("userId") or "")
        payload = {
            "serviceCode": "01010049",
            "source": MEMBER,
            "target": "-1",
            "channelCode": "09",
            "channelNo": "09",
            # 关键：开源实现验证了 quInfo.userId 必传，缺它服务端只回 srvrt、不给 bizrt
            "quInfo": {"userId": user_id},
            "uscInfo": self._usc_info(),
            "token": self.token,
        }
        try:
            plain = self.call(API_DOOR_LIST, payload)
        except (ApiError, LoginError) as exc:
            # 当天登录被限流、或 bearer 续不上又无法重登时，退回缓存的明文户号，
            # 总比用密文查空、日报显示"缺字段"强。
            if self.doors_cache:
                self.log.warning(
                    "户号接口暂不可用（%s），改用缓存的 %d 个明文户号",
                    exc.reason if isinstance(exc, LoginError) else type(exc).__name__,
                    len(self.doors_cache),
                )
                return [dict(d) for d in self.doors_cache]
            raise
        biz = _payload(plain)
        # 兼容不同省份/客户端版本：data.bizrt.powerUserList / bizrt.powerUserList /
        # 顶层 powerUserList，以及 decryption 后直接就是列表的情况。
        doors: Any = None
        for node in (plain, plain.get("data"), biz, (biz.get("bizrt") if isinstance(biz, dict) else None)):
            if not isinstance(node, dict):
                continue
            candidate = node.get("powerUserList")
            if isinstance(candidate, list):
                doors = candidate
                break
        if doors is None and isinstance(biz, list):
            doors = biz
        # 兜底：App 通道登录回包的 userInfo 里直接带 powerUserList，无需该接口
        if not isinstance(doors, list):
            cached = self.user_info.get("powerUserList")
            if isinstance(cached, list):
                doors = cached
                self.log.info("户号列表接口未给数据，改用登录 userInfo 中的 %d 个户号", len(cached))
        if not isinstance(doors, list):
            srv = _srvrt(plain)
            hint = (
                "服务层成功但未返回业务数据（可能缺少 quInfo.userId 或该账号无绑定户号）"
                if str(srv.get("resultCode") or "") in OK_CODES
                else "服务层返回错误"
            )
            self.log.warning("户号列表解析失败（%s），回包结构：%s", hint, shape(plain))
            raise DataError(f"户号列表缺失：{hint}（回包结构：{shape(plain)[:400]}）")
        # 登录回包可能只有密文 consNo；先用缓存明文回填，避免把密文当户号取数
        doors = self._restore_plain_cons(doors)
        result = []
        for door in doors:
            if not isinstance(door, dict):
                continue
            # 05 是非居民/充电类户号，默认跳过
            if str(door.get("elecTypeCode") or "") == "05":
                continue
            if self._door_identity(door)["consNo"]:
                result.append(door)
        # 只要这次拿到了明文户号就更新缓存（供后续密文回包/限流日降级使用）
        plains = [
            d for d in result
            if self._is_plain_cons(self._door_identity(d)["consNo"])
        ]
        if plains:
            self.doors_cache = plains
            self._save_session()
        return result

    def fetch_balance(self, door: dict[str, Any]) -> dict[str, Any]:
        ident = self._door_identity(door)
        user_id = str(self.user_info.get("userId") or "")
        # 字段以真实回包验证为准：funcCode（非 busiTypeCode）；list 项里必须是
        # proCode（非 proNo）且场景用 consSortCode；consNo 用解密后的明文户号。
        payload = {
            "data": {
                "srvCode": "",
                "serialNo": "",
                "channelCode": MEMBER,
                "funcCode": "WEBA1007200",
                "acctId": user_id,
                "userName": str(self.user_info.get("loginAccount")
                                or self.user_info.get("userName")
                                or self.user_info.get("nickname") or ""),
                "promotType": "1",
                "promotCode": "1",
                "userAccountId": user_id,
                "list": [{
                    "consNoSrc": ident["consNo"],
                    "proCode": ident["proNo"],
                    "sceneType": ident["sceneType"],
                    "consNo": ident["consNo"],
                    "orgNo": ident["orgNo"],
                }],
            },
            "serviceCode": "0101143",
            "source": MEMBER,
            "target": ident["proNo"],
        }
        return self.call(API_BALANCE, payload)

    def fetch_ladder(self, door: dict[str, Any], year_month: str) -> dict[str, Any]:
        """year_month 形如 2026-11，按当前阶梯年度查询。

        字段以真实回包验证为准：服务端要 consType（不是 constType）、proCode、
        funcCode、queryDate(YYYY-MM)；consNo 用解密后的明文户号。
        当月账单尚未出数时（code=0/账单基础信息空），自动回退到上一个自然月。
        """
        ident = self._door_identity(door)
        user_id = str(self.user_info.get("userId") or "")

        def request(month: str) -> dict[str, Any]:
            payload = {
                "data": {
                    "acctId": user_id,
                    "channelCode": MEMBER,
                    "clearCache": "11",
                    "consType": ident["consType"],
                    "funcCode": "ALIPAY_01",
                    "orgNo": ident["orgNo"],
                    "proCode": ident["proNo"],
                    "promotCode": "1",
                    "promotType": "1",
                    "serialNo": "",
                    "srvCode": "",
                    "userName": "",
                    "provinceCode": ident["proNo"],
                    "userAccountId": user_id,
                    "consNo": ident["consNo"],
                    "queryDate": month,
                },
                "serviceCode": "BCP_000026",
                "source": "app",
                "target": ident["proNo"],
            }
            return self.call(API_LADDER, payload)

        def no_data(p: dict[str, Any]) -> bool:
            srv = _srvrt(p)
            msg = str(srv.get("resultMessage") or p.get("message") or "")
            return not bool((p.get("data") or {}).get("list")) and (
                "账单基础信息空" in msg or str(p.get("code") or "") not in OK_CODES
            )

        try:
            plain = request(year_month)
        except ApiError:
            # 当月账单未出时 call() 会抛 ApiError，这里转成"无数据"进入回退
            plain = {"code": "0", "message": "账单基础信息空"}
        if no_data(plain):
            # 当月未出账，回退上月再试一次
            try:
                y, m = (int(x) for x in year_month.split("-")[:2])
                prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
            except (ValueError, IndexError):
                return plain
            self.log.info("电费日报：%s 阶梯账单未出，回退到 %s", year_month, prev)
            try:
                plain = request(prev)
            except ApiError:
                return plain
        return plain

    @staticmethod
    def _dash_date(value: str) -> str:
        """把 20261101 / 2026-11-01 统一成服务端要的 2026-11-01。

        实测：c24/f01 只认带横杠的 ISO 日期；传纯数字 20261101 时服务端
        仍回 code=1 成功，但 data 是空的，日用电列表一行都没有——最隐蔽的坑。
        """
        digits = str(value or "").strip().replace("-", "").replace("/", "")
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
        return str(value or "")

    def fetch_daily(self, door: dict[str, Any], start: str, end: str, year: int) -> dict[str, Any]:
        """start/end 接受 20261001 或 2026-10-01，内部统一成带横杠的 ISO 日期。

        字段名与格式以真实账号实测通过的 getday 请求为准，三个坑：
          * startTime/endTime 必须是 2026-11-01 这种带横杠格式，纯数字回空 data；
          * 是 consType（不是 constType），且服务端只认固定值 "01"——用门号的
            constType=03 填会拿到空 sevenEleList；
          * 业务码字段是 funcCode（不是 busiTypeCode），值 WEBALIPAY_01。
        """
        start = self._dash_date(start)
        end = self._dash_date(end)
        ident = self._door_identity(door)
        user_id = str(self.user_info.get("userId") or "")
        user_name = str(self.user_info.get("loginAccount")
                        or self.user_info.get("userName") or "")
        payload = {
            "params1": {
                "serviceCode": "BCP_000026",
                "source": MEMBER,
                "target": "32101",
                "quInfo": {"userId": user_id},
                "uscInfo": self._usc_info(),
                "token": self.token,
            },
            "params3": {
                "data": {
                    "acctId": user_id,
                    "consNo": ident["consNo"],
                    "consType": "01",
                    "endTime": end,
                    "orgNo": ident["orgNo"],
                    "queryYear": year,
                    "proCode": ident["proNo"],
                    "promotCode": "1",
                    "promotType": "1",
                    "serialNo": "",
                    "srvCode": "",
                    "startTime": start,
                    "userName": user_name,
                    "funcCode": "WEBALIPAY_01",
                    "channelCode": MEMBER,
                    "clearCache": "11",
                    "serviceCode": "BCP_000026",
                    "source": "app",
                },
                "serviceCode": "BCP_000026",
                "source": "app",
                "target": ident["proNo"],
            },
            "params4": "010103",
        }
        return self.call(API_DAILY, payload)
