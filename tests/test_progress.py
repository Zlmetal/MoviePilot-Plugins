"""1.1.21 的两个回归点。

1. 转存真成功（离线提交成功 / 分享已确认 / 窗口过期放行）之后必须记放行期，
   否则 PT 一搜到资源又会被当成「还没转存」重新拦下，开一个 24 小时的窗口；
2. 离线文件搬完之后要回写订阅进度，不然媒体库里有片、订阅一直挂着。

运行：python3 -m unittest tests.test_progress -v
"""

import threading
import unittest
from unittest.mock import MagicMock

from tests.test_instant_search import csp, make_candidate


class ReleaseTest(unittest.TestCase):
    """真转存成功 → 记放行期，不能只关窗。"""

    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._enabled = True
        self.plugin._notify = False
        self.plugin._instant_search = True
        self.plugin._offline_enabled = True
        self.plugin._offline_staging_path = "/离线中转"
        self.plugin._windows = {}
        self.plugin._released = {}
        self.plugin._offline_tasks = {}
        self.plugin._lock = threading.RLock()
        self.plugin._persist = lambda: None
        self.plugin._trace = lambda *a, **k: None
        self.plugin._confirm_subscribe_progress = MagicMock()

    def _cloud(self, take_over=False):
        cloud = MagicMock()
        cloud._takeover_new_subscribes = take_over
        cloud._is_takeover_active = lambda: take_over
        return cloud

    def test_offline_submit_marks_released(self):
        self.plugin._windows["8"] = {"sid": 8, "name": "测试片", "instant": True}
        offline = make_candidate(
            "磁力候选", 80, tier=csp._OFFLINE_TIER, url="magnet:?xt=urn:btih:abc",
        )
        self.plugin._submit_offline = MagicMock(return_value=(True, "已提交离线下载"))
        self.plugin._share_transfer_service = MagicMock(return_value=None)

        self.plugin._submit_worker(
            self.plugin._windows["8"], self._cloud(), [offline], "测试片", 8,
        )

        self.assertNotIn("8", self.plugin._windows)
        self.assertIn("8", self.plugin._released, "离线提交成功却没记放行期")
        self.assertTrue(self.plugin._is_released("8"))

    def test_share_success_marks_released(self):
        self.plugin._windows["9"] = {"sid": 9, "name": "测试片", "instant": True}
        self.plugin._share_transfer_service = MagicMock(return_value=None)
        self.plugin._transfer_and_confirm = MagicMock(return_value=(True, "已转存 1 个文件"))

        self.plugin._submit_worker(
            self.plugin._windows["9"], self._cloud(),
            [make_candidate("分享候选", 90)], "测试片", 9,
        )

        self.assertIn("9", self.plugin._released)
        # 分享走上游同步链，它自己会写进度，这里不该再补一遍
        self.plugin._confirm_subscribe_progress.assert_not_called()

    def test_expired_window_without_candidate_marks_released(self):
        """窗口过期、没搜到任何网盘候选时是「放行 PT」，也要记放行期。"""
        event_data = MagicMock()
        event_data.cancel = False
        event_data.episodes = []
        event_data.origin = "subscribe:11"
        event_data.context.meta_info = None
        event_data.context.torrent_info.title = "PT 种子"
        self.plugin._windows["11"] = {
            "sid": 11, "name": "测试片", "type": "电影", "season": 1,
            "episodes": None, "start_at": 0.0, "expire_at": 1.0,
            "opened_by": "PT 种子", "best": None, "searches": 0,
        }
        self.plugin._cloud_plugin = MagicMock(return_value=self._cloud())
        self.plugin._is_managed = MagicMock(return_value=True)
        self.plugin._parse_subscribe = MagicMock(
            return_value={"id": 11, "name": "测试片", "type": "电影", "season": 1}
        )

        self.plugin._handle_resource_download(event_data)

        self.assertNotIn("11", self.plugin._windows)
        self.assertIn("11", self.plugin._released, "放行 PT 却没记放行期")


class ProgressTest(unittest.TestCase):
    """离线搬完 → 推订阅进度，优先复用上游完成链。"""

    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._notify = False
        self.subscribe = MagicMock()
        self.subscribe.id = 12
        self.subscribe.name = "测试片"
        self.subscribe.note = []
        self.plugin._get_subscribe = MagicMock(return_value=self.subscribe)
        self.finisher = MagicMock(return_value=0)
        self.mediainfo = MagicMock()
        self.cloud = MagicMock()
        self.cloud._sync_handler._subscribe_handler.check_and_finish_subscribe = self.finisher
        self.cloud._sync_handler._subscribe_mediainfo.return_value = self.mediainfo

    def _record(self, media_type="movie", episodes=None):
        return {"sid": 12, "name": "测试片", "media_type": media_type,
                "episodes": episodes or []}

    def test_movie_progress_pushed_to_upstream(self):
        self.plugin._confirm_subscribe_progress(
            self.cloud, 12, self._record(media_type="movie"),
        )
        self.finisher.assert_called_once()
        kwargs = self.finisher.call_args.kwargs
        self.assertEqual(kwargs["success_episodes"], [1])
        self.assertIs(kwargs["mediainfo"], self.mediainfo)

    def test_tv_uses_known_episodes(self):
        self.plugin._confirm_subscribe_progress(
            self.cloud, 12, self._record(media_type="tv", episodes=[3, 4]),
        )
        kwargs = self.finisher.call_args.kwargs
        self.assertEqual(sorted(kwargs["success_episodes"]), [3, 4])

    def test_tv_without_episodes_skips(self):
        """剧集不知道具体集数就别乱写，免得把没下全的订阅判完成。"""
        self.plugin._confirm_subscribe_progress(
            self.cloud, 12, self._record(media_type="tv"),
        )
        self.finisher.assert_not_called()

    def test_missing_subscribe_skips(self):
        self.plugin._get_subscribe = MagicMock(return_value=None)
        self.plugin._confirm_subscribe_progress(self.cloud, 12, self._record())
        self.finisher.assert_not_called()

    def test_falls_back_when_upstream_missing(self):
        """上游没这套能力时退回写 note。"""
        self.cloud._sync_handler._subscribe_handler = None
        oper = MagicMock()
        csp.SubscribeOper = MagicMock(return_value=oper)
        try:
            self.plugin._confirm_subscribe_progress(
                self.cloud, 12, self._record(media_type="movie"),
            )
            oper.update.assert_called_once()
            args, _kwargs = oper.update.call_args
            self.assertEqual(args[0], 12)
            self.assertEqual(args[1], {"note": [1]})
        finally:
            csp.SubscribeOper = MagicMock()


if __name__ == "__main__":
    unittest.main()
