"""Independent closed-form oracle: two-bus parallel pi branches, alpha=eps=0.

No runpp, reference implementation or candidate import. Each fixed topology is
eliminated to a quadratic in receiving voltage squared; thermal/relay state
transitions are elementary exponentials and linear ramps.
"""

import math
import numpy as np
import pandapower as pp


def electrical(net, thermal, relays, closed, scale):
    source = net.ext_grid.iloc[0]
    from_bus = int(source.bus)
    if len(net.bus) != 2:
        raise ValueError("The closed-form circuit oracle requires exactly two buses")
    E = float(net.bus.at[from_bus, "vn_kv"]) * 1000 / math.sqrt(3) * float(source.vm_pu)
    P = (
        sum(
            float(r.p_mw) * float(r.scaling)
            for _, r in net.load.iterrows()
            if r.in_service
        )
        * scale
        * 1e6
        / 3
    )
    Q = (
        sum(
            float(r.q_mvar) * float(r.scaling)
            for _, r in net.load.iterrows()
            if r.in_service
        )
        * scale
        * 1e6
        / 3
    )
    if len(net.sgen):
        P -= (
            sum(
                float(r.p_mw) * float(r.scaling)
                for _, r in net.sgen.iterrows()
                if r.in_service
            )
            * 1e6
            / 3
        )
        Q -= (
            sum(
                float(r.q_mvar) * float(r.scaling)
                for _, r in net.sgen.iterrows()
                if r.in_service
            )
            * 1e6
            / 3
        )
    relay_for = {
        int(net.switch.at[r["switch"], "element"]): j for j, r in enumerate(relays)
    }
    lines = {}
    Y, shunt = 0j, 0j
    for idx, row in net.line.iterrows():
        z = complex(row.r_ohm_per_km, row.x_ohm_per_km) * row.length_km
        totalz = z / row.parallel
        B = math.pi * net.f_hz * row.c_nf_per_km * row.length_km * 1e-9 * row.parallel
        j = relay_for.get(int(idx))
        connected = True if j is None else bool(closed[j])
        side = (
            None
            if j is None
            else (
                "from"
                if net.switch.at[relays[j]["switch"], "bus"] == row.from_bus
                else "to"
            )
        )
        lines[int(idx)] = (z, totalz, B, float(row.parallel), connected, side)
        if connected:
            Y += 1 / totalz
            shunt += 1j * B
        elif side == "from":
            shunt += 1j * B + 1j * B / (1 + 1j * B * totalz)
    if Y:
        Z = 1 / Y
        S = complex(P, Q)
        A = 1 + Z * shunt
        D = Z * S.conjugate()
        aa = abs(A) ** 2
        bb = 2 * (A * D.conjugate()).real - E * E
        cc = abs(D) ** 2
        disc = bb * bb - 4 * aa * cc
        if disc <= 0:
            raise ValueError("Closed-form fixture has no separated high-voltage root")
        y = (-bb + math.sqrt(disc)) / (2 * aa)
        V = (A * y + D).conjugate() / E
    else:
        V = None
    series, ports = {}, {}
    for idx, (z, totalz, B, p, connected, side) in lines.items():
        if connected and V is not None:
            a, b = complex(E), V
        elif not connected and side == "to":
            a, b = complex(E), E / (1 + 1j * B * totalz)
        elif not connected and side == "from" and V is not None:
            a, b = V / (1 + 1j * B * totalz), V
        else:
            a = b = 0j
        series[idx] = abs((a - b) / z)
        ports[idx] = (
            abs(p * (a - b) / z + 1j * B * a),
            abs(p * (b - a) / z + 1j * B * b),
        )
    measured = []
    for j, r in enumerate(relays):
        sw = net.switch.loc[r["switch"]]
        row = net.line.loc[int(sw.element)]
        side = 0 if sw.bus == row.from_bus else 1
        measured.append(ports[int(sw.element)][side] if closed[j] else 0.0)
    angle = float(source.va_degree)
    vm = [
        (
            float(source.vm_pu)
            if b == from_bus
            else (abs(V) / E * float(source.vm_pu) if V is not None else None)
        )
        for b in net.bus.index
    ]
    va = [
        (
            angle
            if b == from_bus
            else (
                math.degrees(math.atan2(V.imag, V.real)) + angle
                if V is not None
                else None
            )
        )
        for b in net.bus.index
    ]
    demand = (
        sum(
            float(r.p_mw) * float(r.scaling)
            for _, r in net.load.iterrows()
            if r.in_service
        )
        * scale
    )
    return dict(
        series_current_a=[series[v["line"]] for v in thermal],
        relay_current_a=measured,
        bus_vm_pu=vm,
        bus_va_degree=va,
        gen_q_mvar=[],
        unserved_mw=demand if V is None else 0.0,
    )


