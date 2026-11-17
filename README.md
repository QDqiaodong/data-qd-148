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

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

`tests/oracle.py` 是独立重写的穷举 oracle，用于核对规划器在维数 1–3、小候选集上的
可行性、完整 Pareto 前沿、等指标去重、精确方差与顺序扣费；`tests/test_release.py`
覆盖跨重启身份冲突、别名谱系、停用不退款、原报告重试、并发预算竞争与导出失败重试。
`tests/policy_oracle.py` 与 `tests/test_policy_oracle.py` 用独立枚举 oracle 核对策略
编译器的选择器匹配、并列来源、逐 scope 有效额度、重叠聚合违规与报告稳定性。

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

fixtures 为明确自制的虚构合成样例，代码与样例采用 MIT 许可。
