# 订阅自动保种助手（MoviePilot V3 插件）

识别「同剧、同季、目标分辨率」的**整季打包种子**，自动清理旧的原生电视剧订阅及其产生的一切，然后下载整季全集，让 MoviePilot 正常完成转移与刮削。

> 适用场景：用 MP 官方订阅追剧，剧集完结后 PT 站下架分集、放出整季打包。分集种子后续无法保种、目录杂乱；而**旧的媒体库文件不删，新全集转移进来时 MP 会判定「已存在」而跳过刮削/整理**，所以必须「先删旧、再下新」。

---

## 一、工作流程（两段等待，顺序不可颠倒）

```
Cron 定时扫描（静默后台，不接管 MP 原生订阅搜索）
  ↓
遍历 MP 原生电视剧订阅（只按 type=电视剧 过滤；手动添加的种子不在订阅表，天然排除）
  ↓
在「插件勾选站点 ∩ 订阅自带站点」检索资源
  ↓
判定链：站点 → 宿主 MetaInfo 季集 → 多季排除 → 单集/部分集闸门 → 半季排除
        → 季号与订阅严格相等 → 分辨率 → 集数一致性(可选) → 排除正则 → 包含正则
  ↓ 命中
【删前等待队列，默认 15 分钟】持续复检整包种子是否仍可检索到
        · 连续 N 次检索不到 → 自动取消，不删除任何内容
        · 订阅在期间被删 → 自动取消
  ↓ 到期仍存在
【执行分级清理】L0 删订阅 → L1 删种子(可含文件) → L1b 删下载文件
              → L3 删媒体库文件 → L2 删转移记录/下载历史
        · 清理前把「订阅 → 种子 hash → 下载路径 → 媒体库路径」证据链写进日志并通知
  ↓ 媒体库目标路径已清空
【替换观察窗，默认 15 分钟】冷却该媒体身份，防止重复清理
        · 默认延迟 10 分钟后，把命中的整季全集交给 MP 下载链（DownloadChain.download_single）
        · 关闭自动下载则只推送标题 / 站点 / 页面 / 下载链接，由你手动下载
  ↓
MP 下载完成 → 目标路径为空 → 正常转移 + 刮削生效
```

**测试模式（默认开启）**：只打印日志、推送预警，不入队、不删除、不下载。正式启用前务必先跑一轮核对证据链。

---

## 二、目录结构

```
MoviePilot-Plugins/
├── plugins.v3/
│   └── subscribeautokeep/            # 目录名 = 主类名小写
│       ├── __init__.py               # 主类 SubscribeAutoKeep：生命周期 / 配置页 / 服务 / API / 通知
│       ├── core.py                   # 纯判定引擎（不依赖宿主，可本地单测）
│       ├── cleaner.py                # 证据链收集 + 分级清理（唯一接触宿主业务数据的模块）
│       ├── pending_queue.py          # 等待队列状态与序列化
│       ├── form.json                 # 前端配置页 Vuetify JSON 快照
│       ├── plugin.json               # 插件元数据
│       └── README.md                 # 本文档
├── tests/
│   └── v3/
│       └── subscribeautokeep/
│           └── test_core.py          # 真实 PT 站标题回归 + 单元测试
└── package.v3.json                   # 市场索引（新增 SubscribeAutoKeep 条目）
```

V3 合同要点：主类继承 `app.sdk.plugin._PluginBase`；定时任务由 `get_service()` 交给宿主调度器（不自建线程/调度器）；配置走 `update_config/get_config`，运行状态走 `save_data/get_data`；宿主订阅只读走 `SubscribeOper`，历史走 `TransferHistoryOper` / `DownloadHistoryOper`，下载器走 `app.chain.download.DownloadChain`；不导入 `app.db.models.*`、不使用旧版 `app.core.*` / `app.helper.*`。

---

## 三、安装

