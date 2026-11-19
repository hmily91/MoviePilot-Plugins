"""
订阅自动保种助手 - 判定引擎单元测试（不依赖 MoviePilot 宿主）。

运行方式（在插件仓库根目录）：
    python -m unittest discover -s tests/v3/subscribeautokeep -v

回归样例 SAMPLE_CASES 全部来自真实 PT 站标题截图，覆盖：
    - 整季全集：全N集 / 完结 / Complete / S0N + 无单集标识
    - 单集：S01E40 / 第40集 / 第8集 / Ep01-Ep08
    - 分段包：S01E01-E06 / E36-E40 / 第27-30集 / 第05-06集
    - 跨季合集、无季号电影、中文季号归一化
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins.v3" / "subscribeautokeep"))

import core  # noqa: E402


def verdict_for(title: str, subtitle: str, season: int, **kwargs):
    """对一条真实标题执行判定，默认「全部」分辨率档。"""
    options = {
        "subscribe_season": season,
        "torrent": core.TorrentSnapshot(
            title=title,
            subtitle=subtitle,
            site_id=1,
            site_name="测试站",
        ),
        "resolution": "any",
    }
    options.update(kwargs)
    return core.evaluate(**options)


class TestRealSiteTitles(unittest.TestCase):
    """真实站点标题回归：整季全集必须通过，分集/分段/跨季必须被拦下。"""

    def test_all_sample_cases(self):
        failures = []
        for title, subtitle, season, expected in core.SAMPLE_CASES:
            verdict = verdict_for(title, subtitle, season)
            if verdict.passed != expected:
                failures.append(
                    f"\n  [{'应命中' if expected else '应跳过'}] {title}\n"
                    f"    副标题: {subtitle}\n"
                    f"    实际: passed={verdict.passed} stage={verdict.stage} reason={verdict.reason}"
                )
        self.assertFalse(failures, "真实标题回归失败：" + "".join(failures))

    def test_sample_cases_have_both_sides(self):
        positives = sum(1 for *_, ok in core.SAMPLE_CASES if ok)
        negatives = len(core.SAMPLE_CASES) - positives
        self.assertGreaterEqual(positives, 10)
        self.assertGreaterEqual(negatives, 10)


class TestSeasonParsing(unittest.TestCase):
    """季号归一化与跨季防护。"""

    def test_normalize_season_forms(self):
        self.assertEqual(core.extract_seasons("Show S01 2160p"), [1])
        self.assertEqual(core.extract_seasons("Show Season 4 2160p"), [4])
        self.assertEqual(core.extract_seasons("侠探杰克第4季 全08集"), [4])
        self.assertEqual(core.extract_seasons("别班 第二季 全14集"), [2])
        self.assertEqual(core.extract_seasons("Show S04 ... 第4季"), [4])

    def test_multi_season_detection(self):
        self.assertTrue(core.has_multi_season("Show S01-S03 2160p Complete"))
        self.assertTrue(core.has_multi_season("Show Season 1-3 全集"))
        self.assertTrue(core.has_multi_season("全3季 全集"))
        self.assertFalse(core.has_multi_season("Show S01 2160p 全40集"))

    def test_cross_season_is_rejected(self):
        verdict = verdict_for(
            "Some Show S02 2160p WEB-DL 全10集-GROUP", "第二季 全10集", 1
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "季集校验")


class TestEpisodeGate(unittest.TestCase):
    """单集与分段包闸门（标题 + 副标题）。"""

    def test_episode_markers(self):
        for text in (
            "Show S01E40 2160p", "Show 1x01 1080p", "Show EP01 1080p",
            "Show S01 E36-E40 2160p", "Show S01E01-E06 2160p",
            "第40集", "第27-30集", "第05-06集", "Ep01-Ep08 HDTV",
        ):
            with self.subTest(text=text):
                blocked, _ = core.episode_gate(text)
                self.assertTrue(blocked)

    def test_full_pack_not_blocked(self):
        for text in (
            "Show S01 2160p 全40集", "Show S01 2160p 完结",
            "Show S01 Complete 2160p", "Show S01 2026 2160p WEB-DL-GROUP",
        ):
            with self.subTest(text=text):
                blocked, _ = core.episode_gate(text)
                self.assertFalse(blocked)

    def test_subtitle_drives_gate(self):
        blocked, _ = core.episode_gate(
            "The Silent Storm S01 2160p WEB-DL H265 AAC-CHDWEB",
            "谷雨 第27-30集 | 4K HDR",
        )
        self.assertTrue(blocked, "副标题里的部分集信息必须参与闸门判断")

    def test_completeness_keyword_not_treated_as_episode(self):
        for text in ("全40集", "全12集", "全7集", "完结", "全集"):
            with self.subTest(text=text):
                blocked, _ = core.episode_gate(text)
                self.assertFalse(blocked)


class TestResolution(unittest.TestCase):
    """分辨率四档：全部 / 4k / 1080p / 720p。"""

    def test_resolution_detection(self):
        self.assertIn("2160p", core.detect_resolutions("Show S01 2160p WEB-DL"))
        self.assertIn("2160p", core.detect_resolutions("Show S01 4K HDR"))
        self.assertIn("1080p", core.detect_resolutions("Show S01 1080i HDTV"))
        self.assertIn("720p", core.detect_resolutions("Show S01 720p WEB-DL"))
        self.assertEqual(core.detect_resolutions("Show S01 WEB-DL"), [])

    def test_resolution_choices(self):
        self.assertEqual(
            [v for v, _ in core.RESOLUTION_CHOICES],
            ["any", "4k", "1080p", "720p"],
        )
        self.assertEqual(core.resolution_target("any"), set())
        self.assertEqual(core.resolution_target("4K"), {"2160p"})
        self.assertEqual(core.resolution_target("1080p"), {"1080p"})
        self.assertEqual(core.resolution_target("720p"), {"720p"})

    def test_4k_accepts_2160p_and_rejects_1080p(self):
        self.assertTrue(verdict_for(
            "Show S01 2160p WEB-DL 全40集-GROUP", "全40集", 1, resolution="4k"
        ).passed)
        self.assertTrue(verdict_for(
            "Show S01 4K HDR 全40集-GROUP", "全40集", 1, resolution="4k"
        ).passed)
        verdict = verdict_for(
            "Show S01 1080p WEB-DL 全40集-GROUP", "全40集", 1, resolution="4k"
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "分辨率")

    def test_1080p_accepts_1080i(self):
        self.assertTrue(verdict_for(
            "Show S01 1080i HDTV 全40集-GROUP", "全40集", 1, resolution="1080p"
        ).passed)
        self.assertFalse(verdict_for(
            "Show S01 2160p WEB-DL 全40集-GROUP", "全40集", 1, resolution="1080p"
        ).passed)

    def test_720p_and_any(self):
        self.assertTrue(verdict_for(
            "Show S01 720p WEB-DL 全40集-GROUP", "全40集", 1, resolution="720p"
        ).passed)
        self.assertTrue(verdict_for(
            "Show S01 720p WEB-DL 全40集-GROUP", "全40集", 1, resolution="any"
        ).passed)

    def test_unknown_resolution_is_skipped_when_target_set(self):
        verdict = verdict_for(
            "Show S01 WEB-DL 全40集-GROUP", "全40集", 1, resolution="4k"
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "分辨率")

    def test_any_still_requires_full_pack(self):
        """「全部」只是不筛分辨率，其余闸门一个都不能少。"""
        verdict = verdict_for(
            "Show S01E40 2160p WEB-DL-GROUP", "第40集", 1, resolution="any"
        )
        self.assertFalse(verdict.passed)


class TestCompletenessGate(unittest.TestCase):
    """没有用户正则之后，全集证据是最后一道防误判闸门。"""

    def test_clean_title_without_evidence_is_rejected(self):
        verdict = verdict_for(
            "Show S01 1080p WEB-DL H264-GROUP", "2026年剧集 主演: 某某", 1
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "全集证据")

    def test_text_pack_evidence_passes(self):
        verdict = verdict_for(
            "Show S01 1080p WEB-DL H264-GROUP", "全30集 | 完结", 1
        )
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertTrue(verdict.is_full_pack)

    def test_host_full_season_without_text_evidence_passes(self):
        snapshot = core.TorrentSnapshot(
            title="Show 1080p WEB-DL H264-GROUP",
            subtitle="2026年剧集 主演: 某某",
            host_seasons=[1],
            host_episodes=[],
            host_has_metadata=True,
            host_season_episode="S01",
        )
        verdict = core.evaluate(subscribe_season=1, torrent=snapshot, resolution="any")
        self.assertTrue(verdict.passed, verdict.reason)


class TestHostMetadata(unittest.TestCase):
    """
    MP V3 ``Context.meta_info`` 季集识别优先。

    参考 MP 搜索结果筛选器：S01 代表完结季（无集号），S01 E40 / S01 E36-E40
    代表分集或分段包。宿主结论优先于标题正则，正则只作兜底。
    """

    def _snapshot(self, meta_kw, torrent_kw, subtitle=""):
        from types import SimpleNamespace

        meta = SimpleNamespace(**meta_kw)
        context = SimpleNamespace(meta_info=meta)
        defaults = {"title": "Lets Go S01 2160p TX WEB-DL H265-GROUP", "description": subtitle}
        defaults.update(torrent_kw)
        torrent = SimpleNamespace(**defaults)
        return core.build_snapshot_from_torrent(torrent, context)

    def _run(self, snapshot, season=1, **kwargs):
        options = {
            "subscribe_season": season,
            "torrent": snapshot,
            "resolution": "any",
        }
        options.update(kwargs)
        return core.evaluate(**options)

    def test_s01_without_episode_is_full_season(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": None,
            "end_episode": None, "episode_list": [], "season_episode": "S01",
            "part": None, "resource_pix": "2160p",
        }
        snapshot = self._snapshot(meta, {}, subtitle="走马上任 第一季 全40集")
        self.assertEqual(snapshot.host_seasons, [1])
        self.assertEqual(snapshot.host_episodes, [])
        self.assertTrue(snapshot.host_has_metadata)
        self.assertTrue(self._run(snapshot).passed)

    def test_s01_e40_is_episode(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": 40,
            "end_episode": None, "episode_list": [40], "season_episode": "S01 E40",
            "part": None, "resource_pix": "2160p",
        }
        verdict = self._run(self._snapshot(meta, {}, subtitle="走马上任 第40集"))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "宿主季集")

    def test_episode_range_is_partial(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": 36,
            "end_episode": 40, "episode_list": [36, 37, 38, 39, 40],
            "season_episode": "S01 E36-E40", "part": None, "resource_pix": "2160p",
        }
        verdict = self._run(self._snapshot(meta, {}, subtitle="第36-40集"))
        self.assertFalse(verdict.passed)

    def test_multi_season_from_host_is_rejected(self):
        meta = {
            "begin_season": 1, "end_season": 3, "begin_episode": None,
            "end_episode": None, "episode_list": [], "season_episode": "S01-S03",
            "part": None, "resource_pix": "2160p",
        }
        verdict = self._run(self._snapshot(meta, {}, subtitle="全3季 全集"))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "多季排除")

    def test_host_resolution_is_used(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": None,
            "end_episode": None, "episode_list": [], "season_episode": "S01",
            "part": None, "resource_pix": "4K",
        }
        snapshot = self._snapshot(
            meta, {"title": "Show S01 Complete WEB-DL"}, subtitle="全40集"
        )
        self.assertEqual(snapshot.host_resolution, "4K")
        verdict = self._run(snapshot, resolution="4k")
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertIn("2160p", verdict.resolutions)

    def test_no_host_metadata_falls_back_to_text(self):
        """宿主未给出季集解析时，标题+副标题仍能完成判定。"""
        snapshot = core.TorrentSnapshot(
            title="The Silent Storm S01 2160p WEB-DL AAC-CHDWEB",
            subtitle="谷雨 全30集",
        )
        self.assertFalse(snapshot.host_has_metadata)
        self.assertTrue(self._run(snapshot).passed)


class TestMisc(unittest.TestCase):
    """Cron 校验与站点过滤。"""

    def test_cron_validation(self):
        self.assertTrue(core.validate_cron("0 4 * * *")[0])
        self.assertTrue(core.validate_cron("*/15 * * * *")[0])
        self.assertFalse(core.validate_cron("0 4 * *")[0])
        self.assertFalse(core.validate_cron("bad expr")[0])

    def test_site_filter(self):
        verdict = verdict_for(
            "Show S01 1080p WEB-DL 全40集-GROUP", "全40集", 1,
            allowed_site_ids={9},
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "站点")
        self.assertTrue(verdict_for(
            "Show S01 1080p WEB-DL 全40集-GROUP", "全40集", 1,
            allowed_site_ids={1},
        ).passed)


class TestQueue(unittest.TestCase):
    """等待队列的去重、到期与取消。"""

    def test_queue_lifecycle(self):
        from pending_queue import PendingQueue, PendingTask, make_task_id

        queue = PendingQueue()
        task = PendingTask(
            task_id=make_task_id(7, 1, "Show S01 Complete"),
            subscribe_id=7,
            subscribe_name="测试剧",
            season=1,
            created_ts=100.0,
            execute_ts=200.0,
        )
        self.assertTrue(queue.upsert(task))
        self.assertFalse(queue.upsert(task))
        self.assertEqual(len(queue.waiting()), 1)
        self.assertEqual(len(queue.due(now=150.0)), 0)
        self.assertEqual(len(queue.due(now=250.0)), 1)
        queue.mark(task.task_id, "cancelled", "打包种子消失")
        self.assertEqual(len(queue.waiting()), 0)
        self.assertEqual(queue.all()[0].last_reason, "打包种子消失")
        self.assertEqual(len(queue.from_list(queue.to_list()).all()), 1)


class TestRules(unittest.TestCase):
    """包含 / 排除规则：解析、优先级、HDR 场景、只收紧不放宽。"""

    SDR_TITLE = "Reacher S04 2026 2160p WEB-DL DDP5.1 Atmos H265-Pure@HDSWEB"
    HDR_TITLE = "Reacher S04 2026 2160p WEB-DL DDP5.1 Atmos H265 HDR DV-Pure@HDSWEB"
    SUBTITLE = "美剧: 侠探杰克第4季 全08集 | 主演: 阿兰·里奇森"

    @staticmethod
    def rules(text: str, source: str = "plugin") -> list[core.Rule]:
        parsed, errors = core.parse_rules(text, source)
        assert not errors, errors
        return parsed

    def test_two_2160p_candidates_both_pass_by_default(self):
        """未配置规则时，两份 2160p 全集都能通过——正是需要手动选择的场景。"""
        self.assertTrue(verdict_for(self.SDR_TITLE, self.SUBTITLE, 4).passed)
        self.assertTrue(verdict_for(self.HDR_TITLE, self.SUBTITLE, 4).passed)

    def test_parse_rules(self):
        parsed, errors = core.parse_rules(
            "# 注释行\n\nHDR|DV\n1080p\nATeam", "include"
        )
        self.assertEqual(errors, [])
        self.assertEqual([r.raw for r in parsed], ["HDR|DV", "1080p", "ATeam"])
        self.assertTrue(all(r.source == "include" for r in parsed))
        # 普通关键字按子串匹配且忽略大小写
        self.assertTrue(core.match_rule(parsed, "Show 1080P WEB-DL") is not None)
        self.assertTrue(core.match_rule(parsed, "show ateam release") is not None)

    def test_invalid_regex_is_reported(self):
        for bad in ("[", "(", "*", "a{2,1}", "(?P<"):
            parsed, errors = core.parse_rules(bad, "exclude")
            self.assertEqual(parsed, [])
            self.assertTrue(errors, f"应识别非法正则：{bad!r}")

    def test_invalid_line_does_not_silently_pass(self):
        """非法行必须被报错，而不是被忽略成「没有排除规则」。"""
        parsed, errors = core.parse_rules("HDR\n[\n", "exclude")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(errors), 1)

    def test_exclude_wins_over_include(self):
        """同一资源同时命中包含与排除时，排除优先。"""
        verdict = verdict_for(
            self.HDR_TITLE, self.SUBTITLE, 4,
            include_rules=self.rules("HDR"),
            exclude_rules=self.rules("DV"),
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "排除规则")
        self.assertEqual(verdict.matched_exclude, "DV")

    def test_include_rule_is_required_when_configured(self):
        hit = verdict_for(
            self.HDR_TITLE, self.SUBTITLE, 4,
            include_rules=self.rules(r"HDR|HDR10|DV"),
        )
        self.assertTrue(hit.passed)
        self.assertEqual(hit.matched_include, r"HDR|HDR10|DV")
        miss = verdict_for(
            self.SDR_TITLE, self.SUBTITLE, 4,
            include_rules=self.rules(r"HDR|HDR10|DV"),
        )
        self.assertFalse(miss.passed)
        self.assertEqual(miss.stage, "包含规则")

    def test_hdr_scenario_keep_non_hdr(self):
        """只要不带 HDR 的那份 → 排除填 HDR|DV|杜比视界。"""
        exclude = self.rules(r"HDR|HDR10|DV|Dolby\s*Vision|杜比视界")
        self.assertTrue(verdict_for(
            self.SDR_TITLE, self.SUBTITLE, 4, exclude_rules=exclude,
        ).passed)
        rejected = verdict_for(
            self.HDR_TITLE, self.SUBTITLE, 4, exclude_rules=exclude,
        )
        self.assertFalse(rejected.passed)
        self.assertEqual(rejected.stage, "排除规则")

    def test_hdr_scenario_keep_hdr_only(self):
        """只要带 HDR 的那份 → 包含填 HDR|HDR10|DV。"""
        include = self.rules(r"HDR|HDR10|DV")
        self.assertTrue(verdict_for(
            self.HDR_TITLE, self.SUBTITLE, 4, include_rules=include,
        ).passed)
        self.assertFalse(verdict_for(
            self.SDR_TITLE, self.SUBTITLE, 4, include_rules=include,
        ).passed)

    def test_rules_cannot_relax_safety_gates(self):
        """用户规则只能收紧：即使包含 HDR，分集 / 跨季 / 错分辨率仍被拒。"""
        include = self.rules("HDR")
        episode = verdict_for(
            "Reacher S04E08 2160p WEB-DL HDR-GROUP", "第8集", 4,
            include_rules=include,
        )
        self.assertFalse(episode.passed)
        self.assertNotEqual(episode.stage, "包含规则")
        cross_season = verdict_for(
            "Reacher S03-S04 2160p Complete HDR-GROUP", "全16集", 4,
            include_rules=include,
        )
        self.assertFalse(cross_season.passed)
        wrong_resolution = verdict_for(
            "Reacher S04 1080p WEB-DL HDR 全08集-GROUP", "全08集", 4,
            include_rules=include, resolution="4k",
        )
        self.assertFalse(wrong_resolution.passed)
        self.assertEqual(wrong_resolution.stage, "分辨率")

    def test_rules_match_subtitle_and_labels(self):
        snapshot = core.TorrentSnapshot(
            title="Reacher S04 2160p WEB-DL 全08集-GROUP",
            subtitle="杜比视界 全08集",
            labels="免费 官方",
        )
        self.assertIn("杜比视界", snapshot.rule_text)
        self.assertIn("免费", snapshot.rule_text)
        include = self.rules("杜比视界")
        self.assertTrue(core.evaluate(
            subscribe_season=4, torrent=snapshot, include_rules=include,
        ).passed)
        labels_only = self.rules("官方")
        self.assertTrue(core.evaluate(
            subscribe_season=4, torrent=snapshot, include_rules=labels_only,
        ).passed)

    def test_empty_rules_backward_compatible(self):
        """不配置任何规则时行为与 v3.1.0 一致。"""
        self.assertTrue(verdict_for(
            self.SDR_TITLE, self.SUBTITLE, 4,
            include_rules=[], exclude_rules=[],
        ).passed)
        self.assertFalse(verdict_for(
            "Reacher S04E08 2160p WEB-DL-GROUP", "第8集", 4,
        ).passed)


class TestBestIndex(unittest.TestCase):
    """多候选择优：做种数优先、同分稳定、空输入安全。"""

    def test_pick_highest_seeders(self):
        stats = [
            (10, 100.0, "2026-01-01", "A 2160p"),
            (30, 100.0, "2026-01-01", "B 2160p HDR"),
            (5, 999.0, "2026-01-01", "C 2160p"),
        ]
        self.assertEqual(core.best_index(stats), 1)

    def test_size_and_pubdate_break_ties(self):
        stats = [
            (10, 100.0, "2026-01-01", "A"),
            (10, 200.0, "2026-01-01", "B"),
        ]
        self.assertEqual(core.best_index(stats), 1)
        stats2 = [
            (10, 200.0, "2026-01-01", "A"),
            (10, 200.0, "2026-02-01", "B"),
        ]
        self.assertEqual(core.best_index(stats2), 1)

    def test_stable_when_fully_tied(self):
        stats = [(10, 100.0, "2026-01-01", "Z"), (10, 100.0, "2026-01-01", "A")]
        self.assertEqual(core.best_index(stats), 1)
        self.assertEqual(core.best_index(list(reversed(stats))), 0)

    def test_empty_input(self):
        self.assertEqual(core.best_index([]), -1)


class TestPluginContract(unittest.TestCase):
    """防止配置页按钮与插件 API 鉴权被改回失效写法（v3.2.1 回归防线）。

    配置页由 FormRender.vue 渲染：只认 props 里以 on 开头的字符串函数，
    不认顶层 events；动态路由注册按 ``auth == "bear"`` 选择登录 token 校验，
    其它值一律落到 apikey 校验。任一处写错，按钮都会被静默忽略或 401。
    """

    @classmethod
    def setUpClass(cls) -> None:
        plugin_dir = ROOT / "plugins.v3" / "subscribeautokeep"
        cls.main_src = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
        cls.form = json.loads((plugin_dir / "form.json").read_text(encoding="utf-8"))
        cls.meta = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        cls.index = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))

    def test_api_auth_is_bearer_string(self):
        self.assertEqual(self.main_src.count('"auth": "bear"'), 2)
        self.assertNotIn('"auth": True', self.main_src)
        self.assertNotIn('"auth": "apikey"', self.main_src)

    def test_config_buttons_use_onclick(self):
        self.assertIn('"onclick": onclick', self.main_src)
        self.assertIn("window.MoviePilotAPI.post('plugin/", self.main_src)
        self.assertNotIn('"events": {"click"', self.main_src)

    def test_form_snapshot_buttons(self):
        buttons = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("component") == "VBtn":
                    buttons.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.form["form"])
        self.assertEqual(len(buttons), 2)
        for btn in buttons:
            self.assertNotIn("events", btn)
            onclick = btn["props"].get("onclick", "")
            self.assertIn("window.MoviePilotAPI.post", onclick)
            self.assertIn(".catch", onclick)
        self.assertIn("plugin/SubscribeAutoKeep/run", buttons[0]["props"]["onclick"])
        self.assertIn("plugin/SubscribeAutoKeep/validate", buttons[1]["props"]["onclick"])

    def test_button_endpoints_have_logs_and_message(self):
        for marker in ("【立即扫描一次】", "【校验规则】", '"message"'):
            self.assertIn(marker, self.main_src)

    def test_versions_consistent(self):
        version = re.search(r'plugin_version = "([^"]+)"', self.main_src).group(1)
        self.assertEqual(version, "3.2.1")
        self.assertEqual(self.meta["version"], version)
        self.assertEqual(self.index["SubscribeAutoKeep"]["version"], version)


if __name__ == "__main__":
    unittest.main(verbosity=2)
