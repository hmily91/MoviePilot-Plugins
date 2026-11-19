"""每日通知的消息体组装。纯字符串生成，不接触推送渠道。"""
from __future__ import annotations

from typing import Iterable

from .client import ElectricityError, LoginError
from .models import Report


def _num(value: float | None, unit: str = "") -> str:
    if value is None:
        return "暂无数据"
    return f"{value:.2f}{unit}"


def build_title(reports: Iterable[Report]) -> str:
    items = list(reports)
    if not items:
        return "电费日报 | 暂无数据"
    if len(items) == 1:
        cons = items[0].cons_no
        return f"电费日报 | 户号 {cons[-4:] if cons else '未知'} | {items[0].fetched_at[:10]}"
    return f"电费日报 | {len(items)} 个户号 | {items[0].fetched_at[:10]}"


def build_text(report: Report) -> str:
    lines: list[str] = []
    label = report.cons_no[-4:] if report.cons_no else "未知"
    lines.append(f"### 户号 {label}")
    lines.append("")

    lines.append("**用电**")
    daily = report.daily
    if daily.date and daily.total is not None:
        lines.append(f"- 数据日期：{daily.date}")
        lines.append(f"- 日用电量：{_num(daily.total, ' kWh')}")
        parts = []
        for name, value in (("峰", daily.peak), ("平", daily.flat), ("谷", daily.valley), ("尖", daily.sharp)):
            if value is not None:
                parts.append(f"{name} {value:.2f}")
        if parts:
            lines.append("- 分时：" + " / ".join(parts) + " kWh")
    else:
        lines.append("- 日用电量：暂无数据")
    lines.append("")

    lines.append("**账户**")
    lines.append(f"- 账户余额：{_num(report.balance, ' 元')}")
    lines.append(f"- 本月已产生电费：{_num(report.month_fee, ' 元')}")
    if report.history_owe is not None and report.history_owe > 0:
        lines.append(f"- 历史欠费：{_num(report.history_owe, ' 元')}")
    lines.append("")

    lines.append("**阶梯电量剩余**")
    if report.tiers:
        for tier in report.tiers:
            if tier.remaining is not None:
                remain = f"{tier.remaining:.2f} kWh"
            else:
                remain = "暂无数据"
            detail = []
            if tier.used is not None:
                detail.append(f"已用 {tier.used:.2f}")
            if tier.quota is not None:
                detail.append(f"额度 {tier.quota:.2f}")
            suffix = f"（{'，'.join(detail)} kWh）" if detail else ""
            lines.append(f"- {tier.label}：剩余 {remain}{suffix}")
        if report.stage is not None:
            lines.append(f"- 当前档位：第 {report.stage} 档")
    else:
        lines.append("- 暂无数据")
    lines.append("")

    if report.missing:
        lines.append("> 缺失字段：" + "、".join(report.missing))
    return "\n".join(lines).rstrip()


def build_combined_text(reports: Iterable[Report]) -> str:
    items = list(reports)
    header = f"数据抓取时间：{items[0].fetched_at if items else '暂无'}"
    return header + "\n\n" + "\n\n---\n\n".join(build_text(r) for r in items)


def build_error_text(plugin_name: str, error: BaseException, include_detail: bool = True) -> str:
    """失败通知：只写分类与可安全展示的原因，不写 URL、令牌或密码。"""
    reason = ""
    if isinstance(error, LoginError):
        reason = error.reason
        hints = {
            "invalid_auth": "请检查账号密码；国网密码修改后需重新填写。",
            "rate_limited": "当天登录已被限流，插件会冷却到 24 点后自动恢复。",
            "captcha_required": "服务端要求人工验证，本次未登录，请稍后重试。",
            "cannot_connect": "无法连接 95598，请检查网络与代理。",
        }
        hint = hints.get(reason, "请稍后重试；如持续失败请查看插件日志。")
    else:
        reason = type(error).__name__
        hint = "请查看插件日志定位；不会在通知中打印凭据。"
    lines = [
        f"插件：{plugin_name}",
        f"结果：执行失败（{reason}）",
        hint,
    ]
    if include_detail and isinstance(error, ElectricityError):
        detail = str(error)
        if detail and detail != f"登录失败（{reason}）":
            lines.append(f"说明：{detail}")
    return "\n".join(lines)
