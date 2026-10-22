"""Independent closed-form oracle: two-bus parallel pi branches, alpha=eps=0.

No runpp, reference implementation or candidate import. Each fixed topology is
eliminated to a quadratic in receiving voltage squared; thermal/relay state
transitions are elementary exponentials and linear ramps.
"""
import copy
import math
import numpy as np
import pandapower as pp


def electrical(net, thermal, relays, closed, scale):
    source = net.ext_grid.iloc[0]
    from_bus = int(source.bus)
    to_bus = next(i for i in net.bus.index if i != from_bus)
    E = float(net.bus.at[from_bus, 'vn_kv'])*1000/math.sqrt(3)*float(source.vm_pu)
    P = sum(float(r.p_mw)*float(r.scaling) for _,r in net.load.iterrows() if r.in_service)*scale*1e6/3
    Q = sum(float(r.q_mvar)*float(r.scaling) for _,r in net.load.iterrows() if r.in_service)*scale*1e6/3
    if len(net.sgen):
        P -= sum(float(r.p_mw)*float(r.scaling) for _,r in net.sgen.iterrows() if r.in_service)*1e6/3
        Q -= sum(float(r.q_mvar)*float(r.scaling) for _,r in net.sgen.iterrows() if r.in_service)*1e6/3
    relay_for = {int(net.switch.at[r['switch'],'element']):j for j,r in enumerate(relays)}
    lines = {}
    Y, shunt = 0j, 0j
    for idx,row in net.line.iterrows():
        z = complex(row.r_ohm_per_km,row.x_ohm_per_km)*row.length_km
        totalz = z/row.parallel
        B = math.pi*net.f_hz*row.c_nf_per_km*row.length_km*1e-9*row.parallel
        j = relay_for.get(int(idx))
        connected = True if j is None else bool(closed[j])
        side = None if j is None else ('from' if net.switch.at[relays[j]['switch'],'bus']==row.from_bus else 'to')
        lines[int(idx)] = (z,totalz,B,float(row.parallel),connected,side)
        if connected:
            Y += 1/totalz; shunt += 1j*B
        elif side == 'from':
            shunt += 1j*B + 1j*B/(1+1j*B*totalz)
    if Y:
        Z = 1/Y; S = complex(P,Q); A = 1+Z*shunt; D = Z*S.conjugate()
        aa=abs(A)**2; bb=2*(A*D.conjugate()).real-E*E; cc=abs(D)**2
        disc=bb*bb-4*aa*cc
        assert disc > 0
        y=(-bb+math.sqrt(disc))/(2*aa)
        V=(A*y+D).conjugate()/E
    else:
        V=None
    series, ports = {}, {}
    for idx,(z,totalz,B,p,connected,side) in lines.items():
        if connected and V is not None:
            a,b=complex(E),V
        elif not connected and side=='to':
            a,b=complex(E),E/(1+1j*B*totalz)
        elif not connected and side=='from' and V is not None:
            a,b=V/(1+1j*B*totalz),V
        else:
            a=b=0j
        series[idx]=abs((a-b)/z)
        ports[idx]=(abs(p*(a-b)/z+1j*B*a),abs(p*(b-a)/z+1j*B*b))
    measured=[]
    for j,r in enumerate(relays):
        sw=net.switch.loc[r['switch']]; row=net.line.loc[int(sw.element)]
        side=0 if sw.bus==row.from_bus else 1
        measured.append(ports[int(sw.element)][side] if closed[j] else 0.)
    angle=float(source.va_degree)
    vm=[float(source.vm_pu) if b==from_bus else (abs(V)/E*float(source.vm_pu) if V is not None else None)
        for b in net.bus.index]
    va=[angle if b==from_bus else (math.degrees(math.atan2(V.imag,V.real))+angle if V is not None else None)
        for b in net.bus.index]
    demand=sum(float(r.p_mw)*float(r.scaling) for _,r in net.load.iterrows() if r.in_service)*scale
    return dict(series_current_a=[series[v['line']] for v in thermal],
                relay_current_a=measured,bus_vm_pu=vm,bus_va_degree=va,gen_q_mvar=[],
                unserved_mw=demand if V is None else 0.)


def temperature_after(T, dt, th, heat, ambient):
    h,C=th['h_w_per_mk'],th['capacity_j_per_mk']
    if h==0:
        return T+heat*dt/C
    equilibrium=ambient+heat/h
    return equilibrium+(T-equilibrium)*math.exp(-h*dt/C)


def time_to_temperature(T, target, th, heat, ambient, upward):
    if (upward and T>=target) or (not upward and T<=target):
        return 0.
    h,C=th['h_w_per_mk'],th['capacity_j_per_mk']
    if h==0:
        slope=heat/C
        result=(target-T)/slope if slope else math.inf
        return result if result>0 else math.inf
    equilibrium=ambient+heat/h
    fraction=(target-equilibrium)/(T-equilibrium) if T!=equilibrium else -1
    return -math.log(fraction)*C/h if 0<fraction<1 else math.inf


