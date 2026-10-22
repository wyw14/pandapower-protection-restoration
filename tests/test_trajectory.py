"""Independent physical and hybrid-event acceptance; all candidate work is isolated."""

import copy
import json
import math
import pandapower as pp
import pytest
from candidate_proxy import candidate, run_cli
from analytic_oracle import (
    closed_form,
    analytic_response,
    electrical,
    temperature_after,
)

ATOL = dict(
    temperature_c=2e-4,
    relay_memory=2e-6,
    series_current_a=0.03,
    relay_current_a=0.03,
    bus_vm_pu=2e-6,
    bus_va_degree=2e-4,
    gen_q_mvar=2e-5,
    time_s=0.02,
    unserved_energy_mwh=2e-6,
    start_s=1e-8,
    end_s=1e-8,
    d_unserved_energy_mwh_d_pickup_a=2e-7,
    d_final_temperature_c_d_pickup_a=5e-5,
    d_event_time_s_d_pickup_a=2e-4,
)
RTOL = dict(
    d_unserved_energy_mwh_d_pickup_a=0.005,
    d_final_temperature_c_d_pickup_a=0.005,
    d_event_time_s_d_pickup_a=0.003,
)


def compare(got, expected, field=None, path="$"):
    """Collect every field difference so one angle error cannot mask physics."""
    errors = []
    if isinstance(expected, dict):
        if type(got) is not dict:
            return [f"{path}: expected object, got {type(got).__name__}"]
        if set(got) != set(expected):
            errors.append(f"{path}: keys {set(got)!r}, expected {set(expected)!r}")
        for key in expected.keys() & got.keys():
            errors.extend(compare(got[key], expected[key], key, path + "." + key))
    elif isinstance(expected, list):
        if type(got) is not list:
            return [f"{path}: expected list, got {type(got).__name__}"]
        if len(got) != len(expected):
            errors.append(f"{path}: length {len(got)}, expected {len(expected)}")
        for i, (a, b) in enumerate(zip(got, expected)):
            errors.extend(compare(a, b, field, f"{path}[{i}]"))
    elif expected is None:
        if got is not None:
            errors.append(f"{path}: {got!r}, expected null")
    elif field in ATOL:
        tolerance = max(ATOL[field], RTOL.get(field, 0) * abs(expected))
        if (
            not isinstance(got, (int, float))
            or isinstance(got, bool)
            or not math.isfinite(got)
        ):
            errors.append(f"{path}: {got!r}, expected a finite number")
        elif abs(got - expected) > tolerance:
            errors.append(f"{path}: {got!r}, expected {expected!r} +/- {tolerance}")
    else:
        if type(got) is not type(expected) or got != expected:
            errors.append(
                f"{path}: {got!r} ({type(got).__name__}), expected {expected!r} ({type(expected).__name__})"
            )
    return errors


def run(req):
    old = copy.deepcopy(req)
    got = candidate.analyze(req, timeout=60)
    assert req == old
    json.dumps(got, allow_nan=False)
    return got


def network(
    parallel=1, capacitance=0.0, load=5.0, reactive=2.0, length=10.0, side="from"
):
    net = pp.create_empty_network(sn_mva=100.0, f_hz=50.0)
    pp.create_bus(net, 20.0, index=41)
    pp.create_bus(net, 20.0, index=7)
    pp.create_ext_grid(net, 41, va_degree=3.0)
    pp.create_line_from_parameters(
        net, 41, 7, length, 0.2, 0.25, capacitance, 1.0, index=83, parallel=parallel
    )
    pp.create_load(net, 7, load, reactive, index=19)
    pp.create_switch(net, 41 if side == "from" else 7, 83, "l", closed=True, index=201)
    return net


def conductor(line=83, **kw):
    v = dict(
        line=line,
        capacity_j_per_mk=160.0,
        h_w_per_mk=0.7,
        diameter_m=0.026,
        emissivity=0.0,
        alpha_per_k=0.0,
        initial_c=25.0,
        limit_c=180.0,
    )
    v.update(kw)
    return v


def relay(switch=201, **kw):
    v = dict(
        switch=switch,
        pickup_a=100.0,
        exponent=2.0,
        time_scale_s=40.0,
        reset_tau_s=30.0,
        initial_memory=0.1,
        auto_reclose=False,
        dead_time_s=5.0,
        reclose_c=80.0,
    )
    v.update(kw)
    return v


