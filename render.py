#!/usr/bin/env python3
"""
render.py — v3 版本

读 trending.json + index_template.html (v3 模板), 输出 index.html。

v3 模板的渲染机制：
- HTML/CSS 是固定的（v3 设计）
- 数据通过内嵌 <script> 的 const DATA = { daily: {featured, list}, weekly, monthly } 注入
- 模板里 `${r.rank}` / `${r.slug}` 等占位符由 JS 在浏览器里填充

所以本脚本只需要：
  1. 读取 trending.json
  2. 生成新的 DATA 字符串
  3. 替换模板中 `const DATA={...};` 块
  4. 同时替换 <title> 日期、.brand-date、stats 数字、hero-stat data-count
"""
import json
import re
import sys
from datetime import datetime

TEMPLATE = "index_template.html"
DATA = "trending.json"
OUTPUT = "index.html"


def fmt_stars(n: int) -> str:
    """压缩大数字: 12345 -> 12.3k, 1234567 -> 1.2M"""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}k".replace(".0k", "k")
    return str(n)


def js_str(s: str) -> str:
    """安全地把 Python 字符串转为 JS 字符串字面量"""
    if not s:
        return "''"
    # JSON 编码保证正确转义
    return json.dumps(s, ensure_ascii=False)


def build_panel_js(repos: list[dict]) -> str:
    """把 10 条 repo 转成 JS 的 { featured: [3], list: [7] }"""
    if len(repos) < 10:
        repos = (repos + repos * 3)[:10]
    items = []
    for i, r in enumerate(repos[:10], 1):
        desc = r.get("description_zh") or r.get("description") or ""
        items.append({
            "rank": i,
            "slug": r["full_name"],
            "desc": desc,
            "stars": fmt_stars(r.get("total_stars", 0)),
            "forks": fmt_stars(r.get("forks", 0)),
            "lang": r.get("language", ""),
        })

    def item_to_js(item: dict) -> str:
        return (
            "{"
            f"rank:{item['rank']},"
            f"slug:{js_str(item['slug'])},"
            f"desc:{js_str(item['desc'])},"
            f"stars:{js_str(item['stars'])},"
            f"forks:{js_str(item['forks'])},"
            f"lang:{js_str(item['lang'])}"
            "}"
        )

    featured = ",\n      ".join(item_to_js(x) for x in items[:3])
    list_ = ",\n      ".join(item_to_js(x) for x in items[3:])
    return f"{{\n    featured:[\n      {featured}\n    ],\n    list:[\n      {list_}\n    ]\n  }}"


def main() -> int:
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    with open(DATA, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. 生成新的 DATA 块
    panels_js = ",\n  ".join(
        f"{p}:{build_panel_js(data.get(p, []))}"
        for p in ("daily", "weekly", "monthly")
    )
    new_data_block = f"const DATA={{\n  {panels_js}\n}};"

    # 2. 替换 `const DATA={...};` 整个块（用大括号配对匹配）
    new_html, count = re.subn(
        r"const DATA=\{[\s\S]*?\};",
        lambda m: new_data_block,
        html,
        count=1,
    )
    if count == 0:
        print("[render] 警告：未匹配到 const DATA 块", file=sys.stderr)
        return 1

    # 3. 替换 <title> 日期
    today = datetime.now().strftime("%Y-%m-%d")
    new_html = re.sub(
        r"(<title>GitHub Trending - )[\d-]+(</title>)",
        f"\\g<1>{today}\\g<2>",
        new_html,
        count=1,
    )

    # 4. 替换 .brand-date 日期
    new_html = re.sub(
        r'(<div class="brand-date">)[\d-]+(</div>)',
        f"\\g<1>{today}\\g<2>",
        new_html,
        count=1,
    )

    # 5. 替换 header 右侧 stat-pill (跟踪项目数 / 编程语言数)
    stats = data.get("stats", {})
    total_repos = stats.get("total_repos", 30)
    languages_count = stats.get("languages_count", 0)
    new_html = re.sub(
        r'(<div class="stat-pill"><i class="ti ti-flame"></i>)\d+(\s*热门项目</div>)',
        f"\\g<1>{total_repos}\\g<2>",
        new_html,
        count=1,
    )
    new_html = re.sub(
        r'(<div class="stat-pill"><i class="ti ti-code"></i>)\d+(\s*种语言</div>)',
        f"\\g<1>{languages_count}\\g<2>",
        new_html,
        count=1,
    )

    # 6. 替换 hero-stat data-count (26/8/24)
    new_html = re.sub(
        r'(<div class="hero-stat-val" data-count=")\d+(">0</div><div class="hero-stat-label">跟踪项目)',
        f"\\g<1>{total_repos}\\g<2>",
        new_html,
        count=1,
    )
    new_html = re.sub(
        r'(<div class="hero-stat-val" data-count=")\d+(">0</div><div class="hero-stat-label">编程语言)',
        f"\\g<1>{languages_count}\\g<2>",
        new_html,
        count=1,
    )
    # 24h 更新保持不变（不需要改）

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"[render] v3 写入 {OUTPUT}  ({len(new_html)} 字符)")
    print(f"  daily/weekly/monthly 各 10 条，total_repos={total_repos}, languages={languages_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
