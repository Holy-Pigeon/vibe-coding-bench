# creative-gen 工程意图分析与现状体检

> 生成时间：2026-07-26 · 基于 commit `64926ab` · 所有结论均在本地运行实例上实测复现

---

## 一、这个工程想做什么

`creative-gen` 是一个 **面向内容创作者的 AI 文案生成服务**（Vispie-AI 内部系统，版本 `3.4.1`）。

核心业务命题不是"生成一段文案"，而是 **"生成一段符合这个创作者既有风格与人设的文案"**。整个代码库的设计重心都压在"风格一致性"这一件事上：

| 领域概念 | 代码位置 | 意图 |
|---|---|---|
| **Reference（参考作品）** | `app/store.py` | 创作者的历史作品，用来向模型示范"我是谁、我怎么说话" |
| **Identity Anchor（身份锚点）** | `app/prompt.py:14` | 参考列表的**第一个**元素，专门定调 voice/人设，其余仅作风格示例 |
| **Style Vector（风格向量）** | `app/util.py:11` | 从生成文本导出的 8 维风格签名，用于量化风格 |
| **Style Anchor（风格锚）** | `app/worker.py:60` | 每个 item 维护一份累积风格向量，反复重写时向它收敛，防止跑偏 |
| **Template（模板）** | `app/templates.py` | 创作者自定义 brief 的措辞方式，含 `{voice}` / `{brief}` 占位符 |
| **Fallback（降级模型）** | `app/providers.py` | 主模型限流/超时时切备用模型，保可用性 |
| **Reference-image mode** | `app/refimages.py` | **本次 relaunch 的主打功能**：用参考图的风格向量参与风格融合 |

### 业务流程

```
POST /generate     首次生成：取创作者参考作品 → 渲染模板 → 拼 prompt → 调模型
                   → 导出风格向量 →（可选）融合参考图 → 落库 + 发事件 + 入队
POST /regenerate   迭代重写：在同一 item 上反复重写，每次把新风格融进 style anchor，
                   理论上越改越贴近创作者本人的风格
POST /template     设置创作者自定义模板
GET  /items        按 performance 排序的作品分页列表
GET  /creators/{id}/items   某创作者的作品列表
GET  /healthz      健康检查
```

### 技术架构

```
浏览器 :8080  ──►  nginx（静态 SPA + /generate 等路径反代）
                        │
                        ▼
                   FastAPI :8000（app/api.py，API-Key 鉴权）
                        │
              ┌─────────┼──────────────┬───────────────┐
              ▼         ▼              ▼               ▼
        worker.py   store.py      providers.py   refimages.py
       （编排层）  （内存热数据）  （模型网关）   （参考图风格）
                        │
                        ▼
                  db.py → PostgreSQL :5433（持久化镜像，可缺省降级为 no-op）
```

设计上有意做成 **内存为主、Postgres 为辅**：`store.py` 是"热工作集"和业务逻辑所在，`db.py` 只是把写入镜像一份到 PG，`DATABASE_URL` 为空时整体降级为无库运行（离线测试用）。`repository.py` 额外模拟了"主库 + 只读副本 + 0.5s 复制延迟"，让读路径行为贴近生产。

### README 定义的任务

服务在生产环境**输出质量正在下降**，同时即将上线 reference-image mode，**不能拖慢延迟**。要求：定位问题、修复、发布 relaunch，且不能让情况变得更糟。这是一个**刻意注入缺陷的评测工程**，题目明说"不指望做完"。

---

## 二、现状体检（实测）

服务已在本地跑通，三个容器全部健康。以下每条都附实际复现结果。

### 2.1 输出质量下降 —— 主因（P0）

#### ① 参考作品选取：身份锚点是随机的，且 75% 的参考来自别人

`app/store.py:40` 用 `set` 做去重，直接摧毁了 `prompt.py:14` 明确依赖的"primary anchor 在第一位"的顺序契约；同时把全局热门作品 `_global_exemplars()` 无差别混入。

实测（`c_1` 拥有 x0/x1/x2，`c_2` 拥有 x3/x4/x5）：

```
get_references('c_1', 4) → ['x1', 'x3', 'x4', 'x5']
                            └ 锚点   └────────────┘ 全是 c_2 的作品
c_1 自己的 x0、x2 被挤掉；4 个参考里 3 个是别人的
```

且 `set` 迭代序受 `PYTHONHASHSEED` 影响，**每次进程重启锚点都不一样**：

