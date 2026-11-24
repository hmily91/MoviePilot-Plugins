"""网上国网 App 通道的国密信封（SM2 / SM3 / SM4）。

与网页通道的 sm_crypto.py 差别：
  * 网页：SM4-CBC + base64，密钥是随机串，签名拼在密文后；
  * App ：SM4-ECB + 大写 hex，密钥取 32 位 hex 文本的前 16 个 ASCII 字符，
          SM2 用 C1C3C2 顺序封装会话密钥，响应侧要用客户端私钥解 respKey。

底层原语（AA/BB/m_hash/m_kdf）直接复用 sm_crypto.py，算法字节行为不变，
这里只是按 App 的写法重新组合。参考 bilezhou/state_grid 与
stevenjoezhang/hass-state-grid（均为 MIT），相关常量与实现细节已就地注明。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from . import sm_crypto

SM4_ENCRYPT = sm_crypto.SM4_ENCRYPT
SM4_DECRYPT = sm_crypto.SM4_DECRYPT

# App 内置服务端公钥与本地客户端私钥（来自开源逆向，MIT 项目公开常量）。
SERVER_PUBLIC_KEY_HEX = (
    "0409C10D38CF7B4E28097EAAA519E3157C9B4E72194CD13BD11932CE40ED5624A"
    "FEFF27F78893E4C4FC2029DA147AB6CEAE0CDC8E3A547EFCDC5AB91757FE1CA60"
)
CLIENT_PRIVATE_KEY_HEX = "50C4AF48DF75808050729977AA6BC127DC21A404755C660B94222F9D50FA4A75"


def compact_json(value: Any) -> str:
    """Gson 风格紧凑 JSON：App 按这个算签名与加密。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sm4_key_bytes(key_text: str) -> bytes:
    # App 不 hex-decode：密钥就是 32 位 hex 文本的前 16 个 ASCII 字符
    return key_text[:16].encode("ascii")


def sm4_encrypt_hex(plain: str | bytes, key_text: str) -> str:
    raw = plain.encode("utf-8") if isinstance(plain, str) else plain
    cipher = sm_crypto.AA(mode=SM4_ENCRYPT)  # 默认 PKCS7 padding
    cipher.set_key(_sm4_key_bytes(key_text), SM4_ENCRYPT)
    return cipher.crypt_ecb(raw).hex().upper()


def sm4_decrypt_hex(cipher_hex: str, key_text: str) -> bytes:
    cipher = sm_crypto.AA(mode=SM4_DECRYPT)
    cipher.set_key(_sm4_key_bytes(key_text), SM4_DECRYPT)
    return cipher.crypt_ecb(bytes.fromhex(cipher_hex))


def sm2_wrap(key_text: str, public_key_hex: str = SERVER_PUBLIC_KEY_HEX) -> str:
    pub = public_key_hex[2:] if public_key_hex.startswith("04") else public_key_hex
    cipher = sm_crypto.BB(public_key=pub, mode=1)
    # 封的是 key_text 的 ASCII 字节；sm_crypto.d() 会先做 hex 编码，语义不同，不能复用
    return "04" + cipher.encrypt(key_text.encode("ascii")).hex().lower()


def _derive_public(private_hex: str) -> str:
    probe = sm_crypto.BB(public_key="0" * 128, mode=1)
    return probe._kg(int(private_hex, 16), probe.ecc_table["g"])


