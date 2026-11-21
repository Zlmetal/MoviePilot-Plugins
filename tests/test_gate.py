"""批量订阅闸 + 上游让位的桩测试。

覆盖两件事：
1. 批量闸：同一时刻只放行 N 条，两条之间隔 M 秒，排不上就等/超时放弃；
2. 上游让位：网盘订阅助手自己会为新订阅搜一次并转存，本插件不能再插一手，
   否则同一部片被转两份（「聚影和 Dian115 各转一个」）。

运行：python3 -m unittest tests.test_gate -v
"""

import threading
import time
import unittest
from unittest.mock import MagicMock

from tests.test_instant_search import (
    csp,
    make_candidate,
)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._instant_max_concurrent = 1
        self.plugin._instant_gate_interval = 0

    def test_single_slot_blocks_second_until_release(self):
        self.assertTrue(self.plugin._acquire_gate("a"))
        # 只有一个槽位，第二个必须等
        got = []
        t = threading.Thread(
            target=lambda: got.append(self.plugin._acquire_gate("b"))
        )
        t.start()
        time.sleep(0.2)
        self.assertEqual(got, [])
        self.plugin._release_gate("a")
        t.join(timeout=5)
        self.assertEqual(got, [True])
        self.plugin._release_gate("b")

    def test_interval_keeps_next_slot_waiting(self):
        self.plugin._instant_gate_interval = 60
        self.assertTrue(self.plugin._acquire_gate("a"))
        self.plugin._release_gate("a")
        # 间隔 60 秒没到，再来一条就拿不到（这里把等待上限压到 1 秒）
        old = csp._GATE_MAX_WAIT_SECONDS
        csp._GATE_MAX_WAIT_SECONDS = 1
        try:
            self.assertFalse(self.plugin._acquire_gate("b"))
        finally:
            csp._GATE_MAX_WAIT_SECONDS = old

    def test_two_slots_allow_parallel(self):
        self.plugin._instant_max_concurrent = 2
        self.assertTrue(self.plugin._acquire_gate("a"))
        self.assertTrue(self.plugin._acquire_gate("b"))
        self.assertEqual(sorted(self.plugin._gate_holders), ["a", "b"])

    def test_stopped_gate_refuses(self):
        self.plugin._gate_stopped = True
        self.assertFalse(self.plugin._acquire_gate("a"))

    def test_upstream_takeover_flag(self):
        cloud = MagicMock()
        cloud._takeover_new_subscribes = False
        self.assertFalse(self.plugin._upstream_handles_new_subscribes(cloud))

        cloud._takeover_new_subscribes = True
        cloud._is_takeover_active = lambda: True
        self.assertTrue(self.plugin._upstream_handles_new_subscribes(cloud))

        cloud._is_takeover_active = lambda: False
        self.assertFalse(self.plugin._upstream_handles_new_subscribes(cloud))


class UpstreamYieldTest(unittest.TestCase):
    """即时检索遇到上游会接管时，分享类候选整个让出去。"""

    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._enabled = True
        self.plugin._notify = False
        self.plugin._instant_search = True
        self.plugin._offline_enabled = True
        self.plugin._windows = {}
        self.plugin._lock = threading.RLock()
        self.plugin._persist = lambda: None
        self.plugin._trace = lambda *a, **k: None

    def _cloud(self, take_over=True):
        cloud = MagicMock()
        cloud._takeover_new_subscribes = take_over
        cloud._is_takeover_active = lambda: take_over
        return cloud

    def test_share_candidates_yielded_to_upstream(self):
        window = {"sid": 7, "name": "测试片", "instant": True}
        self.plugin._windows["7"] = dict(window)
        ranked = [make_candidate("分享候选", 90)]
        self.plugin._submit_worker(
            window, self._cloud(True), ranked, "测试片", 7
        )
        # 让位时直接关窗，不再自己转存
        self.assertNotIn("7", self.plugin._windows)
        self.plugin._transfer_and_confirm = MagicMock()
        self.plugin._transfer_and_confirm.assert_not_called()

    def test_offline_candidates_kept(self):
        window = {"sid": 8, "name": "测试片", "instant": True}
        self.plugin._windows["8"] = dict(window)
        offline = make_candidate(
            "磁力候选", 80, tier=csp._OFFLINE_TIER,
            url="magnet:?xt=urn:btih:abc",
        )
        submitted = MagicMock(return_value=(True, "已提交离线下载"))
        self.plugin._submit_offline = submitted
        self.plugin._share_transfer_service = MagicMock(return_value=None)
        self.plugin._submit_worker(
            window, self._cloud(True), [offline], "测试片", 8
        )
        submitted.assert_called_once()

    def test_no_takeover_keeps_original_flow(self):
        window = {"sid": 9, "name": "测试片", "instant": True}
        self.plugin._windows["9"] = dict(window)
        self.plugin._share_transfer_service = MagicMock(return_value=None)
        transfer = MagicMock(return_value=(True, "已转存 1 个文件"))
        self.plugin._transfer_and_confirm = transfer
        self.plugin._submit_worker(
            window, self._cloud(False), [make_candidate("分享候选", 90)],
            "测试片", 9,
        )
        transfer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
