# 离线组织级策略编译器（policy）

`privacy-budget policy` 是一个**纯离线、只读**的策略编译器：输入有限的
dataset/principal 目录、每个 scope 已配置的 epsilon/delta 额度，以及带精确或
`"*"` 通配选择器的组织级上限规则，输出逐 scope 的有效额度与可审计的策略检查
报告。它不创建发布、测量或预留记录，不访问 SQLite，也不改变 v1 一次性核验与
v2 联合发布规划的任何行为。

全部数值使用 `fractions.Fraction` 精确有理数，输入输出均为严格 JSON（拒绝重复
键、NaN/Infinity 与未定义字段）。

## 命令

```sh
privacy-budget policy --source POLICY.json --output REPORT.json
# 等价：
python -m privacy_budget.cli policy --source POLICY.json --output REPORT.json
```

不带 `policy` 子命令的旧用法 `privacy-budget --source ... --output ...` 契约
完全不变。退出码：成功 `0`；输入非法或聚合违规 `2`，且失败时不触碰既有输出
文件。输入与输出必须异址，输出路径是符号链接时直接拒绝。

## 输入契约（POLICY.json）

```json
{
  "scopes": [
    {
      "dataset": "dataset-a",
      "principal": "analyst-a",
      "epsilon": {"numerator": 4, "denominator": 1},
      "delta": {"numerator": 1, "denominator": 20}
    }
  ],
  "rules": [
    {
      "id": "org-wildcard",
      "dataset_selector": "*",
      "principal_selector": "*",
      "epsilon": {"numerator": 5, "denominator": 1},
      "delta": {"numerator": 1, "denominator": 10}
    }
  ]
}
```

* 顶层恰好包含 `scopes`、`rules` 两个字段；`scopes` 至少 1 项、至多 1000 项，
  `rules` 至多 1000 项。
* scope 键 `(dataset, principal)` 必须唯一；`epsilon`、`delta` 均为
  `{numerator, denominator}` 有理数且**非负**（允许 0），分子 ≤ 10^12、
  分母 ∈ [1, 10^12]。
* 每条规则有唯一 `id`、`dataset_selector`、`principal_selector`、`epsilon`、
  `delta`。选择器只允许：精确标识符（该名称必须出现在 scope 目录中）或 `"*"`。
* 规则的 epsilon/delta 同样是非负有界有理数。

以下情况一律拒绝（退出 2，不写输出）：重复 JSON 键、重复 scope 键、重复规则
ID、缺失或非法数值（负数、零分母、超界整数、NaN/Infinity）、引用目录中不存在
的 dataset/principal 名称、以及**选择器组合不命中任何 scope 的规则**（精确名称
各自存在但从未在同一个 scope 中共现，也算未匹配规则）。

## 两类治理义务

编译器把两个**相互独立、缺一不可**的义务分开承担：

### 1. 逐 scope 截断（per-scope truncation）

对每个 scope，有效额度是其配置额度与**所有命中规则**额度的逐项最小值：

```
effective_epsilon(s) = min(configured_epsilon(s), ∪{rule.epsilon : rule 命中 s})
effective_delta(s)   = min(configured_delta(s),   ∪{rule.delta   : rule 命中 s})
```

* 通配规则（任一选择器为 `"*"`）对每个命中 scope 始终是**硬上限**；精确规则
  只能约束被点名的单个 scope，**不能覆盖或放宽**通配上限——最小值语义本身保证
  了这一点，因为加入一个更大的候选不会改变最小值。
* 报告为每个 scope 给出 `configured`、`effective`，以及分别达到 epsilon/delta
  最小值的**全部**规则 ID（`epsilon_sources`、`delta_sources`）。同值并列时
  所有并列规则都保留，不会只留一个“赢家”；若没有任何命中规则在该坐标上达到
  最小值（即配置额度自身最小），来源记为 `"configured"`。
* `matching_rule_ids` 列出命中该 scope 的全部规则，包括额度未达到最小值、但
  仍然构成硬上限的通配规则。

