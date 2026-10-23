"""Scalar quadrature benchmark for a nonlinear two-bus reclose trajectory.

This benchmark eliminates the two-bus AC circuit algebraically, then uses
temperature as the independent variable. No time integrator, runpp, candidate
module, or author event solver is used. The fixture cools monotonically on
each branch, so its event times and relay increments are one-dimensional
definite integrals with separated roots.
"""

import math

from scipy.integrate import quad
from scipy.optimize import brentq


def trajectory(req):
    """Return the complete trajectory for the single, zero-capacitance fixture."""
    th, relay, weather = req["thermal"][0], req["relays"][0], req["segments"][0]
    capacity = th["capacity_j_per_mk"]
    ambient = weather["ambient_c"]
    radiation = th["emissivity"] * 5.670374419e-8 * math.pi * th["diameter_m"]
    # The fixture has a 20 kV source, one 10 km, 0.2+j0.25 ohm/km line,
    # and a balanced 5+j2 MVA constant-power load. Index/angle are fixed by
    # the public test constructor; these are physical circuit coefficients.
    source_voltage = 20_000.0 / math.sqrt(3)
    power = complex(5e6, 2e6) / 3

    def current(temperature):
        resistance = 0.2 * (1 + th["alpha_per_k"] * (temperature - 20))
        impedance = complex(resistance, 0.25) * 10
        product = impedance * power.conjugate()
        b = 2 * product.real - source_voltage**2
        voltage_squared = (-b + math.sqrt(b * b - 4 * abs(product) ** 2)) / 2
        receiving_voltage = (voltage_squared + product).conjugate() / source_voltage
        return abs((source_voltage - receiving_voltage) / impedance)

    def derivative(temperature, connected):
        resistance = 0.2 * (1 + th["alpha_per_k"] * (temperature - 20))
        joule = current(temperature) ** 2 * resistance / 1000 if connected else 0.0
        heat = joule + weather["solar_w_per_m"]
        heat -= th["h_w_per_mk"] * (temperature - ambient)
        heat -= radiation * ((temperature + 273.15) ** 4 - (ambient + 273.15) ** 4)
        return heat / capacity

    def elapsed(start, end, connected):
        if not connected:
            # Factor the open-branch heat loss around its equilibrium. In log
            # temperature excess this remains smooth even after long cooling;
            # direct subtraction of quartic radiation terms would lose digits.
            equilibrium_k = equilibrium_open + 273.15

            def log_integrand(log_excess):
                temperature_k = equilibrium_k + math.exp(log_excess)
                divided_power = (
                    temperature_k**3
                    + temperature_k**2 * equilibrium_k
                    + temperature_k * equilibrium_k**2
                    + equilibrium_k**3
                )
                return -capacity / (th["h_w_per_mk"] + radiation * divided_power)

            return quad(
                log_integrand,
                math.log(start - equilibrium_open),
                math.log(end - equilibrium_open),
                epsabs=2e-10,
                epsrel=2e-11,
            )[0]
        return quad(
            lambda temperature: 1 / derivative(temperature, connected),
            start,
            end,
            epsabs=2e-10,
            epsrel=2e-11,
        )[0]

    def memory_increment(start, end):
        def integrand(temperature):
            rate = (current(temperature) / relay["pickup_a"]) ** relay["exponent"] - 1
            rate /= relay["time_scale_s"]
            return rate / derivative(temperature, True)

        return quad(integrand, start, end, epsabs=2e-11, epsrel=2e-11)[0]

    equilibrium_closed = brentq(
        lambda value: derivative(value, True), ambient, th["initial_c"]
    )
    equilibrium_open = brentq(
        lambda value: derivative(value, False), ambient, th["initial_c"]
    )
    assert ambient < equilibrium_closed < relay["reclose_c"] < th["initial_c"]
    assert current(equilibrium_closed) > relay["pickup_a"]

    def trip_from(start, memory):
        lower = equilibrium_closed + (start - equilibrium_closed) * 1e-5
        temperature = brentq(
            lambda value: memory_increment(start, value) - (1 - memory),
            lower,
            start,
            xtol=1e-11,
        )
        return temperature, elapsed(start, temperature, True)

    temperature1, trip1 = trip_from(th["initial_c"], relay["initial_memory"])
    cooling = elapsed(temperature1, relay["reclose_c"], False)
    assert cooling > relay["dead_time_s"] + 0.1
    reclose = trip1 + cooling
    memory_at_reclose = math.exp(-cooling / relay["reset_tau_s"])
    temperature2, second_run = trip_from(relay["reclose_c"], memory_at_reclose)
    trip2 = reclose + second_run
    duration = weather["duration_s"]
    assert trip1 > 0.05 and second_run > 0.05 and trip2 < duration - 0.05
    lower = equilibrium_open + (temperature2 - equilibrium_open) * 1e-10
    final_temperature = brentq(
        lambda value: elapsed(temperature2, value, False) - (duration - trip2),
        lower,
        temperature2,
        xtol=1e-11,
    )
    energy = 5.0 * (cooling + duration - trip2) / 3600
    events = [
        dict(
            time_s=trip1,
            switch=201,
            line=83,
            action="trip",
            cause="overcurrent",
            trip_count=1,
        ),
        dict(
            time_s=reclose,
            switch=201,
            line=83,
            action="reclose",
            cause="ready",
            trip_count=1,
        ),
        dict(
            time_s=trip2,
            switch=201,
            line=83,
            action="trip",
            cause="overcurrent",
            trip_count=2,
        ),
    ]
    return dict(
        line_order=[83],
        switch_order=[201],
        bus_order=[41, 7],
        gen_order=[],
        events=events,
        segments=[
            dict(
                start_s=0.0,
                end_s=duration,
                temperature_c=[final_temperature],
                relay_memory=[math.exp(-(duration - trip2) / relay["reset_tau_s"])],
                series_current_a=[0.0],
                relay_current_a=[0.0],
                switch_closed=[False],
                locked=[True],
                trip_count=[2],
                bus_vm_pu=[1.0, None],
                bus_va_degree=[3.0, None],
                gen_q_mvar=[],
                unserved_energy_mwh=energy,
            )
        ],
        unserved_energy_mwh=energy,
        sensitivity=None,
    )


def pickup_response(req, step):
    """Differentiate complete, independently recomputed quadrature event chains."""
    import copy

    plus, minus = copy.deepcopy(req), copy.deepcopy(req)
    plus["relays"][0]["pickup_a"] += step
    minus["relays"][0]["pickup_a"] -= step
    right, left = trajectory(plus), trajectory(minus)
    return dict(
        d_event_time_s_d_pickup_a=[
            (a["time_s"] - b["time_s"]) / (2 * step)
            for a, b in zip(right["events"], left["events"])
        ],
        d_final_temperature_c_d_pickup_a=[
            (
                right["segments"][0]["temperature_c"][0]
                - left["segments"][0]["temperature_c"][0]
            )
            / (2 * step)
        ],
        d_unserved_energy_mwh_d_pickup_a=(
            right["unserved_energy_mwh"] - left["unserved_energy_mwh"]
        )
        / (2 * step),
    )
