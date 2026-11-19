"""
订阅自动保种助手 - 全集判定引擎（纯 Python，不依赖 MoviePilot 宿主）。

只回答一个问题：这条站点种子，是不是「与订阅同一季、目标分辨率、完整一季打包，
且用户愿意要的那一份」。顺序不可调换，任一层不过即跳过。

判定链：
    1. 站点过滤
    1b. 排除规则（用户 / 订阅自带，命中任意一条即跳过）
    2. 宿主 MetaInfo 季集闸门（S01 = 完结季；S01 E40 = 分集）
    3. 多季 / 跨季排除
    4. 单集 / 分段包闸门（标题 + 副标题）
    5. 半季 / 分卷排除
    6. 季号解析并与订阅季号严格比对
    7. 分辨率筛选（全部 / 4k / 1080p / 720p）
    8. 全集证据（宿主识别为完结季，或「全N集 / 完结 / Complete / 全集」文本）
    9. 包含规则（用户 / 订阅自带，配置后必须命中任意一条）

设计原则：
    - 宁可漏判，不可误判。插件会删订阅、种子和文件，把整包误判成分集只是
      「本次不清理」，把分集或跨季误判成整包则会造成不可逆的误删。
    - 用户规则只能「收紧」，绝不能放宽内置安全闸门。所以排除放在最前、
      包含放在最后：即使只填一条 `HDR`，分集 / 跨季 / 错分辨率依然会被拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 分辨率：与 MP 原生订阅的下拉完全一致，四档单选
# ---------------------------------------------------------------------------

RESOLUTION_CHOICES: tuple[tuple[str, str], ...] = (
    ("any", "全部"),
    ("4k", "4k"),
    ("1080p", "1080p"),
    ("720p", "720p"),
)

_RESOLUTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "2160p": re.compile(r"(?<![0-9])(?:2160[pi]|4K|UHD|Ultra\s*HD)(?![0-9])", re.I),
    "1080p": re.compile(r"(?<![0-9])1080[pi](?![0-9])|(?<![A-Za-z])FHD(?![A-Za-z])", re.I),
    "720p": re.compile(r"(?<![0-9])720[pi](?![0-9])", re.I),
}

_RESOLUTION_TARGETS: dict[str, set[str]] = {
    "any": set(),
    "4k": {"2160p"},
    "1080p": {"1080p"},
    "720p": {"720p"},
}


def resolution_target(choice: str) -> set[str]:
    """把配置页档位转成内部标准分辨率集合；空集合表示「全部」，不限制。"""
    return set(_RESOLUTION_TARGETS.get(str(choice or "any").lower(), set()))


def detect_resolutions(text: str) -> list[str]:
    """从标题 / 副标题 / 宿主识别结果中提取标准分辨率。"""
    return [key for key, pattern in _RESOLUTION_PATTERNS.items() if pattern.search(text or "")]


# ---------------------------------------------------------------------------
# 内置判定正则
# ---------------------------------------------------------------------------

#: 完整性关键词。命中后从文本中剔除，避免「全40集」被单集正则误杀。
RE_COMPLETENESS = re.compile(
    r"Complete|全集|全\s*\d+\s*集|全\s*\d+\s*话|整季|全季|完结|合集|整套"
    r"|Pack|Box\s?Set|Full\s+Season|Season\s+Pack",
    re.I,
)

#: 集范围写法在 PT 站上通常是「分段打包」，不是完整一季。
#: 真实样例：S01E01-E06 / S01 E36-E40 / 副标题「第27-30集」。
RE_EPISODE_RANGE = re.compile(
    r"(?:S\s*\d{1,3}\s*)?E\s*\d{1,3}\s*[-~–—至]\s*(?:S\s*\d{1,3}\s*)?E?\s*\d{1,3}"
    r"|第\s*\d{1,3}\s*[-~–—至]\s*\d{1,3}\s*[集话]"
    r"|(?<![全\d])\d{1,3}\s*[-~–—至]\s*\d{1,3}\s*[集话]",
    re.I,
)

#: 单集标识族：S01E01 / E01 / EP01 / 1x01 / Episode 01 / 第01集 / 01集
RE_EPISODE_MARKER = re.compile(
    r"S\s*\d{1,3}\s*E\s*\d{1,3}"
    r"|(?<![A-Za-z0-9])EP?\s*[-_]?\s*\d{1,3}(?![A-Za-z0-9])"
    r"|(?<![0-9])\d{1,2}\s*[xX]\s*\d{2,3}(?![0-9])"
    r"|Episode\s*\d{1,3}"
    r"|第\s*\d{1,3}\s*[集话]"
    r"|(?<![全\d])\d{1,3}\s*[集话](?!\s*装)",
    re.I,
)

#: 多季 / 跨季写法
RE_MULTI_SEASON_EXPLICIT = re.compile(
    r"S\s*\d{1,3}\s*[-~–—至]\s*S?\s*\d{1,3}"
    r"|Season\s*\d{1,3}\s*[-~–—至]\s*(?:Season\s*)?\d{1,3}"
    r"|全\s*\d+\s*季"
    r"|第?\s*\d+\s*[-~–—至]\s*\d+\s*季"
    r"|\d+\s*[-~–—至]\s*\d+\s*Season",
    re.I,
)

#: 半季 / 分卷 / 不完整资源
RE_PARTIAL_PACK = re.compile(
    r"Part\s*\d+|PART\s*[A-Z]|Vol\.?\s*\d+|Disc\s*\d+|CD\s*\d+"
    r"|前\s*\d+\s*[集话]|上\s*半|下\s*半|分卷|第\s*[一二三四五六七八九十\d]+\s*部分",
    re.I,
)

#: 季号 token：S01 / Season 1 / 第1季 / 第一季
RE_SEASON_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])S\s*(\d{1,3})(?![0-9])"
    r"|Season\s*(\d{1,3})"
    r"|第\s*([0-9一二三四五六七八九十]+)\s*季",
    re.I,
)

_CN_DIGITS: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 真实 PT 站标题回归样例：(标题, 副标题, 订阅季号, 是否应判定为同季整包命中)
SAMPLE_CASES: tuple[tuple[str, str, int, bool], ...] = (
    # ---- 正例：整季打包 ----
    ("The Long Watch S01 2026 2160p WEB-DL DDP5.1 H265-Pure@HDSWEB [热门]",
     "交锋 全40集 | 主演: 王凯 周依然 欧豪 【去头尾广告纯净版】", 1, True),
    ("Reacher S04 2026 2160p WEB-DL DDP5.1 Atmos H265 HDR DV-Pure@HDSWEB",
     "美剧: 侠探杰克第4季 全08集 | 主演: 阿兰·里奇森", 4, True),
    ("Let s Go S01 2026 2160p TX WEB-DL H265 HDR 60FPS DDP2.0-ADWeb",
     "走马上任 第一季 全40集 | 类型: 剧情 喜剧", 1, True),
    ("Let s Go S01 2026 1080p TX WEB-DL H265 AAC-ADWeb",
     "走马上任 第一季 全40集", 1, True),
    ("Da Xia Qi Wen Gui An Lu S01 2026 2160p WEB-DL AAC H265-HDSWEB",
     "短剧: 大夏奇闻诡案录 全12集", 1, True),
    ("Da Xia Qi Wen Gui An Lu S01 2026 1080p WEB-DL AAC H264-HDSWEB",
     "短剧: 大夏奇闻诡案录 全12集", 1, True),
    ("The Silent Storm S01 2026 2160p WEB-DL H265 HDR 60fps AAC-CHDWEB",
     "谷雨/The Silent Storm 全30集 | 4K/HDR/60帧", 1, True),
    ("The Silent Storm S01 2026 1080p WEB-DL H264 AAC-CHDWEB",
     "谷雨/The Silent Storm 全30集", 1, True),
    ("VIVANT S02 1080p HamiVideo WEB-DL AAC2.0 H.264-CHDWEB",
     "日剧: 别班 第二季 VIVANT 2 全14集", 2, True),
    ("My Mister S01 2018 2160p V2 Tving WEB-DL H265 AAC-HHWEB",
     "我的大叔 全16集 | 4K 60帧 | 类型:剧情", 1, True),
    ("Wo De De Hun Yin Xu Yao Chai Qian Le 2026 S01 720p WEB-DL H.265 AAC-GodDramas",
     "我的婚姻需要拆迁了 | 全60集 | 2026年 | 网络收费短剧", 1, True),
    ("Xun Di Si Shi Nian Zhang Lu You Gui Ren 2026 S01 720p WEB-DL H.265 AAC-GodDramas",
     "寻弟四十年，长路有归人 | 全68集 | 2026年", 1, True),
    ("The Law of Beasts AKA Prawo bestii S01 2026 2160p NF WEB-DL DDP 5.1 Atmos H.265-CHDWEB",
     "罪兽之法 / The Law of Beasts *全4集*", 1, True),
    ("Shou Zhu Mei Yi Cheng Hui Jia Lu 2026 S01 720p WEB-DL H.265 AAC-GodDramas",
     "守住每一程回家路 | 全86集", 1, True),
    ("Gordon S01 2026 2160p NF WEB-DL DDP 5.1 Atmos DV H.265-CHDWEB",
     "戈登档案 *全7集*", 1, True),
    ("Lets Go S01 Complete 2026 2160p TX WEB-DL H.265 HDR HFR DDP2.0-HHWEB",
     "走马上任 | 全40集 | 4K HDR", 1, True),
    # ---- 反例：单集 / 分段包 / 部分集 / 跨季 / 无季号 ----
    ("Let s Go S01E40 2026 2160p TX WEB-DL H265 HDR 60FPS DDP2.0-ADWeb",
     "走马上任 第40集", 1, False),
    ("When I Meet the Moon S01E01-E06 2026 2160p WEB-DL AAC H265-HDSWEB",
     "折月亮 第01-06集 | 主演: 林一 卢昱晓", 1, False),
    ("Blossom in Darkness S01E11 2026 1080p WEB-DL AAC H264-HDSWEB",
     "十万狂花入梦来 / 万花灼灼 第11集", 1, False),
    ("CCTV9 The Dazzling APEC Journey Ep01-Ep08 HDTV 1080i H264-HDSTV",
     "央视纪录 闪亮的APEC之旅 8集", 1, False),
    ("The Silent Storm S01 2026 2160p WEB-DL H265 HDR 60fps AAC-CHDWEB",
     "谷雨/The Silent Storm 第27-30集 | 4K/HDR/60帧", 1, False),
    ("The Laid-Off Cheat-Granting Mage Enjoys a Second Lease on Life S01 1080p CR WEB-DL AAC",
     "2026年10月新番 第一季 第7集", 1, False),
    ("Summer Break S01 1080p friDay WEB-DL AAC2.0 H.264-CHDWEB",
     "韩剧: 暑假 第8集 | 类型:剧情", 1, False),
    ("Still A Family S01 2026 2160p TX WEB-DL H.265 DDP5.1-HHWEB",
     "多喜一家人 | 第05-06集 | 4K | 类型: 家庭", 1, False),
    ("Crash S02E02 1080p friDay WEB-DL AAC2.0 H264-MWeb",
     "碰撞搜查线 第二季 第2集", 2, False),
    ("Lets Go S01 E36-E40 2026 2160p TX WEB-DL H.265 HDR HFR DDP2.0-HHWEB",
     "走马上任 | 第36-40集 | 4K HDR", 1, False),
    ("The Silent Storm S01 2026 2160p YK WEB-DL H.265 HDR HFR AAC2.0-CHDWEB",
     "谷雨 | 第27-30集 | 4K HDR | 类型: 剧情/悬疑/犯罪", 1, False),
    ("Madden 2026 2160p AMZN WEB-DL DDP 5.1 Atmos DV HDR10+ H.265-FLUX",
     "马登 / 麦登 / 约翰·麦登传 | 类型: 传记 / 运动", 1, False),
    ("Some Show S01-S03 2026 2160p Complete BluRay-GROUP",
     "全3季 全集", 1, False),
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class TorrentSnapshot:
    """归一化后的站点种子，供判定链使用。"""

    title: str = ""
    subtitle: str = ""
    site_id: Optional[int] = None
    site_name: str = ""
    page_url: str = ""
    enclosure: str = ""
    host_seasons: list[int] = field(default_factory=list)
    host_episodes: list[int] = field(default_factory=list)
    host_begin_episode: Optional[int] = None
    host_part: str = ""
    host_season_episode: str = ""
    host_resolution: str = ""
    host_has_metadata: bool = False
    labels: str = ""

    @property
    def any_text(self) -> str:
        """标题 + 副标题 + 宿主识别的分辨率，用于完整性证据与分辨率检测。"""
        return " ".join(x for x in (self.title, self.subtitle, self.host_resolution) if x)

    @property
    def rule_text(self) -> str:
        """用户包含/排除规则的匹配范围：标题 + 副标题 + 站点标签。"""
        return " ".join(x for x in (self.title, self.subtitle, self.labels) if x)


@dataclass
class Verdict:
    """一条种子的判定结论，含逐层日志。"""

    passed: bool = False
    stage: str = ""
    reason: str = ""
    seasons: list[int] = field(default_factory=list)
    resolutions: list[str] = field(default_factory=list)
    is_full_pack: bool = False
    matched_exclude: str = ""
    matched_include: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add_check(self, stage: str, ok: bool, detail: str) -> None:
        self.checks.append((stage, ok, detail))

    def fail(self, stage: str, reason: str) -> "Verdict":
        self.passed = False
        self.stage = stage
        self.reason = reason
        self.add_check(stage, False, reason)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "stage": self.stage,
            "reason": self.reason,
            "seasons": self.seasons,
            "resolutions": self.resolutions,
            "is_full_pack": self.is_full_pack,
            "matched_exclude": self.matched_exclude,
            "matched_include": self.matched_include,
            "checks": [{"stage": s, "ok": o, "detail": d} for s, o, d in self.checks],
        }


# ---------------------------------------------------------------------------
# 判定辅助
# ---------------------------------------------------------------------------


def _cn_number_to_int(raw: str) -> Optional[int]:
    """把「一 / 十 / 十二」等中文数字转成 int。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(raw)


