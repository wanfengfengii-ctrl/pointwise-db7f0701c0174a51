# 研究数据湖参与者撤回：受控读取设计（DESIGN）

> 交付边界：本文只给出设计与可重跑证据，**不实现撤回功能**。业务源文件
> `lake.py`、`query.py`、`samples.py`、`probe.py` 与原断言 `checks.py` 保持
> 初始提交字节不变（由 `evidence/verify_reports.py` 用 `git diff` 实际核对）。
> 证据中的过滤+全量 `evaluate` 只是讨论用 ORACLE，不是在线维护算法。

## 1. 判定口径与等价性定义

### 1.1 必须实现的可观察语义

设源版本 `g` 的三张源表为 `raw(g)`（现入口 `Lake.raw`，`lake.py:111-112`），
累计已确认撤回者集合为 `W`。定义整行删除算子与受控结果：

```
F_W(tables) = evaluate( { kind: [ r for r in tables[kind]
                                  if not (set(r["owners"]) ∩ set(W)) ]
                          for kind in KINDS } )            # KINDS 见 query.py:2
```

- 删除单位是**整行**：`owners` 与 `W` 相交即删，绝不编辑 `owners`
  （样本共有行 e5 见 `samples.py:10`，ORACLE 实现见 `probe.py:8-11`）。
- 撤回确认后，新发起的 `raw`、`pin/export`、镜像读取，以及确认前固定的
  旧句柄在确认后的 `export`，都必须恰好等于 `F_W(raw(g))`。
- 已交付给外部的结果不追回；约束只作用于受控接口之后的读取。
- 不要求物理销毁旧字节，不要求账户/角色/密码系统。

### 1.2 观察事实与提议行为的分界

| 事实编号 | 观察（OBSERVED，现有源码实际行为） | 提议（PROPOSED，本设计） |
|---|---|---|
| O1 | 句柄内嵌结果字节，`export` 只解析内嵌 JSON（`lake.py:139-143`），今天确认撤回后旧句柄仍返回旧值 | 句柄改为携带 `(generation, 预订令牌)`，`export` 经受控网关按当前水位重解析 |
| O2 | 对象按规范 JSON 内容去重，块可跨版本共享（`lake.py:36-41`，样本两版共享事件块，见 `evidence/out/e1_baseline.json`） | 索引以**行内容哈希**为主键，共享块只解析一次，版本成员关系另行记录 |
| O3 | 恢复会回退源 head，代次数字可被复用（`lake.py:119-120`，实测恢复后再次发布得到 3） | 撤回账本独立于湖库，恢复不能回退；成员关系按 `(代次,表)` 记录，代次复用不串义 |
| O4 | `prepare` 的对象写入是独立 autocommit，先于发布事务（`lake.py:67`、`lake.py:39`）；发布事务在 `lake.py:86-105` | 建索引/索引增量可安全引用任何已发布对象，无需停写 |
| O5 | 当前代码对撤回者后续补入的行照常存储、照常服务（实测见 e4 时序 C/R1） | 网关在读取路径按内容索引过滤，新补行永不重新可见 |

## 2. 现状调用链与事务边界（源码行号追踪）

### 2.1 写入链 prepare → materialize → evaluate → put → publish

1. `Lake.__init__` 建四张表 `objects/snapshots/head/receipts` 并插入空的
   0 代快照（`lake.py:14-34`）。连接为 `isolation_level=None`（`lake.py:15`），
   即默认 autocommit。
2. `prepare(expected, replacements)`（`lake.py:66-80`）：
   - 取基准快照（`lake.py:68` → `snapshot`，`lake.py:53-60`）；
   - 对替换表逐块 `put`（`lake.py:76`）。`put` 做规范 JSON + sha256 并
     `INSERT OR IGNORE`（`lake.py:36-41`），**对象写入在发布事务之外**，
     注释明确说明于 `lake.py:67`；
   - `materialize` 按 roots 展开全部行（`lake.py:62-64`，内部经 `get`
     `lake.py:43-48`）；
   - `validate` 冻结校验（`lake.py:78` → `query.py:30-62`）；
   - 立即对整表跑冻结查询 `evaluate` 并 `put` 结果对象（`lake.py:79-80`）。