1. 要求 **MoviePilot V3**（`system_version >= 3.0.0`，已按 v3 分支最新宿主源码核对 API）。
2. 本仓库即第三方插件仓库：在 MP 中把 `PLUGIN_MARKET` 指向 `https://github.com/hmily91/MoviePilot-Plugins`，市场只读取 `main` 分支。
3. 在插件市场找到「订阅自动保种助手」安装；或把 `plugins.v3/subscribeautokeep` 整个目录放到 MP 的插件目录后重载。
4. 本地联调可设置 `PLUGIN_LOCAL_REPO_PATHS` + `PLUGIN_AUTO_RELOAD=true` + `DEBUG=true`。
5. 安装后：**保持测试模式开启** → 配置 Cron / 站点 / 分辨率 → 点「立即扫描一次」→ 看日志与通知 → 确认证据链无误后再关闭测试模式。

无额外第三方依赖，因此不提供 `pyproject.toml`。

---

## 四、配置项

| 分组 | 配置项 | 默认 | 说明 |
|---|---|---|---|
| 基础 | `enabled` / `test_mode` | `false` / **`true`** | 测试模式务必先跑一轮 |
| 调度 | `cron` | `0 4 * * *` | 5 段标准 Cron；另有「保存后立即扫描一次」 |
| 站点 | `sites` | 空 | 多选，运行时由 `SiteOper.list_active()` 动态生成；空=不限制 |
| 分辨率 | `resolutions` | `2160p,1080p` | 支持 2160p/1080p/720p/480p；`1080i` 归入 1080p，`4K/UHD` 归入 2160p；`allow_unknown_resolution` 默认关（未标注分辨率保守跳过） |
| 规则 | `include_rules` / `exclude_rules` | 内置示例 | 每行 `字段::正则`；**先判排除，命中即跳过；未命中再判包含，命中任一即通过** |
| 规则 | `require_include_rule` | `true` | 关掉则包含正则降级为可选精筛 |
| 规则 | `require_completeness_evidence` | `false` | 额外要求 `全N集` / `Complete` / `完结` 等整包关键词 |
| 订阅 | `follow_subscribe_settings` | `true` | 跟随 MP 编辑订阅页自带的「订阅站点 / 分辨率 / 排除 / 总集数」：站点取交集、分辨率合并、订阅排除规则叠加（收紧）。**订阅的「包含」不并入**，否则会放宽命中条件 |
| 订阅 | `verify_episode_count` | `false` | 用订阅「总集数」核对资源「全N集」，不符则跳过 |
| 白名单 | `subscribe_whitelist` | 空 | 每行订阅 ID / 剧名 / 媒体 ID，命中永不清理 |
| 隔离 | `subscribe_source_blacklist` | 空 | 每行一个 username，用于排除其他插件写入的订阅 |
| 等待 | `delay_minutes` | `15` | 删前稳定性等待，期间持续复检整包种子 |
| 等待 | `recheck_missing_threshold` | `2` | 连续检索不到几次则取消 |
| 替换 | `replacement_delay_minutes` | `10` | 清理后延迟多久下载新全集（建议 10~15） |
| 替换 | `replacement_window_minutes` | `15` | 观察窗时长，期间冷却该媒体身份 |
| 替换 | `auto_download_replacement` | `true` | 关掉只推送链接，由你手动下载 |
| 动作 | `action_delete_subscribe` | `true` | L0 删除原生订阅 |
| 动作 | `action_delete_torrent` | `true` | L1 删除下载器种子（qBittorrent / Transmission 由宿主适配） |
| 动作 | `action_delete_torrent_files` | `false` | L1 让下载器同时删种子文件 |
| 动作 | `action_delete_local_files` | `false` | L1b 删除下载目录具体文件 |
| 动作 | `action_delete_media_files` | **`false`** | L3 删除媒体库文件，高危 |
| 动作 | `action_delete_history` | `true` | L2 删除转移记录与下载历史 |
| 安全 | `delete_path_prefixes` | 空 | 每行一个路径前缀；**强烈建议填写**，留空=不限制 |
| 通知 | `notify_on_*` | 全部 `true` | 命中 / 入队 / 执行清理 / 取消 / 替换，走 MP 原生通知通道（微信、Telegram 等） |

配置保存时会在 `init_plugin()` 校验全部正则与 Cron；**非法正则直接推送通知、写入日志并暂停扫描**，插件详情页也会显示错误。配置页「校验当前配置」「测试正则与季集判定」按钮可随时复核。

---

## 五、判定链（结合真实 PT 站标题）

