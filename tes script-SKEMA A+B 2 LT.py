#! python3
import clr
import System # type: ignore
import json
import os
import math
import subprocess # LIBRARY PENTING UNTUK SUBPROCESS
import sys # TAMBAHAN: Import sys untuk exit script
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
from Autodesk.Revit.DB.Structure import StructuralType, StructuralFramingUtils
from Autodesk.Revit.UI import TaskDialog
from pyrevit import script, HOST_APP, revit # Added revit import

# Get active document (compatible with all pyRevit versions)
doc = HOST_APP.doc

# ================= INPUT PARAMETER (USER SETUP) =================
# 1. Lokasi Output JSON
OUTPUT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Model data.json"

# 2. KONFIGURASI SUBPROCESS (WAJIB DIGANTI SESUAI KOMPUTER ANDA)
#    Cari path python.exe (harus yang sudah install openseespy/numpy)
PYTHON_EXE_PATH = r"C:\\Users\\hp\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" 
#    Cari path script analisis python eksternal Anda
ANALYSIS_SCRIPT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Analysis\\Analysis.py"

# 2b. PATH OUTPUT GABUNGAN (Result.json)
MERGED_RESULT_PATH = os.path.join(os.path.dirname(OUTPUT_PATH), "Result.json")
ANALYSIS_JSON_PATH = os.path.join(os.path.dirname(OUTPUT_PATH), "Analysis", "Analysis.json")

# 3. Parameter Geometri & Beban
N_STORY     = 2       
BAY_X_COUNT = 2        
BAY_Y_COUNT = 2        
SPAN_X_MM   = 4000   
SPAN_Y_MM   = 4000    
HEIGHT_MM   = 4000     

# ================= FLOOR LOAD INPUT =================

# --- Input Parameters ---
SLAB_THICKNESS = 150.0        # Tebal slab beton (mm)
SLAB_ADD_THICKNESS = 30.0     # Tebal spesi/finishing (mm)

# --- Constants ---
CONCRETE_UNIT_WEIGHT_kN_m3 = 24.0    # Berat jenis beton bertulang (kN/m³)
MORTAR_WEIGHT_kg_m2_cm = 21.0        # Berat spesi per cm tebal (kg/m²/cm)
GRAVITY_m_s2 = 9.81                   # Percepatan gravitasi (m/s²)

# --- Hitung Pressure ---
slab_thickness_m = SLAB_THICKNESS / 1000.0
SW_kN_m2 = slab_thickness_m * CONCRETE_UNIT_WEIGHT_kN_m3
SLAB_SW_PRESSURE = round(SW_kN_m2 * 0.001, 5)  # MPa (N/mm²)

add_thickness_cm = SLAB_ADD_THICKNESS / 10.0
ADL_kg_m2 = add_thickness_cm * MORTAR_WEIGHT_kg_m2_cm
ADL_kN_m2 = ADL_kg_m2 * GRAVITY_m_s2 / 1000.0
SLAB_ADL_PRESSURE = round(ADL_kN_m2 * 0.001, 5)  # MPa (N/mm²)

LIVE_LOAD_PRESSURE = 0.024  # MPa (24 kN/m²)

# ================= LOAD PATTERNS (SAP2000-like) =================
# Setiap pattern memiliki:
#   "type"            : "Dead"|"Live"  (kategori beban)
#   "self_weight_mult": float          (multiplier berat sendiri elemen frame)
#   "pressure_MPa"   : float           (tekanan lantai tambahan)
#
# "SelfWeight" = beban mati total: berat sendiri frame (kolom+balok) + berat slab
# "ADL"        = beban mati tambahan (finishing/spesi), tanpa self-weight frame
# "LIVE"       = beban hidup
#
# NOTE: EQx/EQy otomatis di-generate dari seismic_parameters,
#       tidak perlu didefinisikan di sini.

LOAD_PATTERNS = {
    "SelfWeight": {
        "type": "Dead",
        "self_weight_mult": 1,       # Include berat sendiri frame
        "pressure_MPa": SLAB_SW_PRESSURE  # + beban slab
    },
    "ADL": {
        "type": "Dead",
        "self_weight_mult": 0,
        "pressure_MPa": SLAB_ADL_PRESSURE
    },
    "LIVE": {
        "type": "Live",
        "self_weight_mult": 0,
        "pressure_MPa": LIVE_LOAD_PRESSURE
    },
}

# ================= LOAD COMBINATIONS (SAP2000-like) =================
# Mode: "default" = 10 DSTL SNI 1727-2020 (built-in)
#        "custom"  = HANYA custom di bawah
#        "both"    = default + custom
LOAD_COMBO_MODE = "both"

# Custom combinations: {"NamaKombo": {"NamaPattern": factor, ...}}
# Pattern names harus match keys LOAD_PATTERNS atau "EQx"/"EQy"
CUSTOM_LOAD_COMBOS = {
    "COMB1": {"SelfWeight": 1.0, "ADL": 1.0, "LIVE": 1.0},
    "COMB2": {"SelfWeight": 1.5, "ADL": 1.5, "LIVE": 1.5},
    # "COMB3": {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 0.5, "EQx": 1.3},
}

# ============================================================


# ================= SEISMIC PARAMETERS (SNI 1726) =================
SITE_CLASS = "SC"         # Kelas situs: SA, SB, SC, SD, SE
SS  = 1.0821               # Percepatan respons spektral MCE_R (T=0.2s)
S1  = 0.4896               # Percepatan respons spektral MCE_R (T=1.0s)
TL  = 20                  # Periode transisi panjang (detik)
SDS = 0.87               # Parameter percepatan desain (short period)
SD1 = 0.49               # Parameter percepatan desain (1-detik)

# --- Waktu Getar Alami (Ta) ---
Ct   = 0.0724             # Koefisien tipe struktur (baja MRF)
x_Ta = 0.8                # Eksponen (baja MRF)

# --- Parameter Desain Seismik ---
Ie = 1.0                  # Faktor keutamaan gempa
R  = 8.0                  # Koefisien modifikasi respons (SRPMK)
Cd = 5.5                  # Faktor amplifikasi defleksi (SRPMK)

# ================= SEISMIC HELPER FUNCTIONS =================
VALID_SITE_CLASSES = ["SA", "SB", "SC", "SD", "SE"]

def validate_site_class(sc):
    """Deteksi dan validasi kelas situs dari string keyword."""
    sc_upper = str(sc).strip().upper()
    if sc_upper in VALID_SITE_CLASSES:
        return sc_upper
    raise ValueError("Kelas situs tidak valid: '{}'. Gunakan: {}".format(sc, VALID_SITE_CLASSES))

def get_Fa(site_class, Ss_val):
    """Koefisien situs Fa — Tabel 6 SNI 1726 (dengan interpolasi linier)."""
    sc = validate_site_class(site_class)
    ss_bp = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]
    table = {
        "SA": [0.8, 0.8, 0.8, 0.8, 0.8, 0.8],
        "SB": [0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
        "SC": [1.3, 1.3, 1.2, 1.2, 1.2, 1.2],
        "SD": [1.6, 1.4, 1.2, 1.1, 1.0, 1.0],
        "SE": [2.4, 1.7, 1.3, 1.1, 0.9, 0.8],
    }
    vals = table[sc]
    # Interpolasi linier jika nilai SS tidak tepat di breakpoint
    if Ss_val <= ss_bp[0]:  return vals[0]
    if Ss_val >= ss_bp[-1]: return vals[-1]
    for i in range(len(ss_bp)-1):
        if ss_bp[i] <= Ss_val <= ss_bp[i+1]:
            ratio = (Ss_val - ss_bp[i]) / (ss_bp[i+1] - ss_bp[i])
            return round(vals[i] + ratio * (vals[i+1] - vals[i]), 4)
    return vals[-1]

def get_Fv(site_class, S1_val):
    """Koefisien situs Fv — Tabel 7 SNI 1726 (dengan interpolasi linier)."""
    sc = validate_site_class(site_class)
    s1_bp = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60]
    table = {
        "SA": [0.8, 0.8, 0.8, 0.8, 0.8, 0.8],
        "SB": [0.8, 0.8, 0.8, 0.8, 0.8, 0.8],
        "SC": [1.5, 1.5, 1.5, 1.5, 1.5, 1.4],
        "SD": [2.4, 2.2, 2.0, 1.9, 1.8, 1.7],
        "SE": [4.2, 3.3, 2.8, 2.4, 2.2, 2.0],
    }
    vals = table[sc]
    # Interpolasi linier jika nilai S1 tidak tepat di breakpoint
    if S1_val <= s1_bp[0]:  return vals[0]
    if S1_val >= s1_bp[-1]: return vals[-1]
    for i in range(len(s1_bp)-1):
        if s1_bp[i] <= S1_val <= s1_bp[i+1]:
            ratio = (S1_val - s1_bp[i]) / (s1_bp[i+1] - s1_bp[i])
            return round(vals[i] + ratio * (vals[i+1] - vals[i]), 4)
    return vals[-1]

# --- Hitung variabel turunan ---
Fa = get_Fa(SITE_CLASS, SS)
Fv = get_Fv(SITE_CLASS, S1)
T0 = 0.2 * (SD1 / SDS)
Ts_period = SD1 / SDS
TOTAL_HEIGHT_M = N_STORY * HEIGHT_MM / 1000.0
Ta = Ct * (TOTAL_HEIGHT_M ** x_Ta)

# ============================================================

COLUMN_ROTATION_DEG = 0

# 4. Join Status untuk Structural Framing (Beam)
# True = Allow Join at ends, False = Disallow Join at ends
JOIN_STATUS = True  # Set False to disallow joins (untuk mencegah cut/extend otomatis)

# ================= SECTION DATABASE (SAP2000-LIKE) =================
# Naming: IWFdxbfxtwxtf (semua dimensi dalam mm)
SECTIONS = {
    "IWF303.4x165x6x10.2":   {"d": 303.4, "bf": 165, "tw": 6,  "tf": 10.2, "r": 8.9},
    "IWF307.9x305.3x9.9x15.4": {"d": 307.9, "bf": 305.3, "tw": 9.9,  "tf": 15.4, "r": 15.2},
}

# === SECTION ASSIGNMENT (Per Group) ===
SECTION_COL      = "IWF307.9x305.3x9.9x15.4"      # Kolom
SECTION_BEAM_EXT = "IWF303.4x165x6x10.2"    # Balok Eksterior (tepi grid)
SECTION_BEAM_INT = "IWF303.4x165x6x10.2"     # Balok Interior (dalam grid)

# ================= MATERIAL DATABASE (SAP2000-LIKE) =================
# Properties: Fy(MPa), Fu(MPa), E(MPa), Nu, Rho(kg/m³), thermal
MATERIALS = {
    "BJ 37": {"Fy": 240, "Fu": 370, "E": 200000, "Nu": 0.3, "Rho_kg_m3": 7850,
              "thermal_conductivity": 45.3, "specific_heat": 480},
    "BJ 41": {"Fy": 275, "Fu": 430, "E": 205000, "Nu": 0.3, "Rho_kg_m3": 7156.4437,
              "thermal_conductivity": 45.3, "specific_heat": 480},
    "BJ 50": {"Fy": 290, "Fu": 500, "E": 200000, "Nu": 0.3, "Rho_kg_m3": 7850,
              "thermal_conductivity": 45.3, "specific_heat": 480},
    "BJ 55": {"Fy": 410, "Fu": 550, "E": 200000, "Nu": 0.3, "Rho_kg_m3": 7850,
              "thermal_conductivity": 45.3, "specific_heat": 480},
}

# === MATERIAL ASSIGNMENT (Per Group) ===
MATERIAL_COL      = "BJ 41"
MATERIAL_BEAM_EXT = "BJ 41"
MATERIAL_BEAM_INT = "BJ 41"

# === LOOKUP TABLE PATHS ===
BEAM_TXT_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Steel\AISC 15.0\M_W Shapes.txt"
COL_TXT_PATH  = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Steel\AISC 15.0\M_W Shapes-Column.txt"

# === RFA FAMILY PATHS (untuk reload setelah txt diupdate) ===
BEAM_RFA_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Steel\AISC 15.0\M_W Shapes.rfa"
COL_RFA_PATH  = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Steel\AISC 15.0\M_W Shapes-Column.rfa"

# ===================================================
# FAMILY RELOAD (load sections dari .txt ke Revit)
# ===================================================
def reload_families(doc, beam_rfa, col_rfa):
    """Reload .rfa families dan update section parameters dari SECTIONS dict.
    Setelah LoadFamily, langsung set Web Fillet parameter pada FamilySymbol.
    """
    for rfa_path, label in [(beam_rfa, "Beam"), (col_rfa, "Column")]:
        if not os.path.exists(rfa_path):
            print("  ⚠️ RFA not found: {}".format(rfa_path))
            continue
        
        try:
            result = doc.LoadFamily(rfa_path)
            if result:
                print("  ✅ {} family loaded from: {}".format(label, os.path.basename(rfa_path)))
            else:
                print("  ℹ️ {} family already in project".format(label))
        except Exception as e:
            print("  ⚠️ {} family load error: {}".format(label, str(e)))
    
    # Update Web Fillet pada FamilySymbol yang sudah ada
    for section_name, dims in SECTIONS.items():
        r = dims.get("r", 0)
        if r <= 0:
            continue
        for sym in FilteredElementCollector(doc).OfClass(FamilySymbol):
            if sym.Name == section_name:
                try:
                    p = sym.LookupParameter("Web Fillet")
                    if p and not p.IsReadOnly:
                        r_ft = r / 304.8  # mm → ft (Revit internal)
                        p.Set(r_ft)
                        print("  ✅ '{}' Web Fillet set to {} mm".format(section_name, r))
                except Exception as e:
                    print("  ⚠️ '{}' Web Fillet error: {}".format(section_name, str(e)))
                break

# ===================================================
# HELPER & KONVERSI UNIT
# ===================================================
MM_TO_FT = 0.003280839895
FT_TO_MM = 304.8
SQFT_TO_SQMM = 92903.04
FT4_TO_MM4 = 8630974841.2416
FT3_TO_MM3 = 28316846.592
LB_FORCE_TO_N = 4.4482216
LB_MASS_TO_KG = 0.45359237

def mm_to_ft(mm): return float(mm) * MM_TO_FT
def ft2mm(v): return round(float(v) * FT_TO_MM, 2)
def sqft2sqmm(v): return round(float(v) * SQFT_TO_SQMM, 2)
def ft42mm4(v): return round(float(v) * FT4_TO_MM4, 2)
def ft32mm3(v): return round(float(v) * FT3_TO_MM3, 2)

def stress_psf_to_mpa(v): return round(float(v) * (LB_FORCE_TO_N / SQFT_TO_SQMM), 4)
def density_pcf_to_kgmm3(v): return float(v) * (LB_MASS_TO_KG / FT3_TO_MM3)

def find_structural_family(category, section_name):
    """Cari FamilySymbol berdasarkan exact match nama section."""
    collector = FilteredElementCollector(doc).OfCategory(category).OfClass(FamilySymbol)
    for sym in collector:
        if sym.Name == section_name:
            return sym
    # Fallback: return first available
    return collector.FirstElement()

# ===================================================
# SECTION PROPERTIES CALCULATOR (untuk lookup table)
# ===================================================
def calc_section_props_from_dims(d, bf, tw, tf):
    """
    Hitung semua section properties dari dimensi dasar I-section.
    Return dict dengan semua properties dalam mm.
    """
    r = 0.0  # No fillet for custom sections
    h_web = d - 2*tf
    
    # Area
    Area = 2*bf*tf + h_web*tw
    
    # Moment of Inertia
    Iz = (bf * d**3 - (bf - tw) * h_web**3) / 12.0
    Iy = (2 * tf * bf**3 + h_web * tw**3) / 12.0
    
    # Elastic Section Modulus
    Sz = Iz / (d / 2.0) if d > 0 else 0
    Sy = Iy / (bf / 2.0) if bf > 0 else 0
    
    # Plastic Section Modulus
    Zz = bf * tf * (d - tf) + tw * h_web**2 / 4.0
    Zy = (bf**2 * tf) / 2.0 + (h_web * tw**2) / 4.0
    
    # Torsional Constant — Euler finite-width rectangle correction
    # Formula: J = (1/3) * Σ(b*t³ * (1 - 0.63*t/b + 0.052*(t/b)⁵))
    # Exact match with SAP2000 (verified: beam error=0.00%, column error=0.05%)
    corr_f = 1 - 0.63*(tf/bf) + 0.052*(tf/bf)**5 if bf > 0 else 1
    corr_w = 1 - 0.63*(tw/h_web) + 0.052*(tw/h_web)**5 if h_web > 0 else 1
    J = (2 * bf * tf**3 * corr_f + h_web * tw**3 * corr_w) / 3.0
    
    # Warping Constant
    h0 = d - tf
    Cw = Iy * (h0**2) / 4.0
    
    # Nominal Weight (kg/m)
    Weight = Area * 7850 * 1e-6  # mm² × kg/m³ × 1e-6 = kg/m
    
    # Perimeter (m) — approximate
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
    """
    Generate satu baris CSV untuk Revit lookup table .txt.
    Format sesuai header M_W Shapes.txt.
    """
    d, bf, tw, tf = dims["d"], dims["bf"], dims["tw"], dims["tf"]
    r = dims.get("r", 0)
    sp = calc_section_props_from_dims(d, bf, tw, tf)
    
    # Format: Name,Width,Height,FlangeThck,WebThck,WebFillet,Area,Weight,
    #         Perimeter,Iz,Iy,Sz,Sy,Zz,Zy,J,Cw,ClearWebH,BoltSpacing,NameKey
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