3. `publish(request_id, plan)`（`lake.py:82-109`）在**一个** SQLite 事务内
   `BEGIN IMMEDIATE`（`lake.py:86`）完成：回执查重/冲突（`lake.py:88-94`）、
   STALE_HEAD 检查（`lake.py:96-97`）、插入快照（`lake.py:99-101`）、
   推进 head（`lake.py:102`）、写回执（`lake.py:103-104`）、`COMMIT`
   （`lake.py:105`），异常 `ROLLBACK`（`lake.py:107-109`）。
   快照、head、回执同事务，因此对外要么整代可见、要么不可见。

### 2.2 读取链 pin → export

- `Reader.__init__` 持有实例级 `cache`（`lake.py:127-129`），无任何权限语义。
- `pin(generation=None)`（`lake.py:131-140`）：缺省读 head（`lake.py:132-133`），
  取快照的 `result_oid`（`lake.py:134-135`），缓存未命中则 `get` 对象
  （`lake.py:136-137`），返回的不透明句柄**直接内嵌规范结果字节**
  （`lake.py:138-140`）。
- `export(handle)` 仅 `json.loads(handle["rows"])`（`lake.py:142-143`），
  不再访问湖库。实测篡改句柄 `rows` 即改变导出结果——Python 对象生命周期
  不是安全边界，这正是时序 B 必须解决的缺口。

### 2.3 镜像链 checkpoint → restore

- `checkpoint`（`lake.py:114-117`）用 SQLite online backup 复制整个库到新
  `Lake`；镜像含对象、快照、回执，不含任何 Reader 内存。
- `restore`（`lake.py:119-120`）反向 backup 覆盖当前库。镜像可被 `Reader`
  读取，也可成为继续写入的活动库；恢复后源 head 回退，代次数字可复用
  （实测：恢复到 head=2 后再发布得到 generation 3，见
  `evidence/out/e3_closure.json` 的 `restore_and_reuse`）。

### 2.4 内部适配器的访问边界

`put/get/snapshot/materialize/raw` 今天没有权限边界（CONTRACT.md 第 13 行）。
设计把它们明确划为**内部计算面**：撤回索引构建、闭包时间线读取、网关装配
过滤表时继续使用；消费侧只暴露受控网关对象，文档与导入边界上不再向消费者
转交裸 `Lake`/`Reader`。不新增账户体系，边界靠单一受控入口与代码评审维持；
不防御有人直接复制原始数据库用独立程序读取（CONTRACT.md 第 33 行）。

## 3. 精确影响闭包：适用于任意合法数据的判据

冻结查询的结构（`query.py:5-27`）决定了删除若干源行后，只有三类局部状态
可能变化。设删除行集合为 `D`（按表），逐层定义闭包：

- **C1 候选 device/day（应检查的超集）**
  - 对每个被删事件行，候选 `(device, at//86400)`（日界规则 `query.py:8`，
    样本常量 `day=at//86400`）；
  - 对每个被删 assignment 行，候选 `(device, at//86400)`——注意 assignment
    本身无 day，按其 `at` 归日，且它可能影响该设备同日所有赢家事件的归属；
    严谨的工程实现不取这个近似，而是经 `device_timeline` 取**该设备出现过
    赢家事件的全部 day** 作为候选，因为被删 assignment 的 `at` 可能早于
    另一日的赢家事件、仍满足 `a.at <= event.at`（`query.py:17-18`）。两种
    取法的工作量都只按设备的时间线索引界定，不扫描全库；
  - 对每个被删 hold 行，候选 `(device, day)`（hold 的 day 是独立整数）。
- **C2 实际贡献变化的 device/day（C1 的子集）**：在 C1 内重读该设备的事件
  时间线、hold 集合与 assignment 时间线（索引有界读取），重放 `query.py:7-21`
  的赢家选择、阻塞反连接（`query.py:12`、`query.py:15-16`）与
  `max(at,id)` 归属（`query.py:17-21`），比较前后贡献 `(event, region)`。
- **C3 需要更新的聚合组（day, region）**：仅收集 C2 中前后贡献的 region
  （含 `__UNASSIGNED__`，`query.py:21`），组的 devices/total/maximum 按
  `query.py:22-27` 重算。
