# osmface — 离线 OSM 风格面关系接入器

把项目限定的 OSM 风格资料（共享节点、方向各异的 way、嵌套 relation）
装配成可入仓的 `Polygon`/`MultiPolygon`，并为每段边保留真实来路。
完全离线：不做网络抓取、不吸附或修改坐标、不计算空间统计。

- 输入：JSONL，每行一个 `node` / `way` / `relation` 记录（各类 id 域分开、可乱序出现）
- 暂存：SQLite（node/way 索引落盘，装配时按需取用，不把整个地区图装内存）
- 几何：Shapely 负责有效性与包含判断；成员展开与组环全部自实现，
  不调用任何 OSM 专用多边形装配器
- 历史：一次性暂存库可原地升级为可持续更新、按修订查看的地图数据仓；
  变更按批原子接入，局部重算，几何装配仍只走上面这一套程序

## 安装与运行

依赖：Python ≥ 3.10、`shapely`（测试另需 `pytest`）。

```bash
# 在仓库根目录（package 为 osmface/）
python3 -m osmface load staging.db fixtures/base.jsonl
python3 -m osmface assemble staging.db --out out/base
python3 -m osmface inspect staging.db
python3 -m osmface inspect staging.db --relation 200
python3 -m osmface inspect staging.db --relation 200 --json
python3 -m osmface inspect --package out/base
```

Python API：

```python
import osmface
osmface.load_database("staging.db", ["fixtures/base.jsonl"], force=True)
summary = osmface.assemble_database("staging.db", out_dir="out/base",
                                    max_boundary_nodes=1_000_000)
info = osmface.inspect_database("staging.db", relation_id=200)
pkg = osmface.inspect_package("out/base")
```

## 输入模型

```json
{"type":"node","id":1,"x":0,"y":0}
{"type":"way","id":1,"nodes":[1,2]}
{"type":"relation","id":200,"members":[{"type":"way","ref":1,"role":"outer"},
                                      {"type":"relation","ref":100,"role":"inner"}]}
```

- node：唯一整数 id + 有限平面坐标 x/y；way：唯一 id + 有序 node id（≥2 点）；
  relation：唯一 id + 有序成员，成员为 way/relation 引用及 `outer`/`inner` 角色。
- 不支持历史版本、无角色成员与其他关系类型（schema 故障处理）。
- 完全一致的重复定义静默合并；冲突定义是输入故障，staging 标记 `failed`，
  `assemble` 拒绝运行——损坏输入永远不会成为完整入仓包。

## 装配语义

**状态机**：每个关系最终为 `complete` / `incomplete` / `topology_conflict`。

- **缺口（incomplete）**：引用不存在的 way/relation/node 时按关系列出缺口
  （含到达路径），并传播至所有使用该子关系的父关系；与缺口无关的关系照常成面。
- **引用环（incomplete, `relation_cycle`）**：迭代 Tarjan 找出强连通分量并给出
  实际环链（如 `[400,401,400]`），受影响的父闭包一并隔离；不依赖递归深度。
- **嵌套角色翻转**：`outer` 保持子关系角色，`inner` 反转 outer/inner。
  way 出现身份带完整成员索引链 `[[relation, member_index], ...]`；
  同一 way 被不同关系/不同位置使用不做全局去重；共享子关系只编译一次，
  但父级角色与出现来源不丢。
- **组环**：每种最终角色下，开放 way 只按端点 node 身份拼接（允许倒向；
  坐标恰相等而 id 不同的 node 不算同一点）。端点连接度必须恰为 2，
  得到唯一的无向环分解；度不符、重复片段、无法闭合一律 `topology_conflict`，
  不任意走分支。已闭合 way 自身即候选环，内部节点不变。
- **环几何**：无自交、非零面积的简单环；彼此可嵌套但边界不得相交或相触。
  每环取最小包含父环；顶层必须 outer，嵌套角色逐层 outer/inner 交替。
  每个 outer 与其直接 inner 孩子构成一个面；更深的 outer 是独立岛。
- **输出环规范化**：仅规范方向（outer 逆时针、inner 顺时针）与起点
  （最小 node id），坐标与节点身份不变。
- **边界规模上限**：`--max-boundary-ways` / `--max-boundary-nodes` 按关系限制
  展开规模，超限为 `incomplete`（`boundary_limit_exceeded`），绝不截断冒充成功。

**溯源**：每个完成面的每段边可追到原 relation 成员链、way 出现、
原 node 序列（`way_definition_nodes`）及是否倒向（`reversed`）。

## 暂存与输出状态

- staging（SQLite `meta.staging_state`）：`loading` → `loaded` / `failed`。
- 输出包（`manifest.json` 的 `state`）：先写入 `*.building-<pid>` 目录
  （state=`building`），原子改名就位后才翻转为 `complete`；
  任何写出失败只会留下 `building`/`failed`，不会暴露为完整入仓包。
