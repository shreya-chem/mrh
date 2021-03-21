#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <omp.h>
#include "fblas.h"

#ifndef MINMAX
#define MAX(x, y) (((x) > (y)) ? (x) : (y))
#define MIN(x, y) (((x) < (y)) ? (x) : (y))
#define MINMAX
#endif

#ifndef PI
#define PI 3.14159265358979323846
#endif

/* Fock-Space Unitary Coupled Cluster functions
   Cf. Eqs (1)-(12) of JCTC 17 841 2021
   (DOI:10.1021/acs.jctc.0c01052) 

   No spin, no symmetry, no derivatives, not even any looping over amplitudes.
   Do all of that in the Python caller because none of it can easily be
   multithreaded.

   TODO: double-check that this actually works for number-symmetry-breaking
   generators (it should work for 1P or 1H operators, but I haven't checked 
   nPmH in general
*/

typedef void (*FSUCCmixer) (int, double, double*, double*, uint64_t, uint64_t);

void FSUCCmixdetu (int sgn, double amp, double * psi, double * upsi,
    uint64_t det_ia, uint64_t det_ai)
{
    /* Unitary determinant mixer */
    double ct = sgn * cos (amp);
    double st = sgn * sin (amp);
    double psi_ia = psi[det_ia];
    double psi_ai = psi[det_ai];
    upsi[det_ia] = (ct*psi_ia) - (st*psi_ai);
    upsi[det_ai] = (st*psi_ia) + (ct*psi_ai);

}

void FSUCCmixdeth (int sgn, double amp, double * psi, double * hpsi,
    uint64_t det_ia, uint64_t det_ai)
{
    /* Hermitian determinant mixer */
    hpsi[det_ia] += sgn * amp * psi[det_ai]; 
    if (det_ia^det_ai){ // careful!!
    hpsi[det_ai] += sgn * amp * psi[det_ia];
    }
}

void FSUCCcontract1 (uint8_t * aidx, uint8_t * iidx, double amp,
    double * psi, double * opsi, FSUCCmixer mixer, 
    unsigned int norb, unsigned int na, unsigned int ni)
{
    /* Evaluate O|Psi> = mixer (amp, a0'a1'...i1i0, i0'i1'...a1a0) |Psi> 

       Input:
            aidx : array of shape (na); identifies cr,an ops
            iidx : array of shape (ni); identifies an,cr ops
                Note: creation operators are applied left < right;
                annihilation operators are applied right < left
            amp : the amplitude or angle
            psi : array of shape (2**norb); input wfn
            mixer : function pointer; corresponds to type of operator
                The two valid "types" at the moment are unitary
                and hermitian.

       Output:
            opsi : array of shape (2**norb); output wfn
                Careful about whether or not you're modifying psi
                in-place! You generally ~should~ modify in-place with
                a unitary mixer but ~should not~ with a hermitian mixer 
    */

    const int int_one = 1;
    // const double ct = cos (tamp); // (ct -st) (ia) -> (ia)
    // const double st = sin (tamp); // (st  ct) (ai) -> (ai)
    int r;
    uint64_t det_i = 0; // i is occupied
    for (r = 0; r < ni; r++){ 
        if (det_i & (1<<iidx[r])){ return; } // nilpotent escape
        det_i |= (1<<iidx[r]); 
    }
    uint64_t det_a = 0; // a is occupied
    for (r = 0; r < na; r++){ 
        if (det_a & (1<<aidx[r])){ return; } // nilpotent escape
        det_a |= (1<<aidx[r]);
    }
    // all other spinorbitals in det_i, det_a unoccupied
    uint64_t ndet = (1<<norb); // 2**norb
    for (r = 0; r < norb; r++){ if ((det_i|det_a) & (1<<r)){
        ndet >>= 1; // pop 1 spinorbital per unique i,a
        // we only sum over the spectator-spinorbital determinants
    }}

#pragma omp parallel default(shared)
{

    uint64_t det, det_00, det_ia, det_ai;
    unsigned int p, q, sgnbit;
    int sgn;
    double cia, cai;

#pragma omp for schedule(static)

    for (det = 0; det < ndet; det++){
        // "det" here is the string of spectator spinorbitals
        // To find the full det string I have to insert i, a in ascending order
        det_00 = det;
        for (p = 0; p < norb; p++){
            if ((det_i|det_a) & (1<<p)){
                det_00 = (((det_00 >> p) << (p+1)) // move left bits 1 left
                         | (det_00 & ((1<<p)-1))); // keep right bits
            } 
        } // det_00: spectator spinorbitals; all i, a bits unset
        det_ia = det_00 | det_i;
        det_ai = det_00 | det_a;
        // The sign for the whole excitation is the product of the sign incurred
        // by doing this to det_ia:
        // ...i2'...i1'...i0'|0> -> i0'i1'i2'...|0>
        // and doing this to det_ai:
        // ...a2'...a1'...a0'|0> -> a0'a1'a2'...|0>.
        // To implement this without assuming normal-ordered generators
        // (i.e., i0 < i1 < i2 or a0 < a1 < a2)
        // we need to pop creation operators from the string in the order that
        // we move them to the front. Repurpose det_00 for this.
        sgnbit = 0; // careful to only modify the first bit of this
        det_00 = det_ia;
        for (p = 0; p < ni; p++){
            for (q = iidx[p]+1; q < norb; q++){
                sgnbit ^= (det_00 & (1<<q))>>q; // c1'c2' = -c2'c1' sign toggle
            }
            det_00 ^= (1<<iidx[p]); // pop ip
        }
        det_00 = det_ai;
        for (p = 0; p < na; p++){
            for (q = aidx[p]+1; q < norb; q++){
                sgnbit ^= (det_00 & (1<<q))>>q; // c1'c2' = c2'c1' sign toggle
            }
            det_00 ^= (1<<aidx[p]); // pop ap
        }
        sgn = int_one - 2*((int) sgnbit);
        // The rest of the math is trivial
        // cia = sgn * psi[det_ia];
        // cai = sgn * psi[det_ai];
        // psi[det_ia] = (ct*cia) - (st*cai);
        // psi[det_ai] = (st*cia) + (ct*cai);
        mixer (sgn, amp, psi, opsi, det_ia, det_ai);
    }

}

}

