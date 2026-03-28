#! python3
import clr
import json
import os
import subprocess
import sys
import time

try:
    if sys.stdout and not hasattr(sys.stdout, "flush"):
        sys.stdout.flush = lambda: None
except Exception:
    pass

# Load Revit API
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import (
    Transaction, ElementId, FilteredElementCollector,
    BuiltInCategory, BuiltInParameter, BuiltInParameterGroup,
    FamilySymbol, FillPatternElement,
    OverrideGraphicSettings, Color,
    ExternalDefinitionCreationOptions, StorageType,
)

# Compat: SpecTypeId / GroupTypeId (Revit 2024+) vs older enums
try:
    from Autodesk.Revit.DB import SpecTypeId, GroupTypeId
except ImportError:
    pass
try:
    from Autodesk.Revit.DB import ParameterType
except ImportError:
    pass
from pyrevit import script, HOST_APP, revit

"""
Auto-Select Pushbutton — ROIDA (Full Pipeline)
================================================
Launches Autoselect Engine.py via subprocess.
After engine completes:
  1. Reads Auto-Select Result.json
  2. Updates Revit lookup tables with new sections
  3. Changes element section types via ChangeTypeId
  4. Applies DCR color override from Design_Result_New.json
  5. Displays summary tables
"""

__title__ = "Auto\nSelect"
__author__ = "ROIDA"
__doc__ = "Auto-select optimal IWF sections per group (AISC 360-22 LRFD, DCR 0.5-0.9)"

#UNTUK_SAMBUNGAN_BAJA — Path ke Result.json, Design Result, Result_New, Design_Result_New — Connection Engine harus detect source dari sini
# ═══════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
DESIGN_DIR = os.path.join(PANEL_DIR, "Design.pushbutton", "Steel Design Engine")
CREATE_DIR = os.path.join(PANEL_DIR, "Create.pushbutton")

DESIGN_RESULT_PATH = os.path.join(DESIGN_DIR, "Design Result.json")
RESULT_JSON_PATH = os.path.join(CREATE_DIR, "Result.json")
ENGINE_DIR = os.path.join(SCRIPT_DIR, "Autoselect Engine")
ENGINE_PATH = os.path.join(ENGINE_DIR, "Autoselect Engine.py")

# Output paths (in Auto Select.pushbutton folder)
OUTPUT_DIR = SCRIPT_DIR
AUTOSELECT_RESULT_PATH = os.path.join(OUTPUT_DIR, "Auto-Select Result.json")
RESULT_NEW_PATH = os.path.join(OUTPUT_DIR, "Result_New.json")
DESIGN_RESULT_NEW_PATH = os.path.join(OUTPUT_DIR, "Design_Result_New.json")

# Revit family lookup tables & RFA
BEAM_TXT_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Steel\AISC 15.0\M_W Shapes.txt"
COL_TXT_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Steel\AISC 15.0\M_W Shapes-Column.txt"
BEAM_RFA_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Steel\AISC 15.0\M_W Shapes.rfa"
COL_RFA_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Steel\AISC 15.0\M_W Shapes-Column.rfa"

# Shared params file for DCR parameters
SHARED_PARAMS_PATH = os.path.join(SCRIPT_DIR, "ROIDA_SharedParams.txt")

_ROIDA_PARAM_NAMES = [
    "ROIDA_DCR",
    "ROIDA_Status",
    "ROIDA_Combo",
    "ROIDA_Equation",
    "ROIDA_Label",
]

# API compatibility (Revit 2022+ vs older)
try:
    _SPEC_TEXT = SpecTypeId.String.Text
except Exception:
    _SPEC_TEXT = ParameterType.Text

try:
    _PARAM_GROUP = GroupTypeId.StructuralAnalysis
except Exception:
    _PARAM_GROUP = BuiltInParameterGroup.PG_STRUCTURAL_ANALYSIS


