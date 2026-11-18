"""
订阅自动保种助手 - 判定引擎单元测试（不依赖 MoviePilot 宿主）。

运行方式（在插件仓库根目录）：
    python -m unittest discover -s tests/v3/subscribeautokeep -v

回归样例 SAMPLE_CASES 全部来自真实 PT 站标题截图，覆盖：
    - 整季打包：全N集 / 完结 / Complete / S0N + 无单集标识
    - 单集：S01E40 / 第40集 / 第8集 / Ep01-Ep08
    - 分段包：S01E01-E06 / E36-E40 / 第27-30集 / 第05-06集
    - 跨季合集、无季号电影、中文季号归一化
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins.v3" / "subscribeautokeep"))

import core  # noqa: E402


def verdict_for(title: str, subtitle: str, season: int, **kwargs):
    """用默认包含/排除规则对一条真实标题执行判定。"""
    options = {
        "subscribe_name": "测试剧",
        "subscribe_season": season,
        "torrent": core.TorrentSnapshot(
            title=title,
            subtitle=subtitle,
            site_id=1,
            site_name="测试站",
            release_group=core.extract_release_group(title),
        ),
        "include_rules": core.parse_rules(core.DEFAULT_INCLUDE_RULES),
        "exclude_rules": core.parse_rules(core.DEFAULT_EXCLUDE_RULES),
        "require_include_rule": True,
    }
    options.update(kwargs)
    return core.evaluate(**options)


class TestRealSiteTitles(unittest.TestCase):
    """真实站点标题回归：整包必须通过，分集/分段/跨季必须被拦下。"""

    def test_all_sample_cases(self):
        failures = []
        for title, subtitle, season, expected in core.SAMPLE_CASES:
            verdict = verdict_for(title, subtitle, season)
            if verdict.passed != expected:
                failures.append(
                    f"\n  [{ '应命中' if expected else '应跳过' }] {title}\n"
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
        self.assertTrue(core.has_multi_season("Show S01 ... S02 合集"))
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


class TestResolutionAndRules(unittest.TestCase):
    """分辨率、规则解析与校验。"""

    def test_resolution_detection(self):
        self.assertIn("2160p", core.detect_resolutions("Show S01 2160p WEB-DL"))
        self.assertIn("2160p", core.detect_resolutions("Show S01 4K HDR"))
        self.assertIn("1080p", core.detect_resolutions("Show S01 1080i HDTV"))
        self.assertIn("720p", core.detect_resolutions("Show S01 720p WEB-DL"))
        self.assertEqual(core.detect_resolutions("Show S01 WEB-DL"), [])

    def test_resolution_filter(self):
        verdict = verdict_for(
            "Show S01 720p WEB-DL 全40集-GROUP", "全40集", 1,
            allowed_resolutions={"2160p"},
        )
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "分辨率")

    def test_unknown_resolution_is_skipped_by_default(self):
        verdict = verdict_for(
            "Show S01 WEB-DL 全40集-GROUP", "全40集", 1,
            allowed_resolutions={"2160p"},
        )
        self.assertFalse(verdict.passed)

    def test_rule_parsing_and_validation(self):
        rules = core.parse_rules("# 注释\ntitle::Complete|全集\nany::Sample")
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[1].field, "any")
        self.assertEqual(core.validate_rules(rules), [])
        bad = core.parse_rules("any::(unclosed")
        self.assertTrue(core.validate_rules(bad))

    def test_exclude_has_priority_over_include(self):
        torrent = core.TorrentSnapshot(title="Show S01 Complete 全40集", subtitle="Sample 预告")
        result = core.match_rules(
            torrent,
            core.parse_rules("any::Complete"),
            core.parse_rules("any::预告"),
        )
        self.assertTrue(result.excluded)
        self.assertFalse(result.included)

    def test_rule_field_isolation(self):
        torrent = core.TorrentSnapshot(title="Show S01 2160p", subtitle="全40集")
        only_title = core.match_rules(torrent, core.parse_rules("title::全\\s*\\d+\\s*集"), [])
        only_any = core.match_rules(torrent, core.parse_rules("any::全\\s*\\d+\\s*集"), [])
        self.assertFalse(only_title.included)
        self.assertTrue(only_any.included)

    def test_cron_validation(self):
        self.assertTrue(core.validate_cron("0 4 * * *")[0])
        self.assertTrue(core.validate_cron("*/15 * * * *")[0])
        self.assertFalse(core.validate_cron("0 4 * *")[0])
        self.assertFalse(core.validate_cron("bad expr")[0])


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
            "subscribe_name": "走马上任",
            "subscribe_season": season,
            "torrent": snapshot,
            "include_rules": core.parse_rules(core.DEFAULT_INCLUDE_RULES),
            "exclude_rules": core.parse_rules(core.DEFAULT_EXCLUDE_RULES),
        }
        options.update(kwargs)
        return core.evaluate(**options)

    def test_s01_without_episode_is_full_season(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": None,
            "end_episode": None, "episode_list": [], "season_episode": "S01",
            "part": None, "resource_pix": "2160p", "resource_team": "ADWeb",
        }
        snapshot = self._snapshot(meta, {}, subtitle="走马上任 第一季 全40集")
        self.assertEqual(snapshot.host_seasons, [1])
        self.assertEqual(snapshot.host_episodes, [])
        self.assertTrue(snapshot.has_host_metadata)
        verdict = self._run(snapshot)
        self.assertTrue(verdict.passed, verdict.reason)

    def test_s01_e40_is_episode(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": 40,
            "end_episode": None, "episode_list": [40], "season_episode": "S01 E40",
            "part": None, "resource_pix": "2160p", "resource_team": "ADWeb",
        }
        verdict = self._run(self._snapshot(meta, {}, subtitle="走马上任 第40集"))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "宿主季集")

    def test_episode_range_is_partial(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": 36,
            "end_episode": 40, "episode_list": [36, 37, 38, 39, 40],
            "season_episode": "S01 E36-E40", "part": None,
            "resource_pix": "2160p", "resource_team": "HHWEB",
        }
        verdict = self._run(self._snapshot(meta, {}, subtitle="第36-40集"))
        self.assertFalse(verdict.passed)

    def test_multi_season_from_host_is_rejected(self):
        meta = {
            "begin_season": 1, "end_season": 3, "begin_episode": None,
            "end_episode": None, "episode_list": [], "season_episode": "S01-S03",
            "part": None, "resource_pix": "2160p", "resource_team": "",
        }
        verdict = self._run(self._snapshot(meta, {}, subtitle="全3季 全集"))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.stage, "多季排除")

    def test_host_resolution_and_team_are_used(self):
        meta = {
            "begin_season": 1, "end_season": None, "begin_episode": None,
            "end_episode": None, "episode_list": [], "season_episode": "S01",
            "part": None, "resource_pix": "4K", "resource_team": "HDSWEB",
        }
        snapshot = self._snapshot(
            meta, {"title": "Show S01 Complete WEB-DL"}, subtitle="全40集"
        )
        self.assertEqual(snapshot.release_group, "HDSWEB")
        verdict = self._run(snapshot, allowed_resolutions={"2160p"})
        self.assertTrue(verdict.passed)
        self.assertIn("2160p", verdict.resolutions)

    def test_no_host_metadata_falls_back_to_regex(self):
        """宿主未给出季集解析时，标题+副标题正则仍能完成判定。"""
        snapshot = core.TorrentSnapshot(
            title="The Silent Storm S01 2160p WEB-DL AAC-CHDWEB",
            subtitle="谷雨 全30集",
        )
        self.assertFalse(snapshot.has_host_metadata)
        self.assertTrue(self._run(snapshot).passed)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
