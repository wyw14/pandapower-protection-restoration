# 转供过程中的保护记忆、自动重合闸与失供电量

F.2 能源系统。本仓库根目录就是一个 Harbor 题包，保留 instruction.md、task.toml、environment/、tests/、solution/ 五件套。
基于真实开源项目的作者应用扩展，不宣称是上游已报告的缺陷或生产测量记录。

## 运行

已使用 Harbor 0.24.0。需要 Linux/amd64 Docker、Landlock ABI >= 3 和允许题包独立 Verifier 以 root 引导隔离进程的环境；候选计算以非特权身份运行。强制 Landlock 文件白名单、seccomp 与受信资产权限隔离。任一必要隔离步骤失败都拒绝运行，不以放宽文件访问替代隔离。当前依赖视图复制方案未能满足准备预算，因此未作为兼容降级方案纳入题包。

```sh
harbor run -p . -a oracle -n 1 -k 1 --job-name oracle-check
harbor run -p . -a nop -n 1 -k 1 --job-name nop-check
harbor run -p . -a claude-code --ak version=2.1.306 --install-only -n 1 -k 1 --job-name agent-install-check
```

第三条只安装 Agent，不调用模型。真实模型运行使用 Harbor 内置 Claude Code harness，通过进程环境提供模型服务配置与凭据；不要把密钥写入仓库或命令示例。
基础镜像按 digest 固定。运行和测试 wheels 随包提供并校验 SHA256；Agent 安装阶段需要联网。题包不预装或分发 Claude Code 二进制，不固定本机 Docker 子网。

题面给出完整数学/物理模型、接口、边界及数值容差；README 不替代题面。Docker 构建只复制运行源码和运行依赖，不复制 tests/solution。独立 Verifier 在候选隔离进程外执行测试。

## 返修与诊断

本次返修将专家工时写为带单位的字符串并补齐测试函数逐字契约引用，保留显式的 `python -B -I -m pytest`、精确测试身份及 setup/call/teardown 全通过门槛。专家工时仍是原480分钟作者估计，尚无人类计时记录。

参考解的灵敏度计算会缩小扰动、保持动作序及动作所属外生时段，并检查步长收敛。独立两母线验收改为闭式事件链的解析切向传播，不使用参考解的差分假设。测试保留原24节点，增加4个合法近终点/负荷边界响应、1个未保护热线路接续供电、13种遗漏的拒收等价族、3种可选天气/负荷字段的省略、1个原生ZIP/温度/TDPF干扰字段和1个负荷变化不缩放sgen/gen的独立净注入检验，共47节点；同时检查拒收后恢复和三层数值网格的收敛。科学输入域和公开容差保持不变。每个候选比较报告所有数值字段的差异，避免单个相角错误遮挡后续物理机制。

原生复杂网络验收使用锁定 pandapower 标准库中的有励磁三绕组变压器和 Dyn5 配电变压器，保留150°相移、正常额定范围的负载及原始顺序为72、5的两个无功受限机组。原零励磁、单机组工况未能区分 T/π 模型和机组排序错误，已由实际候选变异确认后补齐；公开容差未改变。新合法相移工况还揭示参考解和数值验收共享平坦初值时可能收敛到低电压根，因此两者冷启动采用相角初始化，参考解仍保留结果热启动。这是实测反例驱动的修复，不声称有限测试证明全部工况上的全局选根性质。

`test.sh` 在依赖安装前安全原子写入 reward=0，表示安装、收集、异常和超时均不能获得成功；全部受信测试通过并完成日志保全后才原子写入 1。内部更新使用0600，最终由受信shell封存为0444；重复执行时仅受信所有者解除旧只读位并替换。安装阶段总预算15秒，科学测试仍为270秒，shell总截止295秒并处理HUP/INT/TERM、跟踪与清理子进程组。不能依据初始化命令推断 Oracle 的最终分数。验收过程禁用外部 pytest 插件自动加载，并使用固定的受信账本插件。

审核失败时请同时保留 trial 级 `result.json`、`verifier/install.log`、`verifier/test-stdout.txt`、`verifier/environment.json`、`verifier/pytest.log`、`verifier/junit.xml`、`verifier/outcomes.json` 与 `verifier/grading.json`。`environment.json` 记录 Python、内核、引导用户、实际文件视图，以及读取受信资产、写评分、在私有区外写入和创建网络套接字的访问预检。宿主不支持必要隔离时报告环境不兼容，不能据此判断参考算法错误。

候选异常跨进程传输按真实内置异常继承族分类，保留自定义子类的 ValueError、TypeError、RuntimeError 和 NotImplementedError 语义；未知异常使用独立错误类型，不能冒充声明的异常族。此修复不改变科学节点、输入域、公开容差和全项通过门槛。
多继承异常保留所有受支持的真实父族，受信端仅按固定白名单重建本地异常类型；畸形成员掩码明确拒绝，不加载候选异常类。

## 历史评测边界

本次返修基于公开提交 `e839332716964baeec70bf18128eb5be04cae680`。该版本的官方 Oracle 为24/24、NOP为0/24；一次真实模型运行10/24，Agent超时，14项失败共享绝对参考相角首断言，不能据此认为14个独立领域难点失败，也不符合要求正常结束的难度校准。新增合法近边界反例证明旧Oracle满分不等于参考实现覆盖完整支持域。

下列更早结果来自 `d04a639ae268fe33cc4983f6b9e780c57c07e602` 一系的旧冻结环境；返修后的 Oracle、NOP、故障场景和重复运行证据应绑定新提交另行交付。
旧冻结环境的官方 Oracle 为 24/24、NOP 为 0/24；NOP 每项在调用阶段失败，setup/teardown 正常。
旧冻结环境的一次 DeepSeek 运行通过 8/24，reward=0，Agent 耗尽3600秒，Verifier完整完成。API返回模型别名 deepseek-flash，harness为Claude Code2.1.306，effort=max。
这些是旧环境的真实历史结果，不冒称发布commit已重新调用过模型；发布环境另行执行Oracle/NOP及保存产物重放，证据随交付登记提供。
历史失败集中在绝对参考相角及接收端电流两个问题。非满分不能解释为16个独立领域难点失败。
提交状态为待审核；本仓库不宣称完整64条内部初审通过。参考解由作者出题过程编写，不是独立盲测。

## 来源

pandapower 3.2.1，https://github.com/e2nIEE/pandapower/tree/v3.2.1 。版权文本保留在 environment/src/PANDAPOWER_LICENSE.txt。
第三方 wheel 保留各自内置许可及原始字节。许可证、研究署名和第三方归属不因本次题目包装而改变。