def extract_seasons(text: str) -> list[int]:
    """提取 S01 / Season 1 / 第1季 / 第一季 形式的所有季号。"""
    found: list[int] = []
    for match in RE_SEASON_TOKEN.finditer(text or ""):
        value = next((g for g in match.groups() if g is not None), None)
        season = _cn_number_to_int(value or "")
        if season is not None and season not in found:
            found.append(season)
    return found


def has_multi_season(text: str) -> bool:
    return bool(RE_MULTI_SEASON_EXPLICIT.search(text or ""))


def has_completeness_evidence(text: str) -> bool:
    return bool(RE_COMPLETENESS.search(text or ""))


def episode_gate(text: str, extra_text: str = "") -> tuple[bool, str]:
    """
    单集 / 部分集闸门。标题和副标题都要检查：中文 PT 站常把「第27-30集」
    写在副标题，标题本身看起来和整包一模一样。
    """
    combined = f"{text or ''} {extra_text or ''}"
    cleaned = RE_COMPLETENESS.sub(" ", combined)
    if RE_EPISODE_RANGE.search(cleaned):
        return True, "命中集范围写法（E01-E06 / 第27-30集），属于分段打包而非完整一季"
    if RE_EPISODE_MARKER.search(cleaned):
        return True, "命中单集标识（S01E01 / E01 / 1x01 / 第N集 等）"
    return False, "标题与副标题均未发现单集 / 部分集标识"


