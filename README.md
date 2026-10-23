# pandapower 保护恢复轨迹

基于 pandapower 3.2.1 的应用扩展，研究负荷与天气变化下的线路温度、保护动作、供电恢复和失供电量。测试工况按题面模型构造。完整接口、物理定义和误差要求见 instruction.md。

## 运行

使用 Harbor 0.24.0 和 Linux/amd64 Docker。运行依赖及测试依赖随包离线提供，基础镜像按摘要固定。

```sh
harbor run -p . -a oracle
harbor run -p . -a nop
```

模型通过 Harbor 内置 Agent 接入，服务配置与凭据由外部提供。

独立 Verifier 以 root 启动，候选以非特权身份计算。运行环境需要 seccomp；文件访问使用 Landlock，可用性不足时使用依赖专用 chroot，后者需要 SYS_CHROOT、MKNOD、SETUID、SETGID 能力。测试和参考解不进入初始应用镜像。每个候选进程使用私有临时目录，同一进程可连续处理请求。

## 来源

上游：[pandapower v3.2.1](https://github.com/e2nIEE/pandapower/tree/v3.2.1)。许可证保留于 environment/src/PANDAPOWER_LICENSE.txt；第三方依赖保留各自许可。
