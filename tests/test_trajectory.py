"""Independent physical and hybrid-event acceptance; all candidate work is isolated."""
import copy
import json
import math
import numpy as np
import pandapower as pp
import pytest
from candidate_proxy import candidate, run_cli
from analytic_oracle import closed_form, analytic_response, electrical, temperature_after

ATOL = dict(temperature_c=2e-4, relay_memory=2e-6, series_current_a=.03, relay_current_a=.03,
            bus_vm_pu=2e-6, bus_va_degree=2e-4, gen_q_mvar=2e-5, time_s=.02,
            unserved_energy_mwh=2e-6, start_s=1e-8, end_s=1e-8,
            d_unserved_energy_mwh_d_pickup_a=2e-7,
            d_final_temperature_c_d_pickup_a=5e-5, d_event_time_s_d_pickup_a=2e-4)
RTOL = dict(d_unserved_energy_mwh_d_pickup_a=.005,
            d_final_temperature_c_d_pickup_a=.005,d_event_time_s_d_pickup_a=.003)


def compare(got, expected, field=None):
    if isinstance(expected,dict):
        assert type(got) is dict and set(got)==set(expected)
        for key in expected: compare(got[key],expected[key],key)
    elif isinstance(expected,list):
        assert type(got) is list and len(got)==len(expected)
        for a,b in zip(got,expected):compare(a,b,field)
    elif expected is None:
        assert got is None
    elif field in ATOL:
        assert isinstance(got,(int,float)) and not isinstance(got,bool) and math.isfinite(got)
        assert abs(got-expected)<=max(ATOL[field],RTOL.get(field,0)*abs(expected))
    else:
        assert type(got) is type(expected) and got==expected


def run(req):
    old=copy.deepcopy(req)
    got=candidate.analyze(req,timeout=60)
    assert req==old
    json.dumps(got,allow_nan=False)
    return got


def network(parallel=1,capacitance=0.,load=5.,reactive=2.,length=10.,side='from'):
    net=pp.create_empty_network(sn_mva=100.,f_hz=50.)
    pp.create_bus(net,20.,index=41);pp.create_bus(net,20.,index=7)
    pp.create_ext_grid(net,41,va_degree=3.)
    pp.create_line_from_parameters(net,41,7,length,.2,.25,capacitance,1.,index=83,parallel=parallel)
    pp.create_load(net,7,load,reactive,index=19)
    pp.create_switch(net,41 if side=='from' else 7,83,'l',closed=True,index=201)
    return net


def conductor(line=83,**kw):
    v=dict(line=line,capacity_j_per_mk=160.,h_w_per_mk=.7,diameter_m=.026,
           emissivity=0.,alpha_per_k=0.,initial_c=25.,limit_c=180.)
    v.update(kw);return v


def relay(switch=201,**kw):
    v=dict(switch=switch,pickup_a=100.,exponent=2.,time_scale_s=40.,reset_tau_s=30.,
           initial_memory=.1,auto_reclose=False,dead_time_s=5.,reclose_c=80.)
    v.update(kw);return v


def segment(duration=90.,**kw):
    v=dict(duration_s=duration,ambient_c=25.,solar_w_per_m=0.,load_scale=1.)
    v.update(kw);return v


def request(net=None,thermal=None,relays=None,segments=None,sensitivity=None):
    return dict(network_json=pp.to_json(network() if net is None else net),
                thermal=[conductor()] if thermal is None else thermal,
                relays=[relay()] if relays is None else relays,
                segments=[segment()] if segments is None else segments,sensitivity=sensitivity)


@pytest.mark.parametrize('parallel,capacitance,side,load',[
    (1,0.,'from',5.),(2,0.,'from',5.),(1,2500.,'from',0.),(1,2500.,'to',0.)])
def test_terminal_ct_and_per_circuit_heat_use_distinct_currents(parallel,capacitance,side,load):
    """§§ 电气与热模型、保护：并联每回路串流与安装端总电流分别决定温升和动作。"""
    net=network(parallel,capacitance,load,0. if not load else 2.,20. if capacitance else 10.,side)
    r=relay(pickup_a=130. if capacitance else 100.,time_scale_s=3. if capacitance else 40.)
    req=request(net,relays=[r],segments=[segment(90.)])
    expected=closed_form(req)
    compare(run(req),expected)
    if capacitance and side=='from':
        initial=electrical(net,req['thermal'],req['relays'],[True],1.)
        assert initial['series_current_a'][0] < r['pickup_a'] < initial['relay_current_a'][0]
        assert expected['events'][0]['cause']=='overcurrent'