- **C4 仅共享存储、不受影响的版本**：经 owner 索引求“引用了任一撤回者行的
  代次集合”，其补集内的版本一行都不改（实测干净代次 3 不进入 alice 命中集，
  见 `evidence/out/e3_closure.json` 的 `unaffected_version`）。

### 3.1 完备性与最小性论证（对任意合法输入成立）

考虑任意不在 C1 中的键 `k=(device,day)`：

1. 该设备该日的赢家候选事件没有被删（否则 k∈C1），故事件集合与
   `(at,id)` 最大值不变（`query.py:7-11`）；
2. 该键的 hold 没有被删，阻塞判定不变（`query.py:12`、`query.py:15-16`）；
3. 该设备的 assignment 一行未删，合格集 `a.at <= event.at` 与
   `max(at,id)` 选择不变（`query.py:17-20`）。

故 k 的贡献逐字节不变，**C1 外不存在任何变化**（完备性）；C2/C3 由定义只
包含实际差异（最小性）。证据中 `keys_changed_outside_candidates` 恒为空、
`bounded_equals_oracle` 恒为真，即对样本全部版本与双撤回组合机器校验了该
判据（`evidence/out/e3_closure.json`）。

### 3.2 为什么只跟踪当前输出的正向血缘不够

- **被遮蔽事件**：非赢家事件不进当前输出，但替换块删除赢家后它会成为新
  赢家。反例 L1/F3：alice 的 e2 在 gen1 被 d1 更晚事件遮蔽，输出里没有它；
  下一块替换后它被扶正，撤回时必须被过滤，结果变为空组
  （`evidence/out/e2_counterfactuals.json` 的
  `L1_masked_event_promoted_after_replacement`）。
- **历史 assignment**：当前赢家只挂一个 region，但删除最新 assignment 后
  更早的 assignment 成为决定项（反例 A3/F2b，west 回退到 east）；若无更早
  项则落入 `__UNASSIGNED__`（反例 A2，`query.py:21`）。
- **排除关系**：hold 从不产生输出，但删除 hold 会经反连接**新增**输出组
  （反例 H1/F4：d3 被释放，新增 `(0, __UNASSIGNED__)` 组）。样本中 h1
  （`samples.py:19-21`）撤回后释放 d3，正是时序 D 强调的“不能只扣可见金额”。

因此索引必须覆盖**全部行**（含遮蔽行、未选中 assignment、hold），而不是
只覆盖当前聚合的正向来源。

## 4. 可行策略比较

### 策略 S1：独立撤回账本 + 内容寻址索引 + 受控网关闭包维护（选定）

撤回状态写在**独立 SQLite 库**；湖库对象与快照保持不可变。读取经网关按
水位过滤，确认时只对 C1–C3 做有界维护，缓存按版本化键失效。

- 优点：确认/读取工作量只与候选设备数和受影响组相关，不随历史版本数增长；
  持久状态最小；镜像/恢复天然不能回退撤回；与现有 `publish` 事务无跨库
  事务耦合。
- 代价：每次受控读取要做一次索引化过滤装配（候选有界，非整库扫描）；
  网关成为必须维护的新入口。

### 策略 S2：每个确认点为受影响版本物化受控结果对象

确认时仍先用同一闭包找出受影响 `(版本)` 集合，然后为每个受影响版本写入
新的受控结果对象（新 oid，不动旧对象），句柄直接指向“该水位的结果 oid”。

- 优点：读取路径最简单，`export` 近似回到一次对象 `get`；旧句柄可在确认
  事务里把预订令牌映射到新 oid，缓存语义直观。
- 代价：每次确认的写放大与受影响版本数成正比；长期运行下
  `(epoch × 版本)` 的对象与映射增长，且每次确认都要重写历史版本的派生物。
  首次启用时的一次性全量建索引本就允许（CONTRACT.md 第 35 行），但 S2 把
  全量成本摊到了**每一次**撤回。
- 结论：读极多、撤回极少的部署可选 S2；本工程样本小、要求“最少持久状态”
  与有界确认，选 S1。S2 与 S1 的索引、账本、闭包组件完全相同，可作为后续
  开关切换，不是互斥架构。

### 被判定为不可行的做法（不计入候选）

