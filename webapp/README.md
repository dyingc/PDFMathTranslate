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
- 中断或失败的任务可以一键「继续」（复用 pdf2zh 的段落缓存，已翻部分不再花钱）
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

两个并发参数在**页面顶部直接可调**（与界面语言并列），改完立即生效并写入
`data/settings.json`：

- **同时翻译的文件数**（1-16）：调大立刻放行正在排队的任务；调小不会打断
  正在跑的，只是不再启动新的
- **每个文件的 LLM 线程数**（1-32）：对新开始的任务生效

也可以在启动时指定，作为首次运行的初始值：

```bash
./webapp/start.sh -w 4 -t 8
```

等价环境变量 `WEBAPP_WORKERS` / `WEBAPP_LLM_THREADS`。优先级是
环境变量 > 上次保存的值 > 默认值（2 / 4）。

打给 DeepSeek 的并发请求上限是两者相乘，调大前先确认账号的速率限制；
版面分析是 CPU 密集的，任务并发过高会互相抢核。

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
| POST | `/api/jobs/{id}/resume` | 重跑中断/失败的任务 |
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

## 扫描件

上传时会检测扫描件（页面几乎全是图片、但能提取出文字 = 有 OCR 层）。命中时**先弹窗询问**，
不会先花钱翻完再让你发现结果没法看。

原因：扫描件里你看到的原文是**图片里的像素**，pdf2zh 只能删除文字绘制指令，删不掉像素，
所以译文会直接叠在原文的照片上。

确认继续后，翻译完成会自动做一次清理：用源文件自身的**段落边界框**定位原文位置，
擦除该区域扫描图上的像素（`apply_redactions(images=PIXELS, text=NONE)`），译文、图表、
线条都保留。代价是这些区域里的底纹或背景也会被一并擦掉。

用段落框而不是逐个文字 span，是因为 OCR 层的 span 并不铺满段落，逐 span 擦会在行间
留下一条条没擦干净的原文碎片——这一点在 weiser 那份 OCR 质量较差的扫描件上很明显。

## 代码块

pdf2zh 会把代码块当成普通段落：合并成一段、送去翻译、再按行宽重排，换行、缩进和
等宽字体全部丢失。用回显翻译器（不调用 API）也能复现，可见破坏来自版面重排本身，
与模型无关。

修法是给 `translate()` 传 `vfont`——匹配到的字体会被原样保留、不参与翻译与重排。
`webapp/app.py` 里的 `VFONT` 在 pdf2zh 内置的公式字体规则之外，补上了等宽字体族
（`txtt`、`cmtt`、Courier、Consolas、Menlo、Inconsolata 等）。

**注意 `vfont` 一旦传入就会覆盖内置规则**，所以 `VFONT` 必须是内置规则的超集，
否则公式会被当成正文翻译掉。冒烟测试会从 pdf2zh 源码里抽出内置规则逐条比对。

代价：代码里的注释不再被翻译。用排版正确、注释是英文的代码，换排版稀烂、注释是
中文的代码，这个交换是划算的。

## 超链接

pdf2zh 的 mono 输出保留了全部链接，但 dual 的组装走 PyMuPDF `insert_file`，
它搬不动**命名目标**（named destination）——LaTeX 的交叉引用和目录项全是这种。
这本 624 条链接里有 618 条就此消失。

翻译完成后会重建：命名目标解析成显式的页面目标（不再依赖名称表），
并把页号映射到译文页，点进去仍然是中文。

**不能沿用原坐标**——译文重排后 "Chapter 12"、"[LSS+15]" 早已不在原处。
每条链接按**锚文本**重新定位：先整段搜索，失败则退回锚文本里的记号
（章节号、引用键、数字）——恰好这些都是翻译不会改动的部分。
找不到就丢弃：**放错位置的链接比没有链接更糟**。

同时从原注释复制 `/Border` 与 `/C`——hyperref 画的彩色方框来自这两个键，
新建的链接默认带 `/BS <</W 0>>`（零宽边框），能点但完全看不见。
注意 `page.get_links()` 在页面重载前看不到刚插入的注释，取 xref 要用 `annot_xrefs()`。

mono 页面上原有的链接会先删除再重建，因为它们停留在原坐标、早已对不上译文。
dual 的英文页不属于处理范围，链接原样保留。

## 图内标签重影

有些插图是「画两遍、后一遍盖前一遍」导出的：第一遍的标签在原文里被后画的不透明色块
完全盖住，读者只看到第二遍。pdf2zh 重建页面时把**所有文字排在所有图形之后**
（`pdfinterp.py`：`q {ops_base}Q ... {ops_new}`，为的是不让译文被图片埋掉），
于是那层本该看不见的标签浮到了最上面，和可见标签错位重叠。

翻译完成后会自动做一次去重，只删除**能被证明在原文里不可见**的文字：
被后画的不透明填充框完整覆盖的 span。两条保险：

- 只有当该位置**还有别的字符存活**时才删——两遍往往是插图的不同修订版
  （本例里是 COND / PRED），存活者不必与它相同；若删完会留下空洞，宁可保留重影
- 删除按区域生效，会连同存活的那份一起抹掉，因此删除前先记录、删除后原样重画

对没有隐藏层的文档，这一步是空操作。

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
这类任务（以及失败的任务）保留着上传的原件，可以在界面上点「继续」重跑。

「继续」不是字节级续传（pdf2zh 没有这个概念），而是用相同参数重跑一遍。
pdf2zh 按段落文本哈希缓存译文，已经翻好的段落直接命中缓存，不会重复调用 API，
所以实际只为未完成的部分付费。费用是**累加**到原有记录上的，不会覆盖。

## 说明
- 默认单机自用：任务队列是进程内的 `ThreadPoolExecutor`（2 并发），未做鉴权与限流，
  不要直接暴露到公网。
