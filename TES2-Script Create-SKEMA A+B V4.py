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

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilySymbol, Material, Level, View3D,
    ElementId, XYZ, Line, Transaction, ElementTransformUtils,
    BuiltInCategory, BuiltInParameter, StorageType,
    FamilyInstance, Family, LocationCurve, LocationPoint,
    Category, Options, ElementMulticategoryFilter,
    Grid, DatumExtentType
)
from Autodesk.Revit.DB.Structure import StructuralType, StructuralFramingUtils, AnalyticalCurveType
from Autodesk.Revit.DB.Structure import TranslationRotationValue, AnalyticalModelSelector
from Autodesk.Revit.UI import TaskDialog
from pyrevit import script, HOST_APP, revit # Added revit import

# Get active document (compatible with all pyRevit versions)
doc = HOST_APP.doc

# ╔═══════════════════════════════════════════════════════════════╗
# ║              INPUT PARAMETER  (USER SETUP)                    ║
# ╚═══════════════════════════════════════════════════════════════╝

# ═══════════════════════════════════════════════════════════════
# 1. PATH KONFIGURASI
# ═══════════════════════════════════════════════════════════════

OUTPUT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Model data.json"

# Python 3 executable (harus sudah install openseespy/numpy)
PYTHON_EXE_PATH = r"C:\\Users\\hp\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
# Script analisis eksternal
ANALYSIS_SCRIPT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Analysis\\Analysis.py"

# Revit family lookup tables & RFA
BEAM_TXT_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Steel\AISC 15.0\M_W Shapes.txt"
COL_TXT_PATH  = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Steel\AISC 15.0\M_W Shapes-Column.txt"
BEAM_RFA_PATH = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Steel\AISC 15.0\M_W Shapes.rfa"
COL_RFA_PATH  = r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Steel\AISC 15.0\M_W Shapes-Column.rfa"

# (auto-derived)
MERGED_RESULT_PATH = os.path.join(os.path.dirname(OUTPUT_PATH), "Result.json")
ANALYSIS_JSON_PATH = os.path.join(os.path.dirname(OUTPUT_PATH), "Analysis", "Analysis.json")

# ═══════════════════════════════════════════════════════════════
# 2. GEOMETRI STRUKTUR
# ═══════════════════════════════════════════════════════════════

N_STORY     = 2          # Jumlah lantai
BAY_X_COUNT = 2         # Jumlah bay arah X
BAY_Y_COUNT = 2          # Jumlah bay arah Y
SPAN_X_MM   = 4000       # Bentang X (mm)
SPAN_Y_MM   = 6000       # Bentang Y (mm)
HEIGHT_MM   = 4000       # Tinggi per lantai (mm)

COLUMN_ROTATION_DEG          = 0    # Rotasi kolom (derajat)
BEAM_ANALYTICAL_ROTATION_DEG = 0    # Rotasi analytical beam (derajat)
JOIN_STATUS                  = True  # True = Allow Join, False = Disallow

# Fabrication kolom
COL_FAB_MAX_LENGTH_MM    = 12000   # Panjang maks kolom fabrikasi (mm)
COL_SPLICE_OFFSET_MM     = 1500    # Offset sambungan di atas level (mm)
COL_TOP_OFFSET_MM        = 500     # Offset di level teratas bangunan (mm)
COL_MIN_DIST_TO_LEVEL_MM = 2000    # Jarak minimum splice ke level (mm)

# Grid (auto-generated)
GRID_X_LABELS      = [str(i+1) for i in range(BAY_X_COUNT + 1)]   # ["1","2",...]
GRID_Y_LABELS      = [chr(65+i) for i in range(BAY_Y_COUNT + 1)]  # ["A","B",...]
CREATE_REVIT_GRIDS = True

_GRID_X_COORDS_MM = [int(round(-(BAY_X_COUNT * SPAN_X_MM / 2.0) + i * SPAN_X_MM))
                     for i in range(BAY_X_COUNT + 1)]
_GRID_Y_COORDS_MM = [int(round(-(BAY_Y_COUNT * SPAN_Y_MM / 2.0) + j * SPAN_Y_MM))
                     for j in range(BAY_Y_COUNT + 1)]
_Z_LEVELS_MM = [k * int(HEIGHT_MM) for k in range(int(N_STORY) + 1)]

# ═══════════════════════════════════════════════════════════════
# 3. PENAMPANG (SECTION) & MATERIAL — ASSIGNMENT PER GROUP
# ═══════════════════════════════════════════════════════════════
#
#   User mengatur penampang dan material per group di sini.
#   Group yang tersedia:
#     - "Kolom"       : Semua kolom
#     - "Balok Induk"  : Balok eksterior + interior (1 profil)
#     - "Balok Anak"   : Balok sekunder / secondary beam
#
# ───────────────────────────────────────────────────────────────

# Database IWF — format: {nama: {d, bf, tw, tf, r}} (mm)
SECTIONS = {
    "IWF200x100x5.5x8":        {"d": 200,   "bf": 100,   "tw": 5.5, "tf": 8,    "r": 11},
    "IWF303.4x165x6x10.2":     {"d": 303.4, "bf": 165,   "tw": 6,   "tf": 10.2, "r": 8.9},
    "IWF307.9x305.3x9.9x15.4": {"d": 307.9, "bf": 305.3, "tw": 9.9, "tf": 15.4, "r": 15.2},
}

#UNTUK_SAMBUNGAN_BAJA — Fy, Fu material untuk kapasitas baut, pelat, dan las sambungan
# Database material — {Fy, Fu, E (MPa), Nu, Rho (kg/m3), thermal}
MATERIALS = {
    "BJ 37": {"Fy": 240, "Fu": 370, "E": 200000, "Nu": 0.3, "Rho_kg_m3": 7850,
              "thermal_conductivity": 45.3, "specific_heat": 480},
    "BJ 41": {"Fy": 250, "Fu": 410, "E": 200000, "Nu": 0.3, "Rho_kg_m3": 7850,
              "thermal_conductivity": 45.3, "specific_heat": 480},
}

# ── NAMA GROUP (ubah nama group di sini) ─────────────────────
# Nama ini menjadi satu-satunya titik kontrol untuk seluruh
# pipeline: Create → Analysis → Design → Auto Select.
GRP_KOLOM       = "Kolom"
GRP_BALOK_INDUK = "Balok Induk"
GRP_BALOK_ANAK  = "Balok Anak"

# ── ASSIGNMENT PER GROUP ──────────────────────────────────────
# Ubah section dan material sesuai kebutuhan desain.
# Section harus terdaftar di SECTIONS di atas.
# Material harus terdaftar di MATERIALS di atas.

GROUP_ASSIGNMENT = {
    GRP_KOLOM: {
        "section":  "IWF307.9x305.3x9.9x15.4",
        "material": "BJ 41",
    },
    GRP_BALOK_INDUK: {
        "section":  "IWF303.4x165x6x10.2",
        "material": "BJ 41",
    },
    GRP_BALOK_ANAK: {
        "enabled":         True,        # True = aktif di seluruh pipeline
        "section":         "IWF200x100x5.5x8",
        "material":        "BJ 41",
        "count_per_bay_x": 1,           # Jumlah balok anak arah X per bay
        "count_per_bay_y": 0,           # Jumlah balok anak arah Y per bay
        "floors":          "all",        # "all" atau list, misal [1, 2]
        "release":         False,         # True = sendi (M3 release) di kedua ujung
    },
}

# ── Derived constants (jangan diubah) ─────────────────────────
SECTION_COL      = GROUP_ASSIGNMENT[GRP_KOLOM]["section"]
SECTION_BEAM_EXT = GROUP_ASSIGNMENT[GRP_BALOK_INDUK]["section"]
SECTION_BEAM_INT = GROUP_ASSIGNMENT[GRP_BALOK_INDUK]["section"]
MATERIAL_COL      = GROUP_ASSIGNMENT[GRP_KOLOM]["material"]
MATERIAL_BEAM_EXT = GROUP_ASSIGNMENT[GRP_BALOK_INDUK]["material"]
MATERIAL_BEAM_INT = GROUP_ASSIGNMENT[GRP_BALOK_INDUK]["material"]
SECONDARY_BEAM_CONFIG = GROUP_ASSIGNMENT[GRP_BALOK_ANAK]

# ═══════════════════════════════════════════════════════════════
# 5. TUMPUAN (BOUNDARY CONDITIONS)
# ═══════════════════════════════════════════════════════════════

# Tipe tumpuan dasar kolom: "Fixed" | "Pinned" | "Roller"
SUPPORT_TYPE = "Fixed"

# ═══════════════════════════════════════════════════════════════
# 6. METODE ANALISIS (AISC 360-22)
# ═══════════════════════════════════════════════════════════════

# Pilih metode analisis desain:
#   "ELM" = Effective Length Method (K dari nomogram, EI nominal dalam analisis)
#   "DAM" = Direct Analysis Method  (K = 1.0, 0.8*tau_b*EI dalam analisis)
ANALYSIS_METHOD = "ELM"

# (auto-resolved)
_SUPPORT_DOF_MAP = {
    "Fixed":  [1, 1, 1, 1, 1, 1],
    "Pinned": [1, 1, 1, 0, 0, 0],
    "Roller": [0, 0, 1, 0, 0, 0],
}
SUPPORT_DOF = _SUPPORT_DOF_MAP.get(SUPPORT_TYPE, [1, 1, 1, 1, 1, 1])

# ═══════════════════════════════════════════════════════════════
# 6. BEBAN (LOADS)
# ═══════════════════════════════════════════════════════════════

# --- Beban Pelat Lantai ---
SLAB_THICKNESS     = 150.0    # Tebal slab beton (mm)
SLAB_ADD_THICKNESS = 30.0     # Tebal spesi/finishing (mm)
LIVE_LOAD_PRESSURE = 0.0024    # Beban hidup (MPa) = 24 kN/m2

# Konstanta perhitungan
CONCRETE_UNIT_WEIGHT_kN_m3 = 24.0    # kN/m3
MORTAR_WEIGHT_kg_m2_cm     = 21.0    # kg/m2/cm
GRAVITY_m_s2               = 9.81    # m/s2

# (auto-computed pressures)
slab_thickness_m  = SLAB_THICKNESS / 1000.0
SW_kN_m2          = slab_thickness_m * CONCRETE_UNIT_WEIGHT_kN_m3
SLAB_SW_PRESSURE  = round(SW_kN_m2 * 0.001, 5)                          # MPa

add_thickness_cm  = SLAB_ADD_THICKNESS / 10.0
ADL_kg_m2         = add_thickness_cm * MORTAR_WEIGHT_kg_m2_cm
ADL_kN_m2         = ADL_kg_m2 * GRAVITY_m_s2 / 1000.0
SLAB_ADL_PRESSURE = round(ADL_kN_m2 * 0.001, 5)                         # MPa

# --- Shell Plate Slab (opsional) ---
SLAB_PLATE_ENABLED      = False
SLAB_PLATE_E_MPA        = 205000.0    # MPa
SLAB_PLATE_NU           = 0.3
SLAB_PLATE_RHO_KG_M3    = 7156.44     # kg/m3
SLAB_PLATE_MESH_SIZE_MM = 250         # mm