def validate_cron(expression: str) -> tuple[bool, str]:
    """轻量校验 5 段 Cron 表达式，避免非法值传给 APScheduler。"""
    parts = (expression or "").strip().split()
    if len(parts) != 5:
        return False, f"Cron 表达式必须包含 5 段（分 时 日 月 周），当前为 {len(parts)} 段"
    for part in parts:
        if not re.fullmatch(r"[0-9*\?/,\-LW#]+", part, re.I):
            return False, f"Cron 段 {part!r} 含非法字符"
    return True, "合法"


# ---------------------------------------------------------------------------
# 包含 / 排除规则（对齐 MP 原生订阅的「关键字、正则式」）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """一条规则。普通文字即关键字子串匹配；写 `A|B` 或量词表达式即正则。"""

    raw: str
    pattern: re.Pattern[str]
    source: str = "plugin"  # plugin=插件全局；subscribe=订阅自带


def parse_rules(text: str, source: str = "plugin") -> tuple[list[Rule], list[str]]:
    """
    解析规则文本：每行一条，`#` 开头为注释，空行忽略，统一忽略大小写。
    普通文字按关键字子串匹配，写成 `A|B` 或量词表达式即正则。

    订阅自带的 include / exclude 是单个字符串（MP 约定用 `|` 组合正则），
    因此整行原样编译，不按 `|` 拆分，避免把 `HDR|DV` 拆坏。

    :return: (合法规则列表, 错误信息列表)。非法行会被跳过，绝不半编译上线。
    """
    rules: list[Rule] = []
    errors: list[str] = []
    for lineno, line in enumerate((text or "").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            rules.append(Rule(raw=raw, pattern=re.compile(raw, re.I), source=source))
        except re.error as error:
            errors.append(f"{source} 第 {lineno} 行 {raw!r}：{error}")
    return rules, errors


def match_rule(rules: list[Rule], text: str) -> Optional[Rule]:
    """返回第一条命中的规则；全部不命中返回 None。"""
    for rule in rules:
        try:
            if rule.pattern.search(text or ""):
                return rule
        except re.error:
            continue
    return None


def _to_int(value: Any) -> Optional[int]:
    """把宿主字段安全转成整数。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 主判定
# ---------------------------------------------------------------------------


def evaluate(
    *,
    subscribe_season: Optional[int],
    torrent: TorrentSnapshot,
    allowed_site_ids: Optional[set[int]] = None,
    resolution: str = "any",
    exclude_rules: Optional[list[Rule]] = None,
    include_rules: Optional[list[Rule]] = None,
) -> Verdict:
    """
    判定一条种子是否为「同季 + 目标分辨率 + 完整一季打包 + 用户要的那一份」。

    :param subscribe_season: MP 原生订阅的指定季号，必须与种子季号严格相等
    :param torrent: 归一化后的种子文本
    :param allowed_site_ids: 勾选的站点 ID；None 或空集合表示不限制
    :param resolution: any / 4k / 1080p / 720p
    :param exclude_rules: 排除规则，命中任意一条即跳过（最先判定）
    :param include_rules: 包含规则，配置后必须命中任意一条（最后判定）；空 = 不限制
    """
    verdict = Verdict()
    title = torrent.title or ""
    full_text = torrent.any_text
    season_text = f"{title} {torrent.subtitle or ''}"

    # 1. 站点过滤
    if allowed_site_ids:
        if torrent.site_id is None or int(torrent.site_id) not in allowed_site_ids:
            return verdict.fail("站点", f"站点 {torrent.site_name or torrent.site_id} 不在勾选范围内")
        verdict.add_check("站点", True, f"命中勾选站点：{torrent.site_name or torrent.site_id}")

    # 1b. 排除规则优先：命中任意一条即跳过，且优先于包含规则
    if exclude_rules:
        hit = match_rule(exclude_rules, torrent.rule_text)
        if hit:
            verdict.matched_exclude = hit.raw
            return verdict.fail(
                "排除规则",
                f"命中排除规则 [{hit.source}] {hit.raw!r}，按优先级直接跳过",
            )
        verdict.add_check("排除规则", True, f"{len(exclude_rules)} 条排除规则均未命中")
    else:
        verdict.add_check("排除规则", True, "未配置排除规则")

    # 2. 宿主季集优先：MP V3 把 S01 视为完结季、S01 E40 视为分集
    if torrent.host_has_metadata:
        if torrent.host_episodes or torrent.host_begin_episode is not None:
            return verdict.fail(
                "宿主季集",
                f"MP 识别为分集/部分集（{torrent.host_season_episode or '含集号'}），不是完结季",
            )
        if torrent.host_part:
            return verdict.fail("宿主季集", f"MP 识别为分段媒体（part={torrent.host_part}）")
        if len(torrent.host_seasons) > 1:
            return verdict.fail("多季排除", f"MP 识别出多季 {torrent.host_seasons}，禁止参与同季判定")
        label = torrent.host_season_episode or (
            f"S{torrent.host_seasons[0]:02d}" if torrent.host_seasons else "S??"
        )
        verdict.add_check("宿主季集", True, f"MP 识别为完结季 {label}，无集号")
    else:
        verdict.add_check("宿主季集", True, "宿主未给出季集解析，回退到标题正则判定")

    # 3. 多季 / 跨季排除（优先级最高，防止跨季误删）
    if has_multi_season(season_text):
        return verdict.fail("多季排除", "识别为多季 / 跨季合集，禁止参与同季判定")
    verdict.add_check("多季排除", True, "未发现多季 / 跨季特征")

    # 4. 单集 / 部分集闸门（标题 + 副标题）
    is_episode, gate_note = episode_gate(title, torrent.subtitle)
    if is_episode:
        return verdict.fail("单集闸门", gate_note)
    verdict.add_check("单集闸门", True, gate_note)

    # 5. 半季 / 分卷排除
    if RE_PARTIAL_PACK.search(season_text):
        return verdict.fail("完整性", "识别为半季 / 分卷 / 部分集数资源，不是完整一季")
    verdict.add_check("完整性", True, "未发现半季 / 分卷特征")

    # 6. 季号解析与订阅季号严格比对（宿主解析优先，文本正则兜底）
    seasons = list(torrent.host_seasons) if torrent.host_seasons else extract_seasons(season_text)
    verdict.seasons = seasons
    if not seasons:
        return verdict.fail("季集校验", "标题中无法解析出季号（S01 / Season 1 / 第1季）")
    if len(seasons) > 1:
        return verdict.fail("季集校验", f"标题含多个季号 {seasons}，疑似跨季合集")
    if subscribe_season is None:
        return verdict.fail("季集校验", "订阅本身没有季号，无法做同季校验")
    if seasons[0] != int(subscribe_season):
        return verdict.fail(
            "季集校验",
            f"种子季号 S{seasons[0]:02d} 与订阅季号 S{int(subscribe_season):02d} 不一致，跳过以防跨季误删",
        )
    verdict.add_check(
        "季集校验", True,
        f"同季校验通过：订阅 S{int(subscribe_season):02d} == 种子 S{seasons[0]:02d}",
    )

    # 7. 分辨率筛选（标题 / 副标题文本 + 宿主识别的 resource_pix）
    resolutions = detect_resolutions(full_text)
    verdict.resolutions = resolutions
    targets = resolution_target(resolution)
    if not targets:
        verdict.add_check("分辨率", True, f"全部（不限制），识别到 {','.join(resolutions) or '未标注'}")
    elif not resolutions:
        return verdict.fail("分辨率", "资源未标注分辨率，按保守策略跳过")
    elif not set(resolutions) & targets:
        return verdict.fail(
            "分辨率",
            f"资源分辨率 {','.join(resolutions)} 与目标 {','.join(sorted(targets))} 不符",
        )
    else:
        verdict.add_check("分辨率", True, f"命中目标分辨率：{','.join(resolutions)}")

    # 8. 全集证据：宿主识别为完结季，或文本带「全N集 / 完结 / Complete / 全集」
    # 没有包含正则之后，这一步是防止把「干净标题的分段包」当成整包的最后一道闸门。
    host_pack = torrent.host_has_metadata and bool(torrent.host_seasons) and not torrent.host_episodes
    text_pack = has_completeness_evidence(full_text)
    if not (host_pack or text_pack):
        return verdict.fail(
            "全集证据",
            "MP 未识别为完结季，且标题/副标题没有「全N集 / 完结 / Complete / 全集」等整包证据",
        )
    verdict.is_full_pack = True
    verdict.add_check(
        "全集证据", True,
        "MP 识别为完结季" if host_pack else "文本含整包关键词",
    )

    # 9. 包含规则：放在所有内置安全闸门之后，用户规则只能收紧、不能放宽
    include_note = ""
    if include_rules:
        hit = match_rule(include_rules, torrent.rule_text)
        if not hit:
            return verdict.fail(
                "包含规则",
                f"已配置 {len(include_rules)} 条包含规则但未命中任何一条，跳过该资源",
            )
        verdict.matched_include = hit.raw
        include_note = f"；命中包含规则 [{hit.source}] {hit.raw!r}"
        verdict.add_check("包含规则", True, f"命中 [{hit.source}] {hit.raw!r}")
    else:
        verdict.add_check("包含规则", True, "未配置包含规则，不额外限制")

    verdict.passed = True
    verdict.stage = "命中"
    verdict.reason = (
        f"同季整季打包 S{seasons[0]:02d}，分辨率 {','.join(resolutions) or '未标注'}，"
        f"目标档位 {resolution}{include_note}"
    )
    return verdict


# ---------------------------------------------------------------------------
# 宿主对象 -> 快照
# ---------------------------------------------------------------------------


def build_snapshot_from_torrent(torrent: Any, context: Any = None) -> TorrentSnapshot:
    """
    把宿主 ``TorrentInfo`` / ``Context`` 转成 TorrentSnapshot。

    MP V3 搜索结果已用 ``Context.meta_info`` 完成季集识别：``S01`` 表示完结季
    （无集号），``S01 E40`` 表示分集（有 begin_episode / episode_list）。
    优先采信这份公开 Schema 的解析结果，插件正则只作为宿主未识别时的兜底。

    全程使用 getattr，避免插件绑定宿主内部 ORM / Schema 的具体版本。
    """
    def g(name: str, default: Any = "") -> Any:
        return getattr(torrent, name, None) or default

    meta = getattr(context, "meta_info", None) if context is not None else None

    def mg(name: str, default: Any = None) -> Any:
        return getattr(meta, name, default) if meta is not None else default

    # 季号：只有宿主明确解析出 begin_season 才采信，避免 TV 默认 [1] 造成误判
    begin_season = _to_int(mg("begin_season"))
    end_season = _to_int(mg("end_season"))
    host_seasons: list[int] = []
    if begin_season is not None:
        last = end_season if end_season is not None and end_season >= begin_season else begin_season
        host_seasons = list(range(begin_season, min(last, begin_season + 30) + 1))

    # 集号：episode_list 优先，其次 begin_episode；非空即代表分集/部分集
    host_episodes: list[int] = []
    raw_episodes = mg("episode_list") or []
    if isinstance(raw_episodes, (list, tuple, set)):
        host_episodes = [v for v in (_to_int(x) for x in raw_episodes) if v is not None]
    begin_episode = _to_int(mg("begin_episode"))
    if not host_episodes and begin_episode is not None:
        host_episodes = [begin_episode]

    raw_labels = g("labels", []) or []
    if isinstance(raw_labels, (list, tuple, set)):
        labels = " ".join(str(x) for x in raw_labels)
    else:
        labels = str(raw_labels)

    return TorrentSnapshot(
        title=str(g("title")),
        subtitle=str(g("description")),
        site_id=g("site", None),
        site_name=str(g("site_name")),
        page_url=str(g("page_url")),
        enclosure=str(g("enclosure")),
        host_seasons=host_seasons,
        host_episodes=host_episodes,
        host_begin_episode=begin_episode,
        host_part=str(mg("part") or ""),
        host_season_episode=str(mg("season_episode") or ""),
        host_resolution=str(mg("resource_pix") or ""),
        host_has_metadata=begin_season is not None,
        labels=labels,
    )


# ---------------------------------------------------------------------------
# 多候选择优：同一订阅同一轮只保留一份整季全集
# ---------------------------------------------------------------------------


def best_index(stats: list[tuple[int, float, str, str]]) -> int:
    """
    从同季同分辨率、且都通过规则筛选的多个整季全集中挑唯一一份。

    :param stats: 每项为 ``(做种数, 体积字节, 发布时间, 标题)``
    :return: 最优候选的下标；空列表返回 -1

    优先级：做种数 → 体积 → 发布时间 → 标题字典序最小。最后一级只为保证
    跨轮次结果稳定（不会今天选 A、明天选 B），真正想精确选哪一份请用
    包含 / 排除规则，把候选收敛到 1 个。
    """
    if not stats:
        return -1
    scores = [
        (int(item[0] or 0), float(item[1] or 0.0), str(item[2] or ""))
        for item in stats
    ]
    best = max(scores)
    tied = [i for i, score in enumerate(scores) if score == best]
    return min(tied, key=lambda i: str(stats[i][3] or ""))
