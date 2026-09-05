#!/usr/bin/env python3
"""
translate.py — 用 LLM 翻译 trending.json 中所有仓库描述为中文
读 trending.json, 加 description_zh 字段, 写回

支持两种 LLM provider（按顺序自动检测）：
  1. 优先用 hermes 当前模型 (MINIMAX_API_KEY 环境变量) — 来自 ~/.hermes/.env
  2. 兜底用 OpenAI 兼容 API (OPENAI_API_KEY + OPENAI_BASE_URL)
  3. 再兜底 hermes-agent 的 custom:minimax 凭据（读 ~/.hermes/config.yaml）

设计要点：
- 一次调用批量翻译 30 条（不是 30 次调用），节省 token
- 容忍少量翻译失败：失败条目的 description_zh 留空字符串（前端展示英文）
- 自动去重：跨 daily/weekly/monthly 重复的仓库只翻译一次
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import yaml

CONFIG_PATH = os.path.expanduser("~/.hermes/config.yaml")
ENV_PATH = os.path.expanduser("~/.hermes/.env")
INPUT = "trending.json"
OUTPUT = "trending.json"  # 原地写回

TRANSLATE_PROMPT = """你是 GitHub 项目技术翻译专家。请将下列仓库描述翻译成**简洁自然的中文**。

要求：
- 每条 1-2 句话，不超过 80 个汉字
- 准确传达技术含义，保留关键专有名词（保留英文原词即可，如 RAG / WebAssembly / LLM / kernel / SDK 等不需要翻译的术语）
- 语气客观，类似产品介绍文案
- 输出格式：每条一行，顺序与输入严格一致
- 严格禁止：添加编号、引号、"以下是翻译"等元说明；不要给空描述生成内容

输入描述（每行一条，空行代表无描述）：
{descriptions}

翻译（严格按行数对应输出，每行一条翻译）："""


def load_api_credentials() -> tuple[str, str, str] | None:
    """
    返回 (api_key, base_url, model) 或 None
    优先级：环境变量 > ~/.hermes/.env > ~/.hermes/config.yaml
    """
    env_lines = {}
    for p in (ENV_PATH, os.path.expanduser("~/.env")):
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env_lines[k.strip()] = v.strip()

    api_key = os.environ.get("MINIMAX_API_KEY") or env_lines.get("MINIMAX_API_KEY")
    base_url = os.environ.get("MINIMAX_BASE_URL") or env_lines.get("MINIMAX_BASE_URL") or "https://api.minimaxi.com/v1"
    model = os.environ.get("MINIMAX_MODEL") or env_lines.get("MINIMAX_MODEL") or "MiniMax-M3"

    if not api_key:
        # 从 hermes config 读
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                providers = cfg.get("providers") or cfg.get("custom_providers") or []
                # 兼容新版 hermes config：凭据在 custom_providers（minimax / mimo）
                for p in providers:
                    if p.get("name") in ("minimax", "mimo"):
                        api_key = p.get("api_key")
                        base_url = p.get("base_url", base_url)
                        model = p.get("model", model)
                        break
            except Exception as e:
                print(f"[translate] 解析 config.yaml 失败: {e}", file=sys.stderr)

    if not api_key:
        return None
    return api_key, base_url, model


def call_llm(api_key: str, base_url: str, model: str, descriptions: list[str]) -> list[str]:
    """调用 LLM 翻译，返回与输入等长的翻译列表"""
    numbered = "\n".join(d if d.strip() else "<EMPTY>" for d in descriptions)
    prompt = TRANSLATE_PROMPT.format(descriptions=numbered)

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "你是专业的技术翻译。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    reply = data["choices"][0]["message"]["content"].strip()

    # 按行切分；同时容忍模型偶尔在中间插入空行
    lines = [l.strip() for l in reply.splitlines() if l.strip()]
    # 与输入对齐
    result = []
    for i, src in enumerate(descriptions):
        if i < len(lines):
            result.append(lines[i])
        elif not src.strip():
            result.append("")  # 原为空
        else:
            result.append("")  # 翻译缺失
    return result


def call_llm_with_retry(api_key: str, base_url: str, model: str, descriptions: list[str],
                        retries: int = 4) -> list[str]:
    """call_llm + 429/网络抖动自动重试（退避 20/40/60/80s）"""
    for attempt in range(retries):
        try:
            return call_llm(api_key, base_url, model, descriptions)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                print(f"[translate] 429 限流，{wait}s 后重试 ({attempt + 1}/{retries - 1})")
                time.sleep(wait)
                continue
            raise
        except Exception:
            raise
    raise RuntimeError("translation failed after retries")


def main() -> int:
    if not os.path.exists(INPUT):
        print(f"[translate] 找不到 {INPUT}，请先运行 scrape.py", file=sys.stderr)
        return 1

    with open(INPUT, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 收集所有唯一 (full_name, description) — 同一仓库跨周期只翻译一次
    unique = {}  # full_name -> description
    for period in ("daily", "weekly", "monthly"):
        for repo in data.get(period, []):
            if repo["full_name"] not in unique and repo["description"]:
                unique[repo["full_name"]] = repo["description"]

    if not unique:
        print("[translate] 无描述需要翻译，跳过")
        return 0

    print(f"[translate] 待翻译 {len(unique)} 个唯一仓库")

    creds = load_api_credentials()
    if not creds:
        print("[translate] 警告：未找到 LLM API key，描述将保持英文", file=sys.stderr)
        print(f"[translate] 提示：在 ~/.hermes/.env 设置 MINIMAX_API_KEY=你的key", file=sys.stderr)
        # 不阻塞流程：保留 description_zh 为空字符串（前端会回退到英文）
        for period in ("daily", "weekly", "monthly"):
            for repo in data.get(period, []):
                repo["description_zh"] = ""
        with open(OUTPUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return 0

    api_key, base_url, model = creds
    print(f"[translate] 使用 {model} @ {base_url}")

    # 批量翻译
    items = list(unique.items())
    descriptions = [d for _, d in items]

    # 单批最多 30 条
    BATCH = 30
    translations = {}
    for i in range(0, len(descriptions), BATCH):
        batch_descs = descriptions[i:i + BATCH]
        batch_names = [n for n, _ in items[i:i + BATCH]]
        try:
            zh = call_llm_with_retry(api_key, base_url, model, batch_descs)
            for n, z in zip(batch_names, zh):
                translations[n] = z
            print(f"[translate] 批次 {i // BATCH + 1} 翻译 {len(zh)} 条")
        except Exception as e:
            print(f"[translate] 批次翻译失败: {e}", file=sys.stderr)
            for n in batch_names:
                translations[n] = ""

    # 写回
    for period in ("daily", "weekly", "monthly"):
        for repo in data.get(period, []):
            repo["description_zh"] = translations.get(repo["full_name"], "")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[translate] 完成，已更新 {OUTPUT}")
    # 抽样展示
    sample = data["daily"][0] if data.get("daily") else None
    if sample:
        print(f"[translate] 样本：{sample['full_name']}")
        print(f"  EN: {sample['description'][:80]}")
        print(f"  ZH: {sample['description_zh'][:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
