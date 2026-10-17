# MoviePilot-Plugins

MoviePilot V3 第三方插件仓库。

## 包含的插件

| 插件 | 说明 | 版本 |
|------|------|------|
| [影视平台上新提醒](plugins.v3/streamingnewalert/) | 监控爱奇艺 / 优酷 / 腾讯视频的电影与电视剧上新，按平台分组推送提醒，永久去重 | 1.2.1 |
| [MJJSpeed站点自动签到](plugins.v3/mjjspeed_checkin/) | MJJSpeed 系列站点账号密码自动登录 + 每日自动签到，支持自定义域名，无需手工抓取 Cookie | 1.0.0 |

## 使用方式

MoviePilot → 插件 → 添加第三方仓库，填入本仓库地址：

```
https://github.com/<你的用户名>/MoviePilot-Plugins
```

然后在插件列表找到对应插件（影视平台上新提醒 / MJJSpeed站点自动签到）→ 安装 → 启用 → 配置 → 保存。

## 目录结构

```
package.v3.json                 # 插件市场索引（新增插件必须在此登记）
plugins.v3/
└── streamingnewalert/          # 目录名必须为插件ID的小写
    ├── __init__.py
    ├── plugin.json
    ├── requirements.txt
    └── README.md
```

> 注意：MoviePilot V3 的加载器按小写目录名匹配插件
> （`app/runtime/extensions/plugin/loader.py`: `targets = [plugin_id.lower()]`），
> 插件目录名必须小写，而 `package.v3.json` 的键与插件类名保持驼峰。