# --- Load Patterns ---
# EQx/EQy otomatis dari seismic_parameters, tidak perlu di sini.
LOAD_PATTERNS = {
    "SelfWeight": {
        "type": "Dead",
        "self_weight_mult": 1,
        "pressure_MPa": 0.0 if SLAB_PLATE_ENABLED else SLAB_SW_PRESSURE
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

# ═══════════════════════════════════════════════════════════════
# 7. KOMBINASI BEBAN
# ═══════════════════════════════════════════════════════════════

# Mode: "default" = 10 DSTL SNI 1727-2020 | "custom" | "both"
LOAD_COMBO_MODE = "both"

# Custom: {"NamaKombo": {"Pattern": factor, ...}}
CUSTOM_LOAD_COMBOS = {
    "COMB1": {"SelfWeight": 1.0, "ADL": 1.0},
    "COMB2": {"SelfWeight": 1.0, "ADL": 1.0, "LIVE": 1.0},
    # "COMB3": {"SelfWeight": 1.2, "ADL": 1.2, "LIVE": 0.5, "EQx": 1.3},
}

# ═══════════════════════════════════════════════════════════════
# 8. PARAMETER GEMPA (SNI 1726)
# ═══════════════════════════════════════════════════════════════

# Spektral percepatan
SITE_CLASS = "SC"            # Kelas situs: SA, SB, SC, SD, SE
SS  = 1.0821                 # MCE_R (T=0.2s)
S1  = 0.4896                 # MCE_R (T=1.0s)
TL  = 20                     # Periode transisi panjang (detik)
# SDS dan SD1 dihitung otomatis dari Fa, Fv, SS, S1 (SNI 1726 Pers. 7-10)

# Waktu getar alami — konstan untuk gedung baja MRF (SNI 1726 Tabel 17)
Ct   = 0.0724
x_Ta = 0.8

# --- Tipe Gedung → Kategori Risiko & Ie (SNI 1726-2019 Tabel 3 & 4) ---
# Pilih salah satu tipe gedung di bawah:
TIPE_GEDUNG = "Gedung Perkantoran"

# Mapping tipe gedung → kategori risiko  (SNI 1726-2019 Tabel 3)
_TIPE_GEDUNG_TO_RISK = {
    # Kategori Risiko I
    "Fasilitas Pertanian":        "I",
    "Fasilitas Sementara":        "I",
    "Gudang Penyimpanan":         "I",
    "Rumah Jaga":                 "I",
    # Kategori Risiko II
    "Perumahan":                  "II",
    "Rumah Toko":                 "II",
    "Pasar":                      "II",
    "Gedung Perkantoran":         "II",
    "Gedung Apartemen":           "II",
    "Pusat Perbelanjaan/Mall":    "II",
    "Bangunan Industri":          "II",
    "Fasilitas Manufaktur":       "II",
    "Rumah Makan/Restoran":       "II",
    # Kategori Risiko III
    "Bioskop":                    "III",
    "Gedung Pertemuan":           "III",
    "Stadion":                    "III",
    "Fasilitas Kesehatan":        "III",   # tanpa bedah/UGD
    "Fasilitas Penitipan Anak":   "III",
    "Penjara":                    "III",
    "Bangunan Orang Jompo":       "III",
    "Pusat Pembangkit Listrik":   "III",
    "Fasilitas Penanganan Air":   "III",
    "Fasilitas Penanganan Limbah":"III",
    "Pusat Telekomunikasi":       "III",
    # Kategori Risiko IV
    "Bangunan Monumental":        "IV",
    "Gedung Sekolah":             "IV",
    "Rumah Ibadah":               "IV",
    "Rumah Sakit":                "IV",    # dengan bedah/UGD
    "Fasilitas Pemadam Kebakaran":"IV",
    "Tempat Perlindungan Darurat":"IV",
    "Fasilitas Kesiapan Darurat": "IV",
    "Pusat Pembangkit Energi Darurat": "IV",
}

# Mapping kategori risiko → Ie  (SNI 1726-2019 Tabel 4)
_RISK_TO_Ie = {"I": 1.0, "II": 1.0, "III": 1.25, "IV": 1.5}

# Auto-resolve
if TIPE_GEDUNG not in _TIPE_GEDUNG_TO_RISK:
    raise ValueError(
        "TIPE_GEDUNG '{}' tidak dikenali. Pilihan: {}".format(
            TIPE_GEDUNG, list(_TIPE_GEDUNG_TO_RISK.keys())))
RISK_CATEGORY = _TIPE_GEDUNG_TO_RISK[TIPE_GEDUNG]
Ie = _RISK_TO_Ie[RISK_CATEGORY]

# Tipe rangka: "SRPMB/OMF" atau "SRPMK/SMF"
FRAME_TYPE = "SRPMK/SMF"

FRAME_CONFIG = {
    "SRPMB/OMF": {
        "R": 3.5, "Cd": 3.0, "Omega_0": 3.0,
        "Ry": 1.0,
        "rho": 1.0,
        "sdc_allowed": ["A", "B", "C"],
        "sdc_disclaimer": ["D", "E"],
        "sdc_prohibited": ["F"],
    },
    "SRPMK/SMF": {
        "R": 8.0, "Cd": 5.5, "Omega_0": 3.0,
        "Ry": 1.1,
        "rho": 1.0,
        "sdc_allowed": ["A", "B", "C", "D", "E", "F"],
        "sdc_disclaimer": [],
        "sdc_prohibited": [],
    }
}

# (auto-assigned)
_cfg = FRAME_CONFIG[FRAME_TYPE]
R  = _cfg["R"]
Cd = _cfg["Cd"]
Omega_0 = _cfg["Omega_0"]
Ry = _cfg["Ry"]
rho = _cfg["rho"]

# --- SDC Compatibility Check ---
def get_sdc(SDS_val, SD1_val, risk_cat):
    """
    Tentukan Kategori Desain Seismik (SDC) per SNI 1726 Tabel 8 & 9.
    Return: SDC string ("A", "B", "C", "D", "E", "F")
    """
    is_cat_IV = (risk_cat == "IV")
    # Tabel 8 - berdasarkan SDS
    if SDS_val < 0.167:
        sdc_sds = "A"
    elif SDS_val < 0.33:
        sdc_sds = "C" if is_cat_IV else "B"
    elif SDS_val < 0.50:
        sdc_sds = "D" if is_cat_IV else "C"
    else:
        sdc_sds = "D"
    # Tabel 9 - berdasarkan SD1
    if SD1_val < 0.067:
        sdc_sd1 = "A"
    elif SD1_val < 0.133:
        sdc_sd1 = "C" if is_cat_IV else "B"
    elif SD1_val < 0.20:
        sdc_sd1 = "D" if is_cat_IV else "C"
    else:
        sdc_sd1 = "D"
    # Governing = yang lebih berat
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}
    return sdc_sds if order.get(sdc_sds, 0) >= order.get(sdc_sd1, 0) else sdc_sd1

def check_sdc_compatibility(sdc, frame_type):
    """
    Cek kompatibilitas sistem rangka dengan SDC menggunakan FRAME_CONFIG.
    Return: (is_compatible, message)
    """
    cfg = FRAME_CONFIG.get(frame_type, {})
    if sdc in cfg.get("sdc_allowed", []):
        return (True, "OK - {} sesuai untuk SDC {}".format(frame_type, sdc))
    elif sdc in cfg.get("sdc_disclaimer", []):
        return (False,
            "DISCLAIMER: Sistem {} tidak sesuai untuk SDC {}. "
            "Ketentuan SNI 7.2.5.6 harus dipenuhi. "
            "Analisis tetap dijalankan untuk referensi.".format(frame_type, sdc))
    elif sdc in cfg.get("sdc_prohibited", []):
        return (False,
            "DISCLAIMER: {} tidak diizinkan untuk SDC {}. "
            "Analisis tetap dijalankan untuk referensi.".format(frame_type, sdc))
    return (True, "OK")


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

# --- Hitung variabel turunan (SNI 1726 Pers. 7-10) ---
Fa  = get_Fa(SITE_CLASS, SS)
Fv  = get_Fv(SITE_CLASS, S1)
SMS = Fa * SS                   # Pers. 7
SM1 = Fv * S1                   # Pers. 8
SDS = round((2.0 / 3.0) * SMS, 2)  # Pers. 9
SD1 = round((2.0 / 3.0) * SM1, 2)  # Pers. 10
T0  = 0.2 * (SD1 / SDS)
Ts_period = SD1 / SDS
TOTAL_HEIGHT_M = N_STORY * HEIGHT_MM / 1000.0
Ta = Ct * (TOTAL_HEIGHT_M ** x_Ta)

# --- SDC check ---
SDC = get_sdc(SDS, SD1, RISK_CATEGORY)
SDC_IS_COMPATIBLE, SDC_MESSAGE = check_sdc_compatibility(SDC, FRAME_TYPE)
if not SDC_IS_COMPATIBLE:
    print("=" * 60)
    print("  {}".format(SDC_MESSAGE))
    print("=" * 60)

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
# DETERMINE GROUP
#UNTUK_SAMBUNGAN_BAJA — Klasifikasi Kolom/Balok Induk/Balok Anak menentukan tipe sambungan (moment EP/clip angle/splice)
# ===================================================
def determine_group(element):
    """Deteksi group berdasarkan element ID yang di-track saat creation."""
    try:
        el_id = element.Id.IntegerValue
        if el_id in _ELEMENT_GROUPS:
            return _ELEMENT_GROUPS[el_id]
    except:
        pass

    # Fallback: deteksi dari category
    try:
        cat_id = element.Category.Id.IntegerValue
        if cat_id == int(BuiltInCategory.OST_StructuralColumns):
            return GRP_KOLOM
        return GRP_BALOK_INDUK
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
#UNTUK_SAMBUNGAN_BAJA — Ekstraksi Fy, Fu, E dari elemen Revit untuk desain kapasitas sambungan (AISC J)
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
    
    # === OVERRIDE: Gunakan nama material dari GROUP_ASSIGNMENT ===
    # Ini mengatasi kasus dimana Revit material param tidak berubah ke BJ 41
    elem_id = element.Id.IntegerValue
    if elem_id in _ELEMENT_GROUPS:
        grp = _ELEMENT_GROUPS[elem_id]
        assigned_mat = GROUP_ASSIGNMENT.get(grp, {}).get("material", MATERIAL_COL)
        
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

#UNTUK_SAMBUNGAN_BAJA — Dimensi section (d, bf, tw, tf, r) untuk sizing pelat, baut, stiffener, dan filler plate
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
                if group in GROUP_ASSIGNMENT:
                    custom_section_name = GROUP_ASSIGNMENT[group].get("section", "")
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

#UNTUK_SAMBUNGAN_BAJA — Koordinat node (start/end) untuk deteksi lokasi joint dan node_map
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
                # PENTING: Untuk analisis OpenSees, gunakan elevasi level TANPA offset fabrikasi
                # Offset hanya untuk model fisik Revit, bukan model analitis
                z_s, z_e = 0.0, 0.0
                
                # Base Level (tanpa offset — analisis menggunakan level murni)
                p_base = element.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
                if p_base:
                    lvl = doc.GetElement(p_base.AsElementId())
                    if lvl: z_s = lvl.Elevation

                # Top Level (tanpa offset — analisis menggunakan level murni)
                p_top = element.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
                if p_top:
                    lvl = doc.GetElement(p_top.AsElementId())
                    if lvl: z_e = lvl.Elevation

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
#UNTUK_SAMBUNGAN_BAJA — Orientasi lokal elemen (web/flange direction) untuk klasifikasi tipe sambungan
# ===================================================

def get_local_axes(element, doc):
    """
    Extract/Construct local coordinate system.
    For Columns: Uses COLUMN_ROTATION_DEG to manually construct rotated axes (Right-Hand Rule).
    For Beams: Use Revit default axes + read analytical CrossSectionRotation.
    
    Revit Default Beam Axes:
      X = Along beam (longitudinal)
      Y = Horizontal (flange width direction, MINOR axis)
      Z = Vertical (web depth direction, MAJOR axis)
    """
    local_axes = {
        "x_axis": [1.0, 0.0, 0.0],
        "y_axis": [0.0, 1.0, 0.0],
        "z_axis": [0.0, 0.0, 1.0],
    }
    
    try:
        cat_id = element.Category.Id.IntegerValue
        
        if cat_id == int(BuiltInCategory.OST_StructuralFraming):
            # BEAM: Use Revit default axes (no -90° rotation)
            transform = element.GetTransform()
            
            # Revit Beam default basis:
            # X = Longitudinal (along beam)
            # Y = Horizontal (flange width, minor axis)
            # Z = Vertical (web depth, major axis)
            orig_x = [transform.BasisX.X, transform.BasisX.Y, transform.BasisX.Z]
            orig_y = [transform.BasisY.X, transform.BasisY.Y, transform.BasisY.Z]
            orig_z = [transform.BasisZ.X, transform.BasisZ.Y, transform.BasisZ.Z]
            
            # Read analytical CrossSectionRotation (user override)
            rot_rad = 0.0
            try:
                am = element.GetAnalyticalModel()
                if am is not None:
                    rot_rad = am.CrossSectionRotation
            except:
                pass
            
            # Apply rotation about x-axis if non-zero
            if abs(rot_rad) > 0.001:
                cos_r = math.cos(rot_rad)
                sin_r = math.sin(rot_rad)
                new_y = [cos_r*orig_y[i] + sin_r*orig_z[i] for i in range(3)]
                new_z = [-sin_r*orig_y[i] + cos_r*orig_z[i] for i in range(3)]
            else:
                new_y = orig_y
                new_z = orig_z
            
            local_axes["x_axis"] = [round(v, 6) for v in orig_x]
            local_axes["y_axis"] = [round(v, 6) for v in new_y]
            local_axes["z_axis"] = [round(v, 6) for v in new_z]
            
            # Web = depth direction = Z axis (Revit default)
            local_axes["web_direction"] = "z_axis"
                
        elif cat_id == int(BuiltInCategory.OST_StructuralColumns):
            # COLUMN: Manual Construction based on COLUMN_ROTATION_DEG
            # Baseline (0 deg): Local X=Vert, Local Y=Global X, Local Z=Global Y
            # Right-Hand Rule Rotation about Vertical Axis (Z)
            
            theta_rad = math.radians(COLUMN_ROTATION_DEG)
            cos_t = math.cos(theta_rad)
            sin_t = math.sin(theta_rad)
            
            # Local Y (was Global X, rotated)
            ly_x = cos_t
            ly_y = sin_t
            ly_z = 0.0
            
            # Local Z (was Global Y, rotated)
            lz_x = -sin_t
            lz_y = cos_t
            lz_z = 0.0
            
            local_axes["x_axis"] = [0.0, 0.0, 1.0]  # Always Vertical
            local_axes["y_axis"] = [round(ly_x, 6), round(ly_y, 6), round(ly_z, 6)]
            local_axes["z_axis"] = [round(lz_x, 6), round(lz_y, 6), round(lz_z, 6)]

    except Exception as e:
        pass
    
    return local_axes

# ===================================================
# AISC 360-22 DESIGN PARAMETERS (SRPMB/OMF)
# Section classification dihitung di Steel Design Engine.
# ===================================================


def get_design_parameters(element_type, topology):
    """
    Generate design parameters for AISC 360-22 checks.
    SRPMB/OMF: K dihitung di Engine via nomogram (sidesway uninhibited).
    Cb dihitung di Engine per load combination.
    
    Args:
        element_type: "Column" or "Beam"
        topology: Dictionary from get_topology_ref()
    
    Returns:
        Dictionary with unbraced lengths dan metadata.
        K factor dihitung di Engine menggunakan nomogram.
    """
    length_mm = topology.get("length_mm", 0)
    
    # Unbraced lengths - konservatif (panjang penuh elemen)
    # Bisa diperhalus berdasarkan titik bracing lateral
    Lx_mm = length_mm  # Strong-axis unbraced length
    Ly_mm = length_mm  # Weak-axis unbraced length
    Lb_mm = length_mm  # Lateral-torsional buckling length (beam)
    
    return {
        "Lx_mm": Lx_mm,
        "Ly_mm": Ly_mm,
        "Lb_mm": Lb_mm,
        "Lcz_mm": Ly_mm,     # Torsional buckling length (konservatif = Ly)
        "Kz": 1.0,          # Torsional K factor (default)
        "frame_type": "SRPMB/OMF",
        "end_condition": "fixed-fixed"
    }


# ===================================================
# ELEMENT DATA
# ===================================================

# ===================================================
# GRID SYSTEM: LABEL & REVIT GRID CREATION
# ===================================================

def assign_label_name(elem_data, x_coords_mm, y_coords_mm, z_levels_mm,
                      grid_x_labels, grid_y_labels, tol=15.0):
    """
    Map koordinat topologi elemen ke label_name grid.

    Format:
      Kolom         : "{gridY}-{gridX}/{floor}"       e.g. "A-1/1"
      Balok arah X  : "{gridY}/{gridX_s}-{gridX_e}/{floor}"  e.g. "A/1-2/1"
      Balok arah Y  : "{gridY_s}-{gridY_e}/{gridX}/{floor}"  e.g. "A-B/1/2"

    Args:
        elem_data      : dict elemen (type, topology)
        x_coords_mm    : list koordinat X grid dalam mm
        y_coords_mm    : list koordinat Y grid dalam mm
        z_levels_mm    : list elevasi lantai dalam mm
        grid_x_labels  : list label X ["1","2","3",...]
        grid_y_labels  : list label Y ["A","B","C",...]
        tol            : toleransi snap (mm)
    """
    def snap_idx(val, coords):
        best_i, best_d = -1, tol + 1
        for i, c in enumerate(coords):
            d = abs(float(val) - float(c))
            if d < best_d:
                best_d, best_i = d, i
        return best_i if best_d <= tol else -1

    def bracket_idx(val, coords):
        """Cari 2 grid yang mengapit nilai val. Return (idx_lower, idx_upper)."""
        fv = float(val)
        lower_i, upper_i = -1, -1
        for i, c in enumerate(coords):
            fc = float(c)
            if fc <= fv + tol:
                if lower_i == -1 or fc > float(coords[lower_i]):
                    lower_i = i
            if fc >= fv - tol:
                if upper_i == -1 or fc < float(coords[upper_i]):
                    upper_i = i
        return (lower_i, upper_i)

    def label_at(idx, labels):
        return labels[idx] if 0 <= idx < len(labels) else "?"

    def floor_num(z_val):
        idx = snap_idx(z_val, z_levels_mm)
        return max(idx, 0)

    topo = elem_data.get("topology", {})
    start = topo.get("start_node", [0, 0, 0])
    end   = topo.get("end_node",   [0, 0, 0])
    sx, sy, sz = float(start[0]), float(start[1]), float(start[2])
    ex, ey, ez = float(end[0]),   float(end[1]),   float(end[2])

    elem_type = elem_data.get("type", "")

    if elem_type == "Column":
        xi = snap_idx(sx, x_coords_mm)
        yi = snap_idx(sy, y_coords_mm)
        fl = floor_num(ez)
        gx = label_at(xi, grid_x_labels)
        gy = label_at(yi, grid_y_labels)
        return "{}-{}/{}".format(gy, gx, fl)

    elif elem_type == "Beam":
        fl = floor_num(sz)
        dx = abs(ex - sx)
        dy = abs(ey - sy)

        if dx >= dy:
            # Arah X (X bervariasi, Y konstan)
            yi   = snap_idx(sy, y_coords_mm)
            xi_s = snap_idx(sx, x_coords_mm)
            xi_e = snap_idx(ex, x_coords_mm)
            if xi_s > xi_e:
                xi_s, xi_e = xi_e, xi_s
            gx_s = label_at(xi_s, grid_x_labels)
            gx_e = label_at(xi_e, grid_x_labels)

            if yi >= 0:
                # Balok utama: tepat di grid Y
                gy = label_at(yi, grid_y_labels)
                return "{}/{}-{}/{}".format(gy, gx_s, gx_e, fl)
            else:
                # Balok anak: di antara 2 grid Y
                lo, hi = bracket_idx(sy, y_coords_mm)
                gy_lo = label_at(lo, grid_y_labels)
                gy_hi = label_at(hi, grid_y_labels)
                return "{}-{}/{}-{}/{}".format(gy_lo, gy_hi, gx_s, gx_e, fl)
        else:
            # Arah Y (Y bervariasi, X konstan)
            xi   = snap_idx(sx, x_coords_mm)
            yi_s = snap_idx(sy, y_coords_mm)
            yi_e = snap_idx(ey, y_coords_mm)
            if yi_s > yi_e:
                yi_s, yi_e = yi_e, yi_s
            gy_s = label_at(yi_s, grid_y_labels)
            gy_e = label_at(yi_e, grid_y_labels)

            if xi >= 0:
                # Balok utama: tepat di grid X
                gx = label_at(xi, grid_x_labels)
                return "{}-{}/{}/{}".format(gy_s, gy_e, gx, fl)
            else:
                # Balok anak: di antara 2 grid X
                lo, hi = bracket_idx(sx, x_coords_mm)
                gx_lo = label_at(lo, grid_x_labels)
                gx_hi = label_at(hi, grid_x_labels)
                return "{}-{}/{}-{}/{}".format(gy_s, gy_e, gx_lo, gx_hi, fl)

    else:
        return "?-?/?"


def create_revit_grids(doc, x_coords_mm, y_coords_mm, grid_x_labels, grid_y_labels,
                       extra_extend_ft=7.0):
    """
    Buat Revit Grid elements dari koordinat grid (dalam mm).
      - Grid vertikal (label angka "1","2",...) : garis di X=konstan, memanjang arah Y
      - Grid horizontal (label huruf "A","B",...): garis di Y=konstan, memanjang arah X

    Harus dipanggil di dalam Transaction yang aktif.

    Langkah internal:
      1. Rename + delete semua grid yang masih ada (unpin dulu) → bebaskan namespace
      2. Buat grid baru dari koordinat yang diberikan
      3. Set nama dengan guard 'if g.Name != label' untuk menghindari konflik auto-name
    """
    if not x_coords_mm or not y_coords_mm:
        print("  ⚠️ Grid: koordinat kosong, dilewati")
        return

    # --- 1. Bersihkan grid yang masih ada ---
    existing_grids = (FilteredElementCollector(doc)
                      .OfCategory(BuiltInCategory.OST_Grids)
                      .WhereElementIsNotElementType()
                      .ToElements())
    if existing_grids:
        # Unpin + rename ke temp name agar namespace bebas
        for idx, eg in enumerate(existing_grids):
            try:
                eg.Pinned = False
            except Exception:
                pass
            try:
                eg.Name = "__roida_tmp_{}__".format(idx)
            except Exception:
                pass
        # Hapus
        del_ids = List[ElementId]()
        for eg in existing_grids:
            del_ids.Add(eg.Id)
        try:
            doc.Delete(del_ids)
            print("  Cleaned {} existing grid(s)".format(del_ids.Count))
        except Exception as e_del:
            print("  ⚠️ Grid cleanup warning: {}".format(str(e_del)))

    # --- 2. Hitung batas ekstensi (dalam ft) ---
    x_ft_list = [mm_to_ft(x) for x in x_coords_mm]
    y_ft_list  = [mm_to_ft(y) for y in y_coords_mm]

    min_x = min(x_ft_list) - extra_extend_ft
    max_x = max(x_ft_list) + extra_extend_ft
    min_y = min(y_ft_list) - extra_extend_ft
    max_y = max(y_ft_list) + extra_extend_ft

    created = 0

    # Kumpulkan semua 3D view untuk set DatumExtentType.Model
    _views_3d = [v for v in FilteredElementCollector(doc).OfClass(View3D).ToElements()
                 if not v.IsTemplate]

    def _set_grid_extent(g, creation_line):
        """Extend grid curve di semua 3D view via SetCurveInView agar menembus garis level."""
        try:
            sp = creation_line.GetEndPoint(0)
            ep = creation_line.GetEndPoint(1)
            d  = (ep - sp).Normalize()
            ext = 50.0  # feet — cukup jauh agar menembus datum level
            new_line = Line.CreateBound(sp - d * ext, ep + d * ext)
            for _v in _views_3d:
                try:
                    g.SetCurveInView(DatumExtentType.Model, _v, new_line)
                except Exception:
                    pass
        except Exception as e_ext:
            print("  ⚠️ Grid SetCurveInView error: {}".format(str(e_ext)))

    # --- 3a. Grid vertikal: di X=konstan, label angka ---
    for x_ft, label in zip(x_ft_list, grid_x_labels):
        try:
            line = Line.CreateBound(XYZ(x_ft, min_y, 0.0), XYZ(x_ft, max_y, 0.0))
            g = Grid.Create(doc, line)
            # Guard: hindari exception "name already in use" jika Revit auto-assign sama
            if g.Name != label:
                g.Name = label
            _set_grid_extent(g, line)
            created += 1
        except Exception as e_g:
            print("  ⚠️ Grid X='{}' error: {}".format(label, str(e_g)))

    # --- 3b. Grid horizontal: di Y=konstan, label huruf ---
    for y_ft, label in zip(y_ft_list, grid_y_labels):
        try:
            line = Line.CreateBound(XYZ(min_x, y_ft, 0.0), XYZ(max_x, y_ft, 0.0))
            g = Grid.Create(doc, line)
            if g.Name != label:
                g.Name = label
            _set_grid_extent(g, line)
            created += 1
        except Exception as e_g:
            print("  ⚠️ Grid Y='{}' error: {}".format(label, str(e_g)))

    print("  ✅ Grid elements: {} dibuat".format(created))


#UNTUK_SAMBUNGAN_BAJA — Integrasi semua data elemen (section + material + topology + axes) → dikonsumsi Connection Engine
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

        # AISC 360-22 Design Parameters (SRPMB/OMF)
        # K factor dan Cb dihitung di Engine, section classification juga di Engine
        "design_parameters": get_design_parameters(elem_type_str, topology_data)
    }

    # 4b. Hitung label_name (grid label) — pakai koordinat global yang sudah dihitung
    try:
        data["label_name"] = assign_label_name(
            data,
            _GRID_X_COORDS_MM, _GRID_Y_COORDS_MM, _Z_LEVELS_MM,
            GRID_X_LABELS, GRID_Y_LABELS
        )
    except Exception as e_lbl:
        data["label_name"] = "?"
        print("  ⚠️ label_name error id={}: {}".format(data["id"], str(e_lbl)))
    
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
#UNTUK_SAMBUNGAN_BAJA — Tracking dict: fabrikasi kolom (splice), parent balok anak (clip angle), index balok induk
_FAB_COL_MAP     = {}   # Mapping: revit_element_id -> (fab_base_idx, fab_top_idx)
_SEC_BEAM_PARENTS = {}  # Mapping: secondary_elem_id (int) -> [parent1_id (int), parent2_id (int)]
_BEAMX_BY_INDEX  = {}   # (j_grid, i_bay, floor_k) -> element_id int  (X-dir primary beams)
_BEAMY_BY_INDEX  = {}   # (i_grid, j_bay, floor_k) -> element_id int  (Y-dir primary beams)

