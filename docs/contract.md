# Joint release planning v2

差分隐私预算台账的 v1（`privacy-budget` 批处理，见文末）保持不变。v2 新增
`python -m privacy_budget.release_cli`，把一次性预算核验扩展为**可复用带噪统计的联合发布
规划器**：在同一次快照内为多个目标查询挑选线性无关的测量基底，精确重构每个目标，沿历史
别名展开有符号原始噪声谱系，按顺序组合扣费，并给出预算—误差 Pareto 前沿。

全部计算使用 `fractions.Fraction` 精确有理数。本工具**不生成噪声、不验证 DP 证明、不做
安全存储**；上游声明的测量向量、估计值、噪声方差与 epsilon/delta 成本均为可信输入。规划
不证明算法本身满足 DP，也不覆盖 advanced composition、用户级并行组合或跨 dataset 隐私
单元识别。

## 命令

```sh
python -m privacy_budget.release_cli init     --db DB --source LIMITS.json
python -m privacy_budget.release_cli plan     --db DB --source REQUEST.json [--output PLAN.json]
python -m privacy_budget.release_cli publish  --db DB --source REQUEST.json --choice CHOICE_ID --output REPORT.json
python -m privacy_budget.release_cli retire   --db DB --dataset D --principal P --snapshot S --measurement M
python -m privacy_budget.release_cli catalog  --db DB --dataset D --principal P --snapshot S
```

`plan`、`publish`、`catalog` 省略 `--output` 时写 stdout。`plan` 与 `catalog` 只读打开
数据库，绝不创建文件或改变状态。退出码：成功 `0`；输入非法、无可行解、陈旧选择、冲突或
交付失败 `2`，且失败不会改变目录、预算或旧输出。

## 预算与限额（init）

`LIMITS.json` 形如 `{"limits":[{"dataset","principal","epsilon","delta"}, ...]}`。
`limits` 唯一 `(dataset, principal)`，`epsilon >= 0`、`delta >= 0`；预算按
`(dataset, principal)` 跨 snapshot 累计。初始化写 `init_marker`，之后再次 `init` 只能与
既有限额逐字节一致，否则拒绝，限额不可修改。

## 请求（REQUEST.json）

严格 JSON：最多 8 MB、拒绝重复键、拒绝 NaN/Infinity、拒绝任何未定义字段。

```json
{
  "release_id": "release-1",
  "dataset": "d",
  "principal": "p",
  "snapshot": "s",
  "dimensions": ["x1", "x2"],
  "fresh": [
    {"id": "F", "coefficients": [{"numerator":1,"denominator":1},{"numerator":1,"denominator":1}],
     "value": {"numerator":0,"denominator":1}, "variance": {"numerator":1,"denominator":1},
     "epsilon": {"numerator":1,"denominator":2}, "delta": {"numerator":0,"denominator":1}}
  ],
  "reuse": [{"release_id":"ab","query_id":"A"}],
  "queries": [{"id":"S","coefficients":[{"numerator":2,"denominator":1},{"numerator":1,"denominator":1}]}],
  "checks": [{"id":"k","weights":{"S":{"numerator":1,"denominator":1}},
              "max_variance":{"numerator":6,"denominator":1}}]
}
```

* `dimensions`：1–6 个唯一名称的有序坐标域；维度顺序本身是契约的一部分。
* `fresh`：1 至多个新测量，`coefficients` 长度等于坐标数，`variance >= 0`，
  `epsilon > 0`，`delta ∈ [0,1)`。
* `reuse`：已发布查询别名 `{release_id, query_id}`。
* `queries`：1–8 个目标，`coefficients` 长度等于坐标数。
* `checks`：1–16 个误差检查，`weights` 为查询 ID→有理数，缺项视为 0，`max_variance >= 0`。
* `fresh` 与 `reuse` 合计至多 16 项，各自 ID 唯一；`queries`、`checks` 的 ID 也须唯一。
* 数值为 `{numerator, denominator}`：分子绝对值 ≤ 10^12（可为负），分母 1–10^12；
  `variance`、`max_variance` 非负。
* 标识符 ≤128 字符、非空白、不含 `:` 或 `/`（候选键用这两种字符编码）。

以下情况一律拒绝：重复 ID、引用未定义查询权重、引用未知/异快照别名、复用原始 ID 时声明
与目录不一致、同 scope/snapshot 已经发布过不同坐标域或维度数。

## 规划语义

候选键为 `fresh:<id>` 或 `reuse:<release_id>/<query_id>`。规划在所有**线性无关候选子集**
中选择基底（包含空基底，空基底只能服务全零查询），不把候选线性相关当错误，也**不**把
冗余测量额外加入基底来降噪——那不在本题选择空间内，也不声称最优。因而已公开测量若在
本次请求中未被选中，不会进入新的发布、不会扣费。

1. 对每个目标查询用唯一解 `w` 精确重构：`target = Σ w_k · basis_k`；若目标不在基底的张成
   空间，该基底不可行。基底线性无关保证表示唯一。
2. 把 `w` 沿每个候选的谱系展开到原始测量，`merge_lineage` 合并同 ID 的有符号系数并删除
   精确零项。别名即使经过多次后处理也沿用原噪声，绝不被当作新的独立样本。
3. 检查合并后谱系：若含已停用原始测量的**非零**系数则不可行。判定基于代数消去后的谱系，
   仅"可达停用祖先"并不足以排除（例如 `B−A` 消去 `n1` 后仍可用）。
4. 只在本次首次公开的选中原始 ID 上扣费，每个 ID 一次；`epsilon`、`delta` 分别顺序求和，
   与既往扣费一起不得超过 scope 限额。已公开测量、reuse 别名零新增成本。