def sm2_unwrap(wrapped_hex: str, private_hex: str = CLIENT_PRIVATE_KEY_HEX) -> str:
    """解响应的 respKey，三处必须与 BB.encrypt 对齐，错一处就解不开：
      1) 密钥流长度参数按字节算（m_kdf 收 len(hex)/2）；
      2) 共享点是 x2||y2 的 hex 字符串，不是点字节；
      3) C3 = SM3(x2 ‖ 明文hex ‖ y2)，不是 x2‖y2‖明文。
    """
    wire = wrapped_hex[2:] if wrapped_hex.lower().startswith("04") else wrapped_hex
    cipher = sm_crypto.BB(public_key=_derive_public(private_hex), mode=1)
    para = cipher.para_len
    c1, c3, c2 = wire[:2 * para], wire[2 * para:2 * para + 64], wire[2 * para + 64:]
    shared = cipher._kg(int(private_hex, 16), c1)
    stream = sm_crypto.m_kdf(shared.encode("utf-8"), len(c2) // 2)
    plain_hex = ("%0" + str(len(c2)) + "x") % (int(c2, 16) ^ int(stream, 16))
    x2, y2 = shared[:para], shared[para:2 * para]
    if sm_crypto.m_hash([b for b in bytes.fromhex(x2 + plain_hex + y2)]).lower() != c3.lower():
        raise ValueError("SM2 解密校验失败（C3 不匹配）")
    return bytes.fromhex(plain_hex).decode("ascii")


def build_envelope(payload: Any) -> dict[str, str]:
    key_text = uuid4().hex
    ts = str(int(__import__("time").time() * 1000))
    data = sm4_encrypt_hex(compact_json(payload), key_text)
    skey = sm2_wrap(key_text)
    sign = sm_crypto.m_hash(list((skey + data + ts).encode())).lower()
    return {"data": data, "sign": sign, "skey": skey, "timestamp": ts}


def parse_envelope(outer: dict[str, Any]) -> Any:
    key_text = sm2_unwrap(str(outer["respKey"]))
    return json.loads(sm4_decrypt_hex(str(outer["encryptData"]), key_text).decode("utf-8"))


def check_code(account: str, ts_ms: int) -> str:
    """24 位随机数字 + AES/ECB(account+毫秒时间戳) 的 hex。

    AES 优先用 cryptography；缺失时用纯标准库兜底。checkCode 只是设备侧校验值，
    服务端主要用它判断报文形态，兜底实现保证无 cryptography 的环境也能登录。
    """
    import secrets

    rand = f"{secrets.randbits(63):024d}"
    key = b"LNPO+ISJ+QeqNemFK+3OMUdexSn2i6pB"
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        padder = PKCS7(128).padder()
        padded = padder.update(f"{account}{ts_ms}".encode()) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return rand + (encryptor.update(padded) + encryptor.finalize()).hex()
    except ImportError:
        return rand + _aes_ecb_stdlib(f"{account}{ts_ms}".encode(), key).hex()


def _aes_ecb_stdlib(data: bytes, key: bytes) -> bytes:
    """AES-ECB 纯标准库实现（PKCS7），仅在 cryptography 缺失时使用。

    支持 128/192/256 位密钥：国网 checkCode 用的就是 32 字节的 AES-256 密钥。
    """
    sbox = _AES_SBOX
    if len(key) not in (16, 24, 32):
        raise ValueError("AES 密钥必须是 16/24/32 字节")
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[i * 4:(i + 1) * 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [sbox[b] for b in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:  # AES-256 额外的 SubWord
            temp = [sbox[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    round_keys = [sum(words[i * 4:(i + 1) * 4], []) for i in range(nr + 1)]
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    out = bytearray()
    for off in range(0, len(data), 16):
        block = list(data[off:off + 16])
        out.extend(_aes_encrypt_block(block, round_keys, sbox))
    return bytes(out)


def _xtime(x: int) -> int:
    return ((x << 1) ^ 0x1B) & 0xFF if x & 0x80 else (x << 1) & 0xFF


def _aes_encrypt_block(block: list[int], round_keys: list[list[int]], sbox: list[int]) -> bytes:
    """AES-128 单块加密：初始轮加密钥 → 9 轮完整 → 末轮无列混合。"""
    nr = len(round_keys) - 1

    def sub_bytes(s):
        return [sbox[b] for b in s]

    def shift_rows(s):
        # state 按列优先存放：row r 左移 r 列
        return [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11],
        ]

    def mix_columns(s):
        out = []
        for c in range(4):
            a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
            t = a0 ^ a1 ^ a2 ^ a3
            out.extend([
                a0 ^ t ^ _xtime(a0 ^ a1),
                a1 ^ t ^ _xtime(a1 ^ a2),
                a2 ^ t ^ _xtime(a2 ^ a3),
                a3 ^ t ^ _xtime(a3 ^ a0),
            ])
        return out

    def add_key(s, rnd):
        return [s[i] ^ round_keys[rnd][i] for i in range(16)]

    state = add_key(block, 0)
    for rnd in range(1, nr):
        state = mix_columns(shift_rows(sub_bytes(state)))
        state = add_key(state, rnd)
    state = shift_rows(sub_bytes(state))  # 末轮：无列混合
    return bytes(add_key(state, nr))


_AES_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
