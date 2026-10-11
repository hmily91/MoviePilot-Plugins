# 股票AI分析助手（MoviePilot V3 插件）

获取沪深A股实时行情与近 5 日 K 线，复用系统「智能助手」或自定义 LLM 接口进行技术面 / 短期走势 / 风险提示分析，并定时推送早盘预判与收盘分析报告。

---

## 一、功能特性

- **行情获取**：东方财富公开接口，无需密钥；支持沪深A股（含创业板），单只请求间隔 1 秒防限流。
- **LLM 智能分析**：默认复用系统「智能助手」已配置的 LLM（API Key / 接口地址 / 模型），也可在配置页自定义 OpenAI 兼容接口；失败自动重试 2 次（间隔 3 秒）。
- **定时任务**：内置「早盘预判（默认 09:00）」「收盘分析（默认 15:30）」，时间可在配置页自定义；支持「立即执行分析」手动触发。
- **消息推送**：复用 MoviePilot 全局消息通道（微信 / 钉钉 / Telegram 等），按股票分组推送。
- **历史分析前端页面**：插件详情页展示历史分析记录（见第六章），支持「全部 / 早盘预判 / 收盘分析」筛选与「立即分析」「清空历史」操作。
- **持久化**：自选股（配置）、历史分析记录（插件专属数据目录，使用系统原生 `save_data` / `get_data`）重启不丢失。
- **安全合规**：所有异常均捕获并记录日志，绝不中断主程序；AI 结论强制标注「仅为技术分析参考，不构成投资建议」。

---

## 二、目录结构（本仓库即一个 V3 插件源）

```text
moviepilot-plugin-stock-ai/            # 仓库根（MoviePilot V3 插件源）
├── package.v3.json          # 全局插件市场索引（id -> 元数据），MoviePilot 据此在市场展示插件
├── README.md                # 仓库（插件源）说明
├── icons/stockai.png        # 插件图标
├── push_to_github.py        # 创建/推送本仓库到 GitHub 的脚本（Token 从环境变量读取）
├── gen_icon.py              # 生成 icons/stockai.png 的脚本（仅开发用）
└── plugins.v3/              # V3 插件代码目录（MoviePilot 扫描此目录识别插件）
    └── stockai/             # 插件目录（目录名 = 插件 id = stockai）
        ├── plugin.json      # 插件元数据 + 配置项 Schema
        ├── main.py          # 插件主程序代码
        ├── __init__.py      # 重导出 StockAi，便于宿主加载
        ├── package.v3.json  # 单插件元数据（与全局索引条目对应）
        ├── README.md        # 插件说明（本文件）
        └── tests/           # 本地联调集成测试（SDK 模拟，无需真实 MoviePilot）
            ├── __init__.py
            ├── mock_moviepilot.py   # 忠实模拟 app.sdk / requests / apscheduler 的最小桩
            └── run_integration.py   # 36 项联调用例，覆盖生命周期/表单/API/定时/分析流水线/页面/异常
```

---

## 三、安装方法（两种）

### 方式 A：作为插件源加入「插件市场」（推荐）

本仓库本身就是一个符合规范的 V3 插件源，把仓库地址加入 MoviePilot 后即可在市场里搜索、安装、更新：

1. 打开 MoviePilot → **插件** 页面 → **插件市场设置**（或「添加第三方仓库」）。
2. 在「插件仓库地址」中加入本仓库地址（多个用逗号分隔）：
   ```
   https://github.com/hmily91/moviepilot-plugin-stock-ai
   ```
   > 也可通过环境变量 `PLUGIN_MARKET` 配置（Docker 用户写在 `docker-compose.yml` 的 environment 中），例如：
   > `PLUGIN_MARKET=https://github.com/jxxghp/MoviePilot-Plugins/,https://github.com/hmily91/moviepilot-plugin-stock-ai`
3. 点击「同步插件源」（或重启 MoviePilot）。
4. 在插件市场中搜索「股票AI分析助手」，点击安装并启用。

### 方式 B：本地目录安装（开发者 / 调试）

1. 将本仓库的 `plugins.v3/stockai/` 整个目录，复制到 MoviePilot 的 `plugins.v3/stockai/` 目录下
   （即最终路径为 `plugins.v3/stockai/__init__.py`、`plugins.v3/stockai/main.py` 等）。
   - Docker 部署：映射到容器内 `/config/plugins.v3/stockai/` 或 `/app/plugins/stockai/`（以你的挂载为准）。
2. 重启 MoviePilot，在插件管理中启用。

> 说明：V3 规定插件主类位于 `plugins.v3/<id>/__init__.py`。本插件 `__init__.py` 已通过 `from main import StockAi` 重导出，因此把 `stockai` 目录放入 `plugins.v3/` 即可被宿主识别；插件 id 为 `stockai`（小写），与目录名、全局索引 key 三者一致。

