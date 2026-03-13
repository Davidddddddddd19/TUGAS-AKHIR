"""
Auto-Select Engine — ROIDA
===========================
Standalone Python 3.x engine.
Reads Design Result.json, finds NG elements (governing_ratio > dcr_limit),
iterates IWF_DATABASE from small to large section (by weight),
finds first section where simplified DCR ≤ 0.95 (compact assumption),
writes Auto-Select Result.json and appends to autoselect_history.json.

AISC checks used (simplified, compact assumption):
  - Compression : AISC E7   (φcPn)
  - Tension     : AISC D2   (φtPn)
  - Flexure     : AISC F2   (φbMn, major + LTB; F6 minor)
  - Shear       : AISC G2.1 (φvVn)
  - Interaction : H1-1a/b   (PMM for columns, Flex+V for beams)

Usage:
    python "Autoselect Engine.py" <design_result_path> <autoselect_result_path>
"""

import json
import math
import sys
import os
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
# IWF DATABASE
# Format identik dengan SECTIONS di Create.pushbutton/script.py:
# {name: {d, bf, tw, tf, r}} — semua dimensi dalam mm
# Diurutkan dari kecil ke besar (berat/meter).
# ═══════════════════════════════════════════════════════════════════
IWF_DATABASE = {
    "IWF148x100x6x9":      {"d": 148,   "bf": 100, "tw": 6,   "tf": 9,   "r": 11},
    "IWF200x100x5.5x8":    {"d": 200,   "bf": 100, "tw": 5.5, "tf": 8,   "r": 11},
    "IWF250x125x6x9":      {"d": 250,   "bf": 125, "tw": 6,   "tf": 9,   "r": 12},
    "IWF300x150x6.5x9":    {"d": 300,   "bf": 150, "tw": 6.5, "tf": 9,   "r": 13},
    "IWF303.4x165x6x10.2": {"d": 303.4, "bf": 165, "tw": 6,   "tf": 10.2,"r": 8.9},
    "IWF350x175x7x11":     {"d": 350,   "bf": 175, "tw": 7,   "tf": 11,  "r": 13},
    "IWF400x200x8x13":     {"d": 400,   "bf": 200, "tw": 8,   "tf": 13,  "r": 16},
    "IWF450x200x9x14":     {"d": 450,   "bf": 200, "tw": 9,   "tf": 14,  "r": 16},
    "IWF500x200x10x16":    {"d": 500,   "bf": 200, "tw": 10,  "tf": 16,  "r": 20},
}

# Material defaults (BJ 41)
Fy_DEFAULT = 250.0    # MPa
E_STEEL    = 200000.0 # MPa
G_STEEL    = 77200.0  # MPa
RHO_STEEL  = 7850.0   # kg/m³

# DCR target for selected section
DCR_LIMIT  = 0.95


# ═══════════════════════════════════════════════════════════════════
# SECTION PROPERTIES
# ═══════════════════════════════════════════════════════════════════

