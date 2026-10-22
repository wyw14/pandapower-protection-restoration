"""Independent fixed-midpoint / bracketed-event oracle for a small native case.

AC equilibria share the upstream library, but series currents use complex branch
voltage drops and the time/event algorithm is separate from the author solver.
This oracle's cases disable reclose; exact reclose chains have closed forms.
"""

import copy
import math
import numpy as np
import pandapower as pp
from pandapower.pypower.idx_bus import VM, VA, BASE_KV
from pandapower.pypower.idx_brch import F_BUS, T_BUS, BR_R, BR_X, BR_STATUS


def point(original, thermal, relays, segment, T, closed):
    net = copy.deepcopy(original)
    net.load.p_mw *= segment.get("load_scale", 1.0)
    net.load.q_mvar *= segment.get("load_scale", 1.0)
    for item, t in zip(thermal, T):
        net.line.at[item["line"], "r_ohm_per_km"] *= 1 + item["alpha_per_k"] * (
            t - 20.0
        )
    for item, c in zip(relays, closed):
        net.switch.at[item["switch"], "closed"] = c
    pp.runpp(
        net,
        enforce_q_lims=True,
        calculate_voltage_angles=True,
        trafo_model="t",
        voltage_depend_loads=False,
        numba=False,
        lightsim2grid=False,
        consider_line_temperature=False,
        tdpf=False,
        init="dc",
        tolerance_mva=1e-10,
        max_iteration=50,
    )
    bus, branch = net._ppc["bus"], net._ppc["branch"]
    start = net._pd2ppc_lookups["branch"]["line"][0]
    currents = []
    for item in thermal:
        row = branch[start + int(net.line.index.get_loc(item["line"]))]
        a, b = int(row[F_BUS].real), int(row[T_BUS].real)
        z = (
            complex(row[BR_R].real, row[BR_X].real)
            * net.line.at[item["line"], "parallel"]
        )
        if not row[BR_STATUS].real or not np.isfinite(bus[[a, b], VM]).all():
            currents.append(0.0)
        else:
            V = bus[[a, b], VM] * np.exp(1j * np.deg2rad(bus[[a, b], VA]))
            currents.append(
                abs((V[0] - V[1]) / z)
                * net.sn_mva
                / (math.sqrt(3) * bus[a, BASE_KV])
                * 1000
            )
    measured = []
    for item, c in zip(relays, closed):
        sw = net.switch.loc[item["switch"]]
        line = net.line.loc[int(sw.element)]
        side = "from" if sw.bus == line.from_bus else "to"
        value = float(net.res_line.at[int(sw.element), "i_" + side + "_ka"]) * 1000
        measured.append(value if c and math.isfinite(value) else 0.0)
    lost = sum(
        float(row.p_mw) * float(row.scaling)
        for _, row in net.load.iterrows()
        if row.in_service and not math.isfinite(float(net.res_bus.at[row.bus, "vm_pu"]))
    )

    def finite(values):
        return [float(v) if math.isfinite(float(v)) else None for v in values]

    return dict(
        series_current_a=currents,
        relay_current_a=measured,
        bus_vm_pu=finite(net.res_bus.vm_pu),
        bus_va_degree=finite(net.res_bus.va_degree),
        gen_q_mvar=finite(net.res_gen.q_mvar),
        unserved_mw=lost,
    )


def fixed_step(req, dt):
    original = pp.from_json_string(req["network_json"])
    ths = req["thermal"]
    rs = req["relays"]
    n = len(ths)
    m = len(rs)
    if any(r["auto_reclose"] for r in rs):
        raise ValueError(
            "Fixed-midpoint oracle supports fixtures without automatic reclose"
        )
    positions = {v["line"]: i for i, v in enumerate(ths)}
    associated = [
        positions[int(original.switch.at[r["switch"], "element"])] for r in rs
    ]
    state = np.array(
        [v["initial_c"] for v in ths] + [r["initial_memory"] for r in rs] + [0.0]
    )
    closed = [True] * m
    counts = [0] * m
    events = []
    records = []
    clock = 0.0
    for segment in req["segments"]:
        end = clock + segment["duration_s"]
        start = clock

        def rhs(y):
            p = point(original, ths, rs, segment, y[:n], closed)
            result = []
            ambient = segment["ambient_c"]
            for k, th in enumerate(ths):
                r = original.line.at[th["line"], "r_ohm_per_km"] * (
                    1 + th["alpha_per_k"] * (y[k] - 20.0)
                )
                heat = p["series_current_a"][k] ** 2 * r / 1000 + segment.get(
                    "solar_w_per_m", 0.0
                )
                heat -= th["h_w_per_mk"] * (y[k] - ambient)
                heat -= (
                    th["emissivity"]
                    * 5.670374419e-8
                    * math.pi
                    * th["diameter_m"]
                    * ((y[k] + 273.15) ** 4 - (ambient + 273.15) ** 4)
                )
                result.append(heat / th["capacity_j_per_mk"])
            for j, r in enumerate(rs):
                current = p["relay_current_a"][j]
                result.append(
                    ((current / r["pickup_a"]) ** r["exponent"] - 1) / r["time_scale_s"]
                    if closed[j] and current > r["pickup_a"]
                    else -y[n + j] / r["reset_tau_s"]
                )
            return np.array(result + [p["unserved_mw"] / 3600])

        while clock < end - 1e-10:
            width = min(dt, end - clock)
            first = rhs(state)

            def advance(t):
                return state + t * rhs(state + t / 2 * first)

            endpoint = advance(width)
            possible = []
            for j, r in enumerate(rs):
                if not closed[j]:
                    continue
                k = associated[j]
                for coordinate, threshold, cause in (
                    (n + j, 1.0, "overcurrent"),
                    (k, ths[k]["limit_c"], "thermal"),
                ):
                    if endpoint[coordinate] >= threshold:
                        lo, hi = 0.0, width
                        for _ in range(24):
                            mid = (lo + hi) / 2
                            if advance(mid)[coordinate] >= threshold:
                                hi = mid
                            else:
                                lo = mid
                        possible.append((hi, j, cause))
            if possible:
                width, j, cause = min(possible)
                endpoint = advance(width)
                closed[j] = False
                counts[j] += 1
                events.append(
                    dict(
                        time_s=clock + width,
                        switch=rs[j]["switch"],
                        line=ths[associated[j]]["line"],
                        action="trip",
                        cause=cause,
                        trip_count=counts[j],
                    )
                )
            state = endpoint
            clock += width
        p = point(original, ths, rs, segment, state[:n], closed)
        records.append(
            dict(
                start_s=start,
                end_s=end,
                temperature_c=state[:n].tolist(),
                relay_memory=state[n : n + m].tolist(),
                switch_closed=closed.copy(),
                locked=[not c for c in closed],
                trip_count=counts.copy(),
                unserved_energy_mwh=float(state[-1]),
                **{k: v for k, v in p.items() if k != "unserved_mw"}
            )
        )
    return dict(
        line_order=[v["line"] for v in ths],
        switch_order=[r["switch"] for r in rs],
        bus_order=list(original.bus.index),
        gen_order=list(original.gen.index),
        events=events,
        segments=records,
        unserved_energy_mwh=float(state[-1]),
        sensitivity=None,
    )