def overwrite_lookup_table(txt_path, sections_dict):
    """
    Overwrite Revit lookup table dengan HANYA section custom.
    Deteksi duplikasi dari run sebelumnya.
    """
    header = ",Width##SECTION_PROPERTY##MILLIMETERS,Height##SECTION_PROPERTY##MILLIMETERS,Flange Thickness##SECTION_PROPERTY##MILLIMETERS,Web Thickness##SECTION_PROPERTY##MILLIMETERS,Web Fillet##SECTION_PROPERTY##MILLIMETERS,Section Area##SECTION_AREA##SQUARE_MILLIMETERS,Nominal Weight##WEIGHT_PER_UNIT_LENGTH##KILOGRAMS_FORCE_PER_METER,Perimeter##SURFACE_AREA##SQUARE_METERS_PER_METER,Moment of Inertia strong axis##MOMENT_OF_INERTIA##MILLIMETERS_TO_THE_FOURTH_POWER,Moment of Inertia weak axis##MOMENT_OF_INERTIA##MILLIMETERS_TO_THE_FOURTH_POWER,Elastic Modulus strong axis##SECTION_MODULUS##CUBIC_MILLIMETERS,Elastic Modulus weak axis##SECTION_MODULUS##CUBIC_MILLIMETERS,Plastic Modulus strong axis##SECTION_MODULUS##CUBIC_MILLIMETERS,Plastic Modulus weak axis##SECTION_MODULUS##CUBIC_MILLIMETERS,Torsional Moment of Inertia##MOMENT_OF_INERTIA##MILLIMETERS_TO_THE_FOURTH_POWER,Warping Constant##WARPING_CONSTANT##MILLIMETERS_TO_THE_SIXTH_POWER,Clear Web Height##SECTION_DIMENSION##MILLIMETERS,Bolt Spacing##SECTION_DIMENSION##MILLIMETERS,Section Name Key##other##"
    
    # Deteksi duplikasi dari run sebelumnya
    existing_names = set()
    try:
        with open(txt_path, 'r') as f:
            for line in f:
                name = line.split(',')[0].strip()
                if name and name != '':
                    existing_names.add(name)
    except FileNotFoundError:
        pass
    
    # Bangun content baru
    lines = [header]
    for name, dims in sections_dict.items():
        if name in existing_names:
            print("  ℹ️ Section '{}' sudah ada dari run sebelumnya — di-update".format(name))
        lines.append(generate_lookup_entry(name, dims))
    
    # Overwrite file
    with open(txt_path, 'w') as f:
        f.write('\r\n'.join(lines) + '\r\n')
    
    print("  ✅ Lookup table updated: {} ({} sections)".format(
        os.path.basename(txt_path), len(sections_dict)))

# ===================================================
# MATERIAL CREATION (Duplicate Steel + Modify)
# ===================================================

# Unit conversions for Revit internal units
def mpa_to_internal(mpa):
    """MPa → Revit internal pressure unit.
    Reverse of val2mpa: val / 304800 = MPa, so write = MPa * 304800.
    """
    return float(mpa) * 304800.0

def kg_m3_to_internal(kg_m3):
    """kg/m³ → Revit internal density (kg/ft³).
    Verified via RevitLookup: AsDouble=7156 → Display=252727 kg/m³.
    Factor = 252727/7156 = 35.3147 = 1/0.0283168 = m³/ft³.
    So internal = kg/m³ × 0.0283168 (= kg/m³ / 35.3147).
    """
    return float(kg_m3) * 0.0283168

def create_or_get_material(doc, mat_name, mat_props):
    """
    Replicate workflow manual user:
    1. Cek material ada + asset Steel → update → return
    2. Material ada tapi BUKAN Steel → delete → re-create
    3. Cari "Metal" material → duplicate → rename → set properties
    4. Fallback: cari ANY Steel/Metal material → duplicate
    """
    
    def _set_physical_props(mat, pse, props):
        """Set physical properties via CopyElement + get_Parameter.
        
        Approach yang terbukti berhasil 7/8 sebelumnya.
        Unit: MPa × 304800 (verified: 80000 MPa → AsDouble 24384000000).
        """
        PRESSURE_FACTOR = 304800.0
        
        E_int   = float(props["E"]) * PRESSURE_FACTOR
        Nu_val  = float(props["Nu"])
        G_int   = float(props["E"] / (2.0 * (1.0 + props["Nu"]))) * PRESSURE_FACTOR
        Rho_val = kg_m3_to_internal(props["Rho_kg_m3"])  # kg/m³ → internal (×0.0283168)
        Alpha   = 1.2e-5
        Fy_int  = float(props["Fy"]) * PRESSURE_FACTOR
        Fu_int  = float(props["Fu"]) * PRESSURE_FACTOR
        
        # Step 1: Copy PSE untuk mendapatkan editable copy
        try:
            copied_ids = ElementTransformUtils.CopyElement(doc, pse.Id, XYZ.Zero)
            new_pse_id = list(copied_ids)[0]
            new_pse = doc.GetElement(new_pse_id)
        except Exception as e:
            print("    ⚠️ CopyElement failed: {} — trying direct set".format(str(e)))
            new_pse = pse
            new_pse_id = pse.Id
        
        # Step 2: Set properties — try BOTH BIP name variants
        bip_pairs = [
            # (BIP tanpa '1', BIP dengan '1', value, label)
            ("PHY_MATERIAL_PARAM_YOUNG_MOD", "PHY_MATERIAL_PARAM_YOUNG_MOD1", E_int, "E"),
            ("PHY_MATERIAL_PARAM_POISSON_MOD", "PHY_MATERIAL_PARAM_POISSON_MOD1", Nu_val, "Nu"),
            ("PHY_MATERIAL_PARAM_SHEAR_MOD", "PHY_MATERIAL_PARAM_SHEAR_MOD1", G_int, "G"),
            ("PHY_MATERIAL_PARAM_STRUCTURAL_DENSITY", "PHY_MATERIAL_PARAM_UNIT_WEIGHT", Rho_val, "Rho"),
            ("PHY_MATERIAL_PARAM_EXP_COEFF1", "PHY_MATERIAL_PARAM_EXP_COEFF", Alpha, "Alpha"),
            ("PHY_MATERIAL_PARAM_MINIMUM_YIELD_STRESS", None, Fy_int, "Fy"),
            ("PHY_MATERIAL_PARAM_MINIMUM_TENSILE_STRENGTH", None, Fu_int, "Fu"),
        ]
        
        set_count = 0
        for bip_a, bip_b, value, label in bip_pairs:
            success = False
            # Try all BIP name variants
            names_to_try = [bip_a, bip_b] if bip_b else [bip_a]
            for bip_name in names_to_try:
                if success:
                    break
                try:
                    bip = getattr(BuiltInParameter, bip_name, None)
                    if bip is None:
                        print("    ⚠️ {} BIP '{}' not in enum".format(label, bip_name))
                        continue
                    p = new_pse.get_Parameter(bip)
                    if p is None:
                        print("    ⚠️ {} BIP '{}' param not found on PSE".format(label, bip_name))
                        continue
                    if p.IsReadOnly:
                        print("    ⚠️ {} BIP '{}' is read-only".format(label, bip_name))
                        continue
                    p.Set(float(value))
                    set_count += 1
                    success = True
                    print("    ✓ {} set via '{}' = {}".format(label, bip_name, value))
                except Exception as e:
                    print("    ⚠️ {} BIP '{}' error: {}".format(label, bip_name, str(e)))
            
            if not success:
                print("    ❌ {} — FAILED all attempts".format(label))
        
        # Step 3: Re-assign PSE ke material (jika di-copy)
        if new_pse_id != pse.Id:
            try:
                mat.StructuralAssetId = new_pse_id
                # Delete old PSE
                try:
                    doc.Delete(pse.Id)
                except:
                    pass
            except Exception as e:
                print("    ⚠️ Reassign failed: {}".format(str(e)))
        
        print("    ✅ Physical: {}/{} properties set".format(set_count, len(bip_pairs)))
    
    def _is_steel_asset(mat):
        """Cek apakah material punya StructuralAsset tipe Steel."""
        try:
            aid = mat.StructuralAssetId
            if aid == ElementId.InvalidElementId:
                return False
            ae = doc.GetElement(aid)
            if not ae:
                return False
            pt = ae.get_Parameter(BuiltInParameter.PHY_MATERIAL_PARAM_TYPE)
            return pt and pt.AsInteger() == 1
        except:
            return False
    
    def _dup_material(base, name):
        """Duplicate material, handle return type."""
        result = base.Duplicate(name)
        if isinstance(result, ElementId):
            return doc.GetElement(result)
        return result
    
    # --- STEP 1: Cek existing ---
    existing_mat = None
    for mat in FilteredElementCollector(doc).OfClass(Material):
        if mat.Name == mat_name:
            existing_mat = mat
            break
    
    if existing_mat:
        if _is_steel_asset(existing_mat):
            # Material ada + asset Steel → update properties saja
            asset_elem = doc.GetElement(existing_mat.StructuralAssetId)
            _set_physical_props(existing_mat, asset_elem, mat_props)
            print("  ✅ Material '{}' ada + Steel asset → updated".format(mat_name))
            return existing_mat
        else:
            # Material ada tapi BUKAN Steel → update properties jika ada asset
            try:
                aid = existing_mat.StructuralAssetId
                if aid != ElementId.InvalidElementId:
                    ae = doc.GetElement(aid)
                    if ae:
                        _set_physical_props(existing_mat, ae, mat_props)
                        print("  ✅ Material '{}' updated (non-Steel asset)".format(mat_name))
                    else:
                        print("  ℹ️ Material '{}' ada tanpa valid asset".format(mat_name))
                else:
                    print("  ℹ️ Material '{}' ada tanpa asset".format(mat_name))
            except Exception as e:
                print("  ⚠️ Material update error: {}".format(str(e)))
            return existing_mat
    
    # --- STEP 2: Cari base material "Metal" (by name) ---
    base_mat = None
    for mat in FilteredElementCollector(doc).OfClass(Material):
        if mat.Name == "Metal" and mat.StructuralAssetId != ElementId.InvalidElementId:
            base_mat = mat
            break
    
    # --- STEP 3: Fallback — cari material Steel by type ---
    if not base_mat:
        for mat in FilteredElementCollector(doc).OfClass(Material):
            if _is_steel_asset(mat):
                base_mat = mat
                break
    
    # --- STEP 4: Fallback — ANY material dengan StructuralAsset + MaterialClass Metal ---
    if not base_mat:
        for mat in FilteredElementCollector(doc).OfClass(Material):
            try:
                if mat.MaterialClass and "Metal" in mat.MaterialClass:
                    if mat.StructuralAssetId != ElementId.InvalidElementId:
                        base_mat = mat
                        break
            except:
                continue
    
    # --- STEP 5: Duplicate + modify ---
    if base_mat:
        try:
            new_mat = _dup_material(base_mat, mat_name)
            if new_mat:
                asset_id = new_mat.StructuralAssetId
                if asset_id != ElementId.InvalidElementId:
                    asset_elem = doc.GetElement(asset_id)
                    if asset_elem:
                        _set_physical_props(new_mat, asset_elem, mat_props)
                
                new_mat.MaterialClass = "Metal"
                print("  ✅ Material '{}' created (dup from '{}') — Fy={}MPa, E={}MPa".format(
                    mat_name, base_mat.Name, mat_props["Fy"], mat_props["E"]))
                return new_mat
        except Exception as e:
            print("  ⚠️ Duplication failed: {}".format(str(e)))
    
    # --- STEP 6: Last resort — bare material ---
    try:
        new_mat_id = Material.Create(doc, mat_name)
        if isinstance(new_mat_id, ElementId):
            new_mat = doc.GetElement(new_mat_id)
        else:
            new_mat = new_mat_id
        new_mat.MaterialClass = "Metal"
        print("  ⚠️ Material '{}' created WITHOUT asset (fallback mode)".format(mat_name))
        return new_mat
    except Exception as e:
        print("  ❌ Material creation failed: {}".format(str(e)))
        return None

# ===================================================
# SET PROJECT UNITS TO MM (Structural Discipline)
# ===================================================
def set_project_units_mm(doc):
    """Auto-set structural discipline units ke mm."""
    try:
        units = doc.GetUnits()
        count = 0
        
        # Revit 2024+ uses SpecTypeId / UnitTypeId (ForgeTypeId based)
        try:
            from Autodesk.Revit.DB import SpecTypeId, UnitTypeId
            
            spec_map = {
                SpecTypeId.SectionArea:     UnitTypeId.SquareMillimeters,
                SpecTypeId.SectionDimension: UnitTypeId.Millimeters,
                SpecTypeId.SectionProperty: UnitTypeId.MillimetersToTheFourthPower,
                SpecTypeId.SectionModulus:  UnitTypeId.CubicMillimeters,
            }
            
            for spec_id, unit_id in spec_map.items():
                try:
                    fo = units.GetFormatOptions(spec_id)
                    fo.SetUnitTypeId(unit_id)
                    units.SetFormatOptions(spec_id, fo)
                    count += 1
                except:
                    pass
            
            doc.SetUnits(units)
            print("  ✅ Project units set to mm ({} categories)".format(count))
            return
        except ImportError:
            pass
        
        # Fallback: Revit 2021 and earlier (UnitType / DisplayUnitType)
        try:
            unit_map = {
                UnitType.UT_Section_Area:      DisplayUnitType.DUT_SQUARE_MILLIMETERS,
                UnitType.UT_Section_Dimension: DisplayUnitType.DUT_MILLIMETERS,
                UnitType.UT_Section_Property:  DisplayUnitType.DUT_MILLIMETERS_TO_THE_FOURTH_POWER,
                UnitType.UT_Section_Modulus:   DisplayUnitType.DUT_CUBIC_MILLIMETERS,
            }
            
            for ut, dut in unit_map.items():
                fo = units.GetFormatOptions(ut)
                fo.DisplayUnits = dut
                units.SetFormatOptions(ut, fo)
                count += 1
            
            doc.SetUnits(units)
            print("  ✅ Project units set to mm ({} categories)".format(count))
        except:
            print("  ⚠️ Could not set units (neither new nor old API available)")
    except Exception as e:
        print("  ⚠️ Set units error: {}".format(str(e)))

# ===================================================
# DETERMINE GROUP (Column / Beam Exterior / Beam Interior)
# ===================================================
def determine_group(element):
    """Deteksi group berdasarkan element ID yang di-track saat creation."""
    try:
        el_id = element.Id.IntegerValue
        if el_id in _ELEMENT_GROUPS:
            return _ELEMENT_GROUPS[el_id]
    except:
        pass
    
    # Fallback: deteksi dari category + Symbol.Name
    try:
        cat_id = element.Category.Id.IntegerValue
        if cat_id == int(BuiltInCategory.OST_StructuralColumns):
            return "Column"
        else:
            sym_name = element.Symbol.Name
            if sym_name == SECTION_BEAM_EXT:
                return "Beam Exterior"
            elif sym_name == SECTION_BEAM_INT:
                return "Beam Interior"
            return "Beam"
    except:
        return "Unknown"

# Global dict untuk track group assignment saat creation
_ELEMENT_GROUPS = {}

def set_beam_alignment_safe(beam_element):
    try:
        p_just = beam_element.get_Parameter(BuiltInParameter.YZ_JUSTIFICATION)
        if p_just and not p_just.IsReadOnly: p_just.Set(3) 
    except: pass

def get_level_at_elevation(doc, target_elev):
    col = FilteredElementCollector(doc).OfClass(Level).ToElements()
    for lvl in col:
        if abs(lvl.Elevation - target_elev) < 0.005: 
            return lvl
    return None