def closed_form(req):
    net=pp.from_json_string(req['network_json'])
    thermal,relays=req['thermal'],req['relays']; n,m=len(thermal),len(relays)
    assert all(v['alpha_per_k']==0 and v['emissivity']==0 for v in thermal)
    positions={v['line']:i for i,v in enumerate(thermal)}
    associated=[positions[int(net.switch.at[r['switch'],'element'])] for r in relays]
    T=[v['initial_c'] for v in thermal]; u=[v['initial_memory'] for v in relays]
    closed=[True]*m; counts=[0]*m; ready=[math.inf]*m; events=[]; records=[]; time=0.; energy=0.
    for seg in req['segments']:
        start=time; end=start+seg['duration_s']; ambient=seg['ambient_c']
        while time < end-1e-10:
            point=electrical(net,thermal,relays,closed,seg.get('load_scale',1.))
            heats=[I*I*net.line.at[v['line'],'r_ohm_per_km']/1000+seg.get('solar_w_per_m',0.)
                   for I,v in zip(point['series_current_a'],thermal)]
            rates=[((I/r['pickup_a'])**r['exponent']-1)/r['time_scale_s'] if c and I>r['pickup_a'] else None
                   for I,r,c in zip(point['relay_current_a'],relays,closed)]
            options=[]
            for j,r in enumerate(relays):
                k=associated[j]
                if closed[j]:
                    if rates[j] is not None: options.append(((1-u[j])/rates[j],j,'trip','overcurrent'))
                    delay=time_to_temperature(T[k],thermal[k]['limit_c'],thermal[k],heats[k],ambient,True)
                    options.append((delay,j,'trip','thermal'))
                elif math.isfinite(ready[j]):
                    cool=time_to_temperature(T[k],r['reclose_c'],thermal[k],heats[k],ambient,False)
                    options.append((max(ready[j]-time,cool,0.),j,'reclose','ready'))
            event=min(options,default=(math.inf,0,'',''))
            dt=min(end-time,max(event[0],0.))
            T=[temperature_after(t,dt,v,h,ambient) for t,v,h in zip(T,thermal,heats)]
            u=[x+rate*dt if rate is not None else x*math.exp(-dt/r['reset_tau_s'])
               for x,rate,r in zip(u,rates,relays)]
            energy+=point['unserved_mw']*dt/3600.; time+=dt
            if event[0] <= dt+1e-9 and time<end-1e-9:
                _,j,action,cause=event; r=relays[j]
                if action=='trip':
                    counts[j]+=1;closed[j]=False
                    ready[j]=time+r['dead_time_s'] if r['auto_reclose'] and counts[j]==1 else math.inf
                else:
                    closed[j]=True;ready[j]=math.inf
                events.append(dict(time_s=time,switch=r['switch'],line=thermal[associated[j]]['line'],
                                   action=action,cause=cause,trip_count=counts[j]))
                assert len(events)<=3*m
            else:
                time=end
        point=electrical(net,thermal,relays,closed,seg.get('load_scale',1.))
        records.append(dict(start_s=start,end_s=end,temperature_c=T.copy(),relay_memory=u.copy(),
                            switch_closed=closed.copy(),trip_count=counts.copy(),
                            locked=[not c and not math.isfinite(d) for c,d in zip(closed,ready)],
                            unserved_energy_mwh=energy,**{k:v for k,v in point.items() if k!='unserved_mw'}))
    return dict(line_order=[v['line'] for v in thermal],switch_order=[r['switch'] for r in relays],
                bus_order=list(net.bus.index),gen_order=list(net.gen.index),events=events,segments=records,
                unserved_energy_mwh=energy,sensitivity=None)


def analytic_response(req):
    """Differentiate the closed-form event chain, without a numerical AC/ODE solver."""
    nominal=closed_form(req)
    target=req['sensitivity']['switch']; j=next(i for i,r in enumerate(req['relays']) if r['switch']==target)
    h=req['relays'][j]['pickup_a']*1e-5
    pair=[]
    for sign in (-1,1):
        changed=copy.deepcopy(req);changed['relays'][j]['pickup_a']+=sign*h
        pair.append(closed_form(changed))
    a,b=pair
    nominal['sensitivity']=dict(
       d_unserved_energy_mwh_d_pickup_a=(b['unserved_energy_mwh']-a['unserved_energy_mwh'])/(2*h),
       d_final_temperature_c_d_pickup_a=((np.array(b['segments'][-1]['temperature_c'])-
                                         np.array(a['segments'][-1]['temperature_c']))/(2*h)).tolist(),
       d_event_time_s_d_pickup_a=[(y['time_s']-x['time_s'])/(2*h) for x,y in zip(a['events'],b['events'])])
    return nominal
