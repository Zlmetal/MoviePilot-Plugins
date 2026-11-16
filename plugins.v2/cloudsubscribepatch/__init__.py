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
3. 窗口到期时：
   - 找到了合格网盘资源 → 交给「网盘订阅助手」提交转存；
   - 没找到 → 放行并重新触发该订阅的 PT 搜索；
4. 全程不修改原插件任何文件，只挂事件与独立定时服务。

候选优先级
----------
同一轮里按「无需解锁的网盘资源 > 需解锁的网盘资源 > PT 资源」三级排序：
网盘候选先比这一层级，再比画质评分；只要窗口内有任何网盘候选，
就不会放行 PT。层级只看「能不能直接拿到链接」：网盘分享直链＝0，
需积分解锁＝1，磁力/ed2k 这类要下载器去拖的＝2。
评分仍按画质/来源/中字/音轨累积。

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
from app.schemas.types import ChainEventType, MediaType

# 本插件等待窗口的持久化 key
_DATA_KEY = "windows"

# 拦 PT 用的事件优先级：必须小于「网盘订阅助手」的默认值 10，才能先跑
_CHAIN_PRIORITY = 1

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
    plugin_version = "1.1.0"
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

    def __init__(self):
        super().__init__()
        self._windows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

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
        self._notify = bool(config.get("notify", True))
        self._dry_run = bool(config.get("dry_run"))

        # 载入持久化的等待窗口
        saved = self.get_data(_DATA_KEY)
        with self._lock:
            self._windows = dict(saved) if isinstance(saved, dict) else {}

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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "本插件只对「网盘订阅助手」纳管的订阅生效，"
                                                "不改动原插件任何文件。等待期间该订阅的 PT 下载会被取消，"
                                                "订阅状态保持追更。\n"
                                                "候选按「无需解锁网盘 > 需解锁网盘 > PT」排序，"
                                                "需解锁资源先按标题/描述参与比价，排到第一才解锁。"
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
        ]

    def _api_status(self) -> dict:
        with self._lock:
            return {
                "success": True,
                "data": {
                    "enabled": self._enabled,
                    "dry_run": self._dry_run,
                    "wait_hours": self._wait_hours,
                    "windows": list(self._windows.values()),
                },
            }

    def _api_check_now(self) -> dict:
        self._monitor_once()
        return {"success": True, "message": "巡检已执行"}

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
                    # 提交动作放到线程里，别卡住下载链
                    threading.Thread(
                        target=self._submit_best,
                        args=(window, cloud),
                        daemon=True,
                        name="cloudsubscribe-patch-submit",
                    ).start()
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
        best = candidates[0]
        old = window.get("best") or {}
        improved = not old or self._candidate_sort_key(best) < self._candidate_sort_key(old)
        if improved:
            window["best"] = best
            # 留一截备选：万一最优候选解锁失败，还能顺位往下试
            window["ranked"] = candidates[:5]
            logger.info(
                f"【网盘订阅智能下载补丁】{window.get('name')} 记录更优候选："
                f"{best.get('title')}（{best.get('tier_text')}，评分 {best.get('score')}）"
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
        self._close_window(str(window.get("sid")))
        if self._notify:
            self.post_message(
                mtype="订阅",
                title="【智能下载】等待期满，回归 PT",
                text=f"{name}\n网盘渠道未找到更合适的资源，已重新触发 PT 搜索下载。",
            )
        self._trigger_pt_search(int(window.get("sid") or 0), str(name))

    def _submit_best(self, window: Dict[str, Any], cloud: Any) -> bool:
        """提交最优候选；需解锁的资源只有排到这里才会真正花积分解锁。

        解锁失败（积分不足/节点失效）时顺位尝试备选，避免整窗白等。
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

        for index, best in enumerate(ranked):
            resource = best.get("resource") or {}
            if not resource:
                continue
            tier = best.get("tier")
            if tier is None:
                tier = self._candidate_tier(resource)
            if int(tier) >= 2:
                # 磁力/PT 类候选不交给网盘链：PT 本来就能直接下，没必要再经离线拖一遍
                logger.info(
                    f"【网盘订阅智能下载补丁】{name} 跳过第 {index + 1} 名"
                    f"（{best.get('tier_text') or 'PT'}），保留 PT 直下更省事"
                )
                continue
            if best.get("need_unlock"):
                logger.info(
                    f"【网盘订阅智能下载补丁】{name} 第一名需解锁"
                    f"（{best.get('unlock_points') or 0} 积分），开始解锁：{best.get('title')}"
                )
            try:
                result = cloud.start_selected_resources(int(window.get("sid") or 0), [resource])
            except Exception as error:
                logger.error(f"【网盘订阅智能下载补丁】提交候选失败：{error}")
                continue
            if not (isinstance(result, dict) and result.get("success")):
                logger.warning(
                    f"【网盘订阅智能下载补丁】候选 {index + 1} 提交未成功：{result}"
                )
                continue
            logger.info(
                f"【网盘订阅智能下载补丁】{name} 已提交网盘资源：{best.get('title')}"
            )
            self._close_window(str(window.get("sid")))
            if self._notify:
                self.post_message(
                    mtype="订阅",
                    title="【智能下载】已选用网盘资源",
                    text=(
                        f"{name}\n"
                        f"资源：{best.get('title')}\n"
                        f"来源：{best.get('source') or '网盘'}"
                        f"（{best.get('tier_text') or ''}）\n"
                        f"评分：{best.get('score')}\n"
                        f"文件大小：{best.get('size_text') or '未知'}"
                    ),
                )
            return True
        return False

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
        for source, items in (results or {}).items():
            for item in items or []:
                candidate = self._score_candidate(source, item)
                if candidate:
                    scored.append(candidate)
        scored.sort(key=self._candidate_sort_key)
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
        return 1 if item.get("need_unlock") else 2

    def _candidate_sort_key(self, candidate: Dict[str, Any]) -> tuple:
        """先按层级、再按评分——层级压过分数，保证「无需解锁 > 需解锁 > PT」."""
        tier = candidate.get("tier")
        if tier is None:
            tier = 2
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            tier = 2
        return (tier, -float(candidate.get("score") or 0))

    def _score_candidate(self, source: str, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        url = str(item.get("url") or "").strip()
        unlock = bool(item.get("need_unlock") and item.get("resource_ref"))
        if not url and not unlock:
            return None
        # 未解锁资源：只在允许解锁时才进候选，且不超过积分上限
        if unlock:
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

        tier = self._candidate_tier({**item, "source": item.get("source") or source})
        return {
            "title": title,
            "source": str(item.get("source") or source or ""),
            "tier": tier,
            "tier_text": {0: "网盘直链", 1: "需解锁网盘", 2: "PT / 其他"}.get(tier, "其他"),
            "score": round(score, 2),
            "tags": tags,
            "size_text": self._size_text(size_bytes),
            "need_unlock": unlock,
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
        try:
            self.save_data(_DATA_KEY, snapshot)
        except Exception as error:
            logger.debug(f"【网盘订阅智能下载补丁】持久化等待窗口失败：{error}")

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
