# -*- coding: utf-8 -*-
"""
金山文档每日影视上新表 → JSON 快照（供 GitHub Actions 定时运行）。

数据流：金山文档分享页（需浏览器渲染）→ 本脚本提取最近 N 天的分节片名
       → 写入 repo/data/kdocs_new.json → 插件每日拉取该快照推送提醒。

用法（在 GitHub Actions 或本机）：
    python tools/kdocs_fetch_ci.py --days 3 --output data/kdocs_new.json
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

KDOCS_URL = "https://www.kdocs.cn/l/caRody46qOEl"
DATE_HEAD_RE = re.compile(r"^20\d{2}\.\d{2}\.\d{2}\s*周.更新的")

# 与插件端 _KDOCS_NOISE_RE 保持一致：剔除链接、推广、时间点、导航行
NOISE_RE = re.compile(
    r"^https?://|pan\.quark|pan\.xunlei|夸克|迅雷|^●|^【?\d{1,2}[:：]\d{2}】?$"
    r"|合集|微博|抖音|QQ|赞赏|防走丢|红包|会员|外卖|进群|搜索方法|注意[:：]|转存"
    r"|^\.\S*更新的?$|拼音更新|\d+\s*(部|个)$|更新的$|未命名|正在协作|个字$"
    r"|^[-—=—-]+$|^&nbsp;$"
)
EPISODE_RE = re.compile(
    r"第\s*\d+\s*集|更新至\s*\d+|更新到\s*\d+|至\s*\d+\s*集|更新\s*\d+\s*集|更新\s*\d+|EP\s*\d+",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    text = title.strip()
    text = re.sub(r"【[^】]*】", "", text)
    text = re.sub(r"\]\s*\[[^\]]*\]", "", text)
    text = EPISODE_RE.sub("", text)
    text = re.sub(r"全\s*\d+\s*集|全集|完结", "", text)
    text = re.sub(r"(4K|8K|2160p|1080p|720p|HDR|杜比|IMAX)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\(（\[【]?\s*(19|20)\d{2}\s*[\)）\]】]?", "", text)
    text = re.sub(r"[\]］〕]+$", "", text).strip(" 　:-·、，,")
    return text.strip()


def date_str(offset: int) -> str:
    d = datetime.date.today() - datetime.timedelta(days=offset)
    return f"{d.year}.{d.month:02d}.{d.day:02d}"


def extract(days: int) -> dict:
    from playwright.sync_api import sync_playwright

    sections: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-CN",
            viewport={"width": 1680, "height": 950},
        )
        page = ctx.new_page()
        page.goto(KDOCS_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_selector(".otl-scroll-container", timeout=90_000)
        page.wait_for_timeout(8_000)

        for offset in range(days):
            date = date_str(offset)
            lines = []
            try:
                lines = extract_section(page, date)
            except Exception as err:  # 单日失败不影响其他日期
                print(f"[kdocs] {date} 目录跳转提取失败（{err}），尝试滚动回退", file=sys.stderr)
                try:
                    lines = extract_by_fullscroll(page, date)
                except Exception as err2:
                    print(f"[kdocs] {date} 滚动回退也失败：{err2}", file=sys.stderr)
                    lines = []
            sections[date] = lines
            print(f"[kdocs] {date}: {len(lines)} 条")
        browser.close()

    # 去噪 + 清洗（保持与插件端解析一致；分类在清洗前基于原始行判断）
    result = {}
    for date, lines in sections.items():
        entries = []
        seen_names = set()
        for t in lines:
            t = (t or "").strip()
            if not t or NOISE_RE.search(t) or DATE_HEAD_RE.match(t):
                continue
            name = clean_title(t)
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            entries.append({"name": name, "type": "movie" if looks_like_movie(t) else "tv"})
        result[date] = entries
    return result


def looks_like_movie(title: str) -> bool:
    """与插件端 _KdocsCloudSource._looks_like_movie 保持一致。"""
    if "电影上线" in title or "【电影" in title:
        return True
    if re.search(r"[\]］]\s*[\[（(]?\d{4}[\)）]?[\]］]", title):
        return True
    return False


def extract_section(page, date: str) -> list:
    """点目录跳转到指定日期分节，渐进滚动收集到下一个日期标题为止。"""
    # 确保目录面板可见：先等，再点「目录」页签
    if page.locator(".catalog-item").count() == 0:
        tab = page.locator(".catalog-tab-item").first
        if tab.count():
            tab.click(timeout=15_000)
            page.wait_for_timeout(1_200)
    page.wait_for_selector(".catalog-item", timeout=20_000)
    cat = page.locator(".catalog-item", has_text=date).first
    cat.click(timeout=20_000)
    page.wait_for_timeout(2500)

    # 关闭可能弹出的协作提示
    page.evaluate(
        "document.querySelectorAll('[class*=mask],[class*=modal],[class*=dialog]')"
        ".forEach(e => { const b = e.querySelector('[class*=close],[class*=Close]'); if (b) b.click(); });"
    )
    page.wait_for_timeout(500)

    seen, lines = set(), []
    stagnant, last_top = 0, -1
    date_head = re.compile(r"^20\d{2}\.\d{2}\.\d{2}\s*周")
    for _ in range(400):
        text = page.evaluate(
            "(document.querySelector('.otl-scroll-container')||{}).innerText || ''"
        )
        for raw in text.split("\n"):
            t = raw.strip()
            if t and len(t) > 1 and t not in seen:
                seen.add(t)
                lines.append(t)
        # 命中下一个日期标题：截断
        for j in range(len(lines) - 1, -1, -1):
            if date_head.match(lines[j]):
                if not lines[j].startswith(date) and j > 3:
                    return lines[:j]
                break
        page.evaluate(
            "document.querySelector('.otl-scroll-container').scrollBy(0, 900);"
        )
        page.wait_for_timeout(140)
        top = page.evaluate(
            "document.querySelector('.otl-scroll-container').scrollTop"
        )
        if top == last_top:
            stagnant += 1
            if stagnant > 15:
                break
        else:
            stagnant, last_top = 0, top
        if len(lines) > 3000:
            break
    return lines


def extract_by_fullscroll(page, date: str) -> list:
    """
    回退方案：从顶部整页滚动收集所有行，再切出目标日期到下一日期标题之间的内容。
    速度慢但完全不依赖目录面板。返回目标日期分节的行（可能混入少量相邻节内容）。
    """
    target_idx = None
    date_head = re.compile(r"^20\d{2}\.\d{2}\.\d{2}\s*周")
    # 目标日期所在序号：所有日期标题按出现顺序排，取目标日期的标题位置之后的内容
    dates_ordered = [date_str(i) for i in range(0, 10)]
    scroller = page.locator(".otl-scroll-container")
    scroller.evaluate("el => { el.scrollTop = 0; }")
    page.wait_for_timeout(1_000)

    seen, lines = set(), []
    stagnant, last_top = 0, -1
    stop_hit = False
    for _ in range(800):
        text = scroller.evaluate("el => el.innerText || ''")
        for raw in text.split("\n"):
            t = raw.strip()
            if t and len(t) > 1 and t not in seen:
                seen.add(t)
                lines.append(t)
                m = date_head.match(t)
                if m and m.group(0).startswith(date):
                    target_idx = len(lines) - 1
                # 滚过目标日期的下一天标题后即可停止
                if m and target_idx is not None:
                    next_dates = [d for d in dates_ordered if d < date]
                    if any(t.startswith(nd) for nd in next_dates):
                        stop_hit = True
        if stop_hit:
            break
        scroller.evaluate("el => el.scrollBy(0, 1500);")
        page.wait_for_timeout(150)
        top = scroller.evaluate("el => el.scrollTop")
        if top == last_top:
            stagnant += 1
            if stagnant > 15:
                break
        else:
            stagnant, last_top = 0, top
        if len(lines) > 8000:
            break

    if target_idx is None:
        raise RuntimeError("滚动收集中未找到目标日期标题")
    # 截取目标日期标题之后、下一个日期标题之前的内容
    out = []
    for t in lines[target_idx + 1:]:
        if date_head.match(t):
            break
        out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="提取最近 N 天（默认 3）")
    ap.add_argument("--output", default="data/kdocs_new.json")
    args = ap.parse_args()

    sections = extract(args.days)
    snapshot = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": KDOCS_URL,
        "sections": sections,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[kdocs] 快照已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
