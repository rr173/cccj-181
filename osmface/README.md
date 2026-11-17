# osmface — 离线 OSM 风格面关系接入器

把项目限定的 OSM 风格资料（共享节点、方向各异的 way、嵌套 relation）
装配成可入仓的 `Polygon`/`MultiPolygon`，并为每段边保留真实来路。
完全离线：不做网络抓取、不吸附或修改坐标、不计算空间统计。

- 输入：JSONL，每行一个 `node` / `way` / `relation` 记录（各类 id 域分开、可乱序出现）
- 暂存：SQLite（node/way 索引落盘，装配时按需取用，不把整个地区图装内存）
- 几何：Shapely 负责有效性与包含判断；成员展开与组环全部自实现，
  不调用任何 OSM 专用多边形装配器

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

## 夹具与测试

小夹具保留在 `fixtures/*.jsonl`，由 `fixtures/make_fixtures.py` 确定性生成
（重跑应为无操作）；规模数据运行时生成，不入库。

```bash
python3 fixtures/make_fixtures.py                 # 重新生成小夹具
python3 fixtures/gen_scale.py /tmp/scale.jsonl \  # 规模数据：10 万未引用节点
    --unused 100000 --ring-nodes 20000 --grid 30  # + 2 万节点大环 + 900 小关系
python3 -m pytest tests/ -q                       # 全部测试
```

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
  export.py         # 入仓包状态机写出
  api.py / cli.py   # Python API 与 load/assemble/inspect 命令
fixtures/           # 小夹具 + 生成器（含规模数据生成器）
tests/              # pytest 测试（含手工核对的环节点序、角色、面积、段来源）
```
