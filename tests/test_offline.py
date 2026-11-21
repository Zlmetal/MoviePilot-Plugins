"""磁力/电驴离线中转的桩测试。

覆盖 m15560 需求的三条硬要求：
1. 磁力/电驴纳入网盘渠道，但排在「需解锁网盘」之后、PT 之前；
2. 分享转存路径不受影响，仍走上游保存目录；
3. 离线先下到本插件的中转目录，确认下完再搬到上游保存目录并补下载历史。

运行：python3 -m unittest tests.test_offline -v
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

from tests.test_instant_search import csp, make_candidate


def offline_candidate(url="magnet:?xt=urn:btih:" + "A" * 40, score=80):
    item = make_candidate("磁力 4K", score, tier=1.5, url=url)
    item["tier_text"] = "磁力/电驴离线"
    return item


class TierTest(unittest.TestCase):
    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._offline_enabled = True

    def test_magnet_is_between_unlock_and_pt(self):
        """磁力档位必须卡在需解锁网盘(1)与 PT(2)之间。"""
        self.assertGreater(csp._OFFLINE_TIER, 1.0)
        self.assertLess(csp._OFFLINE_TIER, 2.0)

    def test_magnet_tier_when_enabled(self):
        tier = self.plugin._candidate_tier({"resource_type": "magnet", "url": "magnet:?xt=1"})
        self.assertEqual(tier, csp._OFFLINE_TIER)

    def test_ed2k_tier_by_url_prefix(self):
        tier = self.plugin._candidate_tier({"url": "ed2k://|file|x.mkv|1|" + "B" * 32 + "|/"})
        self.assertEqual(tier, csp._OFFLINE_TIER)

    def test_magnet_falls_back_to_pt_when_disabled(self):
        """关掉开关后磁力退回纯 PT，保持老行为。"""
        self.plugin._offline_enabled = False
        tier = self.plugin._candidate_tier({"resource_type": "magnet", "url": "magnet:?xt=1"})
        self.assertEqual(tier, 2.0)

    def test_share_direct_link_untouched(self):
        """网盘分享直链仍是最优先，不受离线开关影响。"""
        tier = self.plugin._candidate_tier({"url": "https://115.com/s/abc"})
        self.assertEqual(tier, 0.0)

    def test_sorting_order(self):
        """排序：直链 < 需解锁 < 磁力 < PT。"""
        candidates = [
            {"tier": 2.0, "score": 100},
            {"tier": csp._OFFLINE_TIER, "score": 90},
            {"tier": 1.0, "score": 10},
            {"tier": 0.0, "score": 1},
        ]
        ordered = sorted(candidates, key=self.plugin._candidate_sort_key)
        self.assertEqual([c["tier"] for c in ordered], [0.0, 1.0, csp._OFFLINE_TIER, 2.0])

    def test_tier_text(self):
        scored = self.plugin._score_candidate(
            "dian115", {"title": "磁力 4K", "url": "magnet:?xt=1", "resource_type": "magnet"}
        )
        self.assertIsNotNone(scored)
        self.assertEqual(scored["tier_text"], "磁力/电驴离线")


class SubmitOfflineTest(unittest.TestCase):
    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._enabled = True
        self.plugin._notify = False
        self.plugin._offline_enabled = True
        self.plugin._offline_staging_path = "/离线中转"
        self.plugin._offline_tasks = {}
        self.plugin._windows = {"12": {"sid": 12, "name": "测试片", "type": "电影",
                                       "expire_at": 9_999_999_999.0}}
        self.plugin._released = {}
        self.plugin._persist = lambda: None

        self.offline = MagicMock()
        self.offline.add_offline_download.return_value = True
        self.cloud = MagicMock()
        self.cloud._sync_handler._offline_download = self.offline
        self.cloud._sync_handler._cloud_transfer_path = "/待整理"

    def test_submit_records_staging_and_upstream(self):
        ok, detail = self.plugin._submit_offline(
            self.cloud, 12,
            {"season": 1, "episodes": [3], "type": "电影"},
            offline_candidate(), {"url": "magnet:?xt=urn:btih:" + "A" * 40},
            "测试片",
        )
        self.assertTrue(ok, detail)
        self.offline.add_offline_download.assert_called_once()
        args, kwargs = self.offline.add_offline_download.call_args
        self.assertEqual(args[1], "/离线中转", "离线必须先下到中转目录")
        self.assertEqual(len(self.plugin._offline_tasks), 1)
        record = list(self.plugin._offline_tasks.values())[0]
        self.assertEqual(record["staging"], "/离线中转")
        self.assertEqual(record["upstream"], "/待整理")
        self.assertEqual(record["sid"], 12)

    def test_blank_staging_falls_back_to_upstream(self):
        """没配中转目录时直接下到上游保存目录（等同原行为）。"""
        self.plugin._offline_staging_path = ""
        self.plugin._submit_offline(
            self.cloud, 12, {}, offline_candidate(),
            {"url": "magnet:?xt=urn:btih:" + "A" * 40}, "测试片",
        )
        args, _kwargs = self.offline.add_offline_download.call_args
        self.assertEqual(args[1], "/待整理")

    def test_rejected_submission_reports(self):
        self.offline.add_offline_download.return_value = False
        ok, detail = self.plugin._submit_offline(
            self.cloud, 12, {}, offline_candidate(),
            {"url": "magnet:?xt=urn:btih:" + "A" * 40}, "测试片",
        )
        self.assertFalse(ok)
        self.assertIn("离线下载", detail)
        self.assertEqual(self.plugin._offline_tasks, {})

    def test_share_transfer_does_not_enter_offline(self):
        """分享转存候选绝不能进离线分支，仍交给上游同步链。"""
        captured = {}
        self.plugin._transfer_and_confirm = (
            lambda cloud, sid, resource, name: captured.update(resource=resource) or (True, "已转存 1 个文件")
        )
        self.plugin._submit_offline = MagicMock()
        self.plugin._close_window = MagicMock()
        self.plugin._notify = False
        self.plugin._share_transfer_service = lambda _c: None

        self.plugin._submit_worker(
            {"sid": 12, "name": "测试片", "expire_at": 9_999_999_999.0},
            self.cloud,
            [make_candidate("网盘 4K", 90, tier=0)],
            "测试片", 12,
        )
        self.plugin._submit_offline.assert_not_called()
        self.assertEqual(captured["resource"]["url"], "https://115.com/s/x")


class PollOfflineTest(unittest.TestCase):
    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._enabled = True
        self.plugin._notify = False
        self.plugin._offline_enabled = True
        self.plugin._offline_tasks = {}
        self.plugin._windows = {}
        self.plugin._released = {}
        self.plugin._persist = lambda: None

        self.tasks = MagicMock()
        self.query = MagicMock()
        self.mutations = MagicMock()
        self.cloud = MagicMock()
        self.cloud._sync_handler._offline_tasks = self.tasks
        self.cloud._sync_handler._cloud_query = self.query
        self.cloud._sync_handler._cloud_mutations = self.mutations
        self.plugin._record_offline_history = MagicMock()

        self.record = {
            "pending_key": "offline:" + "A" * 40 + ":12",
            "sid": 12, "name": "测试片", "title": "磁力 4K",
            "url": "magnet:?xt=urn:btih:" + "A" * 40,
            "hash": "A" * 40, "staging": "/离线中转", "upstream": "/待整理",
            "season": 1, "episodes": [], "media_type": "电影",
            "submitted_at": 0.0, "next_check_at": 0.0, "checks": 0,
        }
        self.plugin._offline_tasks[self.record["pending_key"]] = dict(self.record)

    def _file(self, name, is_dir=False):
        item = MagicMock()
        item.name = name
        item.is_directory = is_dir
        return item

    def test_completed_task_moves_to_upstream(self):
        """下完就从中转目录搬到上游保存目录，并补下载历史。"""
        self.tasks.get_offline_task_list_snapshot.return_value = {"tasks": [
            {"id": "A" * 40, "completed": True, "failed": False, "percent": 100},
        ]}
        self.query.list_offline_task_files.return_value = [self._file("片.mkv")]
        self.mutations.move_file.return_value = MagicMock()

        self.plugin._poll_offline_tasks(self.cloud)

        self.mutations.move_file.assert_called_once()
        args, _kwargs = self.mutations.move_file.call_args
        self.assertEqual(args[1], "/待整理", "下完必须搬到上游保存目录")
        self.plugin._record_offline_history.assert_called_once()
        self.assertEqual(self.plugin._offline_tasks, {}, "搬完就该丢掉这条记录")

    def test_running_task_waits(self):
        self.tasks.get_offline_task_list_snapshot.return_value = {"tasks": [
            {"id": "A" * 40, "completed": False, "failed": False, "percent": 30},
        ]}
        self.plugin._poll_offline_tasks(self.cloud)
        self.mutations.move_file.assert_not_called()
        self.assertIn(self.record["pending_key"], self.plugin._offline_tasks)

    def test_failed_task_dropped(self):
        self.tasks.get_offline_task_list_snapshot.return_value = {"tasks": [
            {"id": "A" * 40, "completed": False, "failed": True, "status_text": "下载失败"},
        ]}
        self.plugin._poll_offline_tasks(self.cloud)
        self.mutations.move_file.assert_not_called()
        self.assertEqual(self.plugin._offline_tasks, {})

    def test_non_video_files_ignored(self):
        self.tasks.get_offline_task_list_snapshot.return_value = {"tasks": [
            {"id": "A" * 40, "completed": True, "failed": False},
        ]}
        self.query.list_offline_task_files.return_value = [
            self._file("字幕.srt"), self._file("说明.txt"),
        ]
        self.query.list_files_recursive.return_value = [
            self._file("字幕.srt"), self._file("说明.txt"),
        ]
        self.plugin._poll_offline_tasks(self.cloud)
        self.mutations.move_file.assert_not_called()
        self.assertIn(self.record["pending_key"], self.plugin._offline_tasks)

    def test_same_directory_only_records_history(self):
        """中转目录就是保存目录时别自己搬自己，只补历史。"""
        self.plugin._offline_tasks[self.record["pending_key"]]["staging"] = "/待整理"
        self.tasks.get_offline_task_list_snapshot.return_value = {"tasks": [
            {"id": "A" * 40, "completed": True, "failed": False},
        ]}
        self.query.list_offline_task_files.return_value = [self._file("片.mkv")]
        self.plugin._poll_offline_tasks(self.cloud)
        self.mutations.move_file.assert_not_called()
        self.plugin._record_offline_history.assert_called_once()
        self.assertEqual(self.plugin._offline_tasks, {})

    def test_give_up_after_timeout(self):
        self.plugin._offline_tasks[self.record["pending_key"]]["submitted_at"] = (
            -csp._OFFLINE_GIVE_UP_SECONDS - 10
        )
        self.tasks.get_offline_task_list_snapshot.return_value = {"tasks": [
            {"id": "A" * 40, "completed": False, "failed": False, "percent": 1},
        ]}
        self.plugin._poll_offline_tasks(self.cloud)
        self.assertEqual(self.plugin._offline_tasks, {}, "超时该放弃")


class DeferredResolveTest(unittest.TestCase):
    """聚影这类渠道列表模式只回 resource_id、不回链接，不能当空壳丢掉。"""

    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._enabled = True
        self.plugin._notify = False
        self.plugin._offline_enabled = True
        self.plugin._allow_unlock = True
        self.plugin._max_unlock_points = 0
        self.plugin._offline_staging_path = "/离线中转"
        self.plugin._offline_tasks = {}
        self.plugin._windows = {}
        self.plugin._released = {}
        self.plugin._persist = lambda: None

    def _juying_item(self, **extra):
        item = {
            "title": "疯狂舞蹈办公室[中文字幕].Mad.Dance.Office.2026.1080p.WEB-DL",
            "resource_type": "magnet",
            "url": "",
            "provider_data": {"resource_id": "405799"},
            "resource_ref": "",
            "size_bytes": 6324339343,
        }
        item.update(extra)
        return item

    def test_deferred_magnet_kept(self):
        """url 空、无解锁标记，但有 resource_id 的磁力必须进候选。"""
        scored = self.plugin._score_candidate("juying", self._juying_item())
        self.assertIsNotNone(scored, "延迟解析型磁力被丢掉了")
        self.assertEqual(scored["tier"], csp._OFFLINE_TIER)
        self.assertEqual(scored["tier_text"], "磁力/电驴离线")
        self.assertIn("1080P", scored["tags"])

    def test_blank_shell_still_dropped(self):
        """既没链接也没标识符的空壳仍要丢掉。"""
        item = self._juying_item()
        item["provider_data"] = {}
        self.assertIsNone(self.plugin._score_candidate("juying", item))

    def test_share_without_ref_still_dropped(self):
        """网盘分享类不带 resource_ref 的空壳不许放行。"""
        item = self._juying_item(resource_type="115", provider_data={})
        self.assertIsNone(self.plugin._score_candidate("juying", item))

    def test_deferred_off_when_offline_disabled(self):
        """关掉离线开关后，延迟解析型磁力退回纯 PT，不再进候选。"""
        self.plugin._offline_enabled = False
        self.assertIsNone(self.plugin._score_candidate("juying", self._juying_item()))

    def test_resolve_deferred_returns_url(self):
        """换链接走 can_unlock（聚影是 RESOURCE_RESOLVE）拿到 magnet。"""
        handler = MagicMock()
        handler.can_unlock.return_value = True
        handler.unlock_resource.return_value = (
            "magnet:?xt=urn:btih:7c64fc7f88ab4a89535e6674c958f9ada507da93"
        )
        cloud = MagicMock()
        cloud._search_handler = handler
        best = self.plugin._score_candidate("juying", self._juying_item())
        resolved = self.plugin._resolve_deferred_resource(cloud, best, "疯狂舞蹈办公室")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved["url"].startswith("magnet:?xt=urn:btih:"))
        self.assertTrue(resolved["is_unlocked"])

    def test_resolve_deferred_without_identifier(self):
        """没有标识符就不该去换链接（避免白发请求）。"""
        cloud = MagicMock()
        best = {"resource": {"resource_type": "magnet", "url": ""}, "title": "x"}
        self.assertIsNone(self.plugin._resolve_deferred_resource(cloud, best, "x"))
        cloud._search_handler.unlock_resource.assert_not_called()


if __name__ == "__main__":
    unittest.main()
