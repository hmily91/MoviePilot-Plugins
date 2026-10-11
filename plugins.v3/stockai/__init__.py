# -*- coding: utf-8 -*-
"""
__init__.py —— 便于将本插件直接放入 MoviePilot V3 插件仓库。

MoviePilot V3 规定插件主类必须定义在 plugins.v3/<plugin_id小写>/__init__.py 中。
本文件将 main.py 中定义的 StockAi 主类重新导出，使目录既满足：
    - 用户要求的“plugin.json + 主程序代码文件(main.py)”结构；
    - 又可直接作为 V3 插件目录（plugins.v3/stockai/）被宿主加载。
安装时把整个 moviepilot-plugin-stock-ai 目录重命名为 stockai 放入 plugins.v3/ 即可。
"""
from main import StockAi  # noqa: F401

__all__ = ["StockAi"]