5. 联合协方差 `C_ij = Σ_raw coeff_i(raw) · coeff_j(raw) · variance(raw)`；每条 check 计算
   `wᵀ C w`，若超过 `max_variance` 则该基底不可行。
6. 查询估计用同一组权重：`Σ w_k · value_k`（fresh 用声明值，reuse 用别名值）。

规划**不会**先贪心复用再检查误差，也不会把缓存估计当独立噪声。

## Pareto 前沿

每条合法基底对应一个指标向量 `(新增 epsilon, 新增 delta, 按 check ID 排序的各 check 实际
方差)`，固定维度、均越小越好。支配定义为每项不大于且至少一项严格小于。全部指标完全相等
时只保留按 `(新原始 ID 数, 基底大小, 排序候选键列表)` 字典序最小者。前沿按指标元组与候选
键排序输出，不会擅自把低预算/高误差与高预算/低误差压成单一推荐。无可行基底时明确报错。

`plan` 返回每个 choice 的候选键、重构权重、有效谱系、协方差、成本、check 实际方差与
`feasible` 标记；**不返回任何估计值**，也不改变状态。

### 规范身份与 choice_id

请求按以下方式规范化：所有分数转为约分后的 `str(Fraction)` 字符串；`fresh`/`queries`/
`checks` 按 ID 排序，`reuse` 按 `(release_id, query_id)` 排序；`dimensions` 保持原序。
随后用键排序、不转义非 ASCII、无空白的 UTF-8 JSON 计算 `request_sha256`。
`choice_id = SHA256(canonical({request_sha256, basis: 排序候选键列表}))`。

## 发布（publish）

`publish` 在 `BEGIN IMMEDIATE` 事务内基于**提交时**的目录与剩余预算重算前沿：

* 若 `release_id` 已存在且请求与 `choice_id` 完全一致，直接返回已存报告、不扣费、不重新
  评估当前前沿或停用状态（相同重试语义）。
* 若已存在但请求或 choice 不同，拒绝。
* 否则只提交指定 choice。它若已失去可行性、被支配或因等指标去重而消失，拒绝陈旧选择，
  绝不擅自换方案。

新增原始身份、扣费、完整报告与新查询别名在同一事务内提交；两个独立进程竞争最后一份预算
或与 `retire` 交错时，只会得到某个串行顺序的完整结果。**先持久提交，再交付**：报告写入
数据库后才向 stdout/输出文件写出；交付失败允许非零退出，但相同重试会取回已提交报告且不再
扣费。

报告包含请求/choice 身份、选中基底、精确权重、每个查询的估计/有效谱系、协方差、check
结果、本次成本与提交后的累计预算，全部分数约分输出。

## 停用与目录

* `retire` 永久停用某原始测量，不退款、不改旧报告；幂等。
* `catalog` 只返回已公开查询的估计、向量、有效谱系及祖先停用状态，以及已公开原始测量的
  声明（含 `retired` 标记），绝不暴露未被选中的 fresh，也不去重估任何未公开测量。

## 并发、幂等与安全

* 输入/输出不得同址；输出符号链接拒绝。
* 未选中 fresh 不入目录、不扣费、不出现在 catalog 或任何报告估计里。
* 规划只用当前目录里已公开的测量与别名。

## 与 v1 的关系

`python -m privacy_budget.cli`（v1 批处理）与其 `Fraction` 顺序组合语义保持不变：
`sum epsilon`、`sum delta`，任一 scope 超额则整批原子拒绝。v2 的 `init` 沿用 v1 的限额行
格式，但新增 `plan`/`publish`/`retire`/`catalog` 的持久目录与联合规划能力。

## 与离线策略编译器（policy）的关系

`privacy-budget policy`（见 [policy.md](policy.md)）是 v1/v2 之外的**只读离线**组件：
它只编译 scope 目录与组织级上限规则，输出逐 scope 有效额度与聚合检查报告，不创建发布、
测量或预留记录，也不修改本文档描述的一次性核验与联合发布行为。两者的治理分工不同：
policy 负责把组织规则解析为每个 scope 的有效额度并核验跨 scope 的重叠聚合上限；v1/v2
则在各自的一次性核验或持久目录中按既定额度顺序组合扣费。

## 验收样例（联动）

单位方差、噪声独立的 `n1`、`n2`，先发布 `A = x1 + n1`、`B = x1 + x2 + n1 + n2`：

* 请求 `A`、`B` 之和（向量 `x1 + x2` 组合）时 `Var = 5` 而非 `3`——共享 `n1`。
* 误差上限 4 时缓存基底（新增 epsilon 0、方差 5）不进入前沿。
* 同时提供 fresh `F = x1 + x2 + nF`（`nF` 独立、方差 1、epsilon 1/2、delta 0）且预算充足、
  上限 6 时，缓存基底 `(0, 5)` 与 `A/F` 基底 `(1/2, 2)` 必须**同时**在前沿，并可分别显式
  选择发布。
* 先发布 `B−A`（谱系仅 `n2`）再停用 `n1`：该别名仍能以零新增成本服务 `x2`；单独服务
  `x1` 则因未消去的 `n1` 被拒。

## 缓存复用与新增独立测量

* **缓存复用**：隐私成本为零，因为噪声已在过去花费；代价是联合误差必须按共享原始噪声
  精确累计——相关项的协方差不会被"免费"抹平。适用于已发布统计量恰好张成目标的情形。
* **新增独立测量**：花费新的 epsilon/delta，但引入与历史噪声独立的方差，可降低联合误差
  或覆盖历史统计量无法张成的方向。
* 两者**不可互换**：同一 `release_id/query_id` 别名永远携带其原始噪声身份；重复引用同一
  别名不会产生独立样本。规划在前沿中同时保留两类方案，由调用方显式选择。
