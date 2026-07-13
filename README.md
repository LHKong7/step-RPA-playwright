# pwflow

用 YAML 描述 Playwright 自动化流程，跑 CLI 或起 HTTP 服务。面向网页数据采集设计。

```yaml
name: hacker-news
vars: { pages: 2 }
browser:
  block_resources: [image, font, media]   # 不下载你根本不看的字节

steps:
  - goto: https://news.ycombinator.com

  - repeat:
      times: "{{ vars.pages }}"
      steps:
        - extract:
            name: stories
            selector: ".athing"
            list: true
            append: true                   # 跨页累加
            fields:
              rank:  { selector: ".rank", cast: int }
              title: ".titleline > a"
              url:   { selector: ".titleline > a", type: link }
        - click: "a.morelink"
          optional: true                   # 最后一页没有「更多」，不算失败

  - assert: "{{ data.stories | length > 20 }}"

output:
  path: "out/hn-{{ now() }}.json"
```

```bash
uv run pwflow run flows/hacker-news.yaml --var pages=3
uv run pwflow serve --port 8000
```

---

## 安装

```bash
uv sync
uv run playwright install chromium    # 只装一次
```

## CLI

| 命令 | 作用 |
|---|---|
| `pwflow run <flow.yaml>` | 跑一个 flow。`--var k=v` 覆盖变量，`--headed` 开窗口，`--trace` 录 trace.zip |
| `pwflow validate <flow.yaml>` | 只校验，不开浏览器 |
| `pwflow actions` | 列出所有动作及其参数 |
| `pwflow schema` | 导出 JSON Schema，喂给编辑器做 YAML 补全 |
| `pwflow serve` | 起 HTTP 服务 |

## HTTP 服务

```bash
uv run pwflow serve --flows-dir flows --port 8000 --concurrency 4
```

| 接口 | 说明 |
|---|---|
| `POST /runs` | `{flow: "hacker-news", vars: {...}, wait: true}` 或 `{yaml: "<内联 YAML>"}`。`wait: false` 立即返回 `run_id` |
| `GET /runs/{id}` | 查询某次运行（状态、数据、每步耗时） |
| `GET /runs` | 最近的运行列表 |
| `GET /flows` | `flows_dir` 下所有 flow 及其校验状态 |
| `POST /validate` | 只校验不执行 |
| `GET /actions` | 动作清单 + 每个动作的 JSON Schema |
| `GET /healthz` | 存活与在跑任务数 |

浏览器进程池随服务常驻，每次 run 只新建一个 `BrowserContext`（独立 cookie/缓存），所以并发运行之间不会串 session，也不用每次付浏览器启动的开销。

```bash
curl -X POST localhost:8000/runs -H 'content-type: application/json' \
  -d '{"flow": "hacker-news", "vars": {"pages": 1}}' | jq '.data.stories[0]'
```

---

## DSL

### 顶层结构

```yaml
name: my-flow
description: 可选
vars: { key: value }        # 默认变量，被 --var / API vars 覆盖
browser: { ... }            # 见下
output: { ... }             # 见下
steps: [ ... ]              # 主流程
on_failure: [ ... ]         # 失败时跑的诊断/清理步骤，`error` 变量可用
```

### 步骤：一个动作键 + 若干修饰键

每个 step 是一个 mapping，**有且只有一个动作键**，其余是修饰键：

```yaml
- id: next_page          # 给结果起名，后续 {{ steps.next_page }} 引用
  click: "a.morelink"    # ← 动作键
  when: "{{ page_no < 5 }}"   # 条件不成立就跳过
  timeout: 5000          # 覆盖 browser.timeout
  optional: true         # 失败只记录不中断
  retry: { times: 3, delay: 1000, backoff: 2 }   # 或简写 retry: 3
  name: "翻到下一页"      # 报告里的可读名字
```

动作参数可以写简写标量，也可以写完整 mapping：`click: ".btn"` ≡ `click: {selector: ".btn"}`。

### 模板 `{{ }}`

整串就是一个表达式时，返回**原生类型**；否则渲染成字符串：

```yaml
times: "{{ vars.pages }}"                  # -> 2 (int)
in:    "{{ data.stories }}"                # -> list
path:  "out/{{ flow.name }}-{{ now() }}.json"   # -> str
```

作用域：

| 名字 | 内容 |
|---|---|
| `vars.*` | flow 变量（被 CLI/API 覆盖后的值） |
| `env.*` | 进程环境变量 —— **密码放这里，别写进 YAML** |
| `data.*` | `extract` 目前采到的所有数据 |
| `steps.<id>` | 带 `id` 的步骤的返回值 |
| `flow.name` / `flow.run_id` / `flow.artifacts_dir` | 本次运行 |
| `page.url` | 当前页面 URL |
| `item` / `index` | 所在 `foreach` / `repeat` 的循环变量 |
| `error` | 仅在 `try.catch` 与 `on_failure` 里可用 |

