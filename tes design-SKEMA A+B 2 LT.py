"""
Steel Design Engine — AISC 360-22 (LRFD, SI Units)
====================================================
Standalone Python 3.12 script for steel design checks.
Reads Result.json, performs AISC 360-22 design checks per element,
writes Design Result.json.

Concepts adapted from SAP2000 Steel Design:
- PMM check per station per load combination
- Governing = max TotalRatio across all stations and combos
- Output: Summary + PMM Details + Shear Details

Units: mm, N, MPa (internally), output in kN and kN-m for readability.

Reference:
- AISC 360-22 Specification for Structural Steel Buildings
- SNI 1727-2020 Load Combinations (LRFD)
- DESAIN BAJA.py (reference implementation in ksi/inch)
"""

import json
import math
import sys
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SectionProperties:
    """IWF/W-shape section properties in SI units (mm, MPa)."""
    name: str = ""
    d: float = 0.0       # Total depth (mm)
    bf: float = 0.0      # Flange width (mm)
    tf: float = 0.0      # Flange thickness (mm)
    tw: float = 0.0      # Web thickness (mm)
    A: float = 0.0       # Gross area (mm²)
    Ix: float = 0.0      # Moment of inertia major (mm⁴) — called Iz in Result.json
    Iy: float = 0.0      # Moment of inertia minor (mm⁴)
    Sx: float = 0.0      # Elastic section modulus major (mm³) — called Sz
    Sy: float = 0.0      # Elastic section modulus minor (mm³)
    Zx: float = 0.0      # Plastic section modulus major (mm³) — called Zz
    Zy: float = 0.0      # Plastic section modulus minor (mm³)
    rx: float = 0.0      # Radius of gyration major (mm) — called rz
    ry: float = 0.0      # Radius of gyration minor (mm)
    J: float = 0.0       # Torsional constant (mm⁴)
    Cw: float = 0.0      # Warping constant (mm⁶)
    # Material
    E: float = 200000.0  # Young's modulus (MPa)
    G: float = 77200.0   # Shear modulus (MPa)
    Fy: float = 250.0    # Yield stress (MPa)
    Fu: float = 400.0    # Ultimate stress (MPa)

    @property
    def h(self) -> float:
        """Clear distance between flanges (mm)."""
        return self.d - 2 * self.tf

    @property
    def ho(self) -> float:
        """Distance between flange centroids (mm)."""
        return self.d - self.tf

    @property
    def Aw(self) -> float:
        """Web area (mm²)."""
        return self.d * self.tw

    @property
    def rts(self) -> float:
        """Effective radius of gyration for LTB (mm). AISC F2-7."""
        Iy_val = max(self.Iy, 1.0)
        Sx_val = max(self.Sx, 1.0)
        return math.sqrt(math.sqrt(Iy_val * self.Cw) / Sx_val)


@dataclass
class DesignForces:
    """Design forces at a specific station (N, N·mm). Revit convention."""
    P: float = 0.0    # Axial force (N) — positive = compression
    Fy: float = 0.0   # Minor shear (N) — Revit Fy (horizontal)
    Fz: float = 0.0   # Major shear (N) — Revit Fz (vertical)
    T: float = 0.0    # Torsion (N·mm)
    My: float = 0.0   # Minor moment (N·mm) — Revit My
    Mz: float = 0.0   # Major moment (N·mm) — Revit Mz


@dataclass
class DesignCapacity:
    """Design capacities for an element (N, N·mm)."""
    PcComp: float = 0.0       # φPn compression (N)
    PcTension: float = 0.0    # φPn tension (N)
    McMajor: float = 0.0      # φMn major axis (N·mm)
    McMinor: float = 0.0      # φMn minor axis (N·mm)
    PhiVnMajor: float = 0.0   # φVn major shear (N)
    PhiVnMinor: float = 0.0   # φVn minor shear (N)
    section_class_axial: str = "Nonslender"
    section_class_flexure_flange: str = "Compact"
    section_class_flexure_web: str = "Compact"
    Cb: float = 1.0


@dataclass
class PMMResult:
    """PMM interaction check result (Chapter H1)."""
    equation: str = ""       # "H1-1a" or "H1-1b"
    PRatio: float = 0.0      # Pr/Pc contribution
    MMajRatio: float = 0.0   # Mrx/Mcx contribution
    MMinRatio: float = 0.0   # Mry/Mcy contribution
    TotalRatio: float = 0.0  # DCR
    Pr: float = 0.0          # Applied axial (N)
    MrMajor: float = 0.0     # Applied major moment (N·mm)
    MrMinor: float = 0.0     # Applied minor moment (N·mm)
    Pc_used: float = 0.0     # Pc used (comp or tension)


@dataclass
class ShearResult:
    """Shear check result (Chapter G)."""
    VrMajor: float = 0.0      # Applied major shear (N)
    PhiVnMajor: float = 0.0   # Capacity (N)
    VMajorRatio: float = 0.0  # DCR


@dataclass
class ElementDesignResult:
    """Governing design result per element (SAP2000 concept)."""
    element_id: int = 0
    frame_label: str = ""
    design_type: str = ""        # "Column" or "Beam"
    design_section: str = ""
    status: str = "OK"           # "OK" or "Overstressed"
    governing_ratio: float = 0.0
    ratio_type: str = "PMM"
    governing_combo: str = ""
    governing_location_mm: float = 0.0

    capacity: Optional[DesignCapacity] = None
    pmm_detail: Optional[PMMResult] = None
    shear_detail: Optional[ShearResult] = None
    shear_combo: str = ""
    shear_location_mm: float = 0.0
    station_details: Optional[list] = None  # Per-combo per-station results


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION CLASSIFICATION — Table B4.1
# ═══════════════════════════════════════════════════════════════════════════════

