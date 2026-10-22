"""Existing service: one native balanced AC operating point.

The caller owns operating state. This helper implements neither protection nor
thermal histories, event transitions or response derivatives.
"""
import pandapower as pp


def solve_equilibrium(net, init='flat'):
    net.pop('user_pf_options', None)
    pp.runpp(net, algorithm='nr', calculate_voltage_angles=True, init=init,
             max_iteration=50, tolerance_mva=1e-10, trafo_model='t',
             enforce_q_lims=True, voltage_depend_loads=False,
             check_connectivity=True, numba=False, lightsim2grid=False,
             consider_line_temperature=False, tdpf=False, run_control=False)
    if not net.converged:
        raise RuntimeError('AC operating point did not converge')
    return net