采集常用过滤器：`regex('(\d+)')`、`to_int`、`to_float`、`absurl(base)`、`unique`、`strip`，外加 Jinja2 全部内置过滤器。

### 选择器

字符串直接透传给 Playwright（`".btn"`、`"text=登录"`、`"xpath=//a"`）。结构化写法用于可访问性选择器 —— 这类选择器在页面改版后活得最久：

```yaml
click:
  role: button
  name: "加入购物车"
  within: { test_id: "product-card" }   # 限定在某个父元素内查找
  nth: 0
```

引擎键（选一个）：`css` `xpath` `text` `role` `label` `placeholder` `test_id` `alt` `title`
修饰键：`name`（配合 role）、`exact`、`has_text`、`has`、`within`、`nth` / `first` / `last`

### 动作

**导航** `goto`(=`open`/`visit`) · `back` · `forward` · `reload`

**交互** `click` · `dblclick` · `fill` · `type` · `press` · `select` · `check` · `uncheck` · `hover` · `focus` · `upload` · `scroll`

```yaml
- fill:   { selector: "#user", value: "{{ env.USER }}" }
- select: { selector: "#sort", label: "最新" }
- scroll: bottom            # 触发懒加载
- press:  Enter
```

**等待** `wait_for`（元素状态）· `wait_for_url` · `wait_for_load`（`networkidle`）· `wait_for_function` · `sleep`

Playwright 每个动作前都会自动等待，所以只有在等一个「你不打算点它」的东西时才需要这些：spinner 消失、网络安静、跳转落地。

**抽取** `extract` —— 见下节

**断言** `assert`（=`expect`）

```yaml
- assert: "{{ data.stories | length > 20 }}"        # 表达式
- assert: { selector: ".login-error", state: hidden }
- assert: { url: "**/dashboard" }                  # glob，或 "re:^https://..."
- assert: { selector: ".row", min_count: 10, message: "行数不足，页面结构可能变了" }
```

断言失败**一定致命** —— 不重试，`optional: true` 也压不住。采集的结构保证一旦破了，你要的是停下来告诉你，而不是安静地写出一个只有三行的文件。

**控制流** `if` · `foreach` · `repeat` · `while` · `try` · `block` · `break` · `continue` · `stop`

```yaml
- while:
    cond: "{{ data.has_next }}"
    max: 50                        # 硬上限：跑不完的爬虫是 bug，不是特性
    steps:
      - extract: { name: rows, selector: "tr", list: true, append: true, fields: {...} }
      - extract: { name: has_next, selector: "a.next", type: exists }
      - click: "a.next"
        when: "{{ data.has_next }}"

- foreach:
    in: "{{ data.stories }}"
    as: story
    steps:
      - goto: "{{ story.url }}"
      - extract: { name: bodies, selector: ".content", append: true }

- try:
    steps:
      - click: "#accept-cookies"
    catch:
      - log: "没有 cookie 弹窗，继续：{{ error }}"
```

**其他** `set`（写变量）· `log` · `evaluate`(=`js`) · `screenshot` · `save`

```yaml
- set: { page_no: "{{ page_no + 1 }}" }
- js: "document.querySelectorAll('.row').length"
- screenshot: debug.png
```

### extract

采集场景九成的需求是「一个列表的记录」：

```yaml
- extract:
    name: stories          # 落到 data.stories
    selector: ".athing"    # 一个匹配 = 一条记录
    list: true
    append: true           # 跨翻页累加，而不是覆盖
    limit: 100
    fields:
      title: ".titleline > a"                          # 字符串简写 = 取该元素的文本
      url:   { selector: ".titleline > a", type: link }  # href，自动转绝对 URL
      rank:  { selector: ".rank", cast: int }            # "12." -> 12
      score: { selector: ".score", regex: "(\\d+)", cast: int, default: 0 }
      tags:  { selector: ".tag", many: true }            # 全部匹配收成列表
```

字段选择器**限定在该条记录内部**，所以 `.titleline > a` 是「这一行里的」而不是「整页任意的」。

| 字段键 | 说明 |
|---|---|
| `type` | `text`(默认) `html` `inner_html` `attr` `link` `value` `count` `exists` |
| `attr` | 配合 `type: attr` |
| `many` | 收集全部匹配而非第一个 |
| `regex` | 保留第 1 个捕获组 |
| `cast` | `int` `float` `str` `bool`，容错解析（`"1,234 分"` → `1234`） |
| `trim` | 折叠空白，默认 true |
| `default` | 取不到时的回退值 |