def compute_section_props(dims):
    """
    Compute IWF section properties from dimension dict {d, bf, tw, tf}.
    All input dimensions in mm.
    Returns dict with A (mm²), I (mm⁴), S/Z (mm³), r (mm), J/Cw, w_kgm.
    """
    d  = float(dims["d"])
    bf = float(dims["bf"])
    tw = float(dims["tw"])
    tf = float(dims["tf"])

    h  = d - 2.0 * tf                   # clear web height (mm)
    A  = 2.0 * bf * tf + h * tw         # gross area (mm²)

    # Moments of inertia
    Ix = (bf * d**3 - (bf - tw) * h**3) / 12.0   # strong axis (mm⁴)
    Iy = (2.0 * tf * bf**3 + h * tw**3) / 12.0   # weak  axis (mm⁴)

    # Elastic section moduli
    Sx = Ix / (d / 2.0) if d > 0 else 0.0
    Sy = Iy / (bf / 2.0) if bf > 0 else 0.0

    # Plastic section moduli
    Zx = bf * tf * (d - tf) + tw * h**2 / 4.0
    Zy = (2.0 * tf * bf**2 + h * tw**2) / 4.0

    # Radii of gyration
    rx = math.sqrt(Ix / A) if A > 0 else 0.0
    ry = math.sqrt(Iy / A) if A > 0 else 0.0

    # Torsional constant (St. Venant, open section approximation)
    J = (2.0 * bf * tf**3 + h * tw**3) / 3.0

    # Warping constant Cw (doubly symmetric I, AISC Commentary)
    ho = d - tf  # distance between flange centroids
    Cw = (Iy * ho**2) / 4.0

    # Self-weight (kg/m)
    w_kgm = A * RHO_STEEL / 1.0e6

    return {
        "d": d, "bf": bf, "tf": tf, "tw": tw, "h": h,
        "A": A, "Ix": Ix, "Iy": Iy,
        "Sx": Sx, "Sy": Sy,
        "Zx": Zx, "Zy": Zy,
        "rx": rx, "ry": ry,
        "J": J, "Cw": Cw,
        "w_kgm": w_kgm,
    }


# Pre-compute and sort database by weight (ascending)
_DB_PROPS  = {name: compute_section_props(dims) for name, dims in IWF_DATABASE.items()}
_DB_SORTED = sorted(IWF_DATABASE.keys(), key=lambda n: _DB_PROPS[n]["w_kgm"])


# ═══════════════════════════════════════════════════════════════════
# CAPACITY FORMULAS (simplified, compact assumption)
# ═══════════════════════════════════════════════════════════════════

def phi_Pc_compression(props, K, L_mm, Fy=Fy_DEFAULT, E=E_STEEL):
    """
    AISC E7: φcPn (compression), compact column.
    φc = 0.90. Weak-axis governs (ry).
    """
    ry = props["ry"]
    A  = props["A"]
    if ry <= 0.0 or L_mm <= 0.0:
        return 0.90 * Fy * A  # stub: squash load

    KLr = K * L_mm / ry
    Fe  = math.pi**2 * E / KLr**2

    if KLr <= 4.71 * math.sqrt(E / Fy):
        Fcr = (0.658 ** (Fy / Fe)) * Fy   # inelastic buckling
    else:
        Fcr = 0.877 * Fe                   # elastic buckling

    return 0.90 * Fcr * A


def phi_Pt_tension(props, Fy=Fy_DEFAULT):
    """AISC D2: φtPn (tension yield). φt = 0.90."""
    return 0.90 * Fy * props["A"]


def phi_Mc_major(props, Lb_mm, Fy=Fy_DEFAULT, E=E_STEEL):
    """
    AISC F2: φbMn major axis (compact, doubly symmetric I).
    φb = 0.90. Cb = 1.0 (conservative).
    Includes LTB (Lp → Lr interpolation).
    """
    Zx = props["Zx"]
    Sx = props["Sx"]
    ry = props["ry"]
    Iy = props["Iy"]
    J  = props["J"]
    Cw = props["Cw"]
    d  = props["d"]
    tf = props["tf"]

    Mp = Fy * Zx   # N·mm (plastic moment)

    # rts (AISC F2-7)
    rts = math.sqrt(math.sqrt(max(Iy, 1.0) * max(Cw, 1.0)) / max(Sx, 1.0))

    # Lp (AISC F2-5) — yield limit unbraced length
    Lp = 1.76 * ry * math.sqrt(E / Fy)

    # Lr (AISC F2-6) — inelastic LTB limit
    ho  = d - tf
    c   = 1.0  # doubly symmetric I
    try:
        inner = (J * c / (Sx * ho))**2 + 6.76 * (0.7 * Fy / E)**2
        Lr = 1.95 * rts * (E / (0.7 * Fy)) * math.sqrt(
            J * c / (Sx * ho) + math.sqrt(inner))
    except Exception:
        Lr = Lp * 10.0

    Cb = 1.0  # conservative

    if Lb_mm <= 0.0 or Lb_mm <= Lp:
        Mn = Mp
    elif Lb_mm <= Lr:
        Mn = Cb * (Mp - (Mp - 0.7 * Fy * Sx) * (Lb_mm - Lp) / (Lr - Lp))
        Mn = min(Mn, Mp)
    else:
        # Elastic LTB (AISC F2-4)
        try:
            Fcr_ltb = (Cb * math.pi**2 * E / (Lb_mm / rts)**2 *
                       math.sqrt(1.0 + 0.078 * J * c / (Sx * ho) * (Lb_mm / rts)**2))
            Mn = min(Fcr_ltb * Sx, Mp)
        except Exception:
            Mn = 0.9 * Mp  # fallback

    return 0.90 * Mn