def segment(duration=90.0, **kw):
    v = dict(duration_s=duration, ambient_c=25.0, solar_w_per_m=0.0, load_scale=1.0)
    v.update(kw)
    return v


def request(net=None, thermal=None, relays=None, segments=None, sensitivity=None):
    return dict(
        network_json=pp.to_json(network() if net is None else net),
        thermal=[conductor()] if thermal is None else thermal,
        relays=[relay()] if relays is None else relays,
        segments=[segment()] if segments is None else segments,
        sensitivity=sensitivity,
    )


@pytest.mark.parametrize(
    "parallel,capacitance,side,load",
    [
        (1, 0.0, "from", 5.0),
        (2, 0.0, "from", 5.0),
        (1, 2500.0, "from", 0.0),
        (1, 2500.0, "to", 0.0),
    ],
)
def test_terminal_ct_and_per_circuit_heat_use_distinct_currents(
    parallel, capacitance, side, load
):
    """“含充电及设备并联回路”：独立π电路消元分别校核保护端口电流与热串流。"""
    net = network(
        parallel,
        capacitance,
        load,
        0.0 if not load else 2.0,
        20.0 if capacitance else 10.0,
        side,
    )
    r = relay(
        pickup_a=130.0 if capacitance else 100.0,
        time_scale_s=3.0 if capacitance else 40.0,
    )
    req = request(net, relays=[r], segments=[segment(90.0)])
    expected = closed_form(req)
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_overcurrent_memory_survives_load_steps_and_exponential_reset():
    """“外生时段只改变负荷和天气，不复位保护或开关历史”：闭式跨段记忆、复归与失供。"""
    req = request(
        relays=[relay(initial_memory=0.35, exponent=0.02, time_scale_s=0.2)],
        segments=[
            segment(6.0),
            segment(18.0, load_scale=0.25),
            segment(60.0, load_scale=1.3),
            segment(10.0, load_scale=0.8),
        ],
    )
    expected = closed_form(req)
    assert (
        expected["segments"][1]["relay_memory"][0]
        < expected["segments"][0]["relay_memory"][0]
    )
    assert expected["events"][0]["time_s"] > 24.0
    assert (
        expected["segments"][3]["unserved_energy_mwh"]
        > expected["segments"][2]["unserved_energy_mwh"]
        > 0
    )
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_deadtime_reclose_keeps_memory_and_second_trip_locks():
    """“每个保护最多重合一次，第二次跳闸永久锁定”：闭式事件链检验死区和记忆延续。"""
    req = request(relays=[relay(auto_reclose=True, dead_time_s=5.0, reclose_c=80.0)])
    expected = closed_form(req)
    assert [v["action"] for v in expected["events"]] == ["trip", "reclose", "trip"]
    assert (
        expected["events"][2]["time_s"] - expected["events"][1]["time_s"]
        < expected["events"][0]["time_s"]
    )
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_cooling_gate_delays_reclose_and_preserves_thermal_state():
    """“必须同时满足死区 dead_time_s 已过、T<=reclose_c”：指数冷却独立确定重合时间。"""
    req = request(
        thermal=[conductor(initial_c=60.0, capacity_j_per_mk=20.0, limit_c=120.0)],
        relays=[
            relay(time_scale_s=5.0, auto_reclose=True, dead_time_s=3.0, reclose_c=30.0)
        ],
        segments=[segment(110.0)],
    )
    expected = closed_form(req)
    assert expected["events"][1]["time_s"] - expected["events"][0]["time_s"] > 20.0
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


@pytest.mark.parametrize(
    "omitted", [("solar_w_per_m",), ("load_scale",), ("solar_w_per_m", "load_scale")]
)
def test_optional_weather_and_load_defaults_preserve_the_complete_event_chain(omitted):
    """“solar 非负、缺省 0；load_scale 非负、缺省 1”：省略与显式两种请求分别核对解析重合轨迹。"""
    explicit = request(
        relays=[relay(auto_reclose=True)],
        segments=[segment(12.0), segment(78.0)],
        sensitivity=dict(switch=201),
    )
    expected = analytic_response(explicit)
    assert [event["action"] for event in expected["events"]] == [
        "trip",
        "reclose",
        "trip",
    ]
    implicit = copy.deepcopy(explicit)
    for weather in implicit["segments"]:
        for key in omitted:
            del weather[key]
    explicit_differences = compare(run(explicit), expected)
    implicit_differences = compare(run(implicit), expected)
    assert not explicit_differences, "\n".join(explicit_differences)
    assert not implicit_differences, "\n".join(implicit_differences)


