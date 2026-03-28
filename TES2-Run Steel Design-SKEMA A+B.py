#! python3
import clr
import System # type: ignore
import json
import os
import subprocess
import sys
from System.Collections.Generic import List

try:
    if sys.stdout and not hasattr(sys.stdout, "flush"):
        sys.stdout.flush = lambda: None
except Exception:
    pass

# 1. LOAD LIBRARIES
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, FillPatternElement,
    Transaction, ElementId, Color,
    BuiltInCategory, BuiltInParameterGroup,
    OverrideGraphicSettings,
    ExternalDefinitionCreationOptions
)
try:
    from Autodesk.Revit.DB import SpecTypeId
except ImportError:
    pass
try:
    from Autodesk.Revit.DB import ParameterType
except ImportError:
    pass
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.UI import TaskDialog
from pyrevit import script, HOST_APP, revit

"""
Design Check - pyRevit Push Button Script
==========================================
Launches the Steel Design Engine via subprocess and displays
the design results in the pyRevit console.

Reads: Result.json (from Create.pushbutton)
Writes: Design Result.json (via Steel Design Engine.py)
"""

__title__ = "Design\nCheck"
__author__ = "ROIDA"
__doc__ = "Run AISC 360-22 steel design check (LRFD) for all elements"

#UNTUK_SAMBUNGAN_BAJA — Path ke Result.json (input) dan Design Result.json (output) — dikonsumsi Connection Engine untuk gaya desain
# === PATHS ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..",
                                            "Create.pushbutton"))
ENGINE_PATH = os.path.join(SCRIPT_DIR, "Steel Design Engine",
                           "Steel Design Engine.py")
RESULT_PATH = os.path.join(CREATE_DIR, "Result.json")
DESIGN_RESULT_PATH = os.path.join(SCRIPT_DIR, "Steel Design Engine",
                                  "Design Result.json")

