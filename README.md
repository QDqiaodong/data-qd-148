# data-qd-148 · 差分隐私预算台账

Compositional epsilon-delta budget accounting with atomic reservation checks。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/privacy-budget --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/privacy_budget/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## Scope and sample
# Basic approximate-DP budget composition v1

隐私治理人员核验同一dataset/principal预算范围内的机制预留。limits唯一(dataset,principal)，声明最大epsilon>=0、delta>=0有理数；reservations最多20000个唯一id，绑定已声明范围，epsilon>0、delta∈[0,1)。每值是整数分子/正整数分母(<=10^12)，精确Fraction。
采用基础顺序组合定理：总epsilon=sum epsilon_i，总delta=sum delta_i；任一范围超过预算，则整份批次原子拒绝，不部分接纳。通过时输出每项依据、组合总量、限额和余量。未发生预留的预算也输出零消耗。
这是纯会计台账，不实现差分隐私机制、随机噪声、安全存储、用户级并行组合、advanced composition或跨dataset隐私单元识别；同一principal/dataset组内假定机制确实作用于同一受保护数据范围。预算满足不证明算法本身DP或没有其他隐私泄露。
样例epsilon总计3/2、delta总计1/5000，未超epsilon2/delta1/1000限额。超额时失败且不会输出可误认为部分成功的报告。


## Scope and sample
# Basic approximate-DP budget composition v1

隐私治理人员核验同一dataset/principal预算范围内的机制预留。limits唯一(dataset,principal)，声明最大epsilon>=0、delta>=0有理数；reservations最多20000个唯一id，绑定已声明范围，epsilon>0、delta∈[0,1)。每值是整数分子/正整数分母(<=10^12)，精确Fraction。
采用基础顺序组合定理：总epsilon=sum epsilon_i，总delta=sum delta_i；任一范围超过预算，则整份批次原子拒绝，不部分接纳。通过时输出每项依据、组合总量、限额和余量。未发生预留的预算也输出零消耗。
这是纯会计台账，不实现差分隐私机制、随机噪声、安全存储、用户级并行组合、advanced composition或跨dataset隐私单元识别；同一principal/dataset组内假定机制确实作用于同一受保护数据范围。预算满足不证明算法本身DP或没有其他隐私泄露。
样例epsilon总计3/2、delta总计1/5000，未超epsilon2/delta1/1000限额。超额时失败且不会输出可误认为部分成功的报告。


## Scope and sample
# Basic approximate-DP budget composition v1

隐私治理人员核验同一dataset/principal预算范围内的机制预留。limits唯一(dataset,principal)，声明最大epsilon>=0、delta>=0有理数；reservations最多20000个唯一id，绑定已声明范围，epsilon>0、delta∈[0,1)。每值是整数分子/正整数分母(<=10^12)，精确Fraction。
采用基础顺序组合定理：总epsilon=sum epsilon_i，总delta=sum delta_i；任一范围超过预算，则整份批次原子拒绝，不部分接纳。通过时输出每项依据、组合总量、限额和余量。未发生预留的预算也输出零消耗。
这是纯会计台账，不实现差分隐私机制、随机噪声、安全存储、用户级并行组合、advanced composition或跨dataset隐私单元识别；同一principal/dataset组内假定机制确实作用于同一受保护数据范围。预算满足不证明算法本身DP或没有其他隐私泄露。
样例epsilon总计3/2、delta总计1/5000，未超epsilon2/delta1/1000限额。超额时失败且不会输出可误认为部分成功的报告。

