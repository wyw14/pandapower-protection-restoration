"""Author implementation of the documented electrothermal protection study."""

import copy
import math
import numbers
import numpy as np
from scipy.integrate import solve_ivp
from network_io import load_network
from ac_equilibrium import solve_equilibrium

SIGMA = 5.670374419e-8


def real(x, positive=False, nonnegative=False, temperature=False):
    if isinstance(x, bool) or not isinstance(x, numbers.Real):
        raise ValueError("finite real number required")
    x = float(x)
    if not math.isfinite(x) or (positive and x <= 0) or (nonnegative and x < 0):
        raise ValueError("physical number out of range")
    if temperature and x <= -273.15:
        raise ValueError("temperature below absolute zero")
    return x


def index(x, table):
    if (
        isinstance(x, bool)
        or not isinstance(x, numbers.Integral)
        or x not in table.index
    ):
        raise ValueError("unknown native integer index")
    return int(x)


def parse(request):
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    try:
        net = load_network(request["network_json"])
        for key in ("thermal", "relays", "segments"):
            if not isinstance(request[key], list) or not request[key]:
                raise ValueError("nonempty list required")
        thermal, seen = [], set()
        for item in request["thermal"]:
            d = dict(item)
            d["line"] = index(item["line"], net.line)
            if d["line"] in seen:
                raise ValueError("duplicate thermal line")
            seen.add(d["line"])
            for key in ("capacity_j_per_mk", "diameter_m"):
                d[key] = real(item[key], positive=True)
            for key in ("h_w_per_mk", "alpha_per_k"):
                d[key] = real(item[key], nonnegative=True)
            d["emissivity"] = real(item["emissivity"], nonnegative=True)
            if d["emissivity"] > 1:
                raise ValueError("emissivity exceeds one")
            for key in ("initial_c", "limit_c"):
                d[key] = real(item[key], temperature=True)
            row = net.line.loc[d["line"]]
            if (
                d["initial_c"] >= d["limit_c"]
                or 1 + d["alpha_per_k"] * (d["initial_c"] - 20) <= 0
            ):
                raise ValueError("invalid initial thermal state")
            real(row.r_ohm_per_km, positive=True)
            real(row.length_km, positive=True)
            if float(row.get("g_us_per_km", 0.0)) != 0:
                raise ValueError("tracked lines require zero shunt conductance")
            thermal.append(d)
        positions = {v["line"]: i for i, v in enumerate(thermal)}
        relays, switches, protected = [], set(), set()
        for item in request["relays"]:
            d = dict(item)
            s = index(item["switch"], net.switch)
            row = net.switch.loc[s]
            line = int(row.element)
            if (
                s in switches
                or row.et != "l"
                or not bool(row.closed)
                or line not in positions
            ):
                raise ValueError("invalid or duplicate protected switch")
            lr = net.line.loc[line]
            if (
                line in protected
                or not bool(lr.in_service)
                or row.bus not in (lr.from_bus, lr.to_bus)
            ):
                raise ValueError("invalid protection connection")
            d.update(switch=s, line=line, thermal_position=positions[line])
            for key in (
                "pickup_a",
                "exponent",
                "time_scale_s",
                "reset_tau_s",
                "dead_time_s",
            ):
                d[key] = real(item[key], positive=True)
            d["initial_memory"] = real(item["initial_memory"], nonnegative=True)
            if d["initial_memory"] >= 1 or type(item["auto_reclose"]) is not bool:
                raise ValueError("invalid protection initial state")
            d["reclose_c"] = real(item["reclose_c"], temperature=True)
            if d["reclose_c"] >= thermal[positions[line]]["limit_c"]:
                raise ValueError("reclose threshold must be below trip threshold")
            switches.add(s)
            protected.add(line)
            relays.append(d)
        segments = []
        for item in request["segments"]:
            segments.append(
                dict(
                    duration_s=real(item["duration_s"], positive=True),
                    ambient_c=real(item["ambient_c"], temperature=True),
                    solar_w_per_m=real(
                        item.get("solar_w_per_m", 0.0), nonnegative=True
                    ),
                    load_scale=real(item.get("load_scale", 1.0), nonnegative=True),
                )
            )
        target = request.get("sensitivity")
        if target is not None:
            s = index(target["switch"], net.switch)
            if s not in switches:
                raise ValueError("sensitivity target is not a protected switch")
            target = next(i for i, v in enumerate(relays) if v["switch"] == s)
        return net, thermal, relays, segments, target
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("malformed protection request") from exc


