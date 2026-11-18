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
合流产生的修订在 `revision.json` 里标记为 `kind="merge"` 并记录 `merge_id`。

### 崩溃、并发与确定性测试同步点

落定前后进程被终止，重开只能见到完整旧修订或完整新修订；已提交但回执未送达时，
用同一文件重送即找回原回执。两个进程基于同一 `base_revision` 并发提交，最多一个
成功，另一个得到明确的锁/过期冲突，不存在双双落定的窗口。

测试同步点是确定性的标记文件协议（不靠随机 sleep）：设置环境变量
`OSMFACE_SYNC_DIR` 后，apply 在 `validated` / `precommit` / `committed` 阶段写入
`reached-<phase>`；放置 `wait-<phase>` 会阻塞到出现 `proceed-<phase>`，放置
`crash-<phase>` 则以退出码 17 在该阶段确定性终止。`OSMFACE_BUSY_TIMEOUT`
（毫秒）可调锁等待，便于争用验收快速失败。合流确认（merge-confirm）使用同一
同步点协议。

## 历史面空间查询（点覆盖 / 矩形相交）

分析人员的问题是“这个坐标落在哪些面里？”“这块范围与哪些面相交？”。已启用历史的
数据仓在**已经生成并持久化的面**之上维护一套持久空间候选索引，查询直接读
`face_history` 里该修订保存的 Shapely 几何，绝不重新装配 node/way/relation，也不
修改任何修订。

### 索引结构与维护边界

- `face_rtree`（SQLite R-tree 虚拟表）+ 配套表 `face_rtree_rows`：按修订保存每个
  完整面（含 MultiPolygon 的每个 face）的包围盒，覆盖洞与岛——索引的是完整面几何
  的包围盒，精确判定仍交给 Shapely。
- `face_rtree_bypass`：R-tree 坐标为 float32 精度，有限 float32 范围内 R-tree 一律
  **向外舍入**，不会因向内舍入漏召回；当包围盒超出有限 float32 范围
  （`|坐标| > 3.4028234663852886e38`，SQLite 会存成 `inf` 而无法被有限窗口命中）时，
  该面改走旁路表，以真实 float64 包围盒参与候选。每修订的旁路行数记入
  `spatial_index_meta.bypass_rows`，可审计；真实数据上预期始终为 0。
- `spatial_index_meta`：每个修订一行，记录该修订的 R-tree/旁路行数与构建时间。

启用历史时在同一事务里为修订 0 建好索引；此后每一次 apply 与离线副本合流确认
（merge-confirm）产生的修订，都在**同一个成功提交事务**里增量写入该修订的索引。
被删除、变为 `incomplete`/`topology_conflict` 或不再有面的关系在该修订没有任何索引
行，因此不可能继续作为完整面命中；旧修订的索引行原样保留。索引写入失败会回滚整个
修订，头修订、关系几何与索引版本不可能出现彼此不一致的可见状态。普通未启用历史的
库行为完全不变，不建任何空间表，也不丢弃原始记录、装配结果或来源。

### 查询语义

```bash
python3 -m osmface query-point w.db 8 8 --revision 0
python3 -m osmface query-range w.db 20 -5 20 25 --json   # 零宽范围，允许
python3 -m osmface spatial-index w.db
```

```python
osmface.query_faces_at_point("w.db", 8.0, 8.0, revision=0)
osmface.query_faces_in_range("w.db", 5.0, 5.0, 6.0, 6.0)  # 省略 revision
osmface.spatial_index_info("w.db")
```

- 坐标使用既有平面 x/y；点与范围参数必须是有限数，范围满足 `minx <= maxx`、
  `miny <= maxy`，零宽/零高范围允许。损坏参数、无效修订、未启用历史的库都返回明确
  错误，绝不返回看似成功的空结果。
- 点查询用 `covers`：落在面边界上算覆盖，洞内部不算覆盖，洞内的岛作为独立面正常
  命中。范围查询用 `intersects`：与几何边界仅接触也算相交。
- 一个关系有多个面时只返回一次该关系，并给出实际命中的 `faces` 索引；多关系重叠时
  全部返回，不挑选“最佳”结果。结果按 relation id 稳定排序，并在返回体中说明固定的
  `revision`、查询类型（`point`/`range`）与索引候选数量
  （`candidate_faces`/`candidate_relations`、R-tree 与旁路分别的候选数、精确检查数
  `exact_checks`、最终命中数 `hit_count`）。
