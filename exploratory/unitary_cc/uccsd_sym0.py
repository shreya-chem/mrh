import numpy as np
import time, ctypes, math
from scipy import linalg
from mrh.lib.helper import load_library
from itertools import combinations
from pyscf import lib, ao2mo

libfsucc = load_library ('libfsucc')

# "sym0" means no spin or number symmetry at all: the full fock space,
# with a single flat CI vector corresponding to determinant strings
# identical to CI vector index numbers in a 64-bit unsigned integer format

'''
    fn(vv.ctypes.data_as(ctypes.c_void_p),
       ao.ctypes.data_as(ctypes.c_void_p),
       mo.ctypes.data_as(ctypes.c_void_p),
       ctypes.c_int(nao), ctypes.c_int (nmo),
       ctypes.c_int(ngrids), ctypes.c_int(mol.nbas),
       pnon0tab, pshls_slice, pao_loc)
'''

def _op1u_(norb, aidx, iidx, amp, psi, transpose=False, deriv=0):
    ''' Evaluates U|Psi> = e^(amp * [a0'a1'...i1'i0' - h.c.])|Psi>

        Args:
            norb : integer
                number of orbitals in the fock space
            aidx : list of len (na)
                lists +cr,-an operators
            iidx : list of len (ni)
                lists +an,-cr operators
            amp : float
                amplitude for generator
            psi : ndarray of len (2**norb)
                spinless fock-space CI array; modified in-place

        Kwargs:
            transpose : logical
                Setting to True multiplies the amp by -1
            deriv: int
                Order of differentiation wrt the amp

        Returns:
            psi : ndarray of len (2**norb)
                arg "psi" after operation
    '''
    aidx = np.ascontiguousarray (aidx, dtype=np.uint8)
    iidx = np.ascontiguousarray (iidx, dtype=np.uint8)
    psi = np.ascontiguousarray (psi, dtype=np.float64)
    sgn = 1 - (2*int (transpose))
    my_amp = sgn * (amp + (deriv * math.pi / 2))
    na, ni = aidx.size, iidx.size
    aidx_ptr = aidx.ctypes.data_as (ctypes.c_void_p)
    iidx_ptr = iidx.ctypes.data_as (ctypes.c_void_p)
    psi_ptr = psi.ctypes.data_as (ctypes.c_void_p)
    libfsucc.FSUCCcontract1u (aidx_ptr, iidx_ptr,
        ctypes.c_double (my_amp), psi_ptr,
        ctypes.c_uint (norb),
        ctypes.c_uint (na),
        ctypes.c_uint (ni))
    return psi

def _op1h_spinsym (norb, herm, psi):
    ''' Evaluate H|Psi>, where H is a general spin-symmetric Hermitian
        operator. I'm too lazy to put this function in its own file.

        Args:
            norb : integer
                number of SPATIAL orbitals
            herm : list or tuple
                contains operator terms in order of increasing electron
                count. herm[0] is the constant term, herm[1] is the
                1-electron term, etc. the (n-1)th term consists
                of an ndarray of shape [norb*(norb+1)//2,]*n
            psi : ndarray of shape 2**(2*norb)
                Fock-space CI vector with no symmetry compacting;
                contains input wfn

        Returns:
            hpsi : ndarray of shape 2**(2*norb)
                output wfn
    '''
    psi = np.ascontiguousarray (psi, dtype=np.float64)
    psi_ptr = psi.ctypes.data_as (ctypes.c_void_p)
    hpsi = herm[0] * psi
    hpsi_ptr = hpsi.ctypes.data_as (ctypes.c_void_p)
    fac = 1.0
    for ix, hterm in enumerate (herm[1:]):
        nelec = ix+1
        fac /= ix+1
        hterm = np.ascontiguousarray (fac * hterm, dtype=np.float64)
        hterm_ptr = hterm.ctypes.data_as (ctypes.c_void_p)
        libfsucc.FSUCCfullhop (hterm_ptr, psi_ptr, hpsi_ptr,
            ctypes.c_uint (norb), ctypes.c_uint (nelec))
    return hpsi