class Grid:
    def __init__(self, original, thermal, relays, segment, closed):
        self.net = copy.deepcopy(original)
        self.thermal, self.relays = thermal, relays
        self.segment, self.closed = segment, tuple(closed)
        self.r20 = np.array(
            [original.line.at[v["line"], "r_ohm_per_km"] for v in thermal]
        )
        self.alpha = np.array([v["alpha_per_k"] for v in thermal])
        self.net.load.loc[:, "p_mw"] *= segment["load_scale"]
        self.net.load.loc[:, "q_mvar"] *= segment["load_scale"]
        for relay, value in zip(relays, closed):
            self.net.switch.at[relay["switch"], "closed"] = bool(value)
        self.cache = None
        self.relevant = np.ones(len(thermal), dtype=bool)

    def electrical(self, temperature):
        r = self.r20 * (1 + self.alpha * (np.asarray(temperature) - 20.0))
        if not np.all(np.isfinite(r)) or np.any(r <= 0):
            raise RuntimeError("trajectory left positive resistance domain")
        key = tuple(r[self.relevant])
        if self.cache is not None and self.cache[0] == key:
            return self.cache[1]
        for item, resistance in zip(self.thermal, r):
            self.net.line.at[item["line"], "r_ohm_per_km"] = resistance
        # A phase-aware cold start retains native transformer phase shifts;
        # a zero-angle flat start can converge to a balanced low-voltage root.
        try:
            try:
                solve_equilibrium(
                    self.net, init="results" if self.cache is not None else "dc"
                )
            except Exception:
                solve_equilibrium(self.net, init="dc")
        except Exception as exc:
            raise RuntimeError("AC equilibrium failed") from exc
        series = []
        for item, resistance in zip(self.thermal, r):
            row = self.net.line.loc[item["line"]]
            loss = float(self.net.res_line.at[item["line"], "pl_mw"])
            if not bool(row.in_service) or not math.isfinite(loss):
                loss = 0.0
            if loss < -1e-8:
                raise RuntimeError("negative line resistive loss")
            series.append(
                math.sqrt(
                    max(loss, 0.0)
                    * 1e6
                    / (3 * row.parallel * row.length_km * resistance)
                )
            )
        measured = []
        for relay, closed in zip(self.relays, self.closed):
            if not closed:
                measured.append(0.0)
                continue
            switch = self.net.switch.loc[relay["switch"]]
            row = self.net.line.loc[relay["line"]]
            side = "from" if switch.bus == row.from_bus else "to"
            value = (
                float(self.net.res_line.at[relay["line"], "i_" + side + "_ka"]) * 1000
            )
            measured.append(value if math.isfinite(value) else 0.0)
        lost = 0.0
        for _, row in self.net.load.iterrows():
            if bool(row.in_service) and not math.isfinite(
                float(self.net.res_bus.at[row.bus, "vm_pu"])
            ):
                lost += max(float(row.p_mw) * float(row.scaling), 0.0)

        def values(table, column):
            return [
                float(v) if math.isfinite(float(v)) else None for v in table[column]
            ]

        vm = values(self.net.res_bus.reindex(self.net.bus.index), "vm_pu")
        va = values(self.net.res_bus.reindex(self.net.bus.index), "va_degree")
        # Upstream skips NR on isolated reference-only areas. Preserve their
        # explicitly given absolute reference angle, including fused buses.
        lookup = self.net._pd2ppc_lookups["bus"]
        for _, row in self.net.ext_grid.iterrows():
            if bool(row.in_service):
                mapped = lookup[int(row.bus)]
                for i, bus in enumerate(self.net.bus.index):
                    if vm[i] is not None and lookup[int(bus)] == mapped:
                        vm[i], va[i] = float(row.vm_pu), float(row.va_degree)
        result = dict(
            series_current_a=series,
            relay_current_a=measured,
            bus_vm_pu=vm,
            bus_va_degree=va,
            gen_q_mvar=values(self.net.res_gen.reindex(self.net.gen.index), "q_mvar"),
            unserved_mw=lost,
        )
        # A disconnected branch cannot affect this fixed topology's AC solution.
        # Keep backfed open-end pi branches active: BR_STATUS and solved endpoint
        # voltages, not merely the relay's closed flag, determine relevance.
        branch = self.net._ppc["branch"]
        buses = self.net._ppc["bus"]
        branch_start = self.net._pd2ppc_lookups["branch"]["line"][0]
        for i, item in enumerate(self.thermal):
            row = branch[branch_start + int(self.net.line.index.get_loc(item["line"]))]
            a, b = int(row[0].real), int(row[1].real)
            self.relevant[i] = (
                bool(row[10].real)
                and math.isfinite(buses[a, 7])
                and math.isfinite(buses[b, 7])
            )
        self.cache = tuple(r[self.relevant]), result
        return result

    def rhs(self, time, state):
        n, m = len(self.thermal), len(self.relays)
        temperature, memory = state[:n], state[n : n + m]
        point = self.electrical(temperature)
        rates = []
        ambient = self.segment["ambient_c"]
        for i, item in enumerate(self.thermal):
            r = self.r20[i] * (1 + item["alpha_per_k"] * (temperature[i] - 20.0))
            heat = (
                point["series_current_a"][i] ** 2 * r / 1000
                + self.segment["solar_w_per_m"]
                - item["h_w_per_mk"] * (temperature[i] - ambient)
                - item["emissivity"]
                * SIGMA
                * math.pi
                * item["diameter_m"]
                * ((temperature[i] + 273.15) ** 4 - (ambient + 273.15) ** 4)
            )
            rates.append(heat / item["capacity_j_per_mk"])
        for j, relay in enumerate(self.relays):
            current = point["relay_current_a"][j]
            if self.closed[j] and current > relay["pickup_a"]:
                rates.append(
                    ((current / relay["pickup_a"]) ** relay["exponent"] - 1)
                    / relay["time_scale_s"]
                )
            else:
                rates.append(-memory[j] / relay["reset_tau_s"])
        return np.array(rates + [point["unserved_mw"] / 3600.0])


