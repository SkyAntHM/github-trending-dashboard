#!/usr/bin/env python3
"""
translate.py — 中文翻译准备（不再调用任何 LLM API）

翻译工作由 agent（本会话模型）完成：
  1. `python translate.py`            桥接 translations.tsv 历史缓存；
                                      若有缺失描述，打印清单并返回退出码 2
  2. agent 把翻译结果追加到 translations.tsv（格式: full_name<TAB>中文）
  3. 重新执行 `python publish.py`     桥接自动应用，继续渲染/发布

进程内不发起任何外部 LLM 调用（MiniMax / OpenAI 等已弃用）。
"""
import json
import os
import sys

INPUT = "trending.json"
TSV = "translations.tsv"


def load_tsv() -> dict:
    cached = {}
    if os.path.exists(TSV):
        with open(TSV, encoding="utf-8") as f:
            for line in f:
                if "\t" in line:
                    k, v = line.rstrip("\n").split("\t", 1)
                    cached[k.strip()] = v.strip()
    return cached


def main() -> int:
    with open(INPUT, "r", encoding="utf-8") as f:
        data = json.load(f)

    cached = load_tsv()
    filled = 0
    for p in ("daily", "weekly", "monthly"):
        for repo in data.get(p, []):
            if (not repo.get("description_zh") and repo.get("description")
                    and repo["full_name"] in cached):
                repo["description_zh"] = cached[repo["full_name"]]
                filled += 1
    if filled:
        with open(INPUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[translate] 缓存桥接回填 {filled} 条")

    # 收集缺失（有英文描述但无中文）的唯一仓库
    missing, seen = [], set()
    for p in ("daily", "weekly", "monthly"):
        for repo in data.get(p, []):
            if (repo.get("description") and not repo.get("description_zh")
                    and repo["full_name"] not in seen):
                seen.add(repo["full_name"])
                missing.append({"full_name": repo["full_name"],
                                "description": repo["description"]})

    if missing:
        print(f"[translate] {len(missing)} 条需要 agent 翻译 "
              f"（追加到 {TSV} 后重跑 python publish.py）：", file=sys.stderr)
        for r in missing:
            print(f"  {r['full_name']}\t{r['description']}", file=sys.stderr)
        return 2

    print("[translate] 翻译覆盖 100%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