---

## 四、配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用插件 | 开关 | 关闭 | 开启后才注册定时任务 |
| 立即执行分析 | 开关 | 关闭 | 保存后立即运行一次（运行后自动关闭），用于测试 |
| 自选股票代码 | 多行文本 | 空 | 沪深A股代码，英文逗号分隔，如 `600519,000001,300750` |
| 早盘预判执行时间 | 文本 | 09:00 | 格式 HH:MM |
| 收盘分析执行时间 | 文本 | 15:30 | 格式 HH:MM |
| 推送早盘预判 | 开关 | 开启 | 是否推送开市前报告 |
| 推送收盘分析 | 开关 | 开启 | 是否推送闭市后报告 |
| 休市日自动跳过 | 开关 | 开启 | 周末及内置休市日不执行 |
| 通知标题 | 文本 | 📈 股票AI分析报告 | 自定义推送标题 |
| LLM配置来源 | 下拉 | 跟随系统智能助手 | 跟随系统 / 自定义 |
| 自定义LLM API Key | 密码 | 空 | 来源为自定义时生效 |
| 自定义LLM接口地址 | 文本 | 空 | 兼容 OpenAI 格式 |
| 自定义模型名称 | 文本 | 空 | 自定义模型名 |

**LLM 配置来源 = 跟随系统智能助手** 时，插件自动读取系统「智能助手」模块配置的 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`，无需重复填写。

---

## 五、使用流程

1. 在系统「智能助手」中配置好 LLM（或在插件配置页选择「自定义配置」并填写）。
2. 启用插件，填写自选股代码，按需调整执行时间与推送开关。
3. 保存配置；如需立即验证，打开「立即执行分析」开关后保存。
4. 每日 09:00 / 15:30 将自动生成并推送分析报告。

---

## 六、历史分析前端页面

插件详情页内置「历史分析」页面，用于回看每次自动 / 手动分析的结果：

- **统计卡**：显示历史总条数与当前筛选下的展示条数。
- **筛选**：点击「全部 / 早盘预判 / 收盘分析」按钮，调用插件 API `/set_filter` 并自动刷新页面。
- **操作**：
  - 「立即分析」：调用 `/run`，手动对早盘 + 收盘各分析一次并写入历史（不受休市跳过限制），便于即时验证。
  - 「清空历史」：调用 `/clear`，清空全部历史记录。
- **记录列表**：按时间倒序展示每次分析的时间、类型（早盘预判 / 收盘分析）、各股票名称 / 代码与 AI 分析结论。

> 页面为服务端根据持久化数据实时渲染，调用 API 后自动刷新；结论文本经 HTML 转义，避免 XSS 与排版错乱。

---

## 七、本地联调与真实 MoviePilot 安装

### 7.1 本地联调（SDK 模拟，无需真实 MoviePilot）

本仓库 `tests/` 提供集成测试，用最小桩忠实模拟 MoviePilot V3 SDK（`app.sdk` 与 `requests` / `apscheduler`），
把插件按 V3 契约加载并跑通逻辑，**不发起任何真实网络请求**：

```bash
cd moviepilot-plugin-stock-ai
python tests/run_integration.py
```

覆盖 36 项用例：插件加载与生命周期、配置表单、4 个插件 API 路由声明、定时任务注册、
完整分析流水线（mock 行情 + LLM）、历史持久化、`get_page` 页面渲染、异常处理（行情异常 / LLM 异常不中断流程）。
全部通过即代表插件与 V3 契约兼容、核心逻辑自洽。

> 说明：当前沙箱无 Docker、未安装 MoviePilot，因此完整 MoviePilot 运行时无法在本机启动；
> 联调以 SDK 桩验证契约与逻辑，真实部署见 7.2。

### 7.2 部署到真实 MoviePilot

**方式 A（插件源市场，推荐）**：按第三章方式 A，把本仓库地址加入 MoviePilot 的「插件仓库地址」并同步，
随后在市场搜索「股票AI分析助手」安装启用。

**方式 B（本地目录）**：把本仓库 `plugins.v3/stockai/` 目录整体复制到 MoviePilot 的 `plugins.v3/stockai/` 下，重启并启用。

启用后填写自选股与 LLM 配置并保存；在插件详情页查看「历史分析」页面，可点击「立即分析」即时生成一条历史记录验证效果。

> 若使用 Docker 部署 MoviePilot，可参考官方 `docker-compose.yml`，按方式 A 配置 `PLUGIN_MARKET` 环境变量，或按方式 B 挂载 `plugins.v3/stockai/` 到容器内。

---

## 八、合规声明

本插件所有 AI 分析结论均明确标注「仅为技术分析参考，不构成投资建议」。插件不对任何投资决策负责，使用者需自行承担风险。