```
进程 1: ['a3','a2','a4','a6']    进程 2: ['a4','a5','a3','a2']    进程 3: ['a6','a5','a3','a2']
```

**后果**：创作者的人设锚点每次随机漂移，且大部分风格上下文来自其他创作者 —— 这是"输出退化"最直接的解释，同时也是**跨租户内容泄漏**。

#### ② regenerate 把自己的输出当输入，造成语义坍缩

`app/worker.py:62`：`build_prompt(prev.caption, refs)` —— 传入的是**上一次的输出**而不是原始 brief。叠加 `_hook()` 的 48 字符截断，原始意图被逐次挤出窗口。

实测连续 10 次 regenerate：

```
[149] new running shoe drop
[116] [149] new running shoe drop
[852] [116] [149] new running shoe drop
[605] [852] [116] [149] running shoe drop        ← "new" 被弱模型剔除
...
[601] [563] [881] [366] [653] [368] [623] [605] [852]    ← 原始 brief 已完全消失
```

这是教科书式的自回归退化（model collapse）。前端 `runBatch()` 里那句 "notice how the output drifts / degrades" 正是在演示它。

#### ③ 降级模型静默承担了 1/3 以上的流量

`LOAD_PRESSURE=0.35` 意味着 35% 的主模型调用失败，`providers.py:86` 的 `except Exception` 无声切到 `quality=0.62` 的 `gen-small-v1`，该模型会剔除所有长度 ≤3 的词（`providers.py:53`）。

实测 200 次调用：`gen-large-v3: 127, gen-small-v1: 73`（36.5% 降级）。数据库里同样可见：`gen-large-v3: 7, gen-small-v1: 3`。

#### ④ regenerate 的置信度无上界，风格锚被冻结

`worker.py:65-67`：`conf *= 1.4` 无限增长，`w = conf/(conf+1) → 1`。第 10 次重写时 w ≈ 0.9997，新生成的风格几乎完全被忽略。**风格向量冻结的同时文本却在坍缩** —— 两者朝相反方向跑，风格向量因此完全失去了它本该起的护栏作用。

### 2.2 Reference-image mode（relaunch 主打功能）—— 功能性失效（P0）

#### ⑤ 缓存被就地改写，同一张参考图每次生效强度指数衰减

`refimages.py:86-87` 的衰减循环修改的 `sv` 是 `fetch_style_hot` **直接返回的缓存对象本身**（`refimages.py:44`），不是副本。于是每调用一次，缓存里的向量就被砍半。

实测（相同输入 `base=[1.0]*8`，相同图 `img_a`，连续三次）：

```
call 1: [1.1039, 1.1294, 1.2275, 1.3157, ...]
call 2: [1.0519, 1.0647, 1.1138, 1.1579, ...]   ← 影响力减半
call 3: [1.0260, 1.0324, 1.0569, 1.0789, ...]   ← 再减半
缓存中的向量已从 ~0.21 衰减到 0.026
```

**同样的输入产生不同的输出，且参考图的影响力呈指数级归零**。这个 relaunch 的核心功能在上线后会自己把自己关掉，而且缓存是全局共享的 —— 一个租户的调用会削弱所有租户的参考图效果。

#### ⑥ 无 single-flight 的 TTL 缓存 = 缓存击穿

`refimages.py:39-51`：TTL 仅 2 秒，过期瞬间所有并发请求同时穿透到"昂贵的 provider 调用"（`time.sleep(0.01)`），且串行 for 循环 × `REFERENCE_FANOUT=4`。这正是 README 强调的"不能拖慢延迟"的风险点。当前单机低并发下未观测到（有图 60ms / 无图 10ms），但缓存热时差异被掩盖了 —— 冷路径是 4 × 10ms 串行。

#### ⑦ SSRF 校验存在 TOCTOU

`refimages.py:64-76`：`_validate()` 解析域名 → 校验是否公网 IP → 返回后 `fetch_remote_style` **再解析一次**才使用。两次解析之间 DNS 可以改变（DNS rebinding），且校验通过的那个 IP 被丢弃了。`reference_image_ids` 直接来自请求体，攻击面对外开放。当前 fetch 是桩函数尚未真正发起网络请求，属于**潜伏漏洞** —— 真实 fetch 一落地就会变成可利用的内网探测。

#### ⑧ 缓存 key 只有 24 bit