def calculate_beam_distributed_load(start_node, end_node):
    # 1. Hitung Geometri Balok
    beam_length = math.sqrt((start_node[0] - end_node[0])**2 + (start_node[1] - end_node[1])**2)
    
    # 2. Tentukan Bentang Pelat
    Lx = min(SPAN_X_MM, SPAN_Y_MM) # Bentang Pendek
    Ly = max(SPAN_X_MM, SPAN_Y_MM) # Bentang Panjang
    
    # Cek Rasio One Way
    ratio_dim = Ly / Lx
    is_one_way = ratio_dim > 2.0
    
    # Identifikasi Balok Pendek/Panjang
    tol = 5.0
    is_short_span = abs(beam_length - Lx) < tol
    is_long_span  = abs(beam_length - Ly) < tol
    
    # 3. Deteksi Tepi (Edge) vs Tengah (Internal)
    #    Grid koordinat untuk 2x2 bay dengan span 4000mm:
    #    - Posisi X: -4000, 0, 4000
    #    - Posisi Y: -4000, 0, 4000
    #    - EDGE = balok di tepi luar grid (X = +/- 4000 atau Y = +/- 4000)
    #    - INTERIOR = balok di tengah grid (tapi bukan di tepi)
    
    mid_x = (start_node[0] + end_node[0]) / 2.0
    mid_y = (start_node[1] + end_node[1]) / 2.0
    
    # Batas luar grid (edge positions)
    edge_x = BAY_X_COUNT * SPAN_X_MM / 2.0  # = 4000 untuk 2x2 bay
    edge_y = BAY_Y_COUNT * SPAN_Y_MM / 2.0  # = 4000 untuk 2x2 bay
    edge_tol = 10.0
    
    # Determine beam direction and check if on edge
    is_edge = False
    
    # Balok berjalan arah X (Y konstan): cek posisi Y
    if abs(start_node[1] - end_node[1]) < edge_tol:  # Y constant
        y_pos = start_node[1]
        # Edge jika Y di batas luar (-edge_y atau +edge_y)
        if abs(abs(y_pos) - edge_y) < edge_tol:
            is_edge = True
    
    # Balok berjalan arah Y (X konstan): cek posisi X
    elif abs(start_node[0] - end_node[0]) < edge_tol:  # X constant
        x_pos = start_node[0]
        # Edge jika X di batas luar (-edge_x atau +edge_x)
        if abs(abs(x_pos) - edge_x) < edge_tol:
            is_edge = True

    # 4. Hitung Beban Puncak Distribusi untuk setiap tipe beban
    #    q_peak = Pressure (MPa) * Lebar Tributary Max (mm)
    
    if is_one_way and is_long_span:
        tributary_width = Lx / 2.0 # Persegi panjang setengah bentang
    elif not is_one_way:
        tributary_width = Lx / 2.0 # Puncak segitiga/trapesium selalu Lx/2
    else:
        tributary_width = 0.0 # Balok pendek pada One Way dianggap 0
    
    # LOGIKA BENAR:
    # - Jika balok INTERIOR (tengah), menanggung beban dari KEDUA sisi (x2)
    # - Jika balok EDGE (tepi), hanya menanggung dari SATU sisi (x1)
    multiplier = 2.0 if not is_edge else 1.0
    
    # Hitung q_peak untuk setiap tipe beban (N/mm)
    q_peak_sw = SLAB_SW_PRESSURE * tributary_width * multiplier    # Slab self-weight
    q_peak_adl = SLAB_ADL_PRESSURE * tributary_width * multiplier  # Finishing
    q_peak_ll = LIVE_LOAD_PRESSURE * tributary_width * multiplier  # Live load

    # 5. Konversi ke Beban Titik (Point Load) untuk setiap tipe
    #    Point Load (P) = Luas Area Diagram Beban (Total Force)
    
    def calc_point_load(q_peak, shape_type, beam_len, Lx):
        """Calculate point load based on load shape"""
        if q_peak <= 0:
            return 0.0
        if shape_type == "Rectangle":
            return q_peak * beam_len
        elif shape_type == "Triangle":
            return 0.5 * beam_len * q_peak
        elif shape_type == "Trapezoid":
            calc_len = beam_len if beam_len > Lx else Lx
            return q_peak * (calc_len - 0.5 * Lx)
        return 0.0
    
    # Determine load shape
    shape_type = "None"
    if tributary_width > 0:
        if is_one_way and is_long_span:
            shape_type = "Rectangle"
        elif not is_one_way and is_short_span:
            shape_type = "Triangle"
        elif not is_one_way:
            shape_type = "Trapezoid"
    
    # Calculate point loads for each type
    P_sw = calc_point_load(q_peak_sw, shape_type, beam_length, Lx)
    P_adl = calc_point_load(q_peak_adl, shape_type, beam_length, Lx)
    P_ll = calc_point_load(q_peak_ll, shape_type, beam_length, Lx)

    return {
        "load_shape": shape_type,
        "is_edge": is_edge,
        
        # Deadload (Slab Self-Weight)
        "deadload": {
            "pattern": "Deadload_Slab",
            "q_peak_Nmm": round(q_peak_sw, 4),
            "point_load_N": round(P_sw, 2),
            "location_ratio": 0.5
        },
        
        # Additional Dead Load (Finishing/Spesi)
        "additional_dl": {
            "pattern": "AdditionalDL_Finishing", 
            "q_peak_Nmm": round(q_peak_adl, 4),
            "point_load_N": round(P_adl, 2),
            "location_ratio": 0.5
        },
        
        # Live Load
        "liveload": {
            "pattern": "Liveload_Assign",
            "q_peak_Nmm": round(q_peak_ll, 4),
            "point_load_N": round(P_ll, 2),
            "location_ratio": 0.5
        }
    }
    
# ===================================================
# FUNGSI BANTUAN KONVERSI (HELPER)
# ===================================================
def val2mpa(val):
    """Convert Revit Internal Pressure (PSF) to MPa"""
    if val is None: return 0.0
    # 1 Kg/fts2 = 1.0/304800.0 MPa
    return round(val * 1.0 / 304800.0, 1)

def val2kgmm3(val):
    """Convert Revit Internal Density (PCF) to kg/mm3"""
    if val is None: return 0.0
    # 1 Kg/ft3 = 0.000000035315 kg/mm3
    return val * 0.000000001

def val2invC(val):
    """Convert Revit Internal Thermal (1/F) to 1/C"""
    if val is None: return 0.0
    # 1/F * 1.8 = 1/C
    return float("{:.2e}".format(val * 1.8))

# ===================================================
# LOGIKA UTAMA (Sesuai Referensi + Parameter Ekstra)
# ===================================================
def get_material_data(element, doc):
    mat_data = {} # No default values, strictly lookup

    try:
        # 1. Get Material directly from Member Instance
        mat_id = None
        p_mat = element.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
        if p_mat and p_mat.HasValue: 
            mat_id = p_mat.AsElementId()
        
        # Fallback to Type
        if not mat_id or mat_id == ElementId.InvalidElementId:
            elem_type = doc.GetElement(element.GetTypeId())
            if elem_type:
                p_mat_type = elem_type.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                if p_mat_type and p_mat_type.HasValue: 
                    mat_id = p_mat_type.AsElementId()
            
        if mat_id and mat_id != ElementId.InvalidElementId:
            mat_elem = doc.GetElement(mat_id)
            if mat_elem:
                mat_data["Name"] = mat_elem.Name
                
                # 2. Get Structural Asset from Material
                struc_asset_id = mat_elem.StructuralAssetId
                if struc_asset_id != ElementId.InvalidElementId:
                    pse = doc.GetElement(struc_asset_id)
                    if pse:
                        # Helper for Safe Param Lookup
                        def get_p(bip):
                            p = pse.get_Parameter(bip)
                            if p and p.HasValue: return p.AsDouble()
                            return 0.0

                        # PHY Params Lookup
                        mat_data["E_MPa"] = val2mpa(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_YOUNG_MOD1))
                        mat_data["Nu"]    = round(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_POISSON_MOD1), 3)
                        
                        # Calculate G from E and Nu: G = E / (2*(1+nu))
                        E_val = mat_data["E_MPa"]
                        Nu_val = mat_data["Nu"]
                        if Nu_val > -1.0:  # Avoid division by zero
                            mat_data["G_MPa"] = round(E_val / (2.0 * (1.0 + Nu_val)), 2)
                        else:
                            mat_data["G_MPa"] = val2mpa(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_SHEAR_MOD1))
                        
                        mat_data["Rho_kg/mm3"] = val2kgmm3(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_UNIT_WEIGHT))
                        mat_data["Alpha_C"] = val2invC(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_EXP_COEFF1))
                        
                        mat_data["Fy_MPa"] = val2mpa(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_MINIMUM_YIELD_STRESS))
                        mat_data["Fu_MPa"] = val2mpa(get_p(BuiltInParameter.PHY_MATERIAL_PARAM_MINIMUM_TENSILE_STRENGTH))
                    else:
                        if mat_elem.Name not in MATERIALS:
                            print(f"Warning: PropertySetElement is None for asset {struc_asset_id}")
                else:
                    if mat_elem.Name not in MATERIALS:
                        print(f"Warning: StructuralAssetId is Invalid for Material '{mat_elem.Name}'")

    except Exception as e:
        print(f"Material Error: {str(e)}")
    
    # === OVERRIDE: Gunakan nama material dari assignment berdasarkan group ===
    # Ini mengatasi kasus dimana Revit material param tidak berubah ke BJ 41
    elem_id = element.Id.IntegerValue
    if elem_id in _ELEMENT_GROUPS:
        grp = _ELEMENT_GROUPS[elem_id]
        if grp == "Column":
            assigned_mat = MATERIAL_COL
        elif grp == "Beam Exterior":
            assigned_mat = MATERIAL_BEAM_EXT
        else:
            assigned_mat = MATERIAL_BEAM_INT
        
        # Override name ke material yang benar
        mat_data["Name"] = assigned_mat
        
        # Populate properties dari MATERIALS dict jika ada
        if assigned_mat in MATERIALS:
            custom = MATERIALS[assigned_mat]
            mat_data["Fy_MPa"] = float(custom["Fy"])
            mat_data["Fu_MPa"] = float(custom["Fu"])
            mat_data["E_MPa"]  = float(custom["E"])
            mat_data["Nu"]     = float(custom["Nu"])
            mat_data["G_MPa"]  = round(custom["E"] / (2.0 * (1.0 + custom["Nu"])), 2)
            mat_data["Rho_kg/mm3"] = custom["Rho_kg_m3"] * 1e-9  # kg/m³ → kg/mm³
            mat_data["Alpha_C"] = 1.2e-5
    else:
        # Fallback lama: jika mat_name ada di MATERIALS dan Fy belum di-set
        mat_name = mat_data.get("Name", "")
        if mat_name in MATERIALS and not mat_data.get("Fy_MPa"):
            custom = MATERIALS[mat_name]
            mat_data["Fy_MPa"] = float(custom["Fy"])
            mat_data["Fu_MPa"] = float(custom["Fu"])
            mat_data["E_MPa"]  = float(custom["E"])
            mat_data["Nu"]     = float(custom["Nu"])
            mat_data["G_MPa"]  = round(custom["E"] / (2.0 * (1.0 + custom["Nu"])), 2)
            mat_data["Rho_kg/mm3"] = custom["Rho_kg_m3"] * 1e-9
            mat_data["Alpha_C"] = 1.2e-5
    
    return mat_data