# Python executable
# PENTING: sys.executable di pyRevit CPython = Revit.exe, BUKAN Python.exe!
def _find_python():
    """Cari Python 3 executable yang valid."""
    candidates = [
        r"C:\Users\hp\AppData\Local\Programs\Python\Python312\python.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python311\python.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python310\python.exe",
        r"C:\Python312\python.exe",
        r"C:\Python311\python.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "python"

PYTHON_EXE = _find_python()

# === DESIGN CONFIGURATION ===
# Metode analisis desain dibaca otomatis dari Result.json
# (ditentukan di Create.pushbutton/script.py → ANALYSIS_METHOD)
def _read_analysis_method():
    """Baca analysis_method dari Result.json. Default: ELM."""
    try:
        with open(RESULT_PATH, 'r') as f:
            data = json.load(f)
        return data.get("model_data", {}).get(
            "seismic_parameters", {}).get("analysis_method", "ELM")
    except Exception:
        return "ELM"

# --- PYREVIT OUTPUT ---
out = script.get_output()


# --- HELPER: Markdown Table ---
def print_table(data, columns, title=""):
    """Render tabel Markdown via pyRevit output.print_md()."""
    if not data:
        return
    if title:
        out.print_md("### " + title)

    md = "| " + " | ".join(columns) + " |\n"
    md += "| " + " | ".join([":---:" for _ in columns]) + " |\n"
    for row in data:
        md += "| " + " | ".join([str(x) for x in row]) + " |\n"

    out.print_md(md)
    out.print_md("---")


def _short_section(name):
    """Extract short section name (e.g. UC305x305x97)."""
    if ":" in name:
        return name.split(":")[-1].strip()
    return name


def run_design_engine(analysis_method="ELM"):
    """Run the Steel Design Engine as a subprocess."""
    method_desc = ("Effective Length Method (ELM)" if analysis_method == "ELM"
                   else "Direct Analysis Method (DAM)")
    out.print_md("# AISC 360-22 - STEEL DESIGN CHECK (LRFD)")
    out.print_md("**Analysis Method:** {} | **Kombinasi Beban:** SNI 1727-2020".format(
        method_desc))

    # Validate input
    if not os.path.exists(RESULT_PATH):
        out.print_md("> **ERROR:** Result.json tidak ditemukan!")
        out.print_md("> Path: `{}`".format(RESULT_PATH))
        return False

    if not os.path.exists(ENGINE_PATH):
        out.print_md("> **ERROR:** Steel Design Engine.py tidak ditemukan!")
        return False

    # Run engine
    out.print_md("Menjalankan Steel Design Engine...")
    out.print_md("- Python: `{}`".format(PYTHON_EXE))
    out.print_md("- Engine: `{}`".format(ENGINE_PATH))
    out.print_md("- Method: **{}**".format(analysis_method))

    try:
        proc = subprocess.run(
            [PYTHON_EXE, ENGINE_PATH, RESULT_PATH, DESIGN_RESULT_PATH,
             "--method={}".format(analysis_method)],
            capture_output=True,
            text=True,
            timeout=120
        )

        if proc.returncode != 0:
            out.print_md("> **ERROR:** Steel Design Engine gagal!")
            if proc.stderr:
                print(proc.stderr)
            return False

        # Print engine summary output
        if proc.stdout:
            print(proc.stdout)

        return True

    except subprocess.TimeoutExpired:
        out.print_md("> **ERROR:** Timeout (>120 detik)")
        return False
    except Exception as e:
        out.print_md("> **ERROR:** {}".format(str(e)))
        return False


#UNTUK_SAMBUNGAN_BAJA — Display DCR, capacity, governing combo → data ini menentukan gaya desain sambungan
def display_results():
    """Read Design Result.json and display results using pyRevit Markdown tables."""
    if not os.path.exists(DESIGN_RESULT_PATH):
        out.print_md("> **ERROR:** Design Result.json tidak ditemukan!")
        out.print_md("> Path: `{}`".format(DESIGN_RESULT_PATH))
        return

    with open(DESIGN_RESULT_PATH, 'r') as f:
        data = json.load(f)

    info = data.get("design_info", {})
    summary = data.get("summary", {})
    elements = data.get("elements", [])
    combos = info.get("combinations", {})

    columns_list = [e for e in elements if e.get("design_type") == "Column"]
    beams_list = [e for e in elements if e.get("design_type") == "Beam"]
    failed = [e for e in elements if e.get("status") != "OK"]

    # =====================================================================
    # HEADER + SUMMARY
    # =====================================================================
    out.print_md("# STEEL DESIGN RESULT")
    analysis_method = info.get('analysis_method', 'N/A')
    am_desc = {"ELM": "Effective Length Method (ELM)",
               "DAM": "Direct Analysis Method (DAM)"}.get(analysis_method, analysis_method)
    out.print_md("**Code:** {} | **Method:** {} | **Analysis:** {} | **Framing:** {}".format(
        info.get('code', 'N/A'), info.get('method', 'N/A'),
        am_desc, info.get('framing_type', 'N/A')))

    # --- Design parameters info ---
    dcr_limit = info.get('dcr_limit', 1.0)
    Ry_val = info.get('Ry', 1.0)
    rho_val = info.get('rho', 1.0)
    tau_b_val = info.get('tau_b', 1.0)
    framing = info.get('framing_type', 'N/A')

    out.print_md("**DCR Limit:** {} | **Ry:** {} | **rho:** {} | **tau_b:** {}".format(
        dcr_limit, Ry_val, rho_val, tau_b_val))

    # SMF-specific: show Lb/ry limit using material from first element
    if framing in ["SRPMK/SMF", "SMF"] and elements:
        # Get Fy and E from first element's material for display
        first_elem = elements[0]
        cap = first_elem.get("capacity", {})
        smf = first_elem.get("smf_checks", {})
        Lb_ry_lim = smf.get("Lb_ry_limit", 0)
        if Lb_ry_lim > 0:
            out.print_md("> **AISC 341-22 D1.2b:** Lb/ry &le; 0.086&middot;E/(Ry&middot;Fy) = **{:.1f}**".format(
                Lb_ry_lim))

    max_dcr = summary.get("max_DCR", {})
    summary_data = [
        ["Total Elements", str(summary.get('total_elements', 0))],
        ["Passed (OK)", str(summary.get('passed', 0))],
        ["Failed (NG)", str(summary.get('failed', 0))],
    ]
    if max_dcr:
        summary_data.append([
            "Max DCR",
            "{:.4f} ({})".format(max_dcr.get('DCR', 0), max_dcr.get('frame_label', '?'))
        ])

    print_table(summary_data, ["Parameter", "Value"], "Ringkasan Desain")

    # =====================================================================
    # TABLE 1: DCR SUMMARY
    # =====================================================================
    dcr_data = []
    for i, e in enumerate(elements, 1):
        pmm = e.get("pmm_detail", {})
        dcr = e.get("governing_ratio", 0)
        status = "OK" if e.get("status") == "OK" else "**NG**"
        sec = _short_section(e.get("design_section", "?"))

        dcr_data.append([
            str(i),
            str(e.get('element_id', '')),
            e.get('label_name', '-'),
            e.get('frame_label', '?'),
            e.get('design_type', '?'),
            sec,
            e.get('governing_combo', ''),
            pmm.get('equation', ''),
            "{:.2f}".format(pmm.get('Pr_kN', 0)),
            "{:.2f}".format(pmm.get('MrMajor_kNm', 0)),
            "{:.2f}".format(pmm.get('MrMinor_kNm', 0)),
            "{:.4f}".format(pmm.get('PRatio', 0)),
            "{:.4f}".format(pmm.get('MMajRatio', 0)),
            "{:.4f}".format(pmm.get('MMinRatio', 0)),
            "{:.4f}".format(dcr),
            status,
        ])

    print_table(dcr_data,
                ["No", "ElementID", "Label", "Frame", "Type", "Section", "Combo", "Eq.",
                 "Pr (kN)", "MrMaj (kN-m)", "MrMin (kN-m)",
                 "PRatio", "MMajR", "MMinR", "DCR", "Status"],
                "Design Check Ratio (DCR) Summary")

    # =====================================================================
    # TABLE 2: CAPACITY - COLUMNS
    # =====================================================================
    if columns_list:
        cap_col_data = []
        for e in columns_list:
            cap = e.get("capacity", {})
            sec = _short_section(e.get("design_section", "?"))
            cap_col_data.append([
                str(e.get('element_id', '')),
                e.get('label_name', '-'),
                e['frame_label'],
                sec,
                "{:.2f}".format(cap.get('PcComp_kN', 0)),
                "{:.2f}".format(cap.get('PcTension_kN', 0)),
                "{:.2f}".format(cap.get('McMajor_kNm', 0)),
                "{:.2f}".format(cap.get('McMinor_kNm', 0)),
                "{:.2f}".format(cap.get('PhiVnMajor_kN', 0)),
                "{:.2f}".format(cap.get('Cb', 0)),
                cap.get('section_class_axial', '?'),
                cap.get('section_class_flexure_flange', '?'),
                cap.get('section_class_flexure_web', '?'),
            ])

        print_table(cap_col_data,
                    ["ElementID", "Label", "Frame", "Section", "PcComp (kN)", "PcTens (kN)",
                     "McMaj (kN-m)", "McMin (kN-m)", "PhiVn (kN)", "Cb",
                     "Axial", "Flange", "Web"],
                    "Design Capacity - Columns")

    # =====================================================================
    # TABLE 3: CAPACITY - BEAMS
    # =====================================================================
    if beams_list:
        cap_beam_data = []
        for e in beams_list:
            cap = e.get("capacity", {})
            sec = _short_section(e.get("design_section", "?"))
            cap_beam_data.append([
                str(e.get('element_id', '')),
                e.get('label_name', '-'),
                e['frame_label'],
                sec,
                "{:.2f}".format(cap.get('PcComp_kN', 0)),
                "{:.2f}".format(cap.get('PcTension_kN', 0)),
                "{:.2f}".format(cap.get('McMajor_kNm', 0)),
                "{:.2f}".format(cap.get('McMinor_kNm', 0)),
                "{:.2f}".format(cap.get('PhiVnMajor_kN', 0)),
                "{:.2f}".format(cap.get('Cb', 0)),
                cap.get('section_class_axial', '?'),
                cap.get('section_class_flexure_flange', '?'),
                cap.get('section_class_flexure_web', '?'),
            ])

        print_table(cap_beam_data,
                    ["ElementID", "Label", "Frame", "Section", "PcComp (kN)", "PcTens (kN)",
                     "McMaj (kN-m)", "McMin (kN-m)", "PhiVn (kN)", "Cb",
                     "Axial", "Flange", "Web"],
                    "Design Capacity - Beams")

    # =====================================================================
    # TABLE 4: SHEAR CHECK
    # =====================================================================
    shear_data = []
    for e in elements:
        shear = e.get("shear_detail", {})
        vr = shear.get("VMajorRatio", 0)
        sec = _short_section(e.get("design_section", "?"))
        st = "OK" if vr <= 1.0 else "**NG**"
        shear_data.append([
            str(e.get('element_id', '')),
            e.get('label_name', '-'),
            e.get('frame_label', '?'),
            e.get('design_type', '?'),
            sec,
            shear.get('VMajorCombo', ''),
            "{:.0f}".format(shear.get('VMajorLocation_mm', 0)),
            "{:.2f}".format(shear.get('VrMajDsgn_kN', 0)),
            "{:.2f}".format(shear.get('PhiVnMajor_kN', 0)),
            "{:.4f}".format(vr),
            st,
        ])

    print_table(shear_data,
                ["ElementID", "Label", "Frame", "Type", "Section", "Combo", "Loc (mm)",
                 "Vr (kN)", "PhiVn (kN)", "VRatio", "Status"],
                "Shear Check Detail")

    # =====================================================================
    # TABLE 5: AISC 341 SEISMIC CHECKS (SMF/SRPMK)
    # =====================================================================
    framing = info.get('framing_type', 'N/A')
    if framing in ["SRPMK/SMF", "SMF"]:
        seismic_data = []
        for e in elements:
            smf = e.get("smf_checks")
            if not smf:
                continue
            fl = e.get("frame_label", "?")
            etype = e.get("design_type", "?")
            sec = _short_section(e.get("design_section", "?"))
            msgs = []

            # Column L/r check (AISC 341-22 E3.4c(b))
            if "Lr_max" in smf:
                lr_val = smf.get("Lr_max", 0)
                lr_ok = smf.get("Lr_ok", True)
                if not lr_ok:
                    msgs.append("L/r={:.1f} > 60".format(lr_val))

            # Lb/ry check (AISC 341-22 D1.2b)
            if "Lb_ry" in smf:
                lb_ry = smf.get("Lb_ry", 0)
                lb_lim = smf.get("Lb_ry_limit", 0)
                lb_ok = smf.get("Lb_ry_ok", True)
                if not lb_ok:
                    msgs.append("Lb/ry={:.1f} > {:.1f}".format(lb_ry, lb_lim))

            # HD compactness (AISC 341-22 Table D1.1b)
            sd = smf.get("seismic_ductility", {})
            flange_ok = sd.get("flange_ok", True)
            web_ok = sd.get("web_ok", True)
            lf = sd.get("lambda_flange", 0)
            lf_hd = sd.get("lambda_hd_flange", 0)
            lw = sd.get("lambda_web", 0)
            lw_hd = sd.get("lambda_hd_web", 0)
            if not flange_ok:
                msgs.append("&lambda;f={:.2f} > &lambda;hd={:.2f}".format(lf, lf_hd))
            if not web_ok:
                msgs.append("&lambda;w={:.2f} > &lambda;hd={:.2f}".format(lw, lw_hd))

            err_str = "; ".join(msgs) if msgs else "OK"
            status_mark = "OK" if not msgs else "**NG**"

            seismic_data.append([
                fl,
                etype,
                sec,
                "{:.1f}".format(smf.get("Lb_ry", 0)) if "Lb_ry" in smf else "-",
                "{:.1f}".format(smf.get("Lb_ry_limit", 0)) if "Lb_ry_limit" in smf else "-",
                "OK" if smf.get("Lb_ry_ok", True) else "**NG**",
                "{:.2f}".format(lf),
                "{:.2f}".format(lf_hd),
                "OK" if flange_ok else "**NG**",
                "{:.2f}".format(lw),
                "{:.2f}".format(lw_hd),
                "OK" if web_ok else "**NG**",
                status_mark,
                err_str,
            ])

        print_table(seismic_data,
                    ["Frame", "Type", "Section",
                     "Lb/ry", "Limit", "Bracing",
                     "&lambda;f", "&lambda;hd,f", "Flange",
                     "&lambda;w", "&lambda;hd,w", "Web",
                     "Status", "Error Message"],
                    "AISC 341-22 Seismic Checks — Highly Ductile (SMF)")

    # =====================================================================
    # TABLE 6: PER-ELEMENT STATION DETAIL (SAP2000 Style)
    # =====================================================================
    out.print_md("### Station Detail per Element")
    out.print_md("Menampilkan RATIO = AXL + B-MAJ + B-MIN per combo per station")

    for e in elements:
        details = e.get("station_details", [])
        if not details:
            continue

        sec = _short_section(e.get("design_section", "?"))
        elem_label = "{} | {} (ID:{}) - {} - {}".format(
            e.get('label_name', '-'), e.get('frame_label', '?'), e.get('element_id', ''),
            e.get('design_type', '?'), sec)

        detail_data = []
        for d in details:
            eq_tag = "(C)" if d.get("equation") == "H1-1a" else "(T)" if d.get("axl", 0) > 0 else "(C)"
            ratio_str = "{:.3f}{}".format(d.get("ratio", 0), eq_tag)
            detail_data.append([
                d.get("combo", ""),
                "{:.0f}".format(d.get("location_mm", 0)),
                ratio_str,
                "{:.3f}".format(d.get("axl", 0)),
                "{:.3f}".format(d.get("b_maj", 0)),
                "{:.3f}".format(d.get("b_min", 0)),
                "{:.3f}".format(d.get("maj_shr", 0)),
                "{:.3f}".format(d.get("min_shr", 0)),
            ])

        print_table(detail_data,
                    ["Combo", "Loc", "RATIO", "AXL", "B-MAJ", "B-MIN",
                     "MAJ-SHR", "MIN-SHR"],
                    elem_label)

    # =====================================================================
    # TABLE 7: OVERSTRESSED ELEMENTS DETAIL
    # =====================================================================
    if failed:
        out.print_md("### Overstressed Elements Detail ({} elemen)".format(len(failed)))

        for elem in failed:
            sec = _short_section(elem.get("design_section", "?"))
            cap = elem.get("capacity", {})
            pmm = elem.get("pmm_detail", {})
            shear = elem.get("shear_detail", {})

            out.print_md("#### {} | {} - {} ({})".format(
                elem.get('label_name', '-'), elem['frame_label'], sec, elem['design_type']))

            # Capacity vs Demand table
            cvd_data = [
                ["PcComp", "{:.2f} kN".format(cap.get('PcComp_kN', 0)),
                 "Pr", "{:.2f} kN".format(pmm.get('Pr_kN', 0))],
                ["PcTension", "{:.2f} kN".format(cap.get('PcTension_kN', 0)),
                 "", ""],
                ["McMajor", "{:.2f} kN-m".format(cap.get('McMajor_kNm', 0)),
                 "MrMajor", "{:.2f} kN-m".format(pmm.get('MrMajor_kNm', 0))],
                ["McMinor", "{:.2f} kN-m".format(cap.get('McMinor_kNm', 0)),
                 "MrMinor", "{:.2f} kN-m".format(pmm.get('MrMinor_kNm', 0))],
                ["PhiVnMaj", "{:.2f} kN".format(cap.get('PhiVnMajor_kN', 0)),
                 "VrMajor", "{:.2f} kN".format(shear.get('VrMajDsgn_kN', 0))],
            ]
            print_table(cvd_data,
                        ["Capacity", "Value", "Demand", "Value"],
                        "")

            # Section classification
            out.print_md("**Section Class:** Axial={} | Flange={} | Web={} | Cb={:.4f}".format(
                cap.get('section_class_axial', '?'),
                cap.get('section_class_flexure_flange', '?'),
                cap.get('section_class_flexure_web', '?'),
                cap.get('Cb', 0)))

            # PMM Interaction
            eq = pmm.get('equation', '?')
            pr_pc = pmm.get('PRatio', 0)
            mmaj = pmm.get('MMajRatio', 0)
            mmin = pmm.get('MMinRatio', 0)
            dcr = pmm.get('TotalRatio', 0)

            out.print_md("**PMM Interaction ({})** - Governing: {} at {:.0f} mm".format(
                eq, elem.get('governing_combo', ''),
                elem.get('governing_location_mm', 0)))

            if eq == 'H1-1a':
                out.print_md("Pr/Pc = {:.4f} >= 0.2".format(pr_pc))
                out.print_md("DCR = Pr/Pc + 8/9(Mrx/Mcx + Mry/Mcy) = {:.4f} + 8/9 x ({:.4f} + {:.4f})".format(
                    pr_pc, mmaj, mmin))
            else:
                out.print_md("Pr/Pc = {:.4f} < 0.2".format(pr_pc))
                out.print_md("DCR = Pr/2Pc + (Mrx/Mcx + Mry/Mcy) = {:.4f}/2 + ({:.4f} + {:.4f})".format(
                    pr_pc, mmaj, mmin))

            if dcr > 1.0:
                out.print_md("> **DCR = {:.4f} >> FAIL (> 1.0)**".format(dcr))
            else:
                out.print_md("DCR = {:.4f} >> OK".format(dcr))

            vr = shear.get("VMajorRatio", 0)
            out.print_md("Shear: Vr/PhiVn = {:.4f} >> {}".format(
                vr, "**FAIL**" if vr > 1.0 else "OK"))

            out.print_md("---")

    # =====================================================================
    # LOAD COMBINATIONS
    # =====================================================================
    if combos:
        combo_mode = info.get("combo_mode", "default")
        combo_data = []
        for name, formula in combos.items():
            combo_data.append([name, formula])
        title = "Load Combinations ({} mode, {} combos)".format(combo_mode, len(combos))
        print_table(combo_data, ["DSTL", "Formula"], title)

    # =====================================================================
    # FOOTER
    # =====================================================================
    out.print_md("---")
    out.print_md("**Design check selesai.**")
    out.print_md("Output: `{}`".format(DESIGN_RESULT_PATH))


# =====================================================================
# REVIT INTEGRATION — DCR COLOR OVERRIDE & CUSTOM PARAMETERS
# =====================================================================
# Referensi:
#   - SAP2000 Steel P-M Interaction Ratios color scheme
#   - REFERENSI PROPERTIES OVERIDE/Parameter_Created.py
#   - REFERENSI PROPERTIES OVERIDE/Color_Override.py
# =====================================================================

SHARED_PARAMS_PATH = os.path.join(SCRIPT_DIR, "ROIDA_SharedParams.txt")

_ROIDA_PARAM_NAMES = [
    "ROIDA_DCR",
    "ROIDA_Status",
    "ROIDA_Combo",
    "ROIDA_Equation",
    "ROIDA_Label",
]

# --- API compatibility (Revit 2022+ vs older) ---
try:
    _SPEC_TEXT = SpecTypeId.String.Text
except Exception:
    _SPEC_TEXT = ParameterType.Text

try:
    _PARAM_GROUP = GroupTypeId.StructuralAnalysis
except Exception:
    _PARAM_GROUP = BuiltInParameterGroup.PG_STRUCTURAL_ANALYSIS


def _has_section_error(status):
    """Check if element has seismic section error (SMF-NG, IMF-NG, etc.)."""
    if not status:
        return False
    s = status.upper()
    return "SMF-NG" in s or "IMF-NG" in s or "HD-NG" in s or "L/R=" in s or "LB/RY=" in s


def _dcr_color(dcr, status=""):
    """SAP2000-style color scheme for Steel P-M Interaction Ratios.
    If element has section error (SRPMK/IMF), always return Red.
    """
    if _has_section_error(status):
        return Color(255, 0, 0)       # Red — section error
    if dcr <= 0.0:
        return Color(192, 192, 192)   # Gray — no demand
    if dcr <= 0.5:
        return Color(0, 255, 255)     # Cyan
    if dcr <= 0.7:
        return Color(0, 200, 0)       # Green
    if dcr <= 0.9:
        return Color(255, 255, 0)     # Yellow
    if dcr <= 1.0:
        return Color(255, 140, 0)     # Orange
    return Color(255, 0, 0)           # Red — NG


def _get_solid_fill_id(doc):
    """Find the solid fill pattern ID for surface color override."""
    collector = FilteredElementCollector(doc).OfClass(FillPatternElement)
    for pat_elem in collector:
        fp = pat_elem.GetFillPattern()
        if fp and fp.IsSolidFill:
            return pat_elem.Id
    return None


def _create_definitions(app, read_only=True):
    """Create ROIDA shared parameter definitions (file I/O, no transaction).

    Temporarily switches the app's shared params file, creates definitions,
    then restores the original file path.
    Returns list of ExternalDefinition objects.
    read_only: jika True, parameter tidak bisa diedit user di Properties panel.
    """
    orig_spf = ""
    try:
        orig_spf = app.SharedParametersFilename
    except Exception:
        pass

    definitions = []
    try:
        # Selalu recreate shared params file agar UserModifiable=False berlaku
        with open(SHARED_PARAMS_PATH, 'w') as f:
            f.write("# This is a Revit shared parameter file.\n")
            f.write("# Do not edit manually.\n")
            f.write("*META\tVERSION\tMINVERSION\n")
            f.write("META\t2\t1\n")
            f.write("*GROUP\tID\tNAME\n")
            f.write("*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\t"
                    "GROUP\tVISIBLE\tDESCRIPTION\tUSERMODIFIABLE\n")

        app.SharedParametersFilename = SHARED_PARAMS_PATH
        spf = app.OpenSharedParameterFile()

        group = spf.Groups.Create("ROIDA")

        for pname in _ROIDA_PARAM_NAMES:
            opts = ExternalDefinitionCreationOptions(pname, _SPEC_TEXT)
            opts.UserModifiable = not read_only
            defn = group.Definitions.Create(opts)
            definitions.append(defn)

        return definitions
    except Exception as e:
        out.print_md("> Peringatan: gagal buat parameter definitions — {}".format(e))
        return []
    finally:
        try:
            app.SharedParametersFilename = orig_spf if orig_spf else ""
        except Exception:
            pass


def _set_param(elem, name, value):
    """Set a text parameter value on a Revit element."""
    p = elem.LookupParameter(name)
    if p and not p.IsReadOnly:
        try:
            p.Set(str(value))
        except Exception:
            pass


def apply_dcr_to_revit(doc, read_only=True):
    """Apply DCR results to Revit model:
    1. Create ROIDA_* shared parameters (if not yet exist)
    2. Write DCR, Status, Combo, Equation, Label to each element
    3. Apply color override on active view (SAP2000 scheme)
    read_only: jika True, parameter tidak bisa diedit user.
    """
    if not os.path.exists(DESIGN_RESULT_PATH):
        return

    with open(DESIGN_RESULT_PATH, 'r') as f:
        data = json.load(f)
    elements = data.get("elements", [])
    if not elements:
        return

    app = HOST_APP.app
    active_view = doc.ActiveView
    solid_fill_id = _get_solid_fill_id(doc)

    # --- Step 1: Create shared param definitions (file I/O, no transaction) ---
    definitions = _create_definitions(app, read_only=read_only)

    # --- Step 2: Transaction — bind params, set values, color override ---
    t = Transaction(doc, "ROIDA: Apply DCR to Model")
    t.Start()
    try:
        # Bind/rebind parameters
        params_ok = False
        if definitions:
            cats = app.Create.NewCategorySet()
            cats.Insert(doc.Settings.Categories.get_Item(
                BuiltInCategory.OST_StructuralColumns))
            cats.Insert(doc.Settings.Categories.get_Item(
                BuiltInCategory.OST_StructuralFraming))
            binding = app.Create.NewInstanceBinding(cats)

            # Hapus semua binding ROIDA_* lama (GUID lama, flag lama)
            bm = doc.ParameterBindings
            it = bm.ForwardIterator()
            old_defs = []
            while it.MoveNext():
                d = it.Key
                if d.Name.startswith("ROIDA_"):
                    old_defs.append(d)
            for d in old_defs:
                bm.Remove(d)

            # Insert fresh dengan GUID baru + UserModifiable=False
            for defn in definitions:
                doc.ParameterBindings.Insert(defn, binding, _PARAM_GROUP)

            doc.Regenerate()
            params_ok = True

        # --- Aggregate: group multi-story columns by Revit ID ---
        # Kolom multi-story: element_id = revit_id * 1000 + story_index
        # Balok: element_id = revit_id (langsung)
        #
        # Deteksi composite: kolom dengan >1 segmen sharing base yang sama.
        # Ini lebih robust daripada threshold angka (menghindari false positive
        # pada single-story kolom dengan ID seperti 469159).

        # Pass 1: identifikasi base composite (kolom multi-story)
        col_base_count = {}  # base → jumlah segmen
        for elem_data in elements:
            if elem_data.get("design_type", "") == "Column":
                eid = elem_data.get("element_id")
                if eid is not None:
                    base = eid // 1000
                    col_base_count[base] = col_base_count.get(base, 0) + 1
        composite_bases = {b for b, cnt in col_base_count.items() if cnt > 1}

        # Pass 2: aggregate per Revit element
        revit_map = {}    # revit_id → governing elem_data
        revit_labels = {} # revit_id → [label_name, ...]
        for elem_data in elements:
            eid_int = elem_data.get("element_id")
            if eid_int is None:
                continue

            if (elem_data.get("design_type", "") == "Column"
                    and eid_int // 1000 in composite_bases):
                revit_id = eid_int // 1000
            else:
                revit_id = eid_int

            # Kumpulkan label semua segmen
            seg_label = elem_data.get("label_name", "")
            if seg_label:
                revit_labels.setdefault(revit_id, []).append(seg_label)

            cur = revit_map.get(revit_id)
            if cur is None:
                revit_map[revit_id] = elem_data
            else:
                # Pilih governing: error status > DCR tertinggi
                cur_has_err = _has_section_error(cur.get("status", ""))
                new_has_err = _has_section_error(elem_data.get("status", ""))
                cur_dcr = cur.get("governing_ratio", 0.0)
                new_dcr = elem_data.get("governing_ratio", 0.0)
                if new_has_err and not cur_has_err:
                    revit_map[revit_id] = elem_data
                elif new_has_err == cur_has_err and new_dcr > cur_dcr:
                    revit_map[revit_id] = elem_data

        # Apply per Revit element
        applied = 0
        skipped = 0

        for revit_id, elem_data in revit_map.items():
            try:
                elem_id = ElementId(int(revit_id))
                elem = doc.GetElement(elem_id)
            except Exception:
                skipped += 1
                continue

            if elem is None:
                skipped += 1
                continue

            dcr = elem_data.get("governing_ratio", 0.0)
            status = elem_data.get("status", "?")
            combo = elem_data.get("governing_combo", "")
            equation = elem_data.get("pmm_detail", {}).get("equation", "")

            # Label: compact format untuk multi-story
            labels = revit_labels.get(revit_id, [])
            if len(labels) > 2:
                # "A-1/1 ~ A-1/5" untuk 3+ segmen
                label = "{} ~ {}".format(labels[0], labels[-1])
            elif len(labels) == 2:
                label = ", ".join(labels)
            else:
                label = labels[0] if labels else elem_data.get("label_name", "")

            # Write parameter values
            if params_ok:
                _set_param(elem, "ROIDA_DCR", "{:.4f}".format(dcr))
                _set_param(elem, "ROIDA_Status", status)
                _set_param(elem, "ROIDA_Combo", combo)
                _set_param(elem, "ROIDA_Equation", equation)
                _set_param(elem, "ROIDA_Label", label)

            # Color override on active view
            color = _dcr_color(dcr, status)
            ogs = OverrideGraphicSettings()
            if solid_fill_id:
                ogs.SetSurfaceForegroundPatternId(solid_fill_id)
                ogs.SetSurfaceForegroundPatternColor(color)
            active_view.SetElementOverrides(elem_id, ogs)

            applied += 1

        t.Commit()

        # --- Summary ---
        out.print_md("---")
        out.print_md("### Revit Model Updated")
        out.print_md("- **{}** elemen: warna override + parameter DCR".format(applied))
        if skipped:
            out.print_md("- {} elemen dilewati (tidak ditemukan di model)".format(
                skipped))
        if params_ok:
            out.print_md("- Properties: ROIDA_DCR, ROIDA_Status, "
                         "ROIDA_Combo, ROIDA_Equation, ROIDA_Label")
        legend = (
            "**Legenda Warna DCR (SAP2000 Convention):**\n\n"
            "| Range DCR | Warna | Keterangan |\n"
            "|:---:|:---:|:---|\n"
            "| &le; 0 | Gray | Tidak ada demand |\n"
            "| 0.0 - 0.5 | Cyan | Aman |\n"
            "| 0.5 - 0.7 | Green | Cukup |\n"
            "| 0.7 - 0.9 | Yellow | Mendekati batas |\n"
            "| 0.9 - 1.0 | Orange | Kritis |\n"
            "| > 1.0 | **Red** | **NG (Overstressed)** |"
        )
        out.print_md(legend)

    except Exception as e:
        t.RollBack()
        out.print_md("> **Error** apply DCR: {}".format(str(e)))
        import traceback
        traceback.print_exc()


# === MAIN ===
try:
    success = run_design_engine(analysis_method=_read_analysis_method())
    # Selalu tampilkan hasil jika Design Result.json ada
    display_results()
    # Apply DCR ke Revit model (color override + custom parameters)
    # True = parameter read-only, False = parameter editable
#===========================================================================
    PARAM_READ_ONLY = False
#===========================================================================
    apply_dcr_to_revit(revit.doc, read_only=PARAM_READ_ONLY)
except Exception as e:
    print("ERROR: {}".format(str(e)))
    import traceback
    traceback.print_exc()
