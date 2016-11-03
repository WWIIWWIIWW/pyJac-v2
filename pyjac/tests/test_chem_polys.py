#compatibility
from builtins import range

#local imports
from ..core.rate_subs import polyfit_kernel_gen
from ..sympy.sympy_interpreter import load_equations
from ..core.mech_interpret import read_mech_ct
from .loopy_utils import auto_run

#modules
from optionloop import OptionLoop
import cantera as ct
import numpy as np
from nose.plugins.attrib import attr

conp_vars, conp_eqs = load_equations(True)
conv_vars, conv_eqs = load_equations(False)
gas = ct.Solution('h2o2.cti')
elems, specs, reacs = read_mech_ct('h2o2.cti')

test_size=10000

def __subtest(T, ref_ans, ref_ans_T,
    varname, nicename, varlist, eqs):
    oploop = OptionLoop({'lang': ['opencl'],
        'width' : [4, None],
        'depth' : [4, None],
        'ilp' : [True, False],
        'unr' : [None, 4],
        'order' : ['cpu', 'gpu'],
        'device' : ['0:0', '1']})

    for state in oploop:
        try:
            knl = polyfit_kernel_gen(varname, nicename,
                varlist, eqs, specs, 
                **{x : state[x] for x in state if x != 'device'},
                test_size=test_size)
            ref = ref_ans if state['order'] == 'gpu' else ref_ans_T
            assert auto_run(knl, ref, device=state['device'], 
                T_arr=T)
        except Exception as e:
            if not(state['width'] and state['depth']):
                raise e

def __populate(func):
    T = np.random.uniform(600, 2200, size=test_size)
    ref_ans = np.zeros((len(specs), test_size))
    for i in range(test_size):
        for j in range(len(specs)):
            ref_ans[j, i] = func(j, i, T)

    ref_ans_T = ref_ans.T.copy()
    return T, ref_ans, ref_ans_T


def test_cp():
    T, ref_ans, ref_ans_T = __populate(lambda j, i, T: gas.species(j).thermo.cp(T[i]))
    __subtest(T, ref_ans, ref_ans_T, '{C_p}[k]',
        'cp', conp_vars, conp_eqs)

def test_cv():
    T, ref_ans, ref_ans_T = __populate(lambda j, i, T: gas.species(j).thermo.cp(T[i]) - ct.gas_constant)
    ref_ans_T = ref_ans.T.copy()

    __subtest(T, ref_ans, ref_ans_T, '{C_v}[k]',
        'cv', conp_vars, conp_eqs)

def test_h():
    T, ref_ans, ref_ans_T = __populate(lambda j, i, T: gas.species(j).thermo.h(T[i]))
    __subtest(T, ref_ans, ref_ans_T, 'H[k]',
        'h', conp_vars, conp_eqs)

def test_u():
    T, ref_ans, ref_ans_T = __populate(lambda j, i, T: gas.species(j).thermo.h(T[i]) - T[i] * ct.gas_constant)
    __subtest(T, ref_ans, ref_ans_T, 'U[k]',
        'u', conv_vars, conv_eqs)