def trajectory(net, thermal, relays, segments):
    n, m = len(thermal), len(relays)
    state = np.array(
        [v["initial_c"] for v in thermal]
        + [v["initial_memory"] for v in relays]
        + [0.0]
    )
    closed, counts = [True] * m, [0] * m
    deadlines = [math.inf] * m
    events, records = [], []
    time = 0.0
    for segment in segments:
        start, end = time, time + segment["duration_s"]
        while True:
            changed = False
            for j in sorted(range(m), key=lambda q: relays[q]["switch"]):
                relay = relays[j]
                k = relay["thermal_position"]
                action = cause = None
                if closed[j] and (
                    state[n + j] >= 1 - 1e-9 or state[k] >= thermal[k]["limit_c"] - 1e-8
                ):
                    cause = "overcurrent" if state[n + j] >= 1 - 1e-9 else "thermal"
                    action = "trip"
                    closed[j] = False
                    counts[j] += 1
                    deadlines[j] = (
                        time + relay["dead_time_s"]
                        if relay["auto_reclose"] and counts[j] == 1
                        else math.inf
                    )
                    if cause == "overcurrent":
                        state[n + j] = 1.0
                elif (
                    not closed[j]
                    and math.isfinite(deadlines[j])
                    and time >= deadlines[j] - 1e-9
                    and state[k] <= relay["reclose_c"] + 1e-8
                ):
                    action, cause = "reclose", "ready"
                    closed[j] = True
                    deadlines[j] = math.inf
                if action:
                    changed = True
                    events.append(
                        dict(
                            time_s=float(time),
                            switch=relay["switch"],
                            line=relay["line"],
                            action=action,
                            cause=cause,
                            trip_count=counts[j],
                        )
                    )
            if len(events) > 3 * m:
                raise RuntimeError("invalid protection transition cycle")
            grid = Grid(net, thermal, relays, segment, closed)
            if time >= end - 1e-9:
                break
            stop = min([end] + [v for v in deadlines if time + 1e-9 < v < end])
            roots = []
            for j, relay in enumerate(relays):
                k = relay["thermal_position"]
                if closed[j]:

                    def memory(t, y, j=j):
                        return y[n + j] - 1.0

                    memory.direction = 1
                    memory.terminal = True
                    roots.append(memory)

                    def temperature(t, y, k=k):
                        return y[k] - thermal[k]["limit_c"]

                    temperature.direction = 1
                    temperature.terminal = True
                    roots.append(temperature)
                elif math.isfinite(deadlines[j]) and time >= deadlines[j] - 1e-9:

                    def cooling(t, y, k=k, threshold=relay["reclose_c"]):
                        return y[k] - threshold

                    cooling.direction = -1
                    cooling.terminal = True
                    roots.append(cooling)
            integrated = solve_ivp(
                grid.rhs,
                (time, stop),
                state,
                method="DOP853",
                atol=1e-9,
                rtol=3e-9,
                max_step=min(60.0, stop - time),
                events=roots or None,
            )
            if not integrated.success or not np.all(np.isfinite(integrated.y[:, -1])):
                raise RuntimeError("hybrid trajectory integration failed")
            next_time = float(integrated.t[-1])
            if next_time <= time + 1e-12 and not changed:
                raise RuntimeError("hybrid solver did not advance")
            time, state = next_time, integrated.y[:, -1]
        point = grid.electrical(state[:n])
        records.append(
            dict(
                start_s=float(start),
                end_s=float(end),
                temperature_c=state[:n].tolist(),
                relay_memory=state[n : n + m].tolist(),
                switch_closed=list(closed),
                locked=[
                    not c and not math.isfinite(d) for c, d in zip(closed, deadlines)
                ],
                trip_count=list(counts),
                unserved_energy_mwh=float(state[-1]),
                **{k: v for k, v in point.items() if k != "unserved_mw"}
            )
        )
        time = end
    return dict(
        line_order=[v["line"] for v in thermal],
        switch_order=[v["switch"] for v in relays],
        bus_order=[int(v) for v in net.bus.index],
        gen_order=[int(v) for v in net.gen.index],
        events=events,
        segments=records,
        unserved_energy_mwh=float(state[-1]),
        sensitivity=None,
    )