def test_native_zip_and_temperature_flags_do_not_change_the_defined_model():
    """“忽略 load 的恒阻抗/恒电流百分比及 line 的 temperature/tdpf 字段”：干扰字段不改变解析轨迹。"""
    explicit = request(relays=[relay(auto_reclose=True)], segments=[segment(90.0)])
    expected = closed_form(explicit)
    changed = copy.deepcopy(explicit)
    net = pp.from_json_string(changed["network_json"])
    net.load.loc[:, "const_z_p_percent"] = 30.0
    net.load.loc[:, "const_i_p_percent"] = 25.0
    net.load.loc[:, "const_z_q_percent"] = 20.0
    net.load.loc[:, "const_i_q_percent"] = 35.0
    net.line.loc[:, "temperature_degree_celsius"] = 150.0
    net.line.loc[:, "alpha"] = 0.009
    net.line.loc[:, "tdpf"] = True
    changed["network_json"] = pp.to_json(net)
    baseline_differences = compare(run(explicit), expected)
    changed_differences = compare(run(changed), expected)
    assert not baseline_differences, "\n".join(baseline_differences)
    assert not changed_differences, "\n".join(changed_differences)


def test_load_steps_leave_static_and_limited_generator_injections_unscaled():
    """“不缩放 sgen/gen”：非单位负荷时段以独立两母线净注入解析解核对固定发电与无功上限。"""
    net = network()
    pp.create_sgen(net, 7, p_mw=1.2, q_mvar=0.3, scaling=0.7, index=33)
    pp.create_gen(
        net, 7, p_mw=0.8, vm_pu=1.08, min_q_mvar=-0.3, max_q_mvar=0.2, index=72
    )
    req = request(
        net,
        relays=[relay(pickup_a=10000.0)],
        segments=[segment(15.0, load_scale=1.4), segment(20.0, load_scale=0.6)],
    )
    equivalent = pp.from_json_string(req["network_json"])
    equivalent.gen = equivalent.gen.iloc[:0].copy()
    pp.create_sgen(equivalent, 7, p_mw=0.8, q_mvar=0.2, index=99)
    analytic_request = copy.deepcopy(req)
    analytic_request["network_json"] = pp.to_json(equivalent)
    expected = closed_form(analytic_request)
    expected["gen_order"] = [72]
    for point in expected["segments"]:
        # At the upper Q limit the remote voltage is strictly below the PV
        # target, so the saturated PQ equivalent is the physical branch.
        assert point["bus_vm_pu"][1] < 1.08 - 0.01
        point["gen_q_mvar"] = [0.2]
    assert expected["events"] == []
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_thermal_trip_cooling_reclose_and_solar_energy_balance():
    """“无串流时也继续太阳吸热、对流和辐射”：线性热极限校核热跳闸和冷却重合。"""
    req = request(
        thermal=[conductor(capacity_j_per_mk=20.0, h_w_per_mk=0.2, limit_c=30.0)],
        relays=[
            relay(pickup_a=10000.0, auto_reclose=True, dead_time_s=3.0, reclose_c=27.0)
        ],
        segments=[segment(230.0, solar_w_per_m=0.1)],
    )
    expected = closed_form(req)
    assert [v["cause"] for v in expected["events"]] == ["thermal", "ready", "thermal"]
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def parallel_request(sensitivity=False, capacitance=0.0):
    net = network(load=6.0, reactive=2.0, length=3.0, capacitance=capacitance)
    pp.create_line_from_parameters(
        net, 41, 7, 3.0, 0.3, 0.24, capacitance, 1.0, index=4
    )
    pp.create_switch(net, 41, 4, "l", closed=True, index=305)
    return request(
        net,
        [conductor(83, initial_c=40.0), conductor(4, initial_c=50.0)],
        [
            relay(201, pickup_a=65.0, time_scale_s=8.0, initial_memory=0.15),
            relay(305, pickup_a=100.0, time_scale_s=15.0, initial_memory=0.4),
        ],
        [segment(40.0)],
        dict(switch=201) if sensitivity else None,
    )


