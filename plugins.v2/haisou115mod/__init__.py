import json
import re
import time
from typing import Any, Dict, List, Tuple, Optional
from urllib.parse import urlencode

import requests

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


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
    plugin_version = "1.0.0"
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
                                            "hint": "登录haisou.cc后，从浏览器开发者工具中复制Cookie",
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
        cookie_msg = "未配置Cookie"

        if self._cookie:
            cookie_valid, cookie_msg = self._check_cookie_valid()

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if cookie_valid else "error",
                    "variant": "tonal",
                    "text": f"Cookie状态: {cookie_msg}",
                },
            },
        ]

    def stop_service(self):
        """停用插件"""
        self._search_cache.clear()

    # ==================== 核心功能 ====================

    def _get_headers(self) -> dict:
        """获取请求头"""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://haisou.cc/",
            "Origin": "https://haisou.cc",
            "Cookie": self._cookie,
        }

    def _check_cookie_valid(self) -> Tuple[bool, str]:
        """检查Cookie是否有效"""
        if not self._cookie:
            return False, "未配置Cookie"

        try:
            headers = self._get_headers()
            resp = requests.get(
                f"{self._haisou_base_url}/api/user/info",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    user_info = data.get("result", {})
                    username = user_info.get("username", "未知")
                    membership = user_info.get("membership", "")
                    return True, f"Cookie有效 - 用户: {username} {membership}"
                else:
                    return False, f"Cookie无效: {data.get('msg', '未知错误')}"
            else:
                return False, f"请求失败: HTTP {resp.status_code}"
        except Exception as e:
            return False, f"验证失败: {str(e)}"

    def _search_resources(self, keyword: str, page: int = 1, page_size: int = 10) -> dict:
        """搜索海搜资源"""
        if not self._cookie:
            return {"code": -1, "msg": "未配置Cookie"}

        try:
            headers = self._get_headers()

            # 海搜搜索参数
            params = {
                "keyword": keyword,
                "platform": "115",
                "page": page,
                "pageSize": page_size,
            }

            resp = requests.get(
                f"{self._haisou_base_url}/api/search",
                headers=headers,
                params=params,
                timeout=30,
            )

            if resp.status_code == 200:
                return resp.json()
            else:
                return {"code": -1, "msg": f"请求失败: HTTP {resp.status_code}"}

        except Exception as e:
            logger.error(f"海搜搜索失败: {str(e)}")
            return {"code": -1, "msg": f"搜索失败: {str(e)}"}

    def _get_share_info(self, hsid: str) -> dict:
        """获取分享链接和密码"""
        if not self._cookie:
            return {"code": -1, "msg": "未配置Cookie"}

        try:
            headers = self._get_headers()

            resp = requests.get(
                f"{self._haisou_base_url}/api/share/detail",
                headers=headers,
                params={"hsid": hsid},
                timeout=30,
            )

            if resp.status_code == 200:
                return resp.json()
            else:
                return {"code": -1, "msg": f"请求失败: HTTP {resp.status_code}"}

        except Exception as e:
            logger.error(f"获取分享信息失败: {str(e)}")
            return {"code": -1, "msg": f"获取失败: {str(e)}"}

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

            resp = requests.get(
                f"{base_url}/api/v1/plugin/P115StrmHelper/add_transfer_share",
                headers={"Authorization": f"Bearer {api_key}"},
                params=params,
                timeout=60,
            )

            if resp.status_code == 200:
                return resp.json()
            else:
                return {"code": -1, "msg": f"转存请求失败: HTTP {resp.status_code}"}

        except Exception as e:
            logger.error(f"调用p115strmhelper转存失败: {str(e)}")
            return {"code": -1, "msg": f"转存失败: {str(e)}"}

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f} KB"
        else:
            return f"{size_bytes} B"

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
        text = event_data.get("text", "")

        # 提取搜索关键词（命令后面的文本）
        keyword = text.replace("/hs", "").strip()
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

        # 处理不同的回调
        if text.startswith("hs_select_"):
            # 用户选择了某个搜索结果
            try:
                index = int(text.replace("hs_select_", ""))
                self._handle_select_result(index, channel, source, userid)
            except (ValueError, IndexError):
                pass
        elif text.startswith("hs_confirm_"):
            # 用户确认转存
            hsid = text.replace("hs_confirm_", "")
            self._handle_confirm_transfer(hsid, channel, source, userid)
        elif text.startswith("hs_page_"):
            # 翻页
            try:
                page = int(text.replace("hs_page_", ""))
                keyword = self._search_cache.get(f"{userid}_keyword", "")
                if keyword:
                    self._do_search_and_respond(keyword, channel, source, userid, page)
            except (ValueError, TypeError):
                pass
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

        if result.get("code") != 0:
            self.post_message(
                channel=channel,
                title="115海搜",
                text=f"搜索失败: {result.get('msg', '未知错误')}",
                userid=user,
            )
            return

        items = result.get("result", {}).get("items", [])
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
        result_text = f"搜索结果 (第{page}页):\n\n"
        for i, item in enumerate(items, 1):
            title = item.get("title", "未知标题")
            size_bytes = item.get("sizeBytes", 0) or item.get("size", 0) or 0
            file_count = item.get("fileCount", 0) or item.get("file_count", 0) or 0

            size_str = self._format_size(size_bytes)

            result_text += f"{i}. {title}\n"
            result_text += f"   大小: {size_str} | 文件数: {file_count}\n\n"

        result_text += "请回复数字选择要转存的资源 (如: 1)"

        # 构建按钮
        buttons = []
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
        pagination_row.append({
            "text": "下一页",
            "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_page_{page + 1}",
        })
        pagination_row.append({
            "text": "取消",
            "callback_data": f"[PLUGIN]{self.__class__.__name__}|hs_cancel",
        })
        buttons.append(pagination_row)

        self.post_message(
            channel=channel,
            title="115海搜",
            text=result_text,
            userid=user,
            buttons=buttons,
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
        title = item.get("title", "未知标题")
        hsid = item.get("hsid", "")

        # 发送获取中提示
        self.post_message(
            channel=channel,
            title="115海搜",
            text=f"正在获取分享信息: {title} ...",
            userid=user,
        )

        # 获取分享信息
        share_result = self._get_share_info(hsid)

        if share_result.get("code") != 0:
            self.post_message(
                channel=channel,
                title="115海搜",
                text=f"获取分享信息失败: {share_result.get('msg', '未知错误')}",
                userid=user,
            )
            return

        share_info = share_result.get("result", {})
        share_url = share_info.get("shareUrl", "")
        share_pwd = share_info.get("sharePwd", "")

        if not share_url:
            self.post_message(
                channel=channel,
                title="115海搜",
                text="未获取到有效的分享链接",
                userid=user,
            )
            return

        # 发送确认信息
        confirm_text = f"即将转存:\n\n"
        confirm_text += f"标题: {title}\n"
        confirm_text += f"链接: {share_url}\n"
        if share_pwd:
            confirm_text += f"密码: {share_pwd}\n"
        confirm_text += f"\n确认转存?"

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

        # 缓存分享信息
        self._search_cache[f"{user}_share_{hsid}"] = {
            "share_url": share_url,
            "share_pwd": share_pwd,
            "title": title,
        }

        self.post_message(
            channel=channel,
            title="115海搜",
            text=confirm_text,
            userid=user,
            buttons=buttons,
        )

    def _handle_confirm_transfer(self, hsid: str, channel, source, user):
        """处理确认转存"""
        share_data = self._search_cache.get(f"{user}_share_{hsid}", {})

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

        if result.get("code") == 0:
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
                text=f"转存失败: {result.get('msg', '未知错误')}",
                userid=user,
            )

        # 清理缓存
        self._search_cache.pop(f"{user}_share_{hsid}", None)

    # ==================== API接口 ====================

    def _api_search(self, keyword: str, page: int = 1, page_size: int = 10):
        """API搜索接口"""
        return self._search_resources(keyword, page, page_size)

    def _api_validate_cookie(self):
        """API验证Cookie接口"""
        valid, msg = self._check_cookie_valid()
        return {"valid": valid, "message": msg}