def phi_Mc_minor(props, Fy=Fy_DEFAULT):
    """AISC F6: φbMn minor axis (compact). φb = 0.90."""
    Mp_min = min(Fy * props["Zy"], 1.6 * Fy * props["Sy"])
    return 0.90 * Mp_min


def phi_Vc_major(props, Fy=Fy_DEFAULT, E=E_STEEL):
    """
    AISC G2.1: φvVn major axis shear.
    φv = 1.0 when h/tw ≤ 2.24√(E/Fy); else 0.9.
    """
    d  = props["d"]
    tw = props["tw"]
    h  = props["h"]
    Aw = d * tw

    htw  = h / tw if tw > 0 else 999.0
    lim_cv1 = 2.24 * math.sqrt(E / Fy)

    if htw <= lim_cv1:
        phi_v = 1.0
        Cv1   = 1.0
    else:
        phi_v = 0.9
        kv    = 5.34
        l1 = 1.10 * math.sqrt(kv * E / Fy)
        l2 = 1.37 * math.sqrt(kv * E / Fy)
        if htw <= l1:
            Cv1 = 1.0
        elif htw <= l2:
            Cv1 = l1 / htw
        else:
            Cv1 = 1.51 * kv * E / (htw**2 * Fy)

    return phi_v * 0.6 * Fy * Aw * Cv1


# ═══════════════════════════════════════════════════════════════════
# DCR COMPUTATION
# ═══════════════════════════════════════════════════════════════════

def _get_element_length(elem_result):
    """
    Estimate element length from max station location in station_details.
    Falls back to 3000 mm if not available.
    """
    stations = elem_result.get("station_details", [])
    L = 0.0
    for st in stations:
        loc = st.get("location_mm", 0.0)
        if loc > L:
            L = loc
    return L if L > 0.0 else 3000.0