class SectionClassification:
    """Classify rolled I-shape elements per AISC Table B4.1."""

    def __init__(self, sec: SectionProperties):
        self.sec = sec
        self.E = sec.E
        self.Fy = sec.Fy

        # Slenderness ratios
        self.lambda_flange = sec.bf / (2 * sec.tf) if sec.tf > 0 else 0
        self.lambda_web = sec.h / sec.tw if sec.tw > 0 else 0
        self.sqrt_E_Fy = math.sqrt(self.E / self.Fy) if self.Fy > 0 else 0

    def classify_axial(self) -> Dict[str, str]:
        """Table B4.1a — Elements in compression."""
        # Flange: Case 1 — Flanges of rolled I-shapes
        lr_flange = 0.56 * self.sqrt_E_Fy
        flange_class = "Nonslender" if self.lambda_flange <= lr_flange else "Slender"

        # Web: Case 5 — Webs of rolled I-shapes
        lr_web = 1.49 * self.sqrt_E_Fy
        web_class = "Nonslender" if self.lambda_web <= lr_web else "Slender"

        return {
            "flange": flange_class,
            "web": web_class,
            "lambda_flange": self.lambda_flange,
            "lambda_web": self.lambda_web,
            "lr_flange": lr_flange,
            "lr_web": lr_web,
            "overall": "Slender" if (flange_class == "Slender" or web_class == "Slender") else "Nonslender"
        }

    def classify_flexure(self) -> Dict[str, str]:
        """Table B4.1b — Elements in flexure."""
        # Flange: Case 10 — Flanges of rolled I-shapes in flexure
        lp_flange = 0.38 * self.sqrt_E_Fy
        lr_flange = 1.0 * self.sqrt_E_Fy

        if self.lambda_flange <= lp_flange:
            flange_class = "Compact"
        elif self.lambda_flange <= lr_flange:
            flange_class = "Noncompact"
        else:
            flange_class = "Slender"

        # Web: Case 15 — Webs of doubly symmetric I-shapes in flexure
        lp_web = 3.76 * self.sqrt_E_Fy
        lr_web = 5.70 * self.sqrt_E_Fy

        if self.lambda_web <= lp_web:
            web_class = "Compact"
        elif self.lambda_web <= lr_web:
            web_class = "Noncompact"
        else:
            web_class = "Slender"

        return {
            "flange": flange_class,
            "web": web_class,
            "lambda_flange": self.lambda_flange,
            "lambda_web": self.lambda_web,
            "lp_flange": lp_flange,
            "lr_flange": lr_flange,
            "lp_web": lp_web,
            "lr_web": lr_web
        }


# ═══════════════════════════════════════════════════════════════════════════════
# CHAPTER D — TENSION
# ═══════════════════════════════════════════════════════════════════════════════

class TensionCapacity:
    """AISC Chapter D — Design of Members for Tension."""

    @staticmethod
    def design(sec: SectionProperties) -> float:
        """
        Returns φPn for tension (N).
        D2-1: Yielding — φ = 0.90, Pn = Fy × Ag
        D2-2: Rupture  — φ = 0.75, Pn = Fu × Ae (U = 0.85 for I-shapes)
        """
        phi_y = 0.90
        Pn_yield = sec.Fy * sec.A  # D2-1

        phi_r = 0.75
        U = 0.85  # Shear lag factor for I-shapes (Table D3.1, Case 2)
        Ae = U * sec.A
        Pn_rupture = sec.Fu * Ae  # D2-2

        return min(phi_y * Pn_yield, phi_r * Pn_rupture)


# ═══════════════════════════════════════════════════════════════════════════════
# CHAPTER E — COMPRESSION
# ═══════════════════════════════════════════════════════════════════════════════