# === PRE-TRANSACTION: Overwrite Lookup Tables ===
print("\n🔧 Preparing Custom Sections...")
try:
    # Collect unique sections needed for beams and columns
    beam_sections = {}
    col_sections = {}
    _sec_beam_names = [SECTION_BEAM_EXT, SECTION_BEAM_INT]
    if SECONDARY_BEAM_CONFIG.get("enabled", False):
        _sb_sec = SECONDARY_BEAM_CONFIG.get("section", "")
        if _sb_sec and _sb_sec not in _sec_beam_names:
            _sec_beam_names.append(_sb_sec)
    for sec_name in _sec_beam_names:
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

            # --- GRID ---
            "OST_Grids",

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

        # NOTE: Grid creation (step 2.5) has been moved to AFTER column creation
        # so that actual column positions can be used as grid coordinates.
        # See step 3b below.

        # 3. GEOMETRY BUILD (CENTERED AT ORIGIN 0,0,0)
        # === CREATE/GET MATERIALS ===
        print("\n🔧 Creating Materials...")
        # Validasi material names ada di MATERIALS dict
        for mat_label, mat_name in [("MATERIAL_COL", MATERIAL_COL), 
                                     ("MATERIAL_BEAM_EXT", MATERIAL_BEAM_EXT), 
                                     ("MATERIAL_BEAM_INT", MATERIAL_BEAM_INT)]:
            if mat_name not in MATERIALS:
                available = ", ".join(MATERIALS.keys())
                raise KeyError(
                    "❌ {} = '{}' tidak ditemukan di MATERIALS dict.\n"
                    "Material yang tersedia: {}".format(mat_label, mat_name, available))
        
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

        # Secondary beam symbol (only loaded when enabled)
        beam_sec_sym = None
        mat_beam_sec = None
        if SECONDARY_BEAM_CONFIG.get("enabled", False):
            _sb_sec_name = SECONDARY_BEAM_CONFIG.get("section", "IWF200x100x5.5x8")
            _sb_mat_name = SECONDARY_BEAM_CONFIG.get("material", "BJ 41")
            try:
                beam_sec_sym = find_structural_family(BuiltInCategory.OST_StructuralFraming, _sb_sec_name)
                if not beam_sec_sym.IsActive:
                    beam_sec_sym.Activate()
                print("  Beam Sec:     {} ({})".format(beam_sec_sym.Name, beam_sec_sym.FamilyName))
            except Exception as _e_bsec:
                beam_sec_sym = beam_ext_sym  # fallback
                print("  Beam Sec: fallback to Beam Ext ({})".format(str(_e_bsec)))
            try:
                mat_beam_sec = create_or_get_material(doc, _sb_mat_name, MATERIALS[_sb_mat_name])
            except Exception:
                mat_beam_sec = mat_beam_ext  # fallback
        
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

        # --- FABRICATION SEGMENTATION LOGIC ---
        # Kolom dibuat MENERUS per segmen fabrikasi di Revit (model fisik)
        # Untuk analisis OpenSees: data di-split ke per-story saat JSON export
        total_height_mm = N_STORY * HEIGHT_MM
        effective_max = COL_FAB_MAX_LENGTH_MM - COL_SPLICE_OFFSET_MM  # 10500mm default
        offset_ft = mm_to_ft(COL_SPLICE_OFFSET_MM)
        
        level_elevations_mm = [k * HEIGHT_MM for k in range(N_STORY + 1)]
        
        # Tentukan segmen fabrikasi: [(base_level_idx, top_level_idx)]
        fab_segments = []
        
        if total_height_mm <= effective_max:
            # Single piece: 1 kolom menerus dari Level 1 ke Level teratas + offset
            fab_segments = [(0, N_STORY)]
            print("  Fabrication: Single piece (total {:.0f}mm <= {:.0f}mm)".format(total_height_mm, effective_max))
        else:
            #UNTUK_SAMBUNGAN_BAJA — Penentuan titik splice kolom (multi-segment fabrication)
            # Multi-segment: potong di level + offset (Opsi A)
            seg_start = 0
            for k in range(1, N_STORY + 1):
                base_elev = level_elevations_mm[seg_start]
                if seg_start > 0:
                    base_elev += COL_SPLICE_OFFSET_MM
                top_splice = level_elevations_mm[k] + COL_SPLICE_OFFSET_MM
                seg_length = top_splice - base_elev
                
                if seg_length >= COL_FAB_MAX_LENGTH_MM:
                    if k - 1 > seg_start:
                        fab_segments.append((seg_start, k - 1))
                        seg_start = k - 1
                    else:
                        story_physical = HEIGHT_MM + COL_SPLICE_OFFSET_MM
                        if story_physical > COL_FAB_MAX_LENGTH_MM:
                            print("  Warning: Story {} exceed fabrication limit".format(k))
                
                if k == N_STORY:
                    fab_segments.append((seg_start, k))
            
            print("  Fabrication: {} segments".format(len(fab_segments)))
            for idx, (sb, st) in enumerate(fab_segments):
                base_e = level_elevations_mm[sb] + (COL_SPLICE_OFFSET_MM if sb > 0 else 0)
                top_e = level_elevations_mm[st] + COL_SPLICE_OFFSET_MM
                print("    Seg {}: Level {} -> Level {} + offset ({:.0f}mm -> {:.0f}mm)".format(
                    idx + 1, sb + 1, st + 1, base_e, top_e))
        
        # Simpan fab_segments sebagai global untuk dipakai saat JSON export
        _FAB_SEGMENTS = fab_segments
        _FAB_COL_MAP = {}  # Mapping: revit_element_id -> (fab_base_idx, fab_top_idx)

        # --- CREATE REVIT GRIDS (sebelum kolom) ---
        if CREATE_REVIT_GRIDS:
            print("\n🔧 Creating Revit Grids...")
            try:
                create_revit_grids(doc, _GRID_X_COORDS_MM, _GRID_Y_COORDS_MM,
                                   GRID_X_LABELS, GRID_Y_LABELS)
                # Verifikasi: hitung grid yang benar-benar ada di dokumen sekarang
                _grid_verify = (FilteredElementCollector(doc)
                                .OfCategory(BuiltInCategory.OST_Grids)
                                .WhereElementIsNotElementType()
                                .ToElements())
                _grid_names = [g.Name for g in _grid_verify]
                print("  Grid OK — verifikasi: {} grid di dokumen: {}".format(
                    len(_grid_verify), _grid_names))
            except Exception as e_grid:
                print("  ⚠️ Grid creation error: {}".format(str(e_grid)))
        else:
            print("\nℹ️ CREATE_REVIT_GRIDS=False — Grid elements dilewati")

        # --- CREATE COLUMNS PER FABRICATION SEGMENT (menerus) ---
        for seg_base_idx, seg_top_idx in fab_segments:
            base_level = active_levels[seg_base_idx]
            top_level  = active_levels[seg_top_idx]
            
            # Base offset: 0 untuk segmen pertama, SPLICE_OFFSET untuk segmen ke-2+
            base_offset_ft = offset_ft if seg_base_idx > 0 else 0.0
            # Top offset: COL_TOP_OFFSET_MM di level teratas, COL_SPLICE_OFFSET_MM di splice point
            is_last_segment = (seg_top_idx == N_STORY)
            top_offset_ft = mm_to_ft(COL_TOP_OFFSET_MM) if is_last_segment else offset_ft
            
            base_z = base_level.Elevation + base_offset_ft
            top_z  = top_level.Elevation + top_offset_ft
            
            for i in range(BAY_X_COUNT + 1):
                for j in range(BAY_Y_COUNT + 1):
                    p1 = get_pt(i, j, base_z)
                    p2 = get_pt(i, j, top_z)
                    
                    c = doc.Create.NewFamilyInstance(Line.CreateBound(p1, p2), col_sym, base_level, StructuralType.Column)
                    
                    # Assign material to column
                    if mat_col:
                        try:
                            p_mat = c.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                            if p_mat and not p_mat.IsReadOnly:
                                p_mat.Set(mat_col.Id)
                        except: pass
                    
                    # PHYSICAL ROTATION
                    physical_rotation_rad = math.radians(90+COLUMN_ROTATION_DEG)
                    
                    if abs(physical_rotation_rad) > 0.001:
                        try:
                            axis_end = XYZ(p1.X, p1.Y, p1.Z + 10)
                            axis = Line.CreateBound(p1, axis_end)
                            ElementTransformUtils.RotateElement(doc, c.Id, axis, physical_rotation_rad)
                        except Exception as e:
                            print("Column physical rotation warning: " + str(e))
                    
                    cols_to_process.append({
                        'el': c,
                        'lb': base_level,
                        'lt': top_level,
                        'base_offset_ft': base_offset_ft,
                        'top_offset_ft': top_offset_ft
                    })
                    created_ids.append(c.Id)
                    _ELEMENT_GROUPS[c.Id.IntegerValue] = GRP_KOLOM  #UNTUK_SAMBUNGAN_BAJA — group tracking kolom
                    # Simpan metadata fabrikasi untuk split saat JSON export
                    _FAB_COL_MAP[c.Id.IntegerValue] = (seg_base_idx, seg_top_idx)  #UNTUK_SAMBUNGAN_BAJA — fab mapping untuk splice
        
        # (Grid sudah dibuat sebelum kolom di atas)

        # --- CREATE BEAMS PER STORY (tidak berubah) ---
        for k in range(N_STORY):
            lt = active_levels[k+1]
            z_top = lt.Elevation
            
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
                _ELEMENT_GROUPS[b.Id.IntegerValue] = group_name  #UNTUK_SAMBUNGAN_BAJA — group tracking balok
                
                return b.Id
                
            # Balok Arah X (Horizontal) — Deteksi Eksterior/Interior
            for j in range(BAY_Y_COUNT + 1):
                is_ext = (j == 0 or j == BAY_Y_COUNT)
                sym = beam_ext_sym if is_ext else beam_int_sym
                mat = mat_beam_ext if is_ext else mat_beam_int
                grp = GRP_BALOK_INDUK
                for i in range(BAY_X_COUNT):
                    p_start = get_pt(i, j, z_top)
                    p_end   = get_pt(i+1, j, z_top)
                    bx_id = mk_bm(p_start, p_end, sym, mat, grp)
                    created_ids.append(bx_id)
                    _BEAMX_BY_INDEX[(j, i, k)] = bx_id.IntegerValue

            # Balok Arah Y (Vertikal) — Deteksi Eksterior/Interior
            for i in range(BAY_X_COUNT + 1):
                is_ext = (i == 0 or i == BAY_X_COUNT)
                sym = beam_ext_sym if is_ext else beam_int_sym
                mat = mat_beam_ext if is_ext else mat_beam_int
                grp = GRP_BALOK_INDUK
                for j in range(BAY_Y_COUNT):
                    p_start = get_pt(i, j, z_top)
                    p_end   = get_pt(i, j+1, z_top)
                    by_id = mk_bm(p_start, p_end, sym, mat, grp)
                    created_ids.append(by_id)
                    _BEAMY_BY_INDEX[(i, j, k)] = by_id.IntegerValue

            # --- Balok Anak (Secondary Beams) ---
            #UNTUK_SAMBUNGAN_BAJA — Pembuatan balok anak + tracking parent beams untuk deteksi joint clip angle
            if SECONDARY_BEAM_CONFIG.get("enabled", False) and beam_sec_sym is not None:
                _sb_floors = SECONDARY_BEAM_CONFIG.get("floors", "all")
                _floor_ok  = (_sb_floors == "all") or ((k + 1) in _sb_floors)
                if _floor_ok:
                    _cnt_x = SECONDARY_BEAM_CONFIG.get("count_per_bay_x", 0)
                    _cnt_y = SECONDARY_BEAM_CONFIG.get("count_per_bay_y", 0)
                    for _i in range(BAY_X_COUNT):
                        for _j in range(BAY_Y_COUNT):
                            # Balok anak arah X: bentang dari X_i ke X_{i+1},
                            # ditempatkan di n posisi Y dalam bay (_j, _j+1)
                            for _n in range(1, _cnt_x + 1):
                                _y_ratio = float(_n) / float(_cnt_x + 1)
                                _y_ft    = (start_y + _j * span_y_ft
                                            + _y_ratio * span_y_ft)
                                _ps = XYZ(start_x + _i * span_x_ft,       _y_ft, z_top)
                                _pe = XYZ(start_x + (_i + 1) * span_x_ft, _y_ft, z_top)
                                _sb_id = mk_bm(_ps, _pe, beam_sec_sym, mat_beam_sec, GRP_BALOK_ANAK)
                                created_ids.append(_sb_id)
                                # Parents: Y-dir beams at grid X=_i and X=_i+1 for bay _j
                                _p1 = _BEAMY_BY_INDEX.get((_i,     _j, k), 0)
                                _p2 = _BEAMY_BY_INDEX.get((_i + 1, _j, k), 0)
                                _SEC_BEAM_PARENTS[_sb_id.IntegerValue] = [_p1, _p2]

                            # Balok anak arah Y: bentang dari Y_j ke Y_{j+1},
                            # ditempatkan di n posisi X dalam bay (_i, _i+1)
                            for _n in range(1, _cnt_y + 1):
                                _x_ratio = float(_n) / float(_cnt_y + 1)
                                _x_ft    = (start_x + _i * span_x_ft
                                            + _x_ratio * span_x_ft)
                                _ps = XYZ(_x_ft, start_y + _j * span_y_ft,       z_top)
                                _pe = XYZ(_x_ft, start_y + (_j + 1) * span_y_ft, z_top)
                                _sb_id = mk_bm(_ps, _pe, beam_sec_sym, mat_beam_sec, GRP_BALOK_ANAK)
                                created_ids.append(_sb_id)
                                # Parents: X-dir beams at grid Y=_j and Y=_j+1 for bay _i
                                _p1 = _BEAMX_BY_INDEX.get((_j,     _i, k), 0)
                                _p2 = _BEAMX_BY_INDEX.get((_j + 1, _i, k), 0)
                                _SEC_BEAM_PARENTS[_sb_id.IntegerValue] = [_p1, _p2]
        
        # 4. FIX CONSTRAINTS
        doc.Regenerate()
        for x in cols_to_process:
            try:
                x['el'].get_Parameter(BuiltInParameter.SLANTED_COLUMN_TYPE_PARAM).Set(0)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM).Set(x['lb'].Id)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM).Set(x['lt'].Id)
                x['el'].get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM).Set(x['base_offset_ft'])
                x['el'].get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(x['top_offset_ft'])
            except: pass
        
        # 5. FIX ANALYTICAL MODEL — Reset ke level murni (tanpa offset fabrikasi)
        # Model fisik: kolom dengan offset → model analitis: kolom di level murni
        doc.Regenerate()
        analytical_adjusted = 0
        for x in cols_to_process:
            base_off = x.get('base_offset_ft', 0.0)
            top_off = x.get('top_offset_ft', 0.0)
            
            # Hanya adjust kolom yang memiliki offset
            if abs(base_off) < 0.001 and abs(top_off) < 0.001:
                continue
            
            try:
                col_el = x['el']
                analytical_model = col_el.GetAnalyticalModel()
                
                if analytical_model is not None:
                    curves = analytical_model.GetCurves(AnalyticalCurveType.ActiveCurves)
                    
                    if curves and len(curves) > 0:
                        for curve in curves:
                            start_pt = curve.GetEndPoint(0)
                            end_pt = curve.GetEndPoint(1)
                            
                            # Endpoint analytical = level murni (tanpa offset)
                            # start_pt.Z saat ini = base_level + base_offset
                            # end_pt.Z saat ini   = top_level + top_offset
                            # Target: Z = level elevation tanpa offset
                            new_start = XYZ(start_pt.X, start_pt.Y, start_pt.Z - base_off)
                            new_end   = XYZ(end_pt.X,   end_pt.Y,   end_pt.Z - top_off)
                            
                            new_curve = Line.CreateBound(new_start, new_end)
                            analytical_model.SetCurve(new_curve)
                            analytical_adjusted += 1
            except Exception as e_anal:
                # Fallback: coba method alternatif untuk Revit 2023+
                try:
                    col_el = x['el']
                    # Revit 2023+: AnalyticalToPhysicalAssociationManager
                    # Jika GetAnalyticalModel() tidak tersedia, coba adjust via parameter
                    # ANALYTICAL_MODEL_BASE_OFFSET dan ANALYTICAL_MODEL_TOP_OFFSET 
                    p_anal_base = col_el.get_Parameter(BuiltInParameter.STRUCTURAL_ANALYTICAL_COLUMN_BASE_EXTENSION)
                    p_anal_top = col_el.get_Parameter(BuiltInParameter.STRUCTURAL_ANALYTICAL_COLUMN_TOP_EXTENSION)
                    
                    if p_anal_base and not p_anal_base.IsReadOnly:
                        p_anal_base.Set(-base_off)  # Negatif untuk menarik balik ke level
                    if p_anal_top and not p_anal_top.IsReadOnly:
                        p_anal_top.Set(-top_off)
                    analytical_adjusted += 1
                except:
                    pass
        
        if analytical_adjusted > 0:
            print("  Analytical model adjusted: {} columns reset to level-pure".format(analytical_adjusted))