def compute_dcr(elem_result, props, Fy=Fy_DEFAULT):
    """
    Compute simplified DCR for one element using the governing forces
    recorded in Design Result.json (pmm_detail + shear_detail).

    Returns:
        dcr  (float) — demand/capacity ratio
        dcr_type (str) — controlling check description
    """
    design_type = elem_result.get("design_type", "Beam")
    pmm         = elem_result.get("pmm_detail",   {})
    shear_det   = elem_result.get("shear_detail",  {})

    # Governing forces (kN / kN-m) → convert to N / N·mm
    Pr_kN      = pmm.get("Pr_kN",          0.0)
    MrMaj_kNm  = pmm.get("MrMajor_kNm",   0.0)
    MrMin_kNm  = pmm.get("MrMinor_kNm",   0.0)
    Vr_kN      = shear_det.get("VrMajDsgn_kN", 0.0)

    Pr_N       = abs(Pr_kN)    * 1000.0
    MrMaj_Nmm  = abs(MrMaj_kNm) * 1.0e6
    MrMin_Nmm  = abs(MrMin_kNm) * 1.0e6
    Vr_N       = abs(Vr_kN)    * 1000.0

    L_mm  = _get_element_length(elem_result)
    Lb_mm = L_mm  # conservative: full unsupported length

    K = 1.0  # default effective length factor

    # Capacities for the trial section
    PcComp = max(phi_Pc_compression(props, K, L_mm, Fy), 1.0)
    PcTen  = max(phi_Pt_tension(props, Fy),                1.0)
    McMaj  = max(phi_Mc_major(props, Lb_mm, Fy),           1.0)
    McMin  = max(phi_Mc_minor(props, Fy),                  1.0)
    Vc     = max(phi_Vc_major(props, Fy),                  1.0)

    dcr_shear = Vr_N / Vc

    if design_type == "Column":
        # PMM check per AISC H1-1 (compression governs if Pr_kN < 0)
        is_comp = Pr_kN <= 0.0
        Pc_use  = PcComp if is_comp else PcTen
        pr      = Pr_N  / Pc_use
        mr_maj  = MrMaj_Nmm / McMaj
        mr_min  = MrMin_Nmm / McMin
        if pr >= 0.2:
            dcr_pmm = pr + (8.0 / 9.0) * (mr_maj + mr_min)   # H1-1a
        else:
            dcr_pmm = pr / 2.0 + (mr_maj + mr_min)            # H1-1b
        dcr      = max(dcr_pmm, dcr_shear)
        dcr_type = "PMM+V"
    else:
        # Beam: major flexure + shear
        dcr_flex = MrMaj_Nmm / McMaj
        dcr      = max(dcr_flex, dcr_shear)
        dcr_type = "Flex+V"

    return dcr, dcr_type


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def parse_section_name(design_section_str):
    """
    Extract bare section name from design_section field.
    "M_W Shapes-Column : IWF307.4x305.3x9.9x15.4"  →  "IWF307.4x305.3x9.9x15.4"
    """
    if ":" in design_section_str:
        return design_section_str.split(":")[-1].strip()
    return design_section_str.strip()


def section_weight_kgm(sec_name):
    """Return weight per meter (kg/m) from IWF_DATABASE, or 0 if not found."""
    if sec_name in _DB_PROPS:
        return _DB_PROPS[sec_name]["w_kgm"]
    return 0.0


# ═══════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ═══════════════════════════════════════════════════════════════════

