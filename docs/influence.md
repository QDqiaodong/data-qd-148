# 隐私单元关系影响审计器（influence，只读）

`privacy-budget influence` 是一个**纯只读、不生成噪声、不扣预算**的发布前检查组件。
对真实 SQLite 微数据库上的一个关系查询，它精确回答：**假设把某个隐私单元 unit
在所有表里的全部源记录同时移除，聚合结果会怎样逐格变化**，用于发现联接放大
（join amplification）与核对隐私单元定义。记录不会被真的删除，结果只是在一份
显式只读事务快照上做的局部反事实差分。

> 报告中的每个 unit 影响都是**本次数据快照上的局部量**，不是差分隐私全局敏感度；
> 本组件不做 DP 证明。报告固定携带该声明（`dp_notice`）。

## 命令

```sh
privacy-budget influence --db SOURCE.db --source QUERY.json --output REPORT.json
# 等价：
python -m privacy_budget.cli influence --db SOURCE.db --source QUERY.json --output REPORT.json
```

不带子命令的 v1 用法与 `policy` 子命令契约完全不变。退出码：成功 `0`；任何输入、
快照或执行失败 `2`，且失败不覆盖既有报告。`--db`、`--source`、`--output` 必须
两两异址，输出路径（含其父目录）是符号链接时直接拒绝。仅使用 Python 标准库。

## 查询契约（QUERY.json）

严格 JSON：沿用仓库的 8 MB 上限、拒绝重复键、拒绝 NaN/Infinity、字段集必须精确
匹配（任何未知字段拒绝）。顶层恰好为 `{tables, nodes, root}`，键顺序不影响规范
身份与报告。

### tables（1–4 项）

每项为 `{name, id_column, unit_column, columns}`：

* `columns` 是列名到 `"integer"`/`"text"` 的映射，允许数据值为 NULL。
* ID 列为非空唯一 text；unit 列为非空 text 或 NULL，**NULL 表示公开行**（不归属
  任何 unit，永不被反事实移除）。
* 同一 unit 可横跨多张表；未出现在源数据中的 unit 不会出现在报告里。
* 只允许普通 SQLite 表与声明列：拒绝 view、虚表/影子表、物理列与声明不一致、
  存储类型不符、重复 ID、非法引用。
* integer 范围 ±10^12；标识符与 text 沿用仓库非空、无控制字符、长度上限
  （标识符 ≤128，text ≤1024），NULL 除外。
* 每表至多 20_000 行。

### nodes（1–12 个唯一 id 的无环 DAG）

`root` 必须是唯一的终端 `aggregate`，其余节点不得聚合；所有节点必须从 root
可达。节点字段集必须精确匹配：

* `scan`：`{id, op:"scan", table, alias}`，列名在下游写作 `alias.column`。
  自联接用两个不同 alias 扫描同一表实现；join 两侧列名不得重叠。
* `filter`：`{id, op:"filter", input, column, predicate, value}`，`predicate`
  仅 `eq`/`is_null`；`eq` 的 value 为非 NULL 且类型匹配，`is_null` 的 value
  必须为 NULL。
* `join`：`{id, op:"inner"|"left"|"anti", left, right, on}`，`on` 为非空
  `[{left_column, right_column}]` 等值条件；`anti` 只保留左侧列。
* `aggregate`：`{id, op:"aggregate", input, keys, metrics}`，`keys` 为不重复
  列名数组，`metrics` 为不重复 `{name, op, column}`；`op` 支持
  `count_rows`（column 必须 NULL）、`count`、`count_distinct`、`sum`（仅
  integer）。所有列引用、别名、指标输出名必须无歧义且存在，否则拒绝。

## 关系语义（SQL bag 语义）

* 重复值按行重数（multiplicity）保留；行身份是 `(表名, ID)`，值相同但 ID 不同的
  行不会被合并。
* join 等值条件中的 NULL 永不相等；`left` 在无匹配时产生一行右侧全 NULL 的扩展；
  `anti` 在无匹配时保留左行；filter 的 `eq` 遇 NULL 不通过。
* 分组时 NULL 归同组；`count` 忽略 NULL，`count_distinct` 对非 NULL 值去重，
  `sum` 忽略 NULL 且空和为 0；无 keys 时空输入仍产出一行零指标，有 keys 时空组
  消失。所有计数、求和与差值都是精确 Python 整数，绝不经过 float，也不使用
  SQLite 可能溢出的整数 `SUM`。

## 反事实影响如何计算（影响表示）

生产路径先把 DAG **完整求值一次**为带符号的 bag 关系，每条派生行同时携带

* `idmult`：组成该行的物理源行 `(表,ID) -> 重数`（自联接中同一源行出现多次时
  重数 >1），
* `units`：同样组合下 `unit -> 重数`。

