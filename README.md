# data-qd-148 · 差分隐私预算台账与联合发布规划

精确有理数的差分隐私预算台账，以及建立在同一目录上的**可复用带噪统计联合发布规划器**。
业务输入、计算与边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
```

### v1 批处理台账（语义不变）

```sh
.venv/bin/privacy-budget --source fixtures/input.json --output run/report.json
```

### v1.5 离线组织级策略编译器（只读，无任何状态变更）

```sh
.venv/bin/privacy-budget policy \
    --source fixtures/policy-pass.json --output run/policy-report.json
```

逐 scope 求配置额度与命中规则上限的逐项最小值（通配始终是硬上限、同值并列
来源全部保留），再按每条规则核验所有命中 scope **有效额度之和**的聚合上限；
聚合超额时退出 2、报告确定的违规规则与 scope 明细且不覆盖旧报告。契约与治理
语义见 [离线策略编译器文档](docs/policy.md)。

### v1.6 隐私单元关系影响审计器（只读，不生成噪声、不扣预算）

```sh
python3 tests/build_influence_fixture.py fixtures/influence-source.db
.venv/bin/privacy-budget influence \
    --db fixtures/influence-source.db \
    --source fixtures/influence-query.json \
    --output run/influence-report.json
```

在一份显式只读事务快照上，对每个隐私单元精确计算“移除该 unit 的全部源记录后，
聚合查询的每个 (组,指标) 单元格怎样变化”，用于发布前检查联接放大与隐私单元
定义。它不删除记录、不声称 DP 全局敏感度；契约、影响表示与报告语义见
[隐私单元关系影响审计器文档](docs/influence.md)。

### v2 联合发布规划器

```sh
# 初始化不可变限额（按 dataset/principal 跨 snapshot 累计）
.venv/bin/python -m privacy_budget.release_cli init \
    --db run/catalog.db --source fixtures/release-limits.json

# 只读规划：输出预算—误差 Pareto 前沿，不返回估计、不改变状态
.venv/bin/python -m privacy_budget.release_cli plan \
    --db run/catalog.db --source fixtures/release-request.json --output run/plan.json

# 显式选择并提交；先持久提交再交付报告
.venv/bin/python -m privacy_budget.release_cli publish \
    --db run/catalog.db --source fixtures/release-request.json \
    --choice CHOICE_ID --output run/report.json

# 目录查询与永久停用
.venv/bin/python -m privacy_budget.release_cli catalog \
    --db run/catalog.db --dataset mobility-week --principal analyst-a --snapshot wave-1
.venv/bin/python -m privacy_budget.release_cli retire \
    --db run/catalog.db --dataset mobility-week --principal analyst-a \
    --snapshot wave-1 --measurement n1
```

`plan`/`catalog` 省略 `--output` 时写 stdout。样例见 `fixtures/release-*.json`。

### v3 裁剪微数据交付管道

```sh
# 生产侧：不可变配置 → 提交源批次并交付不可变净差分包 → 随时回放
python3 -m privacy_budget.clip_cli init   --db run/producer.db --source fixtures/clip-config.json
python3 -m privacy_budget.clip_cli apply  --db run/producer.db --source fixtures/clip-batch-1.json \
    --output run/packet-1.json
python3 -m privacy_budget.clip_cli apply  --db run/producer.db --source fixtures/clip-batch-2.json \
    --output run/packet-2.json
python3 -m privacy_budget.clip_cli export --db run/producer.db --sequence 1 --output run/packet-1.json

# 消费侧：独立库只接收已冻结包，维护裁剪行集与回执
python3 -m privacy_budget.clip_cli sink-init --db run/consumer.db --source fixtures/clip-config.json
python3 -m privacy_budget.clip_cli consume   --db run/consumer.db --source run/packet-1.json
python3 -m privacy_budget.clip_cli snapshot  --db run/consumer.db --role consumer \
    --output run/consumer-snapshot.json
