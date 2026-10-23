# 转供过程中的保护记忆、自动重合闸与失供电量

电网研究组需要评估短时转供期间的连续跳闸、供电恢复和失供电量。请扩展现有 pandapower 应用，计算负荷和天气变化下的电热轨迹，支持带记忆的保护动作、一次自动重合及再次跳闸锁定，输出状态、事件和保护参数变化的响应。

完成 /app/src/thermal_trajectory.py 中的 analyze(request)。candidate.py 提供同名函数及 JSON 标准输入输出入口；network_io.py 读取网络；ac_equilibrium.py 提供可用的静态潮流调用。可以调整应用模块，使用 pandapower 3.2.1、NumPy、SciPy 的公开功能，算法不限。

## 电气与热模型

每个温度和开关状态下采用稳定高电压分支的平衡三相 AC 潮流。使用线路 π 型、变压器 T 型等值、恒功率负荷和绝对相角，并约束非参考机组的无功上下限；不运行控制器、不启用 ZIP、TDPF 或其他温度修正。忽略 load 的恒阻抗/恒电流百分比及 line 的 temperature/tdpf 字段。设备 ID、并联数、母联开关、两/三绕组变压器保留原生含义。每个供电分区由 ext_grid 提供参考；失供岛不补假电源，无定义的母线结果为 null。

thermal 线路具有每相、每回路、全线均匀温度 T。原始 r_ohm_per_km 为 20 °C 电阻，电抗、电容固定。每米、每相、每回路满足：

    r(T) = r20 * (1 + alpha_per_k * (T - 20))             [Ω/km]
    C*dT/dt = I_series²*r(T)/1000 + solar - h*(T-ambient)
              - emissivity*sigma*pi*diameter*((T+273.15)^4-(ambient+273.15)^4)
    sigma = 5.670374419e-8                               [W/(m² K⁴)]

温度 °C、时间 s。I_series 是一个回路的串联阻抗电流有效值 [A]，不等于含充电的端口总流。跟踪线路 g_us_per_km=0。开断一个端口不等同于整条线路停运：若另一端仍带电，原生 π 电路的充电及串流仍须计入。无串流时也继续太阳吸热、对流和辐射。

## 保护与一次重合闸

每个保护连接一个原生 line-switch，保护其 element 所指的热线路。测量 I 为安装端的端口总电流 [A]，含充电及设备并联回路；pickup_a 同为一次侧总电流，不再乘 CT 比。闭合时取对应 from/to 端口电流；开关断开时该保护测量为 0。

连续记忆 u 采用以下反时限积分与指数复归研究模型：

    闭合且 I > pickup_a： du/dt = ((I/pickup_a)^exponent - 1)/time_scale_s
    其余情况：           du/dt = -u/reset_tau_s

time_scale_s 单位 s，exponent 无量纲。

闭合保护在 u 首次达到 1 或受保护线 T 首次达到 limit_c 时跳闸，cause 为 overcurrent 或 thermal。跳闸只打开该 line-switch，所有温度和保护记忆连续，不得清零。随后按新拓扑求电流，其他保护承接各自历史。

auto_reclose=false 时首次跳闸即永久锁定。为 true 时首次跳闸后，必须同时满足死区 dead_time_s 已过、T<=reclose_c，取最早满足时刻重合；等待期间继续复归和热演化。每个保护最多重合一次，第二次跳闸永久锁定。重合不重置 u 或 T。reclose_c<limit_c。外生时段只改变负荷和天气，不复位保护或开关历史。

## 输入

request 字典包含：

- network_json：pandapower.to_json(net) 的字符串。支持 bus、line、trafo、trafo3w、load、sgen、gen、ext_grid、switch；其他设备/控制器/保护表为空。原生网络有效，索引可不连续、非排序；load.p_mw 非负。
- thermal：非空列表，每项 {line, capacity_j_per_mk, h_w_per_mk, diameter_m, emissivity, alpha_per_k, initial_c, limit_c}。line 为整数原生索引；容量、直径正；h、alpha 非负；emissivity 在 [0,1]；initial_c<limit_c 且初始电阻正。
- relays：非空列表，每项 {switch, pickup_a, exponent, time_scale_s, reset_tau_s, initial_memory, auto_reclose, dead_time_s, reclose_c}。switch 为整数索引；pickup_a、exponent、time_scale_s、reset_tau_s 正；0<=initial_memory<1；auto_reclose 为布尔；dead_time_s 正。switch 必须是初始闭合的 et='l' 开关，接在其线路的 from_bus/to_bus；该线路在 thermal 且初始在运。每条受保护线至多一个保护，switch 不重复；可以有未保护的热线路。即使禁用重合，也要求 reclose_c 小于该线路 limit_c。
- segments：非空列表，每项 {duration_s, ambient_c, solar_w_per_m, load_scale}。duration_s 正；solar 非负、缺省 0；load_scale 非负、缺省 1。每段以原网络 load 的 P、Q 乘本段 scale，保留原 scaling；不缩放 sgen/gen。段内天气、负荷恒定。
- sensitivity：缺省/null 不请求导数；否则为 {switch: 一个已定义保护的索引}，对其 pickup_a 求局部响应，其余输入固定。