随后对每个 unit 做**局部差分传播**，不在内存复制全表、不重新扫描源库、不调用
完整节点求值器、也不从 scan 重跑 DAG：

* **正向行依赖（inner join / filter / 可加指标）**：对每个节点维护一个签名 bag
  （`DeltaRow` 带符号重数）。join 用三段式差分
  `(L+dL)(R+dR) − LR = dL·R + L·dR + dL·dR`，bag 重数在值元组上自动保留；
  filter 只让满足谓词的差分通过；`count_rows/count/sum` 直接对签名重数求和。
* **缺席条件（left 空扩展 / anti 行）**：基线阶段为每个 join 缓存左右两侧的
  哈希索引与每条左行的匹配重数。移除 unit 后，仅对受影响左行比较“基线匹配重数”
  与“移除后剩余匹配重数”：当右侧匹配**降为零**时才新生成 left 空扩展或 anti
  行，并继续穿过滤、后续联接与聚合。只删掉某一条派生匹配而仍有其它匹配时，
  不会提前产生空扩展。
* **distinct 支持（非线性）**：`count_distinct` 不看派生行数，而按
  `(组, 值)` 的**剩余 bag 支持是否归零**来决定 ±1；多个 unit 共同支持同一值时，
  移除其中一个不会让该值消失，最后一个支持消失才 −1。自联接同一源行时，其移除
  在签名 bag 里是一次带符号变化，支持计数随之正确下降。
* 同一源行在自联接中出现多次，反事实里只按其物理身份移除一次（由 scan 节点按
  unit 重数发出来驱动）。

## 快照与只读保证

* 源库以 `file:...?mode=ro` 打开，从不创建文件、不改库、不装触发器、不执行用户
  提供的 SQL（只运行按声明列生成的 `SELECT`）。
* 所有源行、schema、unit 清单与全部结果取自同一个显式 `BEGIN` 只读事务，事务在
  整个审计期间保持打开；快照建立后的并发写入不影响本次基线与任何反事实。
* 每个被引用表只读取一次（每表一个 scan 节点；自联接是同一快照数据的两个内存
  scan）。
* `logical_snapshot_sha256` 绑定**声明 schema** 与按表名/ID 排序的完整选列值的
  规范 JSON，是逻辑内容哈希，**不是**主库文件 SHA——仅存在于 WAL 的已提交内容也
  会被覆盖。报告绝不包含未声明列。
* 合法输入下，基线与任一 D-u 的每个节点结果不超过 200_000 行，超过受控拒绝，
  禁止静默截断。

## 报告内容（REPORT.json）

* `query_sha256`：规范 QUERY 的 SHA（键顺序无关）；`query_source_sha256`：输入
  字节 SHA；`logical_snapshot_sha256`：逻辑快照 SHA；`report_sha256`：报告内容
  SHA（同一逻辑查询与快照下稳定，不受输入键顺序影响）。
* `baseline`：各组（`group_key` 为 JSON 数组）的基线指标。
* `units[]`：每个 unit 的全部变化单元格
  `(group_key, metric, before, after, delta)`，`delta = after − before`；组键
  比较域为两边并集，缺组指标按 0 对齐，零差异不输出；以及该 unit 的
  `l1`（所有 |delta| 之和）与 `linfinity`（最大 |delta|，无变化为 0）。
* 全体 `max_l1`/`max_linfinity` 及达到最大值的**全部**见证 unit（全为 0 时
  每个 unit 都是见证）。
* 组键按紧凑规范 JSON 的 UTF-8 字节排序，其余按 unit/metric 名排序。
* `diagnostics`：可被测试旁路拦截核实的 `source_scans`、
  `full_node_evaluations`、`delta_node_evaluations` 及逐 unit 计数
  （`per_unit_full_evaluations`、`per_unit_source_scans` 恒为 0）。测试通过子类
  拦截 `evaluate_node`/`_baseline_scan` 验证共享计算，而非仅依赖自报计数。

## 样例与测试

* `fixtures/influence-query.json` 与 `tests/build_influence_fixture.py` 生成的
  `fixtures/influence-source.db` 为可运行合成样例（虚构数据，MIT）。
* `tests/influence_oracle.py`（**仅限测试**）在源库的私有副本上逐 unit 真实
  删行、用 SQL 重跑并在 Python 中聚合，作为独立 oracle。
* `tests/test_influence_*.py` 覆盖：NULL/bag/自联接/left/anti 与 SQLite 参考
  查询一致；组消失、负 sum、distinct 最后支持消失、零影响 unit；联动例（公开
  左行 + 两个同值不同 ID 的 U 右行，移除单条匹配不产生空扩展、整体移除 U 后
  count_rows 0→1）；共享计算旁路拦截；WAL 并发写快照隔离；报告确定性；行数
  上限与输出安全；以及固定/多种子随机差分 fuzz。
