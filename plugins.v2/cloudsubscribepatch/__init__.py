"""网盘订阅助手 · 智能下载策略补丁。

背景
----
PT 站的新片首发资源质量普遍一般（多为 1080P WEB-DL 甚至 HDTV），
几小时后网盘渠道往往会出现 4K / REMUX / 原盘之类更好的版本。
原版「网盘订阅助手」只有 allow / cloud / block 三种平台下载策略，
`allow` 只在插件自己已经建了网盘离线任务时才拦 PT，
没法表达「先等网盘一段时间，等不到再走 PT」这种需求。

本插件补上的能力
----------------
1. PT 资源即将下载时先拦下（不建种、不占下载器），并为该订阅开一个等待窗口；
2. 等待窗口内定时搜索网盘渠道，按画质/来源/音轨等打分，记录最优候选；
   需积分解锁的资源（如 Dian115）也按标题/描述参与打分，
   但只有排到第一时才交给同步链去解锁，不会为了比价白花积分；
   Dian115 上「免费但需要点一次领取」的资源同样进候选（见下文层级 1）；
3. 窗口到期时：
   - 找到了合格网盘资源 → 交给「网盘订阅助手」提交转存；
   - 没找到 → 放行并重新触发该订阅的 PT 搜索；
4. 全程不修改原插件任何文件，只挂事件与独立定时服务。

候选优先级
----------
同一轮里按「无需解锁的网盘资源 > 需解锁的网盘资源 > PT 资源」三级排序：
网盘候选先比这一层级，再比画质评分；只要窗口内有任何网盘候选，
就不会放行 PT。层级只看「能不能直接拿到链接」：网盘分享直链＝0，
需积分解锁（need_unlock）或免费待领取（need_access）＝1，
磁力/ed2k 这类要下载器去拖的＝2。
评分仍按画质/来源/中字/音轨累积。

分享链接有效性
--------------
pansou 这类聚合源给的都是别人的第三方分享，等待期内经常被取消或删除，
等到窗口到期才提交，同步链只会记一句「分享链接无效」然后整批失败。
所以巡检时会向网盘端查一次链接状态，失效的直接剔除、不再占 best/ranked
名额；提交前还会再确认一遍，死链就顺位到下一个候选。
同一链接 6 小时内只查一次，单轮最多查 5 个（115 有 QPS 限制）。

只对「网盘订阅助手」纳管的订阅生效（沿用它的 include/exclude 名单），
两个插件任一未启用时本插件自动放行，不做任何拦截。
"""

import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.interval import IntervalTrigger

from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.plugin import PluginManager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MediaType

# 本插件等待窗口的持久化 key
_DATA_KEY = "windows"
# 「已经放弃网盘、交回 PT」的订阅：持久化 key 与放行时长
_RELEASED_KEY = "pt_released"
_PT_RELEASE_HOLD = 6 * 60 * 60   # 交回 PT 后 6 小时内不再重开窗口拦它

# 拦 PT 用的事件优先级：必须小于「网盘订阅助手」的默认值 10，才能先跑
_CHAIN_PRIORITY = 1

# 分享链接有效性校验：pansou 这类聚合源上的第三方分享常在等待期内被取消，
# 到期才提交会白发一次请求，所以巡检与提交前都要先问一次网盘端。
_SHARE_CHECK_TTL = 6 * 60 * 60   # 同一链接 6 小时内不重复校验
_SHARE_CHECK_BUDGET = 5          # 单次巡检最多校验 5 个链接（115 有 QPS 限制）

# 提交 + 等同步链回结果可能要几十秒到几分钟；这段时间用 submitting_at
# 压住重复提交，超时视为线程已死，允许重来。
_SUBMIT_STALE_SECONDS = 2 * 60 * 60

# 订阅刚添加时的一次性即时检索（不等 PT 首发、也不占等待窗口）。
# 事件里可能同时收到同步与异步两次广播，用这个窗口做幂等。
_INSTANT_DEDUP_SECONDS = 10 * 60

# 链接寿命：pansou 这类聚合源给的都是别人转发的第三方分享，24 小时内
# 常被取消；dian115 解锁出来的链接、平台自有链接要持久得多。
# 排序时把「短效直链」压到「需解锁网盘」后面——宁可花一次积分等个稳的，
# 也别好不容等到期才发现链接早被人删了。
_FRAGILE_SOURCES = frozenset({"pansou"})
_FRAGILE_RANK = 1.9              # 落在「需解锁网盘(1)」与「PT(2)」之间的档位

# 提交后确认：网盘订阅助手的 start_selected_resources 只是把候选塞进队列就
# 返回 success，真正的转存失败（分享在提交瞬间被人取消）发生在几十秒之后。
# 所以提交改走 sync_subscribes 同步跑完，用真实转存数判断成败；
# 全失败时窗口还有时间就留着等下一轮，没时间就真把订阅交回 PT。
# 注意：交回 PT 之后必须留一段「放行期」，否则 PT 刚下就被自己重新开窗拦回去。

# 画质/来源排序权重（数值越大越好）
_RESOLUTION_SCORE = {"4K": 40, "1080P": 25, "720P": 10}
_SOURCE_SCORE = {
    "原盘ISO": 100,
    "REMUX": 90,
    "BluRay": 70,
    "WEB-DL": 60,
    "WebRip": 50,
    "HDTV": 30,
}

# 多数字幕/音轨信息靠标题识别，这里保留最小可用的一组正则；
# 若「网盘订阅助手」在线，会优先复用它的 extract_resource_tags。
_RE_4K = re.compile(r"(?:^|[\s\[\.\(])(?:4K|2160P|UHD)(?:$|[\s\]\.\)])", re.I)
_RE_1080P = re.compile(r"(?:^|[\s\[\.\(])(?:1080P|1080I|FHD)(?:$|[\s\]\.\)])", re.I)
_RE_720P = re.compile(r"(?:^|[\s\[\.\(])720P(?:$|[\s\]\.\)])", re.I)
_RE_NO_SUB = re.compile(r"无(?:中文字幕|中字|字幕)|RAW|生肉", re.I)
_RE_CHS = re.compile(r"中字|内嵌|简繁|双语|中英|\bCHS\b|\bCHT\b", re.I)
_RE_MANDARIN = re.compile(r"国语|国配|国粤", re.I)
_RE_DV = re.compile(r"\bDV\b|DOLBY\s*VISION|杜比视界", re.I)
_RE_REMUX = re.compile(r"REMUX", re.I)
_RE_ISO = re.compile(r"\.ISO\b", re.I)
_RE_BLURAY = re.compile(r"(?:^|[\s\[\.\(])(?:BDMV|BLURAY|BLU-RAY)(?:$|[\s\]\.\)])", re.I)
_RE_WEB = re.compile(r"WEB-?DL|WEBRIP", re.I)


def _local_tags(title: str) -> List[str]:
    """兜底标签提取，结构对齐 cloudsubscribe 的 extract_resource_tags。"""
    text = str(title or "")
    tags: List[str] = []
    if _RE_4K.search(text):
        tags.append("4K")
    elif _RE_1080P.search(text):
        tags.append("1080P")
    elif _RE_720P.search(text):
        tags.append("720P")
    if _RE_ISO.search(text):
        tags.append("原盘ISO")
    elif _RE_REMUX.search(text):
        tags.append("REMUX")
    elif _RE_BLURAY.search(text):
        tags.append("BluRay")
    elif _RE_WEB.search(text):
        tags.append("WEB-DL" if re.search(r"WEB-?DL", text, re.I) else "WebRip")
    if _RE_DV.search(text):
        tags.append("杜比视界")
    if _RE_NO_SUB.search(text):
        tags.append("无中字")
    elif _RE_CHS.search(text):
        tags.append("中字")
    if _RE_MANDARIN.search(text):
        tags.append("国语")
    return tags