def temperature_after(T, dt, th, heat, ambient):
    h, C = th["h_w_per_mk"], th["capacity_j_per_mk"]
    if h == 0:
        return T + heat * dt / C
    equilibrium = ambient + heat / h
    return equilibrium + (T - equilibrium) * math.exp(-h * dt / C)


def time_to_temperature(T, target, th, heat, ambient, upward):
    if (upward and T >= target) or (not upward and T <= target):
        return 0.0
    h, C = th["h_w_per_mk"], th["capacity_j_per_mk"]
    if h == 0:
        slope = heat / C
        result = (target - T) / slope if slope else math.inf
        return result if result > 0 else math.inf
    equilibrium = ambient + heat / h
    fraction = (target - equilibrium) / (T - equilibrium) if T != equilibrium else -1
    return -math.log(fraction) * C / h if 0 < fraction < 1 else math.inf


def closed_form(req):
    net = pp.from_json_string(req["network_json"])
    thermal, relays = req["thermal"], req["relays"]
    m = len(relays)
    if any(v["alpha_per_k"] != 0 or v["emissivity"] != 0 for v in thermal):
        raise ValueError(
            "Closed-form fixture must have constant resistance and linear cooling"
        )
    positions = {v["line"]: i for i, v in enumerate(thermal)}
    associated = [positions[int(net.switch.at[r["switch"], "element"])] for r in relays]
    T = [v["initial_c"] for v in thermal]
    u = [v["initial_memory"] for v in relays]
    closed = [True] * m
    counts = [0] * m
    ready = [math.inf] * m
    events = []
    records = []
    time = 0.0
    energy = 0.0
    for seg in req["segments"]:
        start = time
        end = start + seg["duration_s"]
        ambient = seg["ambient_c"]
        while time < end - 1e-10:
            point = electrical(net, thermal, relays, closed, seg.get("load_scale", 1.0))
            heats = [
                current * current * net.line.at[v["line"], "r_ohm_per_km"] / 1000
                + seg.get("solar_w_per_m", 0.0)
                for current, v in zip(point["series_current_a"], thermal)
            ]
            rates = [
                (
                    ((current / r["pickup_a"]) ** r["exponent"] - 1) / r["time_scale_s"]
                    if c and current > r["pickup_a"]
                    else None
                )
                for current, r, c in zip(point["relay_current_a"], relays, closed)
            ]
            options = []
            for j, r in enumerate(relays):
                k = associated[j]
                if closed[j]:
                    if rates[j] is not None:
                        options.append(
                            ((1 - u[j]) / rates[j], j, "trip", "overcurrent")
                        )
                    delay = time_to_temperature(
                        T[k], thermal[k]["limit_c"], thermal[k], heats[k], ambient, True
                    )
                    options.append((delay, j, "trip", "thermal"))
                elif math.isfinite(ready[j]):
                    cool = time_to_temperature(
                        T[k], r["reclose_c"], thermal[k], heats[k], ambient, False
                    )
                    options.append(
                        (max(ready[j] - time, cool, 0.0), j, "reclose", "ready")
                    )
            event = min(options, default=(math.inf, 0, "", ""))
            dt = min(end - time, max(event[0], 0.0))
            T = [
                temperature_after(t, dt, v, h, ambient)
                for t, v, h in zip(T, thermal, heats)
            ]
            u = [
                (
                    x + rate * dt
                    if rate is not None
                    else x * math.exp(-dt / r["reset_tau_s"])
                )
                for x, rate, r in zip(u, rates, relays)
            ]
            energy += point["unserved_mw"] * dt / 3600.0
            time += dt
            if event[0] <= dt + 1e-9 and time < end - 1e-9:
                _, j, action, cause = event
                r = relays[j]
                if action == "trip":
                    counts[j] += 1
                    closed[j] = False
                    ready[j] = (
                        time + r["dead_time_s"]
                        if r["auto_reclose"] and counts[j] == 1
                        else math.inf
                    )
                else:
                    closed[j] = True
                    ready[j] = math.inf
                events.append(
                    dict(
                        time_s=time,
                        switch=r["switch"],
                        line=thermal[associated[j]]["line"],
                        action=action,
                        cause=cause,
                        trip_count=counts[j],
                    )
                )
                if len(events) > 3 * m:
                    raise RuntimeError(
                        "Closed-form event chain exceeded one reclose per relay"
                    )
            else:
                time = end
        point = electrical(net, thermal, relays, closed, seg.get("load_scale", 1.0))
        records.append(
            dict(
                start_s=start,
                end_s=end,
                temperature_c=T.copy(),
                relay_memory=u.copy(),
                switch_closed=closed.copy(),
                trip_count=counts.copy(),
                locked=[not c and not math.isfinite(d) for c, d in zip(closed, ready)],
                unserved_energy_mwh=energy,
                **{k: v for k, v in point.items() if k != "unserved_mw"},
            )
        )
    return dict(
        line_order=[v["line"] for v in thermal],
        switch_order=[r["switch"] for r in relays],
        bus_order=list(net.bus.index),
        gen_order=list(net.gen.index),
        events=events,
        segments=records,
        unserved_energy_mwh=energy,
        sensitivity=None,
    )