- **直接清 Reader 缓存**：句柄内嵌字节（`lake.py:139-143`）、多 Reader 多
  连接、镜像持独立内存，清实例既覆盖不到旧句柄也覆盖不到其他 Reader
  （时序 B 的实测缺口）。
- **确认时整库扫描重算 / 每次读取全量 evaluate**：直接违反 CONTRACT.md
  第 35 行的禁止性约束。
- **永久拒读历史快照或暂停无关读写、等读者退出**：违反第 33、35 行；
  本设计只有短元数据事务，不持长锁。
- **把撤回写进湖库快照/回执表**：`restore` 的 backup 覆盖（`lake.py:120`）
  会令旧镜像回退撤回，违反“镜像不能回退已确认撤回”。

## 5. 选定设计（S1）细节

### 5.1 最少持久状态及其不可省略性

撤回账本库 `withdrawals.db` 是**独立 SQLite 文件/连接**，绝不放进湖库——
否则 `restore` 的 backup 覆盖（`lake.py:120`）会把撤回一并回退（见 §4 不可行
项与 §5.6）。崩溃语义因此与湖库相互独立：

```sql
CREATE TABLE withdrawals(
  request_id TEXT PRIMARY KEY,      -- 请求绑定与幂等，缺它无法处理响应丢失
  subject    TEXT NOT NULL,         -- 撤回参与者标识
  binding    TEXT NOT NULL,         -- canonical(请求参数)，冲突判定
  status     TEXT NOT NULL,         -- PREPARED | CONFIRMED
  epoch      INTEGER NOT NULL);     -- 确认水位；PREPARED 行为 -1
CREATE TABLE watermark(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  epoch INTEGER NOT NULL);          -- 单一线性化点，缺它无法定义累计语义
```

派生索引建在**第三个独立 SQLite 库** `index.db`（与账本分离：账本是权威
撤回状态，索引是可整体重建的派生数据；二者都不在 backup 覆盖范围内）。
若部署选择把索引表放进湖库，则必须接受“恢复后按 §5.6 对账重建”的恢复
步骤，因为索引本就可由 `row_refs + objects` 重建，不含权威状态：

```sql
CREATE TABLE owner_index(
  row_key TEXT PRIMARY KEY,         -- sha256(canonical(行))，内容身份
  owners  TEXT NOT NULL);           -- canonical(owners)
CREATE TABLE row_refs(
  generation INTEGER NOT NULL,      -- 注意代次可复用，故非外键到 snapshots
  kind       TEXT NOT NULL,
  block_oid  TEXT NOT NULL,
  row_key    TEXT NOT NULL);        -- 版本成员关系，共享块也要逐版记录
CREATE TABLE device_timeline(       -- 可由 row_refs+objects 重建的派生索引
  device TEXT NOT NULL, kind TEXT NOT NULL,
  at_or_day INTEGER NOT NULL, row_key TEXT NOT NULL);
```

不可省略论证：

- 没有 `withdrawals`：重复请求、响应丢失、并发双撤回无法区分
  （对照 `receipts` 在源发布中的作用 `lake.py:25-27`、`lake.py:88-94`）。
- 没有 `watermark.epoch`：读取无法快照“当前累计撤回集合”，跨越确认的调用
  无法线性化。
- 没有 `owner_index`：判断一行是否属于撤回者必须扫描对象，退化成整库扫描；
  内容主键使恢复/代次复用后身份不变（实测见 e3 `restore_and_reuse`）。
- 没有 `row_refs`：共享块无法判断“哪个代次引用了该行”；干净代次会被误判
  受影响，代次复用也会串义。
- `device_timeline` 是唯一可省略项：省略后闭包仍正确，但 C1 时间线读取会
  退化为扫描该代次全表，违反有界约束，故在线部署保留；它可随时由
  `row_refs` 与 `objects` 重建，崩溃后重建不影响正确性。
- 不需要任何“每版本受控结果副本”（那是 S2 的可选附加物）。

### 5.2 首次在线建索引与低水位交接（时序 E）

建索引在服务启用时一次性全量进行，期间**不暂停发布**：