def _projai_(norb, aidx, iidx, psi):
    ''' Project |Psi> into the space that interacts with the operators
        a1'a2'...i1i0 and i1'i2'...a1a0

        Args:
            norb : integer
                number of orbitals in the fock space
            aidx : list of len (na)
                lists +cr,-an operators
            iidx : list of len (ni)
                lists +an,-cr operators
            psi : ndarray of len (2**norb)
                spinless fock-space CI array; modified in-place

        Returns:
            psi : ndarray of len (2**norb)
                arg "psi" after operation
    '''
    aidx = np.ascontiguousarray (aidx, dtype=np.uint8)
    iidx = np.ascontiguousarray (iidx, dtype=np.uint8)
    psi = np.ascontiguousarray (psi, dtype=np.float64)
    na, ni = aidx.size, iidx.size
    aidx_ptr = aidx.ctypes.data_as (ctypes.c_void_p)
    iidx_ptr = iidx.ctypes.data_as (ctypes.c_void_p)
    psi_ptr = psi.ctypes.data_as (ctypes.c_void_p)
    libfsucc.FSUCCprojai (aidx_ptr, iidx_ptr, psi_ptr,
        ctypes.c_uint (norb),
        ctypes.c_uint (na),
        ctypes.c_uint (ni))
    return psi

class FSUCCOperator (object):

    def __init__(self, norb, a_idxs, i_idxs):
        self.norb = norb
        self.ngen = ngen = len (a_idxs)
        self.a_idxs = [np.ascontiguousarray (a, dtype=np.uint8) for a in a_idxs]
        self.i_idxs = [np.ascontiguousarray (i, dtype=np.uint8) for i in i_idxs]
        assert (len (self.i_idxs) == ngen)
        self.amps = np.zeros (ngen)
        self.assert_sanity (nodupes=True)

    def gen_fac (self, reverse=False):
        ''' Iterate over unitary factors/generators. '''
        ngen = self.ngen
        intr = int (reverse)
        start = 0 + (intr * (ngen-1))
        stop = ngen - (intr * (ngen+1))
        step = 1 - (2*intr)
        for igen in range (start, stop, step):
            yield igen, self.a_idxs[igen], self.i_idxs[igen], self.amps[igen]

    def gen_deriv1 (self, psi, transpose=False):
        ''' Iterate over first derivatives of U|Psi> wrt to generator amplitudes '''
        for igend in range (self.ngen):
            dupsi = psi.copy ()
            for ix, aidx, iidx, amp in self.gen_fac (reverse=transpose):
                if ix==igend: _projai_(self.norb, aidx, iidx, dupsi)
                _op1u_(self.norb, aidx, iidx, amp, dupsi,
                    transpose=transpose, deriv=(ix==igend))
            yield dupsi

    def assert_sanity (self, nodupes=True):
        ''' check for nilpotent generators, too many cr/an ops, or orbital
            indices out of range. nodupes -> check for duplicates under
            permutation symmetry (expensive) '''
        norb, ngen = self.norb, self.ngen
        pq_sorted = []
        for a, i in zip (self.a_idxs, self.i_idxs):
            p = np.append (i, a)
            errstr = 'a,i={},{} invalid for norb={}'.format (a,i,norb)
            assert (np.amax (p) < norb), errstr
            a_maxcnt = np.amax (np.unique (a, return_counts=True)[1]) if len (a) else 0
            errstr = 'a={} is nilpotent'.format (a)
            assert (a_maxcnt < 2), errstr
            i_maxcnt = np.amax (np.unique (i, return_counts=True)[1]) if len (i) else 0
            errstr = 'i={} is nilpotent'.format (a)
            assert (i_maxcnt < 2), errstr
            # passing these three implies that there aren't too many ops
            a_sorted, i_sorted = np.sort (a), np.sort (i)
            errstr = 'undefined amplitude detected (i==a) {},{}'.format (i, a)
            if (len (a) and len (i)): assert (not np.all (a_sorted==i_sorted)), errstr
            pq_sorted.append (tuple (sorted ([tuple (a_sorted), tuple (i_sorted)])))
        if nodupes:
            pq_sorted = set (pq_sorted)
            errstr = 'duplicate generators detected'
            assert (len (pq_sorted) == ngen), errstr

    def __call__(self, psi, transpose=False, inplace=False):
        upsi = psi.view () if inplace else psi.copy ()
        for ix, aidx, iidx, amp in self.gen_fac (reverse=transpose):
            _op1u_(self.norb, aidx, iidx, amp, upsi, transpose=transpose, deriv=0)
        return upsi

    def get_uniq_amps (self):
        ''' subclass me to apply s**2 or irrep symmetries '''
        return self.amps.copy ()

    def set_uniq_amps_(self, x):
        ''' subclass me to apply s**2 or irrep symmetries '''
        self.amps[:] = np.asarray (x)
        return self
    
    @property
    def nuniq (self):
        ''' subclass me to apply s**2 or irrep symmetries '''
        return self.ngen

