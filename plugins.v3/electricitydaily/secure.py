"""插件凭据的单向摘要与落盘加密。

设计目标：
1. 用户仍可以在 MoviePilot 配置页里输入明文密码；
2. 明文不写入插件配置、不进入日志、不进入通知；
3. 95598 登录实际需要的是密码的 MD5 摘要，因此配置中只保留一份加密后的摘要；
4. 摘要用宿主的 API_TOKEN 作为 pepper 加密，单独拿到插件配置数据库也无法还原。

兜底路径只使用标准库（HMAC-SHA256 生成密钥流），仅用于没有 MoviePilot 宿主的
单元测试或本地调试；在真实宿主中优先使用 app.sdk.utilities.encrypt/decrypt。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any, Optional

FALLBACK_PREFIX = "v1:"
HOST_PREFIX = "mp1:"


def password_digest(password: str) -> str:
    """95598 登录体使用 32 位 MD5 摘要，大写形式在发请求时再转换。"""
    if not password:
        return ""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def _pepper() -> bytes:
    """优先取宿主 API_TOKEN；它是部署密钥，不随插件配置一起分发。"""
    token = ""
    try:
        from app.sdk.config import settings  # type: ignore

        token = str(getattr(settings, "API_TOKEN", "") or "")
    except Exception:
        pass
    token = token or os.getenv("API_TOKEN", "") or os.getenv("MOVIEPILOT_API_TOKEN", "")
    return hashlib.sha256(
        ("ElectricityDaily|" + token).encode("utf-8")
    ).digest()


def _fallback_crypt(secret: bytes, pepper: bytes, reveal: bool = False) -> bytes:
    """标准库兜底：HMAC 计数器模式异或。只做混淆，真实环境不要依赖这一层。"""
    out = bytearray()
    counter = 0
    while len(out) < len(secret):
        stream = hmac.new(pepper, counter.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = secret[len(out):len(out) + len(stream)]
        out.extend(bytes(x ^ y for x, y in zip(chunk, stream)))
        counter += 1
    return bytes(out) if reveal else bytes(out)


def seal(secret: str) -> str:
    """加密一段短秘密，返回可直接存进插件配置的字符串。空值返回空串。"""
    if not secret:
        return ""
    pepper = _pepper()
    try:
        from app.sdk.utilities import encrypt  # type: ignore

        encrypted = encrypt(secret.encode("utf-8"), pepper)
        return HOST_PREFIX + base64.b64encode(encrypted).decode("ascii")
    except Exception:
        encrypted = _fallback_crypt(secret.encode("utf-8"), pepper)
        return FALLBACK_PREFIX + base64.b64encode(encrypted).decode("ascii")


def unseal(value: str) -> str:
    """解开配置中的秘密；解不开时返回空串，绝不把密文当密码使用。"""
    if not value or not isinstance(value, str):
        return ""
    pepper = _pepper()
    try:
        if value.startswith(HOST_PREFIX):
            from app.sdk.utilities import decrypt  # type: ignore

            raw = base64.b64decode(value[len(HOST_PREFIX):])
            return bytes(decrypt(raw, pepper)).decode("utf-8")
        if value.startswith(FALLBACK_PREFIX):
            raw = base64.b64decode(value[len(FALLBACK_PREFIX):])
            return _fallback_crypt(raw, pepper, reveal=True).decode("utf-8")
    except Exception:
        return ""
    # 兼容无前缀的历史配置：不信任它，要求用户重新输入密码
    return ""


def mask(value: str) -> str:
    """日志/页面展示用：只留长度，不泄露内容。"""
    if not value:
        return "<空>"
    return f"<已隐藏,长度{len(value)}>"


def read_password_digest(config: Optional[dict[str, Any]]) -> str:
    """从配置里取出可用的登录摘要。"""
    return unseal((config or {}).get("password_digest_enc", ""))
