# 115海搜转存 MoviePilot 插件

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
