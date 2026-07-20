import re
import time
from typing import Any, Dict, List, Tuple, Optional, Type

import requests
from pydantic import BaseModel, Field

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


# ==================== 智能体工具 ====================

class HaiSouSearchInput(BaseModel):
    """海搜搜索输入参数"""
    keyword: str = Field(..., description="搜索关键词，如电影名、剧名等")
    platform: str = Field(default="115", description="网盘平台，默认115")


class HaiSouSearchTool:
    """海搜115资源搜索工具 - 供MP智能体调用"""
    name: str = "haisou_search"
    description: str = (
        "从海搜网站(haisou.cc)搜索115网盘资源。"
        "当用户想要搜索电影、电视剧、动漫等影视资源的115网盘链接时使用此工具。"
        "返回搜索结果包含标题、文件大小、文件数、分享链接和提取码。"
    )
    args_schema: Type[BaseModel] = HaiSouSearchInput

    _session_id: Optional[str] = None
    _user_id: Optional[str] = None
    _channel: Optional[str] = None
    _source: Optional[str] = None
    _username: Optional[str] = None

    def _get_plugin(self):
        """通过PluginManager获取插件实例"""
        try:
            from app.core.plugin import PluginManager
            pm = PluginManager()
            plugin = pm.get_plugin_instance("HaiSou115Mod")
            return plugin
        except Exception as e:
            logger.error(f"[115海搜] 获取插件实例失败: {e}")
            return None

    def get_tool_message(self, **kwargs) -> Optional[str]:
        keyword = kwargs.get("keyword", "")
        return f"正在从海搜搜索115资源: {keyword}"

    async def run(self, keyword: str, platform: str = "115", **kwargs) -> str:
        try:
            plugin = self._get_plugin()
            if not plugin:
                return "插件未初始化，无法执行搜索"

            result = plugin._search_resources(keyword, page=1, page_size=10)

            if not result.get("success"):
                return f"搜索失败: {result.get('msg', '未知错误')}"

            items = result.get("items", [])
            if not items:
                return f"未找到与 '{keyword}' 相关的115网盘资源"

            lines = [f"找到 {len(items)} 个115网盘资源:\n"]
            for i, item in enumerate(items, 1):
                title = plugin._clean_html(item.get("share_name", "未知"))
                size_bytes = item.get("stat_size", 0) or 0
                file_count = item.get("stat_file", 0) or 0
                share_code = item.get("share_code", "")
                share_pwd = item.get("share_pwd", "")

                size_str = plugin._format_size(size_bytes)

                share_url = f"https://115.com/s/{share_code}"
                if share_pwd:
                    share_url += f"?password={share_pwd}"

                lines.append(f"{i}. {title}")
                lines.append(f"   大小: {size_str} | 文件数: {file_count}")
                lines.append(f"   链接: {share_url}")
                if share_pwd:
                    lines.append(f"   提取码: {share_pwd}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[115海搜] 智能体搜索异常: {e}")
            return f"搜索异常: {str(e)}"