@pytest.mark.parametrize("capacitance", [0.0, 500.0])
def test_parallel_trip_redistribution_cascades_and_counts_unserved_energy(capacitance):
    """“随后按新拓扑求电流，其他保护承接各自历史”：代数电路与闭式记忆给出两次级联。"""
    req = parallel_request(capacitance=capacitance)
    expected = closed_form(req)
    assert [v["switch"] for v in expected["events"]] == [201, 305]
    assert expected["unserved_energy_mwh"] > 0
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_unprotected_thermal_line_carries_redistributed_load_after_trip():
    """“可以有未保护的热线路”：支路失去保护不等于停运，独立电路消元校核接续供电与温升。"""
    req = parallel_request(capacitance=500.0)
    req["relays"] = req["relays"][:1]
    expected = closed_form(req)
    assert [event["switch"] for event in expected["events"]] == [201]
    assert expected["segments"][0]["series_current_a"][1] > 100
    assert expected["unserved_energy_mwh"] == pytest.approx(0, abs=2e-6)
    got = run(req)
    differences = compare(got, expected)
    assert not differences, "\n".join(differences)


def test_cascade_response_includes_movement_of_both_event_times():
    """“包含事件时间移动，不能冻结名义动作时刻”：解析切向递推校核两次级联的总导数。"""
    req = parallel_request(sensitivity=True)
    expected = analytic_response(req)
    assert abs(expected["sensitivity"]["d_event_time_s_d_pickup_a"][1]) > 1e-3
    assert abs(expected["sensitivity"]["d_unserved_energy_mwh_d_pickup_a"]) > 1e-5
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_single_trip_event_and_supply_derivatives_have_closed_forms():
    """“这是完整混合轨迹的总导数”：单次恒流跳闸的事件、终温与失供导数直接求导校核。"""
    req = request(sensitivity=dict(switch=201), segments=[segment(80.0)])
    expected = closed_form(req)
    net = pp.from_json_string(req["network_json"])
    r = req["relays"][0]
    th = req["thermal"][0]
    point = electrical(net, req["thermal"], req["relays"], [True], 1.0)
    current = point["relay_current_a"][0]
    ratio = (current / r["pickup_a"]) ** r["exponent"]
    derivative = (
        (1 - r["initial_memory"])
        * r["time_scale_s"]
        * r["exponent"]
        * ratio
        / (r["pickup_a"] * (ratio - 1) ** 2)
    )
    trip = expected["events"][0]["time_s"]
    a = th["h_w_per_mk"] / th["capacity_j_per_mk"]
    heat = point["series_current_a"][0] ** 2 * 0.2 / 1000
    dtemp = heat / th["capacity_j_per_mk"] * math.exp(-a * (80.0 - trip)) * derivative
    expected["sensitivity"] = dict(
        d_event_time_s_d_pickup_a=[derivative],
        d_unserved_energy_mwh_d_pickup_a=-5.0 / 3600 * derivative,
        d_final_temperature_c_d_pickup_a=[dtemp],
    )
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


@pytest.mark.parametrize(
    "placement,margin",
    [
        ("before_end", 0.001),
        ("before_end", 0.00001),
        ("before_load_change", 0.001),
        ("after_load_change", 0.001),
    ],
)
def test_local_sensitivity_near_fixed_boundary_preserves_valid_branch(
    placement, margin
):
    """“目标 pickup_a 邻域内动作数量、类型、顺序不变”：合法窄邻域用解析事件链切向验收。"""
    req = request(sensitivity=dict(switch=201))
    net = pp.from_json_string(req["network_json"])
    current = electrical(net, req["thermal"], req["relays"], [True], 1.0)[
        "relay_current_a"
    ][0]
    r = req["relays"][0]
    trip = (
        (1 - r["initial_memory"])
        * r["time_scale_s"]
        / ((current / r["pickup_a"]) ** r["exponent"] - 1)
    )
    arrangements = {
        "before_end": [segment(trip + margin)],
        "before_load_change": [segment(trip + margin), segment(3.0, load_scale=2.0)],
        "after_load_change": [segment(trip - margin), segment(3.0, load_scale=1.5)],
    }
    req["segments"] = arrangements[placement]
    expected = analytic_response(req)
    assert len(expected["events"]) == 1
    assert expected["sensitivity"]["d_event_time_s_d_pickup_a"][0] > 0
    got = run(req)
    differences = compare(got, expected)
    assert not differences, "\n".join(differences)