- 省略修订时，整个查询固定在调用开始时观察到的一份头修订上（查询在一个读事务快照
  内完成）；查询依赖的索引候选与几何都来自同一修订，不会出现当前头索引搭配旧修订
  几何的错配。

### 两阶段判定：索引只缩小候选集合

1. R-tree（加旁路表）按包围盒返回候选面；
2. 只对候选面反序列化该修订实际保存的 Shapely 几何，执行精确 `covers`/`intersects`。

包围盒相交从不直接当成面相交——洞内部与凹形空白的假命中全部在第二阶段消除。查询
不会为每个请求扫描或反序列化全库几何：局部性由 `EXPLAIN QUERY PLAN`（R-tree 虚拟
表索引 + 配套表 revision 索引，无 `face_history` 全表扫描）与 SQLite authorizer
（原始 node/way/relation 载荷表完全不被打开）独立证明，见
`tests/test_spatial_locality.py`。

## 离线工作副本与合流

外业小组可以领取一份离线工作副本，断开主库独立修改、预览，回来后把资料包交给
资料库审阅合流。整条链路：**领取 → 离线编辑/预览 → 导出资料包 → 制订合流方案 →
裁定冲突 → 预览候选 → 确认合流 → 查回执**。

### 来源身份与领取（checkout）

每个已启用历史的资料库都有稳定的 `source_id`（启用时生成；更早的库在首次需要时
补发并持久化，重复领取、进程重开都不会改变）。领取从指定修订（省略时为当时头）
流式复制一次实体快照——含实体定义、墓碑与对象版本——不复制全部历史、既有导出
或运行垃圾。副本记录它来自哪一个资料库、哪一个修订；内容相同但没有共同来源的
两个库不能互相收件。领取后副本不再打开主库路径，可在新进程里离线工作。

```bash
python3 -m osmface source-id main.db
python3 -m osmface checkout main.db fieldA.db            # 固定当时头修订
python3 -m osmface checkout main.db fieldB.db --revision 3
python3 -m osmface wc-info fieldA.db
```

### 离线编辑（wc-edit）与预览（wc-preview）

编辑文件是 UTF-8 JSONL：首行批头 `{"edit_id","base_edit"}`，其后每行一个
put/delete。`base_edit` 必须等于副本当前编辑头，防止用过期副本头覆盖他人已写入
的离线编辑；`edit_id` 绑定文件字节，同 id 同字节重送返回原回执，同 id 不同字节
拒绝。已有对象用主库整数 id（`expected_version` 对副本内版本）；野外新建对象用
副本内局部名称（按实体类型分域、按副本隔离，两份副本的 `n1` 互不相干）：

```json
{"edit_id":"day1","base_edit":0}
{"op":"put","expected_version":1,"entity":{"type":"node","id":7,"x":12,"y":4}}
{"op":"delete","expected_version":2,"type":"way","id":20}
{"op":"put","expected_version":0,"entity":{"type":"node","local_id":"n1","x":5,"y":5}}
{"op":"put","expected_version":0,"entity":{"type":"way","local_id":"w1",
  "nodes":[7,{"local":"n1"},7]}}
{"op":"put","expected_version":0,"entity":{"type":"relation","local_id":"r1",
  "members":[{"type":"way","ref":{"local":"w1"},"role":"outer"}]}}
```

- 局部名称是 1–64 位 `[A-Za-z0-9_.-]` 且不得是整数字面量，与整数 id 明确区分；
  引用写成 `{"local":"n1"}`。不占用负数充当临时空间（负数本来就是合法主库 id）。
- 同批按最终状态解释，允许 way 先引用同批稍后才创建的对象；失败不留半批，
  不消费 `edit_id`，不移动副本编辑头。
- 每批完成时，指向局部名称的引用必须指向已定义且存活的同类对象；主库整数引用
  仍可缺失，预览按既有规则显示 `incomplete` / `topology_conflict`。
- 副本内不能新建整数 id（新对象只能走局部名称）；可对已有对象修改/删除，也可
  反复修改、删除、恢复局部对象（版本续接）。
- 离线编辑与预览只动副本文件，绝不移动主库头。

`wc-preview` 用同一套装配程序（缺口传播、组环、嵌套、溯源）装配副本当前状态，
输出区分主库整数身份与外业局部名称。

### 资料包（wc-export）