- 输出包含 `relations.jsonl`（逐关系状态 + GeoJSON 几何 + 面/环/段溯源）
  与 `manifest.json`（来源哈希、计数、状态）。

## 修订化地图数据仓（变更接入 + 历史查询）

普通 `load` + `assemble` 出来的库可以原地启用历史，成为修订 0 的数据仓。
之后只通过变更批更新，每次成功提交生成下一个连续修订；几何装配复用既有的
缺口传播、展开、组环、嵌套与溯源逻辑，不另写一套几何程序。

### 启用历史（修订 0）

前置条件：库处于 `loaded`、且已有一次成功 `assemble` 的结果。存在
`incomplete` / `topology_conflict` 关系不阻止启用；损坏输入（`failed`）或没有
装配结果的库拒绝启用。启用可安全重试，不清空原始记录、结果或来源。

```bash
python3 -m osmface load w.db fixtures/base.jsonl
python3 -m osmface assemble w.db
python3 -m osmface enable-history w.db        # 现存对象版本均为 1，装配预算沿用
python3 -m osmface inspect w.db --revision 0  # 修订 0 也有可查询身份
```

```python
import osmface
osmface.load_database("w.db", ["world.jsonl"], force=True)
osmface.assemble_database("w.db")
osmface.enable_history("w.db")
osmface.enable_history("w.db")  # 幂等：返回 revision=0，不动数据
```

启用历史后，旧的整库 `assemble` 写入口会被明确拒绝，避免绕过修订直接覆盖结果；
普通旧库上的原命令/API 行为完全不变。

### 变更文件（UTF-8 JSONL）

首个非空行是批头，其后是非空变更集合；行次序不是执行顺序（按整批最终状态处理，
允许 way 先引用同批后面才新建的 node）：

```json
{"batch_id":"survey-1","base_revision":0}
{"op":"put","expected_version":1,"entity":{"type":"node","id":7,"x":12,"y":4}}
{"op":"delete","expected_version":1,"type":"way","id":20}
```

- `put` 必须给完整定义（node/way/relation，沿用现有输入合同校验）；
  新 id 的 `expected_version` 为 0，现存对象必须恰等于当前对象版本；
  布尔值不得充当整数。
- 成功的 `put`/`delete` 都使对象版本加 1；`delete` 保留墓碑与版本，
  删除不存在或已删除对象被拒绝；对墓碑 `put` 即同 id 恢复，版本续接、不重置为 1。
  内容未改变的合法 `put` 也产生新对象版本。
- 每批仅在 `base_revision` 等于当前头修订时接收；任何坏行、同批重复对象、
  版本冲突或读取失败都不消费修订、不留部分更新、不移动头指针。
- `batch_id` 绑定文件原始字节的 SHA256：同 id 同字节重送（即使头已前进）返回原回执、
  不产生新版本；同 id 不同字节拒绝。失败尝试不占用 `batch_id`。
- 合法的对象变更即使造成缺引用、引用环或几何冲突也照收入仓，在新修订把受影响关系
  标记为未完成/冲突并移除其过时面；之后补齐或解环可恢复。删除 relation 后当前结果
  不再有它，历史修订仍可查到。

### 局部更新与影响路径

反向依赖为 node→引用它的 way、way→引用它的 relation、relation→父 relation，
引用缺失对象的边也落盘保留。一次变更的受影响关系是：从本批所有变更对象出发，
在**变更前或整批变更后**任一版反向依赖中可达的 relation（含被直接改/建/删的
relation 本身）。回执给出每个关系至少一条真实影响路径，并标注依据 `old` /
`new` / `both` / `direct` 图；路径只取自那一版真实的成员/节点索引，不拼接新旧边。

除首次启用允许全库扫描建索引外，后续 apply 不扫描原始 node/way/relation 载荷来
找依赖，也不做整库 assemble：依赖分析只走版本化边索引，几何只加载受影响闭包及其
前向依赖。回执的 `recomputed_relations` 是实际重算的关系 id（不是自填数量），
`fetched` 是实际触达的 way/node 行数。

```bash
python3 -m osmface apply w.db survey-1.jsonl
python3 -m osmface apply w.db survey-1.jsonl --json
python3 -m osmface receipt w.db survey-1
python3 -m osmface inspect w.db --relation 100 --revision 0
python3 -m osmface export-revision w.db out/r1 --revision 1
```

```python
receipt = osmface.apply_changes("w.db", "survey-1.jsonl")
receipt["recomputed_relations"]          # 实际重算的关系 id 列表
receipt["affected_relations"]            # 前后状态/影响路径/几何与来源是否变化
old = osmface.inspect_database("w.db", relation_id=100, revision=0)
new = osmface.inspect_database("w.db", relation_id=100, revision=1)
pkg = osmface.export_revision_package("w.db", "out/r1", revision=1)
```

### 历史读、溯源与导出

