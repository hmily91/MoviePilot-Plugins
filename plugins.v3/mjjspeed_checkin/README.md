# MoviePilot V3 插件：MJJSpeed站点自动签到

适配 MoviePilot V3 的站点自动签到插件：使用邮箱账号 + 密码自动登录 MJJSpeed 系列站点并完成每日签到，
无需手工抓取 Cookie，所有配置项在前端表单可视化编辑。

- 插件 ID：`mjjspeed_checkin`
- 插件名称：MJJSpeed站点自动签到
- 版本：v1.0.0
- 分类：工具

## 目录结构

```text
mjjspeed-checkin/                       # 插件仓库根目录
├── package.v3.json                     # V3 插件索引（元数据）
├── package.json                        # 兼容旧版索引（内容同 package.v3.json）
├── mjjspeed.png                        # 插件图标
├── plugins.v3/
│   └── mjjspeed_checkin/
│       └── __init__.py                 # V3 插件源码
└── plugins/
    └── mjjspeed_checkin/
        └── __init__.py                 # 兼容旧版目录布局（内容同上）
```

> 手动安装时，把 `plugins.v3/mjjspeed_checkin/` 整个目录复制到 MoviePilot 的 `app/plugins/` 下即可。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用插件 | 开关 | 开启 | 关闭后不再执行签到 |
| 结果通知 | 开关 | 开启 | 是否通过系统通知推送签到结果 |
| 站点列表 | 多行文本 | 内置 7 个域名 | 每行一个站点地址，留空协议时自动补全 https:// |
| 登录账号 | 文本 | 1970238@qq.com | 登录邮箱 |
| 登录密码 | 密码 | q8484147 | 登录密码（前端密文显示） |
| 签到时间 | 时间选择 | 01:00 | 每日自动签到时间 |
| 失败重试次数 | 数字 | 3 | 签到失败后的重试次数（1-10） |
| 登录接口路径 | 文本 | /auth/login | 登录接口相对路径 |
| 签到接口路径 | 文本 | /user/checkin | 签到接口相对路径 |

## 功能说明

1. **多站点管理**：内置 7 个默认站点，可在配置里增删；每个站点独立保存登录态与最近一次签到记录。
2. **账号密码登录**：默认 `POST /auth/login`，请求体 `{"email": "账号", "password": "密码"}`，登录态过期自动重登。
3. **自动签到**：默认 `POST /user/checkin`；注册系统级定时任务，每日按配置时间执行；支持「今日已签到」识别。
4. **手动立即签到**：发送命令 `/mjjspeed_checkin`，或调用插件 API `POST /api/v1/plugin/mjjspeed_checkin/checkin`。
5. **结果通知**：签到完成后推送系统通知，成功/失败都会提示。
6. **异常处理**：网络超时、登录失败、HTTP 5xx、站点不可达等场景分别捕获并写入日志，失败按配置次数重试。

## 技术要求落实

- 继承 `_PluginBase`，实现 `init_plugin / get_state / get_command / get_api / get_form / get_page / get_service / stop_service`。
- 配置通过系统原生 `get_config` / `update_config` 读写，`get_form()` 返回 Vuetify JSON，前端自动渲染表单。
- 所有 HTTP 请求使用系统内置 `RequestUtils`，自动复用 `settings.PROXY` 代理，超时统一 10 秒。
- 站点登录态与签到记录使用插件数据接口 `get_data` / `save_data` 持久化，按站点独立存储。

## 安装

在 MoviePilot「插件市场」中添加本仓库地址后安装 `mjjspeed_checkin`，或在插件页面使用本地插件源安装。