def get_uccs_op (norb, tp=None, tph=None):
    # This is incomplete
    p = list (range (norb))
    t1_idx = np.tril_indices (norb, k=-1)
    a, i = list (t1_idx[0]), list (t1_idx[1])
    a = p + a
    i = [[] for q in p] + i
    uop = FSUCCOperator (norb, a, i) 
    npair = norb * (norb - 1) // 2
    assert (len (t1_idx[0]) == npair)
    if tp is not None:
        uop.amps[:norb] = tp[:]
    if tph is not None:
        uop.amps[norb:][:npair] = tph[t1_idx] 
    return uop

def get_uccsd_op (norb, tp=None, tph=None, t2=None, uop_s=None):
    # This is incomplete
    if uop_s is None: uop_s = get_uccs_op (norb, tp=tp, tph=tph)
    init_offs = uop_s.ngen
    ab_idxs = uop_s.a_idxs
    ij_idxs = uop_s.i_idxs
    pq = [(p, q) for p, q in zip (*np.tril_indices (norb,k=-1))]
    a = []
    b = []
    i = []
    j = []
    for ab, ij in combinations (pq, 2):
        ab_idxs.append (ab)
        ij_idxs.append (ij)
        a.append (ab[0])
        b.append (ab[1])
        i.append (ij[0])
        j.append (ij[1])
    uop = FSUCCOperator (norb, ab_idxs, ij_idxs)
    uop.amps[:init_offs] = uop_s.amps[:]
    if t2 is not None:
        uop.amps[init_offs:] = t2[(a,i,b,j)]
    return uop

def get_uccs_op_numsym (norb, t1=None):
    a, i = np.tril_indices (norb, k=-1)
    uop = FSUCCOperator (norb, a, i)
    if t1 is not None: uop.amps[:] = t1[(a,i)]
    return uop

def get_uccsd_op_numsym (norb, t1=None, t2=None):
    uop_s = get_uccs_op_numsym (norb, t1=t1)
    return get_uccsd_op (norb, t2=t2, uop_s=uop_s)


