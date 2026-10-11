# 影视平台上新提醒（StreamingNewAlert）

MoviePilot V3 插件。定时监控 **爱奇艺 / 优酷 / 腾讯视频** 的电影与电视剧上新，
按平台分组推送通知，按「平台 + 内容类型 + 内容名称 + 上映年份」**永久去重**。

## 功能

| 能力 | 说明 |
| --- | --- |
| 监控平台 | 爱奇艺、优酷、腾讯视频，可单独开关 |
| 内容类型 | 电影、电视剧（含网剧），可单独开关 |
| 电影规则 | 只推送正式上线可播放的正片，排除预告 / 预告片 / 即将上映 / 花絮 / 纪录片 / 综艺 / 短视频等；付费、会员专享内容只要已上线就纳入 |
| 电视剧规则 | 仅首次正式上线提醒 1 次；每日更新集数、加更、结局更新（`更新至xx集`、`第x集`）自动识别并跳过 |
| 去重 | `平台 + 类型 + 名称 + 年份` 生成唯一标识，写入去重库后永不再推 |
| 定时执行 | 每日定时运行，时间可配置（默认 10:00，支持 `10:00,22:00` 多个时间点） |
| 手动执行 | 配置页「立即执行一次」、远程命令 `/ptnew`、API `POST /run` |
| 消息推送 | 走 MoviePilot 全局消息通道，复用用户已配置的微信 / 钉钉 / Telegram 等渠道 |
| 空结果 | 当日无新内容时不发送通知 |
| 容错 | 单平台失败自动重试 2 次（间隔 3 秒），全部失败只记日志，不影响其他平台与主程序 |

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 启用插件 | 开关 | 关闭 | 插件总开关 |
| 立即执行一次 | 开关 | 关闭 | 保存后立即跑一次，随后自动复位 |
| 监控爱奇艺 | 开关 | 开启 | — |
| 监控优酷 | 开关 | 开启 | — |
| 监控腾讯视频 | 开关 | 开启 | — |
| 推送电影 | 开关 | 开启 | — |
| 推送电视剧 | 开关 | 开启 | — |
| 每日执行时间 | 文本 | 10:00 | 格式 `HH:MM`，多个时间用英文逗号分隔 |
| 上新时间范围 | 数字 | 7 | 只推送近 N 天内上线的内容（单位：天） |
| 通知标题 | 文本 | 🎬 影视平台上新提醒 | 自定义通知标题 |
| 使用 TMDB 数据源 | 开关 | 开启 | 见「数据源说明」，推荐开启 |
| TMDB API Key | 文本 | 空 | 留空则复用 MoviePilot 系统设置中的 TMDB API Key |
| 自定义聚合 API | 文本 | 空 | 见「数据源说明」，支持 `{platform}`、`{type}` 占位符 |
| API Token | 文本 | 空 | 自定义 API 的 Bearer Token |
| 日期策略 | 选择 | 宽松 | 见「日期策略」 |

## 数据源说明（重要，请先读）

三大平台**均已下线公开的「上新榜」接口**，官网榜单页也是前端渲染 + 反爬。
插件按如下优先级取数（每个平台独立请求、独立解析、互不影响）：

1. **TMDB（首选，强烈推荐开启）**
   利用 TMDB 的「观看平台（Watch Providers）」数据，按
   `地区 CN + 平台（爱奇艺 / 优酷 / 腾讯视频）+ 上线日期区间` 精确查询：
   - 电影：`/3/discover/movie?with_watch_providers=<id>&watch_region=CN&release_date.gte/lte`
   - 剧集：`/3/discover/tv?with_watch_providers=<id>&watch_region=CN&first_air_date.gte/lte`
   - **优点**：每条都有精确上线日期，`日期策略` 可以直接设为「严格」；
     电视剧取 `first_air_date`（首播日期），天然满足「仅首次上线提醒一次」。
   - provider_id 不硬编码，运行时调用 `/3/watch/providers` 按名称匹配，平台改名也不失效。
   - API Key：留空即自动复用 MoviePilot 系统设置里的 TMDB API Key（MP 本身就依赖 TMDB，
     绝大多数用户已配置）；也可在插件配置里单独填。
   - 局限性（请知悉）：
     - 中国大陆区流媒体数据由社区 + JustWatch 维护，**国产新剧 / 网剧可能缺条目或延迟入库**；
     - 电影的 `release_date` 多为院线 / 首映日期，**不一定等于平台上线日期**；
     - TMDB 只标注"可观看"，不区分会员 / 付费 / 免费。