def get_section_properties(element, doc):
    """
    Extract section geometric parameters and calculate section properties manually.
    For I-sections (Wide Flange, UC, UB, etc.):
    - Extracts: d, b, tf, tw, r (web fillet), centroids
    - Calculates: Area, Iz, Iy, Zz, Zy, Sz, Sy, J, Cw
    """
    props = {
        "Area_mm2": 0.0, "d_mm": 0.0, "b_mm": 0.0, "tf_mm": 0.0, "tw_mm": 0.0,
        "r_mm": 0.0,  # Web fillet radius
        "cx_mm": 0.0, "cy_mm": 0.0,  # Centroids
        "Iz_mm4": 0.0, "Iy_mm4": 0.0, 
        "Zz_mm3": 0.0, "Zy_mm3": 0.0,  # Plastic modulus
        "Sz_mm3": 0.0, "Sy_mm3": 0.0,  # Elastic modulus
        "J_mm4": 0.0, "Cw_mm6": 0.0,   # Torsion constant, Warping constant
        "Avz_mm2": 0.0, "Avy_mm2": 0.0, # Shear Areas
        "rz_mm": 0.0, "ry_mm": 0.0      # Radii of Gyration
    }
    try:
        # === CUSTOM SECTION: Gunakan dimensi dari SECTIONS dict ===
        # Cari section name via: (1) Symbol.Name match, (2) _ELEMENT_GROUPS tracking
        custom_section_name = None
        try:
            sym_name = element.Symbol.Name
            if sym_name in SECTIONS:
                custom_section_name = sym_name
        except:
            pass
        
        if not custom_section_name:
            try:
                el_id = element.Id.IntegerValue
                group = _ELEMENT_GROUPS.get(el_id, "")
                if group == "Column":
                    custom_section_name = SECTION_COL
                elif group == "Beam Exterior":
                    custom_section_name = SECTION_BEAM_EXT
                elif group == "Beam Interior":
                    custom_section_name = SECTION_BEAM_INT
            except:
                pass
        
        if custom_section_name and custom_section_name in SECTIONS:
            dims = SECTIONS[custom_section_name]
            d, bf, tw, tf = dims["d"], dims["bf"], dims["tw"], dims["tf"]
            r = dims.get("r", 0)
            sp = calc_section_props_from_dims(d, bf, tw, tf)
            
            props["d_mm"] = d
            props["b_mm"] = bf
            props["tf_mm"] = tf
            props["tw_mm"] = tw
            props["r_mm"] = r
            props["Area_mm2"] = sp["Area"]
            props["Iz_mm4"] = sp["Iz"]
            props["Iy_mm4"] = sp["Iy"]
            props["Sz_mm3"] = sp["Sz"]
            props["Sy_mm3"] = sp["Sy"]
            props["Zz_mm3"] = sp["Zz"]
            props["Zy_mm3"] = sp["Zy"]
            props["J_mm4"] = sp["J"]
            props["Cw_mm6"] = sp["Cw"]
            
            h_web = d - 2*tf
            props["Avz_mm2"] = round(d * tw, 2)
            props["Avy_mm2"] = round(2 * bf * tf * (5.0/6.0), 4)
            if sp["Area"] > 0:
                props["rz_mm"] = round(math.sqrt(sp["Iz"] / sp["Area"]), 4)
                props["ry_mm"] = round(math.sqrt(sp["Iy"] / sp["Area"]), 4)
            props["cx_mm"] = round(bf / 2.0, 2)
            props["cy_mm"] = round(d / 2.0, 2)
            
            # Round computed properties to reasonable precision, keep input dims exact
            exact_keys = {"d_mm", "b_mm", "tf_mm", "tw_mm", "r_mm"}
            for k in props:
                if isinstance(props[k], float):
                    if k in exact_keys:
                        props[k] = round(props[k], 2)  # keep decimal precision
                    else:
                        props[k] = round(props[k], 2)  # round to 2 dp instead of int
            return props
        
        elem_type = doc.GetElement(element.GetTypeId())
        if not elem_type: return props

        # Use StructuralSection API if available (Revit 2022+)
        section = None
        if hasattr(elem_type, "GetStructuralSection"):
            section = elem_type.GetStructuralSection()
        
        if section:
            # =========================================================
            # 1. EXTRACT GEOMETRIC PARAMETERS
            # =========================================================
            # Basic dimensions
            try: props["d_mm"] = round(ft2mm(section.Height), 2)
            except: pass
            try: props["b_mm"] = round(ft2mm(section.Width), 2)
            except: pass
            try: props["tf_mm"] = round(ft2mm(section.FlangeThickness), 2)
            except: pass
            try: props["tw_mm"] = round(ft2mm(section.WebThickness), 2)
            except: pass
            
            # Web Fillet (r) - using get_WebFillet() method
            try: 
                r_ft = section.get_WebFillet()
                props["r_mm"] = round(ft2mm(r_ft), 2)
            except: 
                props["r_mm"] = 0.0
            
            # Centroid Horizontal (cx) - from centroid to edge
            try:
                cx_ft = section.get_CentroidHorizontal()
                props["cx_mm"] = round(ft2mm(cx_ft), 2)
            except:
                # Default: b/2 for symmetric I-section
                props["cx_mm"] = round(props["b_mm"] / 2.0, 2) if props["b_mm"] > 0 else 0.0
            
            # Centroid Vertical (cy) - from centroid to edge  
            try:
                cy_ft = section.get_CentroidVertical()
                props["cy_mm"] = round(ft2mm(cy_ft), 2)
            except:
                # Default: d/2 for symmetric I-section
                props["cy_mm"] = round(props["d_mm"] / 2.0, 2) if props["d_mm"] > 0 else 0.0
            
            # Get Area from API first, will recalculate if needed
            try: props["Area_mm2"] = round(sqft2sqmm(section.SectionArea), 2)
            except: pass
            
            # =========================================================
            # 2. MANUAL CALCULATION OF SECTION PROPERTIES
            # =========================================================
            d = props["d_mm"]    # Total depth
            b = props["b_mm"]    # Flange width
            tf = props["tf_mm"]  # Flange thickness
            tw = props["tw_mm"]  # Web thickness
            r = props["r_mm"]    # Web fillet radius
            
            if d > 0 and b > 0 and tf > 0 and tw > 0:
                # -------------------------------------------------------
                # AREA CALCULATION (SAP2000 compatible - no fillet)
                # A = 2*b*tf + (d-2*tf)*tw
                # -------------------------------------------------------
                h_web = d - 2*tf  # Clear height of web
                A_flanges = 2 * b * tf
                A_web = h_web * tw
                Area_calc = A_flanges + A_web
                
                # Use calculated area (SAP2000 compatible)
                props["Area_mm2"] = round(Area_calc, 2)
                
                # -------------------------------------------------------
                # MOMENT OF INERTIA - STRONG AXIS (Iz) - SAP2000 Compatible
                # Iz = (b*d^3 - (b-tw)*(d-2*tf)^3) / 12
                # -------------------------------------------------------
                Iz = (b * d**3 - (b - tw) * h_web**3) / 12.0
                props["Iz_mm4"] = round(Iz, 0)
                
                # -------------------------------------------------------
                # MOMENT OF INERTIA - WEAK AXIS (Iy) - SAP2000 Compatible
                # Iy = (2*tf*b^3 + (d-2*tf)*tw^3) / 12
                # -------------------------------------------------------
                Iy = (2 * tf * b**3 + h_web * tw**3) / 12.0
                props["Iy_mm4"] = round(Iy, 0)
                
                # -------------------------------------------------------
                # ELASTIC SECTION MODULUS (Sz, Sy)
                # Sz = Iz / (d/2) = 2*Iz/d
                # Sy = Iy / (b/2) = 2*Iy/b
                # -------------------------------------------------------
                if d > 0:
                    props["Sz_mm3"] = round(props["Iz_mm4"] / (d / 2.0), 0)
                if b > 0:
                    props["Sy_mm3"] = round(props["Iy_mm4"] / (b / 2.0), 0)
                
                # -------------------------------------------------------
                # PLASTIC SECTION MODULUS (Zz, Zy)
                # For I-section:
                # Zz = b*tf*(d-tf) + tw*(d-2*tf)^2/4
                # Zy = b^2*tf/2 + (d-2*tf)*tw^2/4
                # -------------------------------------------------------
                Zz = b * tf * (d - tf) + tw * h_web**2 / 4.0
                props["Zz_mm3"] = round(Zz, 0)
                
                Zy = (b**2 * tf) / 2.0 + (h_web * tw**2) / 4.0
                props["Zy_mm3"] = round(Zy, 0)
                
                # -------------------------------------------------------
                # TORSIONAL CONSTANT (J) - SAP2000 Compatible
                # J = K * (2*b*tf^3 + (d-2*tf)*tw^3) / 3
                # K = 0.9668 is a correction factor for web-flange junction
                # Based on SAP2000 reference: J = beta * a^3 * b for rectangles
                # -------------------------------------------------------
                J_RAW_FACTOR = 0.9668  # SAP2000 correction factor
                J = J_RAW_FACTOR * (2 * b * tf**3 + h_web * tw**3) / 3.0
                props["J_mm4"] = round(J, 0)
                
                # -------------------------------------------------------
                # Warping Constant Cw
                # For doubly symmetric I-section:
                # Cw = Iy * ((d - tf)^2) / 4
                # -------------------------------------------------------
                h0 = d - tf  # Distance between flange centroids
                Cw = props["Iy_mm4"] * (h0**2) / 4.0
                props["Cw_mm6"] = round(Cw, 0)

                # -------------------------------------------------------
                # SHEAR AREAS (Avz, Avy) - SAP2000 Compatible
                # Avz (Shear in 2 direction) = d * tw
                # Avy (Shear in 3 direction) = 2 * b * tf * (5/6)
                # -------------------------------------------------------
                props["Avz_mm2"] = round(d * tw, 2)
                props["Avy_mm2"] = round(2 * b * tf * (5.0/6.0), 4)

                # -------------------------------------------------------
                # RADIUS OF GYRATION (rz, ry)
                # rz = sqrt(Iz / A)
                # ry = sqrt(Iy / A)
                # -------------------------------------------------------
                if props["Area_mm2"] > 0:
                    props["rz_mm"] = round(math.sqrt(props["Iz_mm4"] / props["Area_mm2"]), 4)
                    props["ry_mm"] = round(math.sqrt(props["Iy_mm4"] / props["Area_mm2"]), 4)
            
            return props

        # =========================================================
        # FALLBACK: BuiltInParameters (legacy Revit versions)
        # =========================================================
        def find_val(bip_list, str_list):
            for name in bip_list:
                if hasattr(BuiltInParameter, name):
                    p = elem_type.get_Parameter(getattr(BuiltInParameter, name))
                    if p and p.HasValue and p.StorageType == StorageType.Double: return p.AsDouble()
            for s in str_list:
                p = elem_type.LookupParameter(s)
                if p and p.HasValue and p.StorageType == StorageType.Double: return p.AsDouble()
            return 0.0

        # Extract geometric parameters
        props["d_mm"]     = ft2mm(find_val(["STRUCTURAL_SECTION_DEPTH", "FAMILY_HEIGHT_PARAM"], ["Height", "Depth", "d", "h"]))
        props["b_mm"]     = ft2mm(find_val(["STRUCTURAL_SECTION_WIDTH", "FAMILY_WIDTH_PARAM"], ["Width", "b"]))
        props["tf_mm"]    = ft2mm(find_val(["STRUCTURAL_SECTION_FLANGE_THICKNESS"], ["Flange Thickness", "tf"]))
        props["tw_mm"]    = ft2mm(find_val(["STRUCTURAL_SECTION_WEB_THICKNESS"], ["Web Thickness", "tw"]))
        props["r_mm"]     = ft2mm(find_val([], ["r", "Web Fillet", "Fillet Radius"]))
        props["Area_mm2"] = sqft2sqmm(find_val(["STRUCTURAL_SECTION_AREA"], ["Section Area", "Area"]))
        
        # Calculate section properties manually using extracted geometry
        d = props["d_mm"]
        b = props["b_mm"]
        tf = props["tf_mm"]
        tw = props["tw_mm"]
        r = props["r_mm"]
        
        if d > 0 and b > 0 and tf > 0 and tw > 0:
            h_web = d - 2*tf
            
            # Area
            if props["Area_mm2"] <= 0:
                A_fillets = (4 - math.pi) * r * r if r > 0 else 0.0
                props["Area_mm2"] = round(2*b*tf + h_web*tw + A_fillets, 2)
            
            # Iz
            Iz = (b * d**3 - (b - tw) * h_web**3) / 12.0
            props["Iz_mm4"] = round(Iz, 0)
            
            # Iy
            Iy = (2 * tf * b**3 + h_web * tw**3) / 12.0
            props["Iy_mm4"] = round(Iy, 0)
            
            # Elastic modulus
            if d > 0: props["Sz_mm3"] = round(Iz / (d/2), 0)
            if b > 0: props["Sy_mm3"] = round(Iy / (b/2), 0)
            
            # Plastic modulus
            props["Zz_mm3"] = round(b*tf*(d-tf) + tw*h_web**2/4, 0)
            props["Zy_mm3"] = round(b**2*tf/2 + h_web*tw**2/4, 0)
            
            # Torsional constant (SAP2000 compatible with K factor)
            J_RAW_FACTOR = 0.9668
            props["J_mm4"] = round(J_RAW_FACTOR * (2*b*tf**3 + h_web*tw**3) / 3.0, 0)
            
            # Warping constant
            h0 = d - tf
            props["Cw_mm6"] = round(Iy * h0**2 / 4.0, 0)
            
            # -------------------------------------------------------
            # SHEAR AREAS (Avz, Avy) - SAP2000 Compatible
            # Avz (Strong Axis Shear) = d * tw
            # Avy (Weak Axis Shear) = 2 * b * tf * (5/6)
            # -------------------------------------------------------
            props["Avz_mm2"] = round(d * tw, 2)
            props["Avy_mm2"] = round(2 * b * tf * (5.0/6.0), 4)

            # -------------------------------------------------------
            # RADIUS OF GYRATION (rz, ry)
            # rz = sqrt(Iz / A)
            # ry = sqrt(Iy / A)
            # -------------------------------------------------------
            if props["Area_mm2"] > 0:
                props["rz_mm"] = round(math.sqrt(props["Iz_mm4"] / props["Area_mm2"]), 4)
                props["ry_mm"] = round(math.sqrt(props["Iy_mm4"] / props["Area_mm2"]), 4)
            
            # Set default centroids for symmetric section
            props["cx_mm"] = round(b / 2.0, 2)
            props["cy_mm"] = round(d / 2.0, 2)

        # Round all values to integers
        for k in props: 
            if isinstance(props[k], float): 
                props[k] = int(round(props[k]))
                
    except: pass
    return props

def get_topology_ref(element, doc):
    # Default values sebagai Integer
    topo = {"start_node": [0, 0, 0], "end_node": [0, 0, 0], "length_mm": 0}
    
    # Helper lokal untuk konversi ke mm lalu bulat ke Integer terdekat
    def to_int_mm(val_ft):
        # 304.8 adalah konversi kaki ke mm
        return int(round(val_ft * 304.8))

    try:
        cat_id = element.Category.Id.IntegerValue
        
        # --- LOGIKA BALOK (STRUCTURAL FRAMING) ---
        if cat_id == int(BuiltInCategory.OST_StructuralFraming):
            loc = element.Location
            if isinstance(loc, LocationCurve):
                c = loc.Curve
                p0 = c.GetEndPoint(0)
                p1 = c.GetEndPoint(1)
                
                topo["start_node"] = [to_int_mm(p0.X), to_int_mm(p0.Y), to_int_mm(p0.Z)]
                topo["end_node"]   = [to_int_mm(p1.X), to_int_mm(p1.Y), to_int_mm(p1.Z)]
                topo["length_mm"]  = to_int_mm(c.Length)

        # --- LOGIKA KOLOM (STRUCTURAL COLUMNS) ---
        elif cat_id == int(BuiltInCategory.OST_StructuralColumns):
            # Ambil titik lokasi XY
            pt_xy = None
            if isinstance(element.Location, LocationCurve):
                pt_xy = element.Location.Curve.GetEndPoint(0)
            elif isinstance(element.Location, LocationPoint):
                pt_xy = element.Location.Point
            
            if pt_xy:
                # Ambil Level Bawah & Atas untuk Z
                z_s, z_e = 0.0, 0.0
                
                # Base Level
                p_base = element.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
                p_base_off = element.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
                if p_base:
                    lvl = doc.GetElement(p_base.AsElementId())
                    if lvl: z_s = lvl.Elevation
                if p_base_off and p_base_off.HasValue:
                    z_s += p_base_off.AsDouble()

                # Top Level
                p_top = element.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
                p_top_off = element.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
                if p_top:
                    lvl = doc.GetElement(p_top.AsElementId())
                    if lvl: z_e = lvl.Elevation
                if p_top_off and p_top_off.HasValue:
                    z_e += p_top_off.AsDouble()

                # Set Topology (Z diambil dari level, XY dari location point)
                topo["start_node"] = [to_int_mm(pt_xy.X), to_int_mm(pt_xy.Y), to_int_mm(z_s)]
                topo["end_node"]   = [to_int_mm(pt_xy.X), to_int_mm(pt_xy.Y), to_int_mm(z_e)]
                
                # Hitung panjang dari selisih Z
                topo["length_mm"]  = abs(to_int_mm(z_e) - to_int_mm(z_s))

    except Exception as e:
        # Jika error, biarkan nilai default (0,0,0)
        pass
        
    return topo

# ===================================================
# LOCAL AXIS
# ===================================================

def get_local_axes(element, doc):
    """
    Extract/Construct local coordinate system.
    For Columns: Uses COLUMN_ROTATION_DEG to manually construct rotated axes (Right-Hand Rule).
    For Beams: Extract from element Transform.
    """
    local_axes = {
        "x_axis": [1.0, 0.0, 0.0],
        "y_axis": [0.0, 1.0, 0.0],
        "z_axis": [0.0, 0.0, 1.0],
        #"rotation_angle_deg": 0.0
    }
    
    try:
        cat_id = element.Category.Id.IntegerValue
        
        if cat_id == int(BuiltInCategory.OST_StructuralFraming):
            # BEAM: Extract from Transform
            transform = element.GetTransform()
            
            # Basis vectors (Original from Revit)
            # Revit Beam: X=Longitudinal, Y=Horizontal(Width), Z=Vertical(Depth)
            orig_x = [transform.BasisX.X, transform.BasisX.Y, transform.BasisX.Z]
            orig_y = [transform.BasisY.X, transform.BasisY.Y, transform.BasisY.Z]
            orig_z = [transform.BasisZ.X, transform.BasisZ.Y, transform.BasisZ.Z]

            # APPLY ROTATION -90 DEG ABOUT X-AXIS
            # Analysis expects: Local Y aligned with Global Z (Vertical)
            # R_x(-90): New Y = Old Z, New Z = -Old Y
            
            # New Basis Vectors
            new_x = orig_x
            new_y = orig_z
            new_z = [-orig_y[0], -orig_y[1], -orig_y[2]]
            
            local_axes["x_axis"] = [round(v, 6) for v in new_x]
            local_axes["y_axis"] = [round(v, 6) for v in new_y]
            local_axes["z_axis"] = [round(v, 6) for v in new_z]
            
            #local_axes["rotation_angle_deg"] = -90.0
            
            # Determine Web Direction based on New Basis
            # Web is usually along local Y (Depth direction for I-beam in analysis convention?)
            # Or is it Z?
            # Revit: Z is Depth (Web).
            # We mapped Old Z -> New Y.
            # So New Y is Web direction.
            local_axes["web_direction"] = "y_axis"
                
        elif cat_id == int(BuiltInCategory.OST_StructuralColumns):
            # COLUMN: Manual Construction based on COLUMN_ROTATION_DEG
            # Baseline (0 deg): Local X=Vert, Local Y=Global X, Local Z=Global Y
            # Right-Hand Rule Rotation about Vertical Axis (Z)
            
            theta_rad = math.radians(COLUMN_ROTATION_DEG)
            cos_t = math.cos(theta_rad)
            sin_t = math.sin(theta_rad)
            
            # Global X and Y rotated by theta around Global Z
            # New Local Y (was Global X)
            ly_x = cos_t
            ly_y = sin_t
            ly_z = 0.0
            
            # New Local Z (was Global Y)
            # Standard 2D rotation: x' = x c - y s, y' = x s + y c
            # Mapping: GX -> LY, GY -> LZ
            # LZ is rotated GY.
            # Vector (0,1) rotated by theta is (-sin, cos)
            lz_x = -sin_t
            lz_y = cos_t
            lz_z = 0.0
            
            local_axes["x_axis"] = [0.0, 0.0, 1.0] # Always Vertical
            local_axes["y_axis"] = [round(ly_x, 6), round(ly_y, 6), round(ly_z, 6)]
            local_axes["z_axis"] = [round(lz_x, 6), round(lz_y, 6), round(lz_z, 6)]
            
            # Set explicit rotation value
            #local_axes["rotation_angle_deg"] = float(COLUMN_ROTATION_DEG)
            
            # Remove web_direction for columns (Requested by User)
            if "web_direction" in local_axes:
                 # Dictionary keys are strings, but we didn't add it in the default dict above? 
                 # Wait, I removed "web_direction" from the default dict above in this replacement content.
                 # Ah, for beams I added it. For columns I just don't add it.
                 pass

    except Exception as e:
        pass
    
    return local_axes

# ===================================================
# AISC 360-22 DESIGN PARAMETERS (SRPMK)
# ===================================================