def test_reclose_response_includes_cooling_and_memory_dependent_second_trip():
    """“重合不重置 u 或 T”：解析切向递推校核冷却门、历史记忆和二次跳闸的完整响应。"""
    req = request(
        thermal=[conductor(initial_c=60.0, capacity_j_per_mk=20.0, limit_c=120.0)],
        relays=[
            relay(time_scale_s=5.0, auto_reclose=True, dead_time_s=3.0, reclose_c=30.0)
        ],
        segments=[segment(110.0)],
        sensitivity=dict(switch=201),
    )
    expected = analytic_response(req)
    assert len(expected["events"]) == 3
    assert abs(expected["sensitivity"]["d_event_time_s_d_pickup_a"][1]) > 1e-4
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)


def test_downstream_isolation_makes_upstream_backup_memory_recover():
    """“跳闸只打开该 line-switch”：串联等效电路和指数复归校核下游隔离后的上游状态。"""
    net = pp.create_empty_network(sn_mva=100.0)
    for idx in (41, 7, 88):
        pp.create_bus(net, 20.0, index=idx)
    pp.create_ext_grid(net, 41)
    pp.create_line_from_parameters(net, 41, 7, 3.0, 0.2, 0.25, 0.0, 1.0, index=83)
    pp.create_line_from_parameters(net, 7, 88, 2.0, 0.2, 0.25, 0.0, 1.0, index=4)
    pp.create_switch(net, 41, 83, "l", closed=True, index=201)
    pp.create_switch(net, 7, 4, "l", closed=True, index=305)
    pp.create_load(net, 88, 6.0, 2.0, index=19)
    th = [conductor(83), conductor(4)]
    rs = [
        relay(201, pickup_a=100.0, time_scale_s=100.0, initial_memory=0.15),
        relay(305, pickup_a=120.0, time_scale_s=5.0, initial_memory=0.1),
    ]
    req = request(net, th, rs, [segment(40.0)])
    got = run(req)
    equivalent = network(load=6.0, reactive=2.0, length=5.0)
    current = electrical(equivalent, [conductor()], [relay()], [True], 1.0)[
        "series_current_a"
    ][0]

    def rate(r):
        return ((current / r["pickup_a"]) ** r["exponent"] - 1) / r["time_scale_s"]

    trip = (1 - rs[1]["initial_memory"]) / rate(rs[1])
    assert len(got["events"]) == 1 and got["events"][0]["switch"] == 305
    differences = compare(got["events"][0]["time_s"], trip, "time_s")
    assert not differences, "\n".join(differences)
    endpoint = got["segments"][0]
    assert endpoint["switch_closed"] == [True, False] and endpoint["locked"] == [
        False,
        True,
    ]
    expected_u = (rs[0]["initial_memory"] + rate(rs[0]) * trip) * math.exp(
        -(40 - trip) / rs[0]["reset_tau_s"]
    )
    differences = compare(endpoint["relay_memory"][0], expected_u, "relay_memory")
    assert not differences, "\n".join(differences)
    differences = compare(
        got["unserved_energy_mwh"], 6.0 * (40 - trip) / 3600, "unserved_energy_mwh"
    )
    assert not differences, "\n".join(differences)
    temperatures = []
    for t in th:
        warm = temperature_after(25.0, trip, t, current * current * 0.2 / 1000, 25.0)
        temperatures.append(temperature_after(warm, 40 - trip, t, 0.0, 25.0))
    differences = compare(endpoint["temperature_c"], temperatures, "temperature_c")
    assert not differences, "\n".join(differences)


def test_reindexed_network_and_permuted_state_vectors_preserve_physics():
    """“索引可不连续、非排序”：同一物理网络重排行索引及状态顺序后独立重算对照。"""
    req = parallel_request(sensitivity=True)
    a = run(req)
    differences = compare(a, analytic_response(req))
    assert not differences, "\n".join(differences)
    net = pp.from_json_string(req["network_json"])
    net.bus = net.bus.iloc[::-1]
    net.line = net.line.iloc[::-1]
    net.switch = net.switch.iloc[::-1]
    changed = copy.deepcopy(req)
    changed["network_json"] = pp.to_json(net)
    changed["thermal"].reverse()
    changed["relays"].reverse()
    # Each independently approximate result is checked against its own oracle;
    # no tighter one-answer tolerance is imposed between the two outputs.
    differences = compare(run(changed), analytic_response(changed))
    assert not differences, "\n".join(differences)