```
# 索引器
w = SELECT COALESCE(MAX(indexed_generation), -1) FROM index_progress
loop:
  rows = SELECT generation, roots FROM snapshots
         WHERE generation > w ORDER BY generation LIMIT B
  if empty: break
  for each (gen, roots):                # roots 是元数据，不是对象扫描
     for kind, oid in roots:
        if oid not in seen_blocks:      # 内容去重，对象只 get 一次
           for row in get(oid):         # lake.py:43-48
              upsert owner_index(row_key(row), row.owners)
              upsert device_timeline(...)
           seen_blocks.add(oid)
        insert row_refs(gen, kind, oid, keys...)   # 成员关系始终记录
     w = gen; update index_progress      # 短事务
```

交接正确性：对象在 `prepare` 时已 autocommit 落盘（`lake.py:67`、
`lake.py:39`），新发布提交（`lake.py:105`）后其快照对索引器立即可见；
索引器按代次顺序 drain，建库期间插入的 gen3/gen4 不会漏（实测四代全部
覆盖、共享块行只扫描一次：`evidence/out/e4_timelines.json` 时序 E 的
`all_snapshot_gens_covered` 与 `each_distinct_row_scanned_once`）。
启用完成前到达的撤回请求进入 `PREPARED` 但不允许 `CONFIRMED`，直到
`w == head()` 追上一次；追上后正常确认，无需停写。

### 5.3 撤回协议：绑定、幂等、并发与线性化点

```
prepare_withdrawal(request_id, subject, params):
  binding = canonical(params)                       # 同 lake.canonical 口径
  ledger: BEGIN IMMEDIATE
    row = SELECT ... WHERE request_id=?
    if row: 若 subject/binding 不同 -> WITHDRAWAL_REQUEST_CONFLICT
            否则原样返回（重复请求）
    insert (request_id, subject, binding, PREPARED, -1)
  COMMIT

confirm(request_id):
  ledger: BEGIN IMMEDIATE                          # 唯一线性化点
    row = SELECT ... WHERE request_id=?
    if missing -> UNKNOWN_PREPARED_REQUEST
    if status=CONFIRMED -> return row.epoch        # 响应丢失后的幂等重放
    e = watermark.epoch + 1
    W = SELECT subject FROM withdrawals WHERE status='CONFIRMED'
    W' = W ∪ {row.subject}
    closure = compute_closure(W, W')               # 仅 C1..C3，见 §3
    update withdrawals SET status='CONFIRMED', epoch=e
    update watermark SET epoch=e
  COMMIT ; return e
```

- **请求绑定/重复请求**：与源发布回执同构（`lake.py:88-94`），同
  request_id 同 binding 幂等，异 binding 冲突。
- **响应丢失**：提交成功但客户端未收到 ACK，重试走 `CONFIRMED` 分支返回
  同一 epoch（实测时序 D `response_lost_replays` 保持 epoch 1、2）。
- **并发双撤回（时序 D）**：两个请求可同时 PREPARED 并各自预计算影响集；
  `BEGIN IMMEDIATE` 串行化确认，后确认者在锁内重新读取累计集合 `W`，
  **丢弃持旧影响集的预计算**，对 `W'={alice,bob}` 重算闭包。共有行 e5
  整行删除、h1 释放 d3 都在并集闭包内，结果等价于一次性双撤回
  （实测 gen1 并集值 east=7/1/max7、west=5/1/max5，与 `checks.py:21-22`
  的冻结断言一致）。
- **跨确认读取的线性化**：读取在短事务内取 `(watermark.epoch, 所需快照)`，
  水位之前的读取见旧集合，水位之后见新集合；不等待任何读者退出。

### 5.4 受控读取路径

```
controlled_raw(g):
  W = subjects_at(watermark.epoch)
  tables = {}
  for kind in KINDS:
    tables[kind] = [r for block_oid in snapshot(g).roots[kind]
                      for r in get(block_oid)
                      if not owners_intersect(r, W)]   # owner_index 判定
  return tables                                        # 等价 §1.1 的过滤步

controlled_export(g):
  if W empty: return get(snapshot(g).result_oid)       # 快路径：零撤回
  return evaluate_bounded(g, W, closure_cache)         # 只重算 C3 组
```