def calculate_section_classification(section_props, material_props):
    """
    Calculate section slenderness (λ) and classify per AISC 360-22 Table B4.1b.
    For SRPMK: Uses λhd (highly ductile) limits per AISC 341-22.
    
    Args:
        section_props: Dictionary from get_section_properties()
        material_props: Dictionary from get_material_data()
    
    Returns:
        Dictionary with λ values and classification
    """
    # Extract values from existing section properties
    d = section_props.get("d_mm", 0)
    b = section_props.get("b_mm", 0)
    tf = section_props.get("tf_mm", 0)
    tw = section_props.get("tw_mm", 0)
    Fy = material_props.get("Fy_MPa", 0)
    E = material_props.get("E_MPa", 0)
    
    # Safety check
    if tf <= 0 or tw <= 0 or Fy <= 0 or E <= 0:
        return {
            "lambda_flange": 0, "lambda_web": 0,
            "lambda_p_flange": 0, "lambda_r_flange": 0, "lambda_hd_flange": 0,
            "lambda_p_web": 0, "lambda_r_web": 0, "lambda_hd_web": 0,
            "flange_class": "unknown", "web_class": "unknown",
            "srpmk_flange_ok": False, "srpmk_web_ok": False
        }
    
    # Calculate slenderness ratios
    lambda_flange = (b / 2.0) / tf   # b/(2*tf) for I-section flanges (half-flange width)
    h_web = d - 2.0 * tf             # Clear web height
    lambda_web = h_web / tw          # h/tw for webs
    
    # AISC 360-22 Table B4.1b limits (flexure) - Case 10 for flanges, Case 15 for webs
    # λp = compact limit, λr = noncompact limit
    lambda_p_flange = 0.38 * math.sqrt(E / Fy)   # Compact flange (Case 10)
    lambda_r_flange = 1.0 * math.sqrt(E / Fy)    # Noncompact flange limit
    
    lambda_p_web = 3.76 * math.sqrt(E / Fy)      # Compact web (Case 15)
    lambda_r_web = 5.70 * math.sqrt(E / Fy)      # Noncompact web limit
    
    # AISC 341-22 Table D1.1 - Highly Ductile Members (SRPMK requirement)
    lambda_hd_flange = 0.30 * math.sqrt(E / Fy)  # Highly ductile flange
    lambda_hd_web = 2.45 * math.sqrt(E / Fy)     # Highly ductile web (Ca assumed ≈ 0)
    
    # Classify per AISC 360-22
    if lambda_flange <= lambda_p_flange:
        flange_class = "compact"
    elif lambda_flange <= lambda_r_flange:
        flange_class = "noncompact"
    else:
        flange_class = "slender"
    
    if lambda_web <= lambda_p_web:
        web_class = "compact"
    elif lambda_web <= lambda_r_web:
        web_class = "noncompact"
    else:
        web_class = "slender"
    
    # SRPMK (Highly Ductile) check per AISC 341-22
    flange_srpmk_ok = lambda_flange <= lambda_hd_flange
    web_srpmk_ok = lambda_web <= lambda_hd_web
    
    return {
        "lambda_flange": round(lambda_flange, 2),
        "lambda_web": round(lambda_web, 2),
        "lambda_p_flange": round(lambda_p_flange, 2),
        "lambda_r_flange": round(lambda_r_flange, 2),
        "lambda_hd_flange": round(lambda_hd_flange, 2),
        "lambda_p_web": round(lambda_p_web, 2),
        "lambda_r_web": round(lambda_r_web, 2),
        "lambda_hd_web": round(lambda_hd_web, 2),
        "flange_class": flange_class,
        "web_class": web_class,
        "srpmk_flange_ok": flange_srpmk_ok,
        "srpmk_web_ok": web_srpmk_ok
    }


def get_design_parameters(element_type, topology):
    """
    Generate design parameters for AISC 360-22 checks.
    SRPMK Fixed-Fixed assumptions: K=0.65 (theoretical 0.5, practical 0.65).
    
    Args:
        element_type: "Column" or "Beam"
        topology: Dictionary from get_topology_ref()
    
    Returns:
        Dictionary with K factors, unbraced lengths, Cb
    """
    length_mm = topology.get("length_mm", 0)
    
    # SRPMK Fixed-Fixed assumption (moment frame with rigid connections)
    # Theoretical K=0.5, practical K=0.65 (accounts for imperfect fixity)
    # For sidesway inhibited (braced) frames with fixed ends
    Kx = 0.65  # Strong-axis (typically braced by diaphragm)
    Ky = 0.65  # Weak-axis (fixed-fixed assumption)
    
    # Unbraced lengths - conservative (full member length)
    # Can be refined based on lateral bracing points
    Lx_mm = length_mm  # Strong-axis unbraced length
    Ly_mm = length_mm  # Weak-axis unbraced length
    Lb_mm = length_mm  # Lateral-torsional buckling length (beam)
    
    return {
        "Kx": Kx,
        "Ky": Ky,
        "Lx_mm": Lx_mm,
        "Ly_mm": Ly_mm,
        "Lb_mm": Lb_mm,
        "Cb": 1.0,  # Conservative per user request
        "frame_type": "SRPMK",
        "end_condition": "fixed-fixed"
    }


# ===================================================
# ELEMENT DATA
# ===================================================

def get_element_data(element, doc):
    # 1. Format Nama Family & Type
    # Target: "FamilyName : TypeName" (Contoh: "UC-Universal Columns : UC305x305x97")
    full_display_name = "Unknown"
    try:
        sym = element.Symbol
        full_display_name = "{} : {}".format(sym.FamilyName, sym.Name)
    except: 
        try: full_display_name = element.Name # Fallback
        except: pass

    # 2. Deteksi Type berdasarkan Category ID
    elem_type_str = "Column" if element.Category.Id.IntegerValue == int(BuiltInCategory.OST_StructuralColumns) else "Beam"
    
    # 3. Get existing properties
    section_props = get_section_properties(element, doc)
    material_props = get_material_data(element, doc)
    topology_data = get_topology_ref(element, doc)
    
    # 4. Susun Dictionary Data
    data = {
        "id": element.Id.IntegerValue,
        "type": elem_type_str,
        "group": determine_group(element),
        "family": full_display_name, 
        "section": section_props,
        "material": material_props,
        "topology": topology_data,
        "local_axes": get_local_axes(element, doc),
        
        # NEW: AISC 360-22 Design Parameters (SRPMK)
        "design_parameters": get_design_parameters(elem_type_str, topology_data),
        
        # NEW: Section Classification per AISC 360-22 & AISC 341-22
        "section_classification": calculate_section_classification(section_props, material_props)
    }
    
    # 5. Hitung Beban (Khusus Beam)
    # Pastikan topology sudah Integer (hasil revisi sebelumnya)
    if data["type"] == "Beam":
        start = data["topology"]["start_node"] 
        end = data["topology"]["end_node"]     
        
        # Panggil fungsi hitungan beban
        data["loads"] = calculate_beam_distributed_load(start, end)
    else:
        data["loads"] = None 
        
    return data

# ===================================================
# MAIN EXECUTION (TRANSACTION)
# ===================================================
cols_to_process, created_ids = [], []

# === PRE-TRANSACTION: Overwrite Lookup Tables ===
print("\n🔧 Preparing Custom Sections...")
try:
    # Collect unique sections needed for beams and columns
    beam_sections = {}
    col_sections = {}
    for sec_name in [SECTION_BEAM_EXT, SECTION_BEAM_INT]:
        if sec_name in SECTIONS:
            beam_sections[sec_name] = SECTIONS[sec_name]
    for sec_name in [SECTION_COL]:
        if sec_name in SECTIONS:
            col_sections[sec_name] = SECTIONS[sec_name]
    
    overwrite_lookup_table(BEAM_TXT_PATH, beam_sections)
    overwrite_lookup_table(COL_TXT_PATH, col_sections)
except Exception as e:
    print("  ⚠️ Lookup table error: {}".format(str(e)))

# Family reload dipindah ke DALAM main transaction (sebelum cleanup)
# agar analytical elements yang mungkin muncul dari reload ikut ter-cleanup

try:
    with revit.Transaction("Generate Model"):
        # 0. SET PROJECT UNITS TO MM
        set_project_units_mm(doc)
        
        # 0.5 RELOAD FAMILIES — selalu reload agar .txt terbaru terbaca
        print("\n🔧 Reloading Families...")
        reload_families(doc, BEAM_RFA_PATH, COL_RFA_PATH)
        doc.Regenerate()
        
        # 1. CLEANUP (PHYSICAL & ANALYTICAL)
        # =========================================================
        # Menghapus elemen lama sebelum membuat yang baru.
        # Target: Fisik (Balok/Kolom) DAN Analitik (Member/Node/dll).
        
        ids_to_del = List[ElementId]()
        
        # Daftar nama kategori yang ingin dihapus.
        # Menggunakan string agar aman untuk berbagai versi Revit (2022/2023/2024).
        categories_to_clean = [
            # --- ELEMEN FISIK ---
            "OST_StructuralColumns", 
            "OST_StructuralFraming",
            
            # --- ELEMEN ANALITIK (Revit 2023+) ---
            "OST_AnalyticalMember", 
            "OST_AnalyticalPanel", 
            "OST_AnalyticalNodes", 
            "OST_AnalyticalLinks",
            
            # --- ELEMEN ANALITIK (Legacy / Revit Lama) ---
            "OST_AnalyticalBeams", 
            "OST_AnalyticalColumns",
            "OST_AnalyticalFloors", 
            "OST_AnalyticalWalls"
        ]

        for cat_name in categories_to_clean:
            # Cek apakah kategori tersebut ada di versi Revit ini?
            if hasattr(BuiltInCategory, cat_name):
                cat_enum = getattr(BuiltInCategory, cat_name)
                
                # Kumpulkan semua elemen dari kategori tersebut
                col = FilteredElementCollector(doc).OfCategory(cat_enum).WhereElementIsNotElementType()
                for e in col: 
                    ids_to_del.Add(e.Id)

        # Eksekusi Penghapusan
        if ids_to_del.Count > 0: 
            doc.Delete(ids_to_del)
        
        doc.Regenerate()

        # -------------------------------------------------------------
        # 2. LEVEL REBUILD (METODE COPY - SAFE MODE)
        # -------------------------------------------------------------
        # Strategi: Sisakan 1 level terbawah sebagai Base, lalu Copy ke atas.
        
        # A. BERSIHKAN LEVEL LAMA
        all_levels = FilteredElementCollector(doc).OfClass(Level).ToElements()
        sorted_levels = sorted(all_levels, key=lambda x: x.Elevation)
        
        active_levels = []
        
        # Siapkan list C# untuk menampung ID yang akan dihapus
        ids_to_delete = List[ElementId]() 
        
        if len(sorted_levels) > 0:
            # Ambil Level paling bawah sebagai Base
            base_level = sorted_levels[0]
            
            # Reset Base Level (Elevasi 0 & Nama "Level 1")
            base_level.Elevation = 0.0
            p_name = base_level.get_Parameter(BuiltInParameter.DATUM_TEXT)
            if p_name: p_name.Set("Level 1")
            
            # Unpin jika terkunci
            if base_level.Pinned: base_level.Pinned = False
                
            active_levels.append(base_level)
            
            # Masukkan level sisanya ke daftar hapus
            for i in range(1, len(sorted_levels)):
                ids_to_delete.Add(sorted_levels[i].Id)
                
            # Hapus Level sisa
            if ids_to_delete.Count > 0:
                doc.Delete(ids_to_delete)
                doc.Regenerate()
        else:
            # Buat baru jika kosong (Safety)
            base_level = Level.Create(doc, 0.0)
            p_name = base_level.get_Parameter(BuiltInParameter.DATUM_TEXT)
            if p_name: p_name.Set("Level 1")
            active_levels.append(base_level)

        # B. COPY LEVEL KE ATAS
        height_ft = mm_to_ft(HEIGHT_MM)
        
        for k in range(1, N_STORY + 1):
            # Hitung vector jarak vertikal (Z)
            offset_z = k * height_ft
            copy_vector = XYZ(0, 0, offset_z)
            
            # --- PERBAIKAN DI SINI (COLLECTION ERROR FIX) ---
            # CopyElement mengembalikan ICollection (.NET), bukan List Python.
            # Kita harus konversi ke list() agar bisa ambil index [0].
            
            copied_collection = ElementTransformUtils.CopyElement(doc, base_level.Id, copy_vector)
            copied_ids_list = list(copied_collection) # Konversi aman
            
            if len(copied_ids_list) > 0:
                new_lvl_id = copied_ids_list[0]
                new_lvl = doc.GetElement(new_lvl_id)
                
                # Rename Level Baru
                target_name = "Level " + str(k + 1)
                p_name_new = new_lvl.get_Parameter(BuiltInParameter.DATUM_TEXT)
                
                if p_name_new:
                    try:
                        p_name_new.Set(target_name)
                    except:
                        # Fallback nama unik jika nama sudah dipakai
                        p_name_new.Set(target_name + "_" + str(new_lvl.Id.IntegerValue))
                
                active_levels.append(new_lvl)

        doc.Regenerate()

        # 3. GEOMETRY BUILD (CENTERED AT ORIGIN 0,0,0)
        # === CREATE/GET MATERIALS ===
        print("\n🔧 Creating Materials...")
        mat_col = create_or_get_material(doc, MATERIAL_COL, MATERIALS[MATERIAL_COL])
        mat_beam_ext = create_or_get_material(doc, MATERIAL_BEAM_EXT, MATERIALS[MATERIAL_BEAM_EXT])
        mat_beam_int = create_or_get_material(doc, MATERIAL_BEAM_INT, MATERIALS[MATERIAL_BEAM_INT])
        
        # Debug: show material names and IDs
        for label, m in [("Column", mat_col), ("Beam Ext", mat_beam_ext), ("Beam Int", mat_beam_int)]:
            if m:
                print("  {} material: '{}' (Id={})".format(label, m.Name, m.Id.IntegerValue))
            else:
                print("  {} material: None!".format(label))
        
        # === FIND 3 FAMILY SYMBOLS ===
        print("\n🔧 Finding Family Symbols...")
        col_sym = find_structural_family(BuiltInCategory.OST_StructuralColumns, SECTION_COL)
        beam_ext_sym = find_structural_family(BuiltInCategory.OST_StructuralFraming, SECTION_BEAM_EXT)
        beam_int_sym = find_structural_family(BuiltInCategory.OST_StructuralFraming, SECTION_BEAM_INT)
        
        if not col_sym.IsActive: col_sym.Activate()
        if not beam_ext_sym.IsActive: beam_ext_sym.Activate()
        if not beam_int_sym.IsActive: beam_int_sym.Activate()
        
        print("  Column:       {} ({})".format(col_sym.Name, col_sym.FamilyName))
        print("  Beam Ext:     {} ({})".format(beam_ext_sym.Name, beam_ext_sym.FamilyName))
        print("  Beam Int:     {} ({})".format(beam_int_sym.Name, beam_int_sym.FamilyName))
        
        # === SET MATERIAL ON FAMILY SYMBOLS (TYPE LEVEL) ===
        for sym, mat, label in [(col_sym, mat_col, "Column"), 
                                 (beam_ext_sym, mat_beam_ext, "Beam Ext"),
                                 (beam_int_sym, mat_beam_int, "Beam Int")]:
            if mat:
                try:
                    p_mat = sym.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                    if p_mat and not p_mat.IsReadOnly:
                        p_mat.Set(mat.Id)
                        print("  ✅ {} type material set to '{}'".format(label, mat.Name))
                    else:
                        print("  ⚠️ {} type STRUCTURAL_MATERIAL_PARAM is read-only".format(label))
                except Exception as e:
                    print("  ⚠️ {} type material set error: {}".format(label, str(e)))

        # --- LOGIKA CENTER OF WORKSPACE ---
        span_x_ft = mm_to_ft(SPAN_X_MM)
        span_y_ft = mm_to_ft(SPAN_Y_MM)
        
        # Hitung total dimensi bangunan dalam feet
        total_width_x = span_x_ft * BAY_X_COUNT
        total_depth_y = span_y_ft * BAY_Y_COUNT
        
        # Tentukan offset agar (0,0) berada tepat di tengah bangunan
        # Kita geser titik mulai ke kiri bawah (negatif)
        start_x = -(total_width_x / 2.0)
        start_y = -(total_depth_y / 2.0)
        
        # Fungsi helper untuk mendapatkan koordinat absolut berdasarkan grid index
        def get_pt(i, j, elev):
            x = start_x + (i * span_x_ft)
            y = start_y + (j * span_y_ft)
            return XYZ(x, y, elev)

        for k in range(N_STORY):
            lb, lt = active_levels[k], active_levels[k+1]
            z_top = lt.Elevation
            
            # A. Create Columns (Grid Nodes)
            for i in range(BAY_X_COUNT + 1):
                for j in range(BAY_Y_COUNT + 1):
                    # Titik Bawah dan Atas Kolom (Sekarang sudah centered)
                    p1 = get_pt(i, j, lb.Elevation)
                    p2 = get_pt(i, j, lt.Elevation)
                    
                    c = doc.Create.NewFamilyInstance(Line.CreateBound(p1, p2), col_sym, lb, StructuralType.Column)
                    
                    # Assign material to column
                    if mat_col:
                        try:
                            p_mat = c.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                            if p_mat and not p_mat.IsReadOnly:
                                p_mat.Set(mat_col.Id)
                        except: pass
                    
                    # PHYSICAL ROTATION: Apply default 90° rotation to physical element only
                    # This does NOT change analytical local axes
                    physical_rotation_rad = math.radians(90+COLUMN_ROTATION_DEG)
                    
                    if abs(physical_rotation_rad) > 0.001:  # Only rotate if angle is non-zero
                        try:
                            # Create vertical axis for column rotation
                            axis_end = XYZ(p1.X, p1.Y, p1.Z + 10)
                            axis = Line.CreateBound(p1, axis_end)
                            ElementTransformUtils.RotateElement(doc, c.Id, axis, physical_rotation_rad)
                        except Exception as e:
                            print("Column physical rotation warning: " + str(e))
                    
                    cols_to_process.append({'el':c, 'lb':lb, 'lt':lt})
                    created_ids.append(c.Id)
                    _ELEMENT_GROUPS[c.Id.IntegerValue] = "Column"

            # B. Create Beams (with Exterior/Interior detection)
            def mk_bm(p_start, p_end, sym, mat, group_name):
                b = doc.Create.NewFamilyInstance(Line.CreateBound(p_start, p_end), sym, lt, StructuralType.Beam)
                set_beam_alignment_safe(b)
                
                # Assign material to beam
                if mat:
                    try:
                        p_mat = b.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                        if p_mat and not p_mat.IsReadOnly:
                            p_mat.Set(mat.Id)
                    except: pass
                
                # Set Join Status for Structural Framing
                try:
                    if JOIN_STATUS:
                        StructuralFramingUtils.AllowJoinAtEnd(b, 0)
                        StructuralFramingUtils.AllowJoinAtEnd(b, 1)
                    else:
                        StructuralFramingUtils.DisallowJoinAtEnd(b, 0)
                        StructuralFramingUtils.DisallowJoinAtEnd(b, 1)
                except Exception as join_err:
                    pass
                
                # Track group assignment
                _ELEMENT_GROUPS[b.Id.IntegerValue] = group_name
                
                return b.Id
                
            # Balok Arah X (Horizontal) — Deteksi Eksterior/Interior
            for j in range(BAY_Y_COUNT + 1):
                is_ext = (j == 0 or j == BAY_Y_COUNT)
                sym = beam_ext_sym if is_ext else beam_int_sym
                mat = mat_beam_ext if is_ext else mat_beam_int
                grp = "Beam Exterior" if is_ext else "Beam Interior"
                for i in range(BAY_X_COUNT):
                    p_start = get_pt(i, j, z_top)
                    p_end   = get_pt(i+1, j, z_top)
                    created_ids.append(mk_bm(p_start, p_end, sym, mat, grp))
            
            # Balok Arah Y (Vertikal) — Deteksi Eksterior/Interior
            for i in range(BAY_X_COUNT + 1):
                is_ext = (i == 0 or i == BAY_X_COUNT)
                sym = beam_ext_sym if is_ext else beam_int_sym
                mat = mat_beam_ext if is_ext else mat_beam_int
                grp = "Beam Exterior" if is_ext else "Beam Interior"
                for j in range(BAY_Y_COUNT):
                    p_start = get_pt(i, j, z_top)
                    p_end   = get_pt(i, j+1, z_top)
                    created_ids.append(mk_bm(p_start, p_end, sym, mat, grp))
        # 4. FIX CONSTRAINTS
        doc.Regenerate()
        for x in cols_to_process:
            try:
                x['el'].get_Parameter(BuiltInParameter.SLANTED_COLUMN_TYPE_PARAM).Set(0)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM).Set(x['lb'].Id)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM).Set(x['lt'].Id)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM).Set(0.0)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(0.0)
            except: pass