def test_overcurrent_memory_survives_load_steps_and_exponential_reset():
    """§ 保护与一次重合闸：负荷跨段变化保留记忆，欠拾取期间指数复归并累计失供。"""
    req=request(relays=[relay(initial_memory=.35,exponent=.02,time_scale_s=.2)],
                segments=[segment(6.),segment(18.,load_scale=.25),segment(60.,load_scale=1.3),segment(10.,load_scale=.8)])
    expected=closed_form(req)
    assert expected['segments'][1]['relay_memory'][0] < expected['segments'][0]['relay_memory'][0]
    assert expected['events'][0]['time_s']>24.
    assert expected['segments'][3]['unserved_energy_mwh']>expected['segments'][2]['unserved_energy_mwh']>0
    compare(run(req),expected)


def test_deadtime_reclose_keeps_memory_and_second_trip_locks():
    """§ 保护与一次重合闸：死区后仅重合一次，保留动作历史并在二次跳闸后锁定。"""
    req=request(relays=[relay(auto_reclose=True,dead_time_s=5.,reclose_c=80.)])
    expected=closed_form(req)
    assert [v['action'] for v in expected['events']]==['trip','reclose','trip']
    assert expected['events'][2]['time_s']-expected['events'][1]['time_s'] < expected['events'][0]['time_s']
    compare(run(req),expected)


def test_cooling_gate_delays_reclose_and_preserves_thermal_state():
    """§ 保护与一次重合闸：重合须同时满足死区和冷却门槛，等待时温度连续演化。"""
    req=request(thermal=[conductor(initial_c=60.,capacity_j_per_mk=20.,limit_c=120.)],
                relays=[relay(time_scale_s=5.,auto_reclose=True,dead_time_s=3.,reclose_c=30.)],
                segments=[segment(110.)])
    expected=closed_form(req)
    assert expected['events'][1]['time_s']-expected['events'][0]['time_s'] > 20.
    compare(run(req),expected)


def test_thermal_trip_cooling_reclose_and_solar_energy_balance():
    """§§ 电气与热模型、保护：太阳吸热与冷却共同决定热跳闸、允许重合及再次热动作。"""
    req=request(thermal=[conductor(capacity_j_per_mk=20.,h_w_per_mk=.2,limit_c=30.)],
                relays=[relay(pickup_a=10000.,auto_reclose=True,dead_time_s=3.,reclose_c=27.)],
                segments=[segment(230.,solar_w_per_m=.1)])
    expected=closed_form(req)
    assert [v['cause'] for v in expected['events']]==['thermal','ready','thermal']
    compare(run(req),expected)


def parallel_request(sensitivity=False,capacitance=0.):
    net=network(load=6.,reactive=2.,length=3.,capacitance=capacitance)
    pp.create_line_from_parameters(net,41,7,3.,.3,.24,capacitance,1.,index=4)
    pp.create_switch(net,41,4,'l',closed=True,index=305)
    return request(net,[conductor(83,initial_c=40.),conductor(4,initial_c=50.)],
                   [relay(201,pickup_a=65.,time_scale_s=8.,initial_memory=.15),
                    relay(305,pickup_a=100.,time_scale_s=15.,initial_memory=.4)],
                   [segment(40.)],dict(switch=201) if sensitivity else None)


@pytest.mark.parametrize('capacitance',[0.,500.])
def test_parallel_trip_redistribution_cascades_and_counts_unserved_energy(capacitance):
    """§§ 电气与热模型、保护、输出：并联支路跳开后重分配电流，引发级联并计入失供。"""
    req=parallel_request(capacitance=capacitance);expected=closed_form(req)
    assert [v['switch'] for v in expected['events']]==[201,305]
    assert expected['unserved_energy_mwh']>0
    compare(run(req),expected)


def test_cascade_response_includes_movement_of_both_event_times():
    """§§ 输入 sensitivity、输出：拾取值变化的总响应须包含两次级联事件的时刻移动。"""
    req=parallel_request(sensitivity=True);expected=analytic_response(req)
    assert abs(expected['sensitivity']['d_event_time_s_d_pickup_a'][1])>1e-3
    assert abs(expected['sensitivity']['d_unserved_energy_mwh_d_pickup_a'])>1e-5
    compare(run(req),expected)


