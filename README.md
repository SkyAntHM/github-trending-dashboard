# GitHub Trending Dashboard

每日 GitHub 热门项目看板（中文），静态站点部署在 Cloudflare Pages：

**https://github-trending-dashboard.pages.dev/**

## 发布

一键全流程（抓取 → 翻译 → 渲染 → 部署 → 线上验证）：

```bash
python publish.py
```

对 agent 说「发布」即可，agent 会执行 `python publish.py`（翻译失败时由 agent 兜底翻译）。

常用参数：

| 参数 | 说明 |
|---|---|
| `--api-only` | 部署走 CF API 直传，不调 wrangler |
| `--no-deploy` | 只跑抓取/翻译/渲染，不发布 |
| `--no-verify` | 部署后跳过线上验证 |

> 注意：该项目是 CF Pages **direct upload**（`source: null`），
> git push 不会触发自动部署；真正生效的是 wrangler / API 直传。
> Git 仓库仅作源码与产物存档。

## 文件

| 文件 | 作用 |
|---|---|
| `scrape.py` | 抓取 GitHub Trending（daily/weekly/monthly 各 10 条）→ `trending.json`；带 IP 池探测 + 本地代理兜底 |
| `translate.py` | 翻译准备：桥接 `translations.tsv` 缓存；缺失条目输出清单并返回码 2，由 agent（会话模型）翻译后追加到 tsv 重跑；**不调用任何 LLM API** |
| `render.py` | v3 模板 `index_template.html` 注入 `const DATA={...}` → `index.html` |
| `publish.py` | 一键发布编排 + CF 部署（wrangler 优先、API 直传回退）+ 线上验证 |
| `translations.tsv` | 中文翻译缓存（`full_name<TAB>中文`），缺失翻译时桥接回填 |
| `index_template.html` | v3 页面模板（渲染源） |
| `index.html` | 渲染产物（部署内容） |
| `.env` | CF 凭据：`CF_TOKEN`、`CF_ACC`（勿提交） |

## 环境要求

- Python 3.10+（标准库即可，无需 pip 依赖）
- `.env`：`CF_TOKEN`（cfut_ 开头 API Token，需 Pages:Edit）+ `CF_ACC`
- `wrangler`（可选，未安装时自动走 API 直传）
- 翻译：由 agent（本会话模型）完成——缺翻译时 `translate.py` 输出清单，
  翻译结果追加到 `translations.tsv` 后重跑 `python publish.py` 即生效