`wc-export` 把固定副本头导出为可搬运目录：`manifest.json`（状态机写出，
`building`→原子改名→`complete`）、`provenance.json`（来源身份、领取基线、
副本头、三份真实摘要）、`baseline.jsonl`（领取基线快照，含版本与墓碑）、
`edits.jsonl`（相对基线的净最终修改；已删除的局部对象不留下痕迹）。
manifest 绑定每个文件的长度与真实 SHA256；重新打开包不依赖副本目录或主库原路径。
缺件、内容损坏、类型错误、重复身份、自填来源与摘要不符都会被拒绝；manifest 里的
路径只接受裸文件名，绝不当作任意宿主路径。

### 合流方案（merge-plan / merge-show / merge-rule）

收件人先生成并持久保存合流方案，而不是直接 apply。方案固定包内容、副本头、
领取基线 B 与接收时的主库头 H，并逐对象比较 B、外业最终状态 L、H 三侧的
**业务定义**（存活/删除 + 坐标或完整有序引用列表；不含对象版本号）：

- `L == B` → 保留主库修改（keep_main）；`H == B` → 采用外业修改（take_field）；
  `L == H` → 接受共同状态（same）；其余为待裁定冲突。
- 坐标对、way 整条节点序列、relation 整条成员序列都作为完整对象比较：换序、
  重复出现次数、角色变化都算定义变化，不按字段或列表位置拼接。
- 删除（墓碑，有版本依据）与从未存在是不同身份；删除后恢复到相同定义、
  不改内容仅版本不同，不算内容冲突。
- 方案同时列出自动保留的主库修改、拟接入的外业修改（含全部存活局部对象，
  即使未被 relation 引用）与待裁定冲突的三侧原值。

裁定分步保存（`merge-rule`，JSON 文件）：每个冲突可选主库一侧、外业一侧、
领取基线，或给一个经过校验的完整自定义定义/删除决定；对没有内容冲突但需要
修复组合引用的已有对象，也可显式添加完整自定义覆盖（须填原因）。方案里看得到
三侧原值、自动结果、覆盖内容与原因；未知对象或失效裁定直接拒绝，不会混入执行。
每次裁定后都对整份候选状态重新解释引用：例如主库删了一个 node 而外业改的 way
仍在用它，方案会醒目列出该缺引用及受影响关系。几何诊断仅供审阅——合法的最终
原始对象仍可按现有入仓规则接受为未完成/冲突状态。局部对象本身的修改靠重新编辑
副本并导出新包，方案不会偷偷制造来源不明的局部身份。

### 候选预览（merge-preview）

预览用全部自动合并结果加当前裁定装配候选地图（不是只装配外业副本），给出受影响
关系的状态、面与引用来源，并分清主库整数身份与外业局部身份。存在未裁定冲突时
只显示冲突清单，不声称有可提交的完整预览。

### 确认合流（merge-confirm / merge-receipt）

确认时主库头必须仍是方案固定的 H，否则明确拒绝（另建方案即可，旧裁定不会被
默默套到新主库上）。确认在一个事务里完成：为所有接入的新对象分配本资料库从未
使用过的整数 id（墓碑也算占用，按类型分域，同一局部对象在一次合流中只有一个
永久身份，不同副本的同名局部对象各分各的），把 node、way、嵌套 relation 中的
局部引用一次性改写成永久 id，按主库现有修订机制落实体版本（已有对象的更改以
H 的对象版本为前提，副本里的版本号从不抄进主库），复用既有受影响关系计算生成
**一个**新修订；若裁定后相对 H 没有实体变化，则不制造空修订。回执记录稳定
`merge_id`、包摘要、方案与裁定身份、B/H/结果修订、新身份分配表与变更对象；
日后可从回执追到外业局部名称和领取时的版本。相同 `merge_id`+包+裁定重试找回
原回执（即使主库后来推进），同 `merge_id` 换包或换裁定拒绝。`merge_id` 与直接
apply 的 `batch_id` 是互不相通的两个命名空间。