except Exception as e:
    TaskDialog.Show("Error", str(e))

# ============================================================================
# GENERATE ANALYTICAL MODEL (LOGIKA PRINT TOTAL GABUNGAN)
# ============================================================================
# Update: Output disederhanakan.
# "Total Analytical Member Baru" & "Total Model Analitik Aktif" = Total Fisik (Balok + Kolom).

from System.Collections.Generic import List

# --- FUNGSI BANTUAN GEOMETRI ---
def get_element_curve(element):
    """Mendapatkan garis sumbu dari elemen (Balok/Kolom)."""
    loc = element.Location
    if isinstance(loc, LocationCurve):
        return loc.Curve
    elif isinstance(loc, LocationPoint): # Kolom Vertikal
        bbox = element.get_BoundingBox(None)
        if bbox:
            center_x = (bbox.Min.X + bbox.Max.X) / 2.0
            center_y = (bbox.Min.Y + bbox.Max.Y) / 2.0
            pt_start = XYZ(center_x, center_y, bbox.Min.Z)
            pt_end   = XYZ(center_x, center_y, bbox.Max.Z)
            if pt_start.DistanceTo(pt_end) > 0.01: 
                return Line.CreateBound(pt_start, pt_end)
    return None

# --- TRANSAKSI UTAMA ---
try:
    with revit.Transaction("Automation: Physical to Analytical"):
        app_version = int(doc.Application.VersionNumber)
        
        # 1. SETUP FILTER
        cat_list = List[BuiltInCategory]()
        cat_list.Add(BuiltInCategory.OST_StructuralFraming)
        cat_list.Add(BuiltInCategory.OST_StructuralColumns)
        
        filter_cat = ElementMulticategoryFilter(cat_list)
        collector = FilteredElementCollector(doc).WhereElementIsNotElementType()
        physical_elements = collector.WherePasses(filter_cat).ToElements()

        # Variabel penghitung
        count_processed_successfully = 0 

        # ---------------------------------------------------------
        # SKENARIO A: REVIT LAMA (< 2023)
        # ---------------------------------------------------------
        if app_version < 2023:
            for el in physical_elements:
                try:
                    p = el.get_Parameter(BuiltInParameter.STRUCTURAL_ANALYTICAL_MODEL)
                    if p and not p.IsReadOnly:
                        # Baik sudah 1 (aktif) atau baru di-set ke 1, kita hitung sukses
                        if p.AsInteger() == 0:
                            p.Set(1)
                        count_processed_successfully += 1
                except:
                    pass
        
        # ---------------------------------------------------------
        # SKENARIO B: REVIT BARU (2023+)
        # ---------------------------------------------------------
        else:
            from Autodesk.Revit.DB.Structure import AnalyticalMember, AnalyticalToPhysicalAssociationManager, AnalyticalStructuralRole
            assoc_manager = AnalyticalToPhysicalAssociationManager.GetAnalyticalToPhysicalAssociationManager(doc)

            for phys_el in physical_elements:
                try:
                    # Cek Existing Association
                    associated_id = assoc_manager.GetAssociatedElementId(phys_el.Id)
                    
                    # Cek apakah analitiknya valid (Existing)
                    is_existing_valid = False
                    if associated_id != ElementId.InvalidElementId:
                        if doc.GetElement(associated_id) is not None:
                            is_existing_valid = True
                    
                    if is_existing_valid:
                        # Jika sudah ada, kita hitung sebagai bagian dari total sukses
                        count_processed_successfully += 1
                        continue 

                    # Jika belum ada, Buat Baru (New)
                    curve = get_element_curve(phys_el)
                    
                    if curve:
                        # Create
                        new_am = AnalyticalMember.Create(doc, curve)
                        
                        # Set Role
                        cat_id = phys_el.Category.Id.IntegerValue
                        if cat_id == int(BuiltInCategory.OST_StructuralColumns):
                            new_am.StructuralRole = AnalyticalStructuralRole.StructuralRoleColumn
                        else:
                            new_am.StructuralRole = AnalyticalStructuralRole.StructuralRoleBeam

                        # Associate
                        assoc_manager.AddAssociation(new_am.Id, phys_el.Id)
                        
                        # Hitung sukses
                        count_processed_successfully += 1
                    else:
                        print("    Warning: Gagal ekstrak geometri ID {}".format(phys_el.Id))
                
                except Exception as e_inner:
                    print("    Error pada ID {}: {}".format(phys_el.Id, str(e_inner)))

        # --- FINAL REPORT (SESUAI REQUEST) ---
        # Menampilkan total gabungan (Existing + Baru)
        

        # --- APPLY ANALYTICAL CROSS-SECTION ROTATION ---
        # This must be done BEFORE extracting local axes
        # Apply default rotations + user overrides to match SAP2000 conventions
        
        if app_version >= 2023:
            try:
                print("🔄 Applying Analytical Cross-Section Rotations...")
                
                # Get all analytical members
                analytical_members = FilteredElementCollector(doc).OfClass(AnalyticalMember).WhereElementIsNotElementType().ToElements()
                
                rotation_count = {"column": 0, "beam": 0}
                
                for am in analytical_members:
                    try:
                        # Get associated physical element to determine type
                        phys_id = assoc_manager.GetAssociatedElementId(am.Id)
                        if phys_id == ElementId.InvalidElementId:
                            continue
                        
                        phys_el = doc.GetElement(phys_id)
                        if phys_el is None:
                            continue
                        
                        # Determine if Column or Beam
                        cat_id = phys_el.Category.Id.IntegerValue
                        is_column = (cat_id == int(BuiltInCategory.OST_StructuralColumns))
                        is_beam = (cat_id == int(BuiltInCategory.OST_StructuralFraming))
                        
                        rotation_applied = False
                        
                        if is_column:
                            # Apply ANALYTICAL_COLUMN_ROTATION_DEG (user override only, no default)
                            rotation_deg = 0 + COLUMN_ROTATION_DEG
                        elif is_beam:
                            # Apply DEFAULT (-90°) + USER OVERRIDE for beams
                            rotation_deg = -90
                        else:
                            continue
                        
                        if abs(rotation_deg) > 0.001:
                            # Convert degrees to radians (Revit internal unit)
                            rotation_rad = math.radians(rotation_deg)
                            
                            # METHOD 1: Try using CrossSectionRotation property (if available)
                            try:
                                current_rotation = am.CrossSectionRotation
                                am.CrossSectionRotation = current_rotation + rotation_rad
                                rotation_applied = True
                            except:
                                pass
                            
                            # METHOD 2: Fallback to Parameter (ANALYTICAL_MEMBER_ROTATION)
                            if not rotation_applied:
                                try:
                                    # Try accessing via BuiltInParameter
                                    if hasattr(BuiltInParameter, 'ANALYTICAL_MEMBER_ROTATION'):
                                        param = am.get_Parameter(BuiltInParameter.ANALYTICAL_MEMBER_ROTATION)
                                    else:
                                        # Fallback: search by parameter name
                                        param = None
                                        for p in am.Parameters:
                                            if p.Definition.Name == "ANALYTICAL_MEMBER_ROTATION":
                                                param = p
                                                break
                                    
                                    if param and not param.IsReadOnly:
                                        current_val = param.AsDouble()
                                        param.Set(current_val + rotation_rad)
                                        rotation_applied = True
                                except:
                                    pass
                            
                            if rotation_applied:
                                if is_column:
                                    rotation_count["column"] += 1
                                elif is_beam:
                                    rotation_count["beam"] += 1
                                    
                    except Exception as e_rot:
                        print(f"  Warning: Could not rotate analytical member {am.Id}: {str(e_rot)}")
                
                print(f"  ✓ Rotated {rotation_count['column']} columns, {rotation_count['beam']} beams analytically.")
                
            except Exception as e_rotation:
                print(f"⚠️ Analytical rotation error: {str(e_rotation)}")
        else:
            print("\\n⚠️ Analytical rotation skipped (Revit < 2023 doesn't have analytical rotation API)")


except Exception as e:
    print("❌ Error Fatal: " + str(e))

# ============================================================================
# ATUR VISIBILITY GRAPHIC (LOGIKA PROPERTY DIRECT)
# ============================================================================
# Referensi User: view.AreAnalyticalModelCategoriesHidden
# Target: Mengubah nilai properti tersebut menjadi False (agar TIDAK Hidden / Muncul)

try:
    with revit.Transaction("Fix Analytical Visibility"):
        view = doc.ActiveView
        
        # --- CEK VIEW TEMPLATE ---
        if view.ViewTemplateId != ElementId.InvalidElementId:
            print("⚠️ WARNING: View ini dikunci oleh View Template.")

        # -----------------------------------------------------------------------
        # A. MASTER SWITCH (MENGGUNAKAN REFERENSI ANDA)
        # -----------------------------------------------------------------------
        # Kita cek apakah properti 'AreAnalyticalModelCategoriesHidden' tersedia
        # Jika ya, kita set ke False (artinya: Jangan Sembunyikan = Tampilkan)
        
        master_switch_succcess = False
        
        try:
            # Cek ketersediaan properti (Pythonic way untuk C# Property)
            if hasattr(view, "AreAnalyticalModelCategoriesHidden"):
                # Cek kondisi sekarang
                if view.AreAnalyticalModelCategoriesHidden == True:
                    # ACTION: Set menjadi False untuk MENCENTANG checkbox
                    view.AreAnalyticalModelCategoriesHidden = False 
                    print(" -> [Master Switch] Berhasil dicentang (via AreAnalyticalModelCategoriesHidden).")
                else:
                    print(" -> [Master Switch] Sudah aktif sebelumnya.")
                master_switch_succcess = True
        except Exception as e_prop:
            print(" -> Info: Akses langsung properti gagal, mencoba metode Parameter fallback.")

        # -----------------------------------------------------------------------
        # B. FALLBACK (JIKA PROPERTI DI ATAS GAGAL/VERSI LAMA)
        # -----------------------------------------------------------------------
        if not master_switch_succcess:
            # Coba akses parameter manual (VG / VIEW_STRUCT)
            param_names = ['VG_ANALYTICAL_MODEL_VISIBILITY', 'VIEW_STRUCT_ANALYTICAL_MODEL_VISIBILITY']
            for p_name in param_names:
                if hasattr(BuiltInParameter, p_name):
                    p_enum = getattr(BuiltInParameter, p_name)
                    p = view.get_Parameter(p_enum)
                    if p and not p.IsReadOnly and p.AsInteger() == 0:
                        p.Set(1)
                        print(" -> [Master Switch] Dicentang via Parameter {}.".format(p_name))
                        break

        # -----------------------------------------------------------------------
        # C. UNHIDE SUB-KATEGORI (SAFE STRING LOOKUP)
        # -----------------------------------------------------------------------
        # Memastikan item di dalam tree (Member, Node, Panel) ikut dicentang
        
        target_cat_names = [
            "OST_AnalyticalMember", "OST_AnalyticalPanel", # Revit 2023+
            "OST_AnalyticalBeams", "OST_AnalyticalColumns", # Revit Lama
            "OST_AnalyticalNodes", "OST_AnalyticalLinks",
            "OST_AnalyticalFloors", "OST_AnalyticalWalls"
        ]

        count = 0
        for name in target_cat_names:
            if hasattr(BuiltInCategory, name):
                cat_enum = getattr(BuiltInCategory, name)
                try:
                    cat_id = ElementId(int(cat_enum))
                    if view.CanCategoryBeHidden(cat_id):
                        if view.GetCategoryHidden(cat_id):
                            view.SetCategoryHidden(cat_id, False) # False = Unhide
                            count += 1
                except:
                    pass

        if count > 0:
            print(" -> [Sub-Category] {} item dimunculkan.".format(count))

        print("✅ Pengaturan Tampilan Selesai.")

except Exception as e:
    print("❌ Gagal mengatur visibility: " + str(e))

# ============================================================================
# 6. EKSPOR DATA KE JSON (GLOBAL LOAD LIST + CLEAN BEAM DATA)
# ============================================================================
import math

json_success = False 

try:
    # --- A. HELPER UNIT ---
    def ft2mm(ft): return ft * 304.8
    def sqft2sqmm(sqft): return sqft * 92903.04
    def ft42mm4(ft4): return ft4 * 863097484.12
    def ft32mm3(ft3): return ft3 * 28316846.59

    # --- B. SET GLOBALS UNTUK FUNGSI HITUNGAN ---
    # 1. Load Pressure Global (SW, ADL, LL terpisah)
    try: 
        globals()['SLAB_SW_PRESSURE'] = float(SLAB_SW_PRESSURE)
        globals()['SLAB_ADL_PRESSURE'] = float(SLAB_ADL_PRESSURE)
        globals()['LIVE_LOAD_PRESSURE'] = float(LIVE_LOAD_PRESSURE)
    except: 
        globals()['SLAB_SW_PRESSURE'] = 0.0
        globals()['SLAB_ADL_PRESSURE'] = 0.0
        globals()['LIVE_LOAD_PRESSURE'] = 0.0

    # 2. Grid Spacing
    try: globals()['SPAN_X_MM'] = float(SPAN_X_MM)
    except: globals()['SPAN_X_MM'] = 4000.0
    
    try: globals()['SPAN_Y_MM'] = float(SPAN_Y_MM)
    except: globals()['SPAN_Y_MM'] = 4000.0

    # 3. Auto-Calculate Bay Count (Penting untuk deteksi Edge)
    collector_all = FilteredElementCollector(doc).WhereElementIsNotElementType()
    cats = List[BuiltInCategory]()
    cats.Add(BuiltInCategory.OST_StructuralFraming)
    cats.Add(BuiltInCategory.OST_StructuralColumns)
    filter_cat = ElementMulticategoryFilter(cats)
    elements_all = collector_all.WherePasses(filter_cat).ToElements()

    if elements_all.Count > 0:
        min_x, max_x = 999999.0, -999999.0
        min_y, max_y = 999999.0, -999999.0
        
        for el in elements_all:
            bb = el.get_BoundingBox(None)
            if bb:
                min_x = min(min_x, bb.Min.X)
                max_x = max(max_x, bb.Max.X)
                min_y = min(min_y, bb.Min.Y)
                max_y = max(max_y, bb.Max.Y)
        
        width_x_mm = ft2mm(max_x - min_x)
        width_y_mm = ft2mm(max_y - min_y)
        
        globals()['BAY_X_COUNT'] = round(width_x_mm / globals()['SPAN_X_MM'])
        globals()['BAY_Y_COUNT'] = round(width_y_mm / globals()['SPAN_Y_MM'])
    else:
        globals()['BAY_X_COUNT'] = 1
        globals()['BAY_Y_COUNT'] = 1

    # --- C. LOOPING & GENERATE ELEMENT DATA ---
    final_elements_list = []
    
    print("Memproses Ekspor JSON...")
    
    for el in elements_all:
        try:
            # Panggil fungsi get_element_data yang sudah Anda definisikan.
            # Fungsi ini akan memanggil calculate_beam_distributed_load yang BARU (tanpa pressure_MPa)
            el_data = get_element_data(el, doc)
            
            if el_data and el_data.get("id"):
                final_elements_list.append(el_data)
                
        except Exception as e_item:
            print("Skip Element ID {}: {}".format(el.Id, str(e_item)))

    # --- D. SAVE JSON (STRUKTUR FINAL) ---
    final_output = {
        # Load Patterns (SAP2000-like)
        "load_patterns": LOAD_PATTERNS,
        
        # Load Combination Config
        "load_combination_config": {
            "mode": LOAD_COMBO_MODE,
            "custom_combinations": CUSTOM_LOAD_COMBOS
        },
        
        # Legacy fields (backward compatibility)
        "slab_sw_pressure": SLAB_SW_PRESSURE,
        "slab_adl_pressure": SLAB_ADL_PRESSURE,
        "live_load_pressure": LIVE_LOAD_PRESSURE,
        
        # Seismic Parameters (SNI 1726)
        "seismic_parameters": {
            "site_class": SITE_CLASS,
            "SS": SS, "S1": S1, "TL": TL,
            "SDS": SDS, "SD1": SD1,
            "Fa": Fa, "Fv": Fv,
            "T0": round(T0, 4), "Ts": round(Ts_period, 4),
            "Ct": Ct, "x_Ta": x_Ta, "Ta": round(Ta, 4),
            "TOTAL_HEIGHT_M": TOTAL_HEIGHT_M,
            "Ie": Ie, "R": R, "Cd": Cd,
            "N_STORY": N_STORY, "HEIGHT_MM": HEIGHT_MM,
        },
        
        "unit_system": "Revit Converted (mm, N, MPa)",
        "model_elements": final_elements_list
    }

    d_dir = os.path.dirname(OUTPUT_PATH)
    if not os.path.exists(d_dir): os.makedirs(d_dir)

    with open(OUTPUT_PATH, 'w') as f:
        json.dump(final_output, f, indent=2)
        
    json_success = True
    print("✅ Export Selesai.")
    print("   Load Patterns: {}".format(list(LOAD_PATTERNS.keys())))
    print("   Combo Mode: {}".format(LOAD_COMBO_MODE))
    if CUSTOM_LOAD_COMBOS:
        print("   Custom Combos: {}".format(list(CUSTOM_LOAD_COMBOS.keys())))
    print("   Total Elements: {}".format(len(final_elements_list)))