def _extract_tags(title: str) -> List[str]:
    """优先复用网盘订阅助手的标签提取，失败则用本地兜底。"""
    try:
        from app.plugins.cloudsubscribe.search.matching import (
            extract_resource_tags,
        )

        tags = extract_resource_tags(title)
        if tags:
            return list(tags)
    except Exception:
        pass
    return _local_tags(title)


class CloudSubscribePatch(_PluginBase):
    """网盘订阅助手 · 智能下载策略补丁。"""

    # 插件名称
    plugin_name = "网盘订阅智能下载补丁"
    # 插件描述
    plugin_desc = (
        "给「网盘订阅助手」补一层智能下载策略：PT 资源先拦下，"
        "在设定时间内监控网盘渠道并按画质择优，超时未找到再放行走 PT。"
    )
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    plugin_version = "1.1.8"
    # 插件作者
    plugin_author = "Zlmetal"
    # 作者主页
    author_url = "https://github.com/Zlmetal"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudsubscribepatch_"
    # 加载顺序（晚于网盘订阅助手的 21，保证能拿到它的实例）
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # ---- 运行时状态 ----
    _enabled: bool = False
    _wait_hours: float = 24.0
    _check_interval_minutes: int = 60
    _submit_policy: str = "best_at_expiry"
    _exclude_no_chinese_sub: bool = True
    _exclude_dolby_vision: bool = False
    _require_mandarin: bool = False
    _min_resolution: str = "any"
    _notify: bool = True
    _dry_run: bool = False
    _prefer_stable_links: bool = True
    _instant_search: bool = True

    def __init__(self):
        super().__init__()
        self._windows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        # 分享链接校验缓存：url -> (校验时间, 是否有效)
        self._share_checked: Dict[str, Tuple[float, bool]] = {}
        self._share_budget = _SHARE_CHECK_BUDGET
        # 已交回 PT 的订阅：sid -> 放行截止时间戳
        self._released: Dict[str, float] = {}
        # 刚添加订阅的即时检索任务：sid -> {sid, started_at, finished_at}
        # 只存内存，重启丢失无妨（重启后 MP 的自动搜索会走常规等待流程）
        self._instant_tasks: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: Optional[dict] = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._wait_hours = self._as_float(config.get("wait_hours"), 24.0)
        self._check_interval_minutes = max(
            5, self._as_int(config.get("check_interval_minutes"), 60)
        )
        self._submit_policy = str(
            config.get("submit_policy") or "best_at_expiry"
        ).strip()
        if self._submit_policy not in {"immediate", "best_at_expiry"}:
            self._submit_policy = "best_at_expiry"
        self._exclude_no_chinese_sub = bool(
            config.get("exclude_no_chinese_sub", True)
        )
        self._exclude_dolby_vision = bool(config.get("exclude_dolby_vision"))
        self._require_mandarin = bool(config.get("require_mandarin"))
        self._min_resolution = str(config.get("min_resolution") or "any").strip()
        if self._min_resolution not in {"any", "1080P", "4K"}:
            self._min_resolution = "any"
        self._allow_unlock = bool(config.get("allow_unlock", True))
        self._max_unlock_points = max(
            0, self._as_int(config.get("max_unlock_points"), 0)
        )
        self._prefer_stable_links = bool(config.get("prefer_stable_links", True))
        self._instant_search = bool(config.get("instant_search", True))
        self._notify = bool(config.get("notify", True))
        self._dry_run = bool(config.get("dry_run"))

        # 载入持久化的等待窗口
        saved = self.get_data(_DATA_KEY)
        released = self.get_data(_RELEASED_KEY)
        with self._lock:
            self._windows = dict(saved) if isinstance(saved, dict) else {}
            self._released = (
                {str(k): float(v) for k, v in released.items() if v}
                if isinstance(released, dict) else {}
            )
            # 放行期早过了的就没必要再记着
            now = time.time()
            self._released = {
                key: until for key, until in self._released.items() if until > now
            }

        if self._enabled:
            logger.info(
                f"【网盘订阅智能下载补丁】已启用：等待 {self._wait_hours} 小时，"
                f"每 {self._check_interval_minutes} 分钟巡检，"
                f"策略 {self._submit_policy}，"
                f"当前等待窗口 {len(self._windows)} 个"
                + ("，干跑模式" if self._dry_run else "")
            )
        else:
            logger.info("【网盘订阅智能下载补丁】未启用")

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self):
        """插件停用/重载时由框架调用。"""
        logger.debug("【网盘订阅智能下载补丁】服务停止")

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        wait_options = [
            {"title": "2 小时", "value": 2},
            {"title": "6 小时", "value": 6},
            {"title": "12 小时", "value": 12},
            {"title": "24 小时（推荐）", "value": 24},
            {"title": "36 小时", "value": 36},
            {"title": "48 小时", "value": 48},
            {"title": "72 小时", "value": 72},
        ]
        interval_options = [
            {"title": "15 分钟", "value": 15},
            {"title": "30 分钟", "value": 30},
            {"title": "60 分钟（推荐）", "value": 60},
            {"title": "120 分钟", "value": 120},
            {"title": "180 分钟", "value": 180},
        ]
        policy_options = [
            {"title": "窗口结束时取最优", "value": "best_at_expiry"},
            {"title": "找到即提交（更快）", "value": "immediate"},
        ]
        resolution_options = [
            {"title": "不限", "value": "any"},
            {"title": "至少 1080P", "value": "1080P"},
            {"title": "只要 4K", "value": "4K"},
        ]
        points_options = [
            {"title": "不限", "value": 0},
            {"title": "10 积分以内", "value": 10},
            {"title": "30 积分以内", "value": 30},
            {"title": "50 积分以内", "value": 50},
            {"title": "100 积分以内", "value": 100},
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dry_run",
                                            "label": "干跑模式",
                                            "hint": "只记录决策、不真正拦 PT 也不提交转存，用来先观察判断是否准确",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "wait_hours",
                                            "label": "等待时长",
                                            "items": wait_options,
                                            "hint": "PT 资源被拦下后，最多等这么久再决定",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "check_interval_minutes",
                                            "label": "巡检间隔",
                                            "items": interval_options,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "submit_policy",
                                            "label": "择优策略",
                                            "items": policy_options,
                                            "hint": "窗口结束时取最优＝整段窗口都等满；找到即提交＝一旦有合格资源马上转存",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "exclude_no_chinese_sub",
                                            "label": "排除无中文字幕",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "exclude_dolby_vision",
                                            "label": "排除杜比视界",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "require_mandarin",
                                            "label": "必须有国语配音",
                                            "hint": "开启后只选带「国语 / 国配 / 国粤」字样的资源，条件较严",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "min_resolution",
                                            "label": "最低分辨率",
                                            "items": resolution_options,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_unlock",
                                            "label": "允许花积分解锁候选",
                                            "hint": "关闭后未解锁资源只参与比价、永远不会被选；开启则只有排到第一时才解锁",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "max_unlock_points",
                                            "label": "单次解锁积分上限",
                                            "items": points_options,
                                            "hint": "超过这个积分数的需解锁资源直接跳过，避免为了一部片花太多积分",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "prefer_stable_links",
                                            "label": "优先稳定链接（第三方短效分享排到需解锁网盘之后）",
                                            "hint": "pansou 这类聚合源给的第三方分享常在等待期内被取消；开启后它们排在需解锁网盘之后，宁可花一次积分解锁个稳的",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "instant_search",
                                            "label": "订阅新增时立即检索网盘",
                                            "hint": "添加订阅后马上搜一次网盘：已有资源就按评分直接转存最优候选、不再等 PT 首发也不用等满等待窗口；"
                                                    "没搜到才回到上面的等待对比流程。剧集按集处理，已入库的集不重复追",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "本插件只对「网盘订阅助手」纳管的订阅生效，"
                                                "不改动原插件任何文件。等待期间该订阅的 PT 下载会被取消，"
                                                "订阅状态保持追更。\n"
                                                "候选按「无需解锁网盘 > 需解锁网盘 > 短效第三方分享 > PT」排序，"
                                                "需解锁资源先按标题/描述参与比价，排到第一才解锁；"
                                                "Dian115 上免费待领取的资源也算中间层候选。\n"
                                                "开启「订阅新增时立即检索网盘」后，新加的订阅会先即时搜一次："
                                                "已有网盘资源就按评分直接转存最优候选，不必等 PT 首发；"
                                                "没搜到才进入上面的等待对比流程。剧集按集粒度处理，"
                                                "已入库的集不重复追，后续新集仍走等待对比。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "dry_run": False,
            "wait_hours": 24,
            "check_interval_minutes": 60,
            "submit_policy": "best_at_expiry",
            "exclude_no_chinese_sub": True,
            "exclude_dolby_vision": False,
            "require_mandarin": False,
            "min_resolution": "any",
            "allow_unlock": True,
            "max_unlock_points": 0,
            "prefer_stable_links": True,
            "instant_search": True,
        }

    def get_page(self) -> Optional[List[dict]]:
        """详情页：列出当前等待窗口与已选中的最优候选。"""
        rows = []
        now = time.time()
        with self._lock:
            for item in list(self._windows.values()):
                expire_at = float(item.get("expire_at") or 0)
                remain = max(0.0, expire_at - now)
                best = item.get("best") or {}
                rows.append(
                    str(item.get("name") or item.get("sid"))
                    + f"：剩余 {remain / 3600:.1f} 小时，"
                    + (
                        f"最优候选「{best.get('title')}」"
                        f"（{best.get('tier_text') or '网盘'}，评分 {best.get('score')}）"
                        if best
                        else "尚未找到网盘候选"
                    )
                )
        text = "\n".join(rows) if rows else "当前没有等待中的订阅"
        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "text-body-2"},
                        "text": text,
                    }
                ],
            }
        ]

    def get_api(self) -> List[dict]:
        return [
            {
                "path": "/status",
                "endpoint": self._api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看等待窗口",
                "description": "返回当前所有等待网盘资源的订阅与最优候选",
            },
            {
                "path": "/check_now",
                "endpoint": self._api_check_now,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即巡检一次",
                "description": "手动触发一次网盘资源巡检",
            },
            {
                "path": "/handlers",
                "endpoint": self._api_handlers,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看事件订阅状态",
                "description": "返回本插件关注的广播事件在事件总线里的注册与启停状态",
            },
        ]

    def _api_status(self) -> dict:
        with self._lock:
            return {
                "success": True,
                "data": {
                    "enabled": self._enabled,
                    "dry_run": self._dry_run,
                    "wait_hours": self._wait_hours,
                    "prefer_stable_links": self._prefer_stable_links,
                    "windows": list(self._windows.values()),
                },
            }

    def _api_check_now(self) -> dict:
        self._monitor_once()
        return {"success": True, "message": "巡检已执行"}

    def _api_handlers(self) -> dict:
        """
        回显事件总线里本插件相关 handler 的注册状态，用于排查事件不触发。
        """
        try:
            items = eventmanager.visualize_handlers()
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"取事件订阅失败：{err}"}
        wanted = {
            "subscribe.added",
            "resource.download",
        }
        rows = [
            item
            for item in items
            if item.get("event_type") in wanted
            and "cloudsubscribepatch" in str(item.get("handler_identifier", "")).lower()
        ]
        return {
            "success": True,
            "data": {
                "instant_search": self._instant_search,
                "handlers": rows,
            },
        }

    # ------------------------------------------------------------------
    # 定时服务
    # ------------------------------------------------------------------
    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [
            {
                "id": "CloudSubscribePatch_Monitor",
                "name": "网盘等待窗口巡检",
                "trigger": IntervalTrigger(minutes=self._check_interval_minutes),
                "func": self._monitor_once,
                "kwargs": {},
            }
        ]

    # ------------------------------------------------------------------
    # 事件：拦下 PT 下载
    # ------------------------------------------------------------------
    @eventmanager.register(ChainEventType.ResourceDownload, priority=_CHAIN_PRIORITY)
    def on_resource_download(self, event: Event):
        if not self._enabled or self._dry_run:
            return
        event_data = getattr(event, "event_data", None)
        if not event_data or getattr(event_data, "cancel", False):
            return
        try:
            self._handle_resource_download(event_data)
        except Exception as error:
            logger.error(f"【网盘订阅智能下载补丁】处理下载事件异常：{error}")

    # ------------------------------------------------------------------
    # 事件：新订阅一加进来就先搜一次网盘
    # ------------------------------------------------------------------
    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        """订阅新增即刻检索：有货就直接转存，没货才等 PT 首发走等待对比。

        老片/补片这类订阅往往一加就命中网盘，没必要先等 PT 首发再等满整个
        等待窗口；而真正还没资源的，这里搜不到就什么都不做，等 PT 首次命中时
        照旧开等待窗口比对。
        """
        logger.info(
            f"【网盘订阅智能下载补丁】收到订阅新增事件：enabled={self._enabled} "
            f"dry_run={self._dry_run} instant={self._instant_search}"
        )
        if not self._enabled or self._dry_run or not self._instant_search:
            return
        event_data = getattr(event, "event_data", None) or {}
        sid = 0
        try:
            raw = event_data.get("subscribe_id") or event_data.get("id")
            if not raw and isinstance(event_data.get("subscribe"), dict):
                raw = event_data["subscribe"].get("id")
            sid = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0:
            return
        try:
            self._handle_subscribe_added(sid)
        except Exception as error:
            logger.error(f"【网盘订阅智能下载补丁】处理新增订阅事件异常：{error}")

    def _handle_subscribe_added(self, sid: int) -> None:
        cloud = self._cloud_plugin()
        if not cloud:
            logger.info("【网盘订阅智能下载补丁】网盘订阅助手未运行，跳过即时检索")
            return
        if not self._is_managed(cloud, sid):
            logger.info(
                f"【网盘订阅智能下载补丁】订阅 {sid} 不在助手纳管范围，跳过即时检索"
            )
            return
        key = str(sid)
        # 已经交回 PT 的订阅，放行期内不抢先；让它按 PT 流程走
        if self._is_released(key):
            return
        now = time.time()
        with self._lock:
            task = self._instant_tasks.get(key) or {}
            started = float(task.get("started_at") or 0)
            if started and now - started < _INSTANT_DEDUP_SECONDS:
                logger.debug(
                    f"【网盘订阅智能下载补丁】订阅 {sid} 刚做过即时检索，跳过重复广播"
                )
                return
            self._instant_tasks[key] = {"sid": sid, "started_at": now}
        threading.Thread(
            target=self._instant_worker,
            args=(sid,),
            daemon=True,
            name=f"cloudsubscribe-patch-instant-{sid}",
        ).start()

    def _instant_worker(self, sid: int) -> None:
        """新增订阅的即时检索线程：搜网盘 → 按评分+缺集覆盖排序 → 直接提交。"""
        key = str(sid)
        name = key
        try:
            cloud = self._cloud_plugin()
            if not cloud:
                return
            subscribe = self._get_subscribe(sid)
            if not subscribe:
                logger.info(
                    f"【网盘订阅智能下载补丁】订阅 {sid} 已不存在，跳过即时检索"
                )
                return
            name = str(getattr(subscribe, "name", "") or key)
            media_type = self._media_type(getattr(subscribe, "type", None))
            if media_type is None:
                return
            season = int(getattr(subscribe, "season", 1) or 1)
            # 剧集只追还没入库的集：已入库的集不再重复请求网盘
            # None = 缺集算不出来（没有媒体库也没有订阅记录），按全集检索兜底
            missing: Optional[List[int]] = None
            if media_type == MediaType.TV:
                missing = self._instant_missing_episodes(cloud, subscribe)
                if missing is not None and not missing:
                    logger.info(
                        f"【网盘订阅智能下载补丁】{name} 当前没有缺集，跳过即时检索"
                    )
                    return
            window = {
                "sid": sid,
                "name": name,
                "type": media_type.value,
                "season": season,
                "episodes": missing or None,
                "instant": True,
                "opened_by": "订阅新增即时检索",
            }
            candidates = self._search_cloud(cloud, subscribe, window)
            # 磁力/PT 类候选本来就能直接下，即时检索只处理网盘
            candidates = [
                item for item in candidates
                if int(item.get("tier") if item.get("tier") is not None else 2) < 2
            ]
            if not candidates:
                logger.info(
                    f"【网盘订阅智能下载补丁】{name} 即时检索未搜到网盘资源，"
                    f"等 PT 首发后走等待对比"
                )
                return
            self._share_budget = _SHARE_CHECK_BUDGET
            alive = self._verify_candidates(cloud, candidates)
            if not alive:
                logger.info(
                    f"【网盘订阅智能下载补丁】{name} 即时检索到的分享均已失效，"
                    f"等 PT 首发后走等待对比"
                )
                return
            if media_type == MediaType.TV and missing:
                alive = self._order_by_episode_coverage(alive, season, missing)
                if not alive:
                    logger.info(
                        f"【网盘订阅智能下载补丁】{name} 搜到的资源都不覆盖当前缺集，"
                        f"等 PT 首发后走等待对比"
                    )
                    return
            ranked = alive[:5]
            best = ranked[0]
            logger.info(
                f"【网盘订阅智能下载补丁】{name} 订阅新增即时检索命中："
                f"{best.get('title')}（{best.get('tier_text')}，评分 {best.get('score')}），直接提交"
            )
            self._submit_worker(window, cloud, list(ranked), name, sid)
        except Exception as error:
            logger.error(f"【网盘订阅智能下载补丁】{name} 即时检索异常：{error}")
        finally:
            with self._lock:
                task = self._instant_tasks.get(key)
                if task is not None:
                    # 留着 started_at 做 10 分钟幂等，别让同步/异步双播重复搜一次
                    task["finished_at"] = time.time()
                    self._instant_tasks[key] = task

    def _instant_missing_episodes(
            self, cloud: Any, subscribe: Any
    ) -> Optional[List[int]]:
        """取订阅当前还没入库的集号。

        助手的 `_subscribe_missing_episodes` 在「算不出总集」和「一集不缺」时
        都返回空列表，两者分不开——照它返回空当「不用搜了」，遇到没有媒体库
        记录的新订阅就会把即时检索整个跳掉。这里自己算，只用两个明确信号：
        总集数取自订阅记录，已入库集优先读媒体库、没有媒体库记录时退回订阅
        自身的集数备注。返回 ``None`` 表示连总集都不知道，按全集检索。
        """
        try:
            season = int(getattr(subscribe, "season", 0) or 1)
            total = int(getattr(subscribe, "total_episode", 0) or 0)
            if total <= 0:
                return None
            start = max(1, int(getattr(subscribe, "start_episode", 1) or 1))
            expected = set(range(start, total + 1))
            mediainfo = cloud._sync_handler._subscribe_mediainfo(
                subscribe, MediaType.TV
            )
            existing: set = set()
            try:
                library = cloud._library_episodes(mediainfo, season)
            except Exception:
                library = None
            if library:
                existing = {int(e) for e in library if int(e) > 0}
            else:
                raw_note = getattr(subscribe, "note", None) or []
                if isinstance(raw_note, str):
                    raw_note = re.findall(r"\d+", raw_note)
                existing = {
                    int(value) for value in raw_note if str(value).isdigit()
                }
            return sorted(expected - existing)
        except Exception as error:
            logger.debug(
                f"【网盘订阅智能下载补丁】缺集计算失败，按全集检索：{error}"
            )
            return None

    def _order_by_episode_coverage(
            self, candidates: List[Dict[str, Any]], season: int, missing: List[int]
    ) -> List[Dict[str, Any]]:
        """剧集专用排序：先按缺集覆盖，再按原有评分/档位，且剔掉完全不覆盖的。"""
        targets = {int(e) for e in (missing or []) if int(e) > 0}
        if not targets:
            return candidates
        try:
            from app.plugins.cloudsubscribe.search.matching import (
                missing_episode_coverage,
            )
        except Exception:
            # 拿不到覆盖计算就按原顺序走，别把能转的资源挡掉
            return candidates
        scored: List[Tuple[int, int, Tuple, Dict[str, Any]]] = []
        for item in candidates:
            rank, covered = 1, set()
            try:
                rank, covered = missing_episode_coverage(
                    item.get("resource") or {}, season, targets
                )
            except Exception:
                rank, covered = 1, set()
            # 2 = 明确不覆盖任何缺集，转存纯属白跑
            if rank >= 2:
                continue
            scored.append((rank, -len(covered), self._candidate_sort_key(item), item))
        scored.sort(key=lambda entry: entry[:3])
        return [entry[3] for entry in scored]

    def _handle_resource_download(self, event_data: Any) -> None:
        cloud = self._cloud_plugin()
        if not cloud:
            return
        origin = str(getattr(event_data, "origin", "") or "")
        subscribe_info = self._parse_subscribe(origin)
        if not subscribe_info:
            return
        sid = int(subscribe_info.get("id") or 0)
        if sid <= 0:
            return
        if not self._is_managed(cloud, sid):
            return

        context = getattr(event_data, "context", None)
        meta = getattr(context, "meta_info", None) if context else None
        title = ""
        if context is not None:
            torrent_info = getattr(context, "torrent_info", None)
            title = str(getattr(torrent_info, "title", "") or "")

        episodes = sorted(int(e) for e in (getattr(event_data, "episodes", None) or []) if e)
        media_type = str(getattr(meta, "type", "") or "") or subscribe_info.get("type") or ""
        season = int(getattr(meta, "begin_season", 0) or subscribe_info.get("season") or 1)

        key = str(sid)
        now = time.time()
        # 已经交回 PT 的订阅：放行期内一律不拦、不再开新窗口
        if self._is_released(key):
            logger.debug(
                f"【网盘订阅智能下载补丁】{subscribe_info.get('name') or key} "
                f"已交回 PT，放行期内不再拦截：{title}"
            )
            return
        with self._lock:
            window = self._windows.get(key)
            if window:
                expire_at = float(window.get("expire_at") or 0)
                if now < expire_at:
                    # 窗口还没结束 → 继续拦
                    remain_hours = (expire_at - now) / 3600
                    event_data.cancel = True
                    event_data.source = "CloudSubscribePatch-等待网盘资源"
                    event_data.reason = (
                        f"正在等待网盘渠道更好的版本，剩余 {remain_hours:.1f} 小时；"
                        f"被拦下的 PT 资源：{title}"
                    )
                    if self._notify:
                        logger.debug(
                            f"【网盘订阅智能下载补丁】继续拦截 {window.get('name')}：{title}"
                        )
                    return
                # 已过期但巡检还没跑到：这里有 PT 资源正等着，就地收尾
                best = window.get("best") or {}
                if best:
                    # 窗口内已经攒到更好的网盘候选 → 拦下 PT，改走网盘
                    event_data.cancel = True
                    event_data.source = "CloudSubscribePatch-等待网盘资源"
                    event_data.reason = (
                        f"等待窗口已到期，改用网盘候选：{best.get('title')}"
                    )
                    logger.info(
                        f"【网盘订阅智能下载补丁】{window.get('name')} 窗口到期，"
                        f"改用网盘候选并拦下 PT：{title}"
                    )
                    # 提交动作由 _submit_best 自己起线程，别卡住下载链
                    self._submit_best(window, cloud)
                    return
                # 没有任何网盘候选 → 放行 PT
                logger.info(
                    f"【网盘订阅智能下载补丁】{window.get('name')} 等待窗口已过期且无网盘候选，放行 PT：{title}"
                )
                self._close_window(key)
                return

        # 第一次遇到 → 开窗并拦下
        window = {
            "sid": sid,
            "name": subscribe_info.get("name") or key,
            "type": media_type,
            "season": season,
            "episodes": episodes,
            "start_at": now,
            "expire_at": now + self._wait_hours * 3600,
            "opened_by": title,
            "best": None,
            "searches": 0,
        }
        with self._lock:
            self._windows[key] = window
            self._persist()
        event_data.cancel = True
        event_data.source = "CloudSubscribePatch-等待网盘资源"
        event_data.reason = (
            f"已开启 {self._wait_hours:g} 小时网盘等待窗口，暂不下载 PT 资源：{title}"
        )
        logger.info(
            f"【网盘订阅智能下载补丁】{window['name']} 开启等待窗口 "
            f"{self._wait_hours:g} 小时，拦下 PT 资源：{title}"
        )
        if self._notify:
            self.post_message(
                mtype="订阅",
                title="【智能下载】已推迟 PT 下载",
                text=(
                    f"{window['name']}\n"
                    f"PT 资源已拦下：{title}\n"
                    f"接下来 {self._wait_hours:g} 小时内会优先找网盘更好的版本，"
                    "到期仍没有合适的再走 PT。"
                ),
            )

    # ------------------------------------------------------------------
    # 巡检
    # ------------------------------------------------------------------
    def _monitor_once(self):
        if not self._enabled:
            return
        cloud = self._cloud_plugin()
        with self._lock:
            keys = list(self._windows.keys())
        if not keys:
            return
        if not cloud:
            logger.warning("【网盘订阅智能下载补丁】未找到「网盘订阅助手」实例，本次巡检跳过")
            return

        now = time.time()
        for key in keys:
            with self._lock:
                window = self._windows.get(key)
            if not window:
                continue
            try:
                if now >= float(window.get("expire_at") or 0):
                    self._finalize(window, cloud)
                else:
                    self._scan_candidates(window, cloud)
            except Exception as error:
                logger.error(
                    f"【网盘订阅智能下载补丁】巡检 {window.get('name')} 失败：{error}"
                )

    def _scan_candidates(self, window: Dict[str, Any], cloud: Any) -> None:
        subscribe = self._get_subscribe(int(window.get("sid") or 0))
        if not subscribe:
            logger.info(
                f"【网盘订阅智能下载补丁】{window.get('name')} 订阅已不存在，关闭等待窗口"
            )
            self._close_window(str(window.get("sid")))
            return
        if str(getattr(subscribe, "state", "")) not in {"R", "P"}:
            logger.info(
                f"【网盘订阅智能下载补丁】{window.get('name')} 订阅状态为 "
                f"{getattr(subscribe, 'state', '')}，关闭等待窗口"
            )
            self._close_window(str(window.get("sid")))
            return

        candidates = self._search_cloud(cloud, subscribe, window)
        # 磁力/PT 类候选本来就能直接下，没必要等窗口、也不进备选
        candidates = [
            item for item in candidates
            if int(item.get("tier") if item.get("tier") is not None else 2) < 2
        ]
        if not candidates:
            logger.info(f"【网盘订阅智能下载补丁】{window.get('name')} 本次未搜到可用网盘资源")
            return
        # 聚合源（pansou 等）里的第三方分享经常在等待期内被取消，
        # 提前校验一遍，别让死链占住 best/ranked 的名额、白等到期
        self._share_budget = _SHARE_CHECK_BUDGET
        alive = self._verify_candidates(cloud, candidates)
        if not alive:
            logger.info(
                f"【网盘订阅智能下载补丁】{window.get('name')} 候选分享链接均已失效，本次不更新候选"
            )
            return
        best = alive[0]
        old = window.get("best") or {}
        old_dead = self._is_best_dead(cloud, old)
        improved = (
            old_dead
            or not old
            or self._candidate_sort_key(best) < self._candidate_sort_key(old)
        )
        if improved:
            window["best"] = best
            # 留一截备选：万一最优候选解锁失败，还能顺位往下试
            window["ranked"] = alive[:5]
            logger.info(
                f"【网盘订阅智能下载补丁】{window.get('name')} 记录更优候选："
                f"{best.get('title')}（{best.get('tier_text')}，评分 {best.get('score')}）"
                + ("（原候选链接已失效，已替换）" if old_dead else "")
            )
        window["searches"] = int(window.get("searches") or 0) + 1
        with self._lock:
            self._windows[str(window.get("sid"))] = window
            self._persist()

        if self._submit_policy == "immediate" and improved:
            self._submit_best(window, cloud)

    def _finalize(self, window: Dict[str, Any], cloud: Any) -> None:
        best = window.get("best") or {}
        name = window.get("name") or window.get("sid")
        if best:
            submitted = self._submit_best(window, cloud)
            if submitted:
                return
        # 没有网盘资源（或提交失败）→ 收尾并重新触发 PT 搜索
        # 必须先记放行期，否则 PT 一搜到就被本插件重新开窗拦回去
        self._mark_released(str(window.get("sid")))
        if self._notify:
            self.post_message(
                mtype="订阅",
                title="【智能下载】等待期满，回归 PT",
                text=f"{name}\n网盘渠道未找到更合适的资源，已重新触发 PT 搜索下载。",
            )
        self._trigger_pt_search(int(window.get("sid") or 0), str(name))

    def _submit_best(self, window: Dict[str, Any], cloud: Any) -> bool:
        """挑出要先试的候选并起线程去提交、等同步链回真实结果。

        返回 True 表示「已经有人接手」，调用方不要再关窗或回归 PT——
        真正的成败由提交线程拿到同步链结果后再定。
        """
        name = window.get("name") or window.get("sid")
        ranked = window.get("ranked") or []
        if not ranked:
            best = window.get("best") or {}
            ranked = [best] if best else []
        if not ranked:
            return False
        if self._dry_run:
            best = ranked[0]
            logger.info(
                f"【网盘订阅智能下载补丁】干跑：将提交 {name} → "
                f"{best.get('title')}（{best.get('tier_text')}）"
            )
            return False

        sid = int(window.get("sid") or 0)
        key = str(sid)
        now = time.time()
        with self._lock:
            current = self._windows.get(key) or {}
            started = float(current.get("submitting_at") or 0)
            if started and now - started < _SUBMIT_STALE_SECONDS:
                logger.info(
                    f"【网盘订阅智能下载补丁】{name} 已有提交任务在跑，跳过重复提交"
                )
                return True
            current["submitting_at"] = now
            self._windows[key] = current
            self._persist()

        threading.Thread(
            target=self._submit_worker,
            args=(dict(window), cloud, list(ranked), str(name), sid),
            daemon=True,
            name="cloudsubscribe-patch-submit",
        ).start()
        return True

    def _submit_worker(
            self, window: Dict[str, Any], cloud: Any,
            ranked: List[Dict[str, Any]], name: str, sid: int
    ) -> None:
        """按名次依次提交，直到同步链确认真的转存成功；全失败再决定要不要回归 PT。"""
        key = str(sid)
        instant = bool(window.get("instant"))
        service = self._share_transfer_service(cloud)
        # 每条候选到底为什么没成，最后要原样告诉用户，别只剩「转存 0 个文件」
        failures: List[str] = []
        try:
            for index, best in enumerate(ranked):
                resource = dict(best.get("resource") or {})
                if not resource:
                    continue
                # 等待期内分享可能已被取消，提交前强制重查一次（不吃 6 小时缓存）
                url = str(resource.get("url") or "").strip()
                if url and service:
                    if self._check_share_alive(service, url, name, force=True) is False:
                        logger.info(
                            f"【网盘订阅智能下载补丁】{name} 第 {index + 1} 名链接已失效，顺位下一个"
                        )
                        failures.append(f"{best.get('title')}：分享链接已失效")
                        continue
                tier = best.get("tier")
                if tier is None:
                    tier = self._candidate_tier(resource)
                if int(tier) >= 2:
                    # 磁力/PT 类候选不交给网盘链：PT 本来就能直接下
                    logger.info(
                        f"【网盘订阅智能下载补丁】{name} 跳过第 {index + 1} 名"
                        f"（{best.get('tier_text') or 'PT'}），保留 PT 直下更省事"
                    )
                    continue
                # 只有当它真排到前面、马上要提交了，才花积分/领链接；
                # 搜索与比价阶段全程不解锁。
                if best.get("need_unlock"):
                    logger.info(
                        f"【网盘订阅智能下载补丁】{name} 第 {index + 1} 名需解锁"
                        f"（{best.get('unlock_points') or 0} 积分），确认采用后才解锁："
                        f"{best.get('title')}"
                    )
                    unlocked = self._unlock_resource(cloud, best, name, free=False)
                    if not unlocked:
                        failures.append(f"{best.get('title')}：解锁未拿到链接")
                        continue
                    resource = unlocked
                elif best.get("need_access"):
                    logger.info(
                        f"【网盘订阅智能下载补丁】{name} 第 {index + 1} 名免费待领取，"
                        f"开始获取链接：{best.get('title')}"
                    )
                    # 免费待领取的候选得先在这里领一次，把 url 补上再交出去
                    claimed = self._claim_free_resource(cloud, best, name)
                    if not claimed:
                        failures.append(f"{best.get('title')}：领取未拿到链接")
                        continue
                    resource = claimed
                success, detail = self._transfer_and_confirm(cloud, sid, resource, name)
                if not success:
                    logger.warning(
                        f"【网盘订阅智能下载补丁】{name} 第 {index + 1} 名转存未成功"
                        f"（{detail}），顺位下一个：{best.get('title')}"
                    )
                    failures.append(f"{best.get('title')}：{detail}")
                    continue
                logger.info(
                    f"【网盘订阅智能下载补丁】{name} 已确认转存：{best.get('title')}"
                )
                self._close_window(key)
                if self._notify:
                    self.post_message(
                        mtype="订阅",
                        title=("【智能下载】订阅新增，已有网盘资源，已直接转存"
                               if instant else "【智能下载】已选用网盘资源"),
                        text=(
                            f"{name}\n"
                            f"资源：{best.get('title')}\n"
                            f"来源：{best.get('source') or '网盘'}"
                            f"（{best.get('tier_text') or ''}）\n"
                            f"评分：{best.get('score')}\n"
                            f"文件大小：{best.get('size_text') or '未知'}\n"
                            f"状态：{detail or '已转存'}"
                        ),
                    )
                return
            self._all_candidates_failed(key, name, sid, failures, instant=instant)
        except Exception as error:
            logger.error(f"【网盘订阅智能下载补丁】{name} 提交线程异常：{error}")
            # 异常也要收尾，否则窗口每轮巡检都会重试同一个坏候选
            failures.append(f"提交线程异常：{error}")
            self._all_candidates_failed(key, name, sid, failures, instant=instant)
        finally:
            with self._lock:
                current = self._windows.get(key)
                if current is not None:
                    current.pop("submitting_at", None)
                    self._persist()

    def _transfer_and_confirm(
            self, cloud: Any, sid: int,
            resource: Dict[str, Any], name: str
    ) -> Tuple[bool, str]:
        """把候选交给同步链并等它回来，用真实转存数判定成败。

        原插件的 `start_selected_resources` 只是把任务丢进线程池，
        它返回的 success 只代表「入队成功」——之前正是这样把死链当成功，
        通知发了、窗口关了，实际转存 0 个文件。这里改成同步等结果。
        """
        if sid <= 0:
            return False, "订阅 ID 无效"
        sync = getattr(cloud, "sync_subscribes", None)
        if not callable(sync):
            return False, "网盘订阅助手版本不支持同步执行，无法确认结果"
        snapshot: Dict[str, Any] = {}
        try:
            # manual_upgrade=False 必须显式传：带订阅 ID 的手动提交在
            # `_do_api_vue_start_manual_sync` 里会被默认注入 manual_upgrade=True，
            # 走进洗版分支——网盘里找不到旧文件时直接 return 0，
            # 既不转存也不报错，正是「转存 0 个文件」的元凶之一。
            sync(
                subscribe_id=sid,
                manual_resources=[resource],
                result=snapshot,
                wait_for_slot=True,
                manual_upgrade=False,
            )
        except Exception as error:
            return False, f"调用同步链异常：{error}"
        if not isinstance(snapshot, dict) or not snapshot:
            return False, "同步链没有返回执行结果"
        message = str(snapshot.get("message") or "").strip()
        data = snapshot.get("data") or {}
        try:
            transferred = int(data.get("transferred") or 0)
        except (TypeError, ValueError):
            transferred = 0
        if not snapshot.get("success"):
            return False, message or "同步链执行失败"
        if transferred > 0:
            return True, f"已转存 {transferred} 个文件"
        # 网盘文件先入离线/待就绪队列也属于成功，真正下载由网盘自己慢慢来
        if "已提交" in message and (
                "等待就绪" in message or "等待下载" in message or "网盘任务" in message
        ):
            return True, message
        return False, message or "未转存任何文件"

    def _all_candidates_failed(
            self, key: str, name: str, sid: int,
            failures: Optional[List[str]] = None,
            instant: bool = False
    ) -> None:
        """候选全部转存失败：窗口还有时间就清空候选等下一轮，没时间就回归 PT。

        `instant=True` 表示这次是「订阅新增即时检索」，此时等待窗口根本还没开、
        PT 也没命中过，谈不上「放弃网盘回归 PT」——什么都不做，等 PT 首次命中
        资源时再照旧开窗比对即可。
        """
        now = time.time()
        with self._lock:
            window = self._windows.get(key) or {}
            expire_at = float(window.get("expire_at") or 0)
            if window and now < expire_at:
                window["best"] = None
                window["ranked"] = []
                self._windows[key] = window
                self._persist()
            else:
                window = {}
        if window:
            logger.info(
                f"【网盘订阅智能下载补丁】{name} 候选全部转存失败，"
                f"窗口内继续等待新的网盘资源"
            )
            return
        if instant:
            detail = "；".join((failures or [])[:3])
            logger.info(
                f"【网盘订阅智能下载补丁】{name} 即时检索候选全部转存失败，"
                f"等 PT 首发后走等待对比"
                + (f"（{detail}）" if detail else "")
            )
            return
        self._mark_released(key)
        detail_lines = "\n".join(f"· {item}" for item in (failures or [])[:5])
        if self._notify:
            self.post_message(
                mtype="订阅",
                title="【智能下载】网盘候选转存失败，回归 PT",
                text=(
                    f"{name}\n"
                    f"已放弃网盘渠道并重新触发 PT 搜索下载。\n"
                    + (f"各候选失败原因：\n{detail_lines}" if detail_lines else "窗口内没有可用候选。")
                ),
            )
        self._trigger_pt_search(sid, name)

    def _claim_free_resource(
            self, cloud: Any, best: Dict[str, Any], name: str
    ) -> Optional[Dict[str, Any]]:
        """兼容旧名：免费待领取与需积分解锁都走同一套换链接流程。"""
        return self._unlock_resource(cloud, best, name, free=True)

    def _unlock_resource(
            self, cloud: Any, best: Dict[str, Any], name: str,
            free: bool = False
    ) -> Optional[Dict[str, Any]]:
        """把「还没有真实链接」的网盘候选换成可转存的直链。

        两种候选都会走到这里：
          * need_access   —— 免费待领取，unlock_points <= 0
          * need_unlock   —— 需要积分解锁，unlock_points > 0
        只有排到第一名、真要提交了才调它，搜索/比价阶段全程不解锁
        （所以每次等待窗口只会消耗第一名的积分，不会为陪跑的候选白花钱）。

        原插件 1.1.4 在这里只对 need_access 调渠道接口，need_unlock 只打一行
        「开始解锁」的日志就往下走，候选带着空 url 进同步链，在
        `_resolve_candidate_resource_url` 里被 `can_unlock` / 预算检查挡掉，
        最终表现为「转存 0 个文件」——这就是 1.1.5 要修的第二个坑。
        """
        resource = dict(best.get("resource") or {})
        source = str(resource.get("source") or best.get("source") or "").strip()
        if not source or not resource:
            return None
        label = "领取" if free else "解锁"
        try:
            search_handler = cloud._search_handler
            if not search_handler.can_unlock(source):
                logger.warning(
                    f"【网盘订阅智能下载补丁】{name} 渠道 {source} 不支持{label}，跳过"
                )
                return None
            unlocked = search_handler.unlock_resource(
                source, resource, search_label=f"智能下载补丁·{name}"
            )
        except Exception as error:
            logger.error(
                f"【网盘订阅智能下载补丁】{name} {label}资源失败：{error}"
            )
            return None
        url = ""
        if isinstance(unlocked, str):
            url = unlocked.strip()
        elif isinstance(unlocked, dict):
            url = str(unlocked.get("url") or "").strip()
        if not url:
            logger.warning(
                f"【网盘订阅智能下载补丁】{name} {label}未拿到链接：{best.get('title')}"
            )
            return None
        resource["url"] = url
        resource["need_access"] = False
        resource["need_unlock"] = False
        resource["is_unlocked"] = True
        logger.info(
            f"【网盘订阅智能下载补丁】{name} 已{label}拿到链接：{best.get('title')}"
        )
        return resource

    def _trigger_pt_search(self, sid: int, name: str) -> None:
        if sid <= 0:
            return

        def _run():
            try:
                logger.info(f"【网盘订阅智能下载补丁】重新触发 PT 搜索：{name}")
                SubscribeChain().search(sid=sid, state="R")
            except Exception as error:
                logger.error(f"【网盘订阅智能下载补丁】重触发 PT 搜索失败：{error}")

        threading.Thread(target=_run, name=f"csp-pt-{sid}", daemon=True).start()

    # ------------------------------------------------------------------
    # 网盘搜索与打分
    # ------------------------------------------------------------------
    def _search_cloud(
            self, cloud: Any, subscribe: Any, window: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        media_type = self._media_type(getattr(subscribe, "type", None))
        if media_type is None:
            return []
        mediainfo = cloud._sync_handler._subscribe_mediainfo(subscribe, media_type)
        if not mediainfo:
            logger.debug(
                f"【网盘订阅智能下载补丁】{window.get('name')} 媒体识别失败，跳过本次搜索"
            )
            return []
        sources = cloud._search_handler.get_enabled_sources(media_type) or []
        if not sources:
            return []
        season = int(window.get("season") or getattr(subscribe, "season", 1) or 1)
        target_episodes = window.get("episodes") or None
        if media_type == MediaType.TV and not target_episodes:
            try:
                target_episodes = sorted(
                    int(e) for e in (cloud._subscribe_missing_episodes(subscribe, mediainfo) or [])
                ) or None
            except Exception:
                target_episodes = None
        results = cloud._search_handler.search_sources(
            sources=sources,
            mediainfo=mediainfo,
            media_type=media_type,
            season=season if media_type == MediaType.TV else None,
            target_episodes=target_episodes,
            subscribe=subscribe,
            # 列表模式：不做平台规则过滤、也不因「未解锁」被剔除，
            # 让需积分解锁的资源也能按标题/描述参与比价
            apply_platform_rules=False,
        )
        scored: List[Dict[str, Any]] = []
        stats: Dict[str, Any] = {}
        for source, items in (results or {}).items():
            total = len(items or [])
            kept = 0
            for item in items or []:
                candidate = self._score_candidate(source, item)
                if candidate:
                    kept += 1
                    scored.append(candidate)
            stats[source] = {"raw": total, "kept": kept}
        scored.sort(key=self._candidate_sort_key)
        # 把每个渠道的原始/入选数量挂在窗口上，方便在 /status 里排查
        window["source_stats"] = stats
        if stats:
            logger.info(
                f"【网盘订阅智能下载补丁】{window.get('name')} 渠道统计："
                + "，".join(
                    f"{source} {value['raw']}→{value['kept']}"
                    for source, value in stats.items()
                )
            )
        return scored

    @staticmethod
    def _candidate_tier(item: Any) -> int:
        """候选层级：0=直接可用的网盘直链，1=需解锁网盘，2=磁力/PT 资源。

        只看「能不能直接拿到链接」：磁力、ed2k 属于要下载器去拖的 PT 类资源，
        网盘分享链接才是直链；没有直链但可花积分解锁的算中间层。
        """
        if not isinstance(item, dict):
            return 2
        resource_type = str(item.get("resource_type") or "").strip().lower()
        if resource_type in {"magnet", "ed2k", "torrent"}:
            return 2
        url = str(item.get("url") or "").strip().lower()
        if url.startswith("magnet:") or url.startswith("ed2k://"):
            return 2
        if url:
            return 0
        # need_unlock=花积分解锁；need_access=免费但要点开页面领取，
        # 两者都没有直链、都要先走一次解锁链，同属中间层
        return 1 if (item.get("need_unlock") or item.get("need_access")) else 2

    def _candidate_rank(self, candidate: Dict[str, Any]) -> float:
        """候选的排序档位（浮点）：0=直链，1=需解锁，1.9=短效直链，2=PT.

        短效直链（pansou 这类聚合源转发的第三方分享）虽然能直接拿到链接，
        但常在等待期内被人取消；把它压在「需解锁网盘」之后——
        宁可花一次积分换个稳的，也别等到期才发现链接早没了。
        关掉「优先稳定链接」开关后，短效直链恢复成普通直链（0）。
        """
        rank = candidate.get("rank")
        if rank is not None:
            try:
                return float(rank)
            except (TypeError, ValueError):
                pass
        tier = candidate.get("tier")
        if tier is None:
            tier = 2
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            tier = 2
        return float(tier)

    def _candidate_sort_key(self, candidate: Dict[str, Any]) -> tuple:
        """先按档位、再按评分——档位压过分数，保证「无需解锁 > 需解锁 > PT」."""
        return (self._candidate_rank(candidate), -float(candidate.get("score") or 0))

    # ------------------------------------------------------------------
    # 分享链接有效性
    # ------------------------------------------------------------------
    def _share_transfer_service(self, cloud: Any) -> Optional[Any]:
        """取网盘客户端的分享能力（能查链接状态的那一层）。"""
        holders = [getattr(cloud, "_sync_handler", None), cloud]
        for holder in holders:
            if holder is None:
                continue
            service = getattr(holder, "_share_transfer", None)
            if service is not None and hasattr(service, "check_share_status"):
                return service
        # 兜底：直接翻当前网盘提供方注册的能力表（不依赖枚举导入路径）
        provider = getattr(cloud, "_cloud_drive", None)
        services = getattr(provider, "services", None)
        try:
            for service in (services or {}).values():
                if hasattr(service, "check_share_status"):
                    return service
        except Exception:
            pass
        return None

    def _check_share_alive(
            self, service: Any, share_url: str, name: str = "",
            force: bool = False
    ) -> Optional[bool]:
        """查一个分享链接还在不在：True 有效 / False 失效 / None 查不了。

        `force=True` 时不吃缓存——巡检留存的那份「有效」可能已经是几小时前的
        结论，提交前必须重新问一次网盘端，否则会把早被删掉的链接交出去。
        """
        url = str(share_url or "").strip()
        if not url or not service:
            return None
        now = time.time()
        cached = self._share_checked.get(url)
        if not force and cached and now - cached[0] < _SHARE_CHECK_TTL:
            return cached[1]
        try:
            status = service.check_share_status(url)
        except Exception as error:
            logger.debug(f"【网盘订阅智能下载补丁】校验分享失败 {url}：{error}")
            return None
        is_valid = bool(getattr(status, "is_valid", False))
        reason = str(getattr(status, "status_text", "") or "")
        if not is_valid:
            logger.info(
                f"【网盘订阅智能下载补丁】{name or ''} 候选链接已失效"
                f"（{reason}）：{url.split('?')[0]}"
            )
        self._share_checked[url] = (now, is_valid)
        return is_valid

    def _verify_candidates(
            self, cloud: Any, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """剔掉分享链接已失效的候选；需解锁/需领取的没有直链，跳过校验。

        只为带 url 的候选查一次，且单轮有次数上限（115 有 QPS 限制）。
        """
        service = self._share_transfer_service(cloud)
        if not service:
            return candidates
        alive: List[Dict[str, Any]] = []
        for item in candidates:
            resource = item.get("resource") or {}
            url = str(resource.get("url") or "").strip()
            if not url:
                alive.append(item)
                continue
            budget = getattr(self, "_share_budget", _SHARE_CHECK_BUDGET)
            if budget <= 0:
                alive.append(item)
                continue
            self._share_budget = budget - 1
            state = self._check_share_alive(service, url, str(item.get("title") or ""))
            if state is False:
                continue
            alive.append(item)
        return alive

    def _is_best_dead(self, cloud: Any, best: Dict[str, Any]) -> bool:
        """当前记录的最优候选链接是否已经失效。"""
        if not best:
            return False
        service = self._share_transfer_service(cloud)
        url = str((best.get("resource") or {}).get("url") or "").strip()
        if not service or not url:
            return False
        return self._check_share_alive(service, url, str(best.get("title") or "")) is False

    def _score_candidate(self, source: str, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        url = str(item.get("url") or "").strip()
        has_ref = bool(item.get("resource_ref"))
        # need_unlock=花积分解锁；need_access=免费领取（也要点一次页面才拿到链接）
        unlock = bool(item.get("need_unlock") and has_ref)
        claim = bool(item.get("need_access") and has_ref)
        if not url and not unlock and not claim:
            return None
        # 未解锁资源：只在允许解锁时才进候选，且不超过积分上限
        if unlock or claim:
            if not self._allow_unlock:
                return None
            points = self._as_int(item.get("unlock_points"), 0)
            if self._max_unlock_points > 0 and points > self._max_unlock_points:
                return None
        title = str(item.get("title") or "")
        description = str(item.get("description") or "")
        # 未解锁资源只能靠标题+描述判断画质，描述里常带完整文件名
        tags = _extract_tags(f"{title} {description}".strip())
        upper_tags = {str(t) for t in tags}

        # 排除规则
        if self._exclude_no_chinese_sub and "无中字" in upper_tags:
            return None
        if self._exclude_dolby_vision and "杜比视界" in upper_tags:
            return None
        if self._require_mandarin and "国语" not in upper_tags:
            return None
        if self._min_resolution == "1080P" and "720P" in upper_tags:
            return None
        if self._min_resolution == "4K" and "4K" not in upper_tags:
            return None

        score = 0.0
        for tag, value in _RESOLUTION_SCORE.items():
            if tag in upper_tags:
                score += value
                break
        for tag, value in _SOURCE_SCORE.items():
            if tag in upper_tags:
                score += value
                break
        if "中字" in upper_tags:
            score += 15
        if "国语" in upper_tags:
            score += 10
        if "杜比视界" in upper_tags:
            score += 8
        if "杜比全景声" in upper_tags:
            score += 5

        size = item.get("size") or 0
        try:
            size_bytes = int(size)
        except (TypeError, ValueError):
            size_bytes = 0
        if size_bytes > 0:
            # 同档位下体积越大通常码率越高，最多再给 5 分
            score += min(5.0, size_bytes / (1024 ** 3) / 10.0)

        source_name = str(item.get("source") or source or "")
        tier = self._candidate_tier({**item, "source": source_name})
        # 短效直链：聚合源转发的第三方分享，能直接拿到链接但寿命短
        fragile = bool(
            self._prefer_stable_links
            and tier == 0
            and source_name.strip().lower() in _FRAGILE_SOURCES
        )
        rank = _FRAGILE_RANK if fragile else float(tier)
        if fragile:
            tier_text = "网盘直链（短效分享）"
        elif tier == 1:
            tier_text = "需解锁网盘" if unlock else "免费领取网盘"
        else:
            tier_text = {0: "网盘直链", 2: "PT / 其他"}.get(tier, "其他")
        return {
            "title": title,
            "source": source_name,
            "tier": tier,
            "rank": round(rank, 2),
            "fragile": fragile,
            "tier_text": tier_text,
            "score": round(score, 2),
            "tags": tags,
            "size_text": self._size_text(size_bytes),
            "need_unlock": unlock,
            "need_access": claim,
            "unlock_points": int(item.get("unlock_points") or 0),
            "resource": dict(item),
            "found_at": time.time(),
        }

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _cloud_plugin(self) -> Optional[Any]:
        """取「网盘订阅助手」插件实例。"""
        plugin = PluginManager()._running_plugins.get("CloudSubscribe")
        if not plugin:
            return None
        if not getattr(plugin, "get_state", lambda: False)():
            return None
        return plugin

    def _is_managed(self, cloud: Any, sid: int) -> bool:
        """沿用网盘订阅助手的纳管判定。"""
        try:
            return not bool(cloud._is_subscribe_excluded(sid))
        except Exception:
            return True

    @staticmethod
    def _parse_subscribe(origin: str) -> Optional[dict]:
        try:
            return SubscribeChain.parse_subscribe_source_keyword(origin)
        except Exception:
            return None

    def _get_subscribe(self, sid: int):
        if sid <= 0:
            return None
        try:
            return SubscribeOper().get(sid)
        except Exception as error:
            logger.debug(f"【网盘订阅智能下载补丁】读取订阅 {sid} 失败：{error}")
            return None

    @staticmethod
    def _media_type(value: Any) -> Optional[MediaType]:
        if isinstance(value, MediaType):
            return value
        text = str(value or "")
        if text == MediaType.TV.value:
            return MediaType.TV
        if text == MediaType.MOVIE.value:
            return MediaType.MOVIE
        return None

    def _close_window(self, key: str) -> None:
        with self._lock:
            self._windows.pop(key, None)
            self._persist()

    def _persist(self) -> None:
        """调用方需已持有锁。"""
        snapshot = {}
        for key, item in self._windows.items():
            value = dict(item)
            snapshot[key] = value
        released = {key: float(until) for key, until in self._released.items()}
        try:
            self.save_data(_DATA_KEY, snapshot)
            self.save_data(_RELEASED_KEY, released)
        except Exception as error:
            logger.debug(f"【网盘订阅智能下载补丁】持久化等待窗口失败：{error}")

    def _mark_released(self, key: str) -> None:
        """关窗并记下「这个订阅已经交回 PT」，放行期内不再重开等待窗口。

        没有这段放行期的话，交回 PT 后重新触发的搜索会在几秒内再次触发
        ResourceDownload 事件，本插件看窗口已关就立刻又开一个新窗口把它拦回去，
        结果就是「通知说回归 PT，实际永远下不成」。
        """
        with self._lock:
            self._windows.pop(key, None)
            self._released[key] = time.time() + _PT_RELEASE_HOLD
            self._persist()

    def _is_released(self, key: str) -> bool:
        with self._lock:
            until = float(self._released.get(key) or 0)
            if until <= 0:
                return False
            if time.time() >= until:
                self._released.pop(key, None)
                return False
            return True

    @staticmethod
    def _size_text(size_bytes: int) -> str:
        if not size_bytes:
            return ""
        size = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.2f}{unit}" if unit != "B" else f"{int(size)}B"
            size /= 1024
        return ""

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