- `raw` 的受控版本返回过滤表；`pin` 返回 `(g, reservation_epoch, token)`，
  `export` 以当前水位经 `controlled_export` 解析——旧句柄在确认后导出新值，
  且不再依赖句柄内嵌字节（修复 O1）。
- 已交付外部的字节不追回，符合“确认之后新发起/再导出”的约束措辞。
- 撤回者的新补行：`owner_index` 以行内容为键，任何代次（含恢复后的新代次）
  中含撤回者的行在装配阶段即被剔除，“后来补入也不得重新可见”由内容匹配
  保证，与代次数字无关（实测 R1 与时序 C）。
- 同物理块内夹带的其他行：过滤按行判定，块仍整体读取，合法行正常返回，
  不算绕过。

### 5.5 缓存策略：版本化键，不清实例

Reader 缓存键由 `result_oid` 改为 `(result_oid, watermark.epoch)`（受控派生
结果可带 ephemeral 缓存层，不落持久状态）。确认只推进水位号：

- 旧键自然失活，无需遍历任何 Reader、无需知道有多少活动连接；
- 零撤回时所有读取命中原始 `result_oid` 快路径，行为与今天逐字节相同；
- 镜像 Reader 的独立缓存同理，因为键里的 epoch 来自统一账本而非实例内存。

### 5.6 镜像与恢复边界（时序 C）

- 账本 `withdrawals.db` 不参与 `checkpoint`/`restore` 的 backup
  （`lake.py:114-120` 只覆盖湖库连接），恢复在物理上无法回退水位。
- 打开受控镜像时，网关把镜像连接挂到同一服务的账本视图：镜像读取同样按
  `watermark.epoch` 过滤。镜像成为新活动库后，新写入经 §5.4 同样处理；
  实测恢复后补入的 alice 行被过滤、cara 合法行保留
  （`evidence/out/e4_timelines.json` 时序 C）。
- 多个逻辑库副本由同一服务控制：任何副本的受控入口都解析同一账本；
  不防御直接拷贝库文件用独立程序读取（CONTRACT.md 第 33 行）。
- 代次复用：`row_refs` 以每次发布的 `(generation, kind, block_oid)` 为记录，
  恢复后复用数字 3 时插入的是新记录；内容哈希保证同一行在任何副本身份
  一致（实测 e3 `alice_hits_after_restore == [1,2,3]`）。

### 5.7 与正常发布的并发

- 源发布事务（`lake.py:86-105`）与账本事务是两个独立 SQLite 事务，没有
  跨库原子性假设；二者通过“已发布快照不可变 + 索引异步 drain + 读取按
  水位”解耦。
- 时序 A：`prepare(2, {"holds":[[]]})` 后长期不发布是现有合法行为
  （`probe.py:31` 同样如此）；撤回确认不推进源 head，旧 plan 的
  STALE_HEAD 检查只看源 head（`lake.py:96-97`），仍可发布为 gen3；网关
  对 gen1/gen2/gen3 统一套用累计集合（实测三个代次的 ORACLE 值均已保存）。
- 新发布与确认并发：发布只写湖库，确认只写账本+读索引，冲突域不重叠；
  读取以 `(epoch, generation)` 两个不可变锚点定序，无长锁。

### 5.8 故障模型与恢复

可信前提：单个 SQLite 事务原子性；不可信跨库事务原子性（CONTRACT.md
第 39 行）。崩溃点枚举：

1. **PREPARED 落盘前崩溃**：无任何记录，客户端重试走全新插入。
2. **PREPARED 后、CONFIRMED 前崩溃**：重开后该请求停在 PREPARED，水位未动，
   任何读取不受影响；重试同 request_id 直接再确认（实测时序 D 的
   `crash_prepared_row_status_on_reopen` 为 PREPARED，重试成功）。
   可加惰性回收：PREPARED 超过请求 TTL 由同 request_id 重试覆盖。
3. **确认事务提交后、响应前崩溃**：状态已是 CONFIRMED，重试幂等返回同一
   epoch（§5.3），水位不跳号。
4. **建索引中途崩溃**：`index_progress` 水位短事务提交，重开从 `w+1`
   drain；`owner_index` 以 row_key 为主键，重复 upsert 幂等。
5. **进程重开**：账本与索引均为持久表，Reader 内存缓存丢失但可重建，无
   安全影响（缓存从不是边界）。