def invalid_request(change):
    """Build one explicit invalid input before the unconditional rejection check."""
    req = request()
    if change == "duplicate":
        req["relays"].append(copy.deepcopy(req["relays"][0]))
    elif change == "pickup":
        req["relays"][0]["pickup_a"] = 0.0
    elif change == "memory":
        req["relays"][0]["initial_memory"] = 1.0
    elif change == "reclose":
        req["relays"][0]["reclose_c"] = 200.0
    elif change == "switch":
        req["relays"][0]["switch"] = 999
    elif change == "bool":
        req["segments"][0]["duration_s"] = True
    elif change == "nan":
        req["thermal"][0]["emissivity"] = float("nan")
    elif change == "missing_field":
        del req["thermal"][0]["capacity_j_per_mk"]
    elif change == "unknown_json":
        req["network_json"] = "not a pandapower JSON object"
    elif change == "empty_thermal":
        req["thermal"] = []
    elif change == "nonlist_segments":
        req["segments"] = {}
    elif change == "duplicate_thermal":
        req["thermal"].append(copy.deepcopy(req["thermal"][0]))
    elif change == "reclose_type":
        req["relays"][0]["auto_reclose"] = 1
    elif change == "absolute_zero":
        req["segments"][0]["ambient_c"] = -273.15
    elif change == "negative_capacity":
        req["thermal"][0]["capacity_j_per_mk"] = -1
    elif change == "emissivity_above_one":
        req["thermal"][0]["emissivity"] = 1.01
    elif change == "initial_overtemperature":
        req["thermal"][0]["initial_c"] = req["thermal"][0]["limit_c"]
    elif change == "unknown_sensitivity":
        req["sensitivity"] = {"switch": 999}
    elif change in ("nonline_switch", "initially_open"):
        net = pp.from_json_string(req["network_json"])
        if change == "nonline_switch":
            pp.create_switch(net, 41, 7, "b", closed=True, index=399)
            req["relays"][0]["switch"] = 399
        else:
            net.switch.at[201, "closed"] = False
        req["network_json"] = pp.to_json(net)
    else:
        raise ValueError("Unknown rejection fixture: " + change)
    return req


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "pickup",
        "memory",
        "reclose",
        "switch",
        "bool",
        "nan",
        "missing_field",
        "unknown_json",
        "empty_thermal",
        "nonlist_segments",
        "duplicate_thermal",
        "reclose_type",
        "absolute_zero",
        "negative_capacity",
        "emissivity_above_one",
        "initial_overtemperature",
        "unknown_sensitivity",
        "nonline_switch",
        "initially_open",
    ],
)
def test_invalid_physical_contract_is_rejected(change):
    """“引用、必填字段、唯一性或参数范围非法抛 ValueError”：每一参数独立注册并必然验收。"""
    req = invalid_request(change)
    with pytest.raises(ValueError):
        candidate.analyze(req, timeout=60)


def test_cli_and_repeated_requests_are_independently_correct():
    """“CLI 成功只输出一个 JSON 并退出0”：交错工况、重复调用和失败入口分别检查。"""
    req = request(segments=[segment(45.0)])
    expected = closed_form(req)
    a = run(req)
    differences = compare(a, expected)
    assert not differences, "\n".join(differences)
    other = request(relays=[relay(auto_reclose=True)], segments=[segment(70.0)])
    differences = compare(run(other), closed_form(other))
    assert not differences, "\n".join(differences)
    with pytest.raises(ValueError):
        candidate.analyze(invalid_request("negative_capacity"), timeout=60)
    differences = compare(run(req), expected)
    assert not differences, "\n".join(differences)
    process = run_cli(json.dumps(req), timeout=60)
    assert process.returncode == 0, process.stderr
    differences = compare(json.loads(process.stdout), expected)
    assert not differences, "\n".join(differences)
    bad = run_cli("{}", timeout=60)
    assert bad.returncode != 0 and not bad.stdout.strip() and bad.stderr.strip()


