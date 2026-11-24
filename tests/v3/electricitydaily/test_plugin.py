"""电费日报插件单元测试（不依赖 MoviePilot 宿主与网络）。"""
from __future__ import annotations

import datetime
import hashlib
import os
import sys
import unittest
from typing import Any
from unittest import mock

PLUGIN_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "plugins.v3")
)
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import electricitydaily  # noqa: E402
from electricitydaily import message, models, secure  # noqa: E402
from electricitydaily.client import LoginError, StateGridClient  # noqa: E402


class FakeCronTrigger:
    @staticmethod
    def from_crontab(expression: str) -> "FakeCronTrigger":
        if len(expression.split()) != 5:
            raise ValueError("bad cron")
        return FakeCronTrigger()


def daily_payload() -> dict[str, Any]:
    return {
        "code": "1",
        "data": {"bizrt": {"sevenEleList": [
            {"day": "20261117", "dayElePq": "9.10", "thisPPq": "2.0"},
            {"day": "20261118", "dayElePq": "12.34", "thisPPq": "3.1",
             "thisNPq": "6.2", "thisVPq": "3.04", "thisTPq": "0"},
        ]}},
    }


def balance_payload() -> dict[str, Any]:
    return {
        "code": "1",
        "data": {"bizrt": {"account_balance": {
            "sumMoney": "123.45", "prepayBal": "123.45",
            "estiAmt": "88.80", "historyOwe": "0",
        }}},
    }


def ladder_payload() -> dict[str, Any]:
    return {
        "code": "1",
        "data": {"bizrt": {"list": [{
            "levlOnePq": "200", "levlOnePqSum": "79.5",
            "levlTwoPq": "200", "levlTwoPqSum": "20",
            "levlThrePqSum": "0", "levelStdCode": "1",
        }]}},
    }


class PluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.plugin_data: dict[str, Any] = {}
        self.messages: list[dict[str, str]] = []

    def make_plugin(self) -> electricitydaily.ElectricityDaily:
        plugin = electricitydaily.ElectricityDaily()
        plugin.update_config = lambda config, plugin_id=None: self.saved.append(dict(config)) or True  # type: ignore[method-assign]
        plugin.get_config = lambda plugin_id=None: self.saved[-1] if self.saved else None  # type: ignore[method-assign]
        plugin.save_data = lambda key, value, plugin_id=None: self.plugin_data.__setitem__(key, value)  # type: ignore[method-assign]
        plugin.get_data = lambda key=None, plugin_id=None: self.plugin_data.get(key)  # type: ignore[method-assign]
        plugin.post_message = lambda **kwargs: self.messages.append(kwargs)  # type: ignore[method-assign]
        return plugin

    def test_message_type_prefers_other_for_admin_only(self) -> None:
        """通知类型应为"其它"(MessageType.Other)，便于 MP 里限定仅管理员可见。"""
        from enum import Enum

        class FullTypes(Enum):
            Plugin = "插件"
            Other = "其它"

        class LegacyTypes(Enum):
            Plugin = "插件"

        self.assertIs(electricitydaily._resolve_message_type(FullTypes), FullTypes.Other)
        # 老版本没有 Other 时回退 Plugin，保证消息仍可发出
        self.assertIs(electricitydaily._resolve_message_type(LegacyTypes), LegacyTypes.Plugin)
        self.assertIsNone(electricitydaily._resolve_message_type(None))

    def test_password_is_never_stored_in_plaintext(self) -> None:
        plugin = self.make_plugin()
        plugin.init_plugin({"enabled": True, "username": "13800000000",
                            "password": "MySecret@2026", "cron": "0 8 * * *"})
        saved = self.saved[-1]
        self.assertEqual(saved.get("password"), "")
        self.assertNotIn("MySecret@2026", str(saved))
        expected = hashlib.md5(b"MySecret@2026").hexdigest()
        self.assertEqual(secure.unseal(saved["password_digest_enc"]), expected)

    def test_config_form_hides_password_and_exposes_cron(self) -> None:
        form, defaults = self.make_plugin().get_form()
        fields: dict[str, tuple[str, dict]] = {}

        def walk(nodes: list[dict]) -> None:
            for item in nodes:
                props = item.get("props", {})
                if props.get("model"):
                    fields[props["model"]] = (item.get("component"), props)
                walk(item.get("content", []))

        walk(form)
        self.assertEqual(fields["password"][0], "VTextField")
        self.assertEqual(fields["password"][1].get("type"), "password")
        self.assertEqual(defaults["cron"], "0 8 * * *")
        self.assertIn("notify_on_error", fields)

    def test_cron_service_is_registered_with_configured_expression(self) -> None:
        old = electricitydaily.CronTrigger
        electricitydaily.CronTrigger = FakeCronTrigger
        try:
            plugin = self.make_plugin()
            digest_enc = secure.seal(hashlib.md5(b"pw").hexdigest())
            plugin.init_plugin({"enabled": True, "username": "13800000000",
                                "password_digest_enc": digest_enc, "cron": "30 9 * * 1"})
            services = plugin.get_service()
            self.assertEqual(len(services), 1)
            self.assertEqual(services[0]["id"], "ElectricityDaily.DailyReport")
            self.assertEqual(services[0]["func"], plugin.refresh)
        finally:
            electricitydaily.CronTrigger = old

    def test_parsing_and_report(self) -> None:
        today = datetime.date(2026, 11, 19)
        report = models.build_report(
            "123456789", daily_payload(), balance_payload(), ladder_payload(), today=today,
        )
        self.assertEqual(report.daily.date, "2026-11-18")
        self.assertEqual(report.daily.total, 12.34)
        self.assertEqual(report.balance, 123.45)
        self.assertEqual(report.month_fee, 88.8)
        self.assertEqual(len(report.tiers), 3)
        self.assertEqual(report.tiers[0].remaining, 120.5)
        self.assertEqual(report.tiers[1].remaining, 180.0)
        self.assertEqual(report.stage, 1)
        text = message.build_text(report)
        self.assertIn("第一档", text)
        self.assertIn("剩余 120.50 kWh", text)
        self.assertIn("账户余额：123.45 元", text)

    def test_missing_data_is_marked_not_fabricated(self) -> None:
        report = models.build_report("123", {}, {}, {}, today=datetime.date(2026, 11, 19))
        self.assertEqual(len(report.missing), 3)
        text = message.build_text(report)
        self.assertIn("日用电量：暂无数据", text)
        self.assertIn("账户余额：暂无数据", text)
        self.assertIn("缺失字段", text)

    def test_login_failure_notifies_and_logs_without_secret(self) -> None:
        plugin = self.make_plugin()
        plugin.init_plugin({"enabled": True, "username": "13800000000",
                            "password": "TopSecret!", "notify_on_error": True})
        original = electricitydaily.AppChannel

        class FailingApp:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            def login(self) -> None:
                raise LoginError("invalid_auth")

        electricitydaily.AppChannel = FailingApp  # type: ignore[assignment]
        try:
            plugin.refresh()
        finally:
            electricitydaily.AppChannel = original  # type: ignore[assignment]
        self.assertEqual(len(self.messages), 1)
        self.assertIn("invalid_auth", self.messages[0]["text"])
        self.assertNotIn("TopSecret!", str(self.messages))
        self.assertIn("登录失败", plugin._last_status)

    def test_partial_api_failure_keeps_other_fields(self) -> None:
        plugin = self.make_plugin()
        plugin.init_plugin({"enabled": True, "username": "13800000000",
                            "password": "pw", "notify_on_error": True})
        original_app = electricitydaily.AppChannel
        original_web = electricitydaily.StateGridClient

        class FakeAccount:
            def as_door(self) -> dict[str, Any]:
                return {"consNo_dst": "123456789", "consNo": "123456789",
                        "proNo": "11102", "orgNo": "11102", "constType": "01"}

        class PartialApp:
            def __init__(self, **kwargs: Any) -> None:
                self.accounts = [FakeAccount()]

            def login(self) -> None:
                pass

            def fetch_daily(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return daily_payload()

            def fetch_balance(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {}

            def fetch_ladder(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {}

        class FailingWeb:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def login(self) -> None:
                raise LoginError("rate_limited")

            def close(self) -> None:
                pass

        electricitydaily.AppChannel = PartialApp  # type: ignore[assignment]
        electricitydaily.StateGridClient = FailingWeb  # type: ignore[assignment]
        try:
            plugin.refresh()
        finally:
            electricitydaily.AppChannel = original_app  # type: ignore[assignment]
            electricitydaily.StateGridClient = original_web  # type: ignore[assignment]
        self.assertEqual(len(self.messages), 1)
        self.assertIn("账户余额：暂无数据", self.messages[0]["text"])
        self.assertIn("**阶梯电量剩余**\n- 暂无数据", self.messages[0]["text"])
        self.assertIn("缺失字段：账户余额、阶梯电量", self.messages[0]["text"])

    def test_client_login_body_uses_digest_only(self) -> None:
        digest = hashlib.md5(b"plain-password").hexdigest()
        client = StateGridClient(
            account="13800000000",
            password_digest=digest,
            data_getter=lambda key: None,
            data_setter=lambda key, value: None,
            logger=mock.MagicMock(),
        )
        body = client._login_body(True)
        self.assertEqual(body["complexSliderType"], "clickImg")
        self.assertEqual(body["params"]["quInfo"]["password"], digest.upper())
        self.assertNotIn("plain-password", str(body))
        client.close()

    def test_page_shows_diagnosis_when_credentials_missing(self) -> None:
        plugin = self.make_plugin()
        plugin.init_plugin({"enabled": True, "username": "", "password": ""})
        page = plugin.get_page()
        text = str(page)
        self.assertIn("未保存", text)
        self.assertIn("尚未运行", text)
        self.assertIn("保存后立即运行一次", text)

    def test_page_shows_login_error_category(self) -> None:
        plugin = self.make_plugin()
        plugin.init_plugin({"enabled": True, "username": "13800000000", "password": "pw"})
        plugin._handle_failure(LoginError("invalid_auth"), "登录失败", notify=False)
        self.assertIn("invalid_auth", plugin._last_status)

    def test_business_error_code_rejected_before_parsing(self) -> None:
        from electricitydaily.client import ApiError, business_error

        limited = {"code": "11401", "message": "操作频繁请稍后再试"}
        self.assertIn("rate_limited", business_error(limited))
        self.assertEqual(business_error({"code": "1", "data": {}}), "")
        self.assertEqual(business_error({"code": "0000"}), "")

    def test_shape_masks_all_values(self) -> None:
        from electricitydaily.client import shape

        secret = "4206668917676"  # 户号，绝不能出现在结构描述里
        plain = {"code": "1", "data": {"bizrt": {"powerUserList": [
            {"consNo": secret, "userName": "张三"}
        ]}}}
        text = shape(plain)
        self.assertNotIn(secret, text)
        self.assertNotIn("张三", text)
        self.assertIn("powerUserList", text)
        self.assertIn("consNo", text)

    def test_srvrt_result_code_rejected_when_top_code_ok(self) -> None:
        """这次的真实故障：顶层 code 成功、srvrt.resultCode 失败，必须拦截。"""
        from electricitydaily.client import business_error

        plain = {"code": 1, "message": "成功", "data": {"srvrt": {
            "timeStamp": "1763700000000", "resultCode": "0100",
            "resultMessage": "该账号未绑定用电户号",
        }}}
        err = business_error(plain)
        self.assertIn("resultCode=0100", err)
        self.assertIn("invalid_auth", err)
        # 服务层成功码则放行
        ok = {"code": 1, "message": "成功", "data": {
            "srvrt": {"resultCode": "0000", "resultMessage": "成功"}, "bizrt": {},
        }}
        self.assertEqual(business_error(ok), "")

    def test_shape_keeps_diagnostic_codes_but_masks_digits(self) -> None:
        """诊断字段留明文便于定位，但其中的户号/手机号必须打码。"""
        from electricitydaily.client import shape

        plain = {"code": 1, "message": "成功", "data": {"srvrt": {
            "resultCode": "0100", "resultMessage": "户号4206668917676无权限",
        }}}
        text = shape(plain)
        self.assertIn("resultCode='0100'", text)
        self.assertNotIn("4206668917676", text)
        self.assertIn("*" * 13, text)

    def _make_client(self) -> StateGridClient:
        return StateGridClient(
            account="13800000000",
            password_digest=hashlib.md5(b"pw").hexdigest(),
            data_getter=lambda key: None,
            data_setter=lambda key, value: None,
            logger=mock.MagicMock(),
        )

    def test_doors_payload_includes_qu_info_user_id(self) -> None:
        client = self._make_client()
        client.user_info = {"userId": "uid-123"}
        captured: dict[str, Any] = {}

        def fake_call(api: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            return {"code": "1", "data": {"bizrt": {"powerUserList": [
                {"consNo_dst": "4206668917676", "elecTypeCode": "01"},
            ]}}}

        client.call = fake_call  # type: ignore[method-assign]
        doors = client.fetch_doors()
        self.assertEqual(len(doors), 1)
        self.assertEqual(captured["payload"]["quInfo"], {"userId": "uid-123"})
        client.close()

    def test_doors_fallback_from_login_userinfo(self) -> None:
        """c9/f02 只回 srvrt 时，从登录 userInfo.powerUserList 兜底取户号。"""
        client = self._make_client()
        client.user_info = {"userId": "uid-123", "powerUserList": [
            {"consNo_dst": "4206668917676", "elecTypeCode": "01"},
        ]}
        # 服务层成功但没有 bizrt —— 正是这次的回包形态
        client.call = lambda api, payload: {"code": 1, "data": {"srvrt": {  # type: ignore[method-assign]
            "resultCode": "0000", "resultMessage": "成功",
        }}}
        doors = client.fetch_doors()
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0]["consNo_dst"], "4206668917676")
        client.close()

    def test_cipher_cons_restored_from_cache(self) -> None:
        """登录回包只给密文 consNo 时，用缓存明文回填，避免密文查空。"""
        client = self._make_client()
        client.user_info = {"userId": "uid-123"}
        client.doors_cache = [{
            "userId": "uid-123", "consNo_dst": "4206668917676",
            "proNo": "345", "orgNo": "4206", "elecTypeCode": "01",
        }]
        client.call = lambda api, payload: {"code": "1", "data": {"bizrt": {  # type: ignore[method-assign]
            "powerUserList": [{
                "userId": "uid-123",
                "consNo": "99:24a9725a494e490f9bd5df7b31cbd75d:02:osg-uc:001",
                "elecTypeCode": "01",
            }],
        }}}
        doors = client.fetch_doors()
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0]["consNo_dst"], "4206668917676")
        self.assertEqual(client._door_identity(doors[0])["consNo"], "4206668917676")
        client.close()

    def test_doors_fallback_to_cache_when_rate_limited(self) -> None:
        """限流/无法重登时，退回缓存明文户号，而不是让整轮失败。"""
        client = self._make_client()
        client.user_info = {"userId": "uid-123"}
        client.doors_cache = [{"userId": "uid-123", "consNo_dst": "4206668917676"}]

        def boom(api, payload):  # type: ignore[no-untyped-def]
            raise LoginError("rate_limited")

        client.call = boom  # type: ignore[method-assign]
        doors = client.fetch_doors()
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0]["consNo_dst"], "4206668917676")
        client.close()

    def test_daily_payload_includes_qu_info_user_id(self) -> None:
        client = self._make_client()
        client.user_info = {"userId": "uid-456"}
        captured: dict[str, Any] = {}

        def fake_call(api: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            return {}

        client.call = fake_call  # type: ignore[method-assign]
        client.fetch_daily({"consNo_dst": "4206668917676"}, "20261101", "20261119", 2026)
        self.assertEqual(captured["payload"]["params1"]["quInfo"], {"userId": "uid-456"})
        client.close()

    def test_daily_payload_uses_server_field_names(self) -> None:
        """日用电 must use consType=01 + funcCode=WEBALIPAY_01, not constType/busiTypeCode."""
        client = self._make_client()
        client.user_info = {"userId": "uid-456", "loginAccount": "acct"}
        captured: dict[str, Any] = {}

        def fake_call(api: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            return {}

        client.call = fake_call  # type: ignore[method-assign]
        # 故意给门号塞 elecTypeCode=03，服务端固定要 01，不能被门号值带偏
        client.fetch_daily(
            {"consNo_dst": "4206668917676", "elecTypeCode": "03"},
            "20261101", "20261119", 2026,
        )
        data = captured["payload"]["params3"]["data"]
        self.assertEqual(data.get("consType"), "01")
        self.assertNotIn("constType", data)
        self.assertEqual(data.get("funcCode"), "WEBALIPAY_01")
        self.assertNotIn("busiTypeCode", data)
        # 实测：服务端只认带横杠的 ISO 日期，纯数字格式回空 data
        self.assertEqual(data.get("startTime"), "2026-11-01")
        self.assertEqual(data.get("endTime"), "2026-11-19")
        self.assertEqual(data.get("queryYear"), 2026)
        client.close()

    def test_daily_payload_accepts_dashed_dates(self) -> None:
        """调用方直接传带横杠日期时不应被二次加工。"""
        client = self._make_client()
        client.user_info = {"userId": "uid-456"}
        captured: dict[str, Any] = {}
        def fake_call(api: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            return {}
        client.call = fake_call  # type: ignore[method-assign]
        client.fetch_daily(
            {"consNo_dst": "4206668917676"}, "2026-10-12", "2026-11-20", 2026,
        )
        data = captured["payload"]["params3"]["data"]
        self.assertEqual(data.get("startTime"), "2026-10-12")
        self.assertEqual(data.get("endTime"), "2026-11-20")
        client.close()

    def test_parse_daily_skips_unbilled_today(self) -> None:
        """服务端会把当天返回一行但 dayElePq 为空，不能取这条空记录。

        实测：end=今天时服务端返回 41 行，最后一行 2026-11-21 无 dayElePq；
        若取最新会拿到 total=None，日报显示"暂无数据"。应跳过空行取昨天。
        """
        rows = [
            {"day": "20261119", "dayElePq": "4.10"},
            {"day": "20261120", "dayElePq": "5.27", "thisPPq": "1.50"},
            {"day": "20261121", "dayElePq": ""},  # 今天，未出账
        ]
        plain = {"code": "1", "data": {"sevenEleList": rows}}
        du = models.parse_daily(plain, datetime.date(2026, 11, 21))
        self.assertEqual(du.date, "2026-11-20")
        self.assertEqual(du.total, 5.27)

    def test_parse_real_ladder_fields(self) -> None:
        """湖北 c04/f03 真实字段：oneLevelPq/firstBalPq/totalYearPq/levelFlag。"""
        plain = {"code": "1", "data": {"list": [{"electricParticulars": {
            "oneLevelPq": "2160", "twoLevelPq": "2640",
            "firstBalPq": "212", "secondBalPq": "2640",
            "totalYearPq": "1948", "levelFlag": "2",
        }}]}}
        tiers, stage = models.parse_ladder(plain)
        self.assertEqual(stage, 2)
        self.assertEqual(len(tiers), 3)
        self.assertEqual(tiers[0].quota, 2160.0)
        self.assertEqual(tiers[0].remaining, 212.0)
        self.assertEqual(tiers[1].quota, 2640.0)
        self.assertEqual(tiers[1].remaining, 2640.0)

    def test_parse_balance_from_data_list(self) -> None:
        """真实余额回包在 data.list[0]，不是 data.bizrt。"""
        plain = {"code": "1", "data": {"rtnCode": "0", "list": [{
            "sumMoney": "162.91", "prepayBal": "215.36",
            "estiAmt": "52.45", "historyOwe": "0", "totalPq": "154",
        }]}}
        result = models.parse_balance(plain)
        self.assertEqual(result["balance"], 162.91)
        self.assertEqual(result["month_fee"], 52.45)
        self.assertEqual(result["history_owe"], 0.0)

    def test_balance_payload_uses_func_code_and_pro_code(self) -> None:
        """余额 payload 必须用 funcCode 与 list 项 proCode，否则服务端返回空。"""
        client = self._make_client()
        client.user_info = {"userId": "uid-1", "loginAccount": "acct"}
        captured: dict[str, Any] = {}

        def fake_call(api: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            return {}

        client.call = fake_call  # type: ignore[method-assign]
        client.fetch_balance({"consNo_dst": "4206668917676", "proNo": "42102",
                              "orgNo": "424050208", "consSortCode": "01"})
        data = captured["payload"]["data"]
        self.assertIn("funcCode", data)
        self.assertNotIn("busiTypeCode", data)
        self.assertIn("proCode", data["list"][0])
        self.assertNotIn("proNo", data["list"][0])
        client.close()

    def test_secure_roundtrip(self) -> None:
        secret = hashlib.md5(b"pw").hexdigest()
        sealed = secure.seal(secret)
        self.assertNotEqual(sealed, secret)
        self.assertEqual(secure.unseal(sealed), secret)


class AppChannelTest(unittest.TestCase):
    """App 通道：模拟官方 App 登录取数，绕开网页通道的每日登录限流。"""

    def test_sm4_sm2_envelope_roundtrip(self) -> None:
        """App 国密信封自洽：SM4-ECB(hex) + SM2 封装会话密钥。"""
        from electricitydaily import app_crypto

        key = "a" * 32
        cipher = app_crypto.sm4_encrypt_hex("hello国网", key)
        self.assertEqual(app_crypto.sm4_decrypt_hex(cipher, key).decode(), "hello国网")
        # 用客户端私钥对应的公钥封，再解，验证 SM2 算法本身
        pub = app_crypto._derive_public(app_crypto.CLIENT_PRIVATE_KEY_HEX)
        wrapped = app_crypto.sm2_wrap(key, pub)
        self.assertEqual(app_crypto.sm2_unwrap(wrapped), key)

    def test_aes_check_code_matches_cryptography(self) -> None:
        """checkCode 的 AES-256 兜底必须与标准库结果一致。"""
        from electricitydaily import app_crypto

        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives.padding import PKCS7
        except ImportError:
            self.skipTest("cryptography 未安装")
        data = b"1380000000123456"
        padder = PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        enc = Cipher(algorithms.AES(b"LNPO+ISJ+QeqNemFK+3OMUdexSn2i6pB"),
                     modes.ECB()).encryptor()
        ref = (enc.update(padded) + enc.finalize()).hex()
        mine = app_crypto._aes_ecb_stdlib(data, b"LNPO+ISJ+QeqNemFK+3OMUdexSn2i6pB").hex()
        self.assertEqual(mine, ref)

    def test_device_token_is_stable_from_seed(self) -> None:
        """同一设备种子生成稳定身份；不同种子得到不同设备。"""
        from electricitydaily.turing.feature_profile import StableProfile
        from electricitydaily.turing.device_token import generate_device_token

        p1 = StableProfile(b"s" * 32)
        p2 = StableProfile(b"s" * 32)
        # 同种子 -> 同设备身份（model/uuid/serial 等稳定）
        self.assertEqual(p1.identity().model, p2.identity().model)
        self.assertEqual(p1.identity().profile_id, p2.identity().profile_id)
        # 令牌本身含每次随机的 nonce 与 SM2 随机点，故不逐字节相同；但都带 v3: 前缀
        t1 = generate_device_token(p1, timestamp_ms=1700000000000)
        self.assertTrue(t1.token.startswith("v3:"))
        self.assertEqual(t1.token_time, "1700000000")

    def test_daily_payload_uses_plain_cons_and_iso_dates(self) -> None:
        from electricitydaily.app_client import AppAccount, _daily_payload

        a = AppAccount.from_api({"powerUserNo": "ENC", "powerUserNo_dst": "1234567890",
                                 "proNo": "11102", "orgNo": "11102", "constType": "01"})
        p = _daily_payload(a, datetime.date(2026, 11, 1), datetime.date(2026, 11, 23))
        self.assertEqual(p["data"]["consNo"], "1234567890")
        self.assertEqual(p["data"]["consNosrc"], "1234567890")
        self.assertEqual(p["data"]["startTime"], "2026-11-01")
        self.assertEqual(p["data"]["funcCode"], "ALIPAY_01")

    def test_cons_type_mapping(self) -> None:
        from electricitydaily.app_client import AppAccount

        self.assertEqual(AppAccount.from_api({"constType": "01"}).cons_type, "01")
        self.assertEqual(AppAccount.from_api({"constType": "04"}).cons_type, "02")
        # 北京的 02 特殊
        self.assertEqual(AppAccount.from_api({"constType": "02", "proNo": "11102"}).cons_type, "02")

    def test_app_wrap_shapes_response_for_existing_parser(self) -> None:
        """App 回包包装成 data.bizrt 后，现有 parse_daily 能直接解析。"""
        from electricitydaily.app_client import AppChannel

        ch = AppChannel.__new__(AppChannel)
        app_plain = {"code": "1", "data": {"sevenEleList": [
            {"day": "2026-11-18", "dayElePq": "12.34", "thisPPq": "3.1"},
        ]}}
        wrapped = ch._wrap(app_plain)
        usage = models.parse_daily(wrapped, today=datetime.date(2026, 11, 24))
        self.assertEqual(usage.date, "2026-11-18")
        self.assertEqual(usage.total, 12.34)

    def test_session_expiry_forces_relogin(self) -> None:
        """令牌剩余不足一天时不应复用旧会话。"""
        from electricitydaily.app_client import AppChannel

        store: dict[str, Any] = {
            "app_device": {"seed_b64": __import__("base64").b64encode(b"s" * 32).decode(),
                           "boot_epoch_ms": 1700000000000},
            "app_session": {"account": "13800000000", "token": "t" * 40,
                            "user_id": "u1", "expires_at": __import__("time").time() + 3600,
                            "accounts": []},
        }
        ch = AppChannel(
            account="13800000000", password_digest="a" * 32,
            data_getter=lambda k: store.get(k),
            data_setter=lambda k, v: store.__setitem__(k, v),
            logger=mock.MagicMock(),
        )
        self.assertFalse(ch._authenticated)  # 只剩 1 小时，强制重登


class LadderParseTest(unittest.TestCase):
    """阶梯解析必须拦截月账单假数据，只认真实档位电量。"""

    def test_app_monthly_bill_rejected(self) -> None:
        # App c51/f04 月账单：levelStdCode/secondRefPq 恒为 0，不是阶梯
        from electricitydaily.models import parse_ladder

        plain = {"code": "1", "data": {"bizrt": {"yearPq": "708", "list": [
            {"levelStdCode": "0", "secondRefPq": "0", "monthPq": "67", "ym": "202610"},
        ]}}}
        tiers, stage = parse_ladder(plain)
        self.assertEqual(tiers, [])
        self.assertIsNone(stage)

    def test_empty_ladder_fields_rejected(self) -> None:
        # 湖北账号实测 c04/f03：档位字段全空，仅 totalPq 有值（月电量，非阶梯）
        from electricitydaily.models import parse_ladder

        plain = {"code": 1, "data": {"list": [{"electricParticulars": {
            "oneLevelPq": "", "twoLevelPq": "0", "totalYearPq": "",
            "totalPq": "67", "levelFlag": "0",
        }}]}}
        tiers, stage = parse_ladder(plain)
        self.assertEqual(tiers, [])
        self.assertIsNone(stage)

    def test_real_ladder_parsed(self) -> None:
        from electricitydaily.models import parse_ladder

        plain = {"code": 1, "data": {"list": [{"electricParticulars": {
            "oneLevelPq": "2160", "twoLevelPq": "2880",
            "firstBalPq": "1452", "secondBalPq": "2880",
            "totalYearPq": "708", "levelFlag": "1",
        }}]}}
        tiers, stage = parse_ladder(plain)
        self.assertTrue(tiers)
        self.assertEqual(tiers[0].quota, 2160.0)
        self.assertEqual(tiers[0].remaining, 1452.0)
        self.assertEqual(stage, 1)

    def test_app_client_ladder_returns_empty(self) -> None:
        # App 通道明确不提供阶梯，返回空驱动网页兜底
        from electricitydaily.app_client import AppChannel

        ch = AppChannel.__new__(AppChannel)
        ch.log = mock.MagicMock()
        self.assertEqual(ch.fetch_ladder({}, "2026-11"), {})


class LadderCacheTest(unittest.TestCase):
    """阶梯缓存：命中直接用、过期/换月失效、保存可复原。"""

    def make_plugin(self) -> Any:
        p = electricitydaily.ElectricityDaily()
        p.update_config = lambda c, plugin_id=None: True  # type: ignore[method-assign]
        p.save_data = lambda k, v, plugin_id=None: None  # type: ignore[method-assign]
        p.get_data = lambda k=None, plugin_id=None: None  # type: ignore[method-assign]
        p.post_message = lambda **kw: None  # type: ignore[method-assign]
        return p

    def test_cache_roundtrip(self) -> None:
        from electricitydaily.models import Tier

        p = self.make_plugin()
        store: dict[str, Any] = {}
        p.get_data = lambda k=None, plugin_id=None: store.get(k)  # type: ignore[method-assign]
        p.save_data = lambda k, v, plugin_id=None: store.__setitem__(k, v)  # type: ignore[method-assign]
        p._save_ladder_cache("2026-11", {"123": ([Tier(1, 2160, 708, 1452)], 1)})
        got = p._cached_ladder("123", "2026-11", p._load_ladder_cache())
        self.assertIsNotNone(got)
        self.assertEqual(got[0][0].remaining, 1452.0)
        self.assertEqual(got[1], 1)

    def test_cache_month_mismatch_invalid(self) -> None:
        from electricitydaily.models import Tier

        p = self.make_plugin()
        store: dict[str, Any] = {}
        p.get_data = lambda k=None, plugin_id=None: store.get(k)  # type: ignore[method-assign]
        p.save_data = lambda k, v, plugin_id=None: store.__setitem__(k, v)  # type: ignore[method-assign]
        p._save_ladder_cache("2026-10", {"123": ([Tier(1, 2160, 708, 1452)], 1)})
        self.assertIsNone(p._cached_ladder("123", "2026-11", p._load_ladder_cache()))

    def test_cache_expired_invalid(self) -> None:
        from electricitydaily.models import Tier

        p = self.make_plugin()
        store: dict[str, Any] = {}
        p.get_data = lambda k=None, plugin_id=None: store.get(k)  # type: ignore[method-assign]
        p.save_data = lambda k, v, plugin_id=None: store.__setitem__(k, v)  # type: ignore[method-assign]
        p._save_ladder_cache("2026-11", {"123": ([Tier(1, 2160, 708, 1452)], 1)})
        store["ladder_cache"]["saved_at"] -= 8 * 86400
        self.assertIsNone(p._cached_ladder("123", "2026-11", p._load_ladder_cache()))


if __name__ == "__main__":
    unittest.main()