所有新增物理数值有限，布尔值不是数值；摄氏温度高于 -273.15 °C。引用、必填字段、唯一性或参数范围非法抛 ValueError，未知网络 JSON 也抛 ValueError；无需验证任意畸形原生电网。不得修改输入对象。

承诺精度与时间预算的工程工况域为：capacity_j_per_mk 在 [20,2000]，h_w_per_mk 在 [0,20]，diameter_m 在 [0.001,0.2]，alpha_per_k 在 [0,0.01]；pickup_a 在 [1,20000] A，exponent 在 [0.02,2]，time_scale_s 在 [0.05,5000] s，reset_tau_s 在 [0.5,3600] s，dead_time_s 在 [0.05,600] s，load_scale 在 [0,5]。原生网络处于 float64 可稳定计算的工程范围，不含数值奇异或逼近潮流鞍结的工况。该工况域限定精度/预算承诺，不是额外的 ValueError 校验条件；超出它不强制拒绝。前述类型、有限性、唯一性与物理正值等校验仍为必需。

规模≤20 母线、4 热线路、4 保护、4 时段、总时长600 s；line/trafo/trafo3w 总行数≤40，load/sgen/gen/ext_grid 总行数≤40，switch≤40；环境温度 -40至60 °C，轨迹温度 -40至200 °C，轨迹电阻正。供电分区有稳定高电压解，保护不产生无参考的受控供电岛。初始没有动作。事件均横截，相邻事件相隔至少0.05 s，事件不恰好位于外生段边界；不要求处理同时动作/擦边。重合的死区和温度约束不同时恰好成为等式。

请求 sensitivity 时，目标 pickup_a 邻域内动作数量、类型、顺序不变，事件横截，结果可微；不要求在事件序变化、边界动作或不可微点求导。

## 输出与精度

返回普通 JSON 对象：

    line_order: thermal 的 line 顺序
    switch_order: relays 的 switch 顺序
    bus_order / gen_order: 原生相应表的行索引顺序
    events: 按时间排列的 {time_s, switch, line, action, cause, trip_count}
    unserved_energy_mwh: 全时域失供电量
    segments: 每个外生时段一个终点记录
    sensitivity: null 或下述对象

action 为 trip/reclose；trip 的 cause 为 overcurrent/thermal，reclose 的 cause 为 ready；trip_count 为该保护事件后的累计跳闸次数（重合不增加）。

每个 segments 记录包含 start_s、end_s、temperature_c、relay_memory、series_current_a、relay_current_a、switch_closed、locked、trip_count、bus_vm_pu、bus_va_degree、gen_q_mvar、unserved_energy_mwh。热数组按 line_order，保护数组按 switch_order，其余按对应表；无定义的母线/机组值为 null。locked 仅对永久锁定为 true。segments[k].unserved_energy_mwh 是从 0 到本段 end_s 的累计失供电量，不是本段增量。终点属于本段设置，下一段天气/负荷尚未生效。start_s/end_s 为从 0 开始按 duration_s 累计的段起止时间，允许 1e-8 s 绝对误差。

失供功率是当前原生在运 load 的正有功需求中落在失供母线的部分，含本段 load_scale 与原 scaling；其他发电不从需求定义中扣除。该功率 [MW] 对时间 [s] 积分除3600，得到 MWh，须包含跳闸、重合和外生段导致的供电时长变化。

sensitivity 对象包含 d_unserved_energy_mwh_d_pickup_a、d_final_temperature_c_d_pickup_a（按 line_order）、d_event_time_s_d_pickup_a（对应名义 events）。这是完整混合轨迹的总导数，包含事件时间移动，不能冻结名义动作时刻。

单答案绝对误差：温度2e-4 °C、记忆2e-6、电流0.03 A、电压2e-6 pu、相角2e-4度、机组无功2e-5 Mvar、事件时间0.02 s、失供电量2e-6 MWh。导数允许绝对/相对误差中的较大者：事件时间2e-4 s/A或0.3%；终温5e-5 °C/A或0.5%；失供电量2e-7 MWh/A或0.5%。ID、键、顺序、维度、布尔/null准确；重复结果按上述精度一致，不要求浮点逐位相同。

CLI 成功只输出一个 JSON 并退出0；失败非零退出、stderr说明，不输出伪成功。数值失败抛 RuntimeError，不交付部分结果。单次函数/CLI 60 s（含导入），1 CPU、2 GiB；不要求特定算法。

计算进程可读取 /app/src、公开输入、Python依赖及必要系统库，只能写私有临时目录；无网络，支持线程。单个常规文件≤32 MiB、打开句柄≤128、单次输入/输出传输≤16 MiB（含序列化开销）。