```bash
# 完整命令链：从旧主库开始，两份外业资料汇回
python3 -m osmface load main.db world.jsonl && python3 -m osmface assemble main.db
python3 -m osmface enable-history main.db
python3 -m osmface checkout main.db fieldA.db
python3 -m osmface checkout main.db fieldB.db
python3 -m osmface wc-edit fieldA.db editsA.jsonl
python3 -m osmface wc-preview fieldA.db
python3 -m osmface wc-export fieldA.db pkgA
python3 -m osmface wc-edit fieldB.db editsB.jsonl
python3 -m osmface wc-export fieldB.db pkgB
python3 -m osmface apply main.db other-team.jsonl      # 主库继续前进
python3 -m osmface merge-plan main.db pkgA             # → plan_id
python3 -m osmface merge-show main.db plan-XXXX
python3 -m osmface merge-rule main.db plan-XXXX rulings.json
python3 -m osmface merge-preview main.db plan-XXXX
python3 -m osmface merge-confirm main.db plan-XXXX --merge-id merge-A
python3 -m osmface merge-receipt main.db merge-A
python3 -m osmface merge-plan main.db pkgB             # 第二份同样合流
```

Python API 一一对应：`checkout_workcopy` / `workcopy_edit` / `workcopy_preview` /
`workcopy_export` / `create_merge_plan` / `get_merge_plan` / `save_merge_rulings` /
`merge_preview` / `confirm_merge` / `get_merge_receipt`（另有 `source_identity`、
`workcopy_info`、`list_merge_plans`）。

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
python3 checks/demo_merge.py                      # 离线合流端到端走查
python3 checks/check_merge.py                     # 三方合并独立验收
python3 checks/check_spatial.py                   # 历史面空间查询独立验收
```

历史测试（`tests/test_history_*.py`）覆盖：启用前置条件与幂等、对象版本/墓碑/恢复、
同值 put 与 way 反向的几何/来源区分、批回滚、旧批重送与改字节冲突、关系删除恢复、
缺口与引用环的引入和解除、双写者争用、确定性中止与读写交错、SQLite authorizer 与
`EXPLAIN QUERY PLAN` 证明的局部性，以及 `checks/reference_projection.py`
（只解释变更语义、不调用任何生产变更/依赖/历史逻辑）逐修订对照普通全量装配。

离线合流测试（`tests/test_workcopy.py`、`tests/test_merge_*.py`）覆盖：领取来源
与快照内容、断主库后的独立编辑/预览、局部名称分域与整数区分、编辑批原子性与
过期副本头、资料包完整性与自洽、三方逐对象判定（含删除/恢复/从未存在、有序重复
成员）、两份副本同名局部对象的分配、交叉缺引用的诊断与自定义修复、未裁定/主库
推进/重送等失败路径、确定性中止与合流-vs-直写争用、规模数据上的合流局部性。
`checks/check_merge.py` 是独立于生产合并判定代码的验收：手写 B/L/H 三列清单
逐对象算期望，候选原始对象独立物料化后送入原装配器做几何对照。
规模数据运行时生成，不入库。

空间查询测试（`tests/test_spatial_queries.py`、`test_spatial_merge.py`、
`test_spatial_locality.py`）覆盖：手写外方/洞/岛/不相交面/重叠面的逐点逐范围
期望（含边界点、洞内部、零宽零高范围）、移动边界后旧修订冻结与新修订差异、
apply 的删除/不完整/恢复与拓扑冲突离线索引、合流确认新增局部对象面与幂等重试、
空结果与多关系重叠、非有限坐标与倒置范围等参数错误、无效修订与未启用历史的明确
报错、R-tree float32 精度边界与有记录旁路、索引写失败整体回滚与恢复、查询期间
并发提交的固定读修订，以及真实 R-tree 行/版本状态/几何结果的物理核对。
`checks/check_spatial.py` 的独立参考不调用任何新空间查询或索引维护函数，只用
手写坐标算术与经 `inspect` 读出的持久几何上的 Shapely 判定逐修订对照。

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
  spatial.py        # 历史面持久 R-tree 候选索引（含精度旁路）与点/范围精确查询
  workcopy.py       # 离线工作副本：领取、编辑、预览、资料包导出
  merge.py          # 合流：包校验、三方方案、裁定、候选预览、原子确认
  export.py         # 入仓包与修订包状态机写出
  model.py          # 常量、异常、确定性测试同步点 SyncPoint
  api.py / cli.py   # Python API 与 load/assemble/inspect/enable-history/apply/
                    #   query-point/query-range/spatial-index 等命令
fixtures/           # 小夹具 + 生成器（含规模数据生成器）
checks/             # 端到端走查、独立参考投影与可复跑核对脚本
tests/              # pytest 测试（含历史/局部性/并发/中止/独立对照/空间查询）
```
