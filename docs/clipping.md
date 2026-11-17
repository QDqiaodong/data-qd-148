# Clipped microdata delivery

`python -m privacy_budget.clip_cli` 维护一条**可恢复的裁剪微数据交付管道**：生产库保存
完整源状态、当前裁剪行集、连续 sequence 游标与不可变交付包；独立消费库只接收已冻结的
包，并维护裁剪行集与已应用回执。

本组件**只做确定性的裁剪选择与交付**。它不计算隐私预算、不生成噪声、不评估反事实影响，
也不声明裁剪本身等于一次差分隐私发布。包摘要保证内容一致性，不提供恶意生产者身份认证。

## 配置

```json
{"stream_id": "demo-stream", "max_partitions": 1, "max_rows": 1}
```

字段集必须恰为 `{stream_id, max_partitions, max_rows}`。`max_partitions ∈ [1,100]`、
`max_rows ∈ [1,1000]`，必须是真正的整数（`true` 不算）；`stream_id` 沿用
`common.ident` 的非空标识符规则。初始化后配置不可变：同配置 `init`/`sink-init` 重试是
幂等空操作（`created: false`），异配置拒绝；已存在的发布目录库、无关库或符号链接路径
一律拒绝。

## 源批次

```json
{"stream_id":"demo-stream","sequence":1,"changes":[
  {"id":"a","before":null,
   "after":{"id":"a","unit":"U","partition":"p","priority":1,"value":10}}
]}
```

* 源行字段恰为 `{id, unit, partition, priority, value}`；`id`/`unit`/`partition` 是非空
  标识符，`priority`、`value` 是 `[-10^12, 10^12]` 的整数。同一 stream 内 `id` 唯一。
* `sequence` 是 `[1, 2^63-1]` 的整数并从 1 连续递增；`changes` 允许为空数组，顺序不影响
  规范批次。
* 每项 `{id, before, after}` 至少一端非 null，非 null 行的 `id` 必须等于外层 `id`，同批
  `id` 不可重复。
* `before` 必须与批前源行精确一致：新增要求不存在，更正/删除要求存在。同批变更同时
  生效；任何一项冲突则整批不生效。所有 `before` 先全部核对，再产生批后状态。

## 裁剪规则

对每个 unit：

1. 以各非空 partition 内最小 `(priority, id)` 作为该 partition 的顺序键，再以 partition
   名打破并列；按此键保留前 `max_partitions` 个 partition。
2. 每个获选 partition 内再按 `(priority, id)` 保留前 `max_rows` 行，导出**完整源行**。

分区排序基于该 unit **当前全部源行**，而不是上次获选行；`value` 不参与排名，priority
并列时 `id` 与 partition 名按 Unicode 码点排序。因此未获选行改变分区的首键、换属、换
分区、删除等都会触发全量重排，而不是"只拒绝超额新行"。

## 交付包

```json
{"stream_id":"demo-stream","config_sha256":"...","sequence":1,
 "previous_packet_sha256":"0000...","before_rows_sha256":"...",
 "after_rows_sha256":"...","batch_sha256":"...","changes":[
   {"id":"a","before":null,"after":{"id":"a","unit":"U","partition":"p","priority":1,"value":10}}
 ],"packet_sha256":"..."}
```

* 字段集恰为上述 9 项；`changes` 按 `id` 排序，元素为 `{id, before, after}`，两端相等者
  不出现。包只含批前后裁剪行集的**净差分**，不交付未获选行，也不含完整源批内容。
* `config_sha256` 绑定整个配置对象；`batch_sha256` 绑定按 `id` 排序后的整个批次对象；
  行集摘要绑定按 `id` 排序的完整源行数组；`packet_sha256` 是排除自身字段后的摘要。
* 规范化：键排序、无空白、不转义非 ASCII 的 UTF-8 JSON；空行集按 `[]`；整数与字符串保留
  原值；摘要为 64 位小写十六进制。第一包 `previous_packet_sha256` 为 64 个 `0`。
* 即使净差分为空，也必须提交并交付该 sequence。

源批次摘要绑定输入，但未获选源行与完整源批内容绝不出现在交付包或快照中。

## 恢复与提交边界

**生产侧提交边界**：一次 `BEGIN IMMEDIATE` 事务内共同提交完整源状态、裁剪成员、
`sequence` 与不可变交付包。随后才向文件交付。因此：