def analyze(request):
    net, thermal, relays, segments, target = parse(request)
    result = trajectory(net, thermal, relays, segments)
    if target is not None:
        pickup = relays[target]["pickup_a"]
        step = min(max(pickup * 2e-4, 1e-4), pickup / 4)

        def signature(value):
            # Crossing a fixed load/weather boundary can change the derivative
            # without changing the ordered list of relay events.
            ends = [s["end_s"] for s in value["segments"]]
            return [
                (
                    e["switch"],
                    e["action"],
                    e["cause"],
                    e["trip_count"],
                    int(np.searchsorted(ends, e["time_s"], side="left")),
                )
                for e in value["events"]
            ]

        def response_vector(value):
            return np.array(
                [value["unserved_energy_mwh"]]
                + value["segments"][-1]["temperature_c"]
                + [e["time_s"] for e in value["events"]]
            )

        expected_signature = signature(result)
        # Use only a fraction of the public error budget for the step-convergence
        # estimate. The integration error is controlled separately by trajectory.
        absolute = np.array(
            [2e-8] + [5e-6] * len(thermal) + [2e-5] * len(result["events"])
        )
        relative = np.array(
            [5e-4] + [5e-4] * len(thermal) + [3e-4] * len(result["events"])
        )
        previous = None
        for _ in range(36):
            if pickup - step == pickup or pickup + step == pickup:
                break
            variants = []
            for direction in (-1, 1):
                changed = copy.deepcopy(relays)
                changed[target]["pickup_a"] = pickup + direction * step
                variants.append(trajectory(net, thermal, changed, segments))
            minus, plus = variants
            if signature(minus) == expected_signature == signature(plus):
                derivative = (response_vector(plus) - response_vector(minus)) / (
                    2 * step
                )
                if previous is not None:
                    error = np.abs(derivative - previous) / 3
                    if np.all(
                        error <= np.maximum(absolute, relative * np.abs(derivative))
                    ):
                        # Richardson removes the leading central-difference term.
                        derivative += (derivative - previous) / 3
                        result["sensitivity"] = dict(
                            d_unserved_energy_mwh_d_pickup_a=float(derivative[0]),
                            d_final_temperature_c_d_pickup_a=derivative[
                                1 : 1 + len(thermal)
                            ].tolist(),
                            d_event_time_s_d_pickup_a=derivative[
                                1 + len(thermal) :
                            ].tolist(),
                        )
                        break
                previous = derivative
            else:
                previous = None
            step /= 2
        else:
            raise RuntimeError(
                "sensitivity failed to converge on the local event branch"
            )
        if result["sensitivity"] is None:
            raise RuntimeError(
                "sensitivity reached floating-point resolution without convergence"
            )
    return result