MP V3 搜索结果筛选器把 `S01` 当作**完结季**、`S01 E40` 当作**分集**。插件优先读取宿主 `Context.meta_info` 的解析结果（`begin_season` / `begin_episode` / `episode_list` / `season_episode` / `part` / `resource_pix` / `resource_team`），标题正则只作宿主未识别时的兜底。

1. **站点**：必须在勾选范围内。
2. **宿主季集**：有集号（`S01 E40`、`S01 E36-E40`）→ 分集/部分集，跳过；`part` 非空 → 分段媒体，跳过；多季 → 跳过。
3. **多季 / 跨季排除**：`S01-S03`、`Season 1-3`、`全3季`、标题含多个不同季号 → 跳过（防止跨季误删）。
4. **单集 / 部分集闸门（标题 + 副标题）**：`S01E01`、`E01`、`1x01`、`EP01`、`Episode 01`、`第N集`、`第N-M集`、`E36-E40`、`Ep01-Ep08` → 跳过。**范围写法是分段包，不是整包**；`全40集`、`完结` 等完整性关键词会先剔除，不会被 `\d+集` 误杀。
5. **半季 / 分卷**：`Part1`、`Vol.1`、`前6集`、`Disc1` → 跳过。
6. **同季校验**：`S1` / `S01` / `Season 1` / `第1季` / `第一季` 归一化为整数，必须**严格等于订阅季号**；不一致直接跳过。
7. **分辨率**：标题、副标题、`resource_pix` 联合识别，必须落入勾选范围。
8. **集数一致性（可选）**：`全N集` 的 N 与订阅「总集数」不符 → 跳过。
9. **排除正则 → 包含正则**：排除优先级最高，命中任意一条即跳过。

### 真实标题对照

| 标题 / 副标题 | 判定 |
|---|---|
| `The Long Watch S01 2026 2160p ...-Pure@HDSWEB` / `交锋 全40集` | ✅ 整季打包 |
| `Reacher S04 2026 2160p ...` / `侠探杰克第4季 全08集` | ✅ 中文季号归一化后与 S04 一致 |
| `Let s Go S01 2160p ...` / `第一季 全40集` | ✅ 整季打包 |
| `VIVANT S02 1080p ...` / `第二季 全14集` | ✅ 整季打包 |
| `Lets Go S01 Complete 2026 2160p ...` / `全40集` | ✅ 整季打包 |
| `Let s Go S01E40 ...` / `第40集` | ❌ 单集 |
| `When I Meet the Moon S01E01-E06 ...` / `第01-06集` | ❌ 分段包 |
| `The Silent Storm S01 2160p ...` / `第27-30集` | ❌ 部分集（**标题干净，靠副标题拦下**） |
| `Still A Family S01 2160p ...` / `第05-06集` | ❌ 分段包 |
| `Some Show S01-S03 2160p Complete` / `全3季` | ❌ 跨季合集 |
| `Madden 2026 2160p ...` / `类型: 传记 / 运动` | ❌ 无季号（电影） |

`tests/v3/subscribeautokeep/test_core.py` 内置 29 条真实站点标题作为回归样例，逐条断言。

---

## 六、正则写法示例

规则文本每行一条：`字段::正则`。字段可选 `title`（标题）、`subtitle`（副标题）、`release_group`（发布组）、`note`（标签/备注）、`any`（全部字段）；省略字段默认 `title`。以 `#` 开头为注释。新增=加一行，删除=删掉该行。

```text
# 内置示例（用户指定）
any::Complete|Season\s*\d+|全集

# 中文 PT 站整包主特征：全N集 / 完结 / 整季
any::全\s*\d+\s*集|整季|全季|合集|完结

# 英文站整包
any::S\d{1,2}\s+Complete|Season\s+Pack

# 只要某几个发布组的整包
release_group::HDSWEB|CHDWEB|HHWEB

# 标题里带 2160p 且带 WEB-DL
title::2160p.*WEB-DL|WEB-DL.*2160p
```

排除规则（先于包含判断）：

```text
any::Sample|预告|花絮|Trailer|Preview
any::Part\s*\d+|Vol\.?\s*\d+|前\s*\d+\s*集
any::第\s*\d+\s*[-~至]?\s*\d*\s*集|第\s*\d+\s*[-~至]\s*\d+\s*集
title::x265.*60FPS
```

