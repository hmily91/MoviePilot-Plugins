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


if __name__ == "__main__":
    unittest.main(verbosity=2)
