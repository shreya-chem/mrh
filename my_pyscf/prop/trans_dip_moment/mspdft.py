from pyscf.lib import logger
from pyscf.mcscf import mc1step, newton_casscf
from functools import reduce
import numpy as np
import time, gc
from pyscf.data import nist
from pyscf import lib
from mrh.my_pyscf.prop.dip_moment import mspdft
from pyscf.fci import direct_spin1
from mrh.my_pyscf.grad import mcpdft as mcpdft_grad

# TODO: docstring?
def mspdft_heff_response (mc_grad, mo=None, ci=None,
        si_bra=None, si_ket=None, state=None, 
        heff_mcscf=None, eris=None):
    ''' Compute the orbital and intermediate-state rotation response 
        vector in the context of an SI-PDFT gradient calculation '''
    mc = mc_grad.base
    if mo is None: mo = mc_grad.mo_coeff
    if ci is None: ci = mc_grad.ci
    if state is None: state = mc_grad.state
    #bra, ket = _unpack_state (state)
    #print('states are  ',state[0],state[1])
    if si_bra is None: si_bra = mc.si[:,state[0]]
    if si_ket is None: si_ket = mc.si[:,state[1]]
    if heff_mcscf is None: heff_mcscf = mc.heff_mcscf
    if eris is None: eris = mc.ao2mo (mo)
    nroots, ncore = mc_grad.nroots, mc.ncore
    moH = mo.conj ().T

    # Orbital rotation (no all-core DM terms allowed!)
    # (Factor of 2 is convention difference between mc1step and newton_casscf)
    casdm1, casdm2 = make_rdm12_heff_offdiag (mc, ci, si_bra, si_ket)
    casdm1 = 0.5 * (casdm1 + casdm1.T)
    casdm2 = 0.5 * (casdm2 + casdm2.transpose (1,0,3,2))
    vnocore = eris.vhf_c.copy ()
    vnocore[:,:ncore] = -moH @ mc.get_hcore () @ mo[:,:ncore]
    with lib.temporary_env (eris, vhf_c=vnocore):
        g_orb = 2 * mc1step.gen_g_hop (mc, mo, 1, casdm1, casdm2, eris)[0]
    g_orb = mc.unpack_uniq_var (g_orb)

    # Intermediate state rotation (TODO: state-average-mix generalization)
    braH = np.dot (si_bra, heff_mcscf)
    Hket = np.dot (heff_mcscf, si_ket)
    si2 = si_bra * si_ket
    g_is  = np.multiply.outer (si_ket, braH)
    g_is += np.multiply.outer (si_bra, Hket)
    g_is -= 2 * si2[:,None] * heff_mcscf
    g_is -= g_is.T
    g_is = g_is[np.tril_indices (nroots, k=-1)]

    return g_orb, g_is

# TODO: state-average-mix generalization
def make_rdm1_heff_offdiag (mc, ci, si_bra, si_ket): 
    '''Compute <bra|O|ket> - sum_i <i|O|i>, where O is the 1-RDM
    operator product, and |bra> and |ket> are both states spanning the
    vector space of |i>, which are multi-determinantal many-electron
    states in an active space.

    Args:
        mc : object of class CASCI or CASSCF
            Only "ncas" and "nelecas" are used, to determine Hilbert
            of ci
        ci : ndarray or list of length (nroots)
            Contains CI vectors spanning a model space
        si_bra : ndarray of shape (nroots)
            Coefficients of ci elements for state |bra>
        si_ket : ndarray of shape (nroots)
            Coefficients of ci elements for state |ket>

    Returns:
        casdm1 : ndarray of shape [ncas,]*2
            Contains O = p'q case
    '''
    ncas, nelecas = mc.ncas, mc.nelecas
    nroots = len (ci)
    ci_arr = np.asarray (ci)
    ci_bra = np.tensordot (si_bra, ci_arr, axes=1)
    ci_ket = np.tensordot (si_ket, ci_arr, axes=1)
    casdm1, _ = direct_spin1.trans_rdm12 (ci_bra, ci_ket, ncas, nelecas)
    ddm1 = np.zeros ((nroots, ncas, ncas), dtype=casdm1.dtype)
    for i in range (nroots):
        ddm1[i,...], _ = direct_spin1.make_rdm12 (ci[i], ncas, nelecas)
    si_diag = si_bra * si_ket
    casdm1 -= np.tensordot (si_diag, ddm1, axes=1)
    return casdm1

