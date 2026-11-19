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
        fields = {}
        for card in form:
            for item in card.get("content", []):
                props = item.get("props", {})
                if props.get("model"):
                    fields[props["model"]] = (item.get("component"), props)
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
        original = electricitydaily.StateGridClient

        class FailingClient:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            def login(self) -> None:
                raise LoginError("invalid_auth")

            def close(self) -> None:
                pass

        electricitydaily.StateGridClient = FailingClient  # type: ignore[assignment]
        try:
            plugin.refresh()
        finally:
            electricitydaily.StateGridClient = original  # type: ignore[assignment]
        self.assertEqual(len(self.messages), 1)
        self.assertIn("invalid_auth", self.messages[0]["text"])
        self.assertNotIn("TopSecret!", str(self.messages))
        self.assertIn("登录失败", plugin._last_status)

    def test_partial_api_failure_keeps_other_fields(self) -> None:
        plugin = self.make_plugin()
        plugin.init_plugin({"enabled": True, "username": "13800000000",
                            "password": "pw", "notify_on_error": True})
        original = electricitydaily.StateGridClient

        class PartialClient:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def login(self) -> None:
                pass

            def fetch_doors(self) -> list[dict[str, Any]]:
                return [{"consNo_dst": "123456789"}]

            def fetch_daily(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return daily_payload()

            def fetch_balance(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {}

            def fetch_ladder(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {}

            def close(self) -> None:
                pass

        electricitydaily.StateGridClient = PartialClient  # type: ignore[assignment]
        try:
            plugin.refresh()
        finally:
            electricitydaily.StateGridClient = original  # type: ignore[assignment]
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

    def test_secure_roundtrip(self) -> None:
        secret = hashlib.md5(b"pw").hexdigest()
        sealed = secure.seal(secret)
        self.assertNotEqual(sealed, secret)
        self.assertEqual(secure.unseal(sealed), secret)


if __name__ == "__main__":
    unittest.main()