不写 `fields` 就是取单值：`extract: { name: title, selector: "h1" }`。
`type: count` / `type: exists` 问的是匹配集本身 —— 分页判断就靠它。

**性能**：`extract` 每个字段都要过一次 driver，30 行 × 3 字段 ≈ 90 次往返。行数上千时，改用一次 `evaluate` 在页面里把数据取完再返回，会快一个数量级。

### browser

```yaml
browser:
  engine: chromium              # chromium | firefox | webkit
  headless: true
  viewport: { width: 1280, height: 800 }
  user_agent: "..."
  locale: zh-CN
  timeout: 30000                # 每个动作的默认超时
  navigation_timeout: 30000
  block_resources: [image, font, stylesheet, media]   # 采集提速的最大杠杆
  storage_state: auth.json      # 复用登录态
  save_storage_state: auth.json # 跑完把 cookie 存回去
  proxy: { server: "http://...", username: ..., password: ... }
  trace: false                  # 录 trace.zip，用 playwright show-trace 看
  record_video: false
```

登录一次、之后复用：先跑一个只做登录的 flow，配 `save_storage_state: auth.json`；采集 flow 配 `storage_state: auth.json`。

### output

```yaml
output:
  path: "out/{{ flow.name }}-{{ now() }}.jsonl"
  format: json | jsonl | csv
  key: stories             # 只导出 data.stories；不写就导整个 data
  artifacts_dir: artifacts # 截图/trace/视频的落点
```

---

## 几个会绊到你的地方

**变量未定义会直接报错，不会静默变成空串。** 这是故意的：`data.has_nxt` 拼错了如果被当成 false，循环一次都不跑，你会拿到一个安静的空文件。需要「可能不存在」的语义时显式写出来：

```yaml
when: "{{ data.has_next | default(false) }}"
```

所以 `while` 的条件变量要在进循环前先 `extract` 一次，或者给 `default`。

**`--var` 的值按 JSON 解析，解析失败才当字符串。** `--var pages=3` 是 int，`--var debug=true` 是 bool，`--var name=hn` 是 str。

**控制动作的数值字段可以写模板，叶子动作的参数则是渲染后才校验的。** `while: {max: "{{ vars.n }}"}` 合法。这是「控制动作拿未渲染载荷」的直接后果。

**`extract` 的 `type: link` 会自动转绝对 URL**，`type: attr, attr: href` 不会 —— 后者给你页面里原样的 `/item?id=1`。

**CSV 里的列表字段会序列化成 JSON**（`["a","b"]`），不是 Python repr。

## 扩展一个自己的动作

动作 = 一个 pydantic 参数模型 + 一个 async 函数。注册后 CLI、HTTP、JSON Schema、校验全部自动认识它：

```python
from pwflow.registry import action
from pwflow.actions._common import RunContext, Step, Strict

class SolveCaptcha(Strict):
    sitekey: str
    provider: str = "2captcha"

@action("solve_captcha", SolveCaptcha, shorthand="sitekey")
async def solve_captcha(ctx: RunContext, p: SolveCaptcha, step: Step) -> str:
    token = await my_solver(p.sitekey, p.provider)
    await ctx.page.evaluate(f"document.querySelector('#g-recaptcha-response').value = {token!r}")
    return token
```

```yaml
- solve_captcha: "6Le-wvk..."
```

## 设计要点

- **加载期就报错**。`load_flow` 之后，每个 step（包括嵌套在 `foreach` 里的）都已确认动作存在、参数形状正确。拼错 `clcik:` 或 `fill:` 少个 `value:`，在浏览器启动之前就会失败。
- **叶子动作 vs 控制动作**。叶子动作（`click`/`extract`）拿到的是**已渲染完**的参数，实现里不用关心模板。控制动作（`foreach`/`while`）拿到的是**未渲染**的载荷，自己按轮渲染条件 —— 这才让 `while` 的条件每轮重算，而不是冻结在加载时的值。
- **数据是一等公民**。`extract` 写进 `data`，`data` 对后续每一步的模板可见，`output` 负责落盘。流程本身不需要知道数据长什么样。
- **`while` 有 `max`，断言不可忽略**。两个都是防止「爬虫安静地跑错」的护栏。

## 项目结构

```
src/pwflow/
  models.py      DSL schema —— 契约在这里
  loader.py      YAML -> 校验过的 Flow
  registry.py    动作注册表（参数模型 + 实现）
  template.py    {{ }} 渲染
  selectors.py   选择器 -> Playwright Locator
  context.py     运行时状态：page / vars / data / 报告
  executor.py    步骤循环：when / retry / optional / 分派
  engine.py      浏览器池与运行入口
  cli.py         命令行
  server/app.py  HTTP 服务
  actions/       navigation interaction waits extract assertions control misc
```
