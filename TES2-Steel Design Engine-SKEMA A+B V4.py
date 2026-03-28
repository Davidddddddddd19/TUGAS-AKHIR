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
    """Design forces at a specific station (N, N·mm). OpenSees/Revit convention.

    RAW from Result.json (before beam axis swap):
      Columns: Mz = major (strong axis), My = minor (weak axis)
      Beams:   My = major (strong axis), Mz = minor (weak axis)

    After beam axis swap in _design_element, My/Mz are normalized so
    Mz always = major and My always = minor for both element types.
    """
    P: float = 0.0    # Axial force (N) — negative = compression, positive = tension
    Fy: float = 0.0   # Shear (N) — minor after swap
    Fz: float = 0.0   # Shear (N) — major after swap
    T: float = 0.0    # Torsion (N·mm)
    My: float = 0.0   # Moment (N·mm) — minor after swap
    Mz: float = 0.0   # Moment (N·mm) — major after swap


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
    Lb: float = 0.0              # Governing unbraced length (mm)


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
    B1_major: float = 1.0    # AISC C2 non-sway amplifier, major axis
    B1_minor: float = 1.0    # AISC C2 non-sway amplifier, minor axis
    B2: float = 1.0           # AISC C2 sway amplifier
    Cm_major: float = 1.0    # Cm factor, major axis
    Cm_minor: float = 1.0    # Cm factor, minor axis


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
    label_name: str = ""
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
    smf_checks: Optional[Dict] = None      # SMF-specific checks (AISC 341)
    # --- Autoselect augmentation fields ---
    group: str = ""                         # "Kolom" / "Balok Induk" / "Balok Anak"
    K_factors: Optional[Dict] = None       # {"Kx": float, "Ky": float, "Kz": float}
    lengths: Optional[Dict] = None         # {"Lx_mm", "Ly_mm", "Lb_mm", "Lcz_mm"}
    material_props: Optional[Dict] = None  # {"Fy_MPa", "Fu_MPa", "E_MPa"}


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

    def classify_seismic(self, frame_type: str, Ry: float = 1.0,
                         Ca: float = 0.0) -> Dict:
        """
        AISC 341-22 Table D1.1b — Seismic ductility classification.
        
        SRPMK/SMF: Members harus memenuhi highly ductile limits (lambda_hd).
        SRPMB/OMF: Tidak ada persyaratan seismik tambahan.
        
        Ca = Pr / (Fy * Ag), capped [0, 1]
        """
        if frame_type not in ["SRPMK/SMF", "SMF"]:
            return {"seismic_class": "N/A", "ok": True,
                    "message": "Tidak diperlukan untuk {}".format(frame_type)}
        
        E = self.E
        Fy = self.Fy
        RyFy = Ry * Fy
        sqrt_E_RyFy = math.sqrt(E / RyFy) if RyFy > 0 else 0
        
        # Flange: Table D1.1b Case 7 — Flanges of rolled I-shapes
        # lambda_hd = 0.30 * sqrt(E / (Ry * Fy))
        lambda_hd_flange = 0.30 * sqrt_E_RyFy
        flange_ok = self.lambda_flange <= lambda_hd_flange
        
        # Web: Table D1.1b Case 11 — Webs in flexure/combined
        # lambda_hd = 2.5 * (1 - Ca)^2.3 * sqrt(E / (Ry * Fy))
        Ca_eff = max(0.0, min(Ca, 1.0))
        lambda_hd_web = 2.5 * ((1.0 - Ca_eff) ** 2.3) * sqrt_E_RyFy
        web_ok = self.lambda_web <= lambda_hd_web
        
        ok = flange_ok and web_ok
        
        return {
            "seismic_class": "highly_ductile" if ok else "NG",
            "ok": ok,
            "flange_ok": flange_ok,
            "web_ok": web_ok,
            "lambda_flange": round(self.lambda_flange, 3),
            "lambda_hd_flange": round(lambda_hd_flange, 3),
            "lambda_web": round(self.lambda_web, 3),
            "lambda_hd_web": round(lambda_hd_web, 3),
            "Ca": round(Ca_eff, 4),
            "Ry": Ry
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
# K FACTOR CALCULATOR — AISC 360-22 Commentary C-A-7 (Nomogram)
# ═══════════════════════════════════════════════════════════════════════════════

class KFactorCalculator:
    """
    Calculate effective length factor K using AISC alignment chart (nomogram).

    Two methods available:
    - solve_K_uninhibited: Fig. C-A-7.2, Eq. C-A-7-2 (moment frame, K >= 1.0)
    - solve_K_inhibited:   Fig. C-A-7.1, Eq. C-A-7-1 (braced frame, K <= 1.0)

    For OMF (moment frame, sidesway uninhibited), use uninhibited nomogram.
    Reference: AISC 360-22 Commentary Appendix 7, Eq. C-A-7-2 and C-A-7-3.
    """

    @staticmethod
    def build_topology_map(model_elements: List[Dict], support_type: str = "fixed") -> Dict:
        """
        Build node-to-elements connectivity map from model data.
        Returns: {
            node_id: {
                'columns': [(elem_dict, 'start'/'end'), ...],
                'beams':   [(elem_dict, 'start'/'end'), ...],
                'is_support': bool
            }
        }
        """
        node_map = {}
        for elem in model_elements:
            topo = elem.get("topology", {})
            start_id = str(topo.get("start_node_id", ""))
            end_id = str(topo.get("end_node_id", ""))
            start_desc = topo.get("start_node_desc", "")
            end_desc = topo.get("end_node_desc", "")
            elem_type = elem.get("type", "")
            kind = "columns" if elem_type == "Column" else "beams"

            for nid, end_label, desc in [(start_id, "start", start_desc),
                                          (end_id, "end", end_desc)]:
                if nid not in node_map:
                    node_map[nid] = {
                        "columns": [], "beams": [],
                        "is_support": "Support" in desc,
                        "support_type": support_type.lower()  # From user config
                    }
                node_map[nid][kind].append((elem, end_label))
                # Update support flag if any desc says support
                if "Support" in desc:
                    node_map[nid]["is_support"] = True
        return node_map

    @staticmethod
    def _get_beam_horiz_dir(elem: Dict) -> Optional[Tuple[float, float]]:
        """Get beam's unit direction in horizontal plane from topology coords."""
        topo = elem.get("topology", {})
        sn = topo.get("start_node", [0, 0, 0])
        en = topo.get("end_node", [0, 0, 0])
        dx = float(en[0]) - float(sn[0])
        dy = float(en[1]) - float(sn[1])
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1e-6:
            return None
        return (dx / length, dy / length)

    @staticmethod
    def _dirs_parallel(dir_2d: Tuple[float, float], plane_dir: List[float],
                       cos_tol: float = 0.7) -> bool:
        """Check if beam 2D direction is approximately parallel to plane_dir.
        Uses |dot product| > cos_tol (default 0.7 ≈ 45°, sufficient for orthogonal grids).
        """
        d2x = plane_dir[0] if len(plane_dir) > 0 else 0.0
        d2y = plane_dir[1] if len(plane_dir) > 1 else 0.0
        dot = abs(dir_2d[0] * d2x + dir_2d[1] * d2y)
        return dot > cos_tol

    @staticmethod
    def _get_EI_L(elem: Dict, axis: str) -> float:
        """
        Calculate EI/L for an element along specified axis.
        axis: 'major' uses Iz (strong axis), 'minor' uses Iy (weak axis).
        """
        sec = elem.get("section", {})
        mat = elem.get("material", {})
        topo = elem.get("topology", {})
        E = mat.get("E_MPa", 200000.0)
        if axis == "major":
            I = sec.get("Iz_mm4", 0)
        else:
            I = sec.get("Iy_mm4", 0)
        L = topo.get("length_mm", 1.0)
        if L <= 0:
            L = 1.0
        return E * I / L

    @staticmethod
    def calc_G(node_id: str, node_map: Dict, axis: str = "major",
               plane_dir: Optional[List[float]] = None) -> float:
        """
        Calculate G factor at a node per AISC C-A-7-3:
        G = Σ(EI/L)_columns / Σ(EI/L)_girders

        Only beams in the plane of bending are counted (plane_dir filter).
        Beams always contribute strong-axis (Iz) stiffness.

        Boundary conditions (matching SAP2000):
        - Fixed base: G = 0.0 (theoretical; no rotation at fixed support)
        - Pinned base: G = 10.0 (essentially free rotation)
        - No beams connected: G = 10.0 (conservative)
        """
        node_info = node_map.get(node_id, {})

        # Support node
        if node_info.get("is_support", False):
            support_type = node_info.get("support_type", "fixed")
            if support_type == "pinned":
                # Theoretical G = infinity for true pin (zero rotational
                # restraint).  AISC Commentary recommends G=10 for the
                # graphical nomograph, but the numerical solver handles
                # the exact limit.  A large value reproduces SAP2000's
                # alignment-chart K2 values.
                return 1e5
            else:  # fixed — theoretical G=0 (SAP2000 default)
                return 0.0

        # Sum EI/L for columns at this node
        sum_col = 0.0
        for (elem, _) in node_info.get("columns", []):
            sum_col += KFactorCalculator._get_EI_L(elem, axis)

        # Sum EI/L for beams at this node — only beams in the plane of bending
        # Beams always contribute their STRONG axis (Iz) stiffness to G,
        # because beams restrain column rotation through strong-axis bending
        # regardless of which column axis is being checked (AISC C-A-7-3).
        sum_beam = 0.0
        for (elem, _) in node_info.get("beams", []):
            if plane_dir is not None:
                beam_dir = KFactorCalculator._get_beam_horiz_dir(elem)
                if beam_dir is None:
                    continue
                if not KFactorCalculator._dirs_parallel(beam_dir, plane_dir):
                    continue
            sum_beam += KFactorCalculator._get_EI_L(elem, "major")

        if sum_beam <= 0:
            return 10.0  # No beams → conservative (cantilever-like)

        return sum_col / sum_beam

    @staticmethod
    def solve_K_inhibited(GA: float, GB: float) -> float:
        """
        Effective length factor for sidesway inhibited (braced) frames.
        AISC Eq. C-A-7-1, Fig. C-A-7.1. Dumonteil (1992) approximation.

        K for sidesway inhibited ranges from ~0.5 to 1.0.
        - Both ends fixed (G=1.0): K ≈ 0.78
        - Both ends pinned (G=10):  K ≈ 0.96
        - One fixed, one pinned:    K ≈ 0.86
        """
        num = 3.0 * GA * GB + 1.4 * (GA + GB) + 0.64
        den = 3.0 * GA * GB + 2.0 * (GA + GB) + 1.28
        if den <= 0:
            return 1.0
        K = num / den
        return round(max(0.5, min(K, 1.0)), 4)

    @staticmethod
    def solve_K_uninhibited(GA: float, GB: float) -> float:
        """
        Solve AISC Eq. C-A-7-2 for sidesway uninhibited (moment) frames.
        Fig. C-A-7.2. Newton-Raphson with French approximation initial guess.

        f(K) = [GA·GB·(π/K)² - 36] / [6·(GA + GB)] - (π/K)/tan(π/K) = 0

        K for sidesway uninhibited is always >= 1.0.
        """
        pi = math.pi

        # Dumonteil (1992) approximation for initial guess — works well even
        # when one G is very large (e.g. pinned base, G → ∞).
        numerator = 1.6 * GA * GB + 4.0 * (GA + GB) + 7.5
        denominator = GA + GB + 7.5
        if denominator > 0:
            K = math.sqrt(numerator / denominator)
        else:
            K = 2.0
        K = max(K, 1.001)  # Must be > 1 for sidesway uninhibited

        sum_G = GA + GB
        prod_G = GA * GB

        def f(K_val):
            """Nomogram equation C-A-7-2."""
            x = pi / K_val
            term1 = (prod_G * x * x - 36.0) / (6.0 * sum_G) if sum_G > 0 else 0.0
            tan_x = math.tan(x)
            if abs(tan_x) < 1e-12:
                return 1e10
            term2 = x / tan_x
            return term1 - term2

        def f_prime(K_val):
            """Analytical derivative of f with respect to K."""
            x = pi / K_val
            dx_dK = -pi / (K_val * K_val)
            dterm1_dx = (2.0 * prod_G * x) / (6.0 * sum_G) if sum_G > 0 else 0.0
            dterm1_dK = dterm1_dx * dx_dK
            sin_x = math.sin(x)
            if abs(sin_x) < 1e-12:
                return 1e10
            dterm2_dx = math.cos(x) / sin_x - x / (sin_x * sin_x)
            dterm2_dK = dterm2_dx * dx_dK
            return dterm1_dK - dterm2_dK

        # Newton-Raphson iteration
        for _ in range(50):
            fk = f(K)
            fpk = f_prime(K)
            if abs(fpk) < 1e-15:
                break
            K_new = K - fk / fpk
            if K_new < 1.0:
                K_new = 1.001  # K ≥ 1.0 for sidesway uninhibited
            if abs(K_new - K) < 1e-10:
                K = K_new
                break
            K = K_new

        return max(round(K, 4), 1.0)

    @staticmethod
    def compute_K_for_element(elem: Dict, node_map: Dict) -> Tuple[float, float, Dict]:
        """
        Compute Kx (major) and Ky (minor) for a column element.
        For beams, K = 1.0 (not compression-governed).

        Only beams in the plane of bending are counted for G factor:
        - Major axis (Iz, bending about 3-3): beams running in column's y_axis direction
        - Minor axis (Iy, bending about 2-2): beams running in column's z_axis direction

        Returns: (Kx, Ky, debug_info)
        """
        elem_type = elem.get("type", "Column")
        if elem_type != "Column":
            return (1.0, 1.0, {"method": "beam_default"})

        topo = elem.get("topology", {})
        start_id = str(topo.get("start_node_id", ""))
        end_id = str(topo.get("end_node_id", ""))

        # Column local axes for in-plane beam filtering
        local_axes = elem.get("local_axes", {})
        y_axis = local_axes.get("y_axis")  # Local 2-axis (weak axis direction)
        z_axis = local_axes.get("z_axis")  # Local 3-axis (strong axis direction)

        # Major axis (Iz / strong axis / bending about 3-3):
        # Column buckles in y_axis direction → beams in y_axis direction restrain it
        GA_major = KFactorCalculator.calc_G(start_id, node_map, "major",
                                            plane_dir=y_axis)
        GB_major = KFactorCalculator.calc_G(end_id, node_map, "major",
                                            plane_dir=y_axis)
        Kx = KFactorCalculator.solve_K_uninhibited(GA_major, GB_major)

        # Minor axis (Iy / weak axis / bending about 2-2):
        # Column buckles in z_axis direction → beams in z_axis direction restrain it
        GA_minor = KFactorCalculator.calc_G(start_id, node_map, "minor",
                                            plane_dir=z_axis)
        GB_minor = KFactorCalculator.calc_G(end_id, node_map, "minor",
                                            plane_dir=z_axis)
        Ky = KFactorCalculator.solve_K_uninhibited(GA_minor, GB_minor)

        debug = {
            "method": "nomogram_uninhibited",
            "GA_major": round(GA_major, 4),
            "GB_major": round(GB_major, 4),
            "GA_minor": round(GA_minor, 4),
            "GB_minor": round(GB_minor, 4),
            "Kx": Kx,
            "Ky": Ky
        }
        return (Kx, Ky, debug)

    @staticmethod
    def _col_floor_index(col: Dict, z_levels: List[float]) -> int:
        """Determine which floor (0-based) a column belongs to from its base Z."""
        start = col.get("topology", {}).get("start_node", [0, 0, 0])
        z_base = start[2] if len(start) > 2 else 0.0
        for fi in range(len(z_levels) - 1):
            if z_base < z_levels[fi + 1] - 1.0:
                return fi
        return max(0, len(z_levels) - 2)

    @staticmethod
    def compute_K2_story_stiffness(
        model_elements: List[Dict],
        analysis: Dict,
        seismic_params: Dict
    ) -> Optional[Dict]:
        """
        Compute K2 (sway effective length factor) using the AISC Story
        Stiffness Method (Commentary C-A-7-5 / C-A-7-8).

        Per-floor iteration: each story has its own drift, story shear,
        and set of columns. K2 is computed per-column using its story's data.

        Formula:
            ΣPe₂ = R_M × (V_story × L_story / Δ_H)
            K₂ᵢ  = π × √(EIᵢ / (Pe₂ᵢ × Lᵢ²))

        where Pe₂ᵢ = ΣPe₂ × (EIᵢ/Lᵢ) / Σⱼ(EIⱼ/Lⱼ)  [sum over same-story columns]

        Returns: { elem_id_str: { "Kx": float, "Ky": float, "method": str, ... } }
                 or None if insufficient seismic data.
        """
        # --- Extract per-floor story drift from seismic analysis ---
        eq_x = analysis.get("SeismicX", {})
        eq_y = analysis.get("SeismicY", {})

        drift_x = eq_x.get("drift_pdelta", [])
        drift_y = eq_y.get("drift_pdelta", [])

        if not drift_x or not drift_y:
            return None  # No seismic data → fall back to alignment chart

        n_floors = len(drift_x)
        L_story_default = seismic_params.get("HEIGHT_MM", 4000.0)

        # Build z_levels from grid_system if available, else uniform
        grid = seismic_params.get("_grid_system", {})
        z_levels = sorted(grid.get("z_levels_mm", [])) if grid else []
        if len(z_levels) < 2:
            n_st = seismic_params.get("N_STORY", n_floors)
            z_levels = [i * L_story_default for i in range(n_st + 1)]

        # R_M factor (AISC C-A-7-5): for pure moment frame R_M = 0.85
        R_M = 0.85

        # --- Per-floor story stiffness and sum_Pe2 ---
        floor_data = []  # [{sum_Pe2_x, sum_Pe2_y, k_story_x, k_story_y, L_story}]
        for fi in range(n_floors):
            dx = drift_x[fi] if fi < len(drift_x) else drift_x[-1]
            dy = drift_y[fi] if fi < len(drift_y) else drift_y[-1]

            Vx_N = dx.get("Vx_kN", 0) * 1000.0
            Vy_N = dy.get("Vx_kN", 0) * 1000.0
            delta_xe_x = dx.get("delta_xe_mm", 0)
            delta_xe_y = dy.get("delta_xe_mm", 0)

            # Per-floor story height
            if fi + 1 < len(z_levels):
                L_story = z_levels[fi + 1] - z_levels[fi]
            else:
                L_story = L_story_default

            if delta_xe_x <= 0 or delta_xe_y <= 0 or Vx_N <= 0 or Vy_N <= 0:
                floor_data.append(None)
                continue

            k_story_x = Vx_N / delta_xe_x
            k_story_y = Vy_N / delta_xe_y
            sum_Pe2_x = R_M * Vx_N * L_story / delta_xe_x
            sum_Pe2_y = R_M * Vy_N * L_story / delta_xe_y

            floor_data.append({
                "sum_Pe2_x": sum_Pe2_x, "sum_Pe2_y": sum_Pe2_y,
                "k_story_x": k_story_x, "k_story_y": k_story_y,
                "L_story": L_story,
            })

        # If ALL floors have invalid data, bail out
        if all(fd is None for fd in floor_data):
            return None

        # --- Group columns by floor ---
        columns = [e for e in model_elements if e.get("type") == "Column"]
        if not columns:
            return None

        E_default = 205000.0
        # Build per-column info with floor assignment
        col_info = []
        for col in columns:
            sec = col.get("section", {})
            mat = col.get("material", {})
            topo = col.get("topology", {})
            E = mat.get("E_MPa", E_default)
            Iz = sec.get("Iz_mm4", 0)
            Iy = sec.get("Iy_mm4", 0)
            L = topo.get("length_mm", L_story_default)
            if L <= 0:
                L = L_story_default

            local_axes = col.get("local_axes", {})
            y_axis = local_axes.get("y_axis", [1, 0, 0])
            z_axis = local_axes.get("z_axis", [0, 1, 0])

            major_sway_x = abs(y_axis[0]) > abs(y_axis[1])
            minor_sway_x = abs(z_axis[0]) > abs(z_axis[1])

            EIz_L = E * Iz / L
            EIy_L = E * Iy / L

            fi = KFactorCalculator._col_floor_index(col, z_levels)
            col_info.append({
                "id": str(col["id"]),
                "E": E, "Iz": Iz, "Iy": Iy, "L": L,
                "major_sway_x": major_sway_x,
                "minor_sway_x": minor_sway_x,
                "EIz_L": EIz_L, "EIy_L": EIy_L,
                "floor": fi,
            })

        # --- Per-floor EI/L sums ---
        floors_used = set(c["floor"] for c in col_info)
        floor_EI_sums = {}
        for fi in floors_used:
            cols_fi = [c for c in col_info if c["floor"] == fi]
            floor_EI_sums[fi] = {
                "major_x": sum(c["EIz_L"] for c in cols_fi if c["major_sway_x"]),
                "major_y": sum(c["EIz_L"] for c in cols_fi if not c["major_sway_x"]),
                "minor_x": sum(c["EIy_L"] for c in cols_fi if c["minor_sway_x"]),
                "minor_y": sum(c["EIy_L"] for c in cols_fi if not c["minor_sway_x"]),
            }

        # --- Compute K2 for each column using its floor's data ---
        result = {}
        pi = math.pi
        for c in col_info:
            eid = c["id"]
            E = c["E"]
            L = c["L"]
            fi = c["floor"]

            fd = floor_data[fi] if fi < len(floor_data) else None
            if fd is None:
                # No valid drift for this floor — skip (alignment chart fallback)
                continue

            sums = floor_EI_sums.get(fi, {})
            sum_Pe2_x = fd["sum_Pe2_x"]
            sum_Pe2_y = fd["sum_Pe2_y"]

            # Major axis K2
            if c["major_sway_x"]:
                s = sums.get("major_x", 0)
                Pe2_major = sum_Pe2_x * (c["EIz_L"] / s) if s > 0 and sum_Pe2_x > 0 else 1e20
            else:
                s = sums.get("major_y", 0)
                Pe2_major = sum_Pe2_y * (c["EIz_L"] / s) if s > 0 and sum_Pe2_y > 0 else 1e20

            Kx2 = pi * math.sqrt(E * c["Iz"] / max(Pe2_major * L * L, 1.0))
            Kx = max(Kx2, 1.0)

            # Minor axis K2
            if c["minor_sway_x"]:
                s = sums.get("minor_x", 0)
                Pe2_minor = sum_Pe2_x * (c["EIy_L"] / s) if s > 0 and sum_Pe2_x > 0 else 1e20
            else:
                s = sums.get("minor_y", 0)
                Pe2_minor = sum_Pe2_y * (c["EIy_L"] / s) if s > 0 and sum_Pe2_y > 0 else 1e20

            Ky2 = pi * math.sqrt(E * c["Iy"] / max(Pe2_minor * L * L, 1.0))
            Ky = max(Ky2, 1.0)

            result[eid] = {
                "method": "story_stiffness",
                "Kx": round(Kx, 4),
                "Ky": round(Ky, 4),
                "k_story_x": round(fd["k_story_x"], 2),
                "k_story_y": round(fd["k_story_y"], 2),
                "R_M": R_M,
                "Pe2_major": round(Pe2_major, 1),
                "Pe2_minor": round(Pe2_minor, 1),
                "floor": fi,
            }

        if result:
            kx_sample = list(result.values())[0]["Kx"]
            ky_sample = list(result.values())[0]["Ky"]
            fd0 = floor_data[0] if floor_data and floor_data[0] else {}
            print(f"  Story K2     : Kx={kx_sample:.4f}, Ky={ky_sample:.4f} "
                  f"(R_M={R_M}, k_X={fd0.get('k_story_x',0):.1f}, "
                  f"k_Y={fd0.get('k_story_y',0):.1f} N/mm)")

            # Store per-floor story data for B2 computation (AISC C2-3)
            result["_stories"] = floor_data
            # Legacy single-story key for backward compatibility
            if floor_data and floor_data[0]:
                result["_story"] = floor_data[0]
                result["_story"]["R_M"] = R_M

        return result


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
        # OpenSees convention: negative P = compression, positive P = tension
        if forces.P <= 0:
            # Compression (P negative = compression in OpenSees convention)
            Pc = cap.PcComp if cap.PcComp > 0 else 1.0
        else:
            # Tension (P positive = tension)
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
# AISC 360-22 Chapter C — MOMENT AMPLIFICATION (B1-B2 METHOD)
# ═══════════════════════════════════════════════════════════════════════════════

class MomentAmplifier:
    """AISC 360-22 C2 — Amplified first-order elastic analysis.

    Second-order effects captured via amplification factors:
        Mr = B1·Mnt + B2·Mlt        (C2-1a)
        Pr = Pnt + B2·Plt            (C2-1b)

    Where:
        Mnt, Pnt = forces from non-sway (gravity) loading
        Mlt, Plt = forces from sway (lateral/seismic) loading
        B1 = non-sway amplifier (P-δ effect within member)
        B2 = sway amplifier (P-Δ effect at story level)
    """

    @staticmethod
    def compute_Cm(M_end_i: float, M_end_j: float) -> float:
        """Cm for members without transverse loading (AISC Eq. C2-2).

        M_end_i, M_end_j: signed non-sway end moments (N·mm).
        Uses internal force sign convention:
          Same sign → reverse curvature → lower Cm
          Opposite sign → single curvature → higher Cm
        """
        absI, absJ = abs(M_end_i), abs(M_end_j)
        if max(absI, absJ) < 1e-6:
            return 1.0
        abs_ratio = min(absI, absJ) / max(absI, absJ)
        if M_end_i * M_end_j > 0:
            # Same sign → reverse curvature
            return 0.6 - 0.4 * abs_ratio
        else:
            # Opposite signs (or one zero) → single curvature
            return 0.6 + 0.4 * abs_ratio

    @staticmethod
    def compute_B1(Pr: float, Pe1: float, Cm: float) -> float:
        """B1 = Cm / (1 - α·Pr/Pe1) ≥ 1.0  (AISC Eq. C2-2).

        Pr  = required axial compressive strength (N, positive = compression)
        Pe1 = π²EI/(K1·L)² with K1 for braced (non-sway) condition
        Cm  = equivalent moment factor
        α   = 1.0 for LRFD
        """
        if Pe1 <= 0 or Pr <= 0:
            return 1.0
        ratio = Pr / Pe1
        if ratio >= 1.0:
            return 10.0  # Flag: member has buckled
        B1 = Cm / (1.0 - ratio)
        return max(B1, 1.0)

    @staticmethod
    def compute_B2(sum_Pr: float, sum_Pe_story: float) -> float:
        """B2 = 1 / (1 - α·ΣPr/ΣPe_story)  (AISC Eq. C2-3).

        sum_Pr       = Σ|Pr| on all columns in story for this combo (N)
        sum_Pe_story = story elastic critical load (N)
                       = R_M · V_story · L / Δ_H  (story stiffness method)
        α = 1.0 for LRFD
        """
        if sum_Pe_story <= 0 or sum_Pr <= 0:
            return 1.0
        ratio = sum_Pr / sum_Pe_story
        if ratio >= 1.0:
            return 10.0  # Flag: story has buckled
        return 1.0 / (1.0 - ratio)

    @staticmethod
    def compute_Pe1(E: float, I: float, L: float, K1: float = 1.0) -> float:
        """Pe1 = π²EI/(K1·L)² — elastic buckling in braced condition."""
        if L <= 0 or K1 <= 0:
            return 1e20
        return math.pi ** 2 * E * I / (K1 * L) ** 2


# ═══════════════════════════════════════════════════════════════════════════════
# AISC 341-22 E3.4a — STRONG COLUMN-WEAK BEAM (SCWB)
# ═══════════════════════════════════════════════════════════════════════════════

class StrongColumnWeakBeam:
    """
    AISC 341-22 E3.4a — Moment ratio check at beam-to-column connections.
    
    ΣMpc* / ΣMbe* > 1.0  (Eq. E3-1)
    
    ΣMpc* = Σ Zc·(Fyc - αs·|Pr|/Ag)      (E3-2)
    ΣMbe* = Σ (Mpr + αs·Mv)               (E3-3)
    
    Mv = Vpr × sh  (shear amplification)
    Vpr = 2·Mpr / Lh                       (E3-5)
    Mpr = 1.1·Ry·Fy·Zx                    (probable moment)
    sh ≈ db  (beam depth, unreinforced connection per AISC 358)
    Lh = L_beam - 2·sh
    """
    
    @staticmethod
    def check(columns_at_joint, beams_at_joint, Ry=1.1, alpha_s=1.0):
        """
        columns_at_joint: list of (SectionProperties, Pr_N)
            - Pr_N: axial force from governing load combo (N)
        beams_at_joint: list of (SectionProperties, L_beam_mm)
            - L_beam_mm: beam length (mm)
        Ry: expected yield stress ratio (1.1 for hot-rolled)
        alpha_s: 1.0 for LRFD
        """
        # ΣMpc* — Column plastic moment capacity (reduced by axial)
        sum_Mpc = 0.0
        for col_sec, Pr in columns_at_joint:
            Zc = col_sec.Zx
            Fyc = col_sec.Fy
            Ag = col_sec.A
            Mpc = Zc * (Fyc - alpha_s * abs(Pr) / Ag)
            sum_Mpc += max(Mpc, 0.0)
        
        # ΣMbe* — Beam expected flexural strength with shear amplification
        sum_Mbe = 0.0
        for beam_sec, L_beam in beams_at_joint:
            Fy_beam = beam_sec.Fy
            Zx_beam = beam_sec.Zx
            db = beam_sec.d  # beam depth (mm)
            
            # Mpr = 1.1 * Ry * Fy * Zx (probable moment at plastic hinge)
            Mpr = 1.1 * Ry * Fy_beam * Zx_beam
            
            # sh = distance from column face to plastic hinge
            # For unreinforced connections (AISC 358): sh ≈ db
            sh = db
            
            # Lh = distance between plastic hinge locations
            Lh = L_beam - 2.0 * sh
            if Lh <= 0:
                Lh = L_beam * 0.5  # Fallback: half of beam length
            
            # Vpr = 2*Mpr / Lh (capacity-limited seismic load, E3-5)
            Vpr = 2.0 * Mpr / Lh
            
            # Mv = additional moment from shear amplification
            Mv = Vpr * sh
            
            sum_Mbe += (Mpr + alpha_s * Mv)
        
        # Ratio
        if sum_Mbe > 0:
            ratio = sum_Mpc / sum_Mbe
        else:
            ratio = float('inf')  # No beams → automatically OK
        
        return {
            "ratio": round(ratio, 4),
            "ok": ratio > 1.0,
            "sum_Mpc_Nmm": round(sum_Mpc, 1),
            "sum_Mbe_Nmm": round(sum_Mbe, 1)
        }


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD COMBINER — SNI 1727-2020 LRFD (Hybrid: Default + Custom)
# ═══════════════════════════════════════════════════════════════════════════════

class LoadCombiner:
    """Combine station forces from individual load cases into DSTL combos.

    Supports hybrid mode:
      - "default": 10 DSTL SNI 1726-2019 + SNI 1727-2020
      - "custom":  only user-defined combos
      - "both":    default + custom

    Seismic load effect per SNI 1726-2019 Pasal 7.4.2:
      E  = Eh ± Ev
      Eh = ρ · QE    (horizontal)
      Ev = 0.2·SDS·D (vertical)

    LRFD Seismic load combinations (SNI 1727-2020 Pasal 4.2.2):
      Combo 5: (1.2 + 0.2·SDS)·D + ρ·QE + L
      Combo 7: (0.9 - 0.2·SDS)·D + ρ·QE
    """

    # Active combinations (populated by build())
    COMBINATIONS = {}
    COMBO_LABELS = {}

    @classmethod
    def build(cls, combo_config: dict = None, available_patterns: list = None,
              SDS: float = 0.0, rho: float = 1.0, analysis_method: str = "ELM"):
        """Build active combinations based on config.

        Args:
            combo_config: {"mode": "default"|"custom"|"both",
                           "custom_combinations": {"COMB1": {...}, ...}}
            available_patterns: list of pattern names from analysis results
            SDS: Design spectral acceleration for Ev = 0.2·SDS·D
            rho: Redundancy factor for Eh = ρ·QE
            analysis_method: "DAM" or "ELM" — pattern live load only for DAM
        """
        mode = (combo_config or {}).get("mode", "default")
        custom = (combo_config or {}).get("custom_combinations", {})

        if mode in ("default", "both"):
            # Compute Ev-adjusted dead load factors
            # SNI 1726-2019 Pasal 7.4.2 + SNI 1727-2020 Pasal 4.2.2
            d_add = round(1.2 + 0.2 * SDS, 4)  # (1.2 + 0.2·SDS) — additive Ev
            d_sub = round(0.9 - 0.2 * SDS, 4)  # (0.9 - 0.2·SDS) — subtractive Ev
            rho_r = round(rho, 4)

            # LIVE factor for seismic combinations (DSTL3-6)
            # SAP2000/ASCE 7: LIVE factor = 1.0 in seismic combos.
            # PatLLF = 0.75 is a DESIGN pattern factor (applied separately
            # to pattern live load zone cases), NOT a combo factor.
            live_seis = 1.0

            cls.COMBINATIONS = {
                "DSTL1":  {"SelfWeight": 1.4, "ADL": 1.4},
                "DSTL2":  {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 1.6},
                "DSTL3":  {"SelfWeight": d_add, "ADL": d_add, "LIVE": live_seis, "SeismicX": rho_r},
                "DSTL4":  {"SelfWeight": d_add, "ADL": d_add, "LIVE": live_seis, "SeismicX": -rho_r},
                "DSTL5":  {"SelfWeight": d_add, "ADL": d_add, "LIVE": live_seis, "SeismicY": rho_r},
                "DSTL6":  {"SelfWeight": d_add, "ADL": d_add, "LIVE": live_seis, "SeismicY": -rho_r},
                "DSTL7":  {"SelfWeight": d_sub, "ADL": d_sub, "SeismicX": rho_r},
                "DSTL8":  {"SelfWeight": d_sub, "ADL": d_sub, "SeismicX": -rho_r},
                "DSTL9":  {"SelfWeight": d_sub, "ADL": d_sub, "SeismicY": rho_r},
                "DSTL10": {"SelfWeight": d_sub, "ADL": d_sub, "SeismicY": -rho_r},
            }
            cls.COMBO_LABELS = {
                "DSTL1":  "1.4(D+ADL)",
                "DSTL2":  "1.2(D+ADL) + 1.6L",
                "DSTL3":  f"{d_add}(D+ADL) + {live_seis}L + {rho_r}EQx",
                "DSTL4":  f"{d_add}(D+ADL) + {live_seis}L - {rho_r}EQx",
                "DSTL5":  f"{d_add}(D+ADL) + {live_seis}L + {rho_r}EQy",
                "DSTL6":  f"{d_add}(D+ADL) + {live_seis}L - {rho_r}EQy",
                "DSTL7":  f"{d_sub}(D+ADL) + {rho_r}EQx",
                "DSTL8":  f"{d_sub}(D+ADL) - {rho_r}EQx",
                "DSTL9":  f"{d_sub}(D+ADL) + {rho_r}EQy",
                "DSTL10": f"{d_sub}(D+ADL) - {rho_r}EQy",
            }

            print(f"  Ev = 0.2*SDS*D = 0.2*{SDS}*D")
            print(f"  PatLLF   : {live_seis} (applied to seismic combos DSTL3-6)")
            print(f"  DSTL3-6 : D factor = (1.2+0.2*SDS) = {d_add}")
            print(f"  DSTL7-10: D factor = (0.9-0.2*SDS) = {d_sub}")
            print(f"  Seismic  : rho = {rho_r}")

            # Pattern live load is handled per-element in _pattern_major_moments()
            # via end-moment-removal algorithm (DSTL2+pat), not via zone combos.
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
                        analysis: Dict, combo_name: str,
                        target_dist: float = None,
                        factors: Dict = None) -> DesignForces:
        """
        Combine forces at a specific station for a given combo.

        Uses distance-based matching: each load case may have different
        station counts (e.g. gravity has 6, seismic has 5).  Forces are
        looked up by distance_mm, with linear interpolation when the exact
        distance is not present in a particular load case.

        Args:
            factors: Optional explicit {lc_name: factor} dict. If provided,
                     overrides COMBINATIONS[combo_name]. Used by B1/B2
                     decomposition to extract sway/non-sway components.
        """
        if factors is None:
            factors = LoadCombiner.COMBINATIONS[combo_name]
        result = DesignForces()
        force_keys = ["P", "Fy", "Fz", "T", "My", "Mz"]

        # Resolve target distance from station_idx using first available LC
        if target_dist is None:
            for lc_name in factors:
                if lc_name not in analysis:
                    continue
                lc_data = analysis[lc_name]
                if "elements" not in lc_data or elem_id not in lc_data["elements"]:
                    continue
                stations = lc_data["elements"][elem_id].get("stations", [])
                if station_idx < len(stations):
                    target_dist = stations[station_idx].get(
                        "distance_mm", stations[station_idx].get("station", 0.0))
                    break
            if target_dist is None:
                return result

        for lc_name, factor in factors.items():
            if lc_name not in analysis:
                continue
            lc_data = analysis[lc_name]
            if "elements" not in lc_data or elem_id not in lc_data["elements"]:
                continue

            stations = lc_data["elements"][elem_id].get("stations", [])
            if not stations:
                continue

            # Fast path: same index, matching distance
            if station_idx < len(stations):
                stn = stations[station_idx]
                d = stn.get("distance_mm", stn.get("station", 0.0))
                if abs(d - target_dist) < 1.0:
                    for key in force_keys:
                        if key in stn:
                            setattr(result, key,
                                    getattr(result, key) + factor * stn[key])
                    continue

            # Distance-based lookup with linear interpolation
            dists = [s.get("distance_mm", s.get("station", 0.0))
                     for s in stations]

            # Find bracketing stations
            lo_i, hi_i = 0, len(stations) - 1
            for i in range(len(dists)):
                if dists[i] <= target_dist + 0.1:
                    lo_i = i
                if dists[i] >= target_dist - 0.1:
                    hi_i = i
                    break

            if abs(dists[lo_i] - target_dist) < 1.0:
                # Exact match at lo_i
                stn = stations[lo_i]
                for key in force_keys:
                    if key in stn:
                        setattr(result, key,
                                getattr(result, key) + factor * stn[key])
            elif abs(dists[hi_i] - target_dist) < 1.0:
                # Exact match at hi_i
                stn = stations[hi_i]
                for key in force_keys:
                    if key in stn:
                        setattr(result, key,
                                getattr(result, key) + factor * stn[key])
            elif lo_i != hi_i and dists[hi_i] > dists[lo_i]:
                # Linear interpolation
                t = ((target_dist - dists[lo_i])
                     / (dists[hi_i] - dists[lo_i]))
                for key in force_keys:
                    v_lo = stations[lo_i].get(key, 0.0)
                    v_hi = stations[hi_i].get(key, 0.0)
                    val = v_lo + t * (v_hi - v_lo)
                    setattr(result, key,
                            getattr(result, key) + factor * val)
            else:
                # Nearest station fallback
                best_i = min(range(len(dists)),
                             key=lambda i: abs(dists[i] - target_dist))
                stn = stations[best_i]
                for key in force_keys:
                    if key in stn:
                        setattr(result, key,
                                getattr(result, key) + factor * stn[key])

        return result

    @staticmethod
    def get_station_distances(elem_id: str, analysis: Dict) -> List[float]:
        """Get merged station distances for an element across all load cases.

        Different load cases may have different station counts (e.g. gravity
        has an extra mid-span station that seismic doesn't).  Merge all
        unique distances into a sorted list so the combiner can interpolate.
        """
        all_dists = set()
        for lc_name in analysis.keys():
            if lc_name.startswith('_'):
                continue
            lc_data = analysis[lc_name]
            if isinstance(lc_data, dict) and "elements" in lc_data and elem_id in lc_data["elements"]:
                stations = lc_data["elements"][elem_id].get("stations", [])
                for s in stations:
                    all_dists.add(round(s.get("distance_mm",
                                              s.get("station", 0.0)), 1))
        return sorted(all_dists) if all_dists else [0.0]

    @staticmethod
    def get_num_stations(elem_id: str, analysis: Dict) -> int:
        """Get number of merged stations for an element."""
        return len(LoadCombiner.get_station_distances(elem_id, analysis))