def native_request():
    net = pp.create_empty_network(sn_mva=100.0)
    for idx, kv in [
        (41, 110.0),
        (7, 20.0),
        (88, 20.0),
        (2, 20.0),
        (301, 10.0),
        (400, 0.4),
    ]:
        pp.create_bus(net, kv, index=idx)
    pp.create_ext_grid(net, 41)
    pp.create_transformer3w_from_parameters(
        net,
        41,
        7,
        301,
        vn_hv_kv=110.0,
        vn_mv_kv=20.0,
        vn_lv_kv=10.0,
        sn_hv_mva=40.0,
        sn_mv_mva=25.0,
        sn_lv_mva=15.0,
        vk_hv_percent=10.0,
        vk_mv_percent=10.0,
        vk_lv_percent=10.0,
        vkr_hv_percent=0.4,
        vkr_mv_percent=0.4,
        vkr_lv_percent=0.4,
        pfe_kw=0.0,
        i0_percent=0.0,
        index=11,
    )
    pp.create_switch(net, 7, 88, "b", closed=True, index=61)
    pp.create_line_from_parameters(net, 88, 2, 3.0, 0.25, 0.2, 300.0, 1.0, index=83)
    pp.create_line_from_parameters(net, 88, 2, 3.0, 0.3, 0.24, 300.0, 1.0, index=4)
    pp.create_switch(net, 88, 83, "l", closed=True, index=201)
    pp.create_switch(net, 2, 4, "l", closed=True, index=305)
    pp.create_load(net, 2, 7.5, 3.75, scaling=0.8, index=9)
    pp.create_load(net, 301, 1.9, 0.57, index=3)
    pp.create_sgen(net, 2, 0.05, 0.02, index=91)
    pp.create_transformer_from_parameters(
        net,
        301,
        400,
        sn_mva=1.0,
        vn_hv_kv=10.0,
        vn_lv_kv=0.4,
        vk_percent=4.0,
        vkr_percent=1.0,
        pfe_kw=0.0,
        i0_percent=0.0,
        index=222,
    )
    pp.create_load(net, 400, 0.1, 0.03, index=54)
    pp.create_gen(net, 2, 0.4, vm_pu=1.03, min_q_mvar=-0.3, max_q_mvar=0.4, index=72)
    th = [
        conductor(
            4, initial_c=35.0, alpha_per_k=0.003, emissivity=0.6, capacity_j_per_mk=45.0
        ),
        conductor(
            83,
            initial_c=65.0,
            alpha_per_k=0.004,
            emissivity=0.7,
            capacity_j_per_mk=35.0,
        ),
    ]
    rs = [
        relay(305, pickup_a=100.0, time_scale_s=25.0, initial_memory=0.2),
        relay(201, pickup_a=70.0, time_scale_s=3.0, initial_memory=0.1),
    ]
    return request(net, th, rs, [segment(6.0, solar_w_per_m=2.0, ambient_c=30.0)])


def test_native_three_winding_q_limits_and_nonlinear_switching_converge_independently():
    """“设备 ID、并联数、母联开关、两/三绕组变压器保留原生含义”：独立固定步中点法收敛校核。"""
    from numeric_oracle import fixed_step

    req = native_request()
    coarse = fixed_step(req, 0.2)
    fine = fixed_step(req, 0.1)
    finer = fixed_step(req, 0.05)
    # Three meshes expose the convergence trend, not merely one close pair.
    coarse_error = normalized_distance(coarse, fine)
    fine_error = normalized_distance(fine, finer)
    assert fine_error <= max(0.5 * coarse_error, 1e-5)
    assert fine_error < 0.15, "Independent numerical error consumes too much tolerance"
    differences = compare(coarse, fine)
    assert not differences, "\n".join(differences)
    assert len(fine["events"]) == 1 and fine["events"][0]["switch"] == 201
    assert fine["segments"][0]["gen_q_mvar"][0] == pytest.approx(0.4, abs=2e-5)
    differences = compare(run(req), finer)
    assert not differences, "\n".join(differences)


def normalized_distance(left, right, field=None):
    """Maximum numerical mesh change measured in the unchanged public tolerance."""
    if isinstance(right, dict):
        return max(
            (
                normalized_distance(left[key], value, key)
                for key, value in right.items()
            ),
            default=0.0,
        )
    if isinstance(right, list):
        return max(
            (normalized_distance(a, b, field) for a, b in zip(left, right)), default=0.0
        )
    if right is not None and field in ATOL:
        return abs(left - right) / max(ATOL[field], RTOL.get(field, 0) * abs(right))
    return 0.0