2. **自定义聚合 API**：在配置页填写接口地址，插件在 TMDB 无结果时调用，
   URL 中的 `{platform}` 会替换为 `iqiyi`/`youku`/`tencent`，`{type}` 替换为 `movie`/`tv`。
   接口返回任意 JSON 即可，插件用通用解析器自动识别标题 / 日期 / 类型字段。
3. **内置公开接口**（按实测结果取舍，随时可能随平台改版失效）：
   - 爱奇艺：`pcw-api.iqiyi.com` 片库推荐接口（电影 / 电视剧），可用；
   - 腾讯视频：`pbaccess.video.qq.com` 热榜接口，按 `tabId` 只取电影 / 电视剧榜
     （**当前腾讯电影榜服务端返回为空**，电视剧榜正常）；
   - 优酷：分类页兜底，**大概率被 JS 反爬拦截**，日志会提示抓取失败。
4. **HTML 兜底**：解析页面内嵌的 `window.__INITIAL_STATE__` / `__NUXT__` / `application/json`
   数据块，其次是带 `title` 的链接标签。

### 为什么不用豆瓣 / IMDB

| 数据源 | 能否实现本功能 | 原因 |
| --- | --- | --- |
| TMDB | ✅ 可以，且是最优解 | 有 Watch Providers（含爱奇艺 / 优酷 / 腾讯视频）+ 精确日期 + 公开免费 API |
| 豆瓣 | ⚠️ 理论可行、工程不可行 | 电影页有"在哪些平台播放"和上映日期，但**官方 API 早已关闭**，只能抓 HTML；反爬严格、需登录 Cookie、频率稍高即封 IP，不适合放进定时插件 |
| IMDB | ❌ 不行 | 没有中国大陆流媒体平台数据，只有海外流媒体信息 |

结论：**用 TMDB**，豆瓣 / IMDB 都不适合做这个场景的主数据源。

### 数据源可用性自检

调用 `GET /api/v1/plugin/StreamingNewAlert/selftest`，会返回各平台的候选条数，
以及 TMDB 是否成功解析到三大平台的 provider_id，便于判断当前哪条通道可用。

所有请求均带随机 UA + Referer，单平台失败重试 2 次（间隔 3 秒），
平台之间请求间隔 ≥ 2 秒（含随机抖动）。

### 日期策略

三大平台公开接口**基本不返回上线日期**，因此提供两档策略：

- **宽松（默认）**：能解析到日期的按「近 N 天」过滤；解析不到日期的按「本次首见」处理。
  配合永久去重，每条内容仍然只会推送一次。
- **严格**：必须解析到近 N 天内的上线日期才推送，否则丢弃（数据源不返回日期时可能一条都推不出）。

## 安装方法

### 方式一：本地 ZIP 安装（推荐，最简单）

1. 下载 `StreamingNewAlert-v1.1.1.zip`（约 30 KB，无需解压）。
2. MoviePilot → 插件 → **本地安装** → 选择该 ZIP 上传。
3. 安装若提示「安装失败，状态码: 404」：先到**右下角设置 → 打开「启用插件」→ 保存**，
   再回到插件页重新上传安装即可（这是首次安装未初始化安装 API 导致）。
4. 安装成功后启用 → 配置 → 保存。

ZIP 包结构（已按本地安装规范打包）：

```
StreamingNewAlert-v1.1.1.zip
└── streamingnewalert/          # ⚠️ 目录名必须是插件ID的小写（MoviePilot V3 加载器硬性要求）
    ├── __init__.py             # 主代码，含 class StreamingNewAlert(_PluginBase)
    ├── plugin.json             # 元数据 + 配置项 Schema（id = StreamingNewAlert）
    ├── requirements.txt        # 依赖声明（本插件零第三方依赖）
    └── README.md
```