要点：
- 正则使用 `re.IGNORECASE`，`search()` 语义（不需要匹配整串）。
- **中文站的完整性信息几乎都在副标题**，所以默认字段用 `any`；只写 `title::全集` 会漏掉 `全40集`。
- 非法正则（如 `any::(unclosed`）保存时会被拦截并提示。
- 配置页「测试正则与季集判定」会用内置真实标题跑一遍，结果写入插件详情页并推送通知。

---

## 七、通知与日志

通知走基类 `post_message()`（`MessageType.Plugin`），沿用 MP 已配置的微信 / Telegram / 等通道，五类事件各自可开关：命中规则、进入等待队列、等待完成执行清理、任务取消、替换/下载新全集。

日志逐条打印：订阅名称与季号、订阅自带设置、站点、标题与副标题、识别到的季号、命中/未命中的正则、是否整季打包、每层校验结论、是否入队、等待倒计时与复检结果、证据链全部路径、最终执行结果或取消原因。

---

## 八、测试

```bash
python -m unittest discover -s tests/v3/subscribeautokeep -v
python -m compileall plugins.v3/subscribeautokeep
git diff --check
```

纯逻辑测试不依赖 MoviePilot 宿主；外部网络、真实下载器、真实数据库均不涉及。**插件最终仍需在真实 V3 宿主中至少加载一次**，确认市场可发现、启停不残留后台资源、API/服务/页面只注册一次。

---

## 九、风险说明（务必阅读）

1. **所有删除都不可回滚**。默认「测试模式」开启、`action_delete_media_files` 关闭、`action_delete_local_files` 关闭；请先用测试模式跑一轮，逐条核对日志里的证据链，再逐项打开开关。建议操作前备份 MP 数据库。
2. **媒体库文件删除风险最高**。MP 默认使用硬链接，下载目录与媒体库目录可能引用同一 inode；`Season 01/` 目录里常混着其他来源或其他订阅的文件。插件**只按证据链登记的具体文件路径逐个 `unlink`，绝不递归删除目录**，并拒绝根目录与过短路径。仍强烈建议填写 `delete_path_prefixes` 限定可删范围。
3. **删除转移记录有副作用**：MP 会认为这些文件「从未转移过」，日后重新订阅同一剧时可能重复下载 / 重复转移。若不想承担这个后果，请关闭 `action_delete_history`。
4. **「洗版」开关会与插件重叠**：MP 编辑订阅页有「洗版」，开启后宿主自己可能去下载整季全集。建议对交给本插件处理的订阅关闭洗版，避免重复下载和重复清理。
5. **顺序敏感**：旧文件必须先删、新全集后下载，否则 MP 判定目标已存在会跳过刮削。插件因此采用「删前 15 分钟稳定性等待 → 清理 → 删后 10~15 分钟替换观察窗 → 下载新全集」；请勿把 `replacement_delay_minutes` 设为 0 并同时手动提前下载。
6. **自动下载会消耗站点额度**：`auto_download_replacement` 默认开启，会调用宿主下载链提交整季全集种子，可能产生 H&R 义务或错过免费期。不希望自动添加下载请关闭，改为手动点击通知里的链接。
7. **等待期与队列**：删前等待期内整包种子连续检索不到即取消清理，不会误删；但宿主调度器每分钟复检会发起站点检索，站点较多时请调大 Cron 间隔。观察窗内该媒体身份被冷却，不会重复清理。
8. **误判防护不等于零风险**：多季合集、分段包、单集、跨季均有硬闸门，且宿主 MetaInfo 优先、宁可漏判不可误判；但站点标题千差万别，请把不希望被处理的订阅写进 `subscribe_whitelist`。
9. **适用范围**：只读取 `SubscribeOper.list()` 中的电视剧订阅。手动添加的种子不在订阅表，天然不受影响；若其他插件也写入订阅表，请用 `subscribe_source_blacklist` 排除其 username。
10. **分级清理不是数据库事务原子操作**：宿主 Oper 每次调用是独立事务，插件按顺序执行并逐步记录成败；任一步失败会在日志与通知中列出，不会静默吞掉。中断后请以日志中的证据链为准人工兜底。