class CompressionCapacity:
    """AISC Chapter E — Design of Members for Compression."""

    @staticmethod
    def design(sec: SectionProperties, Kx: float, Ky: float,
               Lx: float, Ly: float, axial_class: Dict,
               Kz: float = 1.0, Lcz: float = None) -> float:
        """
        Returns φPn for compression (N).
        E3: Flexural buckling
        E4: Torsional/flexural-torsional buckling (doubly symmetric I-shape)
        E7: Members with slender elements
        """
        phi = 0.90
        E = sec.E
        Fy = sec.Fy

        # Effective lengths — Lc = K·L (AISC 360-22 notation)
        Lcx = Kx * Lx                                    # Effective length major
        Lcy = Ky * Ly                                    # Effective length minor
        Lc_rx = Lcx / sec.rx if sec.rx > 0 else 0        # Lc/r major
        Lc_ry = Lcy / sec.ry if sec.ry > 0 else 0        # Lc/r minor
        Lc_r_max = max(Lc_rx, Lc_ry)

        if Lc_r_max == 0:
            return phi * Fy * sec.A

        # E3-4: Elastic flexural buckling stress
        Fe_flexural = (math.pi ** 2 * E) / (Lc_r_max ** 2)

        # E4-4: Elastic torsional buckling stress (doubly symmetric I-shape)
        if Lcz is None:
            Lcz = Lcy  # Default: Lcz = Lcy (konservatif = panjang kolom)
        Lcz_eff = Kz * Lcz   # Kz default = 1.0

        Ix_Iy = sec.Ix + sec.Iy
        if Ix_Iy > 0 and Lcz_eff > 0:
            Fe_torsion = (
                (math.pi**2 * E * sec.Cw) / (Lcz_eff**2)
                + sec.G * sec.J
            ) / Ix_Iy
        else:
            Fe_torsion = Fe_flexural  # Fallback

        # Governing Fe = min(E3, E4)
        Fe = min(Fe_flexural, Fe_torsion)

        # Critical stress Fcr — E3-2 / E3-3
        if Fy / Fe <= 2.25:  # Equivalent to Lc/r <= 4.71*sqrt(E/Fy)
            # Inelastic buckling — E3-2
            Fcr = (0.658 ** (Fy / Fe)) * Fy
        else:
            # Elastic buckling — E3-3
            Fcr = 0.877 * Fe

        # E7: Check for slender elements
        if axial_class["overall"] == "Slender":
            Ae = CompressionCapacity._calc_effective_area(sec, Fcr, axial_class)
            Pn = Fcr * Ae
        else:
            Pn = Fcr * sec.A

        return phi * Pn

    @staticmethod
    def _calc_effective_area(sec: SectionProperties, Fcr: float,
                             axial_class: Dict) -> float:
        """Calculate effective area for slender elements — E7."""
        E = sec.E
        Fy = sec.Fy
        Ae = sec.A

        # Slender flanges — Qs factor (E7-1)
        if axial_class["flange"] == "Slender":
            b_t = sec.bf / (2 * sec.tf)
            lr = 0.56 * math.sqrt(E / Fy)

            if b_t <= 0.56 * math.sqrt(E / Fy):
                Qs = 1.0
            elif b_t <= 1.03 * math.sqrt(E / Fy):
                # E7-4
                Qs = 1.415 - 0.74 * b_t * math.sqrt(Fy / E)
            else:
                # E7-5
                Qs = 0.69 * E / (Fy * b_t ** 2)

            # Reduce flange area
            Af = 2 * sec.bf * sec.tf
            Ae = sec.A - Af * (1 - Qs)

        # Slender web — Qa factor using Table E7.1
        if axial_class["web"] == "Slender":
            f = Fcr  # Use Fcr as the stress level
            # Table E7.1, Case (c): c1=0.22, c2=1.49 (all other elements)
            c1, c2 = 0.22, 1.49
            lambda_web = sec.h / sec.tw                     # Actual λ
            lambda_r = 1.49 * math.sqrt(E / Fy)            # Limiting λr (Table B4.1a)
            # E7-5: Elastic local buckling stress
            Fel = (c2 * lambda_r / lambda_web) ** 2 * Fy
            # E7-17: Effective width
            if f > 0 and Fel > 0:
                be = sec.h * (1 - c1 * math.sqrt(Fel / f)) * math.sqrt(Fel / f)
                be = min(be, sec.h)
            else:
                be = sec.h
            if be < sec.h:
                Ae -= (sec.h - be) * sec.tw

        return max(Ae, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# CHAPTER F — FLEXURE
# ═══════════════════════════════════════════════════════════════════════════════

class FlexureCapacity:
    """AISC Chapter F — Design of Members for Flexure."""

    @staticmethod
    def design_major(sec: SectionProperties, Lb: float, Cb: float,
                     flex_class: Dict) -> float:
        """
        Returns φMn for major axis bending (N·mm).
        Dispatches to F2 (compact), F3 (noncompact/slender flange).
        """
        flange = flex_class["flange"]
        web = flex_class["web"]

        if web == "Compact" and flange == "Compact":
            return FlexureCapacity._design_F2(sec, Lb, Cb)
        elif web == "Compact" and flange in ("Noncompact", "Slender"):
            Mn_F2 = FlexureCapacity._design_F2(sec, Lb, Cb)
            Mn_F3 = FlexureCapacity._design_F3(sec, flex_class)
            # F3 applies to FLB, take minimum with LTB from F2
            return min(Mn_F2, Mn_F3)
        else:
            # NOTE: F4/F5 (noncompact/slender web) belum diimplementasi
            # Menggunakan F2 sebagai upper bound — valid untuk rolled IWF
            # WARNING: Jika built-up section digunakan, F4/F5 HARUS diimplementasi
            return FlexureCapacity._design_F2(sec, Lb, Cb)

    @staticmethod
    def _design_F2(sec: SectionProperties, Lb: float, Cb: float) -> float:
        """
        F2 — Doubly symmetric compact I-shape (major axis).
        Considers yielding and lateral-torsional buckling.
        Returns φMn (N·mm).
        """
        phi = 0.90
        E = sec.E
        Fy = sec.Fy
        Sx = sec.Sx
        Zx = sec.Zx
        Iy = sec.Iy
        J = sec.J
        Cw = sec.Cw
        ho = sec.ho
        rts = sec.rts

        # Plastic moment
        Mp = Fy * Zx

        # Limiting unbraced lengths
        # Lp — F2-5
        Lp = 1.76 * sec.ry * math.sqrt(E / Fy)

        # Lr — F2-6
        c = 1.0  # For doubly symmetric I-shapes
        if Sx > 0 and rts > 0:
            term1 = (J * c) / (Sx * ho)
            term2 = math.sqrt(term1 ** 2 + 6.76 * (0.7 * Fy / E) ** 2)
            Lr = 1.95 * rts * (E / (0.7 * Fy)) * math.sqrt(term1 + term2)
        else:
            Lr = Lp * 3  # Fallback

        if Lb <= Lp:
            # Yielding — F2-1
            Mn = Mp
        elif Lb <= Lr:
            # Inelastic LTB — F2-2
            Mn = Cb * (Mp - (Mp - 0.7 * Fy * Sx) * ((Lb - Lp) / (Lr - Lp)))
            Mn = min(Mn, Mp)
        else:
            # Elastic LTB — F2-3, F2-4
            Fcr = (Cb * math.pi ** 2 * E / (Lb / rts) ** 2) * \
                  math.sqrt(1 + 0.078 * (J * c) / (Sx * ho) * (Lb / rts) ** 2)
            Mn = min(Fcr * Sx, Mp)

        return phi * Mn

    @staticmethod
    def _design_F3(sec: SectionProperties, flex_class: Dict) -> float:
        """
        F3 — I-shapes with noncompact or slender flanges (FLB).
        Returns φMn (N·mm).
        """
        phi = 0.90
        E = sec.E
        Fy = sec.Fy
        Sx = sec.Sx
        Zx = sec.Zx
        Mp = Fy * Zx
        lam = flex_class["lambda_flange"]
        lp = flex_class["lp_flange"]
        lr = flex_class["lr_flange"]

        if flex_class["flange"] == "Noncompact":
            # F3-1 — Interpolation
            Mn = Mp - (Mp - 0.7 * Fy * Sx) * ((lam - lp) / (lr - lp))
        else:
            # F3-2 — Slender flange
            kc = 4.0 / math.sqrt(sec.h / sec.tw)
            kc = max(0.35, min(kc, 0.76))
            Mn = 0.9 * E * kc * Sx / (lam ** 2)

        return phi * min(Mn, Mp)

    @staticmethod
    def design_minor(sec: SectionProperties, flex_class: Dict) -> float:
        """
        F6 — I-shaped members bent about minor axis.
        Returns φMn (N·mm).
        """
        phi = 0.90
        Fy = sec.Fy
        Sy = sec.Sy
        Zy = sec.Zy
        E = sec.E
        Mp = min(Fy * Zy, 1.6 * Fy * Sy)  # F6-1

        lam = flex_class["lambda_flange"]
        lp = flex_class["lp_flange"]
        lr = flex_class["lr_flange"]

        if lam <= lp:
            # Compact — F6-1
            Mn = Mp
        elif lam <= lr:
            # Noncompact — F6-2
            Mn = Mp - (Mp - 0.7 * Fy * Sy) * ((lam - lp) / (lr - lp))
        else:
            # Slender — F6-4
            Fcr = 0.7 * E / (lam ** 2)
            Mn = Fcr * Sy

        return phi * min(Mn, Mp)

    @staticmethod
    def calc_Cb(moments: List[float]) -> float:
        """
        Calculate Cb from moment diagram (quarter-point method).
        AISC F1-1: Cb = 12.5 Mmax / (2.5 Mmax + 3 MA + 4 MB + 3 MC)
        moments: list of absolute moment values at stations along the element
        """
        if not moments or max(abs(m) for m in moments) < 1e-6:
            return 1.0

        abs_moments = [abs(m) for m in moments]
        n = len(abs_moments)
        Mmax = max(abs_moments)

        if n >= 5:
            # Use quarter points
            idx_quarter = n // 4
            idx_mid = n // 2
            idx_3quarter = 3 * n // 4
            MA = abs_moments[idx_quarter]
            MB = abs_moments[idx_mid]
            MC = abs_moments[idx_3quarter]
        elif n >= 3:
            MA = abs_moments[0]
            MB = abs_moments[n // 2]
            MC = abs_moments[-1]
        else:
            return 1.0

        denom = 2.5 * Mmax + 3 * MA + 4 * MB + 3 * MC
        if denom < 1e-6:
            return 1.0

        Cb = 12.5 * Mmax / denom
        # F1-1: Untuk doubly symmetric, Cb ≥ 1.0 secara matematis
        # Reduces to 1.0 pada uniform moment (no transverse loading)
        return max(Cb, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# CHAPTER G — SHEAR
# ═══════════════════════════════════════════════════════════════════════════════

class ShearCapacity:
    """AISC Chapter G — Design of Members for Shear."""

    @staticmethod
    def design_major(sec: SectionProperties) -> float:
        """
        G2 — Shear strength of I-shape webs.
        Returns φVn (N).
        """
        phi = 1.00  # G2-1: φv = 1.00 for rolled I-shapes with h/tw ≤ 2.24√(E/Fy)
        Fy = sec.Fy
        E = sec.E
        Aw = sec.d * sec.tw  # G2: Aw = d × tw
        h_tw = sec.h / sec.tw if sec.tw > 0 else 0

        limit = 2.24 * math.sqrt(E / Fy)

        if h_tw <= limit:
            # G2-2: Cv1 = 1.0, φ = 1.00
            Cv1 = 1.0
            phi = 1.00
        else:
            # G2-3, G2-4: reduced Cv1
            phi = 0.90
            kv = 5.34  # No transverse stiffeners
            limit2 = 1.10 * math.sqrt(kv * E / Fy)
            if h_tw <= limit2:
                Cv1 = 1.0
            else:
                Cv1 = limit2 / h_tw

        Vn = 0.6 * Fy * Aw * Cv1
        return phi * Vn

    @staticmethod
    def design_minor(sec: SectionProperties) -> float:
        """Minor axis shear — AISC G6-1."""
        phi = 0.90
        # G6-1: Vn = 0.6*Fy*bf*tf*Cv2 per shear resisting element (per flange)
        # Cv2 = 1.0 for all ASTM A6/A6M W, S, M, HP shapes (Fy ≤ 485 MPa)
        Cv2 = 1.0
        Vn_per_flange = 0.6 * sec.Fy * sec.bf * sec.tf * Cv2
        # I-shape: 2 flanges sebagai shear resisting elements
        Vn_total = 2 * Vn_per_flange
        return phi * Vn_total


# ═══════════════════════════════════════════════════════════════════════════════
# CHAPTER H — COMBINED FORCES (PMM)
# ═══════════════════════════════════════════════════════════════════════════════

class PMMCheck:
    """AISC Chapter H1 — Doubly symmetric members under combined forces."""

    @staticmethod
    def check(forces: DesignForces, cap: DesignCapacity) -> PMMResult:
        """
        H1-1: Combined axial force and flexure.
        Returns PMMResult with DCR.
        """
        result = PMMResult()

        # Applied forces (absolute values for design)
        Pr = abs(forces.P)
        MrMajor = abs(forces.Mz)  # Mz = major moment (Revit)
        MrMinor = abs(forces.My)  # My = minor moment (Revit)

        result.Pr = forces.P      # Keep sign for reporting
        result.MrMajor = forces.Mz
        result.MrMinor = forces.My

        # Select Pc based on compression or tension
        if forces.P >= 0:
            # Compression (P positive = compression in our convention)
            Pc = cap.PcComp if cap.PcComp > 0 else 1.0
        else:
            # Tension
            Pc = cap.PcTension if cap.PcTension > 0 else 1.0

        result.Pc_used = Pc

        Mcx = cap.McMajor if cap.McMajor > 0 else 1.0
        Mcy = cap.McMinor if cap.McMinor > 0 else 1.0

        pr_pc = Pr / Pc

        # Moment ratios
        mrx_mcx = MrMajor / Mcx
        mry_mcy = MrMinor / Mcy

        if pr_pc >= 0.2:
            # H1-1a
            result.equation = "H1-1a"
            result.TotalRatio = pr_pc + (8.0 / 9.0) * (mrx_mcx + mry_mcy)
        else:
            # H1-1b
            result.equation = "H1-1b"
            result.TotalRatio = pr_pc / 2.0 + (mrx_mcx + mry_mcy)

        result.PRatio = pr_pc
        result.MMajRatio = mrx_mcx
        result.MMinRatio = mry_mcy

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD COMBINER — SNI 1727-2020 LRFD (Hybrid: Default + Custom)
# ═══════════════════════════════════════════════════════════════════════════════

class LoadCombiner:
    """Combine station forces from individual load cases into DSTL combos.
    
    Supports hybrid mode:
      - "default": 10 DSTL SNI 1727-2020
      - "custom":  only user-defined combos
      - "both":    default + custom
    """

    # 10 DSTL default combinations (SNI 1727-2020 Pasal 4.2.2 LRFD)
    DEFAULT_COMBINATIONS = {
        "DSTL1":  {"SelfWeight": 1.4, "ADL": 1.4},
        "DSTL2":  {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 1.6},
        "DSTL3":  {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 1.0, "SeismicX": 1.0},
        "DSTL4":  {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 1.0, "SeismicX": -1.0},
        "DSTL5":  {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 1.0, "SeismicY": 1.0},
        "DSTL6":  {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 1.0, "SeismicY": -1.0},
        "DSTL7":  {"SelfWeight": 0.9, "ADL": 0.9, "SeismicX": 1.0},
        "DSTL8":  {"SelfWeight": 0.9, "ADL": 0.9, "SeismicX": -1.0},
        "DSTL9":  {"SelfWeight": 0.9, "ADL": 0.9, "SeismicY": 1.0},
        "DSTL10": {"SelfWeight": 0.9, "ADL": 0.9, "SeismicY": -1.0},
    }

    DEFAULT_LABELS = {
        "DSTL1":  "1.4(D+ADL)",
        "DSTL2":  "1.2(D+ADL) + 1.6L",
        "DSTL3":  "1.2(D+ADL) + 1.0L + 1.0EQx",
        "DSTL4":  "1.2(D+ADL) + 1.0L - 1.0EQx",
        "DSTL5":  "1.2(D+ADL) + 1.0L + 1.0EQy",
        "DSTL6":  "1.2(D+ADL) + 1.0L - 1.0EQy",
        "DSTL7":  "0.9(D+ADL) + 1.0EQx",
        "DSTL8":  "0.9(D+ADL) - 1.0EQx",
        "DSTL9":  "0.9(D+ADL) + 1.0EQy",
        "DSTL10": "0.9(D+ADL) - 1.0EQy",
    }

    # Active combinations (populated by build())
    COMBINATIONS = dict(DEFAULT_COMBINATIONS)
    COMBO_LABELS = dict(DEFAULT_LABELS)

    @classmethod
    def build(cls, combo_config: dict = None, available_patterns: list = None):
        """Build active combinations based on config.
        
        Args:
            combo_config: {"mode": "default"|"custom"|"both",
                           "custom_combinations": {"COMB1": {...}, ...}}
            available_patterns: list of pattern names from analysis results
        """
        mode = (combo_config or {}).get("mode", "default")
        custom = (combo_config or {}).get("custom_combinations", {})
        
        if mode in ("default", "both"):
            cls.COMBINATIONS = dict(cls.DEFAULT_COMBINATIONS)
            cls.COMBO_LABELS = dict(cls.DEFAULT_LABELS)
        else:  # custom only
            cls.COMBINATIONS = {}
            cls.COMBO_LABELS = {}
        
        # Add custom combinations
        if mode in ("custom", "both") and custom:
            for name, factors in custom.items():
                cls.COMBINATIONS[name] = factors
                cls.COMBO_LABELS[name] = cls._auto_label(name, factors)
        
        # Validate: warn if combo references pattern not in results
        if available_patterns:
            for combo_name, factors in cls.COMBINATIONS.items():
                for pat in factors:
                    if pat not in available_patterns:
                        print(f"  [WARN] Combo '{combo_name}' references "
                              f"'{pat}' — not found in analysis results")
        
        print(f"  Load Combiner: mode='{mode}', "
              f"{len(cls.COMBINATIONS)} active combinations")

    @staticmethod
    def _auto_label(name: str, factors: dict) -> str:
        """Generate human-readable label for a custom combination."""
        parts = []
        for pat, fac in factors.items():
            if fac == 1.0:
                parts.append(pat)
            elif fac == -1.0:
                parts.append(f"-{pat}")
            else:
                parts.append(f"{fac}{pat}")
        return " + ".join(parts)

    @staticmethod
    def combine_station(station_idx: int, elem_id: str,
                        analysis: Dict, combo_name: str) -> DesignForces:
        """
        Combine forces at a specific station index for a given combo.
        """
        factors = LoadCombiner.COMBINATIONS[combo_name]
        result = DesignForces()
        force_keys = ["P", "Fy", "Fz", "T", "My", "Mz"]

        for lc_name, factor in factors.items():
            if lc_name not in analysis:
                continue
            lc_data = analysis[lc_name]
            if "elements" not in lc_data or elem_id not in lc_data["elements"]:
                continue

            elem_data = lc_data["elements"][elem_id]
            stations = elem_data.get("stations", [])

            if station_idx < len(stations):
                stn = stations[station_idx]
                for key in force_keys:
                    if key in stn:
                        current = getattr(result, key)
                        setattr(result, key, current + factor * stn[key])

        return result

    @staticmethod
    def get_station_distances(elem_id: str, analysis: Dict) -> List[float]:
        """Get station distances for an element from any available load case."""
        for lc_name in analysis.keys():
            if lc_name.startswith('_'):
                continue
            lc_data = analysis[lc_name]
            if isinstance(lc_data, dict) and "elements" in lc_data and elem_id in lc_data["elements"]:
                stations = lc_data["elements"][elem_id].get("stations", [])
                return [s.get("distance_mm", s.get("station", 0.0) * 1.0) for s in stations]
        return [0.0]

    @staticmethod
    def get_num_stations(elem_id: str, analysis: Dict) -> int:
        """Get number of stations for an element."""
        for lc_name in analysis.keys():
            if lc_name.startswith('_'):
                continue
            lc_data = analysis[lc_name]
            if isinstance(lc_data, dict) and "elements" in lc_data and elem_id in lc_data["elements"]:
                return len(lc_data["elements"][elem_id].get("stations", []))
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# STEEL DESIGN ENGINE — Main Runner
# ═══════════════════════════════════════════════════════════════════════════════

class SteelDesignEngine:
    """Main engine: reads Result.json, designs all elements, writes output."""

    def __init__(self, result_path: str, output_path: str):
        self.result_path = result_path
        self.output_path = output_path
        self.data = None
        self.results: List[ElementDesignResult] = []

    def run(self):
        """Execute the full design workflow."""
        print("=" * 70)
        print("  AISC 360-22 - STEEL DESIGN ENGINE (LRFD, SI Units)")
        print("  Kombinasi Beban: SNI 1727-2020")
        print("=" * 70)

        # 1. Load data
        print(f"\n  Membaca: {self.result_path}")
        with open(self.result_path, 'r') as f:
            self.data = json.load(f)

        model_elements = self.data["model_data"]["model_elements"]
        analysis = self.data["analysis_results"]

        # 2. Build load combinations (hybrid: default + custom)
        combo_config = self.data.get("model_data", {}).get("load_combination_config", None)
        available = [k for k in analysis.keys() if not k.startswith('_')]
        LoadCombiner.build(combo_config, available)

        print(f"  Jumlah elemen: {len(model_elements)}")
        print(f"  Load patterns: {available}")
        print(f"  Kombinasi desain: {len(LoadCombiner.COMBINATIONS)}")

        # 2. Design each element
        print(f"\n{'-' * 70}")
        print(f"  {'Frame':<8} {'Type':<8} {'Section':<22} {'Ratio':>8} {'Combo':<8} {'Status'}")
        print(f"{'-' * 70}")

        for elem in model_elements:
            result = self._design_element(elem, analysis)
            self.results.append(result)

            status_mark = "OK" if result.status == "OK" else "NG"
            print(f"  {result.frame_label:<8} {result.design_type:<8} "
                  f"{result.design_section:<22} {result.governing_ratio:>8.4f} "
                  f"{result.governing_combo:<8} {status_mark} {result.status}")

        # 3. Summary
        passed = sum(1 for r in self.results if r.status == "OK")
        failed = len(self.results) - passed
        max_dcr = max(self.results, key=lambda r: r.governing_ratio)

        print(f"\n{'-' * 70}")
        print(f"  RINGKASAN:")
        print(f"  Total elemen : {len(self.results)}")
        print(f"  Passed (OK)  : {passed}")
        print(f"  Failed (NG)  : {failed}")
        print(f"  Max DCR      : {max_dcr.governing_ratio:.4f} "
              f"({max_dcr.frame_label}, {max_dcr.governing_combo})")
        print(f"{'=' * 70}")

        # 4. Build fabrication groups for columns
        self.fab_groups = self._build_fabrication_groups()

        # 5. Write output
        self._write_output()
        print(f"\n  Output ditulis: {self.output_path}")

    def _design_element(self, elem: Dict, analysis: Dict) -> ElementDesignResult:
        """Design a single element across all DSTL combos and stations."""

        # --- Extract properties ---
        sec = self._extract_section(elem)
        dp = elem.get("design_parameters", {})
        Kx = dp.get("Kx", 1.0)
        Ky = dp.get("Ky", 1.0)
        Kz = dp.get("Kz", 1.0)        # E4: Torsional K factor (default 1.0)
        Lx = dp.get("Lx_mm", 4000.0)
        Ly = dp.get("Ly_mm", 4000.0)
        Lb = dp.get("Lb_mm", Ly)
        Lcz = dp.get("Lcz_mm", Ly)    # E4: Torsional unbraced length (default=Ly)

        elem_id = str(elem["id"])
        elem_type = elem.get("type", "Column")
        frame_label = elem.get("frame_label", "?")
        section_name = sec.name

        # --- Classify section ---
        classifier = SectionClassification(sec)
        axial_class = classifier.classify_axial()
        flex_class = classifier.classify_flexure()

        # --- Compute capacities (once per element) ---
        PcTension = TensionCapacity.design(sec)
        PcComp = CompressionCapacity.design(sec, Kx, Ky, Lx, Ly, axial_class,
                                            Kz=Kz, Lcz=Lcz)

        # For Cb: collect major moments from DeadLoad (or use default 1.0 initially)
        # We'll compute Cb per combo later if needed; use design_parameters Cb as default
        Cb_default = dp.get("Cb", 1.0)

        McMajor = FlexureCapacity.design_major(sec, Lb, Cb_default, flex_class)
        McMinor = FlexureCapacity.design_minor(sec, flex_class)
        PhiVnMajor = ShearCapacity.design_major(sec)
        PhiVnMinor = ShearCapacity.design_minor(sec)

        cap = DesignCapacity(
            PcComp=PcComp,
            PcTension=PcTension,
            McMajor=McMajor,
            McMinor=McMinor,
            PhiVnMajor=PhiVnMajor,
            PhiVnMinor=PhiVnMinor,
            section_class_axial=axial_class["overall"],
            section_class_flexure_flange=flex_class["flange"],
            section_class_flexure_web=flex_class["web"],
            Cb=Cb_default
        )

        # --- Check all combos × stations ---
        n_stations = LoadCombiner.get_num_stations(elem_id, analysis)
        station_distances = LoadCombiner.get_station_distances(elem_id, analysis)

        best_pmm = PMMResult()
        best_pmm_combo = ""
        best_pmm_location = 0.0

        best_shear = ShearResult()
        best_shear_combo = ""
        best_shear_location = 0.0
        all_station_details = []  # Per-combo per-station (SAP2000 style)

        for combo_name in LoadCombiner.COMBINATIONS:
            # Collect major moments across stations for Cb calculation
            major_moments = []
            for si in range(n_stations):
                f = LoadCombiner.combine_station(si, elem_id, analysis, combo_name)
                major_moments.append(f.Mz)

            # Calculate Cb for this combo
            Cb_combo = FlexureCapacity.calc_Cb(major_moments)
            McMajor_combo = FlexureCapacity.design_major(sec, Lb, Cb_combo, flex_class)

            # Update capacity with combo-specific Cb
            cap_combo = DesignCapacity(
                PcComp=PcComp,
                PcTension=PcTension,
                McMajor=McMajor_combo,
                McMinor=McMinor,
                PhiVnMajor=PhiVnMajor,
                PhiVnMinor=PhiVnMinor,
                section_class_axial=axial_class["overall"],
                section_class_flexure_flange=flex_class["flange"],
                section_class_flexure_web=flex_class["web"],
                Cb=Cb_combo
            )

            for si in range(n_stations):
                forces = LoadCombiner.combine_station(si, elem_id, analysis, combo_name)
                dist = station_distances[si] if si < len(station_distances) else 0.0

                # PMM check
                pmm = PMMCheck.check(forces, cap_combo)
                if pmm.TotalRatio > best_pmm.TotalRatio:
                    best_pmm = pmm
                    best_pmm_combo = combo_name
                    best_pmm_location = dist

                # Shear check
                v_ratio = abs(forces.Fz) / PhiVnMajor if PhiVnMajor > 0 else 0
                v3_ratio = abs(forces.Fy) / PhiVnMinor if PhiVnMinor > 0 else 0
                if v_ratio > best_shear.VMajorRatio or best_shear_combo == "":
                    best_shear = ShearResult(
                        VrMajor=forces.Fz,
                        PhiVnMajor=PhiVnMajor,
                        VMajorRatio=v_ratio
                    )
                    best_shear_combo = combo_name
                    best_shear_location = dist

                # Collect per-station detail (SAP2000 style)
                all_station_details.append({
                    "combo": combo_name,
                    "location_mm": round(dist, 1),
                    "ratio": round(pmm.TotalRatio, 6),
                    "axl": round(pmm.PRatio, 6),
                    "b_maj": round(pmm.MMajRatio, 6),
                    "b_min": round(pmm.MMinRatio, 6),
                    "equation": pmm.equation,
                    "maj_shr": round(v_ratio, 6),
                    "min_shr": round(v3_ratio, 6)
                })

        # --- Determine governing ---
        governing_ratio = max(best_pmm.TotalRatio, best_shear.VMajorRatio)
        if best_pmm.TotalRatio >= best_shear.VMajorRatio:
            gov_combo = best_pmm_combo
            gov_location = best_pmm_location
            ratio_type = "PMM"
        else:
            gov_combo = best_shear_combo
            gov_location = best_shear_location
            ratio_type = "Shear"

        status = "OK" if governing_ratio <= 1.0 else "Overstressed"

        # Update cap with best Cb for reporting
        cap.Cb = best_pmm.Pc_used  # will be overwritten below
        # Find the Cb that was used for the governing PMM combo
        if best_pmm_combo:
            gov_moments = []
            for si in range(n_stations):
                f = LoadCombiner.combine_station(si, elem_id, analysis, best_pmm_combo)
                gov_moments.append(f.Mz)
            cap.Cb = FlexureCapacity.calc_Cb(gov_moments)
            cap.McMajor = FlexureCapacity.design_major(sec, Lb, cap.Cb, flex_class)

        return ElementDesignResult(
            element_id=elem["id"],
            frame_label=frame_label,
            design_type=elem_type,
            design_section=section_name,
            status=status,
            governing_ratio=governing_ratio,
            ratio_type=ratio_type,
            governing_combo=gov_combo,
            governing_location_mm=gov_location,
            capacity=cap,
            pmm_detail=best_pmm,
            shear_detail=best_shear,
            shear_combo=best_shear_combo,
            shear_location_mm=best_shear_location,
            station_details=all_station_details
        )

    def _extract_section(self, elem: Dict) -> SectionProperties:
        """Extract SectionProperties from Result.json element data."""
        sec_data = elem.get("section", {})
        mat_data = elem.get("material", {})

        E = mat_data.get("E_MPa", 200000.0)
        Fy = mat_data.get("Fy_MPa", 250.0)
        Fu = mat_data.get("Fu_MPa", 400.0)

        return SectionProperties(
            name=elem.get("family", elem.get("frame_label", "Unknown")),
            d=sec_data.get("d_mm", 0),
            bf=sec_data.get("b_mm", 0),
            tf=sec_data.get("tf_mm", 0),
            tw=sec_data.get("tw_mm", 0),
            A=sec_data.get("Area_mm2", 0),
            Ix=sec_data.get("Iz_mm4", 0),     # Iz in Result.json = Ix (major)
            Iy=sec_data.get("Iy_mm4", 0),
            Sx=sec_data.get("Sz_mm3", 0),     # Sz = Sx
            Sy=sec_data.get("Sy_mm3", 0),
            Zx=sec_data.get("Zz_mm3", 0),     # Zz = Zx
            Zy=sec_data.get("Zy_mm3", 0),
            rx=sec_data.get("rz_mm", 0),      # rz = rx
            ry=sec_data.get("ry_mm", 0),
            J=sec_data.get("J_mm4", 0),
            Cw=sec_data.get("Cw_mm6", 0),
            E=E,
            G=E / (2 * (1 + mat_data.get("Nu", 0.3))),
            Fy=Fy,
            Fu=Fu
        )

    def _build_fabrication_groups(self) -> List[Dict]:
        """Group columns by (X,Y) coordinate and compute max DCR per fabrication segment."""
        model_elements = self.data["model_data"]["model_elements"]
        seismic_params = self.data.get("model_data", {}).get("seismic_parameters", {})
        
        FAB_MAX = seismic_params.get("COL_FAB_MAX_LENGTH_MM", 12000) if seismic_params else 12000
        SPLICE_OFFSET = seismic_params.get("COL_SPLICE_OFFSET_MM", 1500) if seismic_params else 1500
        HEIGHT_MM = seismic_params.get("HEIGHT_MM", 4000) if seismic_params else 4000
        N_STORY = seismic_params.get("N_STORY", 2) if seismic_params else 2
        effective_max = FAB_MAX - SPLICE_OFFSET
        
        # Build elem_id -> result mapping
        result_map = {r.element_id: r for r in self.results}
        
        # Build elem_id -> model_element mapping (for topology)
        elem_map = {}
        for elem in model_elements:
            elem_map[elem["id"]] = elem
        
        # Group columns by (X, Y) coordinate
        col_groups = {}  # key: (x_rounded, y_rounded) -> list of (elem_id, base_z)
        for r in self.results:
            if r.design_type != "Column":
                continue
            elem = elem_map.get(r.element_id)
            if not elem:
                continue
            topo = elem.get("topology", {})
            start = topo.get("start_node", [0, 0, 0])
            x, y, z = round(start[0], 0), round(start[1], 0), start[2]
            key = (x, y)
            col_groups.setdefault(key, []).append({
                "element_id": r.element_id,
                "frame_label": r.frame_label,
                "base_z": z,
                "governing_ratio": r.governing_ratio,
                "governing_combo": r.governing_combo,
                "status": r.status
            })
        
        # Calculate fabrication segments for each grid position
        level_elevations = [k * HEIGHT_MM for k in range(N_STORY + 1)]
        
        # Determine segment boundaries (same logic as script.py)
        total_height = N_STORY * HEIGHT_MM
        fab_seg_boundaries = []  # List of (base_idx, top_idx)
        if total_height <= effective_max:
            fab_seg_boundaries = [(0, N_STORY)]
        else:
            seg_start = 0
            for k in range(1, N_STORY + 1):
                base_elev = level_elevations[seg_start]
                if seg_start > 0:
                    base_elev += SPLICE_OFFSET
                top_splice = level_elevations[k] + SPLICE_OFFSET
                seg_length = top_splice - base_elev
                
                if seg_length >= FAB_MAX:
                    if k - 1 > seg_start:
                        fab_seg_boundaries.append((seg_start, k - 1))
                        seg_start = k - 1
                
                if k == N_STORY:
                    fab_seg_boundaries.append((seg_start, k))
        
        # Build output with group naming (A, B, C, ...)
        fab_groups_output = []
        group_letter_idx = 0
        for (gx, gy), columns in sorted(col_groups.items()):
            # Sort columns by base elevation
            columns.sort(key=lambda c: c["base_z"])
            
            # Assign columns to fabrication segments
            segments = []
            for seg_idx, (seg_base_idx, seg_top_idx) in enumerate(fab_seg_boundaries):
                seg_base_elev = level_elevations[seg_base_idx]
                seg_top_elev = level_elevations[seg_top_idx]
                
                # Find columns within this segment elevation range
                seg_columns = [c for c in columns
                               if c["base_z"] >= seg_base_elev - 1 and c["base_z"] < seg_top_elev + 1]
                
                if seg_columns:
                    max_dcr_col = max(seg_columns, key=lambda c: c["governing_ratio"])
                    segments.append({
                        "segment_id": seg_idx + 1,
                        "base_level": seg_base_idx + 1,
                        "top_level": seg_top_idx + 1,
                        "elements": [c["element_id"] for c in seg_columns],
                        "frame_labels": [c["frame_label"] for c in seg_columns],
                        "fab_dcr": round(max_dcr_col["governing_ratio"], 6),
                        "governing_element": max_dcr_col["element_id"],
                        "governing_combo": max_dcr_col["governing_combo"]
                    })
            
            if segments:
                # Generate group name: A, B, C, ... AA, AB, ...
                if group_letter_idx < 26:
                    group_name = chr(65 + group_letter_idx)  # A-Z
                else:
                    group_name = chr(64 + group_letter_idx // 26) + chr(65 + group_letter_idx % 26)
                
                overall_max = max(s["fab_dcr"] for s in segments)
                fab_groups_output.append({
                    "group_name": f"Column Group {group_name}",
                    "grid_position": f"({gx:.0f}, {gy:.0f})",
                    "grid_x_mm": gx,
                    "grid_y_mm": gy,
                    "total_segments": len(segments),
                    "max_fab_dcr": round(overall_max, 6),
                    "segments": segments
                })
                group_letter_idx += 1
        
        # Console output
        if fab_groups_output:
            print(f"\n{'=' * 70}")
            print(f"  FABRICATION COLUMN GROUPS - Max DCR per Group")
            print(f"{'=' * 70}")
            print(f"  {'Group':<18} {'Grid Position':<20} {'Seg':>4} {'Max DCR':>10} {'Status'}")
            print(f"{'=' * 70}")
            for g in fab_groups_output:
                status = "OK" if g["max_fab_dcr"] <= 1.0 else "NG"
                print(f"  {g['group_name']:<18} {g['grid_position']:<20} {g['total_segments']:>4} "
                      f"{g['max_fab_dcr']:>10.4f} {status}")
            print(f"{'=' * 70}")
            max_fab = max(fab_groups_output, key=lambda g: g["max_fab_dcr"])
            print(f"  Max Fab DCR: {max_fab['max_fab_dcr']:.4f} - {max_fab['group_name']} {max_fab['grid_position']}")
        
        return fab_groups_output

    def _write_output(self):
        """Write Design Result.json."""
        output = {
            "design_info": {
                "code": "AISC 360-22",
                "method": "LRFD",
                "framing_type": "SRPMK",
                "units": {
                    "force": "kN",
                    "moment": "kN-m",
                    "stress": "MPa",
                    "length": "mm"
                },
                "combinations": LoadCombiner.COMBO_LABELS
            },
            "summary": {
                "total_elements": len(self.results),
                "passed": sum(1 for r in self.results if r.status == "OK"),
                "failed": sum(1 for r in self.results if r.status != "OK"),
                "max_DCR": None
            },
            "elements": [],
            "fabrication_groups": []
        }

        # Find max DCR
        if self.results:
            max_r = max(self.results, key=lambda r: r.governing_ratio)
            output["summary"]["max_DCR"] = {
                "element_id": max_r.element_id,
                "frame_label": max_r.frame_label,
                "DCR": round(max_r.governing_ratio, 6)
            }

        # Per-element results
        for r in self.results:
            elem_out = {
                "element_id": r.element_id,
                "frame_label": r.frame_label,
                "design_type": r.design_type,
                "design_section": r.design_section,
                "status": r.status,
                "governing_ratio": round(r.governing_ratio, 6),
                "ratio_type": r.ratio_type,
                "governing_combo": r.governing_combo,
                "governing_location_mm": round(r.governing_location_mm, 1),

                "capacity": {
                    "PcComp_kN": round(r.capacity.PcComp / 1000, 2),
                    "PcTension_kN": round(r.capacity.PcTension / 1000, 2),
                    "McMajor_kNm": round(r.capacity.McMajor / 1e6, 2),
                    "McMinor_kNm": round(r.capacity.McMinor / 1e6, 2),
                    "PhiVnMajor_kN": round(r.capacity.PhiVnMajor / 1000, 2),
                    "section_class_axial": r.capacity.section_class_axial,
                    "section_class_flexure_flange": r.capacity.section_class_flexure_flange,
                    "section_class_flexure_web": r.capacity.section_class_flexure_web,
                    "Cb": round(r.capacity.Cb, 4)
                },

                "pmm_detail": {
                    "combo": r.governing_combo if r.ratio_type == "PMM" else
                             (r.pmm_detail.equation if r.pmm_detail else ""),
                    "location_mm": round(r.governing_location_mm, 1),
                    "Pr_kN": round(r.pmm_detail.Pr / 1000, 2) if r.pmm_detail else 0,
                    "MrMajor_kNm": round(r.pmm_detail.MrMajor / 1e6, 2) if r.pmm_detail else 0,
                    "MrMinor_kNm": round(r.pmm_detail.MrMinor / 1e6, 2) if r.pmm_detail else 0,
                    "equation": r.pmm_detail.equation if r.pmm_detail else "",
                    "PRatio": round(r.pmm_detail.PRatio, 6) if r.pmm_detail else 0,
                    "MMajRatio": round(r.pmm_detail.MMajRatio, 6) if r.pmm_detail else 0,
                    "MMinRatio": round(r.pmm_detail.MMinRatio, 6) if r.pmm_detail else 0,
                    "TotalRatio": round(r.pmm_detail.TotalRatio, 6) if r.pmm_detail else 0
                },

                "shear_detail": {
                    "VMajorCombo": r.shear_combo,
                    "VMajorLocation_mm": round(r.shear_location_mm, 1),
                    "VMajorRatio": round(r.shear_detail.VMajorRatio, 6) if r.shear_detail else 0,
                    "VrMajDsgn_kN": round(r.shear_detail.VrMajor / 1000, 2) if r.shear_detail else 0,
                    "PhiVnMajor_kN": round(r.shear_detail.PhiVnMajor / 1000, 2) if r.shear_detail else 0
                },

                "station_details": r.station_details if r.station_details else []
            }
            output["elements"].append(elem_out)

        # Add fabrication groups
        output["fabrication_groups"] = self.fab_groups if self.fab_groups else []

        with open(self.output_path, 'w') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Entry point for Steel Design Engine."""
    # Default paths (relative to Create.pushbutton)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    create_dir = os.path.normpath(os.path.join(script_dir, "..", "..",
                                                "Create.pushbutton"))

    result_path = os.path.join(create_dir, "Result.json")
    output_path = os.path.join(script_dir, "Design Result.json")

    # Allow command-line override
    if len(sys.argv) >= 2:
        result_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]

    if not os.path.exists(result_path):
        print(f"ERROR: Result.json tidak ditemukan: {result_path}")
        sys.exit(1)

    engine = SteelDesignEngine(result_path, output_path)
    engine.run()


if __name__ == "__main__":
    main()