### 2. 重叠聚合约束（overlap aggregation）

每条规则对**所有命中 scope 的有效额度之和**分别施加 epsilon 与 delta 上限：

```
Σ effective_epsilon(s) ≤ rule.epsilon   （s 遍历 rule 命中的 scope）
Σ effective_delta(s)   ≤ rule.delta
```

聚合使用的是**有效额度**而非配置额度。每个 scope 单独不超限并不能推出聚合
通过：两条重叠规则即使各自都能被满足，其交集上的额度分配也可能让其中一条
聚合超额。此时编译器**不会静默裁剪任何额度，也不会选择性忽略规则**，而是在
报告中给出确定的违规规则、实际总和、上限、超额量（epsilon 与 delta 分别
计算）以及贡献该总和的全部 scope 明细，进程以退出码 2 失败。

## 报告结构（REPORT.json）

| 字段 | 含义 |
| --- | --- |
| `schema` | 固定 `privacy-budget-policy-report-v1` |
| `source_sha256` | 输入源字节的 SHA256（沿用严格读取器） |
| `report_sha256` | 除该字段外报告主体规范化 JSON（键排序、无空白、UTF-8）的 SHA256 |
| `status` | `"ok"` 或 `"violation"` |
| `scopes[]` | 每 scope：`configured`、`effective`、`epsilon_sources`、`delta_sources`、`matching_rule_ids`，按 `(dataset, principal)` 排序 |
| `rule_checks[]` | 每规则：选择器、命中 scope 列表与计数、epsilon/delta 的实际上限、总和与超额量，按规则 ID 排序 |
| `unmatched_rule_ids` | 恒为空数组；未匹配规则在编译期即被拒绝 |
| `violations[]` | 每条聚合违规的规则 ID、上限、总和、分别的超额量与命中 scope 明细 |

所有分数以约分后的 `str(Fraction)` 输出；所有列表都有确定排序。输入数组顺序
与 JSON 对象键顺序不影响规范化结果，因此也不影响 `report_sha256`。

## 联动验收样例

通配规则 epsilon 上限 5、delta 上限 1/10，命中 scope A、B：

1. A 配置 `(4, 1/20)`、B 配置 `(3, 1/20)`。两个 scope 的每项都低于 5，但
   epsilon 合计 `4 + 3 = 7 > 5`，编译器拒绝并在违规明细中同时指出 A、B
   （超额量 2）；delta 合计 `1/20 + 1/20 = 1/10` 恰好达限，不超额。
2. 把 B 的 epsilon 配置降为 1 后，合计 `4 + 1 = 5`，delta 仍为 `1/10`，
   策略通过。
3. 再加入与通配同值的精确规则（A 的 epsilon 4、delta 1/20），A 的有效额度
   来源同时包含该精确规则；当通配规则本身也取到相同最小值时，通配与精确
   规则的 ID 会**并列**出现在来源列表中。

样例文件：`fixtures/policy-violation.json`（拒绝）、`fixtures/policy-pass.json`
（通过）、`fixtures/policy-tie.json`（同值并列来源）。

## 可测试 API

`privacy_budget.policy_model` 不做任何 I/O：

* `parse_policy(data) -> (scopes, rules)`：严格契约解析，失败抛 `PolicyError`。
* `compile_policy(scopes, rules, source_sha256) -> report`：纯函数式编译，
  未匹配规则抛 `PolicyError`，聚合违规体现在报告的 `status`/`violations` 中。
* `matches(rule, scope)`、`report_digest(body)`、`violation_message(body)`
  可供单独测试。

`tests/policy_oracle.py` 是独立重写的小型枚举 oracle（不导入被测包），
`tests/test_policy_oracle.py` 穷举通配/精确选择器组合、两条重叠规则的完整
上限网格、并列来源、未匹配规则、epsilon/delta 分别超限、报告稳定性与失败
保留旧文件，并逐项核对 oracle 与编译器输出。