`refimages.py:25`：`md5(...)[:6]` = 24 bit 空间，约 4000 条目即达生日碰撞概率 50%。碰撞时返回**另一张图的风格向量** —— 又一条无声的质量污染路径。

### 2.3 可观测性 —— 为什么没人发现（P0）

#### ⑨ 监控在结构上是瞎的

`providers.py:89`：降级分支里也调用 `monitoring.record(ok=True)`。实测 200 次调用（其中 73 次主模型失败）：

```
total=200  errors=0  error_rate=0.0000  sla_ok=True
```

四个九的 SLA 永远不会告警，因为**错误从来没有被计数过**。这解释了题目设定里最关键的一环：质量在降，但仪表盘一片绿。另外 `monitoring.sla_ok()` 在整个代码库中从未被调用。

#### ⑩ 启动时无条件打 ERROR 日志

`config.py:25`：OTLP collector 不可达是预期情况，却以 `log.error` 输出。每次启动都有一条 ERROR —— 告警疲劳，真错误被淹没。实测日志首行即是。

#### ⑪ 异步后处理从未执行

`worker.py:53` 用同步方式调用 `async def post_generate()`，协程从未被 await。实测日志：

```
/app/app/worker.py:53: RuntimeWarning: coroutine 'post_generate' was never awaited
```

trending 预热和通知服务**从上线起就没跑过一次**，且没有任何报错。

### 2.4 安全（P0/P1）

#### ⑫ ReDoS：单个请求可打满一核（P0，可远程触发）

`templates.py:10`：`re.compile(r"^(a+)+$")` 是经典的指数级回溯正则，且在 `render()` 中对**用户完全可控的 `creator_id`** 执行，位于 `/generate` 热路径上。

实测（本地实例，真实 HTTP 请求）：

```
creator_id = "a"×28 + "!"   →  单个请求耗时 6.65 秒
```

每多一个字符耗时翻倍：19 字符 8ms → 23 字符 131ms → 27 字符 2.1s → 29 字符 6.65s。40 字符量级即以小时计。**几个请求就能拖垮整个服务**。（讽刺的是这个校验的返回值在 `templates.py:32` 被直接丢弃，根本没起过校验作用。）

#### ⑬ 模板注入：创作者可读取服务内部对象

`templates.py:37` 把内部对象 `_CTX` 作为 `ctx` 传入用户提供的格式化字符串。实测：

```python
set_template('c_x', '{ctx.build_token} | {brief}')
render(...) → "bld_7f3a9c2e1d4b | my brief"    ← 内部构建令牌被泄漏
```

`str.format` 的属性遍历可进一步升级为 `{ctx.__class__.__init__.__globals__[...]}`，读取模块全局变量（包括 `auth._API_SECRET`）。这是已知的 Python format-string 逃逸模式。

#### ⑭ 无真实多租户隔离（IDOR）

`auth.py:8` 硬编码单一密钥 `sk_live_demo`，且**明文写在前端 JS 里**（`frontend/index.html:70`），任何打开控制台的人都能拿到。所有请求映射到同一个 `tenant_default`，而 `/items`、`/creators/{creator_id}/items` 完全不做租户过滤 —— 任意 key 持有者可读取任意创作者的作品。

#### ⑮ pickle 反序列化

`cache.py` 用 `pickle` 序列化，docstring 明写"以后要迁到 Redis"。一旦迁移，Redis 即成为 RCE 入口。

#### ⑯ 生成 prompt 以 INFO 级别打印

`worker.py:33` 记录完整 prompt（含创作者内容与人设）。当前 uvicorn 默认日志配置下 `creative_gen` logger 未挂 handler 所以未输出，但生产环境配置了 root logger 就会全量落盘。