void FSUCCcontract1u (uint8_t * aidx, uint8_t * iidx, double tamp,
    double * psi, unsigned int norb, unsigned int na, unsigned int ni)
{
    /* Evaluate U|Psi> = e^(t [a0'a1'...i1i0 - i0'i1'...a1a0]) |Psi> 
       Pro tip: add pi/2 to the amplitude to evaluate dU/dt |Psi>

       Input:
            aidx : array of shape (na); identifies +cr,-an ops
            iidx : array of shape (ni); identifies +an,-cr ops
                Note: creation operators are applied left < right;
                annihilation operators are applied right < left
            tamp : the amplitude or angle

       Input/Output:
            psi : array of shape (2**norb); contains wfn
                Modified in place. Make a copy in the caller
                if you don't want to modify the input
    */
    FSUCCmixer mixer = &FSUCCmixdetu;
    FSUCCcontract1 (aidx, iidx, tamp, psi, psi, mixer, norb, na, ni);
}

void FSUCCcontract1h (uint8_t * aidx, uint8_t * iidx, double hamp,
    double * psi, double * hpsi, unsigned int norb,
    unsigned int na, unsigned int ni)
{
    /* Evaluate H|Psi> = h (a0'a1'...i1i0 + i0'i1'...a1a0) |Psi> 

       Input:
            aidx : array of shape (na); identifies +cr,-an ops
            iidx : array of shape (ni); identifies +an,-cr ops
                Note: creation operators are applied left < right;
                annihilation operators are applied right < left
            amp : the amplitude
            psi : array of shape (2**norb); input wfn

       Input/Output:
            hpsi : array of shape (2**norb); output wfn
    */
    FSUCCmixer mixer = &FSUCCmixdeth;
    FSUCCcontract1 (aidx, iidx, hamp, psi, hpsi, mixer, norb, na, ni);
}

void FSUCCprojai (uint8_t * aidx, uint8_t * iidx, double * psi, 
    unsigned int norb, unsigned int na, unsigned int ni)
{
    /* Project |Psi> into the space that interacts with the operators
       a1'a2'...i1i0 and i1'i2'...a1a0.

       Input:
            aidx : array of shape (na); identifies +cr,-an ops
            iidx : array of shape (ni); identifies +an,-cr ops

       Input/Output:
            psi : array of shape (2**norb); contains wfn
                Modified in place. Make a copy in the caller
                if you don't want to modify the input
    */
    const uint64_t ndet = (1<<norb); // 2**norb
    int r;
    uint64_t det_i = 0; // i is occupied
    for (r = 0; r < ni; r++){ det_i |= (1<<iidx[r]); }
    uint64_t det_a = 0; // a is occupied
    for (r = 0; r < na; r++){ det_a |= (1<<aidx[r]); }
    const uint64_t det_ia = det_i|det_a; // active orbitals (so to speak)

// Should I even bother? I guess 2**n is so bad I should even though this
// should be super fast...        
#pragma omp parallel default(shared)
{
    uint64_t det, det_proj;
#pragma omp for schedule(static)
    for (det = 0; det < ndet; det++){
        det_proj = det & det_ia;
        if (det_proj == det_i){ continue; }
        if (det_proj == det_a){ continue; }
        psi[det] = 0.0;
    }
}
}

void _fullhop_(double * hop, double * psi, double * hpsi,
    uint8_t * pidx, uint8_t * qidx, unsigned int norb,
    unsigned int nelec, unsigned int ielec)
{

    const unsigned int npair = norb*(norb+1)/2;
    const unsigned int opstep = pow (npair, nelec-(ielec+1));
    unsigned int pq, p, q;
    unsigned int spin;

    for (pq = 0; pq < npair; pq++){
        // unpack lower-triangular index 
        p = 0; q = pq; while (p<q){ p++; q-=p; }
        for (spin = 0; spin < 2; spin++){
            pidx[ielec] = p+(spin*norb);
            qidx[ielec] = q+(spin*norb);
            if (ielec+1<nelec){ // recurse to next-minor dimension
                _fullhop_(hop+(pq*opstep), psi, hpsi, pidx, qidx, norb,
                    nelec, ielec+1);
            } else { // execute
                FSUCCcontract1h (pidx, qidx, hop[pq], psi, hpsi, 2*norb,
                    nelec, nelec);
            }
        }
    }

}

void FSUCCfullhop (double * hop, double * psi, double * hpsi,
    unsigned int norb, unsigned int nelec)
{
    /* Evaluate H|Psi>, where H is an nelec-body spin-symmetric Hermitian
       operator and |Psi> is a Fock-space FCI vector with no symmetry
       compactification.

       Input:
            hop : array of shape [norb*(norb+1)/2]*nelec
                Contains operator amplitudes
            psi : array of shape 2**(2*norb); input wfn
            
       Output:
            hpsi : array of shape 2**(2*norb); output wfn
    */
    uint8_t * pidx = malloc (nelec * sizeof (uint8_t));
    uint8_t * qidx = malloc (nelec * sizeof (uint8_t));
    _fullhop_(hop, psi, hpsi, pidx, qidx, norb, nelec, 0);
    free (pidx);
    free (qidx);
}


