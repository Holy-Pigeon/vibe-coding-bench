# Code Review — fix 分支最近三次提交

- **Review 日期**: 2026-07-26
- **Review 范围**: `fix/quality-degradation-and-relaunch` 分支最近三次提交
  - `e6ed98f` fix: P0 production-incident bugs before relaunch
  - `2754d43` docs: architecture analysis + root-cause diagnosis writeup(纯文档,无代码)
  - `80b683a` fix: stop silent quality degradation; make relaunch reference-image mode correct
- **Review 关注点**: 仅代码变更是否影响功能正确性
- **验证**: 全部 10 个测试通过(`tests/test_smoke.py` + `tests/test_quality.py`,offline 模式)

## 总体结论

**两个代码提交(80b683a、e6ed98f)未发现功能正确性回归**,各项修复逻辑与提交说明一致。存在少量非阻塞的小问题(死代码、边界情况),见文末「值得留意的小问题」。

---

## 80b683a — 质量退化修复

### `store.get_references`(app/store.py)

- 原实现 `list(set(ids) | exemplars)` 破坏顺序契约(`prompt.build_prompt` 依赖首个引用作为身份 anchor),且无条件混入其他 creator 的作品。
- 新实现保序去重、只取本 creator 的引用,全局 exemplars 仅在冷启动(creator 无任何历史)时生效。
- ✅ 逻辑正确;`fanout` 截断、`_ITEMS` 存在性检查均无误。

### `providers.generate`(app/providers.py)

- fallback 由 `record(ok=True)` 改为 `record(ok=True, degraded=True)`:可用性 SLA 不受影响(仍是 200),质量退化通过 `degraded_rate()` / `quality_ok()` 可观测。
- `finally: client.close()` 修复连接泄漏。`client` 在 `try` 之前创建,不存在 NameError 风险。
- ✅ 正确。注:若 fallback 自身抛异常,该请求不会被 `monitoring.record` 计数——属既有行为,非本次引入。

### `refimages`(app/refimages.py)

- `apply_reference_images` 改为在拷贝上操作,不再原地篡改共享缓存条目 → 同输入同输出,消除跨租户缓存污染。
- 缓存 key 从 `md5[:6]`(24 bit,约 4k 条即碰撞)扩为全宽 digest。
- ✅ 确定性修复正确。⚠️ 但见「小问题 1」:衰减循环变为死代码。

### `worker.regenerate`(app/worker.py)

- 自坍缩修复:`base_brief = prev.brief or prev.caption`,重新使用原始 brief 而非上一次输出;对无 `brief` 字段的旧数据有兜底。
- 置信度封顶:`_MAX_CONF = MAX_STYLE_WEIGHT / (1 - MAX_STYLE_WEIGHT) = 0.85/0.15 ≈ 5.667`,则 `w = conf/(conf+1)` 最大值恰为 0.85。数学正确。
- `Creative` 新增 `brief` 字段(Optional,默认 None),向后兼容。
- ✅ 正确。

---

## e6ed98f — P0 安全 / 事故修复

### ReDoS(app/models.py, app/templates.py)

- `creator_id` 在 schema 边界用 `StringConstraints(pattern=r"^[A-Za-z0-9_]+$", max_length=64)` 约束,恶意输入在进入任何正则前被 422 拒绝。
- `templates._NAME_RE` 的灾难性回溯模式 `^(a+)+$` 替换为线性 `^[A-Za-z0-9_]+$`。
- ✅ 正确。`_valid_name` 现已无调用方(死代码,无害)。

### 模板注入(app/templates.py)

- `render()` 不再使用 `str.format(ctx=_CTX)`,改为对 `{voice}` / `{brief}` 的字面 `replace` —— 无格式引擎、无对象图暴露,`{ctx.__class__...}` 类逃逸(原可泄露 `auth._API_SECRET`)被堵死。
- 行为变化:模板中的未知占位符原先会 KeyError(500),现在原样保留——是改善而非退化。
- 极小边界:替换顺序为先 voice 后 brief,若 voice 文本(来自生成的 hook)恰含字面 `{brief}` 会被二次展开,仅造成文本重复,无安全影响。
- ✅ 正确。

### 租户隔离 / IDOR(app/auth.py, app/api.py, app/worker.py, app/store.py, app/repository.py)

- API key 从环境变量加载(`API_KEYS="key:tenant,..."`),常量时间比较;key → tenant 贯穿全部读写路径。
- `Creative` 新增 `tenant_id`;`/items`、`/creators/{id}/items` 按 tenant 过滤;regenerate 做属主校验,跨租户以 `PermissionError → 404` 返回,不泄露资源存在性。
- 调用方一致性已逐一核对:`list_recent(offset, limit, tenant)`、`list_by_creator(creator_id, tenant)` 传参位置正确;`worker.generate/regenerate` 新增 tenant 参数带默认值,测试中的旧式调用不受影响。
- `tenant_id=None` 的遗留 item 在属主校验中放行、regenerate 后归属调用方租户——内存存储重启后不存在遗留数据,实际无影响。
- ✅ 正确。

### 其余修复

| 修复 | 位置 | 结论 |
|---|---|---|
| 可变默认参数泄漏 → 有界 deque + 计数器 | `store.remember_brief` | ✅ 唯一调用方(worker.py:38)已同步更新 |
| 无界队列 → `deque(maxlen=10000)` | `app/queue.py` | ✅ 满时丢最旧并告警;见「小问题 5」 |
| 协程未 await → 线程池执行 | `app/async_pipeline.py` | ✅ `asyncio.run` 于工作线程中执行,`post_generate` 真正运行;异常被捕获记日志 |

---

## 值得留意的小问题(非阻塞,不构成正确性回归)

1. **`refimages` 衰减逻辑变成死代码**(`app/refimages.py:89-91`):decay 循环现在只作用于随即丢弃的本地拷贝。「同一参考图重复使用贡献递减」这一文档声明的特性被静默移除(同一请求内重复同一 image_id 也不再衰减)。若为确定性刻意为之,建议直接删除该循环,避免误导。
2. **`secrets.compare_digest` 对非 ASCII 字符串抛 TypeError**(`app/auth.py:42`):FastAPI header 按 latin-1 解码,乱码 `X-API-Key` 会得到 500 而非 401。建议比较前 `.encode()`。
3. **冷启动 `_global_exemplars` 不过滤租户**(`app/store.py:38-41`):新 creator 首次生成时,prompt 引用中可能混入其他租户的 caption/hook。仅内容进入 prompt,不经 API 直接暴露,但与 e6ed98f 的租户隔离目标有轻微出入。
4. **死代码**:`queue.dequeue()` 无调用方(队列仍只进不出,只是不再无界);`templates._valid_name` 不再被调用。
5. **队列满丢弃最旧条目**(`app/queue.py`):有日志,属有意的策略选择,但积压时旧任务会静默丢失,后续接入真实消费者时需重新评估丢弃策略。

## 验证记录

```
DATABASE_URL= python -m pytest tests/ -q
10 passed
```