def make_rdm12_heff_offdiag (mc, ci, si_bra, si_ket): 
    # TODO: state-average-mix generalization
    #print('During Gradient, CI',ci)
    ncas, nelecas = mc.ncas, mc.nelecas
    nroots = len (ci)
    ci_arr = np.asarray (ci)
    ci_bra = np.tensordot (si_bra, ci_arr, axes=1)
    ci_ket = np.tensordot (si_ket, ci_arr, axes=1)
    casdm1, casdm2 = direct_spin1.trans_rdm12 (ci_bra, ci_ket, ncas, nelecas)
    ddm1 = np.zeros ((nroots, ncas, ncas), dtype=casdm1.dtype)
    ddm2 = np.zeros ((nroots, ncas, ncas, ncas, ncas), dtype=casdm1.dtype)
    for i in range (nroots):
        ddm1[i,...], ddm2[i,...] = direct_spin1.make_rdm12 (ci[i], ncas, nelecas)
    si_diag = si_bra * si_ket
    casdm1 -= np.tensordot (si_diag, ddm1, axes=1)
    casdm2 -= np.tensordot (si_diag, ddm2, axes=1)
    return casdm1, casdm2

def sipdft_HellmanFeynman_dipole (mc, state=None, mo_coeff=None, ci=None, si=None, atmlst=None, verbose=None, max_memory=None, auxbasis_response=False):
    if state is None: state = mc.state
    if mo_coeff is None: mo_coeff = mc.mo_coeff
    if ci is None: ci = mc.ci
    if si is None: si = mc.si
    if mc.frozen is not None:
        raise NotImplementedError
    if max_memory is None: max_memory = mc.max_memory
    t0 = (logger.process_clock (), logger.perf_counter ())

    si_bra = si[:,state[0]]
    si_ket = si[:,state[1]]
    si_diag = si_bra * si_ket

    mol = mc.mol                                           
    ncore = mc.ncore                                       
    ncas = mc.ncas                                         
    nelecas = mc.nelecas                                   
    nocc = ncore + ncas                                    
                                                           
    mo_core = mo_coeff[:,:ncore]                           
    mo_cas  = mo_coeff[:,ncore:nocc]                        
                                                           
    dm_core = np.dot(mo_core, mo_core.T) * 2               

    # ----- Electronic contribution ------
    dm_diag=np.zeros_like(dm_core)
    # Diagonal part
    for i, (amp, c) in enumerate (zip (si_diag, ci)):
        if not amp: continue
        casdm1 = mc.fcisolver.make_rdm1(ci[i], ncas, nelecas)     
        dm_cas = reduce(np.dot, (mo_cas, casdm1, mo_cas.T))    
        dm_i = dm_cas + dm_core 
        dm_diag += amp * dm_i
        
    # Off-diagonal part
    casdm1 = make_rdm1_heff_offdiag (mc, ci, si_bra, si_ket)
#    casdm1 = 0.5 * (casdm1 + casdm1.T)
    dm_off = reduce(np.dot, (mo_cas, casdm1, mo_cas.T))    

    dm = dm_diag + dm_off                                             

    #charges = mol.atom_charges()
    #coords = mol.atom_coords()
    #nuc_charge_center = numpy.einsum('z,zx->x', charges, coords) / charges.sum()
    #with mol.set_common_orig_(nuc_charge_center)
    with mol.with_common_orig((0,0,0)):                    
        ao_dip = mol.intor_symmetric('int1e_r', comp=3)    
    el_dip = np.einsum('xij,ij->x', ao_dip, dm).real       
                                                           
    return el_dip                                         

