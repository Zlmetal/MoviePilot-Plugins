# MoviePilot 插件集（Zlmetal）

本仓库包含以下插件：

| 插件 | 说明 |
|------|------|
| [115海搜转存](#115海搜转存) | 从海搜搜索115网盘资源并自动转存 |
| [动态企微QQ通知补丁](#动态企微qq通知补丁) | 让「动态企微可信IP」的通知走 QQ Bot |

安装方式：在 MoviePilot 插件市场中添加第三方插件源
```
https://github.com/Zlmetal/MoviePilot-Plugins
```

---

# 115海搜转存

从[海搜](https://haisou.cc/)搜索115网盘资源并自动转存到115网盘。

## 功能

- 从海搜网站搜索115网盘资源
- 搜索结果包含标题、文件大小、文件数、提取码
- 一键转存到115网盘
- 支持MP消息交互（微信、Telegram等）
- 支持AI智能体调用搜索

## 前置依赖

**必须先安装并启用 [115网盘STRM助手](https://github.com/DDSRem-Dev/MoviePilot-Plugins) (P115StrmHelper) 插件**

本插件的转存功能依赖 P115StrmHelper 插件实现，请确保：
1. 已安装 P115StrmHelper 插件
2. 已配置115网盘账号和转存目录
3. P115StrmHelper 插件处于启用状态

## 安装

在 MoviePilot 插件市场中添加第三方插件源：
```
https://github.com/Zlmetal/MoviePilot-Plugins
```

搜索"115海搜转存"并安装。

## 使用方法

### 搜索资源
```
/hs 电影名
```
例如：`/hs 绿液惊魂 2026`

### 选择并转存
```
/hs select 序号
```
例如：`/hs select 1`（转存搜索结果中的第1个资源）

### 智能体调用
在支持AI智能体的渠道中，直接发送影视名称，智能体会自动调用海搜搜索115资源。

## 配置

| 配置项 | 说明 |
|--------|------|
| 启用插件 | 开启/关闭插件 |
| 海搜Cookie | 登录haisou.cc后从浏览器获取（留空也可用，有每日次数限制） |

### 获取Cookie（可选）

1. 浏览器登录 https://haisou.cc
2. 按 F12 打开开发者工具
3. 切换到 Network 标签
4. 刷新页面，点击任意请求
5. 在 Request Headers 中复制完整的 Cookie 值

VIP用户配置Cookie后可享受无限制搜索次数。

## 许可证

GPL-3.0

---

# 动态企微QQ通知补丁

让 [动态企微可信IP](https://github.com/jxxghp/MoviePilot-Plugins)（`DynamicWeChat`）的通知走 **QQ Bot**。

## 解决的问题

`DynamicWeChat` 的通知发送器 `MySender` 只认四种通道，按 Token 前缀猜：

- Token 含 `wechat` → 走 MoviePilot 的微信渠道
- 以 `sct` 开头 → ServerChan
- 含 `iyuu` → IYUU
- 其它一律当 PushPlus

**没有 QQ 分支**。所以哪怕 MoviePilot 里 QQ Bot 已经配好了，企微 cookie 失效通知、登录二维码、换 IP 结果这些消息也推不到 QQ。

## 怎么做的

不修改 `DynamicWeChat` 的任何文件，只在本插件加载时，于**内存中**替换
`app.plugins.dynamicwechat.helper.MySender._send_v2_wechat`，
把 `channel=MessageChannel.Wechat` 换成 `MessageChannel.QQ`，其余参数原样透传。

- 插件加载顺序 `plugin_order = 46`，排在 `DynamicWeChat`(47) 之前，保证先打补丁再初始化
- 监听 `PluginReload` 事件，`DynamicWeChat` 重载/升级后自动重新打补丁
- 关闭本插件即还原原始方法
- 目标插件未安装、或方法名变更时会打警告日志，不影响 MoviePilot 启动

## 前置条件

1. MoviePilot 版本 ≥ 2.15（`MessageChannel.QQ` 与 `NotificationType.Plugin` 已存在）
2. 已安装并启用 `动态企微可信IP`（DynamicWeChat）
3. MoviePilot **通知设置 → QQ** 已配置并启用，且**消息类型勾选了「插件」**
4. `DynamicWeChat` 配置里的**「通知方式」Token 必须填一个含 `wechat` 字样的值**（比如 `wechat`），这是触发 `_send_v2_wechat` 分支的条件；逗号后那段会被当作 userid，可以留空

## 配置

| 配置项 | 说明 |
|--------|------|
| 启用插件 | 开启后立即接管通知通道 |
| 通知渠道 | `QQ`（默认）或 `微信`（还原原版行为） |

## 自检

启用后打开插件详情页，会显示「补丁状态：已接管 → QQ」。
也可以调用 `/api/v1/plugin/DynamicWeChatQQPatch/status` 查看补丁状态，
用 `/test` 直接发一条测试通知验证通道。

## 许可证

GPL-3.0
