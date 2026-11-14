import importlib
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MessageChannel, NotificationType

# 补丁标记（打在函数对象上，用于幂等判断）
_PATCH_FLAG = "__dwqq_patched__"
# 原方法在类上的暂存属性名
_ORIGINAL_ATTR = "__dwqq_original_send_v2_wechat__"
# 目标渠道在类上的暂存属性名
_CHANNEL_ATTR = "__dwqq_channel__"

# 目标插件模块
_TARGET_MODULE = "app.plugins.dynamicwechat.helper"
_TARGET_CLASS = "MySender"
_TARGET_METHOD = "_send_v2_wechat"


def _build_patched_method(my_sender_cls: Any):
    """
    构造替换后的 _send_v2_wechat。
    渠道从类属性上读取（而不是闭包），这样即使本插件模块被重载也能保持生效。
    """

    def _send_v2_wechat(self, title, content, image, token):
        channel = getattr(my_sender_cls, _CHANNEL_ATTR, None) or MessageChannel.Wechat
        if token and ',' in token:
            _, actual_userid = token.split(',', 1)
        else:
            actual_userid = None
        self.post_message_func(
            channel=channel,
            mtype=NotificationType.Plugin,
            title=title,
            text=content,
            image=image,
            link=image,
            userid=actual_userid,
        )
        return None

    setattr(_send_v2_wechat, _PATCH_FLAG, True)
    return _send_v2_wechat