# ═══════════════════════════════════════════════════════════════════════════════
# STEEL DESIGN ENGINE — Main Runner
# ═══════════════════════════════════════════════════════════════════════════════

class SteelDesignEngine:
    """Main engine: reads Result.json, designs all elements, writes output."""

    # DCR Limit: per SAP2000 reference, status NG jika ratio > 0.95
    DCR_LIMIT = 0.95

    # Stiffness Reduction Factor τb = 1.0 (fixed)
    # Konsisten dengan SAP2000 'Tau-b Fixed'. Nilai ini sudah implisit
    # dalam Effective Length Method yang digunakan.
    TAU_B = 1.0

    # Pattern Live Load factor (PatLLF) — SAP2000 equivalent.
    # For beam design, LIVE end moments are redistributed to approximate
    # skip-loading: M_pattern = M_dead + PatLLF × f_L × M_live_simple_span
    PAT_LLF = 0.75

    # Valid analysis methods
    VALID_METHODS = ["ELM", "DAM"]

    def __init__(self, result_path: str, output_path: str,
                 analysis_method: str = None):
        self.result_path = result_path
        self.output_path = output_path
        self.data = None
        self.results: List[ElementDesignResult] = []
        # Analysis method: "ELM" (Effective Length) or "DAM" (Direct Analysis)
        # None = auto-detect from Result.json during run()
        if analysis_method is not None:
            method = analysis_method.upper()
            if method not in self.VALID_METHODS:
                print(f"  WARNING: Unknown method '{analysis_method}', defaulting to ELM")
                method = "ELM"
            self.analysis_method = method
        else:
            self.analysis_method = None  # Will be read from JSON in run()
        # Seismic parameters (populated from JSON during run())
        self.frame_type = "SRPMB/OMF"
        self.Ry = 1.0
        self.rho = 1.0

    def run(self):
        """Execute the full design workflow."""
        # 1. Load data
        print(f"\n  Membaca: {self.result_path}")
        with open(self.result_path, 'r') as f:
            self.data = json.load(f)

        model_elements = self.data["model_data"]["model_elements"]
        analysis = self.data["analysis_results"]

        # 1b. Read seismic parameters from JSON
        seismic_params = self.data.get("model_data", {}).get("seismic_parameters", {})
        # Inject grid_system for per-floor K2/B2 computation
        seismic_params["_grid_system"] = self.data.get("model_data", {}).get("grid_system", {})
        self.frame_type = seismic_params.get("frame_type", "SRPMB/OMF")
        self.Ry = seismic_params.get("Ry", 1.0)
        self.rho = seismic_params.get("rho", 1.0)

        # 1b2. Read group names — Balok Anak is gravity framing, exempt from SMF
        gn = self.data.get("model_data", {}).get("group_names", {})
        self._grp_balok_anak = gn.get("balok_anak", "Balok Anak")

        # 1c. Resolve analysis method: CLI override > JSON > default "ELM"
        if self.analysis_method is None:
            json_method = seismic_params.get("analysis_method", "").upper()
            if json_method in self.VALID_METHODS:
                self.analysis_method = json_method
            else:
                self.analysis_method = "ELM"

        method_desc = ("Effective Length Method (ELM)" if self.analysis_method == "ELM"
                       else "Direct Analysis Method (DAM)")
        print("=" * 70)
        print("  AISC 360-22 - STEEL DESIGN ENGINE (LRFD, SI Units)")
        print(f"  Analysis Method: {method_desc}")
        print("  Kombinasi Beban: SNI 1727-2020")
        print("=" * 70)

        # 2. Build load combinations (hybrid: default + custom)
        # Pass SDS and rho for Ev-adjusted seismic load combinations
        self.SDS = seismic_params.get("SDS", 0.0)
        combo_config = self.data.get("model_data", {}).get("load_combination_config", None)
        available = [k for k in analysis.keys() if not k.startswith('_')]
        LoadCombiner.build(combo_config, available, SDS=self.SDS, rho=self.rho,
                           analysis_method=self.analysis_method)

        print(f"  Analysis     : {self.analysis_method}")
        print(f"  Frame type   : {self.frame_type}")
        print(f"  Ry           : {self.Ry}")
        print(f"  DCR Limit    : {self.DCR_LIMIT}")
        print(f"  Jumlah elemen: {len(model_elements)}")
        print(f"  Load patterns: {available}")
        print(f"  Kombinasi desain: {len(LoadCombiner.COMBINATIONS)}")

        # 2b. Build topology map for K factor calculation
        # Read support type from model data
        support_config = self.data.get("model_data", {}).get("support_config", {})
        support_type = support_config.get("type", "Fixed")
        self.support_type = support_type
        self.node_map = KFactorCalculator.build_topology_map(model_elements, support_type)
        print(f"  Node map     : {len(self.node_map)} nodes")

        # 2c. Compute K2 via alignment chart (ELM) — primary method
        #     Story stiffness kept as diagnostic cross-check only.
        if self.analysis_method == "ELM":
            # Print alignment chart K2 for first column as diagnostic
            for elem in model_elements:
                if elem.get("type") == "Column":
                    _Kx, _Ky, _kd = KFactorCalculator.compute_K_for_element(
                        elem, self.node_map)
                    print(f"  Alignment K2 : Kx={_Kx:.4f}, Ky={_Ky:.4f} "
                          f"(G_top_maj={_kd.get('GA_major',0):.3f}, "
                          f"G_bot_maj={_kd.get('GB_major',0):.3f})")
                    break
            self.story_K2 = KFactorCalculator.compute_K2_story_stiffness(
                model_elements, analysis, seismic_params)
        else:
            self.story_K2 = None

        # 2d. Build lateral bracing map for Cb/Lb calculation
        self.bracing_map = self._build_bracing_map(model_elements)
        braced_count = sum(1 for v in self.bracing_map.values() if v)
        print(f"  Bracing pts  : {braced_count} beams with intermediate bracing")

        # 2e. Build continuous-span map for pattern live load applicability
        self.continuous_span_map = self._build_continuous_span_map(model_elements)
        multi_span = sum(1 for v in self.continuous_span_map.values() if v)
        print(f"  Multi-span   : {multi_span} beams with adjacent spans (pattern eligible)")

        # 2f. Build slab bracing map: interior beams with floor panels on both
        #     sides get continuous lateral bracing from the slab (top flange).
        self.slab_bracing_map = self._build_slab_bracing_map(model_elements)
        slab_braced = sum(1 for v in self.slab_bracing_map.values() if v)
        print(f"  Slab bracing : {slab_braced} beams with slab on both sides (interior)")

        # 2g. Pre-compute ΣPr per combo per floor for B2 (AISC C2-3)
        #     ΣPr = total factored axial on all columns in each story
        # Build z_levels for floor assignment
        grid = self.data.get("model_data", {}).get("grid_system", {})
        z_lvl = sorted(grid.get("z_levels_mm", [])) if grid else []
        if len(z_lvl) < 2:
            h = seismic_params.get("HEIGHT_MM", 4000.0)
            n_st = seismic_params.get("N_STORY", 1)
            z_lvl = [i * h for i in range(n_st + 1)]
        self._z_levels = z_lvl

        self.story_B2_data = {}  # legacy single-story (backward compat)
        self.stories_B2_data = []  # per-floor [{sum_Pe2_x, sum_Pe2_y, ...}]
        self.sum_Pr_per_combo = {}  # legacy single total
        self.sum_Pr_per_floor_combo = {}  # {floor_idx: {combo: sum_pr}}

        has_stories = self.story_K2 and "_stories" in self.story_K2
        if has_stories:
            self.stories_B2_data = self.story_K2["_stories"]
        if self.story_K2 and "_story" in self.story_K2:
            self.story_B2_data = self.story_K2["_story"]

        if has_stories or self.story_B2_data:
            columns = [e for e in model_elements if e.get("type") == "Column"]
            # Assign floor to each column
            col_floors = {}
            for col in columns:
                eid = str(col["id"])
                col_floors[eid] = KFactorCalculator._col_floor_index(col, z_lvl)

            for combo_name in LoadCombiner.COMBINATIONS:
                total_pr = 0.0
                for col in columns:
                    eid = str(col["id"])
                    f = LoadCombiner.combine_station(0, eid, analysis, combo_name)
                    pr = abs(f.P)
                    total_pr += pr
                    fi = col_floors[eid]
                    if fi not in self.sum_Pr_per_floor_combo:
                        self.sum_Pr_per_floor_combo[fi] = {}
                    self.sum_Pr_per_floor_combo[fi][combo_name] = (
                        self.sum_Pr_per_floor_combo[fi].get(combo_name, 0.0) + pr)
                self.sum_Pr_per_combo[combo_name] = total_pr

            # Diagnostic: print B2 for first seismic combo (floor 0)
            fd0 = self.stories_B2_data[0] if self.stories_B2_data and self.stories_B2_data[0] else self.story_B2_data
            pr0 = self.sum_Pr_per_floor_combo.get(0, self.sum_Pr_per_combo)
            for _cn in ("DSTL3", "DSTL4"):
                if _cn in pr0:
                    _sPr = pr0[_cn]
                    _sPe = fd0.get("sum_Pe2_x", 1e20) if fd0 else 1e20
                    _B2 = MomentAmplifier.compute_B2(_sPr, _sPe)
                    print(f"  B2 ({_cn:>6}) : {_B2:.4f} "
                          f"(SPr={_sPr/1000:.1f} kN, "
                          f"SPe2_x={_sPe/1000:.1f} kN)")
                    break
            for _cn in ("DSTL5", "DSTL6"):
                if _cn in pr0:
                    _sPr = pr0[_cn]
                    _sPe = fd0.get("sum_Pe2_y", 1e20) if fd0 else 1e20
                    _B2 = MomentAmplifier.compute_B2(_sPr, _sPe)
                    print(f"  B2 ({_cn:>6}) : {_B2:.4f} "
                          f"(SPr={_sPr/1000:.1f} kN, "
                          f"SPe2_y={_sPe/1000:.1f} kN)")
                    break

        # 3. Design each element
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

        # 3b. SCWB post-processing (AISC 341-22 E3.4a)
        if self.frame_type in ["SRPMK/SMF", "SMF"]:
            scwb_updates = self._scwb_post_process(model_elements, analysis)
            if scwb_updates:
                print(f"\n  SCWB (AISC 341-22 E3.4a) — {scwb_updates} columns updated")
                # Reprint updated columns
                for r in self.results:
                    if r.ratio_type.startswith("SCWB"):
                        status_mark = "OK" if r.status == "OK" else "NG"
                        print(f"  {r.frame_label:<8} {r.design_type:<8} "
                              f"{r.design_section:<22} {r.governing_ratio:>8.4f} "
                              f"{r.governing_combo:<8} {status_mark} {r.status}")

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

    @staticmethod
    def _build_bracing_map(model_elements: List[Dict], tol: float = 5.0) -> Dict:
        """Build lateral bracing map: for each beam, find intermediate bracing
        points where other beams frame in perpendicular (or at an angle).

        A bracing point exists when another beam's start or end node lies
        along this beam's span at an interior point (not at the beam's own
        start/end nodes).

        Returns: { elem_id_str: [dist_mm_1, dist_mm_2, ...] }
            sorted distances from start_node, excluding 0 and L.
        """
        bracing = {}
        beams = [e for e in model_elements if e.get("type") == "Beam"]

        # Collect all beam endpoint coordinates
        beam_endpoints = []
        for b in beams:
            topo = b.get("topology", {})
            sn = topo.get("start_node", [0, 0, 0])
            en = topo.get("end_node", [0, 0, 0])
            beam_endpoints.append((str(b["id"]),
                                   [float(c) for c in sn],
                                   [float(c) for c in en]))

        for b in beams:
            bid = str(b["id"])
            topo = b.get("topology", {})
            sn = [float(c) for c in topo.get("start_node", [0, 0, 0])]
            en = [float(c) for c in topo.get("end_node", [0, 0, 0])]
            L = topo.get("length_mm", 0)
            if L <= 0:
                bracing[bid] = []
                continue

            # Direction vector (unit)
            dx, dy, dz = en[0] - sn[0], en[1] - sn[1], en[2] - sn[2]
            brace_dists = []

            for other_id, other_sn, other_en in beam_endpoints:
                if other_id == bid:
                    continue
                # Check both endpoints of the other beam
                for pt in [other_sn, other_en]:
                    # Must be at same elevation (within tol)
                    if abs(pt[2] - sn[2]) > tol:
                        continue
                    # Project pt onto this beam's axis
                    # Vector from sn to pt
                    vx = pt[0] - sn[0]
                    vy = pt[1] - sn[1]
                    vz = pt[2] - sn[2]
                    # Parameter t = (v . d) / (d . d)
                    dd = dx * dx + dy * dy + dz * dz
                    if dd < 1e-6:
                        continue
                    t = (vx * dx + vy * dy + vz * dz) / dd
                    dist = t * L
                    # Check if the point lies on the beam (interior only)
                    if dist <= tol or dist >= L - tol:
                        continue  # Skip start/end
                    # Check perpendicular distance (must be close to beam axis)
                    proj = [sn[0] + t * dx, sn[1] + t * dy, sn[2] + t * dz]
                    perp = math.sqrt((pt[0] - proj[0]) ** 2 +
                                     (pt[1] - proj[1]) ** 2 +
                                     (pt[2] - proj[2]) ** 2)
                    if perp <= tol:
                        brace_dists.append(round(dist, 1))

            # Deduplicate and sort
            brace_dists = sorted(set(brace_dists))
            bracing[bid] = brace_dists

        return bracing

    @staticmethod
    def _build_continuous_span_map(model_elements: List[Dict]) -> Dict:
        """Detect beams with adjacent continuous spans in the same direction.

        Pattern live load (skip-loading) only affects beams that have at least
        one adjacent span sharing a node and running collinearly.  Single-span
        beams (e.g. in a 1×1 bay frame) have no adjacent spans to skip-load,
        so pattern loading has no effect — consistent with SAP2000 behaviour.

        Returns: { elem_id_str: bool }  — True if ≥1 adjacent span exists.
        """
        beams = [e for e in model_elements if e.get("type") == "Beam"]
        result: Dict[str, bool] = {}

        for b in beams:
            bid = str(b["id"])
            topo = b.get("topology", {})
            n_start = str(topo.get("start_node_id", ""))
            n_end = str(topo.get("end_node_id", ""))
            sn = [float(c) for c in topo.get("start_node", [0, 0, 0])]
            en = [float(c) for c in topo.get("end_node", [0, 0, 0])]
            dx, dy, dz = en[0] - sn[0], en[1] - sn[1], en[2] - sn[2]
            length = math.sqrt(dx * dx + dy * dy + dz * dz)
            if length < 1e-6:
                result[bid] = False
                continue
            d_vec = (dx / length, dy / length, dz / length)

            has_adjacent = False
            for other in beams:
                oid = str(other["id"])
                if oid == bid:
                    continue
                ot = other.get("topology", {})
                on_s = str(ot.get("start_node_id", ""))
                on_e = str(ot.get("end_node_id", ""))

                # Must share a node
                if not (on_s == n_start or on_s == n_end or
                        on_e == n_start or on_e == n_end):
                    continue

                # Must be collinear (|dot product| > 0.95)
                os = [float(c) for c in ot.get("start_node", [0, 0, 0])]
                oe = [float(c) for c in ot.get("end_node", [0, 0, 0])]
                odx, ody, odz = oe[0] - os[0], oe[1] - os[1], oe[2] - os[2]
                ol = math.sqrt(odx * odx + ody * ody + odz * odz)
                if ol < 1e-6:
                    continue
                dot = abs(d_vec[0] * odx / ol + d_vec[1] * ody / ol +
                          d_vec[2] * odz / ol)
                if dot > 0.95:
                    has_adjacent = True
                    break

            result[bid] = has_adjacent

        return result

    @staticmethod
    def _build_slab_bracing_map(model_elements: List[Dict],
                                tol: float = 100.0) -> Dict:
        """Detect which beams have floor slab on both sides (interior beams).

        Interior beams have floor panels on both perpendicular sides, so the
        floor slab continuously braces the top (compression) flange under
        positive/sagging moment.  Edge/perimeter beams only have a slab on
        one side — SAP2000 does not consider them continuously braced.

        Algorithm: for each beam, compute the perpendicular direction in plan.
        If other floor beams exist on BOTH perpendicular sides, the beam is
        interior and receives slab bracing.

        Returns: { elem_id_str: bool }
        """
        beams = [e for e in model_elements if e.get("type") == "Beam"]
        result: Dict[str, bool] = {}

        # Pre-compute midpoints for all beams
        beam_info = []
        for b in beams:
            topo = b.get("topology", {})
            sn = [float(c) for c in topo.get("start_node", [0, 0, 0])]
            en = [float(c) for c in topo.get("end_node", [0, 0, 0])]
            mid = ((sn[0] + en[0]) / 2.0, (sn[1] + en[1]) / 2.0, sn[2])
            beam_info.append((str(b["id"]), sn, en, mid))

        for bid, sn, en, mid in beam_info:
            dx, dy = en[0] - sn[0], en[1] - sn[1]
            length = math.sqrt(dx * dx + dy * dy)
            if length < 1e-6:
                result[bid] = False
                continue

            # Perpendicular direction in plan (rotate 90°)
            perp_x = -dy / length
            perp_y = dx / length
            bz = mid[2]

            has_pos = False
            has_neg = False

            for oid, osn, oen, omid in beam_info:
                if oid == bid:
                    continue
                # Must be at the same floor level
                if abs(omid[2] - bz) > tol:
                    continue
                # Perpendicular distance from this beam's midpoint
                vx = omid[0] - mid[0]
                vy = omid[1] - mid[1]
                perp_dist = vx * perp_x + vy * perp_y
                if perp_dist > tol:
                    has_pos = True
                elif perp_dist < -tol:
                    has_neg = True

            result[bid] = has_pos and has_neg

        return result

    def _pattern_major_moments(self, elem_id: str, analysis: Dict,
                               std_major_moments: List[float],
                               station_distances: List[float],
                               elem_length: float,
                               live_factor: float) -> Optional[List[float]]:
        """Compute pattern-modified major moments for a beam (SAP2000 PatLLF).

        SAP2000 algorithm for pattern live load on beams:
        1. Extract LIVE major-axis moments at each station from analysis
        2. Compute simple-span LIVE moments by removing end-moment interpolation
        3. Pattern M3 = Standard M3 + f_L × [PatLLF × M_live_ss - M_live]

        This effectively:
        - Removes LIVE end moments (simulating no LIVE on adjacent spans)
        - Retains PatLLF fraction of the simple-span LIVE moment on this span
        - Keeps P, V, T, M_minor unchanged

        Returns list of pattern major moments, or None if not applicable.
        """
        live_data = analysis.get("LIVE", {})
        if not isinstance(live_data, dict):
            return None
        elems = live_data.get("elements", {})
        if elem_id not in elems:
            return None

        live_stations = elems[elem_id].get("stations", [])
        n = len(std_major_moments)
        if len(live_stations) < 2 or n < 2:
            return None

        # Build distance-based LIVE moment lookup (handles station count mismatch
        # when LIVE has duplicate stations, e.g. at secondary beam connections)
        live_dists = [s.get("distance_mm", s.get("station", 0.0))
                      for s in live_stations]
        live_raw = [s.get("My", 0.0) for s in live_stations]

        def _lookup_live(target_d):
            """Find LIVE My at target distance by closest-distance match."""
            best_i = 0
            best_gap = abs(live_dists[0] - target_d)
            for li in range(1, len(live_dists)):
                gap = abs(live_dists[li] - target_d)
                if gap < best_gap:
                    best_gap = gap
                    best_i = li
            return live_raw[best_i]

        # Map LIVE moments to merged station distances
        live_maj = [_lookup_live(station_distances[si])
                    for si in range(n)]

        M_end_A = live_maj[0]
        M_end_B = live_maj[n - 1]

        # If LIVE end moments are negligible, pattern has no effect
        if abs(M_end_A) < 1.0 and abs(M_end_B) < 1.0:
            return None

        L = elem_length
        PatLLF = self.PAT_LLF
        pattern = []
        for si in range(n):
            x = station_distances[si] if si < len(station_distances) else 0.0
            frac = x / L if L > 1e-6 else 0.0
            M_end_interp = M_end_A * (1.0 - frac) + M_end_B * frac
            M_live_ss = live_maj[si] - M_end_interp
            delta = live_factor * (PatLLF * M_live_ss - live_maj[si])
            pattern.append(std_major_moments[si] + delta)

        return pattern

    def _design_element(self, elem: Dict, analysis: Dict) -> ElementDesignResult:
        """Design a single element across all DSTL combos and stations."""

        # --- Extract properties ---
        sec = self._extract_section(elem)
        dp = elem.get("design_parameters", {})

        # --- K Factor ---
        if self.analysis_method == "DAM":
            # Direct Analysis Method: K = 1.0 for all members (AISC C2.1)
            Kx_calc, Ky_calc = 1.0, 1.0
            k_debug = {"method": "DAM", "Kx": 1.0, "Ky": 1.0}
        else:
            # ELM: alignment chart / nomogram (AISC C-A-7-2)
            Kx_calc, Ky_calc, k_debug = KFactorCalculator.compute_K_for_element(
                elem, self.node_map)
        # Allow manual override from design_parameters if present
        Kx = dp.get("Kx", Kx_calc)
        Ky = dp.get("Ky", Ky_calc)
        Kz = dp.get("Kz", 1.0)        # E4: Torsional K factor (default 1.0)
        Lx = dp.get("Lx_mm", 4000.0)
        Ly = dp.get("Ly_mm", 4000.0)
        Lb = dp.get("Lb_mm", Ly)
        Lcz = dp.get("Lcz_mm", Ly)    # E4: Torsional unbraced length (default=Ly)

        elem_id = str(elem["id"])
        elem_type = elem.get("type", "Column")
        frame_label = elem.get("frame_label", "?")
        label_name = elem.get("label_name", "-")
        section_name = sec.name

        # --- Classify section ---
        classifier = SectionClassification(sec)
        axial_class = classifier.classify_axial()
        flex_class = classifier.classify_flexure()

        # --- Lateral bracing points ---
        # SAP2000 concept: where another beam frames in perpendicular along
        # this beam's span, that point provides lateral bracing for LTB
        # AND weak-axis compression buckling.
        elem_length = elem.get("topology", {}).get("length_mm", Lb)
        brace_pts = self.bracing_map.get(elem_id, [])

        # If bracing points exist, reduce Ly (weak-axis) and Lcz to the max
        # unbraced segment length. Lx (strong-axis) stays at full span since
        # in-plane buckling is restrained only by column-to-column distance.
        if brace_pts and elem_type == "Beam":
            all_bounds = [0.0] + list(brace_pts) + [elem_length]
            max_seg = max(all_bounds[i+1] - all_bounds[i]
                         for i in range(len(all_bounds) - 1))
            Ly = max_seg
            Lb = max_seg
            Lcz = max_seg

        # --- Compute capacities (once per element) ---
        PcTension = TensionCapacity.design(sec)
        PcComp = CompressionCapacity.design(sec, Kx, Ky, Lx, Ly, axial_class,
                                            Kz=Kz, Lcz=Lcz)

        McMinor = FlexureCapacity.design_minor(sec, flex_class)
        PhiVnMajor = ShearCapacity.design_major(sec)
        PhiVnMinor = ShearCapacity.design_minor(sec)

        # Segment boundaries: [0, bp1, bp2, ..., L]
        seg_bounds = [0.0] + list(brace_pts) + [elem_length]

        # Default capacity with Cb=1.0 (will be overridden per combo/segment)
        Cb_default = 1.0
        McMajor_default = FlexureCapacity.design_major(sec, Lb, Cb_default, flex_class)

        # Slab bracing: floor slab continuously braces the top flange of
        # floor beams. When top flange is in compression (positive/sagging
        # moment), LTB is prevented -> Mn = Mp. For negative/hogging regions,
        # use the length of the negative moment zone as effective Lb.
        # Disabled: geometry-based detection (beams on both sides) does not
        # match SAP2000 lateral bracing behaviour.  SAP2000 does NOT apply
        # continuous slab bracing to beams even when shell elements are
        # connected along the span.  The Cb × LTB formula already captures
        # the correct capacity; enabling slab bracing would be unconservative.
        McMajor_Mp = FlexureCapacity.design_major(sec, 0.0, 1.0, flex_class)
        is_floor_beam = (elem_type == "Beam")
        slab_bracing = False

        cap = DesignCapacity(
            PcComp=PcComp,
            PcTension=PcTension,
            McMajor=McMajor_default,
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
        best_pmm_Cb = 1.0
        best_pmm_Lb = Lb
        best_pmm_McMajor = McMajor_default

        best_shear = ShearResult()
        best_shear_combo = ""
        best_shear_location = 0.0
        all_station_details = []  # Per-combo per-station (SAP2000 style)

        # Helper: find which unbraced segment a station belongs to
        def _seg_index(dist):
            for s in range(len(seg_bounds) - 1):
                if dist <= seg_bounds[s + 1] + 0.1:
                    return s
            return len(seg_bounds) - 2

        for combo_name in LoadCombiner.COMBINATIONS:
            # Identify sway (lateral) load case in this combo for B1/B2
            combo_factors = LoadCombiner.COMBINATIONS[combo_name]
            sway_lc = None
            sway_factor = 0.0
            for _lc in ("SeismicX", "SeismicY"):
                if _lc in combo_factors and combo_factors[_lc] != 0:
                    sway_lc = _lc
                    sway_factor = combo_factors[_lc]
                    break

            # 1) Collect forces at all stations for this combo (single pass)
            combo_forces = []
            sway_forces = []   # Sway component only (for B1/B2 decomposition)
            major_moments = []
            for si in range(n_stations):
                td = station_distances[si] if si < len(station_distances) else None
                f = LoadCombiner.combine_station(si, elem_id, analysis, combo_name,
                                                 target_dist=td)
                # Extract sway component (factor × seismic LC)
                f_lt = DesignForces()
                if sway_lc:
                    f_lt = LoadCombiner.combine_station(
                        si, elem_id, analysis, combo_name, target_dist=td,
                        factors={sway_lc: sway_factor})

                # For beams: My = strong-axis (major), Mz = weak-axis (minor)
                # Swap so Mz always = major, My always = minor
                if elem_type == "Beam":
                    f.My, f.Mz = f.Mz, f.My
                    f_lt.My, f_lt.Mz = f_lt.Mz, f_lt.My
                combo_forces.append(f)
                sway_forces.append(f_lt)
                major_moments.append(f.Mz)

            # 2) Compute Cb and McMajor per unbraced segment
            n_segs = len(seg_bounds) - 1
            seg_Cb = []
            seg_McMajor = []
            seg_Lb = []

            for s in range(n_segs):
                lo, hi = seg_bounds[s], seg_bounds[s + 1]
                seg_len = hi - lo
                seg_Lb.append(seg_len)

                # Gather moments within this segment
                seg_moments = []
                for si in range(n_stations):
                    d = station_distances[si] if si < len(station_distances) else 0.0
                    if lo - 0.1 <= d <= hi + 0.1:
                        seg_moments.append(major_moments[si])

                Cb_s = FlexureCapacity.calc_Cb(seg_moments)
                McMaj_s = FlexureCapacity.design_major(sec, seg_len, Cb_s, flex_class)
                seg_Cb.append(Cb_s)
                seg_McMajor.append(McMaj_s)

            # 2b) Slab bracing: per-station McMajor for floor beams
            #     Positive moment -> top flange compressed -> slab braces -> Mn=Mp
            #     Negative moment -> bottom flange compressed -> use neg zone Lb
            station_McMaj = [None] * n_stations
            if is_floor_beam and slab_bracing:
                dists = [station_distances[i] if i < len(station_distances)
                         else 0.0 for i in range(n_stations)]
                for si in range(n_stations):
                    if major_moments[si] >= 0:
                        station_McMaj[si] = McMajor_Mp
                    else:
                        # Find negative zone boundaries by interpolation
                        neg_lo = 0.0
                        for j in range(si - 1, -1, -1):
                            if major_moments[j] >= 0:
                                m0, m1 = major_moments[j], major_moments[j + 1]
                                dm = abs(m0) + abs(m1)
                                frac = abs(m0) / dm if dm > 1e-6 else 0.5
                                neg_lo = dists[j] + frac * (dists[j + 1] - dists[j])
                                break
                        neg_hi = elem_length
                        for j in range(si + 1, n_stations):
                            if major_moments[j] >= 0:
                                m0, m1 = major_moments[j - 1], major_moments[j]
                                dm = abs(m0) + abs(m1)
                                frac = abs(m0) / dm if dm > 1e-6 else 0.5
                                neg_hi = dists[j - 1] + frac * (dists[j] - dists[j - 1])
                                break
                        neg_Lb = max(neg_hi - neg_lo, 1.0)
                        neg_moms = [major_moments[k] for k in range(n_stations)
                                    if neg_lo - 0.1 <= dists[k] <= neg_hi + 0.1]
                        neg_Cb = FlexureCapacity.calc_Cb(neg_moms) if neg_moms else 1.0
                        station_McMaj[si] = FlexureCapacity.design_major(
                            sec, neg_Lb, neg_Cb, flex_class)

            # 2c) Compute B1/B2 moment amplification (AISC 360-22 C2)
            #     B1: non-sway amplifier (P-δ), per-element
            #     B2: sway amplifier (P-Δ), per-story
            Pr_for_B1 = max(abs(combo_forces[si].P) for si in range(n_stations))

            Pe1_major = MomentAmplifier.compute_Pe1(sec.E, sec.Ix, Lx, K1=1.0)
            Pe1_minor = MomentAmplifier.compute_Pe1(sec.E, sec.Iy, Ly, K1=1.0)

            # Cm from non-sway end moments (columns only; beams have
            # transverse loading so Cm = 1.0 per AISC C2-2)
            if elem_type == "Column" and n_stations >= 2:
                Mnt_end_0 = combo_forces[0].Mz - sway_forces[0].Mz
                Mnt_end_n = combo_forces[-1].Mz - sway_forces[-1].Mz
                Cm_major = MomentAmplifier.compute_Cm(Mnt_end_0, Mnt_end_n)

                Mnt_end_0_min = combo_forces[0].My - sway_forces[0].My
                Mnt_end_n_min = combo_forces[-1].My - sway_forces[-1].My
                Cm_minor = MomentAmplifier.compute_Cm(Mnt_end_0_min,
                                                       Mnt_end_n_min)
            else:
                Cm_major = 1.0
                Cm_minor = 1.0

            B1_major = MomentAmplifier.compute_B1(Pr_for_B1, Pe1_major,
                                                   Cm_major)
            B1_minor = MomentAmplifier.compute_B1(Pr_for_B1, Pe1_minor,
                                                   Cm_minor)

            # B2: from pre-computed per-floor story data
            B2_val = 1.0
            if sway_lc and (self.stories_B2_data or self.story_B2_data):
                # Determine column's floor for per-floor B2
                col_fi = KFactorCalculator._col_floor_index(elem, self._z_levels)
                # Per-floor sum_Pr
                fl_pr = self.sum_Pr_per_floor_combo.get(col_fi, {})
                sum_Pr = fl_pr.get(combo_name, self.sum_Pr_per_combo.get(combo_name, 0.0))
                # Per-floor sum_Pe2
                fd = None
                if self.stories_B2_data and col_fi < len(self.stories_B2_data):
                    fd = self.stories_B2_data[col_fi]
                if fd is None:
                    fd = self.story_B2_data
                pe_key = "sum_Pe2_x" if sway_lc == "SeismicX" else "sum_Pe2_y"
                sum_Pe = fd.get(pe_key, 1e20) if fd else 1e20
                B2_val = MomentAmplifier.compute_B2(sum_Pr, sum_Pe)

            # 3) Check each station with amplified forces
            for si in range(n_stations):
                forces = combo_forces[si]
                f_lt = sway_forces[si]
                dist = station_distances[si] if si < len(station_distances) else 0.0
                s_idx = _seg_index(dist)

                # AISC C2-1: Amplify forces for second-order effects
                #   Pr = Pnt + B2·Plt
                #   Mr = B1·Mnt + B2·Mlt
                Pnt = forces.P - f_lt.P
                Mnt_z = forces.Mz - f_lt.Mz
                Mnt_y = forces.My - f_lt.My

                forces_amp = DesignForces(
                    P=Pnt + B2_val * f_lt.P,
                    Mz=B1_major * Mnt_z + B2_val * f_lt.Mz,
                    My=B1_minor * Mnt_y + B2_val * f_lt.My,
                    Fy=forces.Fy,
                    Fz=forces.Fz,
                    T=forces.T
                )

                cap_seg = DesignCapacity(
                    PcComp=PcComp,
                    PcTension=PcTension,
                    McMajor=station_McMaj[si] if station_McMaj[si] is not None else seg_McMajor[s_idx],
                    McMinor=McMinor,
                    PhiVnMajor=PhiVnMajor,
                    PhiVnMinor=PhiVnMinor,
                    section_class_axial=axial_class["overall"],
                    section_class_flexure_flange=flex_class["flange"],
                    section_class_flexure_web=flex_class["web"],
                    Cb=seg_Cb[s_idx]
                )

                # PMM check with amplified forces
                pmm = PMMCheck.check(forces_amp, cap_seg)
                pmm.B1_major = B1_major
                pmm.B1_minor = B1_minor
                pmm.B2 = B2_val
                pmm.Cm_major = Cm_major
                pmm.Cm_minor = Cm_minor

                if pmm.TotalRatio > best_pmm.TotalRatio:
                    best_pmm = pmm
                    best_pmm_combo = combo_name
                    best_pmm_location = dist
                    if station_McMaj[si] is not None:
                        best_pmm_McMajor = station_McMaj[si]
                        # Slab-braced positive moment: Lb=0 (full bracing)
                        if abs(station_McMaj[si] - McMajor_Mp) < 1.0:
                            best_pmm_Lb = 0.0
                            best_pmm_Cb = 1.0
                        else:
                            best_pmm_Lb = seg_Lb[s_idx]
                            best_pmm_Cb = seg_Cb[s_idx]
                    else:
                        best_pmm_McMajor = seg_McMajor[s_idx]
                        best_pmm_Cb = seg_Cb[s_idx]
                        best_pmm_Lb = seg_Lb[s_idx]

                # Shear check (unamplified — shear not affected by B1/B2)
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
                    "min_shr": round(v3_ratio, 6),
                    "B1_major": round(B1_major, 4),
                    "B1_minor": round(B1_minor, 4),
                    "B2": round(B2_val, 4),
                })

            # === PATTERN LIVE LOAD — beam design variant (SAP2000 PatLLF) ===
            # For each combo containing LIVE, generate a pattern-modified moment
            # diagram and check if it produces a worse DCR than the standard case.
            # Pattern only changes the DEMAND (moment values), not the beam's
            # physical LTB capacity.  SAP2000 uses the standard combo's Cb for
            # the McMajor capacity. Cb is computed from the pattern moment
            # diagram per AISC F1-1 (moment gradient reflects pattern loading).
            _f_L = LoadCombiner.COMBINATIONS[combo_name].get("LIVE", 0)
            if is_floor_beam and _f_L != 0 and self.PAT_LLF < 1.0:
                _pat_moms = self._pattern_major_moments(
                    elem_id, analysis, major_moments,
                    station_distances, elem_length, _f_L)
                if _pat_moms is not None:
                    _pat_combo = combo_name + "+pat"

                    # Compute pattern Cb and McMajor per segment from pattern moments
                    pat_seg_Cb = []
                    pat_seg_McMajor = []
                    for s in range(n_segs):
                        lo, hi = seg_bounds[s], seg_bounds[s + 1]
                        seg_len = hi - lo
                        pat_seg_moments = []
                        for si2 in range(n_stations):
                            d = station_distances[si2] if si2 < len(station_distances) else 0.0
                            if lo - 0.1 <= d <= hi + 0.1:
                                pat_seg_moments.append(_pat_moms[si2])
                        pat_Cb_s = FlexureCapacity.calc_Cb(pat_seg_moments)
                        pat_McMaj_s = FlexureCapacity.design_major(
                            sec, seg_len, pat_Cb_s, flex_class)
                        pat_seg_Cb.append(pat_Cb_s)
                        pat_seg_McMajor.append(pat_McMaj_s)

                    # Pattern slab bracing: per-station McMajor override
                    pat_station_McMaj = [None] * n_stations
                    if is_floor_beam and slab_bracing:
                        _dists = [station_distances[i] if i < len(station_distances)
                                  else 0.0 for i in range(n_stations)]
                        for _si in range(n_stations):
                            if _pat_moms[_si] >= 0:
                                pat_station_McMaj[_si] = McMajor_Mp
                            else:
                                neg_lo = 0.0
                                for j in range(_si - 1, -1, -1):
                                    if _pat_moms[j] >= 0:
                                        m0, m1 = _pat_moms[j], _pat_moms[j + 1]
                                        dm = abs(m0) + abs(m1)
                                        frac = abs(m0) / dm if dm > 1e-6 else 0.5
                                        neg_lo = (_dists[j] + frac *
                                                  (_dists[j + 1] - _dists[j]))
                                        break
                                neg_hi = elem_length
                                for j in range(_si + 1, n_stations):
                                    if _pat_moms[j] >= 0:
                                        m0, m1 = _pat_moms[j - 1], _pat_moms[j]
                                        dm = abs(m0) + abs(m1)
                                        frac = abs(m0) / dm if dm > 1e-6 else 0.5
                                        neg_hi = (_dists[j - 1] + frac *
                                                  (_dists[j] - _dists[j - 1]))
                                        break
                                neg_Lb = max(neg_hi - neg_lo, 1.0)
                                neg_moms = [_pat_moms[k] for k in range(n_stations)
                                            if neg_lo - 0.1 <= _dists[k] <= neg_hi + 0.1]
                                neg_Cb = (FlexureCapacity.calc_Cb(neg_moms)
                                          if neg_moms else 1.0)
                                pat_station_McMaj[_si] = FlexureCapacity.design_major(
                                    sec, neg_Lb, neg_Cb, flex_class)

                    # PMM check with pattern forces and pattern-derived capacity
                    for si in range(n_stations):
                        _dist = station_distances[si] if si < len(station_distances) else 0.0
                        _sidx = _seg_index(_dist)
                        _McMaj = (pat_station_McMaj[si] if pat_station_McMaj[si] is not None
                                  else pat_seg_McMajor[_sidx])
                        _pat_cap = DesignCapacity(
                            PcComp=PcComp, PcTension=PcTension,
                            McMajor=_McMaj, McMinor=McMinor,
                            PhiVnMajor=PhiVnMajor, PhiVnMinor=PhiVnMinor,
                            section_class_axial=axial_class["overall"],
                            section_class_flexure_flange=flex_class["flange"],
                            section_class_flexure_web=flex_class["web"],
                            Cb=pat_seg_Cb[_sidx])
                        _pat_f = DesignForces(
                            P=combo_forces[si].P, Fy=combo_forces[si].Fy,
                            Fz=combo_forces[si].Fz, T=combo_forces[si].T,
                            My=combo_forces[si].My, Mz=_pat_moms[si])
                        _pmm = PMMCheck.check(_pat_f, _pat_cap)
                        if _pmm.TotalRatio > best_pmm.TotalRatio:
                            best_pmm = _pmm
                            best_pmm_combo = _pat_combo
                            best_pmm_location = _dist
                            best_pmm_McMajor = _McMaj
                            if (pat_station_McMaj[si] is not None and
                                    abs(pat_station_McMaj[si] - McMajor_Mp) < 1.0):
                                best_pmm_Lb = 0.0
                                best_pmm_Cb = 1.0
                            else:
                                best_pmm_Cb = pat_seg_Cb[_sidx]
                                best_pmm_Lb = seg_Lb[_sidx]

                        _vr = abs(combo_forces[si].Fz) / PhiVnMajor if PhiVnMajor > 0 else 0
                        _v3r = abs(combo_forces[si].Fy) / PhiVnMinor if PhiVnMinor > 0 else 0
                        all_station_details.append({
                            "combo": _pat_combo,
                            "location_mm": round(_dist, 1),
                            "ratio": round(_pmm.TotalRatio, 6),
                            "axl": round(_pmm.PRatio, 6),
                            "b_maj": round(_pmm.MMajRatio, 6),
                            "b_min": round(_pmm.MMinRatio, 6),
                            "equation": _pmm.equation,
                            "maj_shr": round(_vr, 6),
                            "min_shr": round(_v3r, 6)
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

        # (status determined after SMF checks below)

        # Update cap with governing Cb/Lb/McMajor for reporting
        cap.Cb = best_pmm_Cb
        cap.Lb = best_pmm_Lb
        cap.McMajor = best_pmm_McMajor

        # --- SMF-specific checks (AISC 341-22) ---
        # Balok Anak (Secondary) is gravity framing, NOT part of moment frame
        # → exempt from SMF requirements
        smf_checks = None
        smf_ng_reasons = []  # Collect SMF failure reasons
        elem_group = elem.get("group", "")
        is_smf_member = (elem_group != getattr(self, "_grp_balok_anak", "Balok Anak"))
        if self.frame_type in ["SRPMK/SMF", "SMF"] and is_smf_member:
            smf_checks = {}
            
            # L/r ≤ 60 for columns (AISC 341 E3.4c(b))
            if elem_type == "Column":
                Lr_x = Lx / sec.rx if sec.rx > 0 else 999
                Lr_y = Ly / sec.ry if sec.ry > 0 else 999
                Lr_max = max(Lr_x, Lr_y)
                smf_checks["Lr_x"] = round(Lr_x, 2)
                smf_checks["Lr_y"] = round(Lr_y, 2)
                smf_checks["Lr_max"] = round(Lr_max, 2)
                smf_checks["Lr_ok"] = Lr_max <= 60.0
                if not smf_checks["Lr_ok"]:
                    smf_ng_reasons.append("L/r={:.1f}>60".format(Lr_max))
            
            # Lb/ry stability bracing for highly ductile members
            # AISC 341-22 D1.2b: Lb ≤ 0.086·ry·E/(Ry·Fy)
            # (AISC 341-16 used 0.095; AISC 341-22 updated to 0.086)
            if sec.ry > 0:
                Lb_ry_actual = Lb / sec.ry
                Lb_ry_limit = 0.086 * sec.E / (self.Ry * sec.Fy)
                smf_checks["Lb_ry"] = round(Lb_ry_actual, 2)
                smf_checks["Lb_ry_limit"] = round(Lb_ry_limit, 2)
                smf_checks["Lb_ry_ok"] = Lb_ry_actual <= Lb_ry_limit
                if not smf_checks["Lb_ry_ok"]:
                    smf_ng_reasons.append(
                        "Lb/ry={:.1f}>{:.1f}".format(Lb_ry_actual, Lb_ry_limit))
            
            # Seismic ductility (highly ductile limits)
            # Ca = Pr / (Fy * Ag) — using Pr from governing combo
            Pr_gov = abs(best_pmm.Pr) if best_pmm.Pr != 0 else 0.0
            Ca = Pr_gov / (sec.Fy * sec.A) if (sec.Fy * sec.A) > 0 else 0.0
            seismic_class = classifier.classify_seismic(
                self.frame_type, Ry=self.Ry, Ca=Ca)
            smf_checks["seismic_ductility"] = seismic_class
            if not seismic_class.get("ok", True):
                reasons = []
                if not seismic_class.get("flange_ok", True):
                    reasons.append("flange")
                if not seismic_class.get("web_ok", True):
                    reasons.append("web")
                smf_ng_reasons.append("HD-NG({})".format("+".join(reasons)))
        
        # --- Final status determination ---
        # Combine DCR status with SMF checks
        if governing_ratio > self.DCR_LIMIT and smf_ng_reasons:
            status = "Overstressed; " + "; ".join(smf_ng_reasons)
        elif governing_ratio > self.DCR_LIMIT:
            status = "Overstressed"
        elif smf_ng_reasons:
            status = "SMF-NG: " + "; ".join(smf_ng_reasons)
        else:
            status = "OK"

        return ElementDesignResult(
            element_id=elem["id"],
            frame_label=frame_label,
            label_name=label_name,
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
            station_details=all_station_details,
            smf_checks=smf_checks,
            group=elem.get("group", ""),
            K_factors={"Kx": round(Kx, 4), "Ky": round(Ky, 4), "Kz": round(Kz, 4)},
            lengths={"Lx_mm": round(Lx, 1), "Ly_mm": round(Ly, 1),
                     "Lb_mm": round(Lb, 1), "Lcz_mm": round(Lcz, 1)},
            material_props={"Fy_MPa": sec.Fy, "Fu_MPa": sec.Fu, "E_MPa": sec.E},
        )

    def _scwb_post_process(self, model_elements: List[Dict],
                           analysis: Dict) -> int:
        """
        AISC 341-22 E3.4a — Strong Column-Weak Beam post-processing.

        After all elements are designed, check SCWB at every beam-to-column
        joint.  If the SCWB demand/capacity ratio (1/ratio) exceeds the
        element's current governing_ratio, update it so SCWB governs.

        Returns the number of columns whose governing check was updated.
        """
        result_map = {str(r.element_id): r for r in self.results}

        # Identify seismic combos (those containing SeismicX or SeismicY)
        seis_combos = []
        for cname, cfactors in LoadCombiner.COMBINATIONS.items():
            if any(p in cfactors and cfactors[p] != 0
                   for p in ("SeismicX", "SeismicY")):
                seis_combos.append(cname)
        if not seis_combos:
            return 0

        # Element lookup for quick access
        elem_by_id = {str(e["id"]): e for e in model_elements}

        # Cache extracted sections
        sec_cache = {}
        def _get_sec(eid):
            if eid not in sec_cache:
                sec_cache[eid] = self._extract_section(elem_by_id[eid])
            return sec_cache[eid]

        # Balok Anak group name (gravity framing, exempt from SCWB)
        grp_balok_anak = getattr(self, "_grp_balok_anak", "Balok Anak")

        # AISC 341-22 E3.4a Exception: top-story joints are exempt
        # A top-story joint is one where columns terminate (end) but no
        # column starts going upward.
        top_story_nodes = set()
        col_starts_at = set()  # nodes where a column's start connects
        col_ends_at = set()    # nodes where a column's end connects
        for nid, ninfo in self.node_map.items():
            for c_elem, c_end in ninfo.get("columns", []):
                if c_end == "start":
                    col_starts_at.add(nid)
                else:
                    col_ends_at.add(nid)
        top_story_nodes = col_ends_at - col_starts_at

        updated_count = 0

        for node_id, node_info in self.node_map.items():
            cols = node_info.get("columns", [])
            beams = node_info.get("beams", [])
            if not cols or not beams:
                continue  # SCWB only at beam-to-column joints

            # Exception: skip top-story joints (AISC 341-22 E3.4a)
            if node_id in top_story_nodes:
                continue

            # Filter out Balok Anak from SCWB beams (gravity framing)
            smf_beams = [(e, end) for e, end in beams
                         if e.get("group", "") != grp_balok_anak]
            if not smf_beams:
                continue

            # For each column at this joint, run SCWB in both planes
            for col_elem, col_end in cols:
                col_id = str(col_elem["id"])
                col_sec = _get_sec(col_id)
                local_axes = col_elem.get("local_axes", {})
                y_axis = local_axes.get("y_axis")  # weak-axis direction
                z_axis = local_axes.get("z_axis")  # strong-axis direction

                # Station index at this end of the column
                dists = LoadCombiner.get_station_distances(col_id, analysis)
                if col_end == "start":
                    col_sta = 0
                else:
                    col_sta = len(dists) - 1 if len(dists) > 1 else 0

                for plane_name, plane_dir, col_Z in [
                    ("major", y_axis, col_sec.Zx),
                    ("minor", z_axis, col_sec.Zy),
                ]:
                    # Filter beams in this bending plane
                    beams_in_plane = []
                    for beam_elem, beam_end in smf_beams:
                        if plane_dir is not None:
                            bdir = KFactorCalculator._get_beam_horiz_dir(
                                beam_elem)
                            if bdir is None:
                                continue
                            if not KFactorCalculator._dirs_parallel(
                                    bdir, plane_dir):
                                continue
                        beam_id = str(beam_elem["id"])
                        bsec = _get_sec(beam_id)
                        L_beam = beam_elem.get("topology", {}).get(
                            "length_mm", 6000.0)
                        beams_in_plane.append((bsec, L_beam))

                    if not beams_in_plane:
                        continue  # No moment-frame beams in this plane

                    # ── ΣM*pc: sum column capacities at this joint ────
                    # All columns meeting at this node contribute
                    # (column above + column below)
                    sum_Mpc = 0.0
                    col_breakdown = []
                    for c_elem, c_end in cols:
                        cid = str(c_elem["id"])
                        csec = _get_sec(cid)
                        # Column Z about this bending plane
                        if plane_name == "major":
                            Zc = csec.Zx
                        else:
                            Zc = csec.Zy
                        Ag = csec.A
                        Fyc = csec.Fy

                        # Max compressive |Pr| across seismic combos
                        cdists = LoadCombiner.get_station_distances(
                            cid, analysis)
                        if c_end == "start":
                            csta = 0
                        else:
                            csta = len(cdists) - 1 if len(cdists) > 1 else 0

                        max_Pr = 0.0
                        for combo in seis_combos:
                            td = cdists[csta] if csta < len(cdists) else None
                            f = LoadCombiner.combine_station(
                                csta, cid, analysis, combo, target_dist=td)
                            max_Pr = max(max_Pr, abs(f.P))

                        # E3-2: M*pc = Zc·(Fyc - αs·Pr/Ag)
                        Mpc_star = Zc * (Fyc - 1.0 * max_Pr / Ag)
                        Mpc_used = max(Mpc_star, 0.0)
                        sum_Mpc += Mpc_used
                        col_breakdown.append({
                            "element_id": c_elem["id"],
                            "end": c_end,
                            "Pr_kN": round(max_Pr / 1000, 2),
                            "Zc_mm3": round(Zc, 1),
                            "Ag_mm2": round(Ag, 1),
                            "Fyc_MPa": Fyc,
                            "Mpc_kNm": round(Mpc_used / 1e6, 2),
                        })

                    # ── ΣM*be: sum beam expected strengths ────────────
                    sum_Mbe = 0.0
                    for bsec, L_beam in beams_in_plane:
                        Fy_b = bsec.Fy
                        Zx_b = bsec.Zx
                        db = bsec.d

                        # Mpr = 1.1·Ry·Fy·Zx  (probable moment)
                        Mpr = 1.1 * self.Ry * Fy_b * Zx_b

                        # sh = distance from column face to plastic hinge
                        # Unreinforced connection per AISC 358: sh ≈ db
                        sh = db

                        # Lh = distance between plastic hinge locations
                        Lh = L_beam - 2.0 * sh
                        if Lh <= 0:
                            Lh = L_beam * 0.5

                        # Vpr = 2·Mpr / Lh  (E3-5)
                        Vpr = 2.0 * Mpr / Lh

                        # Mv = Vpr × sh  (shear amplification)
                        Mv = Vpr * sh

                        # E3-3: M*be = Mpr + αs·Mv  (αs=1.0 LRFD)
                        sum_Mbe += (Mpr + 1.0 * Mv)

                    # ── Ratio (E3-1) ──────────────────────────────────
                    if sum_Mbe > 0:
                        scwb_ratio = sum_Mpc / sum_Mbe
                    else:
                        continue  # No beam demand → skip

                    # Convert to DCR style: higher = worse
                    # SAP2000 reports SCWB as 1/ratio (demand/capacity)
                    scwb_dcr = 1.0 / scwb_ratio if scwb_ratio > 0 else 99.0

                    # ── Store on column result ────────────────────────
                    r = result_map.get(col_id)
                    if not r:
                        continue

                    if r.smf_checks is None:
                        r.smf_checks = {}

                    key = "scwb_" + plane_name
                    prev = r.smf_checks.get(key, {})
                    prev_dcr = prev.get("dcr", 0.0)

                    # Keep worst (highest DCR) from all joints
                    if scwb_dcr > prev_dcr:
                        r.smf_checks[key] = {
                            "ratio_E3_1": round(scwb_ratio, 4),
                            "dcr": round(scwb_dcr, 4),
                            "ok": scwb_ratio > 1.0,
                            "sum_Mpc_kNm": round(sum_Mpc / 1e6, 2),
                            "sum_Mbe_kNm": round(sum_Mbe / 1e6, 2),
                            "node_id": node_id,
                            "columns": col_breakdown,
                        }

                    # ── SCWB status update ────────────────────────────
                    # SCWB detail is stored in smf_checks (above).
                    # governing_ratio / governing_combo / ratio_type are
                    # kept as-is (PMM or Shear) so they stay comparable
                    # to SAP2000's DCR convention.  SCWB failure is
                    # communicated via the status field only.
                    worst_scwb = max(
                        r.smf_checks.get("scwb_major", {}).get("dcr", 0),
                        r.smf_checks.get("scwb_minor", {}).get("dcr", 0),
                    )
                    if worst_scwb > 1.0 and "SCWB-NG" not in r.status:
                        old_smf = ""
                        if "SMF-NG: " in r.status:
                            old_smf = r.status.split("SMF-NG: ", 1)[1]
                        elif r.status.startswith("Overstressed; "):
                            old_smf = r.status.split("Overstressed; ", 1)[1]
                        r.status = ("SCWB-NG; " + old_smf
                                    if old_smf else "SCWB-NG")
                        updated_count += 1

        return updated_count

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
        # Use actual z_levels from grid_system (supports non-uniform heights)
        grid = self.data.get("model_data", {}).get("grid_system", {})
        z_lvl = sorted(grid.get("z_levels_mm", [])) if grid else []
        if len(z_lvl) >= 2:
            level_elevations = z_lvl
        else:
            level_elevations = [k * HEIGHT_MM for k in range(N_STORY + 1)]

        # Determine segment boundaries (same logic as script.py)
        total_height = level_elevations[-1] - level_elevations[0] if len(level_elevations) > 1 else N_STORY * HEIGHT_MM
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
                status = "OK" if g["max_fab_dcr"] <= SteelDesignEngine.DCR_LIMIT else "NG"
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
                "analysis_method": self.analysis_method,
                "framing_type": self.frame_type,
                "dcr_limit": self.DCR_LIMIT,
                "Ry": self.Ry,
                "rho": self.rho,
                "tau_b": self.TAU_B,
                "units": {
                    "force": "kN",
                    "moment": "kN-m",
                    "stress": "MPa",
                    "length": "mm"
                },
                "combinations": LoadCombiner.COMBO_LABELS,
                "B2_story_data": {
                    "sum_Pe2_x_kN": round(self.story_B2_data.get("sum_Pe2_x", 0) / 1000, 2) if self.story_B2_data else 0,
                    "sum_Pe2_y_kN": round(self.story_B2_data.get("sum_Pe2_y", 0) / 1000, 2) if self.story_B2_data else 0,
                    "sum_Pr_per_combo_kN": {k: round(v / 1000, 2) for k, v in self.sum_Pr_per_combo.items()} if self.sum_Pr_per_combo else {},
                } if self.story_B2_data else None,
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
                "label_name": r.label_name,
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
                    "Cb": round(r.capacity.Cb, 4),
                    "Lb_governing_mm": round(r.capacity.Lb, 1)
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
                    "TotalRatio": round(r.pmm_detail.TotalRatio, 6) if r.pmm_detail else 0,
                    "B1_major": round(r.pmm_detail.B1_major, 4) if r.pmm_detail else 1.0,
                    "B1_minor": round(r.pmm_detail.B1_minor, 4) if r.pmm_detail else 1.0,
                    "B2": round(r.pmm_detail.B2, 4) if r.pmm_detail else 1.0,
                    "Cm_major": round(r.pmm_detail.Cm_major, 4) if r.pmm_detail else 1.0,
                    "Cm_minor": round(r.pmm_detail.Cm_minor, 4) if r.pmm_detail else 1.0,
                },

                "shear_detail": {
                    "VMajorCombo": r.shear_combo,
                    "VMajorLocation_mm": round(r.shear_location_mm, 1),
                    "VMajorRatio": round(r.shear_detail.VMajorRatio, 6) if r.shear_detail else 0,
                    "VrMajDsgn_kN": round(r.shear_detail.VrMajor / 1000, 2) if r.shear_detail else 0,
                    "PhiVnMajor_kN": round(r.shear_detail.PhiVnMajor / 1000, 2) if r.shear_detail else 0
                },

                "station_details": r.station_details if r.station_details else [],
                "smf_checks": r.smf_checks,

                "group": r.group,
                "K_factors": r.K_factors,
                "lengths": r.lengths,
                "material": r.material_props
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
    analysis_method = None  # Default: auto-detect from Result.json

    # Allow command-line override
    # Usage: python engine.py [result_path] [output_path] [--method=ELM|DAM]
    positional_idx = 0
    for arg in sys.argv[1:]:
        if arg.startswith("--method="):
            analysis_method = arg.split("=", 1)[1]
        elif arg == "--method" or arg == "-m":
            # Next arg is the method (handled below)
            continue
        elif positional_idx == 0:
            result_path = arg
            positional_idx += 1
        elif positional_idx == 1:
            output_path = arg
            positional_idx += 1

    # Handle "--method DAM" (two-arg form)
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in ("--method", "-m") and i < len(sys.argv) - 1:
            analysis_method = sys.argv[i + 1]

    if not os.path.exists(result_path):
        print(f"ERROR: Result.json tidak ditemukan: {result_path}")
        sys.exit(1)

    engine = SteelDesignEngine(result_path, output_path,
                               analysis_method=analysis_method)
    engine.run()


if __name__ == "__main__":
    main()