### 方式二：本地目录安装（无需打包）

把 `streamingnewalert` 目录复制到 MoviePilot 的插件运行目录，**目录名保持小写**：

```bash
# 容器内典型路径（按你的部署方式调整）
cp -r streamingnewalert /app/plugins/streamingnewalert
# 或挂载目录：
cp -r streamingnewalert ./config/plugins/streamingnewalert
```

然后在 MoviePilot 插件页刷新，即可看到插件；启用并保存配置生效。

### 方式三：插件市场（第三方仓库）

1. 把 `plugins.v3/streamingnewalert/` 整个目录放入你的插件仓库（fork `MoviePilot-Plugins` 的结构）。
2. 把 `package.v3.json` 中的 `StreamingNewAlert` 条目合并进仓库根目录的 `package.v3.json`。
3. MoviePilot → 插件 → 添加第三方仓库，填入你的仓库地址（或 `PLUGIN_MARKET` 环境变量）。
4. 在插件列表找到「影视平台上新提醒」→ 安装 → 启用 → 配置 → 保存。

> 备份：本地安装的插件会备份到 `/config/plugins/LocalPluginInstall/backups`，
> 可在插件页「从备份恢复」回滚版本，请勿手动删除该目录。

## 使用说明

1. 启用插件，按需打开 / 关闭平台与内容类型开关。
2. 设置「每日执行时间」（如 `10:00`）与「上新时间范围」（默认 7 天）。
3. 点「立即执行一次」试跑；也可在聊天渠道发送 `/ptnew` 立即执行一次。
4. 查看运行日志：日志前缀为 `[影视上新]`，会打印候选数、过滤数、重复数、推送数，
   以及被过滤内容的具体原因（DEBUG 级别）。
5. 插件详情页可查看已推送记录总量、上次运行时间与最近 50 条推送明细。

### 内置接口 / API

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/plugin/StreamingNewAlert/run` | POST | 立即执行一次 |
| `/api/v1/plugin/StreamingNewAlert/selftest` | GET | 数据源连通性自检，返回各平台候选条数 |
| `/api/v1/plugin/StreamingNewAlert/clear` | POST | 清空去重库（会重新推送一次全量） |

### 数据持久化

已推送记录保存在插件数据目录（MoviePilot `PLUGIN_DATA_PATH/StreamingNewAlert/`）下的
`pushed_history.json`，重启插件 / 重启容器均不丢失；
删除该文件即可清空去重库。

## 依赖

无需额外安装依赖。HTTP 请求优先使用 MoviePilot 宿主自带的 `requests`，
缺失时自动回退到 Python 标准库 `urllib`。

## 版本

- `1.1.1` —— **修复「插件加载失败」**：插件物理目录名必须是插件ID的小写（`streamingnewalert`），
  这与 MoviePilot V3 加载器 `loader.py` 的 `targets = [plugin_id.lower()]` 匹配规则一致；
  同时加固宿主依赖导入，任何可选依赖缺失/异常都不再导致插件被标记为 LOAD_FAILED。
- `1.1.0` —— 新增 TMDB 数据源（按「地区 + 观看平台 + 上线日期区间」精确查询），
  补全三大平台公开接口缺失的上线日期；新增 `use_tmdb` / `tmdb_api_key` 配置项与数据源自检接口。
- `1.0.0` —— 首次发布。

## 排错：插件加载失败

若插件卡片显示「插件加载失败，请查看插件日志」，按序检查：

1. **目录名是否为小写** `streamingnewalert`（最常见原因）。大写驼峰目录会被 V3 加载器直接跳过，
   日志会出现 `加载器未返回匹配的插件类`。
2. `__init__.py` 中是否存在 `class StreamingNewAlert(_PluginBase):`（行首无缩进，字面量一致）。
3. 查看运行日志中 `加载插件 StreamingNewAlert 出错` 之后的堆栈；本插件已对全部宿主导入做降级处理，
   正常情况下不会因依赖缺失失败。