def test_single_trip_event_and_supply_derivatives_have_closed_forms():
    """§ 输出与精度：单次跳闸的事件时间、终温和累计失供电量导数满足闭式结果。"""
    req=request(sensitivity=dict(switch=201),segments=[segment(80.)])
    expected=closed_form(req)
    net=pp.from_json_string(req['network_json']);r=req['relays'][0];th=req['thermal'][0]
    point=electrical(net,req['thermal'],req['relays'],[True],1.);I=point['relay_current_a'][0]
    ratio=(I/r['pickup_a'])**r['exponent']
    derivative=(1-r['initial_memory'])*r['time_scale_s']*r['exponent']*ratio/(r['pickup_a']*(ratio-1)**2)
    trip=expected['events'][0]['time_s'];a=th['h_w_per_mk']/th['capacity_j_per_mk']
    heat=point['series_current_a'][0]**2*.2/1000
    dtemp=heat/th['capacity_j_per_mk']*math.exp(-a*(80.-trip))*derivative
    expected['sensitivity']=dict(d_event_time_s_d_pickup_a=[derivative],
       d_unserved_energy_mwh_d_pickup_a=-5./3600*derivative,
       d_final_temperature_c_d_pickup_a=[dtemp])
    compare(run(req),expected)


def test_reclose_response_includes_cooling_and_memory_dependent_second_trip():
    """§§ 保护与一次重合闸、输出：冷却和历史记忆耦合重合及二次跳闸的完整轨迹导数。"""
    req=request(thermal=[conductor(initial_c=60.,capacity_j_per_mk=20.,limit_c=120.)],
                relays=[relay(time_scale_s=5.,auto_reclose=True,dead_time_s=3.,reclose_c=30.)],
                segments=[segment(110.)],sensitivity=dict(switch=201))
    expected=analytic_response(req)
    assert len(expected['events'])==3
    assert abs(expected['sensitivity']['d_event_time_s_d_pickup_a'][1])>1e-4
    compare(run(req),expected)


def test_downstream_isolation_makes_upstream_backup_memory_recover():
    """§§ 电气与热模型、保护、输出：下游隔离使上游复归，失供需求和两线冷却保持正确。"""
    net=pp.create_empty_network(sn_mva=100.)
    for idx in (41,7,88):pp.create_bus(net,20.,index=idx)
    pp.create_ext_grid(net,41)
    pp.create_line_from_parameters(net,41,7,3.,.2,.25,0.,1.,index=83)
    pp.create_line_from_parameters(net,7,88,2.,.2,.25,0.,1.,index=4)
    pp.create_switch(net,41,83,'l',closed=True,index=201)
    pp.create_switch(net,7,4,'l',closed=True,index=305)
    pp.create_load(net,88,6.,2.,index=19)
    th=[conductor(83),conductor(4)]
    rs=[relay(201,pickup_a=100.,time_scale_s=100.,initial_memory=.15),
        relay(305,pickup_a=120.,time_scale_s=5.,initial_memory=.1)]
    req=request(net,th,rs,[segment(40.)]);got=run(req)
    equivalent=network(load=6.,reactive=2.,length=5.)
    I=electrical(equivalent,[conductor()],[relay()],[True],1.)['series_current_a'][0]
    rate=lambda r:((I/r['pickup_a'])**r['exponent']-1)/r['time_scale_s']
    trip=(1-rs[1]['initial_memory'])/rate(rs[1])
    assert len(got['events'])==1 and got['events'][0]['switch']==305
    compare(got['events'][0]['time_s'],trip,'time_s')
    endpoint=got['segments'][0]
    assert endpoint['switch_closed']==[True,False] and endpoint['locked']==[False,True]
    expected_u=(rs[0]['initial_memory']+rate(rs[0])*trip)*math.exp(-(40-trip)/rs[0]['reset_tau_s'])
    compare(endpoint['relay_memory'][0],expected_u,'relay_memory')
    compare(got['unserved_energy_mwh'],6.*(40-trip)/3600,'unserved_energy_mwh')
    for k,t in enumerate(th):
        warm=temperature_after(25.,trip,t,I*I*.2/1000,25.)
        expected=temperature_after(warm,40-trip,t,0.,25.)
        compare(endpoint['temperature_c'][k],expected,'temperature_c')


def test_reindexed_network_and_permuted_state_vectors_preserve_physics():
    """§§ 输入、输出与精度：非排序原生索引和状态列表置换保持物理结果及约定输出顺序。"""
    req=parallel_request(sensitivity=True);a=run(req);compare(a,analytic_response(req))
    net=pp.from_json_string(req['network_json'])
    net.bus=net.bus.iloc[::-1];net.line=net.line.iloc[::-1];net.switch=net.switch.iloc[::-1]
    changed=copy.deepcopy(req);changed['network_json']=pp.to_json(net)
    changed['thermal'].reverse();changed['relays'].reverse()
    # Each independently approximate result is checked against its own oracle;
    # no tighter one-answer tolerance is imposed between the two outputs.
    compare(run(changed),analytic_response(changed))