class UCCS (lib.StreamObject):
    ''' This is just a super janky way of implementing Hartree-Fock.
        Its only value is testing the operators.
        Do not, repeat, do not, waste time trying to make it "fit"
        anywhere or adding "useful" features. '''

    def __init__(self, mol):
        self.mol = mol
        self.norb = mol.nao_nr ()
        self.mo_coeff = None # One needs at least an orthonormal basis
        self.verbose = mol.verbose
        self.stdout = mol.stdout
        self.x = None
        self.e_tot = None

    def get_uop (self):
        return get_uccs_op_numsym (2*self.norb)

    def get_ham (self, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        h0  = self.mol.energy_nuc ()
        h1  = mol.intor_symmetric ('int1e_kin') 
        h1 += mol.intor_symmetric ('int1e_nuc')
        # Gotta at least make an orthonormal basis
        if mo_coeff is None:
            s0 = mol.intor_symmetric ('int1e_ovlp')
            e1, mo_coeff = linalg.eigh (h1, b=s0)
        h1 = mo_coeff.T @ h1 @ mo_coeff
        h1 = h1[np.tril_indices (self.norb)]
        h2 = ao2mo.restore (4, ao2mo.full (mol, mo_coeff), self.norb)
        return mo_coeff, h0, h1, h2

    def get_hop (self, mo_coeff=None, ham=None):
        if ham is None:
            mo_coeff, h0, h1, h2 = self.get_ham (mo_coeff=mo_coeff)
            ham = [h0, h1, h2]
        norb = self.norb
        def hop (psi): return _op1h_spinsym (norb, ham, psi)
        return mo_coeff, hop

    def get_psi0 (self):
        ''' just aufbau for now '''
        neleca = (self.mol.nelectron + self.mol.spin) // 2
        nelecb = (self.mol.nelectron - self.mol.spin) // 2
        n0a, n0b = 0, 0
        for ielec in range (neleca): n0a |= (1<<ielec)
        for ielec in range (nelecb): n0b |= (1<<ielec)
        psi0 = np.zeros (2**(2*self.norb), dtype=np.float64)
        psi0[(n0a<<self.norb)|n0b] = 1.0
        return psi0

    def get_obj_fun (self, mo_coeff=None, uop=None, hop=None, psi0=None, x0=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if uop is None: uop = self.get_uop ()
        if hop is None: mo_coeff, hop = self.get_hop (mo_coeff=mo_coeff, ham=ham)
        if psi0 is None: psi0 = self.get_psi0 ()
        if x0 is None: x0 = uop.get_uniq_amps ()
        def obj_fun (x):
            uop.set_uniq_amps_(x)
            upsi = uop (psi0) 
            hupsi = hop (upsi)
            e_tot = upsi.conj ().dot (hupsi)
            jac = []
            for ix, dupsi in enumerate (uop.gen_deriv1 (psi0)):
                jac.append (2*dupsi.conj ().dot (hupsi)) 
            return e_tot, np.asarray (jac)
        return mo_coeff, obj_fun, x0

    def kernel (self, mo_coeff=None, psi0=None, x0=None):
        self.mo_coeff, obj_fun, x0 = self.get_obj_fun (mo_coeff=mo_coeff, psi0=psi0)
        res = optimize.minimize (obf_fun, x0, method='BFGS', jac=True)
        lib.logger.info (self, 'UCCS BFGS {}'.format (
            ('not converged','converged')[int (res.success)]))
        self.x = res.x
        self.e_tot = res.fun
        return self.mo_coeff, self.x, self.e_tot

if __name__ == '__main__':
    norb = 4
    def pbin (n):
        s = bin (n)[2:]
        m = norb - len (s)
        if m: s = ''.join (['0',]*m) + s
        return s
    psi = np.zeros (2**norb)
    psi[5] = 1.0

    #a, i = np.tril_indices (norb, k=-1)
    #uop = FSUCCOperator (norb, a, i)
    #uop.amps = (1 - 2*np.random.rand (uop.ngen))*math.pi
    tp_rand = np.random.rand (norb)
    tph_rand = np.random.rand (norb,norb)
    t2_rand = np.random.rand (norb,norb,norb,norb)
    uop_s = get_uccs_op (norb, tp=tp_rand, tph=tph_rand)
    upsi = uop_s (psi)
    uTupsi = uop_s (upsi, transpose=True)
    for ix in range (2**norb):
        print (pbin (ix), psi[ix], upsi[ix], uTupsi[ix])
    print ("<psi|psi> =",psi.dot (psi), "<psi|U|psi> =",psi.dot (upsi),"<psi|U'U|psi> =",upsi.dot (upsi))

    uop_sd = get_uccsd_op (norb, tp=tp_rand, tph=tph_rand, t2=t2_rand)
    upsi = uop_sd (psi)
    uTupsi = uop_sd (upsi, transpose=True)
    for ix in range (2**norb):
        print (pbin (ix), psi[ix], upsi[ix], uTupsi[ix])
    print ("<psi|psi> =",psi.dot (psi), "<psi|U|psi> =",psi.dot (upsi),"<psi|U'U|psi> =",upsi.dot (upsi))

    h0 = 1.0
    npair = (norb//2)*((norb//2)+1)//2
    h1 = np.random.rand (npair)
    h2 = np.random.rand (npair, npair)
    h2 += h2.T
    h2 /= 2
    hpsi = _op1h_spinsym (norb//2, [h0,h1,h2], psi)
    for ix in range (2**norb):
        print (pbin (ix), psi[ix], hpsi[ix])
    print ("<psi|H|psi> =",psi.dot (hpsi))

    def obj_fun (x):
        uop_sd.amps[:] = x
        upsi = uop_sd (psi)
        err = upsi.dot (upsi) - (upsi[7]**2)
        jac = np.zeros_like (x)
        for ix, dupsi in enumerate (uop_sd.gen_deriv1 (psi)):
            jac[ix] += 2*(upsi.dot (dupsi) - dupsi[7]*upsi[7])
        return err, jac

    from scipy import optimize
    res = optimize.minimize (obj_fun, uop_sd.amps, method='BFGS', jac=True)

    print (res.success)
    uop_sd.amps[:] = res.x
    upsi = uop_sd (psi)
    uTupsi = uop_sd (upsi, transpose=True)
    for ix in range (2**norb):
        print (pbin (ix), psi[ix], upsi[ix], uTupsi[ix])
    print ("<psi|psi> =",psi.dot (psi), "<psi|U|psi> =",psi.dot (upsi),"<psi|U'U|psi> =",upsi.dot (upsi))

