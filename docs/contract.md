# Basic approximate-DP budget composition v1

隐私治理人员核验同一dataset/principal预算范围内的机制预留。limits唯一(dataset,principal)，声明最大epsilon>=0、delta>=0有理数；reservations最多20000个唯一id，绑定已声明范围，epsilon>0、delta∈[0,1)。每值是整数分子/正整数分母(<=10^12)，精确Fraction。
采用基础顺序组合定理：总epsilon=sum epsilon_i，总delta=sum delta_i；任一范围超过预算，则整份批次原子拒绝，不部分接纳。通过时输出每项依据、组合总量、限额和余量。未发生预留的预算也输出零消耗。
这是纯会计台账，不实现差分隐私机制、随机噪声、安全存储、用户级并行组合、advanced composition或跨dataset隐私单元识别；同一principal/dataset组内假定机制确实作用于同一受保护数据范围。预算满足不证明算法本身DP或没有其他隐私泄露。
样例epsilon总计3/2、delta总计1/5000，未超epsilon2/delta1/1000限额。超额时失败且不会输出可误认为部分成功的报告。
