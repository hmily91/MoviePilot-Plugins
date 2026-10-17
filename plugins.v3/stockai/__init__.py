# -*- coding: utf-8 -*-
"""
__init__.py —— 便于将本插件直接放入 MoviePilot V3 插件仓库。

MoviePilot V3 的插件加载约定（重要，均为实测确认）：
1. 插件物理目录名必须为「插件 id 的小写」：plugins.v3/stockai/（id=StockAi）。
2. 加载器按「模块内变量名 == 插件 id（区分大小写）」匹配插件类，
   因此主类名必须与 plugin.json 的 id 完全一致：StockAi == StockAi。
本文件将 main.py 中定义的 StockAi 主类重新导出，使目录既满足：
    - plugin.json + 主程序代码文件(main.py)的结构要求；
    - 又可直接作为 V3 插件目录（plugins.v3/stockai/）被宿主加载。

导入方式说明：优先使用包内相对导入（宿主以 app.plugins.stockai 包形式加载），
回退绝对导入（个别加载器以模块文件方式直接加载）。
"""
try:
    from .main import StockAi  # 包形式加载（app.plugins.stockai）
except ImportError:  # pragma: no cover
    from main import StockAi  # 模块文件方式直接加载

# 统一图标文件名：宿主更新插件时可能只覆盖入口文件，导致仍沿用旧 main.py 中的
# 图标名（如 StockAi.png）而取不到图标，这里在入口处强制校正。
try:
    StockAi.plugin_icon = "stockai-v2.png"
except Exception:  # pragma: no cover
    pass

__all__ = ["StockAi"]