class TransitionDipole (mspdft.ElectricDipole):

    def convert_dipole (self, ham_response, LdotJnuc, mol_dip, unit='Debye'):
        val = np.linalg.norm(mol_dip)
        i   = self.state[0]
        j   = self.state[1]
        dif = abs(self.e_states[i]-self.e_states[j]) 
        osc = 2/3*dif*val**2
        if unit.upper() == 'DEBYE':
            ham_response *= nist.AU2DEBYE
            LdotJnuc     *= nist.AU2DEBYE
            mol_dip      *= nist.AU2DEBYE
        log = lib.logger.new_logger(self, self.verbose)
        log.note('CMS-PDFT TDM <{}|mu|{}>          {:>10} {:>10} {:>10}'.format(i,j,'X','Y','Z'))
        log.note('Hamiltonian Contribution (%s) : %9.5f, %9.5f, %9.5f', unit, *ham_response)
        log.note('Lagrange Contribution    (%s) : %9.5f, %9.5f, %9.5f', unit, *LdotJnuc)
        log.note('Transition Dipole Moment (%s) : %9.5f, %9.5f, %9.5f', unit, *mol_dip)
        log.note('Oscillator strength  : %9.5f', osc)
        return mol_dip

    def get_ham_response (self, state=None, atmlst=None, verbose=None, mo=None, ci=None, eris=None, si=None, **kwargs):
        mc = self.base
        if state is None: state = self.state
        if atmlst is None: atmlst = self.atmlst
        if verbose is None: verbose = self.verbose
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if si is None: si = self.base.si

        fcasscf = self.make_fcasscf (state)
        fcasscf.mo_coeff = mo
        fcasscf.ci = ci
        return sipdft_HellmanFeynman_dipole (fcasscf, state=state, mo_coeff=mo, ci=ci, si=si, atmlst=atmlst, verbose=verbose)

    def get_wfn_response (self, si_bra=None, si_ket=None, state=None, mo=None,
            ci=None, si=None, eris=None, veff1=None, veff2=None,
            _freeze_is=False, **kwargs):
        if mo is None: mo = self.base.mo_coeff
        if ci is None: ci = self.base.ci
        if si is None: si = self.base.si
        if state is None: state = self.state
        if si_bra is None: si_bra = si[:,state[0]]
        if si_ket is None: si_ket = si[:,state[1]]
        log = lib.logger.new_logger (self, self.verbose)
        si_diag = si_bra * si_ket
        nroots, ngorb, nci = self.nroots, self.ngorb, self.nci
        ptr_is = ngorb + nci

        # Diagonal: PDFT component
        nlag = self.nlag-self.nis
        g_all_pdft = np.zeros (nlag)
        for i, (amp, c, v1, v2) in enumerate (zip (si_diag, ci, veff1, veff2)):
            if not amp: continue
            g_i = mcpdft_grad.Gradients.get_wfn_response (self,
                state=i, mo=mo, ci=ci, veff1=v1, veff2=v2, nlag=nlag, **kwargs)
            g_all_pdft += amp * g_i
            if self.verbose >= lib.logger.DEBUG:
                g_orb, g_ci = self.unpack_uniq_var (g_i)
                g_ci, g_is = self._separate_is_component (g_ci, ci=ci, symm=0)
                log.debug ('g_is pdft state {} component:\n{} * {}'.format (i,
                    amp, g_is))

        # DEBUG
        g_orb_pdft, g_ci = self.unpack_uniq_var (g_all_pdft)
        g_ci, g_is_pdft = self._separate_is_component (g_ci, ci=ci, symm=0)

        # Off-diagonal: heff component
        g_orb_heff, g_is_heff = mspdft_heff_response (self, mo=mo, ci=ci,
            si_bra=si_bra, si_ket=si_ket, eris=eris)

        log.debug ('g_is pdft total component:\n{}'.format (g_is_pdft))
        log.debug ('g_is heff component:\n{}'.format (g_is_heff))

        # Combine
        g_orb = g_orb_pdft + g_orb_heff
        g_is = g_is_pdft + g_is_heff
        if _freeze_is: g_is[:] = 0.0
        g_all = self.pack_uniq_var (g_orb, g_ci, g_is)

        return g_all