class HaiSou115Mod(_PluginBase):
    """
    115海搜转存插件
    从海搜网站搜索115网盘资源并自动转存
    """

    # 插件名称
    plugin_name = "115海搜转存"
    # 插件描述
    plugin_desc = "从海搜网站搜索115网盘资源，支持消息交互选择并自动转存"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Zlmetal"
    # 作者主页
    author_url = "https://github.com/Zlmetal"
    # 插件配置项ID前缀
    plugin_config_prefix = "haisou115mod_"
    # 加载顺序
    plugin_order = 50
    # 可使用的用户级别
    auth_level = 1

    # 运行时状态
    _enabled = False
    _cookie = ""

    # 海搜API基础URL
    _haisou_base_url = "https://haisou.cc"

    # 用户搜索结果缓存 (userid -> search results)
    _search_cache: Dict[str, Any] = {}

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cookie = config.get("cookie") or ""
        logger.info(f"[115海搜] 插件初始化完成，启用状态: {self._enabled}")

    def get_state(self) -> bool:
        """返回插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令"""
        return [
            {
                "cmd": "/hs",
                "event": EventType.PluginAction,
                "desc": "海搜115资源",
                "category": "插件命令",
                "data": {
                    "action": "haisou_search",
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """获取API接口"""
        return [
            {
                "path": "/search",
                "endpoint": self._api_search,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "搜索115资源",
                "description": "通过海搜搜索115网盘资源",
            },
            {
                "path": "/validate_cookie",
                "endpoint": self._api_validate_cookie,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "验证Cookie有效性",
                "description": "检查海搜Cookie是否有效",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页JSON和默认配置"""
        return [
            {
                "component": "VForm",
                "content": [
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
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            }
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cookie",
                                            "label": "海搜Cookie",
                                            "placeholder": "请粘贴从浏览器获取的海搜网站Cookie",
                                            "hint": "登录haisou.cc后，从浏览器开发者工具中复制Cookie（留空也可用，有每日次数限制）",
                                            "persistent-hint": True,
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
            "cookie": "",
        }

    def get_page(self) -> List[dict]:
        """返回详情页JSON - 检查Cookie有效性"""
        cookie_valid = False
        cookie_msg = "未配置Cookie（匿名模式，有每日次数限制）"

        if self._cookie:
            cookie_valid, cookie_msg = self._check_cookie_valid()

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if cookie_valid else ("warning" if not self._cookie else "error"),
                    "variant": "tonal",
                    "text": f"Cookie状态: {cookie_msg}",
                },
            },
        ]

    def get_agent_tools(self) -> List[Type]:
        """注册智能体工具"""
        return [HaiSouSearchTool]

    def stop_service(self):
        """停用插件"""
        self._search_cache.clear()
        logger.info("[115海搜] 插件已停止")

    # ==================== 核心功能 ====================

    def _get_headers(self) -> dict:
        """获取请求头"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://haisou.cc/",
            "Origin": "https://haisou.cc",
            "Content-Type": "application/json",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def _check_cookie_valid(self) -> Tuple[bool, str]:
        """检查Cookie是否有效"""
        if not self._cookie:
            return False, "未配置Cookie（匿名模式，有每日次数限制）"

        try:
            headers = self._get_headers()
            resp = requests.get(
                f"{self._haisou_base_url}/api/v2/users/me",
                headers=headers,
                timeout=10,
            )
            logger.debug(f"[115海搜] Cookie验证响应: {resp.status_code}")

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    user_info = data.get("data", {})
                    username = user_info.get("username", "未知")
                    membership = user_info.get("membership_type", "")
                    return True, f"Cookie有效 - 用户: {username} ({membership})"
                else:
                    error = data.get("error", {})
                    return False, f"Cookie无效: {error.get('message', '未知错误')}"
            else:
                return False, f"请求失败: HTTP {resp.status_code}"
        except Exception as e:
            logger.error(f"[115海搜] Cookie验证异常: {e}")
            return False, f"验证失败: {str(e)}"

    def _search_resources(self, keyword: str, page: int = 1, page_size: int = 10) -> dict:
        """搜索海搜资源"""
        try:
            headers = self._get_headers()

            # 海搜v2搜索参数 - scope必须是 "title" 或 "files"，不能用 "all"
            payload = {
                "query": keyword,
                "filters": {
                    "scope": "title",
                    "platforms": ["115"],
                },
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                },
            }

            logger.info(f"[115海搜] 搜索关键词: {keyword}, 页码: {page}")

            resp = requests.post(
                f"{self._haisou_base_url}/api/v2/shares/search",
                headers=headers,
                json=payload,
                timeout=60,
            )

            logger.debug(f"[115海搜] 搜索响应: {resp.status_code}")

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    items = data.get("data", {}).get("items", [])
                    pagination = data.get("data", {}).get("pagination", {})
                    logger.info(f"[115海搜] 搜索成功，找到 {len(items)} 个结果")
                    return {"success": True, "items": items, "pagination": pagination}
                else:
                    error = data.get("error", {})
                    msg = error.get("message", "未知错误")
                    logger.warning(f"[115海搜] 搜索失败: {msg}")
                    return {"success": False, "msg": msg}
            else:
                logger.error(f"[115海搜] 搜索请求失败: HTTP {resp.status_code}")
                return {"success": False, "msg": f"请求失败: HTTP {resp.status_code}"}

        except Exception as e:
            logger.error(f"[115海搜] 搜索异常: {e}")
            return {"success": False, "msg": f"搜索失败: {str(e)}"}

    def _get_share_detail(self, hsid: str) -> dict:
        """获取分享详情"""
        try:
            headers = self._get_headers()

            resp = requests.get(
                f"{self._haisou_base_url}/api/v2/shares/{hsid}",
                headers=headers,
                timeout=30,
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return {"success": True, "data": data.get("data", {})}
                else:
                    return {"success": False, "msg": data.get("error", {}).get("message", "获取失败")}
            else:
                return {"success": False, "msg": f"请求失败: HTTP {resp.status_code}"}

        except Exception as e:
            logger.error(f"[115海搜] 获取分享详情异常: {e}")
            return {"success": False, "msg": f"获取失败: {str(e)}"}

    def _transfer_to_115(self, share_url: str, share_pwd: str, target_dir: str = "") -> dict:
        """调用p115strmhelper的API进行转存"""
        try:
            from app.core.config import settings

            base_url = f"http://127.0.0.1:{settings.PORT}"
            api_key = settings.API_KEY

            # 调用p115strmhelper的添加分享转存接口
            params = {
                "share_url": share_url,
                "share_pwd": share_pwd,
            }
            if target_dir:
                params["target_dir"] = target_dir

            logger.info(f"[115海搜] 调用转存接口: {share_url}")

            resp = requests.get(
                f"{base_url}/api/v1/plugin/P115StrmHelper/add_transfer_share",
                headers={"Authorization": f"Bearer {api_key}"},
                params=params,
                timeout=60,
            )

            if resp.status_code == 200:
                result = resp.json()
                logger.info(f"[115海搜] 转存结果: {result}")
                return result
            else:
                logger.error(f"[115海搜] 转存请求失败: HTTP {resp.status_code}")
                return {"code": -1, "msg": f"转存请求失败: HTTP {resp.status_code}"}

        except Exception as e:
            logger.error(f"[115海搜] 调用p115strmhelper转存异常: {e}")
            return {"code": -1, "msg": f"转存失败: {str(e)}"}

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        if not size_bytes:
            return "未知"
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f} KB"
        else:
            return f"{size_bytes} B"

    def _clean_html(self, text: str) -> str:
        """清理HTML标签"""
        if not text:
            return "未知标题"
        return re.sub(r'<[^>]+>', '', text)

    # ==================== 消息交互 ====================

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        """处理远程命令"""
        event_data = event.event_data or {}
        if event_data.get("action") != "haisou_search":
            return

        # 获取用户信息
        channel = event_data.get("channel")
        source = event_data.get("source")
        user = event_data.get("user")
        text = event_data.get("text", "") or ""

        logger.info(f"[115海搜] 收到搜索命令, 原始text: '{text}', event_data keys: {list(event_data.keys())}")

        # 提取搜索关键词 - 尝试多种方式
        keyword = ""

        # 方式1: 直接从text中去掉命令前缀
        if text:
            keyword = text.replace("/hs", "").strip()

        # 方式2: 从event_data的其他字段获取
        if not keyword:
            keyword = event_data.get("keyword", "") or event_data.get("query", "") or event_data.get("content", "") or ""

        # 方式3: 从整个event_data中查找可能的关键词
        if not keyword:
            for key in ["message", "msg", "input", "search_keyword", "name", "title"]:
                val = event_data.get(key, "")
                if val and "/hs" not in str(val):
                    keyword = str(val).strip()
                    break

        logger.info(f"[115海搜] 提取到的关键词: '{keyword}'")

        if not keyword:
            self.post_message(
                channel=channel,
                title="115海搜",
                text="请输入搜索关键词，格式: /hs 电影名",
                userid=user,
            )
            return

        # 执行搜索
        self._do_search_and_respond(keyword, channel, source, user)

    @eventmanager.register(EventType.MessageAction)
    def handle_message_action(self, event: Event):
        """处理消息按钮回调"""
        event_data = event.event_data or {}
        if not event_data:
            return

        # 检查是否为本插件的回调
        plugin_id = event_data.get("plugin_id")
        if plugin_id != self.__class__.__name__:
            return

        text = event_data.get("text", "")
        channel = event_data.get("channel")
        source = event_data.get("source")
        userid = event_data.get("userid")

        logger.info(f"[115海搜] 收到回调: {text}")

        # 处理不同的回调
        if text.startswith("hs_select_"):
            # 用户选择了某个搜索结果
            try:
                index = int(text.replace("hs_select_", ""))
                self._handle_select_result(index, channel, source, userid)
            except (ValueError, IndexError) as e:
                logger.error(f"[115海搜] 处理选择回调异常: {e}")
        elif text.startswith("hs_confirm_"):
            # 用户确认转存
            cache_key = text.replace("hs_confirm_", "")
            self._handle_confirm_transfer(cache_key, channel, source, userid)
        elif text.startswith("hs_page_"):
            # 翻页
            try:
                page = int(text.replace("hs_page_", ""))
                cache_key = f"{userid}_keyword"
                keyword = self._search_cache.get(cache_key, "")
                if keyword:
                    self._do_search_and_respond(keyword, channel, source, userid, page)
            except (ValueError, TypeError) as e:
                logger.error(f"[115海搜] 处理翻页回调异常: {e}")
        elif text == "hs_cancel":
            # 取消操作
            self.post_message(
                channel=channel,
                title="115海搜",
                text="已取消操作",
                userid=userid,
            )

    def _do_search_and_respond(self, keyword: str, channel, source, user, page: int = 1):
        """执行搜索并返回结果"""
        # 发送搜索中提示
        self.post_message(
            channel=channel,
            title="115海搜",
            text=f"正在搜索: {keyword} ...",
            userid=user,
        )

        # 执行搜索
        result = self._search_resources(keyword, page=page)

        if not result.get("success"):
            self.post_message(
                channel=channel,
                title="115海搜",
                text=f"搜索失败: {result.get('msg', '未知错误')}",
                userid=user,
            )
            return

        items = result.get("items", [])
        pagination = result.get("pagination", {})

        if not items:
            self.post_message(
                channel=channel,
                title="115海搜",
                text=f"未找到与 '{keyword}' 相关的115资源",
                userid=user,
            )
            return

        # 缓存搜索结果
        cache_key = f"{user}_results"
        self._search_cache[cache_key] = items
        self._search_cache[f"{user}_keyword"] = keyword

        # 格式化搜索结果
        total = pagination.get("total", len(items))
        result_text = f"搜索结果 (第{page}页，共{total}条):\n\n"
        for i, item in enumerate(items, 1):
            title = self._clean_html(item.get("share_name", "未知标题"))
            size_bytes = item.get("stat_size", 0) or 0
            file_count = item.get("stat_file", 0) or 0
            share_code = item.get("share_code", "")
            share_pwd = item.get("share_pwd", "")

            size_str = self._format_size(size_bytes)

            result_text += f"{i}. {title}\n"
            result_text += f"   大小: {size_str} | 文件数: {file_count}\n"
            if share_pwd:
                result_text += f"   提取码: {share_pwd}\n"
            result_text += "\n"

        result_text += "请回复数字选择要转存的资源 (如: 1)"

        # 构建按钮（仅在支持按钮回调的渠道显示）
        buttons = []
        if channel and channel.lower() in ["telegram", "slack"]:
            button_row = []
            for i in range(1, min(len(items) + 1, 6)):
                button_row.append({
                    "text": f"{i}",
                    "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_select_{i}",
                })
                if len(button_row) == 3:
                    buttons.append(button_row)
                    button_row = []
            if button_row:
                buttons.append(button_row)

            # 添加翻页和取消按钮
            pagination_row = []
            if page > 1:
                pagination_row.append({
                    "text": "上一页",
                    "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_page_{page - 1}",
                })
            has_next = pagination.get("has_next", False)
            if has_next:
                pagination_row.append({
                    "text": "下一页",
                    "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_page_{page + 1}",
                })
            pagination_row.append({
                "text": "取消",
                "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_cancel",
            })
            if pagination_row:
                buttons.append(pagination_row)

        self.post_message(
            channel=channel,
            title="115海搜",
            text=result_text,
            userid=user,
            buttons=buttons if buttons else None,
        )

    def _handle_select_result(self, index: int, channel, source, user):
        """处理用户选择搜索结果"""
        cache_key = f"{user}_results"
        items = self._search_cache.get(cache_key, [])

        if index < 1 or index > len(items):
            self.post_message(
                channel=channel,
                title="115海搜",
                text="无效的选择，请重新输入",
                userid=user,
            )
            return

        item = items[index - 1]
        title = self._clean_html(item.get("share_name", "未知标题"))
        share_code = item.get("share_code", "")
        share_pwd = item.get("share_pwd", "")
        hsid = item.get("hsid", "")

        if not share_code:
            self.post_message(
                channel=channel,
                title="115海搜",
                text="未获取到有效的分享码",
                userid=user,
            )
            return

        # 构造115分享链接
        share_url = f"https://115.com/s/{share_code}"
        if share_pwd:
            share_url += f"?password={share_pwd}"

        # 发送确认信息
        confirm_text = f"即将转存:\n\n"
        confirm_text += f"标题: {title}\n"
        confirm_text += f"链接: {share_url}\n"
        if share_pwd:
            confirm_text += f"提取码: {share_pwd}\n"
        confirm_text += f"\n确认转存?"

        # 缓存分享信息
        cache_key = f"{user}_share_{hsid}"
        self._search_cache[cache_key] = {
            "share_url": share_url,
            "share_pwd": share_pwd,
            "title": title,
            "hsid": hsid,
        }

        # 构建确认按钮
        buttons = []
        if channel and channel.lower() in ["telegram", "slack"]:
            buttons = [
                [
                    {
                        "text": "确认转存",
                        "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_confirm_{hsid}",
                    },
                    {
                        "text": "取消",
                        "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_cancel",
                    },
                ]
            ]

        self.post_message(
            channel=channel,
            title="115海搜",
            text=confirm_text,
            userid=user,
            buttons=buttons if buttons else None,
        )

    def _handle_confirm_transfer(self, cache_key: str, channel, source, user):
        """处理确认转存"""
        share_data = self._search_cache.get(f"{user}_share_{cache_key}", {})

        if not share_data:
            self.post_message(
                channel=channel,
                title="115海搜",
                text="分享信息已过期，请重新搜索",
                userid=user,
            )
            return

        share_url = share_data.get("share_url", "")
        share_pwd = share_data.get("share_pwd", "")
        title = share_data.get("title", "未知")

        # 发送转存中提示
        self.post_message(
            channel=channel,
            title="115海搜",
            text=f"正在转存: {title} ...",
            userid=user,
        )

        # 调用转存
        result = self._transfer_to_115(share_url, share_pwd)

        # 检查转存结果
        success = False
        msg = ""
        if isinstance(result, dict):
            if result.get("code") == 0 or result.get("success"):
                success = True
            else:
                msg = result.get("msg") or result.get("message") or result.get("error", {}).get("message", "未知错误")

        if success:
            self.post_message(
                channel=channel,
                title="115海搜",
                text=f"转存成功!\n\n标题: {title}\n链接: {share_url}",
                userid=user,
            )
        else:
            self.post_message(
                channel=channel,
                title="115海搜",
                text=f"转存失败: {msg or '未知错误'}\n\n请确认已安装并启用115网盘STRM助手插件",
                userid=user,
            )

        # 清理缓存
        self._search_cache.pop(f"{user}_share_{cache_key}", None)

    # ==================== API接口 ====================

    def _api_search(self, keyword: str, page: int = 1, page_size: int = 10):
        """API搜索接口"""
        return self._search_resources(keyword, page, page_size)

    def _api_validate_cookie(self):
        """API验证Cookie接口"""
        valid, msg = self._check_cookie_valid()
        return {"valid": valid, "message": msg}
