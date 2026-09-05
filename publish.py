#!/usr/bin/env python3
"""
publish.py — GitHub Trending Dashboard 一键发布

用法:
    python publish.py              # 完整流程: scrape -> translate -> render -> deploy -> verify
    python publish.py --api-only   # 部署走 CF API 直传 (不调 wrangler)
    python publish.py --no-deploy  # 只到渲染, 不发布不验证
    python publish.py --no-verify  # 部署后跳过线上验证

流程:
  1. scrape.py    抓取 GitHub Trending -> trending.json（失败即中止，绝不发布旧数据）
  2. translate.py LLM 批量翻译 description_zh（429 自动退避重试）
  3. translations.tsv 缓存桥接：为缺失翻译的条目回填历史缓存
  4. render.py    渲染 index.html（校验 const DATA / 日期 / 中文）
  5. 部署: wrangler 后台启动 + 轮询 CF API 确认 deployment success；
            wrangler 不可用/失败时回退 CF API multipart 直传
  6. 验证线上 https://github-trending-dashboard.pages.dev/（重试直至标题=今日/const DATA/中文）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "github-trending-dashboard"
LIVE_URL = f"https://{PROJECT}.pages.dev/"
TODAY = datetime.now().strftime("%Y-%m-%d")
PROXY = "http://127.0.0.1:7897"


def log(msg: str) -> None:
    print(f"[publish] {msg}", flush=True)


# ---------- 凭据 ----------

def load_cf_env() -> dict:
    env = {}
    with open(os.path.join(BASE, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    token, acc = env.get("CF_TOKEN", ""), env.get("CF_ACC", "")
    if not token or not acc:
        raise RuntimeError(".env 缺少 CF_TOKEN / CF_ACC")
    return {"token": token, "acc": acc}


def cf_get(cf: dict, path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        headers={"Authorization": "Bearer " + cf["token"]},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def latest_deployment(cf: dict) -> dict | None:
    d = cf_get(cf, f"/accounts/{cf['acc']}/pages/projects/{PROJECT}/deployments?per_page=1")
    res = d.get("result") or []
    return res[0] if res else None


# ---------- 1. 抓取 ----------

def step_scrape() -> dict:
    log("1/5 抓取 GitHub Trending ...")
    r = subprocess.run([sys.executable, "scrape.py"], cwd=BASE, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("scrape.py 失败，中止发布（不发布旧/错数据）")
    with open(os.path.join(BASE, "trending.json"), encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("updated_at", "").startswith(TODAY):
        raise RuntimeError(f"trending.json 不是今日数据: {data.get('updated_at')}")
    total = sum(len(data.get(p, [])) for p in ("daily", "weekly", "monthly"))
    if total < 30:
        raise RuntimeError(f"条目不足 30: {total}")
    log(f"OK 今日 {data['updated_at']}，{total} 条，"
        f"unique={data['stats']['total_repos']} languages={data['stats']['languages_count']}")
    return data


# ---------- 2/3. 翻译 + 缓存桥接 ----------

def step_translate(data: dict) -> None:
    log("2/5 LLM 翻译描述 ...")
    subprocess.run([sys.executable, "translate.py"], cwd=BASE, timeout=600)

    tsv = os.path.join(BASE, "translations.tsv")
    cached = {}
    if os.path.exists(tsv):
        with open(tsv, encoding="utf-8") as f:
            for line in f:
                if "\t" in line:
                    k, v = line.rstrip("\n").split("\t", 1)
                    cached[k.strip()] = v.strip()

    filled = 0
    for p in ("daily", "weekly", "monthly"):
        for repo in data.get(p, []):
            if (not repo.get("description_zh") and repo.get("description")
                    and repo["full_name"] in cached):
                repo["description_zh"] = cached[repo["full_name"]]
                filled += 1
    if filled:
        with open(os.path.join(BASE, "trending.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"缓存桥接回填 {filled} 条")

    with_desc = {r["full_name"] for p in ("daily", "weekly", "monthly")
                 for r in data.get(p, []) if r.get("description")}
    zh_ok = {r["full_name"] for p in ("daily", "weekly", "monthly")
             for r in data.get(p, []) if r.get("description_zh")}
    missing = sorted(with_desc - zh_ok)
    if missing:
        log(f"警告 {len(missing)} 条缺少中文翻译（将显示英文）: " + ", ".join(missing))
    else:
        log("OK 翻译覆盖 100%")


# ---------- 4. 渲染 ----------

def step_render() -> None:
    log("3/5 渲染 index.html ...")
    r = subprocess.run([sys.executable, "render.py"], cwd=BASE, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("render.py 失败")
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        html = f.read()
    data_cnt = len(re.findall(r"const DATA=", html))
    title = re.search(r"<title>([^<]*)</title>", html)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", html))
    ok = data_cnt == 1 and title and TODAY in title.group(1) and cjk > 100
    log(f"const DATA={data_cnt} title={title.group(1) if title else 'N/A'} CJK={cjk}")
    if not ok:
        raise RuntimeError("index.html 校验失败")
    log("OK index.html 就绪")


# ---------- 5. 部署 ----------

def poll_deployment(cf: dict, before_id: str | None, timeout: int = 150) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(10)
        dep = latest_deployment(cf)
        if not dep or dep.get("id") == before_id:
            continue
        stage = dep.get("latest_stage") or {}
        log(f"deployment {dep.get('short_id')} stage={stage.get('name')}/{stage.get('status')}")
        if stage.get("status") == "success":
            return dep
        if stage.get("status") == "failure":
            return None
    return None


def deploy_wrangler(cf: dict) -> dict | None:
    """wrangler 后台启动（不等待进程退出——它偶尔挂起），轮询 CF API 确认成功"""
    wr = shutil.which("wrangler")
    if not wr:
        log("wrangler 未安装，直接走 API 直传")
        return None
    before = latest_deployment(cf)
    before_id = before.get("id") if before else None
    env = {
        **os.environ,
        "CLOUDFLARE_API_TOKEN": cf["token"],
        "CLOUDFLARE_ACCOUNT_ID": cf["acc"],
        "HTTPS_PROXY": PROXY, "HTTP_PROXY": PROXY,
        "https_proxy": PROXY, "http_proxy": PROXY,
    }
    cmd = f'"{wr}" pages deploy . --project-name={PROJECT} --commit-dirty=true'
    log(f"wrangler 后台部署 (pid 后台运行) ...")
    p = subprocess.Popen(cmd, shell=True, cwd=BASE, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        dep = poll_deployment(cf, before_id, timeout=150)
    finally:
        if p.poll() is None:
            p.kill()
    return dep


def deploy_api(cf: dict) -> dict | None:
    """CF Pages API multipart 直传（wrangler 失败时的回退）"""
    with open(os.path.join(BASE, "index.html"), "rb") as f:
        idx = f.read()
    boundary = "----publish" + uuid.uuid4().hex
    parts = [
        (f"--{boundary}").encode(),
        b'Content-Disposition: form-data; name="manifest"',
        b"",
        json.dumps({"index.html": len(idx)}).encode(),
        (f"--{boundary}").encode(),
        b'Content-Disposition: form-data; name="index.html"; filename="index.html"',
        b"Content-Type: text/html; charset=utf-8",
        b"",
        idx,
        (f"--{boundary}--").encode(),
        b"",
    ]
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{cf['acc']}/pages/projects/{PROJECT}/deployments",
        data=body,
        headers={
            "Authorization": "Bearer " + cf["token"],
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    log("CF API 直传 ...")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log(f"API 部署失败: {e.code} {e.read().decode()[:300]}")
        return None
    if not resp.get("success"):
        log(f"API 部署失败: {resp.get('errors')}")
        return None
    dep_id = (resp.get("result") or {}).get("id")
    return poll_deployment(cf, None, timeout=150) if dep_id else None


def step_deploy(cf: dict, api_only: bool) -> str | None:
    log("4/5 部署到 Cloudflare Pages ...")
    dep = None
    if not api_only:
        dep = deploy_wrangler(cf)
    if dep is None:
        log("回退 CF API 直传 ...")
        dep = deploy_api(cf)
    if dep is None:
        raise RuntimeError("两种部署方式都失败")
    sid = dep.get("short_id") or dep.get("id") or "?"
    log(f"OK 部署成功 deployment={sid}")
    return sid


# ---------- 6. 验证 ----------

def step_verify() -> None:
    log("5/5 验证线上 URL ...")
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(LIVE_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", errors="replace")
            data_cnt = len(re.findall(r"const DATA=", html))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", html))
            ok = TODAY in html and data_cnt == 1 and cjk > 100
            log(f"尝试 {attempt}: DATA={data_cnt} CJK={cjk} 今日日期={'在' if TODAY in html else '不在'}")
            if ok:
                log(f"OK 线上已更新: {LIVE_URL}")
                return
        except Exception as e:
            log(f"尝试 {attempt} 失败: {e}")
        time.sleep(8)
    raise RuntimeError(f"线上验证失败: {LIVE_URL}")


# ---------- main ----------

def main() -> int:
    api_only = "--api-only" in sys.argv
    no_deploy = "--no-deploy" in sys.argv
    no_verify = "--no-verify" in sys.argv

    try:
        cf = None if (no_deploy and not api_only) else load_cf_env()
        if cf:
            # 预检凭据
            cf_get(cf, "/user/tokens/verify")
            log("CF 凭据有效")
        data = step_scrape()
        step_translate(data)
        step_render()
        if not no_deploy:
            step_deploy(cf, api_only)
            if not no_verify:
                step_verify()
        log("=== 发布流程完成 ===")
        return 0
    except Exception as e:
        log(f"发布失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