except Exception as e:
    json_success = False
    print("❌ Error Export: " + str(e))
    TaskDialog.Show("Export Error", str(e))

# ===================================================
# SUBPROCESS TRIGGER (MANUAL HARDCODED PATH)
# ===================================================
if json_success:
    try:
        # 1. TENTUKAN LOKASI FILE HASIL SECARA MANUAL (HARDCODED)
        # Pastikan folder "Analysis" sudah ada di direktori tersebut
        RESULT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Analysis\\Analysis.json"
        
        # Cek apakah folder induknya ada, jika tidak buat dulu (untuk keamanan)
        result_dir = os.path.dirname(RESULT_PATH)
        if not os.path.exists(result_dir):
            os.makedirs(result_dir)

        # 2. VALIDASI FILE EXE & SCRIPT
        if os.path.exists(PYTHON_EXE_PATH) and os.path.exists(ANALYSIS_SCRIPT_PATH):
            
            out = script.get_output()
            out.print_md("### 🚀 Menjalankan OpenSees...")
            out.print_md("_Target Output: {}_".format(RESULT_PATH))

            # 3. SIAPKAN ARGUMEN (4 Item)
            # [Python Engine, Script Analysis, File Input (Model), File Output (Hasil)]
            args = [PYTHON_EXE_PATH, ANALYSIS_SCRIPT_PATH, OUTPUT_PATH, RESULT_PATH]
            
            # 4. JALANKAN PROSES (WAIT MODE)
            # Startupinfo menyembunyikan layar hitam CMD
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                startupinfo=startupinfo
            )
            
            # Tunggu proses selesai...
            stdout, stderr = process.communicate()
            
            if stderr:
                print("Python Error/Warning:")
                print(stderr)

            # ============================================================================
            # FUNGSI MERGE: Model data.json + Analysis.json → Result.json
            # ============================================================================
            def merge_to_result_json(model_path, analysis_path, merged_path, height_mm):
                """
                Menggabungkan Model data.json dan Analysis.json menjadi Result.json
                dengan enrichment: frame_label, node_id mapping, node description,
                klasifikasi node, dan relabeling koordinat/displacement.
                Siap digunakan sebagai input script desain dan cek kapasitas baja.
                """
                try:
                    # 1. Baca kedua file JSON
                    with open(model_path, 'r') as f:
                        model_data = json.load(f)
                    with open(analysis_path, 'r') as f:
                        analysis_data = json.load(f)
                    
                    # 2. Bangun mapping koordinat → node ID dari Analysis (SelfWeight)
                    coord_to_node_id = {}
                    sw_data = analysis_data.get('SelfWeight', {})
                    if 'nodes' in sw_data:
                        for nid, nval in sw_data['nodes'].items():
                            coords = nval.get('coords', [])
                            if len(coords) == 3:
                                key = (int(round(coords[0])), int(round(coords[1])), int(round(coords[2])))
                                coord_to_node_id[key] = nid
                    
                    # 3. Helper: Tentukan deskripsi node
                    def get_node_desc(coord_z, has_reaction, nid):
                        z_val = int(round(coord_z))
                        if z_val == 0:
                            return "Support Node (Fixed)"
                        else:
                            lantai = int(round(z_val / height_mm))
                            return "Joint Node (Lantai {})".format(lantai)
                    
                    # 4. Cek apakah node punya reaksi (= support)
                    def node_has_reaction(nid):
                        if 'nodes' in sw_data:
                            node_info = sw_data['nodes'].get(nid, {})
                            return node_info.get('reaction') is not None
                        return False
                    
                    # 5. Enrichment: Tambahkan frame_label, node mapping ke setiap elemen
                    col_counter = 0
                    beam_counter = 0
                    
                    for elem in model_data.get('model_elements', []):
                        elem_type = elem.get('type', '')
                        if elem_type == 'Column':
                            col_counter += 1
                            elem['frame_label'] = "C{}".format(col_counter)
                        elif elem_type == 'Beam':
                            beam_counter += 1
                            elem['frame_label'] = "B{}".format(beam_counter)
                        else:
                            elem['frame_label'] = "E{}".format(elem.get('id', '?'))
                        
                        topo = elem.get('topology', {})
                        start = topo.get('start_node', [0, 0, 0])
                        end = topo.get('end_node', [0, 0, 0])
                        
                        start_key = (int(round(start[0])), int(round(start[1])), int(round(start[2])))
                        end_key = (int(round(end[0])), int(round(end[1])), int(round(end[2])))
                        
                        start_nid = coord_to_node_id.get(start_key, "-")
                        end_nid = coord_to_node_id.get(end_key, "-")
                        
                        topo['start_node_id'] = start_nid
                        topo['end_node_id'] = end_nid
                        topo['start_node_desc'] = get_node_desc(start[2], node_has_reaction(start_nid), start_nid)
                        topo['end_node_desc'] = get_node_desc(end[2], node_has_reaction(end_nid), end_nid)
                    
                    # ================================================================
                    # 6. KLASIFIKASI NODE & RELABELING
                    # ================================================================
                    # Klasifikasi: support_nodes, floor_joint_nodes, subdivision_nodes
                    support_nodes = {}
                    floor_joint_nodes = {}
                    subdivision_nodes = {}
                    
                    if 'nodes' in sw_data:
                        for nid, nval in sw_data['nodes'].items():
                            coords = nval.get('coords', [0, 0, 0])
                            z_val = coords[2] if len(coords) == 3 else 0
                            has_reaction = nval.get('reaction') is not None
                            z_rounded = int(round(z_val))
                            
                            # Tentukan kategori node
                            is_floor_level = (z_rounded % int(height_mm) == 0)
                            
                            if has_reaction and z_rounded == 0:
                                category = "support"
                            elif is_floor_level:
                                category = "floor_joint"
                            else:
                                category = "subdivision"
                            
                            # Buat info node dengan label yang jelas
                            node_info = {
                                "coords": {
                                    "X": coords[0],
                                    "Y": coords[1],
                                    "Z": coords[2]
                                },
                                "category": category
                            }
                            
                            # Tambah deskripsi
                            if category == "support":
                                node_info["description"] = "Support Node (Fixed) - Z=0"
                            elif category == "floor_joint":
                                lantai = int(round(z_rounded / height_mm))
                                node_info["description"] = "Joint Node (Lantai {})".format(lantai)
                            else:
                                # Cari lantai terdekat untuk subdivision node
                                lantai_approx = int(round(z_rounded / height_mm))
                                node_info["description"] = "Subdivision Node (dekat Lantai {})".format(lantai_approx)
                            
                            # Simpan ke kategori
                            if category == "support":
                                support_nodes[nid] = node_info
                            elif category == "floor_joint":
                                floor_joint_nodes[nid] = node_info
                            else:
                                subdivision_nodes[nid] = node_info
                    
                    node_classification = {
                        "summary": {
                            "total_nodes": len(sw_data.get('nodes', {})),
                            "support_count": len(support_nodes),
                            "floor_joint_count": len(floor_joint_nodes),
                            "subdivision_count": len(subdivision_nodes)
                        },
                        "support_nodes": support_nodes,
                        "floor_joint_nodes": floor_joint_nodes,
                        "subdivision_nodes": subdivision_nodes
                    }
                    
                    # ================================================================
                    # 7. RELABELING NODE DI SETIAP LOAD CASE
                    # ================================================================
                    # Transformasi coords [x,y,z] → {X, Y, Z}
                    # Transformasi disp [d0..d5] → {ux, uy, uz, rx, ry, rz}
                    # Dynamically get all load case keys from analysis data
                    load_case_keys = [
                        k for k in analysis_data.keys()
                        if not k.startswith('_') and isinstance(analysis_data[k], dict)
                        and 'nodes' in analysis_data[k]
                    ]
                    
                    for case_key in load_case_keys:
                        case_data = analysis_data.get(case_key, {})
                        if 'nodes' not in case_data:
                            continue
                        
                        for nid, nval in case_data['nodes'].items():
                            # A. Relabel coords: array → dict {X, Y, Z}
                            old_coords = nval.get('coords', [0, 0, 0])
                            if isinstance(old_coords, list) and len(old_coords) == 3:
                                nval['coords'] = {
                                    "X": old_coords[0],
                                    "Y": old_coords[1],
                                    "Z": old_coords[2]
                                }
                            
                            # B. Relabel disp: array → dict {ux, uy, uz, rx, ry, rz}
                            old_disp = nval.get('disp', [0, 0, 0, 0, 0, 0])
                            if isinstance(old_disp, list) and len(old_disp) == 6:
                                nval['disp'] = {
                                    "ux": old_disp[0],
                                    "uy": old_disp[1],
                                    "uz": old_disp[2],
                                    "rx": old_disp[3],
                                    "ry": old_disp[4],
                                    "rz": old_disp[5]
                                }
                    
                    # ================================================================
                    # 8. Susun Result.json dengan 3 section terpisah
                    # ================================================================
                    result = {
                        "model_data": model_data,
                        "node_classification": node_classification,
                        "analysis_results": analysis_data
                    }
                    
                    # 9. Tulis Result.json
                    with open(merged_path, 'w') as f:
                        json.dump(result, f, indent=2)
                    
                    print("✅ Result.json berhasil dibuat: {}".format(merged_path))
                    print("   Model elements: {} ({}C + {}B)".format(
                        col_counter + beam_counter, col_counter, beam_counter))
                    print("   Nodes: {} total ({} support + {} floor joint + {} subdivision)".format(
                        node_classification['summary']['total_nodes'],
                        node_classification['summary']['support_count'],
                        node_classification['summary']['floor_joint_count'],
                        node_classification['summary']['subdivision_count']))
                    print("   Analysis cases: {}".format(len(analysis_data)))
                    
                except Exception as e_merge:
                    print("⚠️ Gagal membuat Result.json: {}".format(str(e_merge)))


            # --- [HELPER] FUNGSI UNTUK MEMBUAT TABEL RATA TENGAH ---
            def print_center_table(output, data, columns, title=""):
                """
                Fungsi pengganti out.print_table untuk memaksa rata tengah (Center Align)
                menggunakan sintaks Markdown ':---:'.
                """
                if not data: return
                
                # 1. Judul Tabel
                if title: 
                    output.print_md("### " + title)
                
                # 2. Header
                md_str = "| " + " | ".join(columns) + " |\n"
                
                # 3. Separator dengan Syntax Center Alignment (:---:)
                md_str += "| " + " | ".join([":---:" for _ in columns]) + " |\n"
                
                # 4. Isi Data
                for row in data:
                    row_str = [str(x) for x in row]
                    md_str += "| " + " | ".join(row_str) + " |\n"
                
                # 5. Print
                output.print_md(md_str)
                output.print_md("---")
                
            # [BARU] PRE-CALCULATION: DATA KOORDINAT NODE UTAMA (REVIT)
            # ============================================================================
            # Kita hanya ingin menampilkan lendutan di ujung-ujung balok/kolom.
            # Jadi kita kumpulkan dulu koordinat (x,y,z) dari semua elemen struktur di Revit.
            
            valid_node_coords = set() # Gunakan Set untuk pencarian cepat
            
            # Ambil semua Column & Framing
            collector = FilteredElementCollector(doc).WhereElementIsNotElementType().OfClass(FamilyInstance)
            
            for el in collector:
                cat_id = el.Category.Id.IntegerValue
                # Filter kategori Struktur
                if cat_id == int(BuiltInCategory.OST_StructuralColumns) or \
                   cat_id == int(BuiltInCategory.OST_StructuralFraming):
                    
                    # Ambil Geometry Curve (Garis sumbu elemen)
                    if hasattr(el.Location, "Curve"):
                        curve = el.Location.Curve
                        if curve:
                            p1 = curve.GetEndPoint(0) # Titik Start
                            p2 = curve.GetEndPoint(1) # Titik End
                            
                            # Konversi Feet ke mm dan bulatkan jadi Integer agar mudah dicocokkan
                            # (Revit 1 ft = 304.8 mm)
                            pt1_tup = (int(round(p1.X * 304.8)), int(round(p1.Y * 304.8)), int(round(p1.Z * 304.8)))
                            pt2_tup = (int(round(p2.X * 304.8)), int(round(p2.Y * 304.8)), int(round(p2.Z * 304.8)))
                            
                            valid_node_coords.add(pt1_tup)
                            valid_node_coords.add(pt2_tup)

            # ============================================================================
            # 5. BACA HASIL ANALISIS (MULTI-CASE: SW, LL, COMB)
            # ============================================================================
            
            if os.path.exists(RESULT_PATH):
                try:
                    with open(RESULT_PATH, 'r') as f:
                        all_results = json.load(f)

                    # Cek jika terjadi error global pada script Analysis.py
                    if "status" in all_results and all_results["status"] == "Error":
                        out.print_md("## ❌ Analisis Gagal Total")
                        out.print_md("**Pesan:** " + all_results.get("message", "Unknown Error"))
                    
                    else:
                        # === MERGE: Gabungkan Model data + Analysis → Result.json ===
                        merge_to_result_json(OUTPUT_PATH, ANALYSIS_JSON_PATH, MERGED_RESULT_PATH, HEIGHT_MM)

                        out.print_md("# 📑 LAPORAN HASIL ANALISIS STRUKTUR")
                                                # Build dynamic load case list from Analysis.json keys
                        gravity_keys = [k for k in all_results.keys() 
                                       if not k.startswith('_') 
                                       and k not in ('SeismicX', 'SeismicY')
                                       and isinstance(all_results[k], dict)]
                        
                        out.print_md("Berikut adalah hasil untuk {} skenario pembebanan:".format(len(gravity_keys)))
                        
                        # Build display list with titles
                        pattern_titles = {
                            "SelfWeight": "BEBAN MATI - Self Weight (Frame + Slab)",
                            "ADL": "BEBAN MATI TAMBAHAN - ADL (Finishing/Spesi)",
                            "LIVE": "BEBAN HIDUP (Live Load)",
                            "AdditionalDL": "BEBAN MATI TAMBAHAN (ADL)",
                            "DeadLoad": "TOTAL BEBAN MATI (SW + ADL)",
                            "LiveLoad": "BEBAN HIDUP (Live Load)",
                            "Combination": "KOMBINASI",
                        }
                        
                        load_cases = []
                        for idx, gk in enumerate(gravity_keys, 1):
                            title = pattern_titles.get(gk, gk)
                            # Untuk kombinasi, tampilkan formula
                            case_data = all_results.get(gk, {})
                            combo_factors = case_data.get('combination_factors', {})
                            if combo_factors:
                                parts = []
                                for pname, fval in combo_factors.items():
                                    if fval == 1.0:
                                        parts.append(pname)
                                    else:
                                        parts.append("{}{}".format(fval, pname))
                                formula = " + ".join(parts)
                                title = "{} = {}".format(gk, formula)
                            load_cases.append((gk, "KASUS {}: {}".format(idx, title)))

                        for case_key, case_title in load_cases:
                            # Ambil data spesifik per kasus
                            results = all_results.get(case_key)
                            
                            # Separator antar kasus
                            out.print_md("---") 
                            out.print_md("## " + case_title)

                            if not results:
                                out.print_md("> _Data hasil tidak ditemukan untuk kasus ini._")
                                continue

                            status = results.get("status", "Unknown")

                            if status == "Success":
                                # ===========================================================
                                # A. TABEL PERPINDAHAN (Displacement)
                                # ===========================================================
                                data_disp = []
                                if 'nodes' in results:
                                    for nid, val in results['nodes'].items():
                                        c = val['coords'] # Koordinat dari JSON [x, y, z] dalam mm
                                        d = val['disp']   # Displacement
                                        
                                        # Ambil koordinat node analisis (bulatkan ke int)
                                        check_coords = (int(round(c[0])), int(round(c[1])), int(round(c[2])))
                                        
                                        # --- LOGIKA FILTER ---
                                        # Hanya masukkan jika koordinat ini ada di daftar "valid_node_coords"
                                        # (Artinya node ini adalah ujung balok/kolom asli, bukan pecahan tengah)
                                        if check_coords in valid_node_coords:
                                            data_disp.append([
                                                nid, 
                                                check_coords[0], check_coords[1], check_coords[2], 
                                                round(d[2], 2) # Defleksi Z
                                            ])
                                    
                                    # Sort berdasarkan ID Node
                                    data_disp.sort(key=lambda x: int(x[0]))
                                    
                                    if data_disp:
                                        print_center_table(
                                            output=out,
                                            data=data_disp,
                                            columns=["Node ID", "X", "Y", "Z", "Defleksi Z (mm)"],
                                            title="Lendutan Vertikal Node Utama ({})".format(case_key)
                                        )
                                    else:
                                        out.print_md("> _Tidak ada node utama yang terdeteksi cocok dengan hasil analisis._")

                                # ===========================================================
                                # B. TABEL REAKSI TUMPUAN (Reaction)
                                # ===========================================================
                                data_reac = []
                                if 'nodes' in results:

                                    for nid, val in results['nodes'].items():
                                        reac = val.get('reaction')
                                        if reac:
                                            # Dictionary keys are already F1, F2, F3, M1, M2, M3
                                            f1 = reac.get('F1', 0.0)
                                            f2 = reac.get('F2', 0.0)
                                            f3 = reac.get('F3', 0.0)
                                            m1 = reac.get('M1', 0.0)
                                            m2 = reac.get('M2', 0.0)
                                            m3 = reac.get('M3', 0.0)
                                            
                                            data_reac.append([
                                                nid, 
                                                f1, f2, f3, m1, m2, m3
                                            ])
                                    
                                    if data_reac:
                                        data_reac.sort(key=lambda x: int(x[0]))
                                        print_center_table(
                                            output=out,
                                            data=data_reac,
                                            columns=["Node ID", "Fx (N)", "Fy (N)", "Fz (N)", "M1 (Nmm)", "M2 (Nmm)", "M3 (Nmm)"],
                                            title="Reaksi Tumpuan ({})".format(case_key)
                                        )
                                    else:
                                        out.print_md("> _Info: Tidak ada data reaksi (Check tumpuan)._")

                                # ===========================================================
                                # C. TABEL GAYA DALAM & SUMMARY (DENGAN FILTER ID)
                                # ===========================================================
                                data_elem = []
                                
                                # Reset Variabel Max/Min Stats untuk semua komponen
                                components = ["p", "v2", "v3", "t", "m2", "m3"]
                                # Init dengan +/- infinity
                                stats = {k: {"max": -1.0e20, "max_id": "-", "min": 1.0e20, "min_id": "-"} for k in components}

                                if 'elements' in results:
                                    for eid, val in results['elements'].items():
                                        try:
                                            # --- LOGIKA FILTERING BARU ---
                                            # 1. Konversi ID ke Integer Revit
                                            revit_id_int = int(eid)
                                            revit_el_id = ElementId(revit_id_int)
                                            
                                            # 2. Cek Keberadaan Elemen di Revit
                                            el = doc.GetElement(revit_el_id)
                                            
                                            # 3. JIKA NULL (ID Analisis/Split Node), SKIP LOOP INI
                                            if not el:
                                                continue 
                                            
                                            # 4. Cek Kategori (Hanya Balok & Kolom)
                                            cat_id = el.Category.Id.IntegerValue
                                            category_type = "Other"
                                            
                                            if cat_id == int(BuiltInCategory.OST_StructuralColumns):
                                                category_type = "Column"
                                            elif cat_id == int(BuiltInCategory.OST_StructuralFraming):
                                                category_type = "Beam"
                                            else:
                                                # Jika elemen bukan struktur utama (misal detail item), skip
                                                continue 

                                            # --- AMBIL DATA DATA ---
                                            elem_name = "{} : {}".format(el.Symbol.FamilyName, el.Name)
                                            
                                            # NEW: Multi-Station Support (SAP2000 Diagram Style)
                                            # Read stations array from JSON (adaptive stationing)
                                            stations = val.get('stations', [])
                                            
                                            if not stations:
                                                # Fallback: No stations data (old format or error)
                                                continue
                                            
                                            # Process each station
                                            for station_data in stations:
                                                station_loc = station_data.get('station', 0.0)
                                                p_val = station_data.get('P', 0.0)
                                                v2_val = station_data.get('V2', 0.0)
                                                v3_val = station_data.get('V3', 0.0)
                                                t_val = station_data.get('T', 0.0)
                                                m2_val = station_data.get('M2', 0.0)
                                                m3_val = station_data.get('M3', 0.0)

                                                # --- UPDATE STATISTIK MAKSIMUM & MINIMUM ---
                                                id_display = "[{}] {}".format(eid, elem_name)
                                                current_vals = {
                                                    "p": p_val, "v2": v2_val, "v3": v3_val, 
                                                    "t": t_val, "m2": m2_val, "m3": m3_val
                                                }
                                                
                                                for k in components:
                                                    val_comp = current_vals[k]
                                                    # Update Max
                                                    if val_comp > stats[k]["max"]:
                                                        stats[k]["max"] = val_comp
                                                        stats[k]["max_id"] = id_display
                                                    # Update Min
                                                    if val_comp < stats[k]["min"]:
                                                        stats[k]["min"] = val_comp
                                                        stats[k]["min_id"] = id_display

                                                # --- MASUKKAN KE LIST TABEL ---
                                                data_elem.append([
                                                    str(eid),
                                                    elem_name,
                                                    "{:.2f}".format(station_loc),  # Station location
                                                    round(p_val, 2),
                                                    round(v2_val, 2),
                                                    round(v3_val, 2),
                                                    round(t_val, 2),
                                                    round(m2_val, 2),
                                                    round(m3_val, 2)
                                                ])
                                        
                                        except Exception as e_inner:
                                            # Jika terjadi error konversi ID (misal ID string aneh), skip saja
                                            continue
                                    
                                    # Sort berdasarkan ID agar rapi
                                    data_elem.sort(key=lambda x: int(x[0]))

                                    # TAMPILKAN TABEL DETAIL
                                    if data_elem:
                                        print_center_table(
                                            output=out,
                                            data=data_elem,
                                            columns=["ID", "Family & Type", "Station", "P (N)", "V2 (N)", "V3 (N)", "T (Nmm)", "M2 (Nmm)", "M3 (Nmm)"],
                                            title="Detail Gaya Dalam Elemen Asli ({})".format(case_key)
                                        )
                                    else:
                                        out.print_md("> _Tidak ada elemen Revit yang cocok dengan hasil analisis._")

                                    # TAMPILKAN SUMMARY
                                    # TAMPILKAN SUMMARY KOMPREHENSIF (Max & Min)
                                    summary_rows = []
                                    labels = {
                                        "p": "Axial (P)", "t": "Torsi (T)", 
                                        "v2": "Shear (V2)", "v3": "Shear (V3)",
                                        "m2": "Momen (M2)", "m3": "Momen (M3)"
                                    }
                                    
                                    for k in components:
                                        # Handle jika tidak ada data (masih initial value)
                                        max_v = stats[k]["max"]
                                        if max_v < -1.0e19: max_v = 0.0
                                        
                                        min_v = stats[k]["min"]
                                        if min_v > 1.0e19: min_v = 0.0
                                        
                                        # Tentukan satuan: N untuk gaya, Nmm untuk momen/torsi
                                        unit = "N" if k in ["p", "v2", "v3"] else "Nmm"
                                        
                                        summary_rows.append([
                                            labels[k],
                                            "{} {}".format(round(max_v, 2), unit), 
                                            stats[k]["max_id"],
                                            "{} {}".format(round(min_v, 2), unit), 
                                            stats[k]["min_id"]
                                        ])
                                    
                                    print_center_table(
                                        output=out,
                                        data=summary_rows,
                                        columns=["Komponen", "Max (+)", "Elem Max", "Min (-)", "Elem Min"],
                                        title="📊 Ringkasan Analisis ({})".format(case_key)
                                    )

                                    # ===========================================================
                                    # D. TABEL DEFLEKSI MAKSIMUM (NEW)
                                    # ===========================================================
                                    deflection_data = []
                                    
                                    if 'elements' in results:
                                        for eid, val in results['elements'].items():
                                            try:
                                                revit_id_int = int(eid)
                                                revit_el_id = ElementId(revit_id_int)
                                                el = doc.GetElement(revit_el_id)
                                                
                                                if not el:
                                                    continue
                                                
                                                cat_id = el.Category.Id.IntegerValue
                                                if cat_id not in [int(BuiltInCategory.OST_StructuralColumns), 
                                                                  int(BuiltInCategory.OST_StructuralFraming)]:
                                                    continue
                                                
                                                elem_type = val.get('element_type', 'Unknown')
                                                elem_name_short = el.Name
                                                
                                                # Get max_deflection data
                                                max_defl = val.get('max_deflection', None)
                                                if max_defl:
                                                    dy_max = max_defl.get('delta_y_max_mm', 0.0)
                                                    dy_station = max_defl.get('delta_y_station', 0.0)
                                                    dy_dist = max_defl.get('delta_y_distance_mm', 0.0)
                                                    dz_max = max_defl.get('delta_z_max_mm', 0.0)
                                                    dz_station = max_defl.get('delta_z_station', 0.0)
                                                    dz_dist = max_defl.get('delta_z_distance_mm', 0.0)
                                                    
                                                    deflection_data.append([
                                                        str(eid),
                                                        elem_type,
                                                        elem_name_short,
                                                        "{:.4f}".format(dy_max),
                                                        "{:.3f}".format(dy_station),
                                                        "{:.0f}".format(dy_dist),
                                                        "{:.4f}".format(dz_max),
                                                        "{:.3f}".format(dz_station),
                                                        "{:.0f}".format(dz_dist)
                                                    ])
                                            except Exception:
                                                continue
                                    
                                    if deflection_data:
                                        deflection_data.sort(key=lambda x: int(x[0]))
                                        print_center_table(
                                            output=out,
                                            data=deflection_data,
                                            columns=["ID", "Type", "Section", "δy Max (mm)", "Station Y", "Dist Y (mm)", "δz Max (mm)", "Station Z", "Dist Z (mm)"],
                                            title="📐 Defleksi Maksimum Elemen ({})".format(case_key)
                                        )
                                        
                                        # Find overall max deflection
                                        max_dy_elem = max(deflection_data, key=lambda x: abs(float(x[3])))
                                        max_dz_elem = max(deflection_data, key=lambda x: abs(float(x[6])))
                                        out.print_md("**Defleksi Maksimum Overall:**")
                                        out.print_md("  - **δy max:** {} mm @ ID {} (station {}, dist {} mm)".format(
                                            max_dy_elem[3], max_dy_elem[0], max_dy_elem[4], max_dy_elem[5]))
                                        out.print_md("  - **δz max:** {} mm @ ID {} (station {}, dist {} mm)".format(
                                            max_dz_elem[6], max_dz_elem[0], max_dz_elem[7], max_dz_elem[8]))

                            else:
                                out.print_md("❌ **Analisis Gagal**")
                                out.print_md("**Pesan:** " + str(results.get("message", "Unknown Error")))

                        # ===========================================================
                        # SEISMIC ANALYSIS RESULTS (EQx & EQy)
                        # ===========================================================
                        seismic_cases = [
                            ("SeismicX", "EQx", "Fx", "Vx"),
                            ("SeismicY", "EQy", "Fy", "Vy"),
                        ]
                        
                        for s_key, s_dir, f_label, v_label in seismic_cases:
                            eq_data = all_results.get(s_key)
                            if not eq_data:
                                continue
                            
                            out.print_md("---")
                            out.print_md("## 🌊 BEBAN GEMPA — {} (SNI 1726 ELF)".format(s_dir))
                            
                            if eq_data.get('status') != 'Success':
                                out.print_md("> _Analisis gempa {} gagal._".format(s_dir))
                                continue
                            
                            sp = eq_data.get('seismic_parameters', {})
                            fd = eq_data.get('floor_data', [])
                            
                            # A. Parameter Table
                            param_rows = [
                                ["SDS", str(sp.get('SDS', '-'))],
                                ["SD1", str(sp.get('SD1', '-'))],
                                ["T (periode)", "{:.4f} s".format(sp.get('T', 0))],
                                ["Cs", "{:.6f}".format(sp.get('Cs', 0))],
                                ["R", str(sp.get('R', '-'))],
                                ["Ie", str(sp.get('Ie', '-'))],
                                ["W total", "{:.3f} kN".format(sp.get('W_total_kN', 0))],
                                [v_label, "{:.4f} kN".format(sp.get('V_kN', 0))],
                            ]
                            print_center_table(
                                output=out,
                                data=param_rows,
                                columns=["Parameter", "Nilai"],
                                title="Parameter Seismik ({})".format(s_dir)
                            )
                            
                            # B. Floor Force Distribution Table
                            floor_rows = []
                            fd_sorted = sorted(fd, key=lambda x: x['floor'], reverse=True)
                            for flr in fd_sorted:
                                floor_rows.append([
                                    str(flr['floor']),
                                    "{:.3f}".format(flr['Wi_kN']),
                                    "{:.3f}".format(flr['hi_m']),
                                    "{:.6f}".format(flr['Cvx']),
                                    "{:.4f}".format(flr['Fx_kN']),
                                ])
                            
                            # Add total row
                            sum_Fx = sum(f['Fx_kN'] for f in fd)
                            floor_rows.append([
                                "TOTAL",
                                "{:.3f}".format(sp.get('W_total_kN', 0)),
                                "-",
                                "1.000000",
                                "{:.4f}".format(sum_Fx),
                            ])
                            
                            print_center_table(
                                output=out,
                                data=floor_rows,
                                columns=["Lantai", "Wi (kN)", "hi (m)", "Cvx", "{} (kN)".format(f_label)],
                                title="Distribusi Gaya Lateral ({})".format(s_dir)
                            )
                            
                            # C. Reactions Table
                            eq_nodes = eq_data.get('nodes', {})
                            reac_rows = []
                            for nid, nval in eq_nodes.items():
                                reac = nval.get('reaction')
                                if reac:
                                    reac_rows.append([
                                        str(nid),
                                        "{:.2f}".format(reac.get('Fx', reac.get('F1', 0))),
                                        "{:.2f}".format(reac.get('Fy', reac.get('F2', 0))),
                                        "{:.2f}".format(reac.get('Fz', reac.get('F3', 0))),
                                    ])
                            
                            if reac_rows:
                                reac_rows.sort(key=lambda x: int(x[0]))
                                print_center_table(
                                    output=out,
                                    data=reac_rows,
                                    columns=["Node ID", "Fx (N)", "Fy (N)", "Fz (N)"],
                                    title="Reaksi Tumpuan ({})".format(s_dir)
                                )
                            
                            # D. Equilibrium check
                            eq_res = eq_data.get('summary', {}).get('equilibrium_residual_N', 0)
                            V_N = sp.get('V_kN', 0) * 1000.0
                            ratio_pct = abs(eq_res / V_N * 100) if V_N > 0 else 0
                            out.print_md("**Equilibrium:** |Sum(R) - V| = {:.2f} N ({:.4f}%)".format(eq_res, ratio_pct))

                except Exception as e:
                    out.print_md("## ❌ Gagal Membaca Output JSON")
                    out.print_md(str(e))
            else:
                TaskDialog.Show("Error", "File output Analysis.json tidak terbentuk.")
            
    except Exception as e:
        TaskDialog.Show("System Error", str(e))