class DynamicWeChatQQPatch(_PluginBase):
    """
    动态企微可信IP · QQ 通知补丁

    动态企微可信IP(DynamicWeChat) 的通知发送器 MySender 只支持
    微信/ServerChan/IYUU/PushPlus 四种通道，没有 QQ 分支。
    本插件通过运行时替换 MySender._send_v2_wechat，
    把通知渠道从「微信」改到「QQ」，复用 MoviePilot 已配置的 QQ Bot。
    """

    # 插件名称
    plugin_name = "动态企微QQ通知补丁"
    # 插件描述
    plugin_desc = "把「动态企微可信IP」插件的通知通道从微信改到 QQ Bot，复用 MoviePilot 已配置的 QQ 渠道。不修改原插件任何文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/QQ_A.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Zlmetal"
    # 作者主页
    author_url = "https://github.com/Zlmetal"
    # 插件配置项ID前缀
    plugin_config_prefix = "dynamicwechatqqpatch_"
    # 加载顺序（必须小于 DynamicWeChat 的 47，保证先于它初始化）
    plugin_order = 46
    # 可使用的用户级别
    auth_level = 1

    # 运行时状态
    _enabled = False
    _channel = "QQ"
    _patched = False

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._channel = (config.get("channel") or "QQ").strip()
        self._patched = False

        if self._enabled:
            self._apply_patch()
        else:
            self._restore_patch()
            logger.info("【动态企微QQ补丁】未启用，未接管通知通道")

    def get_state(self) -> bool:
        """返回插件状态"""
        return self._enabled

    def stop_service(self):
        """停止插件（MP 基类的抽象方法，必须实现）：还原补丁"""
        self._enabled = False
        self._restore_patch()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/status",
                "endpoint": self._api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看补丁状态",
                "description": "检查补丁是否已成功接管 DynamicWeChat 的通知发送",
            },
            {
                "path": "/test",
                "endpoint": self._api_test,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "发送测试通知",
                "description": "通过已打补丁的通道发送一条测试通知",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "channel",
                                            "label": "通知渠道",
                                            "items": [
                                                {"title": "QQ", "value": "QQ"},
                                                {"title": "微信（原版行为）", "value": "Wechat"},
                                            ],
                                            "hint": "选 QQ 会用 MoviePilot 已配置的 QQ Bot 发送企微登录二维码/换IP结果；选微信则保持原插件行为",
                                            "persistent-hint": True,
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "本插件只在内存里替换 DynamicWeChat 的发送方法，不修改它的任何文件。"
                                                    "DynamicWeChat 升级/重载后会自动重新打补丁。"
                                                    "使用前请确认 MoviePilot 的 QQ 渠道已启用，且「消息类型」勾选了「插件」。",
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
            "channel": "QQ",
        }

    def get_page(self) -> List[dict]:
        """返回详情页JSON"""
        if not self._enabled:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "插件未启用。启用后会立即接管通知通道。",
                    },
                }
            ]

        ok, msg = self._check_target()
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if (ok and self._patched) else "warning",
                    "variant": "tonal",
                    "text": (
                        f"补丁状态：已接管 → {self._channel}" if (ok and self._patched)
                        else f"补丁状态：未接管（{msg}）"
                    ),
                },
            },
        ]

    # ==================== 补丁逻辑 ====================

    def _resolve_channel(self) -> Optional[MessageChannel]:
        """把配置值转成 MessageChannel 成员"""
        name = self._channel or "QQ"
        try:
            return MessageChannel[name]
        except KeyError:
            logger.warning(f"【动态企微QQ补丁】当前 MoviePilot 不支持消息渠道 {name}，回退到 Wechat")
            return MessageChannel.Wechat

    def _check_target(self) -> Tuple[bool, str]:
        """检查目标插件是否存在且含目标方法"""
        try:
            module = importlib.import_module(_TARGET_MODULE)
        except Exception as err:
            return False, f"未找到 {_TARGET_MODULE}（DynamicWeChat 未安装？）：{err}"
        clazz = getattr(module, _TARGET_CLASS, None)
        if clazz is None:
            return False, f"{_TARGET_MODULE} 里没有 {_TARGET_CLASS} 类"
        if not hasattr(clazz, _TARGET_METHOD):
            return False, f"{_TARGET_CLASS} 没有 {_TARGET_METHOD} 方法（插件版本可能已变更）"
        return True, "ok"

    def _apply_patch(self) -> bool:
        """打补丁（幂等）"""
        ok, msg = self._check_target()
        if not ok:
            self._patched = False
            logger.warning(f"【动态企微QQ补丁】无法打补丁：{msg}")
            return False

        channel = self._resolve_channel()
        module = importlib.import_module(_TARGET_MODULE)
        clazz = getattr(module, _TARGET_CLASS)
        current = getattr(clazz, _TARGET_METHOD)

        if getattr(current, _PATCH_FLAG, False):
            # 已打过，只更新渠道
            setattr(clazz, _CHANNEL_ATTR, channel)
            self._patched = True
            logger.info(f"【动态企微QQ补丁】补丁已在位，渠道更新为：{channel.value}")
            return True

        setattr(clazz, _ORIGINAL_ATTR, current)
        setattr(clazz, _TARGET_METHOD, _build_patched_method(clazz))
        setattr(clazz, _CHANNEL_ATTR, channel)
        self._patched = True
        logger.info(f"【动态企微QQ补丁】已接管 DynamicWeChat 通知发送：{_TARGET_CLASS}.{_TARGET_METHOD} → {channel.value}")
        return True

    def _restore_patch(self) -> None:
        """还原补丁"""
        try:
            module = importlib.import_module(_TARGET_MODULE)
        except Exception:
            return
        clazz = getattr(module, _TARGET_CLASS, None)
        if clazz is None:
            return
        original = getattr(clazz, _ORIGINAL_ATTR, None)
        if original is not None:
            setattr(clazz, _TARGET_METHOD, original)
            try:
                delattr(clazz, _ORIGINAL_ATTR)
            except AttributeError:
                pass
            logger.info("【动态企微QQ补丁】已还原 DynamicWeChat 原始发送方法")
        self._patched = False

    @eventmanager.register(EventType.PluginReload)
    def _on_plugin_reload(self, event: Event):
        """DynamicWeChat 重载后模块会被重新导入，需要重新打补丁"""
        if not self._enabled:
            return
        data = event.event_data or {}
        plugin_id = str(data.get("plugin_id") or "")
        if plugin_id.lower() != "dynamicwechat":
            return
        logger.info(f"【动态企微QQ补丁】检测到 DynamicWeChat 重载，重新打补丁")
        self._apply_patch()

    # ==================== API ====================

    def _api_status(self) -> dict:
        ok, msg = self._check_target()
        return {
            "enabled": self._enabled,
            "channel": self._channel,
            "patched": self._patched,
            "target_ok": ok,
            "target_message": msg,
        }

    def _api_test(self) -> dict:
        """通过打补丁后的方法发一条测试通知"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        ok, msg = self._check_target()
        if not ok:
            return {"success": False, "message": msg}

        try:
            from app.core.plugin import PluginManager
            plugin = PluginManager()._running_plugins.get("DynamicWeChat")
        except Exception as err:
            return {"success": False, "message": f"获取 DynamicWeChat 实例失败：{err}"}

        if not plugin:
            return {"success": False, "message": "DynamicWeChat 未运行（未安装或未启用）"}

        sender = getattr(plugin, "_my_send", None)
        if not sender:
            return {
                "success": False,
                "message": "DynamicWeChat 的通知发送器未初始化，请在它的配置里填一个「通知方式」Token（含 wechat 字样）",
            }

        if not getattr(sender, "post_message_func", None):
            return {
                "success": False,
                "message": "DynamicWeChat 运行在 v1 模式，未注入 post_message，无法走 QQ 渠道",
            }

        try:
            send = getattr(sender, _TARGET_METHOD)
            send(
                "动态企微QQ通知补丁 测试",
                "收到这条说明 QQ 通知通道已经接通，原始企微登录二维码也会推到这里。",
                None,
                "",
            )
        except Exception as err:
            logger.error(f"【动态企微QQ补丁】测试发送失败：{err}")
            return {"success": False, "message": f"测试发送失败：{err}"}

        return {"success": True, "message": f"已通过 {self._channel} 发送测试通知"}