### 2.5 正确性与资源（P1/P2）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| ⑰ | 连接每次泄漏，`finally: pass` | `providers.py:92` | 实测 200 次调用泄漏 200 个 client，无回收 |
| ⑱ | 请求上下文是进程级全局 dict 而非 `ContextVar` | `context.py:3` | 线程池并发下 actor 串号（当前无人读取，是定时炸弹） |
| ⑲ | 可变默认参数 `_seen: list = []` | `store.py:45` | 无界内存泄漏，每次 generate 追加 |
| ⑳ | `increment_usage` 读-改-写无锁 | `store.py:51-57` | 并发下丢失更新 → 计费少算 |
| ㉑ | `while True` 无退避无上限重试 | `queue.py:20` | 永久失败项会 CPU 空转死循环 |
| ㉒ | 队列只入不出、无界 | `queue.py:9` | 内存泄漏，`depth()` 单调增长 |
| ㉓ | N+1 查询 + 读副本 | `repository.py:40-47` | 每个 item 一次查询；0.5s 复制延迟内新作品在 `api.py:37` 的 `if r` 处被静默丢弃（read-after-write 不一致） |
| ㉔ | 双写无 outbox | `repository.py:50-60` | 主库已提交但事件发布失败 → 事件永久丢失 |
| ㉕ | `utcnow()` 与 `now()` 混用 | `store.py:73` vs `worker.py:50` | 朴素时间戳跨时区比较，本机偏差 8 小时；DB 取回的 aware 时间会直接抛 `TypeError` |
| ㉖ | `int(length * RATE)` 向下截断 | `billing.py:8` | RATE=0.07，任何 15 字符以下输出计费为 0；且 `worker.py:34` 的返回值被丢弃，**计费根本没有落地** |
| ㉗ | 每次请求全量排序 | `store.py:66` | O(n log n)/请求；相同 performance 无稳定 tiebreak，翻页会重复/漏项 |
| ㉘ | `created_at` 在落库之后才赋值 | `worker.py:47,50` | 写入 PG 的行 `created_at` 依赖 DB 默认值，与应用侧不一致 |
| ㉙ | DB 写入异常吞在 `log.debug` | `db.py:63` | 持久化静默失败 |
| ㉚ | 连接池 `max_size=8` vs uvicorn 线程池 40 | `db.py:29` | 高负载下池耗尽排队 |
| ㉛ | `.dockerignore` 排除了 `tests/` | `.dockerignore:7` | 镜像里跑不了测试，CI 无法在制品上验证 |
| ㉜ | `@app.on_event` 已废弃 | `api.py:45` | FastAPI ≥0.110 弃用告警，应改用 lifespan |

### 2.6 关于现有测试

`tests/test_smoke.py` 的 2 个用例**全部通过**：

```
2 passed, 5 warnings in 0.14s
```

但它自己的注释已经承认了问题："confirm generation RUNS. They say nothing about quality" / "we do not assert anything about the quality or style-consistency"。**绿色的测试是这套系统里最具误导性的信号** —— 它和第 ⑨ 条的假监控构成了同一个失效模式：所有的健康指标都只验证"有响应"，没有一个验证"响应是对的"。

---

## 三、根因归纳

三十余条问题背后其实是**三个系统性的失效模式**：

1. **失败被伪装成成功。** provider 降级记为 ok（⑨）、DB 写入失败吞进 debug（㉙）、事件发布失败只记一行（㉔）、协程从未执行却毫无声响（⑪）、异常一律 `except Exception` 兜底。系统从不承认自己出错，所以退化可以无限累积而无人知晓。

2. **共享可变状态被就地改写。** 参考图缓存被调用方改写（⑤）、模板编译缓存永不失效（⑬ 相邻问题）、请求上下文是进程全局（⑱）、可变默认参数（⑲）。内存态成了热工作集又缺乏所有权约定，导致"同样的输入产生不同的输出"。

3. **顺序契约被无序容器摧毁。** `prompt.py` 依赖"锚点在第一位"的约定，`store.py` 用 `set` 打散了它（①）。一个类型选择就废掉了整个身份锚点机制 —— 而这恰恰是这个产品最核心的卖点。

**如果只修三处**：① 参考选取（恢复顺序 + 按创作者过滤）、⑤ 参考图缓存别名改写、⑨ 监控记账。前两条直接止住质量下滑，第三条让后续所有改动变得可验证 —— 在监控修好之前，任何"修复"都无法被证明有效。

**紧随其后的安全项**：⑫ ReDoS（一个请求打满一核，可远程触发）应与上述三项同批修复。

---

## 四、如何运行

### 依赖

Docker 环境已就绪（Docker 28.4.0 via Colima，Compose 5.0.1）。注意本机 **没有 `docker compose` 子命令插件**，需使用独立二进制 `docker-compose`。

### 启动

```bash
cd /Users/holy/Documents/UGit/vibe-coding-bench
docker-compose up --build -d
```

### 访问

| 服务 | 地址 | 状态 |
|---|---|---|
| 前端控制台 | http://localhost:8080 | ✅ HTTP 200 |
| 后端 API | http://localhost:8000 | ✅ `{"ok":true,"db_items":7}` |
| PostgreSQL | localhost:5433（creative/creative/creativegen）| ✅ healthy |