@pytest.mark.parametrize('change',['duplicate','pickup','memory','reclose','switch','bool','nan'])
def test_invalid_physical_contract_is_rejected(change):
    """§ 输入：重复保护、非法引用、越界参数、布尔数值和非有限数必须抛 ValueError。"""
    req=request()
    if change=='duplicate':req['relays'].append(copy.deepcopy(req['relays'][0]))
    elif change=='pickup':req['relays'][0]['pickup_a']=0.
    elif change=='memory':req['relays'][0]['initial_memory']=1.
    elif change=='reclose':req['relays'][0]['reclose_c']=200.
    elif change=='switch':req['relays'][0]['switch']=999
    elif change=='bool':req['segments'][0]['duration_s']=True
    elif change=='nan':req['thermal'][0]['emissivity']=float('nan')
    with pytest.raises(ValueError):candidate.analyze(req,timeout=60)


def test_cli_and_repeated_requests_are_independently_correct():
    """§ 输出与精度：交错重复调用无状态污染，CLI 成功输出 JSON、失败非零且仅 stderr。"""
    req=request(segments=[segment(45.)]);expected=closed_form(req)
    a=run(req);compare(a,expected)
    other=request(relays=[relay(auto_reclose=True)],segments=[segment(70.)])
    compare(run(other),closed_form(other))
    compare(run(req),expected)
    process=run_cli(json.dumps(req),timeout=60)
    assert process.returncode==0,process.stderr
    compare(json.loads(process.stdout),expected)
    bad=run_cli('{}',timeout=60)
    assert bad.returncode!=0 and not bad.stdout.strip() and bad.stderr.strip()


def native_request():
    net=pp.create_empty_network(sn_mva=100.)
    for idx,kv in [(41,110.),(7,20.),(88,20.),(2,20.),(301,10.),(400,.4)]:
        pp.create_bus(net,kv,index=idx)
    pp.create_ext_grid(net,41)
    pp.create_transformer3w_from_parameters(net,41,7,301,
        vn_hv_kv=110.,vn_mv_kv=20.,vn_lv_kv=10.,sn_hv_mva=40.,sn_mv_mva=25.,sn_lv_mva=15.,
        vk_hv_percent=10.,vk_mv_percent=10.,vk_lv_percent=10.,
        vkr_hv_percent=.4,vkr_mv_percent=.4,vkr_lv_percent=.4,pfe_kw=0.,i0_percent=0.,index=11)
    pp.create_switch(net,7,88,'b',closed=True,index=61)
    pp.create_line_from_parameters(net,88,2,3.,.25,.2,300.,1.,index=83)
    pp.create_line_from_parameters(net,88,2,3.,.3,.24,300.,1.,index=4)
    pp.create_switch(net,88,83,'l',closed=True,index=201)
    pp.create_switch(net,2,4,'l',closed=True,index=305)
    pp.create_load(net,2,7.5,3.75,scaling=.8,index=9)
    pp.create_load(net,301,1.9,.57,index=3)
    pp.create_sgen(net,2,.05,.02,index=91)
    pp.create_transformer_from_parameters(net,301,400,sn_mva=1.,vn_hv_kv=10.,vn_lv_kv=.4,
        vk_percent=4.,vkr_percent=1.,pfe_kw=0.,i0_percent=0.,index=222)
    pp.create_load(net,400,.1,.03,index=54)
    pp.create_gen(net,2,.4,vm_pu=1.03,min_q_mvar=-.3,max_q_mvar=.4,index=72)
    th=[conductor(4,initial_c=35.,alpha_per_k=.003,emissivity=.6,capacity_j_per_mk=45.),
        conductor(83,initial_c=65.,alpha_per_k=.004,emissivity=.7,capacity_j_per_mk=35.)]
    rs=[relay(305,pickup_a=100.,time_scale_s=25.,initial_memory=.2),
        relay(201,pickup_a=70.,time_scale_s=3.,initial_memory=.1)]
    return request(net,th,rs,[segment(6.,solar_w_per_m=2.,ambient_c=30.)])


def test_native_three_winding_q_limits_and_nonlinear_switching_converge_independently():
    """§§ 电气与热模型、输出：三绕组和母联、无功限值、非线性热切换符合独立收敛计算。"""
    from numeric_oracle import fixed_step
    req=native_request()
    coarse=fixed_step(req,.2);fine=fixed_step(req,.1)
    # Convergence of a different integrator is checked before judging submission.
    compare(coarse,fine)
    assert len(fine['events'])==1 and fine['events'][0]['switch']==201
    assert fine['segments'][0]['gen_q_mvar'][0]==pytest.approx(.4,abs=2e-5)
    compare(run(req),fine)