```

每个 unit 先按各 partition 的 `(min priority, id, partition 名)` 排序取前
`max_partitions` 个分区，再在每个获选分区内按 `(priority, id)` 取前 `max_rows` 行；每批
都基于**整批最终源状态**重新裁剪，因此换属、换分区、删除与优先级更正都会重新决定补入
与撤回。生产侧在一次事务中提交完整源状态、裁剪成员、sequence 与不可变包，之后才写文件；
消费侧在一次事务中提交行差分与回执。契约、恢复边界与 8 MB 可消费上限见
[裁剪交付文档](docs/clipping.md)。该管道不计算隐私预算、噪声或反事实影响，也不声称
裁剪本身等于 DP 发布。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

`tests/oracle.py` 是独立重写的穷举 oracle，用于核对规划器在维数 1–3、小候选集上的
可行性、完整 Pareto 前沿、等指标去重、精确方差与顺序扣费；`tests/test_release.py`
覆盖跨重启身份冲突、别名谱系、停用不退款、原报告重试、并发预算竞争与导出失败重试。
`tests/policy_oracle.py` 与 `tests/test_policy_oracle.py` 用独立枚举 oracle 核对策略
编译器的选择器匹配、并列来源、逐 scope 有效额度、重叠聚合违规与报告稳定性。
`tests/influence_oracle.py` 是仅限测试的真实逐 unit 删行 SQL oracle；
`tests/test_influence_*.py` 覆盖影响审计器的关系语义、精确反事实差分、left/anti
缺席条件、distinct 支持、共享计算旁路核实、WAL 快照隔离与随机差分 fuzz。
`tests/clip_oracle.py` 是独立重写的逐批全量裁剪 oracle（不调用生产裁剪函数）；
`tests/test_clip.py` 与 `tests/test_clip_contract.py` 覆盖验收场景、跨分区优先级
更正、换属、零净差分、8 MB 受控拒绝、生产导出失败、消费提交前后故障、跨重启重试、
历史同序异内容、断链、并发与严格路径/字段契约。

## 约束与边界

* 输入 JSON 最多 8 MB，拒绝重复键、非有限数值和未定义字段；源字节 SHA256 随结果保留。
* 输出是独立可读 JSON；成功后原子替换，失败退出 2 并保留旧报告。
* 输入和输出不得相同，输出符号链接拒绝。
* 规划只用当前目录已公开的测量与别名；不生成噪声、不验证 DP 证明、不声称规划本身满足 DP。
* 新增测量按顺序组合（`sum epsilon`、`sum delta`）扣费；复用别名零新增成本，但共享噪声
  的协方差会精确累计，不会被当作独立样本。
* 数据库写操作在 `BEGIN IMMEDIATE` 事务内完成，两个进程竞争预算或与 `retire` 交错时只会
  得到某个串行顺序的完整结果。

## 模块

* `model.py`、`budget.py`、`cli.py`、`common.py`：v1 批处理台账；`cli.py` 同时承载
  只读的 `policy` 离线组织级策略编译子命令（无子命令时旧契约不变）。
* `policy_model.py`：策略文档的严格契约解析、精确/通配选择器匹配、逐 scope 有效额度
  与并列来源追踪、重叠规则聚合核验、规范化报告摘要（无 I/O）。
* `release_model.py`：严格契约解析、精确线性代数、谱系展开、Pareto 枚举（无 I/O）。
* `store.py`：SQLite 目录、限额、原始测量身份、查询别名与已提交报告。
* `release_api.py`：`init`/`plan`/`publish`/`retire`/`catalog` 事务实现。
* `release_cli.py`：`python -m privacy_budget.release_cli` 入口。
* `influence_model.py`：影响查询的严格契约解析、DAG/schema 校验与规范 SHA（无 I/O）。
* `influence_snapshot.py`：只读 SQLite 快照、声明 schema/类型校验与逻辑快照哈希。
* `influence_engine.py`：bag 关系执行、行身份管理与逐 unit 局部反事实差分传播。
* `influence_api.py`：只读审计 API、报告装配与确定性内容摘要。
* `clip_model.py`：裁剪配置/批次/包严格契约、全量重裁剪、净差分与规范摘要（无 I/O）。
* `clip_store.py`：生产/消费两份 SQLite 状态、角色绑定与路径/符号链接防护。
* `clip_api.py`：`init`/`apply`/`export`/`sink-init`/`consume`/`snapshot` 事务实现与
  测试专用故障注入边界。
* `clip_cli.py`：`python -m privacy_budget.clip_cli` 入口。

fixtures 为明确自制的虚构合成样例，代码与样例采用 MIT 许可。