前端控制台提供 Generate / Regenerate / Regenerate ×10（用于观察退化）/ List items 四个操作。

### 冒烟验证

```bash
K='x-api-key: sk_live_demo'

# 生成
curl -s -H "$K" -H 'content-type: application/json' \
  -d '{"creator_id":"c_018","brief":"new running shoe drop"}' \
  localhost:8000/generate

# 观察退化：连续重写 10 次
for i in $(seq 1 10); do
  curl -s -H "$K" -H 'content-type: application/json' \
    -d '{"creator_id":"c_018","item_id":"i_1"}' localhost:8000/regenerate; echo
done

# 参考图模式
curl -s -H "$K" -H 'content-type: application/json' \
  -d '{"creator_id":"c_018","brief":"ref mode","reference_image_ids":["img_a","img_b"]}' \
  localhost:8000/generate
```

### 运行测试

`tests/` 被 `.dockerignore` 排除在镜像外，需挂载进容器：

```bash
docker-compose run --rm --no-deps -v "$PWD/tests:/app/tests:ro" -e DATABASE_URL= \
  backend python -m pytest tests/ -q
```

### 查看数据

```bash
docker-compose exec db psql -U creative -d creativegen \
  -c "SELECT served_by, count(*) FROM items GROUP BY 1;"
```

### 可调参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LOAD_PRESSURE` | 0.35 | 主模型失败率（调 0 可隔离出降级模型的影响） |
| `GEN_SLOW_S` | 0.0 | 主模型失败前的挂起时长（调大可复现超时场景） |
| `STYLE_BLEND` | 0.6 | 重写时保留的历史风格权重（**注：定义了但 `worker.py` 从未使用**） |
| `REFERENCE_FANOUT` | 4 | 参考作品数量 |
| `GEN_TIMEOUT_S` | 8.0 | 生成超时（**注：定义了但从未使用**） |
| `DATABASE_URL` | — | 置空则无库运行 |

### 停止

```bash
docker-compose down          # 保留数据卷
docker-compose down -v       # 连同 pgdata 一起清除
```

---

## 五、复现命令附录

上文所有实测结论的复现方式：

```bash
# ① 参考选取错乱
docker-compose exec -T backend python -c "
from app.models import Creative
from app import store
for i in range(6):
    store.save_item(Creative(item_id=f'x{i}', creator_id='c_1' if i<3 else 'c_2',
                             caption=f'cap{i}', hook=f'hook{i}', style_vector=[0.0]*8))
refs = store.get_references('c_1', 4)
print('order=', [r.item_id for r in refs])
print('foreign=', [r.item_id for r in refs if r.creator_id!='c_1'])"

# ⑤ 参考图缓存别名改写
docker-compose exec -T backend python -c "
from app import refimages
base=[1.0]*8
for i in range(3): print(f'call {i+1}:', refimages.apply_reference_images(base, ['img_a']))
print('cached:', refimages._HOT_CACHE[refimages._cache_key('img_a')][0])"

# ⑨ 监控失明 + ⑰ 连接泄漏 + ③ 降级比例
docker-compose exec -T backend python -c "
import collections
from app import monitoring, providers
for i in range(200): providers.generate('p')
print('total=%s errors=%s rate=%.4f sla_ok=%s' % (monitoring._calls['total'],
      monitoring._calls['errors'], monitoring.error_rate(), monitoring.sla_ok()))
print('leaked clients:', providers._open_clients)
print(collections.Counter(providers.generate('p')['served_by'] for _ in range(200)))"

# ⑫ ReDoS（会占满一核约 7 秒）
time curl -s -o /dev/null -m 60 -H 'x-api-key: sk_live_demo' \
  -H 'content-type: application/json' \
  -d '{"creator_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaa!","brief":"x"}' localhost:8000/generate

# ⑬ 模板注入 + 模板缓存不失效
docker-compose exec -T backend python -c "
from app import templates
templates.set_template('c_x', '{ctx.build_token} | {brief}')
print('injection ->', templates.render('c_x','my brief','voice'))
templates.set_template('c_x', 'CHANGED {brief}')
print('after update ->', templates.render('c_x','my brief','voice'))"

# ⑪ 协程未 await
docker-compose logs backend 2>&1 | grep "never awaited"
```