def analytic_response(req):
    """Analytic tangent of each selected elementary event, with no differencing.

    These fixtures have alpha=emissivity=0, so current is constant between
    topology/load changes and independent of pickup on each fixed topology.
    Differentiate the exact ramp, exponential, and event-time equations. This
    calculation never samples across an event or external-segment boundary.
    """
    nominal = closed_form(req)
    net = pp.from_json_string(req["network_json"])
    thermal, relays = req["thermal"], req["relays"]
    target = req["sensitivity"]["switch"]
    positions = {v["line"]: i for i, v in enumerate(thermal)}
    associated = [positions[int(net.switch.at[r["switch"], "element"])] for r in relays]
    temperature = np.array([v["initial_c"] for v in thermal], dtype=float)
    memory = np.array([v["initial_memory"] for v in relays], dtype=float)
    d_temperature = np.zeros(len(thermal))
    d_memory = np.zeros(len(relays))
    closed = [True] * len(relays)
    counts = [0] * len(relays)
    ready = [math.inf] * len(relays)
    d_ready = np.zeros(len(relays))
    time = d_time = d_energy = 0.0
    events = []
    event_derivatives = []
    for seg in req["segments"]:
        end = time + seg["duration_s"]
        ambient = seg["ambient_c"]
        while time < end - 1e-10:
            point = electrical(net, thermal, relays, closed, seg.get("load_scale", 1.0))
            heats = [
                current**2 * net.line.at[item["line"], "r_ohm_per_km"] / 1000
                + seg.get("solar_w_per_m", 0.0)
                for current, item in zip(point["series_current_a"], thermal)
            ]
            rates = []
            d_rates = []
            for current, relay, is_closed in zip(
                point["relay_current_a"], relays, closed
            ):
                if is_closed and current > relay["pickup_a"]:
                    ratio = (current / relay["pickup_a"]) ** relay["exponent"]
                    rates.append((ratio - 1) / relay["time_scale_s"])
                    d_rates.append(
                        -relay["exponent"]
                        * ratio
                        / (relay["pickup_a"] * relay["time_scale_s"])
                        if relay["switch"] == target
                        else 0.0
                    )
                else:
                    rates.append(None)
                    d_rates.append(0.0)

            def temperature_delay(k, threshold, upward):
                item = thermal[k]
                delay = time_to_temperature(
                    temperature[k], threshold, item, heats[k], ambient, upward
                )
                if delay == 0 or not math.isfinite(delay):
                    return delay, 0.0
                decay = math.exp(
                    -item["h_w_per_mk"] * delay / item["capacity_j_per_mk"]
                )
                slope = (heats[k] - item["h_w_per_mk"] * (threshold - ambient)) / item[
                    "capacity_j_per_mk"
                ]
                return delay, -decay * d_temperature[k] / slope

            options = []
            for j, relay in enumerate(relays):
                k = associated[j]
                if closed[j]:
                    if rates[j] is not None:
                        delay = (1 - memory[j]) / rates[j]
                        derivative = (
                            -d_memory[j] / rates[j] - delay * d_rates[j] / rates[j]
                        )
                        options.append((delay, j, "trip", "overcurrent", derivative))
                    delay, derivative = temperature_delay(
                        k, thermal[k]["limit_c"], True
                    )
                    options.append((delay, j, "trip", "thermal", derivative))
                elif math.isfinite(ready[j]):
                    cooling, d_cooling = temperature_delay(k, relay["reclose_c"], False)
                    delay, derivative = max(
                        (ready[j] - time, d_ready[j] - d_time),
                        (cooling, d_cooling),
                        (0.0, 0.0),
                        key=lambda pair: pair[0],
                    )
                    options.append((delay, j, "reclose", "ready", derivative))
            event = min(
                options, key=lambda item: item[:4], default=(math.inf, 0, "", "", 0.0)
            )
            has_event = event[0] < end - time - 1e-9
            dt = max(event[0], 0.0) if has_event else end - time
            d_dt = event[4] if has_event else -d_time
            for k, item in enumerate(thermal):
                temperature[k] = temperature_after(
                    temperature[k], dt, item, heats[k], ambient
                )
                decay = math.exp(-item["h_w_per_mk"] * dt / item["capacity_j_per_mk"])
                slope = (
                    heats[k] - item["h_w_per_mk"] * (temperature[k] - ambient)
                ) / item["capacity_j_per_mk"]
                d_temperature[k] = decay * d_temperature[k] + slope * d_dt
            for j, relay in enumerate(relays):
                if rates[j] is None:
                    decay = math.exp(-dt / relay["reset_tau_s"])
                    d_memory[j] = decay * (
                        d_memory[j] - memory[j] * d_dt / relay["reset_tau_s"]
                    )
                    memory[j] *= decay
                else:
                    d_memory[j] += d_rates[j] * dt + rates[j] * d_dt
                    memory[j] += rates[j] * dt
            d_energy += point["unserved_mw"] * d_dt / 3600.0
            time += dt
            d_time += d_dt
            if has_event:
                _, j, action, cause, _ = event
                relay = relays[j]
                if action == "trip":
                    counts[j] += 1
                    closed[j] = False
                    ready[j] = (
                        time + relay["dead_time_s"]
                        if relay["auto_reclose"] and counts[j] == 1
                        else math.inf
                    )
                    d_ready[j] = d_time
                else:
                    closed[j] = True
                    ready[j] = math.inf
                events.append((relay["switch"], action, cause, counts[j]))
                event_derivatives.append(d_time)
            else:
                time = end
                d_time = 0.0
    expected_events = [
        (event["switch"], event["action"], event["cause"], event["trip_count"])
        for event in nominal["events"]
    ]
    if events != expected_events:
        raise RuntimeError(
            f"Tangent branch {events!r} differs from nominal branch {expected_events!r}"
        )
    nominal["sensitivity"] = dict(
        d_unserved_energy_mwh_d_pickup_a=float(d_energy),
        d_final_temperature_c_d_pickup_a=d_temperature.tolist(),
        d_event_time_s_d_pickup_a=event_derivatives,
    )
    return nominal
