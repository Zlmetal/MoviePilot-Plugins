"""订阅新增即时检索的桩测试。

覆盖 m13553 需求的三条硬要求：
1. 订阅一加进来就搜网盘，命中就按评分直接转存最高分候选，不等 PT 首发；
2. 没搜到 / 搜到但全失败时，不误判成「回归 PT」——PT 还没命中过，谈不上回归；
3. 剧集按集粒度：只追当前缺集，已入库的集不重复追，不覆盖缺集的候选被剔除。

运行：python3 -m unittest tests.test_instant_search -v
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins.v2/cloudsubscribepatch/__init__.py"


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent in sys.modules:
        setattr(sys.modules[parent], child, module)
    return module


class _Member:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return other is self or other == self.value

    def __hash__(self):
        return hash(self.value)


class _Type:
    def __init__(self, value, name):
        self.value = value
        self.name = name


def install_stubs():
    """把插件依赖的 app.* 与第三方库换成占位实现。"""
    for name in ("app", "app.chain", "app.core", "app.db", "app.schemas",
                 "app.plugins"):
        _module(name)

    class _EventManager:
        def register(self, *_args, **_kwargs):
            def decorator(func):
                return func
            return decorator

    _module("app.core.event", Event=object, eventmanager=_EventManager())
    _module("app.core.config", settings=MagicMock())
    _module("app.core.plugin", PluginManager=MagicMock)
    _module("app.chain.subscribe", SubscribeChain=MagicMock)
    _module("app.db.subscribe_oper", SubscribeOper=MagicMock)
    _module("app.log", logger=MagicMock())
    _module("app.plugins", _PluginBase=type("_PluginBase", (), {
        "__init__": lambda self: None,
    }))
    _module(
        "app.schemas.types",
        ChainEventType=type("ChainEventType", (), {
            "ResourceDownload": "chain.resource_download",
        }),
        EventType=type("EventType", (), {"SubscribeAdded": "subscribe.added"}),
        MediaType=type("MediaType", (), {
            "MOVIE": _Member("movie"),
            "TV": _Member("tv"),
        }),
    )

    pytz = _module("pytz")
    pytz.timezone = lambda _name: None
    _module("apscheduler")
    _module("apscheduler.triggers")
    _module("apscheduler.triggers.interval", IntervalTrigger=MagicMock)


def load_plugin():
    install_stubs()
    name = "_csp_instant_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


csp = load_plugin()


def make_candidate(title, score, tier=0, url="https://115.com/s/x", rank=None,
                   episodes=None):
    return {
        "title": title,
        "source": "dian115",
        "tier": tier,
        "rank": rank if rank is not None else tier,
        "score": score,
        "tier_text": "网盘直链",
        "size_text": "10GB",
        "need_unlock": False,
        "need_access": False,
        "unlock_points": 0,
        "resource": {"url": url, "episodes": episodes or []},
        "found_at": 0.0,
    }


class InstantSearchTest(unittest.TestCase):
    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.plugin._enabled = True
        self.plugin._dry_run = False
        self.plugin._instant_search = True
        self.plugin._notify = False
        self.plugin._share_budget = 5
        self.plugin._share_checked = {}
        self.plugin._windows = {}
        self.plugin._released = {}
        self.plugin._instant_tasks = {}

        self.subscribe = types.SimpleNamespace(
            id=12, name="测试剧", type="电视剧", season=1, state="N",
        )
        self.missing = [3, 4]

        self.cloud = MagicMock()
        self.cloud.get_state.return_value = True
        self.cloud._is_subscribe_excluded.return_value = False
        self.plugin._cloud_plugin = lambda: self.cloud
        self.plugin._get_subscribe = lambda sid: self.subscribe
        self.plugin._media_type = lambda _type: csp.MediaType.TV
        self.plugin._instant_missing_episodes = lambda _c, _s: list(self.missing)
        self.plugin._verify_candidates = lambda _c, items: list(items)
        self.submitted = []
        self.plugin._submit_worker = (
            lambda window, cloud, ranked, name, sid:
            self.submitted.append((dict(window), list(ranked), name, sid))
        )

    def test_hit_submits_best_immediately(self):
        """命中就立刻提交最高分候选，且不进等待窗口。"""
        self.plugin._search_cloud = lambda *a, **k: [
            make_candidate("低分 1080P", 40),
            make_candidate("高分 4K", 90, episodes=[3, 4]),
        ]
        self.plugin._order_by_episode_coverage = (
            lambda items, season, missing: sorted(
                items, key=lambda i: (-i["score"],)
            )
        )
        self.plugin._instant_worker(12)

        self.assertEqual(len(self.submitted), 1)
        window, ranked, name, sid = self.submitted[0]
        self.assertTrue(window["instant"], "即时检索窗口必须打上 instant 标记")
        self.assertEqual(name, "测试剧")
        self.assertEqual(sid, 12)
        self.assertEqual(ranked[0]["title"], "高分 4K")
        self.assertEqual(self.plugin._windows, {}, "即时检索不应占用等待窗口")

    def test_miss_does_nothing(self):
        """没搜到网盘就不动作：不开窗、不回归 PT、不提交。"""
        self.plugin._search_cloud = lambda *a, **k: []
        self.plugin._trigger_pt_search = MagicMock()
        self.plugin._instant_worker(12)

        self.assertEqual(self.submitted, [])
        self.assertEqual(self.plugin._windows, {})
        self.plugin._trigger_pt_search.assert_not_called()
        self.assertEqual(self.plugin._released, {})

    def test_no_missing_episodes_skips(self):
        """剧集都入库了就一次网盘都不搜。"""
        self.plugin._instant_missing_episodes = lambda _c, _s: []
        calls = []
        self.plugin._search_cloud = lambda *a, **k: calls.append(1) or []
        self.plugin._instant_worker(12)

        self.assertEqual(calls, [], "没有缺集时不应发起检索")
        self.assertEqual(self.submitted, [])

    def test_pure_pt_candidates_filtered(self):
        """纯 PT 候选不进即时检索（它们本来就能直接下）。

        注意：磁力/电驴在开启「把磁力/电驴纳入网盘渠道」后是 tier=1.5，
        属于网盘渠道，不再被这道过滤挡掉——见 test_offline.py。
        """
        self.plugin._search_cloud = lambda *a, **k: [
            make_candidate("PT 4K", 99, tier=2, url="https://pt.example/x"),
        ]
        self.plugin._instant_worker(12)
        self.assertEqual(self.submitted, [])

    def test_instant_failure_does_not_mark_released(self):
        """即时检索候选全失败：不写放行期、不触发 PT 搜索。"""
        self.plugin._trigger_pt_search = MagicMock()
        self.plugin._all_candidates_failed(
            "12", "测试剧", 12, ["高分 4K：转存 0 个文件"], instant=True,
        )
        self.assertEqual(self.plugin._released, {})
        self.plugin._trigger_pt_search.assert_not_called()

    def test_window_failure_still_releases(self):
        """常规等待窗口全失败仍要回归 PT（不能被新分支带坏）。"""
        self.plugin._trigger_pt_search = MagicMock()
        self.plugin._all_candidates_failed(
            "12", "测试剧", 12, ["高分 4K：转存 0 个文件"],
        )
        self.assertIn("12", self.plugin._released)
        self.plugin._trigger_pt_search.assert_called_once()

    def test_dedup_within_window(self):
        """同步/异步双播同一订阅，只起一次检索线程。"""
        dispatched = []

        class _InlineThread:
            def __init__(self, target=None, args=(), **_kwargs):
                self._target = target
                self._args = args

            def start(self):
                dispatched.append(self._args)

        original = csp.threading.Thread
        csp.threading.Thread = _InlineThread
        try:
            self.plugin._handle_subscribe_added(12)
            started = self.plugin._instant_tasks["12"]["started_at"]
            self.plugin._handle_subscribe_added(12)
        finally:
            csp.threading.Thread = original
        self.assertEqual(dispatched, [(12,)])
        self.assertEqual(
            self.plugin._instant_tasks["12"]["started_at"], started
        )

    def test_released_subscribe_skipped(self):
        """放行期内的订阅不抢先，让 PT 流程走完。"""
        self.plugin._instant_worker = MagicMock()
        self.plugin._released["12"] = 9_999_999_999.0
        self.plugin._handle_subscribe_added(12)
        self.plugin._instant_worker.assert_not_called()
        self.assertEqual(self.plugin._instant_tasks, {})

    def test_event_handler_ignores_switched_off(self):
        """开关关掉后事件进来什么都不做。"""
        self.plugin._instant_search = False
        called = []
        self.plugin._handle_subscribe_added = lambda sid: called.append(sid)
        event = types.SimpleNamespace(event_data={"subscribe_id": 12})
        self.plugin.on_subscribe_added(event)
        self.assertEqual(called, [])

    def test_event_handler_accepts_alternate_payload(self):
        """事件体兼容 subscribe_id / id / subscribe.id 三种写法。"""
        for payload in ({"subscribe_id": 7}, {"id": 7}, {"subscribe": {"id": 7}}):
            seen = []
            self.plugin._handle_subscribe_added = lambda sid: seen.append(sid)
            self.plugin._instant_search = True
            self.plugin.on_subscribe_added(
                types.SimpleNamespace(event_data=payload)
            )
            self.assertEqual(seen, [7], payload)

    def test_coverage_ordering_drops_uncovered(self):
        """剧集排序：覆盖缺集的排前面，明确不覆盖的直接剔除。"""
        matching = _module("app.plugins.cloudsubscribe")
        _module("app.plugins.cloudsubscribe.search")
        _module(
            "app.plugins.cloudsubscribe.search.matching",
            missing_episode_coverage=lambda resource, season, targets: (
                (0, set(targets)) if "全" in resource["url"]
                else (2, set())
            ),
        )
        items = [
            make_candidate("不覆盖", 90, url="https://115.com/s/no"),
            make_candidate("全覆盖", 50, url="https://115.com/s/全"),
        ]
        ordered = self.plugin._order_by_episode_coverage(items, 1, [3, 4])
        self.assertEqual([i["title"] for i in ordered], ["全覆盖"])
        sys.modules.pop("app.plugins.cloudsubscribe.search.matching", None)


class InstantMissingEpisodesTest(unittest.TestCase):
    """缺集计算：别把「算不出来」当成「一集不缺」把检索跳掉。"""

    def setUp(self):
        self.plugin = csp.CloudSubscribePatch()
        self.cloud = MagicMock()
        self.cloud._sync_handler._subscribe_mediainfo.return_value = {"title": "测试剧"}

    def _subscribe(self, **kwargs):
        base = dict(id=9, name="测试剧", type="电视剧", season=1, state="N",
                    total_episode=8, start_episode=1, note=[])
        base.update(kwargs)
        return types.SimpleNamespace(**base)

    def test_library_episodes_win(self):
        """有媒体库记录时用媒体库算缺集。"""
        self.cloud._library_episodes.return_value = {1, 2, 3}
        missing = self.plugin._instant_missing_episodes(
            self.cloud, self._subscribe(note=[1, 2])
        )
        self.assertEqual(missing, [4, 5, 6, 7, 8])

    def test_note_used_without_library(self):
        """没有媒体库记录时退回订阅自身的集数备注。"""
        self.cloud._library_episodes.return_value = None
        missing = self.plugin._instant_missing_episodes(
            self.cloud, self._subscribe(note=["1", "2", "8"])
        )
        self.assertEqual(missing, [3, 4, 5, 6, 7])

    def test_note_as_string(self):
        """note 存成字符串也能解析出集号。"""
        self.cloud._library_episodes.return_value = None
        missing = self.plugin._instant_missing_episodes(
            self.cloud, self._subscribe(note="1,2")
        )
        self.assertEqual(missing, [3, 4, 5, 6, 7, 8])

    def test_all_present_returns_empty(self):
        """真的全入库了才返回空列表。"""
        self.cloud._library_episodes.return_value = set(range(1, 9))
        missing = self.plugin._instant_missing_episodes(
            self.cloud, self._subscribe()
        )
        self.assertEqual(missing, [])

    def test_unknown_total_returns_none(self):
        """连总集都不知道时返回 None，让调用方按全集检索。"""
        missing = self.plugin._instant_missing_episodes(
            self.cloud, self._subscribe(total_episode=0)
        )
        self.assertIsNone(missing)

    def test_start_episode_respected(self):
        """从中间集开始追的订阅只算后端集。"""
        self.cloud._library_episodes.return_value = set()
        missing = self.plugin._instant_missing_episodes(
            self.cloud, self._subscribe(start_episode=5, total_episode=8, note=[])
        )
        self.assertEqual(missing, [5, 6, 7, 8])

    def test_worker_searches_full_set_when_unknown(self):
        """缺集算不出来时不能跳过检索，要按全集搜一次。"""
        self.plugin._enabled = True
        self.plugin._dry_run = False
        self.plugin._instant_search = True
        self.plugin._notify = False
        self.plugin._windows = {}
        self.plugin._released = {}
        self.plugin._instant_tasks = {}
        self.plugin._cloud_plugin = lambda: self.cloud
        self.plugin._get_subscribe = lambda sid: self._subscribe()
        self.plugin._media_type = lambda _type: csp.MediaType.TV
        self.plugin._instant_missing_episodes = lambda _c, _s: None
        calls = []
        self.plugin._search_cloud = lambda *a, **k: calls.append(k) or []
        self.plugin._instant_worker(9)
        self.assertEqual(len(calls), 1, "缺集未知时必须检索一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