except Exception as e:
    TaskDialog.Show("Error", str(e))

# Refresh active view agar grid langsung terlihat tanpa perlu reload manual
try:
    revit.uidoc.RefreshActiveView()
except Exception:
    pass

# Aktifkan grid di active 3D view
try:
    _active_view = revit.uidoc.ActiveView
    if isinstance(_active_view, View3D):
        _all_lvls = FilteredElementCollector(doc).OfClass(Level).ToElements()
        with revit.Transaction("ROIDA: Show Grids in Active 3D View"):
            # A. Pastikan kategori Grid tidak hidden
            _grid_cat_id = ElementId(int(BuiltInCategory.OST_Grids))
            try:
                if _active_view.CanCategoryBeHidden(_grid_cat_id):
                    if _active_view.GetCategoryHidden(_grid_cat_id):
                        _active_view.SetCategoryHidden(_grid_cat_id, False)
                        print("  Grid category: unhidden")
            except Exception as e_cat:
                print("  ⚠️ Grid category visibility: {}".format(str(e_cat)))

            # B. ShowGridsOnLevel (Revit 2024 API) — hanya Level 1
            _grid_ok = False
            _lvl1 = None
            for _lvl in _all_lvls:
                if _lvl.Name == "Level 1":
                    _lvl1 = _lvl
                    break
            # Fallback: ambil level dengan elevasi terendah
            if _lvl1 is None:
                _lvl1 = min(_all_lvls, key=lambda l: l.Elevation)

            try:
                _active_view.ShowGridsOnLevel(_lvl1.Id)
                print("  ✅ ShowGridsOnLevel: {}".format(_lvl1.Name))
                _grid_ok = True
            except Exception as e_sg:
                print("  ⚠️ ShowGridsOnLevel: {}".format(str(e_sg)))

            # Verifikasi
            if _grid_ok:
                try:
                    _showing = _active_view.GetLevelsThatShowGrids()
                    print("  Verifikasi: {} level(s) showing grids".format(_showing.Count))
                except Exception:
                    pass

        print("✅ Grid visibility applied to: {}".format(_active_view.Name))
    else:
        print("ℹ️ Active view bukan 3D view — ShowGrids dilewati")