def run_autoselect(design_result_path, autoselect_result_path):
    """
    Main entry point. Reads design results, runs auto-select,
    writes Auto-Select Result.json and appends autoselect_history.json.
    """
    # --- 1. Load Design Result ---
    with open(design_result_path, "r") as f:
        design_data = json.load(f)

    design_info = design_data.get("design_info", {})
    dcr_limit   = design_info.get("dcr_limit", 0.95)
    all_elements = design_data.get("elements", [])

    Fy = Fy_DEFAULT  # Use BJ41 default; extend here if material varies

    # --- 2. Separate NG and OK ---
    ng_elements = [
        e for e in all_elements
        if e.get("status", "OK") != "OK" or e.get("governing_ratio", 0.0) > dcr_limit
    ]
    ok_elements = [e for e in all_elements if e not in ng_elements]

    changes    = []
    unresolved = []

    # --- 3. Auto-select for each NG element ---
    for elem in ng_elements:
        elem_id  = elem.get("element_id", 0)
        label    = elem.get("label_name", elem.get("frame_label", "?"))
        dcr_orig = elem.get("governing_ratio", 0.0)
        sec_orig = parse_section_name(elem.get("design_section", ""))
        w_orig   = section_weight_kgm(sec_orig)

        found = False
        for sec_name in _DB_SORTED:   # iterate small → large
            props          = _DB_PROPS[sec_name]
            dcr_new, dtype = compute_dcr(elem, props, Fy)

            if dcr_new <= DCR_LIMIT:
                w_new    = props["w_kgm"]
                delta_w  = ((w_new - w_orig) / w_orig * 100.0) if w_orig > 0.0 else 0.0

                changes.append({
                    "element_id":        elem_id,
                    "label_name":        label,
                    "design_type":       elem.get("design_type", "?"),
                    "check_type":        dtype,
                    "before": {
                        "section":    sec_orig,
                        "dcr":        round(dcr_orig, 4),
                        "weight_kgm": round(w_orig, 3),
                    },
                    "after": {
                        "section":    sec_name,
                        "dcr":        round(dcr_new, 4),
                        "weight_kgm": round(w_new, 3),
                    },
                    "weight_increase_pct": round(delta_w, 1),
                })
                found = True
                break

        if not found:
            unresolved.append({
                "element_id": elem_id,
                "label_name": label,
                "design_type": elem.get("design_type", "?"),
                "section": sec_orig,
                "dcr": round(dcr_orig, 4),
                "note": "No section in IWF_DATABASE satisfies DCR <= {:.2f}".format(DCR_LIMIT),
            })

    # --- 4. Build output ---
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    result = {
        "timestamp":   timestamp,
        "design_info": design_info,
        "summary": {
            "total_elements": len(all_elements),
            "ng_elements":    len(ng_elements),
            "ok_elements":    len(ok_elements),
            "changed":        len(changes),
            "not_resolved":   len(unresolved),
        },
        "changes":    changes,
        "not_resolved": unresolved,
        "ok_elements": [
            {
                "element_id":  e.get("element_id"),
                "label_name":  e.get("label_name", e.get("frame_label", "?")),
                "design_type": e.get("design_type", "?"),
                "section":     parse_section_name(e.get("design_section", "")),
                "dcr":         round(e.get("governing_ratio", 0.0), 4),
            }
            for e in ok_elements
        ],
    }

    # --- 5. Write Auto-Select Result.json ---
    os.makedirs(os.path.dirname(autoselect_result_path) or ".", exist_ok=True)
    with open(autoselect_result_path, "w") as f:
        json.dump(result, f, indent=2)

    # --- 6. Append to history ---
    history_path = os.path.join(os.path.dirname(os.path.abspath(autoselect_result_path)),
                                "autoselect_history.json")
    history = {"runs": []}
    if os.path.exists(history_path):
        try:
            with open(history_path, "r") as f:
                history = json.load(f)
        except Exception:
            history = {"runs": []}

    history["runs"].append({
        "timestamp": timestamp,
        "changes":   changes,
        "summary": {
            "total_changed": len(changes),
            "not_resolved":  len(unresolved),
            "total_ok":      len(ok_elements),
        },
    })

    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)

    # --- 7. Print summary (captured by pyRevit script) ---
    print("=== AUTO-SELECT SUMMARY ===")
    print("Timestamp    : {}".format(timestamp))
    print("NG elements  : {}".format(len(ng_elements)))
    print("Resolved     : {}".format(len(changes)))
    print("Not resolved : {}".format(len(unresolved)))
    print("OK elements  : {}".format(len(ok_elements)))

    if changes:
        print("")
        hdr = "{:<22} {:<7} {:<25} {:>6}  {:<25} {:>6}  {:>6}".format(
            "Label", "Type", "Section Asal", "DCR", "Rekomendasi", "DCR Baru", "ΔBerat%")
        print(hdr)
        print("-" * len(hdr))
        for c in changes:
            print("{:<22} {:<7} {:<25} {:>6.3f}  {:<25} {:>6.3f}  {:>+6.1f}%".format(
                c["label_name"], c["design_type"],
                c["before"]["section"], c["before"]["dcr"],
                c["after"]["section"],  c["after"]["dcr"],
                c["weight_increase_pct"]))

    if unresolved:
        print("")
        print("=== TIDAK DAPAT DISELESAIKAN ===")
        for nc in unresolved:
            print("  {} ({}) DCR={:.3f} — {}".format(
                nc["label_name"], nc["design_type"], nc["dcr"], nc["note"]))

    print("")
    print("Output  : {}".format(autoselect_result_path))
    print("History : {}".format(history_path))

    return result


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python \"Autoselect Engine.py\" "
              "<design_result.json> <autoselect_result.json>")
        sys.exit(1)

    _design_path    = sys.argv[1]
    _autosel_path   = sys.argv[2]

    if not os.path.exists(_design_path):
        print("ERROR: File tidak ditemukan: {}".format(_design_path))
        sys.exit(1)

    run_autoselect(_design_path, _autosel_path)
