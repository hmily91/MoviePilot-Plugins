"""把 95598 原始回包整理成通知所需的统一结构。

解析原则：宁可标记“暂无数据”，也不编造数值。字段名在各地/各版本间不完全一致，
这里用别名集合做容错，并保留“已用 / 额度 / 剩余”三者的互相推算。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Iterable

from .client import DataError, OK_CODES

# ── 通用工具 ──────────────────────────────────────────────────────────────


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def api_ok(plain: Any) -> bool:
    if not isinstance(plain, dict):
        return False
    return str(plain.get("code") or "") in OK_CODES


def find_value(obj: Any, names: Iterable[str], max_depth: int = 8) -> Any:
    """在嵌套字典/列表里找第一个非空命中值。"""
    targets = set(names)

    def walk(node: Any, depth: int) -> Any:
        if depth > max_depth:
            return None
        if isinstance(node, dict):
            for key in targets:
                if key in node and node[key] not in (None, "", [], {}):
                    return node[key]
            for value in node.values():
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(obj, 0)


def find_dicts(obj: Any, keys: Iterable[str], max_depth: int = 8) -> list[dict[str, Any]]:
    """找出所有含有给定字段之一的字典，按命中字段数量从多到少返回。"""
    targets = set(keys)
    found: list[tuple[int, dict[str, Any]]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, dict):
            score = sum(1 for key in targets if key in node)
            if score:
                found.append((score, node))
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(obj, 0)
    found.sort(key=lambda item: -item[0])
    return [item[1] for item in found]


# ── 数据结构 ──────────────────────────────────────────────────────────────


@dataclass
class Tier:
    """一个阶梯档位；任一字段都可能为 None，表示服务端没有给。"""

    index: int
    quota: float | None = None
    used: float | None = None
    remaining: float | None = None

    @property
    def label(self) -> str:
        return {1: "第一档", 2: "第二档", 3: "第三档"}.get(self.index, f"第{self.index}档")

    def normalized(self) -> "Tier":
        if self.remaining is None and self.quota is not None and self.used is not None:
            self.remaining = round(max(0.0, self.quota - self.used), 2)
        return self


@dataclass
class DailyUsage:
    date: str = ""
    total: float | None = None
    peak: float | None = None
    flat: float | None = None
    valley: float | None = None
    sharp: float | None = None


@dataclass
class Report:
    cons_no: str
    daily: DailyUsage = field(default_factory=DailyUsage)
    balance: float | None = None
    month_fee: float | None = None
    history_owe: float | None = None
    tiers: list[Tier] = field(default_factory=list)
    stage: int | None = None
    data_date: str = ""
    missing: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def add_missing(self, name: str) -> None:
        if name not in self.missing:
            self.missing.append(name)


# ── 解析函数 ──────────────────────────────────────────────────────────────


def parse_daily(plain: dict[str, Any], today: datetime.date | None = None) -> DailyUsage:
    """从 c24/f01 里取最近一个已出数日（不取未来日期）。"""
    today = today or datetime.date.today()
    biz = (plain.get("data") or {}).get("bizrt") or plain.get("data") or plain
    rows = None
    for node in (biz, plain):
        if isinstance(node, dict):
            for key in ("sevenEleList", "daily_bill_list", "list"):
                value = node.get(key)
                if isinstance(value, list):
                    rows = value
                    break
        if rows:
            break
    if not rows:
        return DailyUsage()

    parsed: list[tuple[str, DailyUsage]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("day") or row.get("date") or row.get("dataDate") or "").strip()
        digits = day.replace("-", "").replace("/", "")
        if len(digits) != 8 or not digits.isdigit():
            continue
        try:
            day_date = datetime.datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            continue
        if day_date > today:
            continue
        # 服务端会把"今天"也返回一行，但当天还没出账、dayElePq 为空。
        # 若不过滤，排序取最新时会正好取到这条空记录，日报显示"暂无数据"。
        # 跳过没有日用电量的行；真实 0 用电会解析成 0.0（不是 None），不受影响。
        total = to_float(row.get("dayElePq") or row.get("dayPq"))
        if total is None:
            continue
        parsed.append((digits, DailyUsage(
            date=f"{digits[:4]}-{digits[4:6]}-{digits[6:]}",
            total=total,
            peak=to_float(row.get("thisPPq") or row.get("peakPq")),
            flat=to_float(row.get("thisNPq") or row.get("flatPq") or row.get("thisFPq")),
            valley=to_float(row.get("thisVPq") or row.get("valleyPq")),
            sharp=to_float(row.get("thisTPq") or row.get("sharpPq")),
        )))
    if not parsed:
        return DailyUsage()
    parsed.sort(key=lambda item: item[0], reverse=True)
    return parsed[0][1]


def parse_balance(plain: dict[str, Any]) -> dict[str, float | None]:
    """从 c05/f01 里取余额、本月电费、历史欠费。"""
    biz = (plain.get("data") or {}).get("bizrt") or plain.get("data") or plain
    node = find_dicts(biz, ("account_balance", "prepayBal", "sumMoney", "estiAmt", "historyOwe"))
    account = None
    for candidate in node:
        if isinstance(candidate.get("account_balance"), dict):
            account = candidate["account_balance"]
            break
        if any(k in candidate for k in ("prepayBal", "sumMoney", "estiAmt", "historyOwe")):
            account = candidate
            break
    account = account or {}
    return {
        "balance": to_float(account.get("sumMoney") if account.get("sumMoney") not in (None, "") else account.get("prepayBal")),
        "month_fee": to_float(account.get("estiAmt")),
        "history_owe": to_float(account.get("historyOwe")),
    }


_TIER_KEYS = set()
for _idx in (1, 2, 3, 4, 5):
    _TIER_KEYS.update({
        f"levl{_idx}Pq", f"level{_idx}Pq", f"firstRefPq" if _idx == 1 else f"ref{_idx}Pq",
        f"levl{_idx}PqSum", f"levl{_idx}PqRemain", f"levl{_idx}PqRemainPq",
        f"step{_idx}Used", f"step{_idx}Remain", f"tier{_idx}Used", f"tier{_idx}Remain",
        f"levlThrePqSum" if _idx == 3 else f"",
    })
_TIER_KEYS.discard("")
_TIER_KEYS.update({
    "levelStdCode", "stepStage", "acuPq", "levlBgnYm", "levlEndYm",
    # 湖北等省 c04/f03 的真实字段（实测 electricParticulars 节点）
    "oneLevelPq", "twoLevelPq", "threeLevelPq",
    "firstBalPq", "secondBalPq", "thirdBalPq",
    "levelFlag", "totalYearPq", "totalPq",
})


def _first_float(node: dict[str, Any], names: Iterable[str]) -> float | None:
    for name in names:
        if name in node:
            value = to_float(node.get(name))
            if value is not None:
                return value
    return None


def parse_ladder(plain: dict[str, Any]) -> tuple[list[Tier], int | None]:
    """从 c04/f03 解析各档额度/已用/剩余，并给出当前档位。"""
    candidates = find_dicts(plain, _TIER_KEYS)
    if not candidates:
        return [], None
    node = candidates[0]
    tiers: list[Tier] = []
    alias = {
        1: (
            ("levlOnePq", "levelOnePq", "firstRefPq", "levl1Pq", "tier1Quota", "step1Quota", "oneLevelPq"),
            ("levlOnePqSum", "levl1PqSum", "step1Used", "tier1Used", "firstUsedPq"),
            ("levlOnePqRemain", "levlOnePqRemainPq", "levl1PqRemain", "step1Remain", "tier1Remain", "firstRemainPq", "firstBalPq"),
        ),
        2: (
            ("levlTwoPq", "levelTwoPq", "secondRefPq", "levl2Pq", "tier2Quota", "step2Quota", "twoLevelPq"),
            ("levlTwoPqSum", "levl2PqSum", "step2Used", "tier2Used", "secondUsedPq"),
            ("levlTwoPqRemain", "levlTwoPqRemainPq", "levl2PqRemain", "step2Remain", "tier2Remain", "secondRemainPq", "secondBalPq"),
        ),
        3: (
            ("levlThrePq", "levelThreePq", "thirdRefPq", "levl3Pq", "tier3Quota", "step3Quota", "threeLevelPq"),
            ("levlThrePqSum", "levl3PqSum", "step3Used", "tier3Used", "thirdUsedPq"),
            ("levlThrePqRemain", "levlThrePqRemainPq", "levl3PqRemain", "step3Remain", "tier3Remain", "thirdRemainPq", "thirdBalPq"),
        ),
    }
    for idx in (1, 2, 3):
        quota_names, used_names, remain_names = alias[idx]
        quota = _first_float(node, quota_names)
        used = _first_float(node, used_names)
        remain = _first_float(node, remain_names)
        if quota is None and used is None and remain is None:
            continue
        tiers.append(Tier(idx, quota, used, remain).normalized())

    # 真实回包里额度是"各档区间电量"(oneLevelPq/twoLevelPq)、剩余是 firstBalPq/
    # secondBalPq，年度累计已用 totalYearPq。据此补全各档 used 与第三档余量。
    year_used = _first_float(node, ("totalYearPq", "acuPq"))
    if year_used is not None:
        cum = 0.0
        for tier in tiers:
            prev = cum
            if tier.quota is not None:
                cum += tier.quota
            tier.used = round(max(0.0, min(year_used, cum) - prev), 2) if tier.quota is not None else tier.used
            if tier.remaining is None and tier.quota is not None:
                tier.remaining = round(max(0.0, cum - year_used) if tier.index == len(tiers) else max(0.0, cum - year_used), 2)
        # 第三档（超额）额度/剩余：用年度累计与前两档额度推算
        if len(tiers) >= 2 and tiers[0].quota is not None and tiers[1].quota is not None:
            first_two = tiers[0].quota + tiers[1].quota
            third_remaining = round(max(0.0, first_two - year_used), 2)
            if len(tiers) == 2:
                tiers.append(Tier(3, None, round(max(0.0, year_used - first_two), 2), third_remaining))
            elif tiers[2].quota is None:
                tiers[2].quota = None
                tiers[2].used = round(max(0.0, year_used - first_two), 2)
                tiers[2].remaining = third_remaining

    stage = to_float(find_value(node, ("levelStdCode", "stepStage", "currentLevel", "levelFlag")))
    stage_int = int(stage) if stage is not None else None
    if stage_int is None and tiers:
        # 服务端没给档位码时按额度推算：已用超过前档额度就进入下一档
        stage_int = 1
        for tier in tiers:
            if tier.used is not None and tier.quota is not None and tier.used >= tier.quota:
                stage_int = max(stage_int, tier.index + 1)
    return tiers, stage_int


def build_report(
    cons_no: str,
    daily_plain: dict[str, Any] | None,
    balance_plain: dict[str, Any] | None,
    ladder_plain: dict[str, Any] | None,
    today: datetime.date | None = None,
) -> Report:
    """合并三路数据；任何一路失败都保留其它数据并写入 missing。"""
    report = Report(cons_no=cons_no)
    today = today or datetime.date.today()
    fetched = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report.fetched_at = fetched

    daily = parse_daily(daily_plain or {}, today)
    if daily.date and daily.total is not None:
        report.daily = daily
        report.data_date = daily.date
    else:
        report.add_missing("每日用电量")

    balance_data = parse_balance(balance_plain or {}) if balance_plain else {}
    report.balance = balance_data.get("balance")
    report.month_fee = balance_data.get("month_fee")
    report.history_owe = balance_data.get("history_owe")
    if report.balance is None:
        report.add_missing("账户余额")

    if ladder_plain:
        tiers, stage = parse_ladder(ladder_plain)
        report.tiers = tiers
        report.stage = stage
    if not report.tiers:
        report.add_missing("阶梯电量")
    return report