def _find_python():
    """Locate a valid Python 3 executable."""
    candidates = [
        r"C:\Users\hp\AppData\Local\Programs\Python\Python312\python.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python311\python.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python310\python.exe",
        r"C:\Python312\python.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "python"


PYTHON_EXE = _find_python()

# ═══════════════════════════════════════════════════════════════════
# PYREVIT OUTPUT
# ═══════════════════════════════════════════════════════════════════
out = script.get_output()


def print_table(data, columns, title=""):
    """Render a center-aligned Markdown table via pyRevit output."""
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


# ═══════════════════════════════════════════════════════════════════
# SECTION PROPERTIES (replicated from Create.pushbutton/script.py)
# ═══════════════════════════════════════════════════════════════════

def calc_section_props_from_dims(d, bf, tw, tf):
    """Compute all section properties from IWF dimensions (mm)."""
    h_web = d - 2 * tf
    Area = 2 * bf * tf + h_web * tw

    Iz = (bf * d**3 - (bf - tw) * h_web**3) / 12.0
    Iy = (2 * tf * bf**3 + h_web * tw**3) / 12.0

    Sz = Iz / (d / 2.0) if d > 0 else 0
    Sy = Iy / (bf / 2.0) if bf > 0 else 0

    Zz = bf * tf * (d - tf) + tw * h_web**2 / 4.0
    Zy = (bf**2 * tf) / 2.0 + (h_web * tw**2) / 4.0

    # Torsional constant — Euler finite-width correction
    corr_f = 1 - 0.63 * (tf / bf) + 0.052 * (tf / bf)**5 if bf > 0 else 1
    corr_w = 1 - 0.63 * (tw / h_web) + 0.052 * (tw / h_web)**5 if h_web > 0 else 1
    J = (2 * bf * tf**3 * corr_f + h_web * tw**3 * corr_w) / 3.0

    h0 = d - tf
    Cw = Iy * (h0**2) / 4.0

    Weight = Area * 7850 * 1e-6
    Perimeter = (2 * (d + bf)) * 1e-3

    return {
        "Area": round(Area, 0),
        "Weight": round(Weight, 1),
        "Perimeter": round(Perimeter, 3),
        "Iz": round(Iz, 0),
        "Iy": round(Iy, 0),
        "Sz": round(Sz, 0),
        "Sy": round(Sy, 0),
        "Zz": round(Zz, 0),
        "Zy": round(Zy, 0),
        "J": round(J, 0),
        "Cw": round(Cw, 0),
        "ClearWebHeight": round(h_web, 0),
    }


def generate_lookup_entry(section_name, dims):
    """Generate one CSV row for Revit lookup table .txt."""
    d, bf, tw, tf = dims["d"], dims["bf"], dims["tw"], dims["tf"]
    r = dims.get("r", 0)
    sp = calc_section_props_from_dims(d, bf, tw, tf)

    return "{name},{bf},{d},{tf},{tw},{r},{A},{W},{P},{Iz},{Iy},{Sz},{Sy},{Zz},{Zy},{J},{Cw},{hw},{bs},{nk}".format(
        name=section_name,
        bf=bf, d=d, tf=tf, tw=tw, r=r,
        A=int(sp["Area"]),
        W=sp["Weight"],
        P=sp["Perimeter"],
        Iz=int(sp["Iz"]),
        Iy=int(sp["Iy"]),
        Sz=int(sp["Sz"]),
        Sy=int(sp["Sy"]),
        Zz=int(sp["Zz"]),
        Zy=int(sp["Zy"]),
        J=int(sp["J"]),
        Cw="{:.2E}".format(sp["Cw"]),
        hw=int(sp["ClearWebHeight"]),
        bs=0,
        nk=section_name
    )


def update_lookup_table(txt_path, sections_dict):
    """Add new sections to Revit lookup table, preserving existing entries."""
    header = (",Width##SECTION_PROPERTY##MILLIMETERS"
              ",Height##SECTION_PROPERTY##MILLIMETERS"
              ",Flange Thickness##SECTION_PROPERTY##MILLIMETERS"
              ",Web Thickness##SECTION_PROPERTY##MILLIMETERS"
              ",Web Fillet##SECTION_PROPERTY##MILLIMETERS"
              ",Section Area##SECTION_AREA##SQUARE_MILLIMETERS"
              ",Nominal Weight##WEIGHT_PER_UNIT_LENGTH##KILOGRAMS_FORCE_PER_METER"
              ",Perimeter##SURFACE_AREA##SQUARE_METERS_PER_METER"
              ",Moment of Inertia strong axis##MOMENT_OF_INERTIA##MILLIMETERS_TO_THE_FOURTH_POWER"
              ",Moment of Inertia weak axis##MOMENT_OF_INERTIA##MILLIMETERS_TO_THE_FOURTH_POWER"
              ",Elastic Modulus strong axis##SECTION_MODULUS##CUBIC_MILLIMETERS"
              ",Elastic Modulus weak axis##SECTION_MODULUS##CUBIC_MILLIMETERS"
              ",Plastic Modulus strong axis##SECTION_MODULUS##CUBIC_MILLIMETERS"
              ",Plastic Modulus weak axis##SECTION_MODULUS##CUBIC_MILLIMETERS"
              ",Torsional Moment of Inertia##MOMENT_OF_INERTIA##MILLIMETERS_TO_THE_FOURTH_POWER"
              ",Warping Constant##WARPING_CONSTANT##MILLIMETERS_TO_THE_SIXTH_POWER"
              ",Clear Web Height##SECTION_DIMENSION##MILLIMETERS"
              ",Bolt Spacing##SECTION_DIMENSION##MILLIMETERS"
              ",Section Name Key##other##")

    # Read existing entries, preserving them
    existing_lines = []
    existing_names = set()
    if os.path.exists(txt_path):
        try:
            with open(txt_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    name = line.split(',')[0].strip()
                    if name and not name.startswith(','):
                        existing_names.add(name)
                        existing_lines.append(line)
                    # Skip old header (will be rewritten)
        except Exception:
            pass

    # Build output: header + existing entries + new entries
    lines = [header]
    # Keep existing entries that are NOT being replaced
    for line in existing_lines:
        name = line.split(',')[0].strip()
        if name not in sections_dict:
            lines.append(line)

    # Add new/updated entries
    for name, dims in sections_dict.items():
        lines.append(generate_lookup_entry(name, dims))

    with open(txt_path, 'w') as f:
        f.write('\r\n'.join(lines) + '\r\n')


# ═══════════════════════════════════════════════════════════════════
# ENGINE RUNNER
# ═══════════════════════════════════════════════════════════════════

#UNTUK_SAMBUNGAN_BAJA — Engine menghasilkan Result_New.json + Design_Result_New.json (section & gaya berubah → sambungan harus adapt)
def run_engine():
    """Invoke Autoselect Engine as a subprocess (Phase 1 + Phase 2)."""
    out.print_md("# ROIDA — AUTO-SELECT PENAMPANG")
    out.print_md("**Basis:** AISC 360-22 LRFD | DCR target 0.5 - 0.9")
    out.print_md("**Mode:** Group-based (Kolom, Balok Induk, Balok Anak)")

    # ── Prerequisite: Result.json (Create) ──
    if not os.path.exists(RESULT_JSON_PATH):
        out.print_md("> **ERROR:** Result.json tidak ditemukan.")
        out.print_md("> Path: `{}`".format(RESULT_JSON_PATH))
        out.print_md("> Jalankan **Create** terlebih dahulu.")
        return False
    try:
        with open(RESULT_JSON_PATH, "r") as _f:
            _rj = json.load(_f)
        if not isinstance(_rj, dict) or "model_data" not in _rj:
            out.print_md("> **ERROR:** Result.json tidak valid "
                         "(field 'model_data' tidak ditemukan).")
            out.print_md("> Jalankan **Create** ulang.")
            return False
    except (json.JSONDecodeError, IOError) as e:
        out.print_md("> **ERROR:** Result.json tidak bisa dibaca: {}".format(e))
        out.print_md("> Jalankan **Create** ulang.")
        return False

    # ── Prerequisite: Design Result.json (Steel Design Check) ──
    if not os.path.exists(DESIGN_RESULT_PATH):
        out.print_md("> **ERROR:** Design Result.json tidak ditemukan.")
        out.print_md("> Path: `{}`".format(DESIGN_RESULT_PATH))
        out.print_md("> Jalankan **Design Check** terlebih dahulu.")
        return False
    try:
        with open(DESIGN_RESULT_PATH, "r") as _f:
            _dr = json.load(_f)
        if not isinstance(_dr, dict) or "elements" not in _dr:
            out.print_md("> **ERROR:** Design Result.json tidak valid "
                         "(field 'elements' tidak ditemukan).")
            out.print_md("> Jalankan **Design Check** ulang.")
            return False
        # Design Result HARUS lebih baru dari Result.json
        dr_mtime = os.path.getmtime(DESIGN_RESULT_PATH)
        rj_mtime = os.path.getmtime(RESULT_JSON_PATH)
        if dr_mtime < rj_mtime:
            out.print_md("> **ERROR:** Design Result.json lebih lama "
                         "dari Result.json.")
            out.print_md("> Model telah di-Create ulang sejak Design Check "
                         "terakhir. Jalankan **Design Check** terlebih "
                         "dahulu.")
            return False
    except (json.JSONDecodeError, IOError) as e:
        out.print_md("> **ERROR:** Design Result.json tidak bisa dibaca: "
                     "{}".format(e))
        out.print_md("> Jalankan **Design Check** ulang.")
        return False

    if not os.path.exists(ENGINE_PATH):
        out.print_md("> **ERROR:** Autoselect Engine.py tidak ditemukan.")
        out.print_md("> Path: `{}`".format(ENGINE_PATH))
        return False

    out.print_md("Menjalankan Autoselect Engine (Phase 1 + Phase 2)...")
    out.print_md("- Python : `{}`".format(PYTHON_EXE))
    out.print_md("- Engine : `{}`".format(ENGINE_PATH))
    out.print_md("- Design Result: `{}`".format(DESIGN_RESULT_PATH))
    out.print_md("- Result.json  : `{}`".format(RESULT_JSON_PATH))
    out.print_md("- Output Dir   : `{}`".format(OUTPUT_DIR))

    # Catat waktu sebelum engine jalan — untuk verifikasi output baru
    _t_before = time.time()

    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        proc = subprocess.Popen(
            [PYTHON_EXE, ENGINE_PATH,
             DESIGN_RESULT_PATH, RESULT_JSON_PATH, OUTPUT_DIR],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )
        stdout_data, stderr_data = proc.communicate(timeout=600)

        if proc.returncode != 0:
            out.print_md("> **ERROR:** Autoselect Engine gagal (return code {})".format(
                proc.returncode))
            if stderr_data:
                print(stderr_data.decode("utf-8", errors="replace"))
            return False

        if stdout_data:
            print(stdout_data.decode("utf-8", errors="replace"))

        # ── Verifikasi output baru ──
        # Engine berhasil (returncode 0) tapi output harus benar-benar baru
        if not os.path.exists(AUTOSELECT_RESULT_PATH):
            out.print_md("> **ERROR:** Auto-Select Result.json tidak "
                         "ditemukan setelah engine selesai.")
            return False
        if os.path.getmtime(AUTOSELECT_RESULT_PATH) < _t_before:
            out.print_md("> **ERROR:** Auto-Select Result.json bukan hasil "
                         "run terbaru (file lama dari run sebelumnya).")
            out.print_md("> Engine mungkin gagal menulis output baru.")
            return False

        return True

    except subprocess.TimeoutExpired:
        out.print_md("> **ERROR:** Timeout (>600 detik)")
        return False
    except Exception as e:
        out.print_md("> **ERROR:** {}".format(str(e)))
        return False


# ═══════════════════════════════════════════════════════════════════
# REVIT REGENERATION — Duplicate FamilySymbol approach
# ═══════════════════════════════════════════════════════════════════

# Family names (must match the RFA family names loaded by Create)
BEAM_FAMILY_NAME = "M_W Shapes"
COL_FAMILY_NAME  = "M_W Shapes-Column"


def _search_symbol(doc, category, section_name):
    """Search for an existing FamilySymbol by exact name match."""
    collector = FilteredElementCollector(doc)\
        .OfClass(FamilySymbol)\
        .OfCategory(category)
    for sym in collector:
        try:
            if sym.Name == section_name:
                if not sym.IsActive:
                    sym.Activate()
                    doc.Regenerate()
                return sym
        except Exception:
            continue
    return None


def _find_template_symbol(doc, category, family_name):
    """Find an existing FamilySymbol from the correct family to use as template."""
    collector = FilteredElementCollector(doc)\
        .OfClass(FamilySymbol)\
        .OfCategory(category)
    for sym in collector:
        try:
            if sym.FamilyName == family_name:
                return sym
        except Exception:
            continue
    # Fallback: any symbol in this category
    return collector.FirstElement()


# ── Unit conversion: display → Revit internal (feet-based) ──
# Revit stores ALL values in Imperial internally regardless of project template.
# Proven approach from Create.pushbutton/script.py line 422: r_ft = r / 304.8
MM_TO_FT = 1.0 / 304.8

_PARAM_CONV = {
    # Length: mm → ft
    "Width":                          MM_TO_FT,
    "Height":                         MM_TO_FT,
    "Flange Thickness":               MM_TO_FT,
    "Web Thickness":                   MM_TO_FT,
    "Web Fillet":                      MM_TO_FT,
    "Clear Web Height":                MM_TO_FT,
    # Area: mm² → ft²
    "Section Area":                    MM_TO_FT ** 2,
    # Moment of inertia: mm⁴ → ft⁴
    "Moment of Inertia strong axis":   MM_TO_FT ** 4,
    "Moment of Inertia weak axis":     MM_TO_FT ** 4,
    "Torsional Moment of Inertia":     MM_TO_FT ** 4,
    # Section modulus: mm³ → ft³
    "Elastic Modulus strong axis":     MM_TO_FT ** 3,
    "Elastic Modulus weak axis":       MM_TO_FT ** 3,
    "Plastic Modulus strong axis":     MM_TO_FT ** 3,
    "Plastic Modulus weak axis":       MM_TO_FT ** 3,
    # Warping constant: mm⁶ → ft⁶
    "Warping Constant":                MM_TO_FT ** 6,
    # Weight per length: kgf/m → lbf/ft  (1 kgf=2.20462 lbf, 1 m=3.28084 ft)
    "Nominal Weight":                  2.20462 / 3.28084,
    # Perimeter (surface area/length): m → ft  (m²/m = m, ft²/ft = ft)
    "Perimeter":                       1.0 / 0.3048,
}


def _apply_section_params(sym, dims):
    """Set all type parameters on a duplicated FamilySymbol.

    Direct manual mm→ft conversion (no UnitUtils — proven unreliable in cpython3).
    """
    d  = float(dims["d"])
    bf = float(dims["bf"])
    tw = float(dims["tw"])
    tf = float(dims["tf"])
    r  = float(dims.get("r", 0))
    sp = calc_section_props_from_dims(d, bf, tw, tf)

    param_values = [
        ("Width",                          bf),
        ("Height",                         d),
        ("Flange Thickness",               tf),
        ("Web Thickness",                   tw),
        ("Web Fillet",                      r),
        ("Section Area",                    sp["Area"]),
        ("Nominal Weight",                  sp["Weight"]),
        ("Perimeter",                       sp["Perimeter"]),
        ("Moment of Inertia strong axis",   sp["Iz"]),
        ("Moment of Inertia weak axis",     sp["Iy"]),
        ("Elastic Modulus strong axis",     sp["Sz"]),
        ("Elastic Modulus weak axis",       sp["Sy"]),
        ("Plastic Modulus strong axis",     sp["Zz"]),
        ("Plastic Modulus weak axis",       sp["Zy"]),
        ("Torsional Moment of Inertia",     sp["J"]),
        ("Warping Constant",                sp["Cw"]),
        ("Clear Web Height",                sp["ClearWebHeight"]),
    ]

    ok_count = 0
    fail_info = []
    for name, display_val in param_values:
        p = sym.LookupParameter(name)
        if not p:
            fail_info.append(name + "(not found)")
            continue
        if p.IsReadOnly:
            fail_info.append(name + "(read-only)")
            continue
        if p.StorageType != StorageType.Double:
            fail_info.append(name + "(not Double)")
            continue
        conv = _PARAM_CONV.get(name, 1.0)
        internal_val = float(display_val) * conv
        try:
            p.Set(internal_val)
            ok_count += 1
        except Exception as e:
            fail_info.append("{}({})".format(name, e))

    out.print_md("  - Params: {}/{} OK{}".format(
        ok_count, len(param_values),
        " | FAIL: " + ", ".join(fail_info) if fail_info else ""))


DESIGN_PREFIX = "DESIGN_"


def find_or_create_symbol(doc, category, section_name, dims):
    """
    Find existing FamilySymbol or create by duplicating an existing one.
    Duplicated types use prefix 'DESIGN_' to distinguish from originals.
    e.g. "IWF400x200x8x13" → "DESIGN_IWF400x200x8x13"
    """
    design_name = DESIGN_PREFIX + section_name

    # 1. Check if DESIGN_ version already exists (from previous auto-select run)
    sym = _search_symbol(doc, category, design_name)
    if sym:
        out.print_md("- '{}' sudah ada — update parameters".format(design_name))
        _apply_section_params(sym, dims)
        doc.Regenerate()
        return sym

    # 2. Check if the exact section already exists in project (original)
    sym = _search_symbol(doc, category, section_name)
    if sym:
        return sym

    # 3. Not found — create by duplicating from the correct family
    family_name = COL_FAMILY_NAME if category == BuiltInCategory.OST_StructuralColumns \
        else BEAM_FAMILY_NAME
    template = _find_template_symbol(doc, category, family_name)
    if not template:
        out.print_md("> Tidak ada FamilySymbol dari family '{}' untuk di-duplicate".format(
            family_name))
        return None

    try:
        result = template.Duplicate(design_name)
        # Duplicate() returns ElementId (older API) or Element directly (newer API)
        if isinstance(result, ElementId):
            new_sym = doc.GetElement(result)
        else:
            new_sym = result
    except Exception as e:
        out.print_md("> Gagal duplicate '{}': {}".format(design_name, e))
        return None

    if not new_sym:
        return None

    # Set all dimensional and section properties
    _apply_section_params(new_sym, dims)

    if not new_sym.IsActive:
        new_sym.Activate()
    doc.Regenerate()

    out.print_md("- FamilySymbol '{}' dibuat via Duplicate".format(design_name))
    return new_sym


#UNTUK_SAMBUNGAN_BAJA — Konversi virtual ID kolom (id*1000+story) ke physical Revit ID — pattern sama dipakai di Steel Connection
def _to_revit_id(eid_int, is_column, composite_bases):
    """Convert analytical element_id to physical Revit element_id.

    Fabricated columns use virtual IDs: physical_id * 1000 + story_num.
    Returns the physical Revit ElementId int.
    """
    if is_column and eid_int // 1000 in composite_bases:
        return eid_int // 1000
    return eid_int


def _detect_composite_columns(elements_list):
    """Detect which column base IDs are composite (multi-story fabricated).

    Returns a set of base IDs (physical Revit IDs) that have multiple
    analytical entries.
    """
    base_count = {}
    for el in elements_list:
        eid = el.get("element_id")
        if eid is not None:
            base = eid // 1000
            base_count[base] = base_count.get(base, 0) + 1
    return {b for b, cnt in base_count.items() if cnt > 1}


def _copy_structural_material(doc, first_elem_info, new_sym,
                              composite_bases=None):
    """Copy STRUCTURAL_MATERIAL_PARAM from original element's type to new symbol."""
    eid_int = first_elem_info.get("element_id")
    if eid_int is None:
        return
    if composite_bases is None:
        composite_bases = set()
    revit_eid = _to_revit_id(eid_int, True, composite_bases)
    try:
        orig_elem = doc.GetElement(ElementId(int(revit_eid)))
        if not orig_elem:
            return
        orig_type = doc.GetElement(orig_elem.GetTypeId())
        if not orig_type:
            return
        p_src = orig_type.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
        if p_src and p_src.HasValue:
            mat_id = p_src.AsElementId()
            if mat_id and mat_id != ElementId.InvalidElementId:
                p_dst = new_sym.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                if p_dst and not p_dst.IsReadOnly:
                    p_dst.Set(mat_id)
    except Exception:
        pass


def _get_grp_kolom():
    """Read group name for kolom from Result.json."""
    try:
        with open(RESULT_JSON_PATH, "r") as f:
            data = json.load(f)
        return data.get("model_data", {}).get("group_names", {}).get("kolom", "Kolom")
    except Exception:
        return "Kolom"


def apply_autoselect_to_revit(doc, result):
    """
    Apply auto-select results to Revit model:
    1. Update lookup tables with new sections
    2. ChangeTypeId for each element per group
    """
    groups = result.get("groups", {})
    sections_needed = result.get("sections_needed", {})
    grp_kolom = _get_grp_kolom()

    if not groups or not sections_needed:
        out.print_md("> Tidak ada perubahan penampang untuk diterapkan.")
        return

    # Step 1: Update lookup tables
    out.print_md("### Memperbarui Lookup Tables...")
    col_sections = {}
    beam_sections = {}

    for g_name, g_data in groups.items():
        sec_name = g_data.get("recommended_section")
        if not sec_name or sec_name not in sections_needed:
            continue
        dims = sections_needed[sec_name]
        if g_name == grp_kolom:
            col_sections[sec_name] = dims
        else:
            beam_sections[sec_name] = dims

    if col_sections:
        update_lookup_table(COL_TXT_PATH, col_sections)
        out.print_md("- Column lookup: {} sections ditambahkan".format(len(col_sections)))

    if beam_sections:
        update_lookup_table(BEAM_TXT_PATH, beam_sections)
        out.print_md("- Beam lookup: {} sections ditambahkan".format(len(beam_sections)))

    # Step 2: Detect composite columns (fabricated multi-story → virtual IDs)
    kolom_data = groups.get(grp_kolom, {})
    kolom_elements = kolom_data.get("elements", [])
    composite_bases = _detect_composite_columns(kolom_elements)

    # Step 3: Create symbols + ChangeTypeId
    out.print_md("### Mengubah penampang elemen di Revit...")
    if composite_bases:
        out.print_md("- Detected {} composite column(s): {}".format(
            len(composite_bases),
            ", ".join(str(b) for b in sorted(composite_bases))))

    t = Transaction(doc, "ROIDA: Auto-Select ChangeTypeId")
    t.Start()
    try:
        changed = 0
        skipped = 0
        already_changed = set()  # Track physical IDs already processed

        for g_name, g_data in groups.items():
            sec_name = g_data.get("recommended_section")
            if not sec_name:
                continue

            dims = sections_needed.get(sec_name)
            if not dims:
                continue

            is_kolom = (g_name == grp_kolom)
            category = BuiltInCategory.OST_StructuralColumns \
                if is_kolom \
                else BuiltInCategory.OST_StructuralFraming

            new_sym = find_or_create_symbol(doc, category, sec_name, dims)
            if not new_sym:
                out.print_md("> **Peringatan:** FamilySymbol '{}' tidak ditemukan untuk group '{}'".format(
                    sec_name, g_name))
                continue

            # Copy structural material from original element type to new symbol
            elements_list = g_data.get("elements", [])
            if elements_list:
                _copy_structural_material(
                    doc, elements_list[0], new_sym,
                    composite_bases if is_kolom else None)

            out.print_md("- Group '{}': {} -> '{}' (Id={})".format(
                g_name, g_data.get("original_section", "?"),
                new_sym.Name, new_sym.Id.IntegerValue))

            for elem_info in elements_list:
                eid_int = elem_info.get("element_id")
                if eid_int is None:
                    continue

                # Map virtual column ID back to physical Revit ID
                revit_eid = _to_revit_id(
                    eid_int, is_kolom, composite_bases)

                # Skip if already changed (multi-story → same physical elem)
                if revit_eid in already_changed:
                    continue

                try:
                    elem_id = ElementId(int(revit_eid))
                    elem = doc.GetElement(elem_id)
                    if elem:
                        old_type_id = elem.GetTypeId()
                        elem.ChangeTypeId(new_sym.Id)
                        out.print_md("  - Elem {}: type {} -> {}".format(
                            revit_eid, old_type_id.IntegerValue,
                            new_sym.Id.IntegerValue))
                        changed += 1
                        already_changed.add(revit_eid)
                    else:
                        out.print_md("  - Elem {}: NOT FOUND in doc".format(
                            revit_eid))
                        skipped += 1
                except Exception as e:
                    out.print_md("  - Elem {}: ERROR {}".format(revit_eid, e))
                    skipped += 1

        # Regenerate to update Structural Analysis panel properties
        doc.Regenerate()

        # Verify: read back key dimensions from new symbols
        out.print_md("### Verifikasi Parameter")
        for g_name, g_data in groups.items():
            sec_name = g_data.get("recommended_section")
            if not sec_name:
                continue
            category = BuiltInCategory.OST_StructuralColumns \
                if g_name == grp_kolom \
                else BuiltInCategory.OST_StructuralFraming
            design_name = DESIGN_PREFIX + sec_name
            vsym = _search_symbol(doc, category, design_name)
            if not vsym:
                vsym = _search_symbol(doc, category, sec_name)
            if vsym:
                pw = vsym.LookupParameter("Width")
                ph = vsym.LookupParameter("Height")
                w_ft = pw.AsDouble() if pw else 0
                h_ft = ph.AsDouble() if ph else 0
                expected_dims = sections_needed.get(sec_name, {})
                out.print_md("- {}: Width={:.1f}mm (expect {}), Height={:.1f}mm (expect {})".format(
                    vsym.Name,
                    w_ft / MM_TO_FT, expected_dims.get("bf", "?"),
                    h_ft / MM_TO_FT, expected_dims.get("d", "?")))

        t.Commit()

        out.print_md("- **{}** elemen diubah penampangnya".format(changed))
        if skipped:
            out.print_md("- {} elemen dilewati".format(skipped))
        out.print_md("- Structural Analysis properties diperbarui")

    except Exception as e:
        t.RollBack()
        out.print_md("> **Error** ChangeTypeId: {}".format(str(e)))
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════
# DCR COLOR OVERRIDE (from Design_Result_New.json)
# ═══════════════════════════════════════════════════════════════════

def _dcr_color(dcr):
    """SAP2000-style color scheme for DCR."""
    if dcr <= 0.0:
        return Color(192, 192, 192)   # Gray
    if dcr <= 0.5:
        return Color(0, 255, 255)     # Cyan
    if dcr <= 0.7:
        return Color(0, 200, 0)       # Green
    if dcr <= 0.9:
        return Color(255, 255, 0)     # Yellow
    if dcr <= 1.0:
        return Color(255, 140, 0)     # Orange
    return Color(255, 0, 0)           # Red


def _get_solid_fill_id(doc):
    """Find the solid fill pattern ID."""
    collector = FilteredElementCollector(doc).OfClass(FillPatternElement)
    for pat_elem in collector:
        fp = pat_elem.GetFillPattern()
        if fp and fp.IsSolidFill:
            return pat_elem.Id
    return None


def _create_definitions(app, read_only=True):
    """Create ROIDA shared parameter definitions."""
    orig_spf = ""
    try:
        orig_spf = app.SharedParametersFilename
    except Exception:
        pass

    definitions = []
    try:
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


def _build_revit_map(elements):
    """Aggregate composite column virtual IDs to physical Revit IDs.

    For fabricated columns (virtual_id = physical_id * 1000 + story),
    keeps the entry with the worst DCR per physical element.
    Beams pass through unchanged.
    """
    col_base_count = {}
    for elem_data in elements:
        if elem_data.get("design_type", "") == "Column":
            eid = elem_data.get("element_id")
            if eid is not None:
                base = eid // 1000
                col_base_count[base] = col_base_count.get(base, 0) + 1
    composite_bases = {b for b, cnt in col_base_count.items() if cnt > 1}

    revit_map = {}
    for elem_data in elements:
        eid_int = elem_data.get("element_id")
        if eid_int is None:
            continue

        if (elem_data.get("design_type", "") == "Column"
                and eid_int // 1000 in composite_bases):
            revit_id = eid_int // 1000
        else:
            revit_id = eid_int

        cur = revit_map.get(revit_id)
        if cur is None:
            revit_map[revit_id] = elem_data
        else:
            cur_dcr = cur.get("governing_ratio", 0.0)
            new_dcr = elem_data.get("governing_ratio", 0.0)
            if new_dcr > cur_dcr:
                revit_map[revit_id] = elem_data

    return revit_map


def apply_dcr_to_revit(doc, design_result_path, read_only=False):
    """
    Apply DCR color override + shared parameters from a design result file.
    Uses Design_Result_New.json (post auto-select) instead of the original.
    """
    out.print_md("### Applying DCR Color Override")
    out.print_md("- Source: `{}`".format(design_result_path))

    if not os.path.exists(design_result_path):
        out.print_md("> **ERROR:** Design Result file tidak ditemukan!")
        return

    with open(design_result_path, 'r') as f:
        data = json.load(f)
    elements = data.get("elements", [])
    if not elements:
        out.print_md("> **ERROR:** Tidak ada elemen dalam Design Result!")
        return

    out.print_md("- Jumlah elemen dalam file: **{}**".format(len(elements)))

    app = HOST_APP.app
    active_view = doc.ActiveView
    out.print_md("- Active view: '{}' (type: {})".format(
        active_view.Name, active_view.ViewType))

    solid_fill_id = _get_solid_fill_id(doc)
    out.print_md("- Solid fill pattern: {}".format(
        "ID={}".format(solid_fill_id.IntegerValue) if solid_fill_id else "**NOT FOUND**"))

    # Create shared parameter definitions
    definitions = _create_definitions(app, read_only=read_only)
    out.print_md("- Shared parameter definitions: **{}** created".format(len(definitions)))

    t = Transaction(doc, "ROIDA: Apply DCR (Auto-Select)")
    t.Start()
    try:
        # Bind parameters
        params_ok = False
        if definitions:
            cats = app.Create.NewCategorySet()
            cats.Insert(doc.Settings.Categories.get_Item(
                BuiltInCategory.OST_StructuralColumns))
            cats.Insert(doc.Settings.Categories.get_Item(
                BuiltInCategory.OST_StructuralFraming))
            binding = app.Create.NewInstanceBinding(cats)

            # Remove old ROIDA_* bindings
            bm = doc.ParameterBindings
            it = bm.ForwardIterator()
            old_defs = []
            while it.MoveNext():
                d = it.Key
                if d.Name.startswith("ROIDA_"):
                    old_defs.append(d)
            for d in old_defs:
                bm.Remove(d)

            bind_ok = 0
            for defn in definitions:
                try:
                    doc.ParameterBindings.Insert(defn, binding, _PARAM_GROUP)
                    bind_ok += 1
                except Exception:
                    pass

            doc.Regenerate()
            params_ok = bind_ok > 0
            out.print_md("- Parameter bindings: {}/{} OK".format(bind_ok, len(definitions)))

        # Build revit_map: aggregate composite columns to worst DCR
        revit_map = _build_revit_map(elements)

        applied = 0
        skipped = 0
        color_applied = 0

        for revit_eid, elem_data in revit_map.items():
            try:
                elem_id = ElementId(int(revit_eid))
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
            label = elem_data.get("label_name", "")

            if params_ok:
                _set_param(elem, "ROIDA_DCR", "{:.4f}".format(dcr))
                _set_param(elem, "ROIDA_Status", status)
                _set_param(elem, "ROIDA_Combo", combo)
                _set_param(elem, "ROIDA_Equation", equation)
                _set_param(elem, "ROIDA_Label", label)

            color = _dcr_color(dcr)
            ogs = OverrideGraphicSettings()
            if solid_fill_id:
                ogs.SetSurfaceForegroundPatternId(solid_fill_id)
                ogs.SetSurfaceForegroundPatternColor(color)
                color_applied += 1
            active_view.SetElementOverrides(elem_id, ogs)

            applied += 1

        t.Commit()

        out.print_md("- **{}** elemen: override applied".format(applied))
        out.print_md("- **{}** elemen: color set".format(color_applied))
        if skipped:
            out.print_md("- {} elemen dilewati (tidak ditemukan di model)".format(skipped))

    except Exception as e:
        t.RollBack()
        out.print_md("> **Error** apply DCR: {}".format(str(e)))
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════
# DISPLAY RESULTS
# ═══════════════════════════════════════════════════════════════════

def display_results():
    """Read Auto-Select Result.json and display group-based tables."""
    if not os.path.exists(AUTOSELECT_RESULT_PATH):
        out.print_md("> **ERROR:** Auto-Select Result.json tidak ditemukan.")
        return

    with open(AUTOSELECT_RESULT_PATH, "r") as f:
        data = json.load(f)

    groups = data.get("groups", {})
    timestamp = data.get("timestamp", "")
    dcr_target = data.get("dcr_target", {})
    reanalysis = data.get("reanalysis_success", False)

    # Summary header
    out.print_md("## HASIL AUTO-SELECT")
    out.print_md("**Waktu:** {}".format(timestamp))
    out.print_md("**DCR Target:** {:.1f} - {:.1f}".format(
        dcr_target.get("min", 0.5), dcr_target.get("max", 0.9)))
    iterations = data.get("iterations", 1)
    out.print_md("**Re-analysis:** {} ({} iterasi)".format(
        "Berhasil" if reanalysis else "Gagal / Dilewati", iterations))

    # Group summary table
    group_rows = []
    for g_name, g_data in groups.items():
        dcr_v = g_data.get("max_dcr_verified")
        dcr_v_str = "{:.4f}".format(dcr_v) if dcr_v is not None else "N/A"
        status = g_data.get("status", "?")
        in_range = g_data.get("in_target_range", False)
        status_display = "OK" if status == "ok" and in_range else status.upper()

        group_rows.append([
            g_name,
            g_data.get("original_section", "?"),
            g_data.get("recommended_section") or "?",
            "{:.4f}".format(g_data.get("max_dcr_initial", 0)),
            dcr_v_str,
            str(g_data.get("element_count", 0)),
            status_display,
        ])

    print_table(
        group_rows,
        ["Group", "Section Asal", "Section Baru", "DCR Initial",
         "DCR Verified", "Elemen", "Status"],
        title="Ringkasan Per Group",
    )

    # Per-element detail per group
    for g_name, g_data in groups.items():
        elems = g_data.get("elements", [])
        elems_v = g_data.get("elements_verified", [])

        if elems:
            v_lookup = {}
            for ev in elems_v:
                v_lookup[ev.get("element_id")] = ev.get("dcr_verified", "N/A")

            elem_rows = []
            for e in elems:
                eid = e.get("element_id", 0)
                dcr_v_val = v_lookup.get(eid)
                dcr_v_display = "{:.4f}".format(dcr_v_val) \
                    if isinstance(dcr_v_val, (int, float)) else "N/A"
                elem_rows.append([
                    e.get("label_name", "?"),
                    e.get("design_type", "?"),
                    "{:.4f}".format(e.get("dcr_initial", 0)),
                    dcr_v_display,
                ])

            print_table(
                elem_rows,
                ["Label", "Tipe", "DCR Initial", "DCR Verified"],
                title="Detail: {} ({})".format(
                    g_name, g_data.get("recommended_section", "?")),
            )


def display_result_new():
    """Display contents of Result_New.json — model elements after section update."""
    out.print_md("---")
    out.print_md("## RESULT_NEW.JSON (Model Setelah Perubahan Section)")

    if not os.path.exists(RESULT_NEW_PATH):
        out.print_md("> File tidak ditemukan: `{}`".format(RESULT_NEW_PATH))
        return

    with open(RESULT_NEW_PATH, 'r') as f:
        data = json.load(f)

    # Grid system info
    model_data = data.get("model_data", {})
    grid = model_data.get("grid_system", {})
    out.print_md("**Grid:** X={}, Y={}, Lantai={}".format(
        grid.get("x_labels", []),
        grid.get("y_labels", []),
        grid.get("floor_labels", [])))

    # Model elements table
    elements = model_data.get("model_elements", [])
    out.print_md("**Jumlah elemen:** {}".format(len(elements)))

    if elements:
        elem_rows = []
        for el in elements:
            sp = el.get("section", el.get("section_properties", {}))
            elem_rows.append([
                el.get("frame_label", "?"),
                el.get("element_type", "?"),
                el.get("group", "?"),
                sp.get("section_name", "?"),
                "{:.0f}".format(sp.get("d_mm", 0)),
                "{:.0f}".format(sp.get("b_mm", 0)),
                "{:.1f}".format(sp.get("tw_mm", 0)),
                "{:.1f}".format(sp.get("tf_mm", 0)),
                "{:.0f}".format(sp.get("Area_mm2", 0)),
                "{:.0f}".format(sp.get("Iz_mm4", 0)),
            ])

        print_table(
            elem_rows,
            ["Label", "Type", "Group", "Section", "d(mm)",
             "bf(mm)", "tw(mm)", "tf(mm)", "A(mm2)", "Iz(mm4)"],
            title="Model Elements (Section Properties Baru)",
        )

    # Analysis results summary
    analysis = data.get("analysis_results", {})
    if analysis:
        combos = list(analysis.keys())
        out.print_md("**Analisis:** {} load combinations".format(len(combos)))
        out.print_md("Combinations: {}".format(", ".join(combos[:10])))
        if len(combos) > 10:
            out.print_md("... dan {} lainnya".format(len(combos) - 10))

        # Show sample forces for first few elements (SelfWeight)
        sw = analysis.get("SelfWeight", {})
        if sw:
            elem_forces = sw.get("element_forces", {})
            if elem_forces:
                force_rows = []
                for eid_str, forces_list in list(elem_forces.items())[:6]:
                    if isinstance(forces_list, list) and len(forces_list) > 0:
                        f0 = forces_list[0]
                        if isinstance(f0, dict):
                            force_rows.append([
                                eid_str,
                                "{:.2f}".format(f0.get("N", 0)),
                                "{:.2f}".format(f0.get("Vy", 0)),
                                "{:.2f}".format(f0.get("Vz", 0)),
                                "{:.2f}".format(f0.get("My", 0)),
                                "{:.2f}".format(f0.get("Mz", 0)),
                            ])
                        elif isinstance(f0, list) and len(f0) >= 6:
                            force_rows.append([
                                eid_str,
                                "{:.2f}".format(f0[0]),
                                "{:.2f}".format(f0[1]),
                                "{:.2f}".format(f0[2]),
                                "{:.2f}".format(f0[4] if len(f0) > 4 else 0),
                                "{:.2f}".format(f0[5] if len(f0) > 5 else 0),
                            ])
                if force_rows:
                    print_table(
                        force_rows,
                        ["Element", "N(kN)", "Vy(kN)", "Vz(kN)", "My(kNm)", "Mz(kNm)"],
                        title="Sample Forces (SelfWeight, Station 0)",
                    )
    else:
        out.print_md("> **Peringatan:** Tidak ada analysis_results dalam Result_New.json")


def display_design_result_new():
    """Display contents of Design_Result_New.json — DCR per element after re-analysis."""
    out.print_md("---")
    out.print_md("## DESIGN_RESULT_NEW.JSON (DCR Setelah Re-Analysis)")

    if not os.path.exists(DESIGN_RESULT_NEW_PATH):
        out.print_md("> File tidak ditemukan: `{}`".format(DESIGN_RESULT_NEW_PATH))
        return

    with open(DESIGN_RESULT_NEW_PATH, 'r') as f:
        data = json.load(f)

    # Design info
    di = data.get("design_info", {})
    out.print_md("**Code:** {} | **Method:** {} | **Analysis:** {}".format(
        di.get("code", "?"), di.get("method", "?"), di.get("analysis_method", "?")))

    # Summary
    summary = data.get("summary", {})
    out.print_md("**Total:** {} elemen | **Passed:** {} | **Failed:** {}".format(
        summary.get("total_elements", 0),
        summary.get("passed", 0),
        summary.get("failed", 0)))

    max_dcr_info = summary.get("max_DCR", {})
    if max_dcr_info:
        out.print_md("**Max DCR:** {:.4f} (Element {}, {})".format(
            max_dcr_info.get("DCR", 0),
            max_dcr_info.get("element_id", "?"),
            max_dcr_info.get("frame_label", "?")))

    # Per-element detail table
    elements = data.get("elements", [])
    if elements:
        elem_rows = []
        for el in elements:
            dcr = el.get("governing_ratio", 0)
            status = el.get("status", "?")
            pmm = el.get("pmm_detail", {})
            cap = el.get("capacity", {})

            elem_rows.append([
                el.get("label_name", "?"),
                el.get("frame_label", "?"),
                el.get("design_type", "?"),
                el.get("design_section", "?").split(":")[-1].strip(),
                "{:.4f}".format(dcr),
                status,
                el.get("governing_combo", "?"),
                pmm.get("equation", "?"),
            ])

        print_table(
            elem_rows,
            ["Label", "Frame", "Type", "Section", "DCR",
             "Status", "Gov.Combo", "Eq."],
            title="Detail DCR Per Elemen (Post Re-Analysis)",
        )

        # Capacity detail table
        cap_rows = []
        for el in elements:
            cap = el.get("capacity", {})
            pmm = el.get("pmm_detail", {})
            cap_rows.append([
                el.get("label_name", "?"),
                "{:.1f}".format(cap.get("phi_Pn_compression_kN", 0)),
                "{:.1f}".format(cap.get("phi_Pn_tension_kN", 0)),
                "{:.2f}".format(cap.get("phi_Mn_major_kNm", 0)),
                "{:.2f}".format(cap.get("phi_Mn_minor_kNm", 0)),
                "{:.1f}".format(cap.get("phi_Vn_major_kN", 0)),
                "{:.2f}".format(pmm.get("Pr_kN", 0)),
                "{:.2f}".format(pmm.get("MrMaj_kNm", 0)),
                "{:.2f}".format(pmm.get("MrMin_kNm", 0)),
            ])

        print_table(
            cap_rows,
            ["Label", "phiPn_C(kN)", "phiPn_T(kN)", "phiMn_Maj(kNm)",
             "phiMn_Min(kNm)", "phiVn(kN)", "Pr(kN)", "MrMaj(kNm)", "MrMin(kNm)"],
            title="Kapasitas vs Demand (Post Re-Analysis)",
        )

        # Group & K-factors if available
        kf_rows = []
        for el in elements:
            kf = el.get("K_factors", {})
            ln = el.get("lengths", {})
            mat = el.get("material", {})
            if kf or ln:
                kf_rows.append([
                    el.get("label_name", "?"),
                    el.get("group", "?"),
                    "{:.2f}".format(kf.get("Kx", 0)),
                    "{:.2f}".format(kf.get("Ky", 0)),
                    "{:.0f}".format(ln.get("Lx_mm", 0)),
                    "{:.0f}".format(ln.get("Ly_mm", 0)),
                    "{:.0f}".format(ln.get("Lb_mm", 0)),
                    "{:.0f}".format(mat.get("Fy_MPa", 0)),
                ])
        if kf_rows:
            print_table(
                kf_rows,
                ["Label", "Group", "Kx", "Ky", "Lx(mm)",
                 "Ly(mm)", "Lb(mm)", "Fy(MPa)"],
                title="K-Factors & Lengths (Post Re-Analysis)",
            )

    # Legenda warna
    legend = (
        "\n**Legenda Warna DCR (SAP2000 Convention):**\n\n"
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


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    # Run engine (Phase 1 + Phase 2)
    success = run_engine()

    if success:
        # Display auto-select summary
        display_results()

        # Display Result_New.json content
        display_result_new()

        # Display Design_Result_New.json content
        display_design_result_new()

        # Read Auto-Select Result for Revit operations
        if os.path.exists(AUTOSELECT_RESULT_PATH):
            with open(AUTOSELECT_RESULT_PATH, "r") as f:
                result_data = json.load(f)

            # Apply section changes to Revit model
            apply_autoselect_to_revit(revit.doc, result_data)

            # Apply DCR color override from Design_Result_New.json
            if result_data.get("reanalysis_success") and \
               os.path.exists(DESIGN_RESULT_NEW_PATH):
                apply_dcr_to_revit(revit.doc, DESIGN_RESULT_NEW_PATH,
                                   read_only=False)
            else:
                out.print_md("> DCR override dilewati (re-analysis tidak tersedia)")

        out.print_md("---")
        out.print_md("### Selesai")
        out.print_md("Re-analysis selesai, penampang dan visualisasi "
                     "telah diperbarui di Revit.")


main()