`inspect` 带可选 `--revision`：状态、诊断、面、成员索引路径、原 way 节点次序与
倒向全部来自同一修订；完整面的每段来源标注沿途 relation/way 以及**实际使用 node 的
对象版本**。未改动实体沿用旧版本，不会被盖上本批版本号。省略修订时一次查询先固定
头修订，并发提交期间不会把旧状态与新节点/way 拼在一起。历史在新进程重开后可查，
不依赖原始变更文件；保留历史靠增量结果行，不复制整库也不全量重算。

回执逐关系列出：前后状态（含 `absent`）、是否新增/删除、几何是否变化（按拓扑相等
判定，无面视为空几何）、来源是否变化（含出现路径与所用对象版本）。因此同值 `put`
或 way 整体反向导致的“面积不变但来源改变”可明确区分。

导出包的 `manifest.json` 与 `relations.jsonl` 绑定所选修订及产生它的批次
（另有 `revision.json`、`batches.jsonl`），来源字段全部取自该修订的历史表，
不从当前实体表回填。已导出的目录不会被后续 apply 改写；写出失败只留下
`building`/`failed` 标记，不会出现可当成完整包的目录，也不回滚已提交的修订。

### 崩溃、并发与确定性测试同步点

落定前后进程被终止，重开只能见到完整旧修订或完整新修订；已提交但回执未送达时，
用同一文件重送即找回原回执。两个进程基于同一 `base_revision` 并发提交，最多一个
成功，另一个得到明确的锁/过期冲突，不存在双双落定的窗口。

测试同步点是确定性的标记文件协议（不靠随机 sleep）：设置环境变量
`OSMFACE_SYNC_DIR` 后，apply 在 `validated` / `precommit` / `committed` 阶段写入
`reached-<phase>`；放置 `wait-<phase>` 会阻塞到出现 `proceed-<phase>`，放置
`crash-<phase>` 则以退出码 17 在该阶段确定性终止。`OSMFACE_BUSY_TIMEOUT`
（毫秒）可调锁等待，便于争用验收快速失败。

## 夹具与测试

小夹具保留在 `fixtures/*.jsonl`，由 `fixtures/make_fixtures.py` 确定性生成
（重跑应为无操作）；规模数据运行时生成，不入库。

```bash
python3 fixtures/make_fixtures.py                 # 重新生成小夹具
python3 fixtures/gen_scale.py /tmp/scale.jsonl \  # 规模数据：10 万未引用节点
    --unused 100000 --ring-nodes 20000 --grid 30  # + 2 万节点大环 + 900 小关系
python3 -m pytest tests/ -q                       # 全部测试
python3 checks/demo_history.py                    # 修订仓端到端走查
python3 checks/check_history.py                   # 鞋带公式 + 参考投影独立核对
```

历史测试（`tests/test_history_*.py`）覆盖：启用前置条件与幂等、对象版本/墓碑/恢复、
同值 put 与 way 反向的几何/来源区分、批回滚、旧批重送与改字节冲突、关系删除恢复、
缺口与引用环的引入和解除、双写者争用、确定性中止与读写交错、SQLite authorizer 与
`EXPLAIN QUERY PLAN` 证明的局部性，以及 `checks/reference_projection.py`
（只解释变更语义、不调用任何生产变更/依赖/历史逻辑）逐修订对照普通全量装配。
规模数据运行时生成，不入库。

基准夹具 `base.jsonl`：四个乱向 way 拼成 10×10 外方环（100），子关系 C 含
outer 洞环 H（16）与 inner 小岛环 I（4），根关系以 inner 引用 C，于是 H 翻为
inner、I 翻为 outer —— 两个面，100−16+4 = **88**。其余夹具各隔离一类故障：
乱序/整体倒向（逻辑面不变）、删 node、子关系环、同坐标不同端点 id、三岔、
共享 way、共享子关系、边界相触、自交环、顶层 inner、角色交替错误、
重复/冲突定义。

## 目录结构

```
osmface/            # 包
  db.py             # SQLite 暂存、校验、索引读取
  graph.py          # 引用环检测（迭代 Tarjan）与依赖序
  rings.py          # 端点身份组环、环有效性、规范化与段溯源
  nesting.py        # 最小包含父环、角色交替、面构造
  assemble.py       # 缺口传播、展开（含共享子关系编译缓存）、限额、落库
  history.py        # 版本化实体/边/结果、原子 apply、反向依赖闭包、历史读
  export.py         # 入仓包与修订包状态机写出
  model.py          # 常量、异常、确定性测试同步点 SyncPoint
  api.py / cli.py   # Python API 与 load/assemble/inspect/enable-history/apply 等命令
fixtures/           # 小夹具 + 生成器（含规模数据生成器）
checks/             # 端到端走查、独立参考投影与可复跑核对脚本
tests/              # pytest 测试（含历史/局部性/并发/中止/独立对照）
```