6. **湖库恢复到旧镜像**：见 §5.6，账本水位保持；索引按内容对账，缺失的
   row_refs 由镜像快照补齐后即可服务。
7. 全部持久介质同时丢失不在本轮恢复承诺内（CONTRACT.md 第 39 行）。

## 6. 兼容改造顺序（每步可独立重放，原断言始终保留）

1. 新增 `evidence/` 证据包与受控网关**新模块**，不改 `lake.py` 等一行；
   `checks.py` 持续输出 `NORMAL_REGRESSION_OK`（`checks.py:51`）。
2. 部署账本库与索引表（§5.1），启用 §5.2 在线建索引；此阶段湖库行为零
   变化，索引只做旁路写入并以 ORACLE 对账。
3. 引入受控包装器，先以“影子模式”并行返回 `evaluate(raw)` 与
   `controlled_export`，断言二者在零撤回时相等、撤回演练时等于 ORACLE。
4. 切换消费侧入口到受控包装器；`put/get/snapshot/materialize` 收编为内部
   适配器（§2.4），`Reader` 缓存键版本化（§5.5）。
5. 打开 `prepare_withdrawal/confirm`；镜像打开路径统一挂账本（§5.6）。
6. 可选：读多写少时按 §4 切换 S2 物化模式，索引/账本组件复用。

每一步都不改动冻结查询 `query.evaluate`（`query.py:5-27`）与源发布语义，
因此 `checks.py:15-30` 的全部数值断言与时序断言持续成立。

## 7. 时序 A–F 与证据/输出的对应索引

| 时序 | 关键判断（源码依据） | 证据位置（可重跑） |
|---|---|---|
| A | 撤回不动源 head；旧 plan 仍过 STALE 检查（`lake.py:96-105`）；三代统一过滤 | `e4_timelines.json` → A：`head_unchanged_by_withdrawal`、`old_plan_publishes_as_gen3`、三代 `oracle_controlled_after_confirm` |
| B | 句柄内嵌字节是缺口（`lake.py:139-143`）；版本化缓存键修复，多 Reader 不清实例 | e4 → B：`observed_gap_handle_embeds_stale_bytes` 全 true，对照 `oracle_required_under_watermark` |
| C | restore 只覆盖湖库（`lake.py:120`）；账本独立；新 alice 行内容过滤、cara 行保留 | e4 → C：`alice_row_gone`、`legal_row_kept`、`ledger_not_rewound_by_restore` |
| D | 串行确认 + 并集闭包；e5 整行删、h1 释放 d3；三类崩溃与幂等重放 | e4 → D：`cumulative_oracle_gen1`=[east 7/1/7, west 5/1/5]、epoch 1/2、重放同 epoch、水位跨重开为 3 |
| E | 低水位 drain 不停写；共享对象只解析一次；四代无遗漏 | e4 → E：`all_snapshot_gens_covered=[1,2,3,4]`、`each_distinct_row_scanned_once` |
| F | §3.1 任意输入完备性；赢家回退 W1、归属回退 A1/A3/F2b、反连接 H1/F4、遮蔽 L1/F3 | `e2_counterfactuals.json` 九例 + e4 → F 四步断言 |

## 8. 证据与重放入口

- `python3 evidence/run.py verify`：先跑原回归 `checks.py`，再生成
  `evidence/out/e1..e4` 四份 JSON，最后执行 `verify_reports.py`：
  新鲜 ORACLE 重算比对、DESIGN 引用行号存在性与锚点内容核对、业务源
  `git diff` 完整性核对；任一失败以非零退出码报告。
- `python3 evidence/run.py demo`：输出未修改业务的真实 `probe.observe()`
  基线（含两个历史版本的 before/without_alice/without_bob/without_both、
  固定句柄、重放、恢复实际值）与单因反事实摘要。
- 证据分层标签：每个脚本与报告标注 OBSERVED / ORACLE / PROPOSED
  （定义见 `evidence/common.py` 模块头）。ORACLE 仅用独立过滤加冻结全量
  evaluate 求期望值，不作为在线维护算法；在线有界性由 `BlockIndex`
  的候选闭包与扫描计数演示（`evidence/out/e3_closure.json`）。