* 提交成功但文件交付失败时，进程非零退出；包已冻结，可跨重启 `export --sequence N`
  取回原包，不回滚已提交批，也不重新裁剪历史。
* 相同 `sequence` 绑定规范批次：完全相同的重试返回原包，且**不重新检查旧 `before`**；
  异内容、缺号或跨 `stream_id` 拒绝。生产库可继续前进，历史包不可重写。

**消费侧提交边界**：应用前核对配置 SHA、链顺序、包自身摘要与批前行集摘要，逐项核对
`before`，再在一次事务内共同提交全部行差分、批后摘要与回执。回执至少包含
`stream_id`、`sequence`、`packet_sha256`、`after_rows_sha256`。

* 相同 sequence 且相同包 SHA 的重试**先返回原回执**，即使消费库已前进。
* 同序异包、断链、错误 `before` 或批后摘要不符，整包拒绝，状态与旧输出不变。
* 消费库初始 sequence `0`、空行集、最后包 SHA 为 64 个 `0`；不读取生产库、不重新裁剪、
  不借助包外数据补行。

**可消费性上限**：若计算出的规范包 UTF-8 字节数超过 8,000,000，`apply` 在提交前受控
拒绝（退出 2），源状态不前进；绝不提交无法被 `consume` 在 8 MB 限制内读取的包，也不
拆成多个 sequence 或静默裁掉替补。数据库累计大小不受单文件 8 MB 限制。

测试用故障注入边界可通过 `privacy_budget.clip_api.set_fault_hook` 安装，触发点：
`producer_prepare`（提交前）、`producer_committed`（提交后、文件导出前）、
`consumer_applying`/`consumer_precommit`（行应用后、提交前）、`consumer_committed`
（提交后、回执交付前）。恢复必须从已提交状态验证，不能依赖进程自报状态。

## 命令

```sh
python -m privacy_budget.clip_cli init      --db PROD --source CONFIG.json
python -m privacy_budget.clip_cli apply     --db PROD --source BATCH.json --output PACKET.json
python -m privacy_budget.clip_cli export    --db PROD --sequence N --output PACKET.json
python -m privacy_budget.clip_cli sink-init --db SINK --source CONFIG.json
python -m privacy_budget.clip_cli consume   --db SINK --source PACKET.json
python -m privacy_budget.clip_cli snapshot  --db DB --role producer|consumer --output SNAPSHOT.json
```

`init`/`sink-init`/`consume` 可省略 `--output` 写 stdout。`snapshot` 输出
`{stream_id, config_sha256, sequence, rows, rows_sha256}`，consumer 另含
`last_packet_sha256`；两者都不含未获选行。`export` 只返回已提交 sequence；`consume` 的
输出文件是回执。退出码：成功 `0`；非法输入、冲突、缺号、验包失败或交付失败 `2`。

输入配置/批次/包均受 8 MB 限制，严格 JSON、拒绝重复键与未知字段。数据库、输入与输出
（含父目录）的符号链接一律拒绝，各路径按真实路径比较且必须互不重合。

## 合成样例

`fixtures/clip-config.json`、`fixtures/clip-batch-1.json`、`fixtures/clip-batch-2.json`
演示验收场景：`U` 的 `p` 分区有 `a(priority=1)`、`b(priority=2)`，`q` 分区有 `c(priority=3)`，
初始只有 `a` 获选；第二批判 `a` 改归 `V` 并删除 `b` 后，`U` 补入 `c`，`V` 保留 `a`，
包只含 `a` 的更正与 `c` 的新增。

## 测试旁路

`privacy_budget.clip_api.set_fault_hook(fn)` 是进程内测试边界（CLI 不暴露）。回调收到触发点
名称；在提交后触发点抛出异常即模拟"已提交但未交付"，恢复须通过 `export`/`consume` 重试
从数据库状态验证：

| 触发点 | 位置 |
| --- | --- |
| `producer_prepare` | 生产事务内、已算出包之后、写行之前 |
| `producer_committed` | 生产提交之后、文件导出之前 |
| `consumer_applying` | 校验完成、行差分写入之前 |
| `consumer_precommit` | 行差分写入之后、提交之前 |
| `consumer_committed` | 消费提交之后、回执交付之前 |