except Exception as e_show_grid:
    print("⚠️ ShowGrids active view: {}".format(str(e_show_grid)))

# ============================================================================
# GENERATE ANALYTICAL MODEL (LOGIKA PRINT TOTAL GABUNGAN)
# ============================================================================
# Update: Output disederhanakan.
# "Total Analytical Member Baru" & "Total Model Analitik Aktif" = Total Fisik (Balok + Kolom).

from System.Collections.Generic import List

# --- FUNGSI BANTUAN GEOMETRI ---
def get_element_curve(element):
    """Mendapatkan garis sumbu dari elemen (Balok/Kolom).
    Untuk kolom: gunakan elevasi level murni (tanpa offset fabrikasi)."""
    loc = element.Location
    if isinstance(loc, LocationCurve):
        # Balok: gunakan kurva fisik langsung
        cat_id = element.Category.Id.IntegerValue if element.Category else -1
        if cat_id == int(BuiltInCategory.OST_StructuralColumns):
            # Kolom dengan LocationCurve: adjust ke level murni
            curve = loc.Curve
            p0 = curve.GetEndPoint(0)
            p1_end = curve.GetEndPoint(1)
            
            # Ambil level elevasi tanpa offset
            p_base = element.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
            p_top = element.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
            z_s = p0.Z  # default
            z_e = p1_end.Z  # default
            
            if p_base:
                lvl = doc.GetElement(p_base.AsElementId())
                if lvl: z_s = lvl.Elevation
            if p_top:
                lvl = doc.GetElement(p_top.AsElementId())
                if lvl: z_e = lvl.Elevation
            
            pt_start = XYZ(p0.X, p0.Y, z_s)
            pt_end = XYZ(p0.X, p0.Y, z_e)
            if pt_start.DistanceTo(pt_end) > 0.01:
                return Line.CreateBound(pt_start, pt_end)
            return curve
        return loc.Curve  # Balok: return kurva fisik langsung
    elif isinstance(loc, LocationPoint): # Kolom Vertikal
        pt = loc.Point
        # Gunakan level elevasi tanpa offset (bukan bounding box)
        p_base = element.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
        p_top = element.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
        z_s, z_e = 0.0, 0.0
        
        if p_base:
            lvl = __revit__.ActiveUIDocument.Document.GetElement(p_base.AsElementId())
            if lvl: z_s = lvl.Elevation
        if p_top:
            lvl = __revit__.ActiveUIDocument.Document.GetElement(p_top.AsElementId())
            if lvl: z_e = lvl.Elevation
        
        if abs(z_e - z_s) > 0.01:
            return Line.CreateBound(XYZ(pt.X, pt.Y, z_s), XYZ(pt.X, pt.Y, z_e))
        
        # Fallback ke bounding box
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

            # Track unassociated column AMs for rotation (multi-story split
            # creates N AMs but only the first gets association)
            _unassoc_column_am_ids = set()

            # Ambil level elevations untuk split kolom per-story
            all_levels_sorted = sorted(
                FilteredElementCollector(doc).OfClass(Level).ToElements(),
                key=lambda lv: lv.Elevation)

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
                        count_processed_successfully += 1
                        continue 

                    cat_id = phys_el.Category.Id.IntegerValue
                    is_column = (cat_id == int(BuiltInCategory.OST_StructuralColumns))
                    fab_info = _FAB_COL_MAP.get(phys_el.Id.IntegerValue) if is_column else None
                    
                    if fab_info and is_column:
                        seg_base_idx, seg_top_idx = fab_info
                        n_stories = seg_top_idx - seg_base_idx
                        
                        if n_stories > 1:
                            # SPLIT: buat N analytical members per-story
                            # Ambil XY dari lokasi elemen
                            loc = phys_el.Location
                            if isinstance(loc, LocationCurve):
                                pt = loc.Curve.GetEndPoint(0)
                            elif isinstance(loc, LocationPoint):
                                pt = loc.Point
                            else:
                                pt = None
                            
                            if pt:
                                first_am = True
                                for k in range(seg_base_idx, seg_top_idx):
                                    z_base = all_levels_sorted[k].Elevation if k < len(all_levels_sorted) else 0
                                    z_top = all_levels_sorted[k+1].Elevation if k+1 < len(all_levels_sorted) else z_base + 1

                                    story_curve = Line.CreateBound(
                                        XYZ(pt.X, pt.Y, z_base),
                                        XYZ(pt.X, pt.Y, z_top))

                                    am = AnalyticalMember.Create(doc, story_curve)
                                    am.StructuralRole = AnalyticalStructuralRole.StructuralRoleColumn
                                    # Hanya associate analytical member pertama (Revit: 1 association per physical)
                                    if first_am:
                                        assoc_manager.AddAssociation(am.Id, phys_el.Id)
                                        first_am = False
                                    else:
                                        # Track unassociated AMs so rotation loop can find them
                                        _unassoc_column_am_ids.add(am.Id)
                                
                                count_processed_successfully += 1
                                print("    Column {} -> {} analytical members (per-story)".format(
                                    phys_el.Id, n_stories))
                            continue
                    
                    # Default: 1 analytical member (balok atau kolom single-story)
                    curve = get_element_curve(phys_el)
                    
                    if curve:
                        new_am = AnalyticalMember.Create(doc, curve)
                        
                        if is_column:
                            new_am.StructuralRole = AnalyticalStructuralRole.StructuralRoleColumn
                        else:
                            new_am.StructuralRole = AnalyticalStructuralRole.StructuralRoleBeam

                        assoc_manager.AddAssociation(new_am.Id, phys_el.Id)
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

                        # Check if this is an unassociated multi-story column AM
                        is_unassoc_col = am.Id in _unassoc_column_am_ids

                        if phys_id == ElementId.InvalidElementId and not is_unassoc_col:
                            continue

                        if is_unassoc_col:
                            is_column = True
                            is_beam = False
                        else:
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
                            # Apply analytical rotation for beams
                            rotation_deg = BEAM_ANALYTICAL_ROTATION_DEG
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

        # Terapkan ke SEMUA 3D view (bukan hanya active view),
        # sehingga view baru yang dibuat setelah run pun sudah benar.
        all_3d_views = [v for v in FilteredElementCollector(doc).OfClass(View3D).ToElements()
                        if not v.IsTemplate]

        # Kategori analytical model
        analytical_cat_names = [
            "OST_AnalyticalMember", "OST_AnalyticalPanel",   # Revit 2023+
            "OST_AnalyticalBeams",  "OST_AnalyticalColumns", # Revit lama
            "OST_AnalyticalNodes",  "OST_AnalyticalLinks",
            "OST_AnalyticalFloors", "OST_AnalyticalWalls",
        ]

        # Kategori Internal Origin & titik referensi
        origin_cat_names = [
            "OST_InternalOrigin",    # Internal Origin (Revit 2022+)
            "OST_ProjectBasePoint",  # Project Base Point
            "OST_SharedBasePoint",   # Survey Point
            "OST_IOS_GeoSite",       # Geographic Site
        ]

        def _apply_visibility(view):
            if view.ViewTemplateId != ElementId.InvalidElementId:
                return  # dikunci View Template — skip

            # A. Master switch analytical model
            try:
                if hasattr(view, "AreAnalyticalModelCategoriesHidden"):
                    if view.AreAnalyticalModelCategoriesHidden:
                        view.AreAnalyticalModelCategoriesHidden = False
            except Exception:
                pass

            # B. Fallback parameter
            try:
                for p_name in ("VG_ANALYTICAL_MODEL_VISIBILITY",
                               "VIEW_STRUCT_ANALYTICAL_MODEL_VISIBILITY"):
                    if hasattr(BuiltInParameter, p_name):
                        p = view.get_Parameter(getattr(BuiltInParameter, p_name))
                        if p and not p.IsReadOnly and p.AsInteger() == 0:
                            p.Set(1)
                            break
            except Exception:
                pass

            # C. Unhide analytical sub-categories
            for name in analytical_cat_names:
                if hasattr(BuiltInCategory, name):
                    try:
                        cat_id = ElementId(int(getattr(BuiltInCategory, name)))
                        if view.CanCategoryBeHidden(cat_id) and view.GetCategoryHidden(cat_id):
                            view.SetCategoryHidden(cat_id, False)
                    except Exception:
                        pass

            # D. Aktifkan Internal Origin via parameter VIEWER_DISPLAY_INTERNAL_ORIGIN
            try:
                p_origin = view.get_Parameter(BuiltInParameter.VIEWER_DISPLAY_INTERNAL_ORIGIN)
                if p_origin and not p_origin.IsReadOnly and p_origin.AsInteger() != 1:
                    p_origin.Set(1)
            except Exception:
                pass

            # E. Unhide reference point categories (fallback)
            for name in origin_cat_names:
                if hasattr(BuiltInCategory, name):
                    try:
                        cat_id = ElementId(int(getattr(BuiltInCategory, name)))
                        if view.CanCategoryBeHidden(cat_id) and view.GetCategoryHidden(cat_id):
                            view.SetCategoryHidden(cat_id, False)
                    except Exception:
                        pass

        applied = 0
        for v3d in all_3d_views:
            try:
                _apply_visibility(v3d)
                applied += 1
            except Exception:
                pass

        print("✅ Visibility fix diterapkan ke {} 3D view.".format(applied))

        # F. Aktifkan Grid visibility di semua 3D view
        # F. Aktifkan Grid visibility — hanya Level 1
        try:
            all_levels = FilteredElementCollector(doc).OfClass(Level).ToElements()
            _grid_cat_id = ElementId(int(BuiltInCategory.OST_Grids))
            # Cari Level 1 (fallback: level elevasi terendah)
            _lvl1 = None
            for lvl in all_levels:
                if lvl.Name == "Level 1":
                    _lvl1 = lvl
                    break
            if _lvl1 is None:
                _lvl1 = min(all_levels, key=lambda l: l.Elevation)

            grid_shown = 0
            for v3d in all_3d_views:
                try:
                    if v3d.CanCategoryBeHidden(_grid_cat_id):
                        if v3d.GetCategoryHidden(_grid_cat_id):
                            v3d.SetCategoryHidden(_grid_cat_id, False)
                    v3d.ShowGridsOnLevel(_lvl1.Id)
                    grid_shown += 1
                except Exception:
                    pass
            if grid_shown > 0:
                print("✅ Grid visibility (Level 1): diaktifkan di {} 3D view(s)".format(grid_shown))
        except Exception as e_grid_vis:
            print("⚠️ ShowGrids error: {}".format(str(e_grid_vis)))

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
    
    # Level elevations in mm for per-story splitting
    _level_elevs_mm = [k * int(HEIGHT_MM) for k in range(int(N_STORY) + 1)]
    
    print("Memproses Ekspor JSON...")
    
    for el in elements_all:
        try:
            el_data = get_element_data(el, doc)
            
            if el_data and el_data.get("id"):
                el_id = el.Id.IntegerValue
                fab_info = _FAB_COL_MAP.get(el_id)
                
                #UNTUK_SAMBUNGAN_BAJA — Split kolom multi-story → virtual ID per lantai (id*1000+story) untuk deteksi splice
                if fab_info and el_data["type"] == "Column":
                    seg_base_idx, seg_top_idx = fab_info
                    n_stories_in_seg = seg_top_idx - seg_base_idx

                    if n_stories_in_seg > 1:
                        # SPLIT: kolom menerus -> per-story virtual elements
                        topo = el_data["topology"]
                        x_coord = topo["start_node"][0]
                        y_coord = topo["start_node"][1]

                        for k_story in range(seg_base_idx, seg_top_idx):
                            import copy
                            story_data = copy.deepcopy(el_data)

                            # Unique ID untuk setiap virtual element
                            story_data["id"] = el_id * 1000 + (k_story - seg_base_idx + 1)

                            # Topology per-story (level murni tanpa offset)
                            z_base = _level_elevs_mm[k_story]
                            z_top = _level_elevs_mm[k_story + 1]
                            story_data["topology"] = {
                                "start_node": [x_coord, y_coord, z_base],
                                "end_node": [x_coord, y_coord, z_top],
                                "length_mm": z_top - z_base
                            }

                            # Update design parameters for this story height
                            story_data["design_parameters"] = get_design_parameters(
                                "Column", story_data["topology"])

                            # Update label_name dengan floor number yang benar
                            try:
                                story_data["label_name"] = assign_label_name(
                                    story_data,
                                    _GRID_X_COORDS_MM, _GRID_Y_COORDS_MM, _Z_LEVELS_MM,
                                    GRID_X_LABELS, GRID_Y_LABELS
                                )
                            except Exception:
                                pass  # tetap pakai label dari deepcopy

                            final_elements_list.append(story_data)
                        
                        print("  Column {} split: {} stories (Level {}->{})".format(
                            el_id, n_stories_in_seg, seg_base_idx + 1, seg_top_idx + 1))
                    else:
                        # Single story segment: topology per-story langsung
                        topo = el_data["topology"]
                        x_coord = topo["start_node"][0]
                        y_coord = topo["start_node"][1]
                        z_base = _level_elevs_mm[seg_base_idx]
                        z_top = _level_elevs_mm[seg_top_idx]
                        el_data["topology"] = {
                            "start_node": [x_coord, y_coord, z_base],
                            "end_node": [x_coord, y_coord, z_top],
                            "length_mm": z_top - z_base
                        }
                        el_data["design_parameters"] = get_design_parameters(
                            "Column", el_data["topology"])
                        final_elements_list.append(el_data)
                else:
                    # Beam atau kolom tanpa fab info: langsung append
                    final_elements_list.append(el_data)
                
        except Exception as e_item:
            print("Skip Element ID {}: {}".format(el.Id, str(e_item)))

    #UNTUK_SAMBUNGAN_BAJA — Attach parent_beams ke balok anak (relasi untuk deteksi joint clip angle)
    # --- C2. ATTACH parent_beams TO SECONDARY BEAM ELEMENTS ---
    # Build id->label_name lookup from final list
    if _SEC_BEAM_PARENTS:
        _id_to_lbl = {e["id"]: e.get("label_name", "?") for e in final_elements_list}
        for elem in final_elements_list:
            if elem.get("group") == GRP_BALOK_ANAK:
                parent_ids = _SEC_BEAM_PARENTS.get(elem["id"], [])
                elem["parent_beams"] = [
                    {"id": pid, "label_name": _id_to_lbl.get(pid, "?")}
                    for pid in parent_ids if pid
                ]

    #UNTUK_SAMBUNGAN_BAJA — Struktur Result.json final: semua data elemen yang dikonsumsi Connection Engine
    # --- D. SAVE JSON (STRUKTUR FINAL) ---
    final_output = {
        # Grid System (label referensi elemen)
        "grid_system": {
            "x_labels":      GRID_X_LABELS,
            "y_labels":      GRID_Y_LABELS,
            "x_coords_mm":   _GRID_X_COORDS_MM,
            "y_coords_mm":   _GRID_Y_COORDS_MM,
            "floor_labels":  [str(k) for k in range(int(N_STORY) + 1)],
            "z_levels_mm":   _Z_LEVELS_MM,
        },

        # Load Patterns (SAP2000-like)
        "load_patterns": LOAD_PATTERNS,
        
        # Load Combination Config
        "load_combination_config": {
            "mode": LOAD_COMBO_MODE,
            "custom_combinations": CUSTOM_LOAD_COMBOS
        },
        
        # Support / Boundary Conditions
        "support_config": {
            "type": SUPPORT_TYPE,
            "dof": SUPPORT_DOF
        },

        # Group names (single source of truth untuk downstream)
        "group_names": {
            "kolom":       GRP_KOLOM,
            "balok_induk": GRP_BALOK_INDUK,
            "balok_anak":  GRP_BALOK_ANAK,
        },

        # Secondary beam release (M3 pin at both ends)
        "secondary_beam_release": SECONDARY_BEAM_CONFIG.get("release", False),
        
        # Legacy fields (backward compatibility)
        "slab_sw_pressure": SLAB_SW_PRESSURE,
        "slab_adl_pressure": SLAB_ADL_PRESSURE,
        "live_load_pressure": LIVE_LOAD_PRESSURE,
        
        # Shell plate slab configuration
        "slab_plate": {
            "enabled": SLAB_PLATE_ENABLED,
            "E_MPa": SLAB_PLATE_E_MPA,
            "nu": SLAB_PLATE_NU,
            "rho_kg_m3": SLAB_PLATE_RHO_KG_M3,
            "thickness_mm": SLAB_THICKNESS,
            "mesh_size_mm": SLAB_PLATE_MESH_SIZE_MM,
        },
        
        # Seismic Parameters (SNI 1726)
        "seismic_parameters": {
            "site_class": SITE_CLASS,
            "SS": SS, "S1": S1, "TL": TL,
            "Fa": Fa, "Fv": Fv,
            "SMS": round(SMS, 4), "SM1": round(SM1, 4),
            "SDS": SDS, "SD1": SD1,
            "T0": round(T0, 4), "Ts": round(Ts_period, 4),
            "Ct": Ct, "x_Ta": x_Ta, "Ta": round(Ta, 4),
            "TOTAL_HEIGHT_M": TOTAL_HEIGHT_M,
            "Ie": Ie, "R": R, "Cd": Cd, "Omega_0": Omega_0,
            "Ry": Ry, "rho": rho,
            "frame_type": FRAME_TYPE,
            "SDC": SDC,
            "sdc_compatible": SDC_IS_COMPATIBLE,
            "sdc_message": SDC_MESSAGE,
            "N_STORY": N_STORY, "HEIGHT_MM": HEIGHT_MM,
            "COL_SPLICE_OFFSET_MM": COL_SPLICE_OFFSET_MM,
            "analysis_method": ANALYSIS_METHOD,
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
    print("   Support Type: {}".format(SUPPORT_TYPE))
    
    # ================================================================
    # APPLY BOUNDARY CONDITIONS TO REVIT ANALYTICAL MODEL
    # ================================================================
    # Load SEMUA 3 BC families (Revit membutuhkan ketiganya)
    BC_FAMILIES = [
        r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Boundary Conditions\M_Boundary Condition-Fixed.rfa",
        r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Boundary Conditions\M_Boundary Condition-Pinned.rfa",
        r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Boundary Conditions\M_Boundary Condition-Roller.rfa",
    ]
    
    try:
        with revit.Transaction("Load BC Families"):
            for rfa_path in BC_FAMILIES:
                fname = os.path.splitext(os.path.basename(rfa_path))[0]
                # Cek apakah sudah loaded
                already = False
                for fam in FilteredElementCollector(doc).OfClass(Family):
                    if fam.Name == fname:
                        already = True
                        break
                if already:
                    print("   BC [OK] " + fname)
                    continue
                # Load dari library
                if os.path.exists(rfa_path):
                    result = doc.LoadFamily(rfa_path)
                    if result:
                        print("   BC [LOADED] " + fname)
                    else:
                        print("   BC [EXISTS] " + fname)
                else:
                    print("   BC [MISSING] " + rfa_path)
        print("   BC Family check completed.")
    except Exception as e_bc:
        print("   WARNING: BC Family error: " + str(e_bc))
    
    # ================================================================
    # CONFIGURE STRUCTURAL SETTINGS + APPLY BOUNDARY CONDITIONS
    # ================================================================
    try:
        from Autodesk.Revit.DB.Structure import (
            AnalyticalMember, AnalyticalToPhysicalAssociationManager,
            StructuralSettings as SS
        )
        
        _BC_FAMILY_NAMES = {
            "Fixed":  "M_Boundary Condition-Fixed",
            "Pinned": "M_Boundary Condition-Pinned",
            "Roller": "M_Boundary Condition-Roller",
        }
        
        # ---- Step 1: Collect BC FamilySymbol IDs ----
        bc_symbol_ids = {}
        for fs in FilteredElementCollector(doc).OfClass(FamilySymbol):
            fam = fs.Family
            if fam and fam.Name in _BC_FAMILY_NAMES.values():
                if not fs.IsActive:
                    with revit.Transaction("Activate BC Symbol"):
                        fs.Activate()
                        doc.Regenerate()
                bc_symbol_ids[fam.Name] = fs.Id
        
        print("   BC Symbols: {} loaded".format(len(bc_symbol_ids)))
        
        # ---- Step 2: Auto-configure BC Settings via direct properties ----
        ss = SS.GetStructuralSettings(doc)
        if ss is not None and len(bc_symbol_ids) >= 3:
            settings_ok = 0
            
            with revit.Transaction("Set BC Family Symbols in Settings"):
                try:
                    ss.BoundaryConditionFamilySymbolFixed = bc_symbol_ids[_BC_FAMILY_NAMES["Fixed"]]
                    settings_ok += 1
                except Exception as ef:
                    print("   BC Settings Fixed error: " + str(ef))
                
                try:
                    ss.BoundaryConditionFamilySymbolPinned = bc_symbol_ids[_BC_FAMILY_NAMES["Pinned"]]
                    settings_ok += 1
                except Exception as ep:
                    print("   BC Settings Pinned error: " + str(ep))
                
                try:
                    ss.BoundaryConditionFamilySymbolRoller = bc_symbol_ids[_BC_FAMILY_NAMES["Roller"]]
                    settings_ok += 1
                except Exception as er:
                    print("   BC Settings Roller error: " + str(er))
            
            print("   BC Settings: {}/3 assigned".format(settings_ok))
        
        # ---- Step 3: Apply Boundary Conditions to Column Bases ----
        assoc_manager = AnalyticalToPhysicalAssociationManager.GetAnalyticalToPhysicalAssociationManager(doc)
        
        columns = FilteredElementCollector(doc)\
            .OfCategory(BuiltInCategory.OST_StructuralColumns)\
            .WhereElementIsNotElementType().ToElements()
        
        # Use TranslationRotationValue for DOF
        tv_fixed = TranslationRotationValue.Fixed
        tv_release = TranslationRotationValue.Release
        
        if SUPPORT_TYPE == "Fixed":
            dof = [tv_fixed]*6
        elif SUPPORT_TYPE == "Pinned":
            dof = [tv_fixed, tv_fixed, tv_fixed, tv_release, tv_release, tv_release]
        else:  # Roller
            dof = [tv_release, tv_release, tv_fixed, tv_release, tv_release, tv_release]
        
        bc_count = 0
        bc_skip = 0
        bc_errors = []

        # First pass: find minimum Z among all column bases (ground level)
        all_base_z = []
        for col in columns:
            try:
                anal_id = assoc_manager.GetAssociatedElementId(col.Id)
                if anal_id == ElementId.InvalidElementId:
                    continue
                anal_member = doc.GetElement(anal_id)
                if anal_member is None:
                    continue
                curve = anal_member.GetCurve()
                if curve is None:
                    continue
                pt0 = curve.GetEndPoint(0)
                pt1 = curve.GetEndPoint(1)
                all_base_z.append(min(pt0.Z, pt1.Z))
            except:
                pass
        min_base_z = min(all_base_z) if all_base_z else 0.0

        with revit.Transaction("Apply Boundary Conditions"):
            for col in columns:
                try:
                    anal_id = assoc_manager.GetAssociatedElementId(col.Id)
                    if anal_id == ElementId.InvalidElementId:
                        bc_skip += 1
                        continue

                    anal_member = doc.GetElement(anal_id)
                    if anal_member is None:
                        bc_skip += 1
                        continue

                    curve = anal_member.GetCurve()
                    if curve is None:
                        bc_skip += 1
                        continue

                    pt0 = curve.GetEndPoint(0)
                    pt1 = curve.GetEndPoint(1)
                    base_point = pt0 if pt0.Z <= pt1.Z else pt1

                    # Only apply BC at ground level, not upper story columns
                    if abs(base_point.Z - min_base_z) > 0.5:  # ~150mm tolerance in feet
                        bc_skip += 1
                        continue
                    
                    bc_created = False
                    
                    # Try: NewPointBoundaryConditions via analytical member reference
                    # Get stable reference from geometry
                    try:
                        geo_opts = Options()
                        geo_opts.ComputeReferences = True
                        geo = anal_member.get_Geometry(geo_opts)
                        ref = None
                        if geo:
                            for g in geo:
                                if hasattr(g, 'GetEndPointReference'):
                                    base_idx = 0 if pt0.Z <= pt1.Z else 1
                                    ref = g.GetEndPointReference(base_idx)
                                    if ref:
                                        break
                        
                        if ref:
                            bc = doc.Create.NewPointBoundaryConditions(
                                ref,
                                dof[0], 0.0, dof[1], 0.0, dof[2], 0.0,
                                dof[3], 0.0, dof[4], 0.0, dof[5], 0.0
                            )
                            if bc is not None:
                                bc_created = True
                                bc_count += 1
                    except Exception as eBC:
                        if len(bc_errors) < 3:
                            bc_errors.append("Col {}: {}".format(col.Id.IntegerValue, str(eBC)[:80]))
                    
                    if not bc_created:
                        bc_skip += 1
                        
                except Exception as e_col_bc:
                    bc_skip += 1
                    if len(bc_errors) < 3:
                        bc_errors.append("Col {}: {}".format(col.Id.IntegerValue, str(e_col_bc)[:80]))
        
        print("   BC Applied: {} columns, {} skipped (type={})".format(
            bc_count, bc_skip, SUPPORT_TYPE))
        for err in bc_errors:
            print("   BC Detail: " + err)
    except Exception as e_bc:
        print("   WARNING: BC error: " + str(e_bc))

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

            # 3. SIAPKAN ARGUMEN
            # [Python Engine, Script Analysis, File Input (Model), File Output (Hasil), --method]
            args = [PYTHON_EXE_PATH, ANALYSIS_SCRIPT_PATH, OUTPUT_PATH, RESULT_PATH,
                    "--method={}".format(ANALYSIS_METHOD)]
            
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
            #UNTUK_SAMBUNGAN_BAJA — Merge enriches node_id, frame_label, node classification → dipakai untuk node_map sambungan
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
                                node_info["description"] = "Support Node ({}) - Z=0".format(SUPPORT_TYPE)
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

                        # === BUILD label_name LOOKUP dari Model data.json ===
                        _elem_label_lookup = {}   # {str(elem_id): label_name}
                        try:
                            with open(OUTPUT_PATH, 'r') as _f_md:
                                _md = json.load(_f_md)
                            for _me in _md.get("model_elements", []):
                                _eid = str(_me.get("id", ""))
                                _lbl = _me.get("label_name", "")
                                if _eid and _lbl:
                                    _elem_label_lookup[_eid] = _lbl
                        except Exception:
                            pass  # lookup tetap kosong, tidak apa-apa

                        # === HELPER: Node coord -> grid label (e.g. "A-1/0", "A-B/1/1") ===
                        def _node_grid_label(coords):
                            """Map node [x,y,z] ke notasi grid. Snap ke grid, atau bracket jika di antara 2 grid."""
                            try:
                                nx, ny, nz = float(coords[0]), float(coords[1]), float(coords[2])
                                tol = 50.0

                                def _snap(val, grid_coords, labels):
                                    """Return label jika snap, atau 'Lo~Hi' jika di antara 2 grid."""
                                    for i, gc in enumerate(grid_coords):
                                        if abs(val - gc) <= tol:
                                            return labels[i] if i < len(labels) else "?"
                                    # Bracket: cari 2 grid yang mengapit
                                    lo_i, hi_i = -1, -1
                                    for i, gc in enumerate(grid_coords):
                                        if gc <= val + tol:
                                            if lo_i == -1 or gc > grid_coords[lo_i]:
                                                lo_i = i
                                        if gc >= val - tol:
                                            if hi_i == -1 or gc < grid_coords[hi_i]:
                                                hi_i = i
                                    if lo_i >= 0 and hi_i >= 0 and lo_i != hi_i:
                                        l1 = labels[lo_i] if lo_i < len(labels) else "?"
                                        l2 = labels[hi_i] if hi_i < len(labels) else "?"
                                        return "{}~{}".format(l1, l2)
                                    return "?"

                                gx = _snap(nx, _GRID_X_COORDS_MM, GRID_X_LABELS)
                                gy = _snap(ny, _GRID_Y_COORDS_MM, GRID_Y_LABELS)

                                # Floor level
                                fl = 0
                                for k, zl in enumerate(_Z_LEVELS_MM):
                                    if abs(nz - zl) <= tol:
                                        fl = k
                                        break
                                return "{}-{}/{}".format(gy, gx, fl)
                            except Exception:
                                return "-"

                        out.print_md("# 📑 LAPORAN HASIL ANALISIS STRUKTUR")

                        # ================================================================
                        # INFO KONFIGURASI BALOK ANAK
                        # ================================================================
                        if SECONDARY_BEAM_CONFIG.get("enabled", False):
                            _sb_release = SECONDARY_BEAM_CONFIG.get("release", False)
                            _sb_status = "RELEASE (Sendi - M3 pin di kedua ujung)" if _sb_release else "FIXED (Jepit - momen ditransfer ke balok induk)"
                            out.print_md("**Balok Anak:** {} | Tumpuan: **{}**".format(
                                SECONDARY_BEAM_CONFIG.get("section", "-"), _sb_status))

                        # ================================================================
                        # TAMPILKAN FREKUENSI NATURAL STRUKTUR (_modal)
                        # ================================================================
                        _modal = all_results.get('_modal', {})
                        if _modal and _modal.get('status') == 'Success':
                            _modes = _modal.get('modes', [])
                            _ta = Ta  # Ta empiris dari config
                            out.print_md("## Frekuensi Natural Struktur")
                            if _modes:
                                _modal_rows = []
                                for _m in _modes[:6]:  # tampilkan maks 6 mode
                                    _mno = _m.get('mode', '-')
                                    _T   = round(_m.get('period_s', 0), 4)
                                    _f   = round(_m.get('frequency_Hz', 0), 4)
                                    _vs_ta = "{:.3f} s (Ta empiris)".format(_ta) if _mno == 1 else ""
                                    _modal_rows.append([_mno, _T, _f, _vs_ta])
                                print_center_table(
                                    output=out,
                                    data=_modal_rows,
                                    columns=["Mode", "T (s)", "f (Hz)", "Keterangan"],
                                    title="Periode & Frekuensi Natural (Ta empiris = {:.3f} s)".format(_ta)
                                )

                                # --- Modal Participating Mass Ratio ---
                                _mpmr_rows = []
                                for _m in _modes[:6]:
                                    _mno = _m.get('mode', '-')
                                    _T   = round(_m.get('period_s', 0), 4)
                                    _ux  = _m.get('UX_ratio', 0) * 100
                                    _uy  = _m.get('UY_ratio', 0) * 100
                                    _rz  = _m.get('RZ_ratio', 0) * 100
                                    _sux = _m.get('cum_UX', 0) * 100
                                    _suy = _m.get('cum_UY', 0) * 100
                                    _srz = _m.get('cum_RZ', 0) * 100
                                    _dom = _m.get('dominant', '-')
                                    _mpmr_rows.append([
                                        _mno, _T,
                                        "{:.2f}".format(_ux), "{:.2f}".format(_uy), "{:.2f}".format(_rz),
                                        "{:.2f}".format(_sux), "{:.2f}".format(_suy), "{:.2f}".format(_srz),
                                        _dom
                                    ])
                                print_center_table(
                                    output=out,
                                    data=_mpmr_rows,
                                    columns=["Mode", "T (s)", "UX (%)", "UY (%)", "RZ (%)",
                                             "Sum UX (%)", "Sum UY (%)", "Sum RZ (%)", "Dominant"],
                                    title="Modal Participating Mass Ratios"
                                )
                        # ================================================================

                        # Build dynamic load case list from Analysis.json keys
                        gravity_keys = [k for k in all_results.keys()
                                       if not k.startswith('_')
                                       and k not in ('SeismicX', 'SeismicY')
                                       and not k.startswith('LIVE_ZONE_')
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
                                            _dlbl = _node_grid_label(c)
                                            data_disp.append([
                                                nid,
                                                _dlbl,
                                                check_coords[0], check_coords[1], check_coords[2],
                                                "{:.5f}".format(d[0]),
                                                "{:.5f}".format(d[1]),
                                                "{:.5f}".format(d[2]),
                                                "{:.5f}".format(d[3]),
                                                "{:.5f}".format(d[4]),
                                                "{:.5f}".format(d[5])
                                            ])

                                    # Sort berdasarkan ID Node
                                    data_disp.sort(key=lambda x: int(x[0]))

                                    if data_disp:
                                        print_center_table(
                                            output=out,
                                            data=data_disp,
                                            columns=["Node ID", "Grid", "X", "Y", "Z", "U1 (mm)", "U2 (mm)", "U3 (mm)", "R1 (rad)", "R2 (rad)", "R3 (rad)"],
                                            title="Perpindahan Node Utama ({})".format(case_key)
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
                                            f1 = reac.get('F1', 0.0)
                                            f2 = reac.get('F2', 0.0)
                                            f3 = reac.get('F3', 0.0)
                                            m1 = reac.get('M1', 0.0)
                                            m2 = reac.get('M2', 0.0)
                                            m3 = reac.get('M3', 0.0)
                                            _rlbl = _node_grid_label(val.get('coords', [0,0,0]))

                                            data_reac.append([
                                                nid, _rlbl,
                                                f1, f2, f3, m1, m2, m3
                                            ])

                                    if data_reac:
                                        data_reac.sort(key=lambda x: int(x[0]))
                                        print_center_table(
                                            output=out,
                                            data=data_reac,
                                            columns=["Node ID", "Grid", "Fx (N)", "Fy (N)", "Fz (N)", "M1 (Nmm)", "M2 (Nmm)", "M3 (Nmm)"],
                                            title="Reaksi Tumpuan ({})".format(case_key)
                                        )
                                    else:
                                        out.print_md("> _Info: Tidak ada data reaksi (Check tumpuan)._")

                                # ===========================================================
                                # C. TABEL GAYA DALAM & SUMMARY (DENGAN FILTER ID)
                                # ===========================================================
                                data_elem = []
                                
                                # Reset Variabel Max/Min Stats untuk semua komponen
                                components = ["P", "V2", "V3", "T", "M2", "M3"]
                                # Init dengan +/- infinity
                                stats = {k: {"max": -1.0e20, "max_id": "-", "min": 1.0e20, "min_id": "-"} for k in components}

                                _force_col_count = 0
                                _force_beam_count = 0
                                _force_skip_count = 0
                                if 'elements' in results:
                                    for eid, val in results['elements'].items():
                                        try:
                                            # --- LOGIKA FILTERING ---
                                            # 1. Konversi ID ke Integer Revit
                                            revit_id_int = int(eid)
                                            revit_el_id = ElementId(revit_id_int)

                                            # 2. Cek Keberadaan Elemen di Revit
                                            el = doc.GetElement(revit_el_id)

                                            # 2b. Fallback: kolom split punya synthetic ID = parent_id*1000+story
                                            if not el:
                                                parent_id = revit_id_int // 1000
                                                if parent_id > 0:
                                                    el = doc.GetElement(ElementId(parent_id))

                                            # 3. JIKA NULL (ID Analisis/Split Node), SKIP
                                            if not el:
                                                _force_skip_count += 1
                                                continue

                                            # 4. Cek Kategori (Hanya Balok & Kolom)
                                            if not el.Category:
                                                _force_skip_count += 1
                                                continue
                                            cat_id = el.Category.Id.IntegerValue
                                            category_type = "Other"

                                            if cat_id == int(BuiltInCategory.OST_StructuralColumns):
                                                category_type = "Column"
                                            elif cat_id == int(BuiltInCategory.OST_StructuralFraming):
                                                category_type = "Beam"
                                            else:
                                                _force_skip_count += 1
                                                continue

                                            # --- AMBIL DATA ---
                                            try:
                                                elem_name = "{} : {}".format(el.Symbol.FamilyName, el.Name)
                                            except Exception:
                                                elem_name = str(el.Name) if el.Name else "ID {}".format(eid)

                                            # Multi-Station Support (SAP2000 Diagram Style)
                                            stations = val.get('stations', [])

                                            if not stations:
                                                _force_skip_count += 1
                                                continue

                                            if category_type == "Column":
                                                _force_col_count += 1
                                            else:
                                                _force_beam_count += 1
                                            
                                            # --- UPDATE STATS from ALL stations ---
                                            _lbl_name = _elem_label_lookup.get(str(eid), "")
                                            id_display = "[{}] {}{}".format(
                                                eid, elem_name,
                                                " ({})".format(_lbl_name) if _lbl_name else ""
                                            )
                                            for station_data in stations:
                                                current_vals = {
                                                    "P": station_data.get('P', 0.0),
                                                    "V2": station_data.get('Fy', 0.0),
                                                    "V3": station_data.get('Fz', 0.0),
                                                    "T": station_data.get('T', 0.0),
                                                    "M2": station_data.get('My', 0.0),
                                                    "M3": station_data.get('Mz', 0.0)
                                                }
                                                for k in components:
                                                    val_comp = current_vals[k]
                                                    if val_comp > stats[k]["max"]:
                                                        stats[k]["max"] = val_comp
                                                        stats[k]["max_id"] = id_display
                                                    if val_comp < stats[k]["min"]:
                                                        stats[k]["min"] = val_comp
                                                        stats[k]["min_id"] = id_display

                                            # --- DISPLAY only at target stations ---
                                            display_targets = [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]
                                            _lbl = _elem_label_lookup.get(str(eid), "-")
                                            for target in display_targets:
                                                best = None
                                                best_diff = 1e9
                                                for sd in stations:
                                                    diff = abs(sd.get('station', 0.0) - target)
                                                    if diff < best_diff:
                                                        best_diff = diff
                                                        best = sd
                                                if best and best_diff < 0.03:
                                                    data_elem.append([
                                                        str(eid),
                                                        _lbl,
                                                        elem_name,
                                                        "{:.3f}".format(target),
                                                        round(best.get('P', 0.0), 2),
                                                        round(best.get('Fy', 0.0), 2),
                                                        round(best.get('Fz', 0.0), 2),
                                                        round(best.get('T', 0.0), 2),
                                                        round(best.get('My', 0.0), 2),
                                                        round(best.get('Mz', 0.0), 2)
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
                                            columns=["ID", "Label", "Family & Type", "Station", "P (N)", "V2 (N)", "V3 (N)", "T (Nmm)", "M2 (Nmm)", "M3 (Nmm)"],
                                            title="Detail Gaya Dalam Elemen Asli ({})".format(case_key)
                                        )
                                        out.print_md("_Elemen ditampilkan: {} kolom, {} balok (skip: {})_".format(
                                            _force_col_count, _force_beam_count, _force_skip_count))
                                    else:
                                        out.print_md("> _Tidak ada elemen Revit yang cocok dengan hasil analisis._")
                                        out.print_md("> _Debug: {} elemen dilewati (GetElement null atau kategori tidak cocok)_".format(_force_skip_count))

                                    # TAMPILKAN SUMMARY
                                    # TAMPILKAN SUMMARY KOMPREHENSIF (Max & Min)
                                    summary_rows = []
                                    labels = {
                                        "P": "Axial (P)", "T": "Torsi (T)", 
                                        "V2": "Shear (V2)", "V3": "Shear (V3)",
                                        "M2": "Momen (M2)", "M3": "Momen (M3)"
                                    }
                                    
                                    for k in components:
                                        # Handle jika tidak ada data (masih initial value)
                                        max_v = stats[k]["max"]
                                        if max_v < -1.0e19: max_v = 0.0
                                        
                                        min_v = stats[k]["min"]
                                        if min_v > 1.0e19: min_v = 0.0
                                        
                                        # Tentukan satuan: N untuk gaya, Nmm untuk momen/torsi
                                        unit = "N" if k in ["P", "V2", "V3"] else "Nmm"
                                        
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

                                                # Fallback: kolom split punya synthetic ID = parent_id*1000+story
                                                if not el:
                                                    parent_id = revit_id_int // 1000
                                                    if parent_id > 0:
                                                        el = doc.GetElement(ElementId(parent_id))

                                                if not el:
                                                    continue

                                                if not el.Category:
                                                    continue
                                                cat_id = el.Category.Id.IntegerValue
                                                if cat_id not in [int(BuiltInCategory.OST_StructuralColumns),
                                                                  int(BuiltInCategory.OST_StructuralFraming)]:
                                                    continue

                                                elem_type = val.get('element_type', 'Unknown')
                                                try:
                                                    elem_name_short = el.Name
                                                except Exception:
                                                    elem_name_short = "ID {}".format(eid)
                                                _lbl_d = _elem_label_lookup.get(str(eid), "-")

                                                # Get max_deflection data
                                                max_defl = val.get('max_deflection', None)
                                                if max_defl and isinstance(max_defl, dict):
                                                    dy_max = max_defl.get('delta_y_max_mm', 0.0)
                                                    dy_station = max_defl.get('delta_y_station', 0.0)
                                                    dy_dist = max_defl.get('delta_y_distance_mm', 0.0)
                                                    dz_max = max_defl.get('delta_z_max_mm', 0.0)
                                                    dz_station = max_defl.get('delta_z_station', 0.0)
                                                    dz_dist = max_defl.get('delta_z_distance_mm', 0.0)

                                                    deflection_data.append([
                                                        str(eid),
                                                        _lbl_d,
                                                        elem_type,
                                                        elem_name_short,
                                                        "{:.5f}".format(dy_max),
                                                        "{:.3f}".format(dy_station),
                                                        "{:.0f}".format(dy_dist),
                                                        "{:.5f}".format(dz_max),
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
                                            columns=["ID", "Label", "Type", "Section", "δy Max (mm)", "Station Y", "Dist Y (mm)", "δz Max (mm)", "Station Z", "Dist Z (mm)"],
                                            title="📐 Defleksi Maksimum Elemen ({})".format(case_key)
                                        )

                                        # Find overall max deflection
                                        # row indices shifted by 1 (label inserted at index 1)
                                        max_dy_elem = max(deflection_data, key=lambda x: abs(float(x[4])))
                                        max_dz_elem = max(deflection_data, key=lambda x: abs(float(x[7])))
                                        out.print_md("**Defleksi Maksimum Overall:**")
                                        out.print_md("  - **δy max:** {} mm @ {} / ID {} (station {}, dist {} mm)".format(
                                            max_dy_elem[4], max_dy_elem[1], max_dy_elem[0], max_dy_elem[5], max_dy_elem[6]))
                                        out.print_md("  - **δz max:** {} mm @ {} / ID {} (station {}, dist {} mm)".format(
                                            max_dz_elem[7], max_dz_elem[1], max_dz_elem[0], max_dz_elem[8], max_dz_elem[9]))

                            else:
                                out.print_md("❌ **Analisis Gagal**")
                                out.print_md("**Pesan:** " + str(results.get("message", "Unknown Error")))
                        # ===========================================================
                        # MODAL ANALYSIS RESULTS (Period & Frequency)
                        # ===========================================================
                        modal_data = all_results.get('_modal')
                        if modal_data and modal_data.get('status') == 'Success':
                            out.print_md("---")
                            out.print_md("## 🔔 MODAL - PERIODE DAN FREKUENSI")
                            
                            modes = modal_data.get('modes', [])
                            summary = modal_data.get('summary', {})
                            
                            if modes:
                                # SAP2000-style modal table
                                mode_rows = []
                                for m in modes:
                                    mode_rows.append([
                                        "MODAL",
                                        "Mode",
                                        str(m['mode']),
                                        "{:.6f}".format(m.get('period_s', 0)),
                                        "{:.6f}".format(m.get('frequency_Hz', 0)),
                                        "{:.6f}".format(m.get('omega_rad_s', 0)),
                                        "{:.6f}".format(m.get('eigenvalue', 0)),
                                    ])
                                
                                print_center_table(
                                    output=out,
                                    data=mode_rows,
                                    columns=[
                                        "OutputCase",
                                        "StepType",
                                        "StepNum",
                                        "Period (Sec)",
                                        "Frequency (Cyc/sec)",
                                        "CircFreq (rad/sec)",
                                        "Eigenvalue (rad2/sec2)",
                                    ],
                                    title="Modal Periods And Frequencies"
                                )
                                
                                # Summary
                                out.print_md("**T1 = {:.6f} s** | **f1 = {:.4f} Hz**".format(
                                    summary.get('T1', 0), summary.get('f1', 0)))

                                # --- Modal Participating Mass Ratios (SAP2000-style) ---
                                mpmr_rows = []
                                for m in modes:
                                    mpmr_rows.append([
                                        "MODAL",
                                        "Mode",
                                        str(m['mode']),
                                        "{:.6f}".format(m.get('period_s', 0)),
                                        "{:.4f}".format(m.get('UX_ratio', 0)),
                                        "{:.4f}".format(m.get('UY_ratio', 0)),
                                        "{:.4f}".format(m.get('cum_UX', 0)),
                                        "{:.4f}".format(m.get('cum_UY', 0)),
                                        "{:.4f}".format(m.get('RZ_ratio', 0)),
                                        "{:.4f}".format(m.get('cum_RZ', 0)),
                                    ])
                                print_center_table(
                                    output=out,
                                    data=mpmr_rows,
                                    columns=[
                                        "OutputCase", "StepType", "StepNum",
                                        "Period (Sec)",
                                        "UX", "UY", "SumUX", "SumUY", "RZ", "SumRZ",
                                    ],
                                    title="Modal Participating Mass Ratios"
                                )
                                out.print_md(
                                    "**Cumulative: SumUX = {:.2f}%** | **SumUY = {:.2f}%** | **SumRZ = {:.2f}%**".format(
                                        summary.get('cum_UX_pct', 0),
                                        summary.get('cum_UY_pct', 0),
                                        summary.get('cum_RZ_pct', 0)))

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
                                    _sr_lbl = _node_grid_label(nval.get('coords', [0,0,0]))
                                    reac_rows.append([
                                        str(nid),
                                        _sr_lbl,
                                        "{:.2f}".format(reac.get('Fx', reac.get('F1', 0))),
                                        "{:.2f}".format(reac.get('Fy', reac.get('F2', 0))),
                                        "{:.2f}".format(reac.get('Fz', reac.get('F3', 0))),
                                        "{:.2f}".format(reac.get('Mx', reac.get('M1', 0))),
                                        "{:.2f}".format(reac.get('My', reac.get('M2', 0))),
                                        "{:.2f}".format(reac.get('Mz', reac.get('M3', 0))),
                                    ])

                            if reac_rows:
                                reac_rows.sort(key=lambda x: int(x[0]))
                                print_center_table(
                                    output=out,
                                    data=reac_rows,
                                    columns=["Node ID", "Grid", "Fx (N)", "Fy (N)", "Fz (N)", "Mx (Nmm)", "My (Nmm)", "Mz (Nmm)"],
                                    title="Reaksi Tumpuan ({})".format(s_dir)
                                )
                            
                            # D. Equilibrium check
                            eq_res = eq_data.get('summary', {}).get('equilibrium_residual_N', 0)
                            V_N = sp.get('V_kN', 0) * 1000.0
                            ratio_pct = abs(eq_res / V_N * 100) if V_N > 0 else 0
                            out.print_md("**Equilibrium:** |Sum(R) - V| = {:.2f} N ({:.4f}%)".format(eq_res, ratio_pct))

                            # E. Element Internal Forces (seismic)
                            eq_elems = eq_data.get('elements', {})
                            if eq_elems:
                                seis_data_elem = []
                                seis_components = ["P", "V2", "V3", "T", "M2", "M3"]
                                seis_stats = {k: {"max": -1.0e20, "max_id": "-", "min": 1.0e20, "min_id": "-"} for k in seis_components}

                                for se_eid, se_val in eq_elems.items():
                                    try:
                                        se_revit_id = int(se_eid)
                                        se_el_id = ElementId(se_revit_id)
                                        se_el = doc.GetElement(se_el_id)
                                        # Fallback: kolom split punya synthetic ID = parent_id*1000+story
                                        if not se_el:
                                            parent_id = se_revit_id // 1000
                                            if parent_id > 0:
                                                se_el = doc.GetElement(ElementId(parent_id))
                                        if not se_el:
                                            continue
                                        if not se_el.Category:
                                            continue
                                        se_cat_id = se_el.Category.Id.IntegerValue
                                        if se_cat_id not in [int(BuiltInCategory.OST_StructuralColumns),
                                                             int(BuiltInCategory.OST_StructuralFraming)]:
                                            continue

                                        se_stations = se_val.get('stations', [])
                                        if not se_stations:
                                            continue

                                        try:
                                            se_name = "{} : {}".format(se_el.Symbol.FamilyName, se_el.Name)
                                        except Exception:
                                            se_name = str(se_el.Name) if se_el.Name else "ID {}".format(se_eid)
                                        se_lbl_name = _elem_label_lookup.get(str(se_eid), "")
                                        se_id_display = "[{}] {}{}".format(
                                            se_eid, se_name,
                                            " ({})".format(se_lbl_name) if se_lbl_name else ""
                                        )
                                        for se_sd in se_stations:
                                            se_cv = {
                                                "P": se_sd.get('P', 0.0), "V2": se_sd.get('Fy', 0.0),
                                                "V3": se_sd.get('Fz', 0.0), "T": se_sd.get('T', 0.0),
                                                "M2": se_sd.get('My', 0.0), "M3": se_sd.get('Mz', 0.0)
                                            }
                                            for sk in seis_components:
                                                sv = se_cv[sk]
                                                if sv > seis_stats[sk]["max"]:
                                                    seis_stats[sk]["max"] = sv
                                                    seis_stats[sk]["max_id"] = se_id_display
                                                if sv < seis_stats[sk]["min"]:
                                                    seis_stats[sk]["min"] = sv
                                                    seis_stats[sk]["min_id"] = se_id_display

                                        se_targets = [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]
                                        se_lbl = _elem_label_lookup.get(str(se_eid), "-")
                                        for se_tgt in se_targets:
                                            se_best = None
                                            se_best_diff = 1e9
                                            for se_sd in se_stations:
                                                se_diff = abs(se_sd.get('station', 0.0) - se_tgt)
                                                if se_diff < se_best_diff:
                                                    se_best_diff = se_diff
                                                    se_best = se_sd
                                            if se_best and se_best_diff < 0.03:
                                                seis_data_elem.append([
                                                    str(se_eid), se_lbl, se_name,
                                                    "{:.3f}".format(se_tgt),
                                                    round(se_best.get('P', 0.0), 2),
                                                    round(se_best.get('Fy', 0.0), 2),
                                                    round(se_best.get('Fz', 0.0), 2),
                                                    round(se_best.get('T', 0.0), 2),
                                                    round(se_best.get('My', 0.0), 2),
                                                    round(se_best.get('Mz', 0.0), 2)
                                                ])
                                    except Exception:
                                        continue

                                if seis_data_elem:
                                    seis_data_elem.sort(key=lambda x: int(x[0]))
                                    print_center_table(
                                        output=out,
                                        data=seis_data_elem,
                                        columns=["ID", "Label", "Family & Type", "Station",
                                                 "P (N)", "V2 (N)", "V3 (N)", "T (Nmm)", "M2 (Nmm)", "M3 (Nmm)"],
                                        title="Gaya Dalam Elemen ({})".format(s_dir)
                                    )

                except Exception as e:
                    out.print_md("## ❌ Gagal Membaca Output JSON")
                    out.print_md(str(e))
            else:
                TaskDialog.Show("Error", "File output Analysis.json tidak terbentuk.")
            
    except Exception as e:
        TaskDialog.Show("System Error", str(e))
