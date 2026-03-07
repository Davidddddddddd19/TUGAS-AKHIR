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

from Autodesk.Revit.DB import *
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

# === PATHS ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..",
                                            "Create.pushbutton"))
ENGINE_PATH = os.path.join(SCRIPT_DIR, "Run Design Check",
                           "Steel Design Engine.py")
RESULT_PATH = os.path.join(CREATE_DIR, "Result.json")
DESIGN_RESULT_PATH = os.path.join(SCRIPT_DIR, "Run Design Check",
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


def run_design_engine():
    """Run the Steel Design Engine as a subprocess."""
    out.print_md("# AISC 360-22 - STEEL DESIGN CHECK (LRFD)")
    out.print_md("**Kombinasi Beban: SNI 1727-2020**")

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

    try:
        proc = subprocess.run(
            [PYTHON_EXE, ENGINE_PATH, RESULT_PATH, DESIGN_RESULT_PATH],
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
    out.print_md("**Code:** {} | **Method:** {} | **Framing:** {}".format(
        info.get('code', 'N/A'), info.get('method', 'N/A'),
        info.get('framing_type', 'N/A')))

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
                ["No", "ElementID", "Frame", "Type", "Section", "Combo", "Eq.",
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
                    ["ElementID", "Frame", "Section", "PcComp (kN)", "PcTens (kN)",
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
                    ["ElementID", "Frame", "Section", "PcComp (kN)", "PcTens (kN)",
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
                ["ElementID", "Frame", "Type", "Section", "Combo", "Loc (mm)",
                 "Vr (kN)", "PhiVn (kN)", "VRatio", "Status"],
                "Shear Check Detail")

    # =====================================================================
    # TABLE 5: PER-ELEMENT STATION DETAIL (SAP2000 Style)
    # =====================================================================
    out.print_md("### Station Detail per Element")
    out.print_md("Menampilkan RATIO = AXL + B-MAJ + B-MIN per combo per station")

    for e in elements:
        details = e.get("station_details", [])
        if not details:
            continue

        sec = _short_section(e.get("design_section", "?"))
        elem_label = "{} (ID:{}) - {} - {}".format(
            e.get('frame_label', '?'), e.get('element_id', ''),
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
    # TABLE 6: OVERSTRESSED ELEMENTS DETAIL
    # =====================================================================
    if failed:
        out.print_md("### Overstressed Elements Detail ({} elemen)".format(len(failed)))

        for elem in failed:
            sec = _short_section(elem.get("design_section", "?"))
            cap = elem.get("capacity", {})
            pmm = elem.get("pmm_detail", {})
            shear = elem.get("shear_detail", {})

            out.print_md("#### {} - {} ({})".format(
                elem['frame_label'], sec, elem['design_type']))

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


# === MAIN ===
try:
    success = run_design_engine()
    # Selalu tampilkan hasil jika Design Result.json ada
    display_results()
except Exception as e:
    print("ERROR: {}".format(str(e)))
    import traceback
    traceback.print_exc()
