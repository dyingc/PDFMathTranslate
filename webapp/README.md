# PDF 翻译小应用（DeepSeek）

在 pdf2zh 之上加一层极简 Web UI：上传 PDF → 选语言/模型 → 下载译文。公式、图表版式由 pdf2zh 的
版面模型保留，本应用不碰这部分逻辑。

## 特性

- 后端模型：`deepseek-v4-flash` / `deepseek-v4-pro`（DeepSeek OpenAI 兼容接口）
- API Key **只存在服务端进程内存**中：
  - 保存时会先调一次 DeepSeek `/models` 做校验
  - 浏览器只拿到一个 `sid` 会话 cookie（httponly），刷新页面无需重新输入
  - 应用重启 → 内存清空 → 需重新输入
  - 已禁用 pdf2zh 的配置落盘（见 `app.py` 顶部对 `ConfigManager._save_config` 的处理），
    否则 pdf2zh 会把 `DEEPSEEK_API_KEY` 写进 `~/.config/PDFMathTranslate/config.json`
- 界面支持全部 10 种语言（与可翻译的目标语言相同），在首屏即可切换，
  选择存服务端 `data/settings.json`，首次使用默认简体中文
- 可选思考强度（`off` / `low` / `high` / `max`，对应 DeepSeek 的 `thinking` 与
  `reasoning_effort`），默认 `high` 与 API 默认行为一致
- 每个任务显示实际 token 用量与费用（人民币），按**每次调用当时的价格**累计
- 输出可选：纯译文（`-mono.pdf`）、原文/译文对照（`-dual.pdf`）或两者
- 除 API Key 外的设置都是持久的：并发参数存服务端 `data/settings.json`，
  界面上的语言/模型/输出等选择存浏览器 localStorage
- 任务记录存 SQLite、产物存固定目录，刷新页面或重启应用都不会丢；任务列表从服务端记录渲染，
  因此翻译大文件时可以随意刷新/关标签页

## 安装

项目要求 Python 3.11/3.12：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . fastapi "uvicorn[standard]" python-multipart
# 上游 tencentcloud SDK 新版本删掉了 pdf2zh 引用的 TextTranslateRequest，需要降级
uv pip install --python .venv/bin/python "tencentcloud-sdk-python-tmt==3.0.1250"
```

## 运行

```bash
./webapp/start.sh          # 默认 8077
./webapp/start.sh 8123     # 指定端口
```

端口被本应用的旧实例占用时会先杀掉旧的、沿用同一端口；被其他程序占用则自动在
8000-9000 里找一个空闲端口。也可以直接跑 `.venv/bin/uvicorn webapp.app:app --port 8077`。

### 并发

```bash
./webapp/start.sh -w 4 -t 8     # 4 个任务并行，每个任务 8 个 LLM 线程
```

打给 DeepSeek 的并发请求上限是 `workers × llm-threads`，调大前先确认账号的速率限制；
版面分析是 CPU 密集的，任务并发过高会互相抢核。等价的环境变量是 `WEBAPP_WORKERS` /
`WEBAPP_LLM_THREADS`。**设过一次就会记在 `data/settings.json` 里**，之后不带参数启动也生效。

打开 http://127.0.0.1:8077 ，填入 DeepSeek API Key（https://platform.deepseek.com/api_keys）即可。

首次启动会下载版面分析模型和目标语言字体，需要等待一会儿。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/config` | 可选模型、语言，以及当前会话是否已有 key |
| POST | `/api/session` | 表单 `api_key`，校验后写入内存并下发 cookie |
| DELETE | `/api/session` | 清除本会话的 key |
| POST | `/api/translate` | 表单 `file`/`model`/`lang_in`/`lang_out`/`pages`/`output`，返回 `job_id` |
| GET | `/api/jobs` | 全部任务记录（倒序，最多 50 条） |
| GET | `/api/jobs/{id}` | 轮询进度；完成后 `kinds` 列出可下载的类型 |
| DELETE | `/api/jobs/{id}` | 删除记录与文件（进行中的任务拒绝删除） |
| GET | `/api/jobs/{id}/download/{mono\|dual}` | 下载结果 |

## 同步上游

```bash
./webapp/sync-upstream.sh            # 把 upstream/main 合并进 main
./webapp/sync-upstream.sh --push     # 顺带推送
```

本应用直接住在 `main` 上，上游改动用 **merge** 并入——这样默认分支永远不需要
force-push。工作区不干净时脚本会直接拒绝执行；合并冲突时会停在冲突现场，交给你
`git commit` 或 `git merge --abort`。（如果你另开了功能分支，脚本会把它变基到
更新后的 `main` 上。）

变基结束后会自动跑一次冒烟测试（也可单独执行）：

```bash
.venv/bin/python -m webapp.smoke_test
```

它不翻译、不联网，只检查本应用依赖的几个 pdf2zh 内部约定是否还成立，外加界面翻译的完整性——
`translate()` 的参数、DeepSeek translator 的 envs 键名、产物命名，以及
**API Key 确实没有落盘**。上游重构完全可能变基无冲突却把这些悄悄改坏。
测试未通过时 `--push` 会拒绝推送。

## 费用计算

`pricing.json` 是价格表，单位为元 / 百万 tokens，取自[官方中文定价页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)（不是汇率换算）。
它按生效时间分档，每档可带峰谷时段：

- 2026-08-17 00:00（北京时间）前：统一定价
- 之后：高峰时段为北京时间 9:00-12:00、14:00-18:00，空闲时段价格减半

费用**不是**任务结束后用总 token 数乘以当前价格算的，而是**每次 API 调用返回时**
按那一刻生效的价格立即累加。这样跨越峰谷分界、甚至跨越调价时点的长任务也算得对，
和 DeepSeek 自己的计费方式一致。缓存命中的 token 单独计价（便宜得多），
取自响应里的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。

官方调价时，在 `regimes` 数组**末尾追加**一条即可，不要修改历史条目——
旧任务的费用是按它当时的价格算出来存下的，改动历史条目会篡改已有账目。

## 数据存放

默认在 `webapp/data/`（已 gitignore），可用环境变量 `PDF2ZH_WEBAPP_DATA` 覆盖：

```
data/
  jobs.sqlite          # 任务记录
  settings.json        # 并发、界面语言等设置（不含 API Key）
  jobs.sqlite          # 含每个任务的 token 用量与费用
  files/<job_id>/      # <名字>-mono.pdf / <名字>-dual.pdf
```

上传的原件在翻译成功后删除（可重新上传），译文一直保留，直到你在界面上点"删除"。
应用重启时，处于 queued/running 的任务会被标记为 `interrupted`——进程内的翻译线程无法跨重启恢复。

## 说明
- 默认单机自用：任务队列是进程内的 `ThreadPoolExecutor`（2 并发），未做鉴权与限流，
  不要直接暴露到公网。
