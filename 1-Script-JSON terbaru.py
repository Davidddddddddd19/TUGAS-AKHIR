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
from Autodesk.Revit.DB.Structure import StructuralType 
from Autodesk.Revit.UI import TaskDialog
from pyrevit import script # HAPUS 'forms' DARI SINI

doc = __revit__.ActiveUIDocument.Document

# ================= INPUT PARAMETER (USER SETUP) =================
# 1. Lokasi Output JSON
OUTPUT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Model data.json"

# 2. KONFIGURASI SUBPROCESS (WAJIB DIGANTI SESUAI KOMPUTER ANDA)
#    Cari path python.exe (harus yang sudah install openseespy/numpy)
PYTHON_EXE_PATH = r"C:\\Users\\hp\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" 
#    Cari path script analisis python eksternal Anda
ANALYSIS_SCRIPT_PATH = r"C:\\Users\\hp\\AppData\\Roaming\\Tugas Akhir 2025\\RevitAPI.extension\\Tugas Akhir.tab\\ROIDA.panel\\Create.pushbutton\\Analysis\\Analysis.py"

# 3. Parameter Geometri & Beban
N_STORY     = 1        
BAY_X_COUNT = 1        
BAY_Y_COUNT = 1        
SPAN_X_MM   = 4000.0     
SPAN_Y_MM   = 4000.0     
HEIGHT_MM   = 4000.0     
LOAD_LIVE_OFFICE_MPA = 0.024 # 2.4 kPa

SEARCH_TERMS_COL = ["Universal Column", "M_Concrete-Rectangular-Column", "UC", "Col"]
SEARCH_TERMS_BEAM = ["Universal Beam", "M_Concrete-Rectangular-Beam", "UB", "Framing"]

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

def find_structural_family(category, search_terms):
    collector = FilteredElementCollector(doc).OfCategory(category).OfClass(FamilySymbol)
    found = None
    for sym in collector:
        full_name = sym.FamilyName + " " + sym.Name
        for term in search_terms:
            if term.lower() in full_name.lower():
                found = sym
                break
        if found: break
    if not found: found = collector.FirstElement()
    return found

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
    mid_x = (start_node[0] + end_node[0]) / 2.0
    mid_y = (start_node[1] + end_node[1]) / 2.0
    limit_x = BAY_X_COUNT * SPAN_X_MM
    limit_y = BAY_Y_COUNT * SPAN_Y_MM
    edge_tol = 10.0
    
    is_edge = False
    if (abs(mid_x) < edge_tol or abs(mid_x - limit_x) < edge_tol or 
        abs(mid_y) < edge_tol or abs(mid_y - limit_y) < edge_tol):
        is_edge = True

    # 4. Hitung Beban Puncak Distribusi (q_peak)
    #    q_peak = Pressure (MPa) * Lebar Tributary Max (mm)
    w = LOAD_LIVE_OFFICE_MPA # N/mm2
    
    if is_one_way and is_long_span:
        tributary_width = Lx / 2.0 # Persegi panjang setengah bentang
    elif not is_one_way:
        tributary_width = Lx / 2.0 # Puncak segitiga/trapesium selalu Lx/2
    else:
        tributary_width = 0.0 # Balok pendek pada One Way dianggap 0

    # Hitung q puncak (N/mm)
    q_peak = w * tributary_width
    
    # Jika balok Tengah (Internal), dia menanggung kiri & kanan (x2)
    if not is_edge:
        q_peak = q_peak * 2.0
    
    # 5. [BARU] KONVERSI KE BEBAN TITIK (POINT LOAD) DI TENGAH BENTANG
    #    Prinsip: Point Load (P) = Luas Area Diagram Beban (Total Force)
    
    P_total = 0.0
    shape_type = "None"

    if q_peak > 0:
        if is_one_way and is_long_span:
            # Pola: PERSEGI PANJANG
            # Luas = q * L
            shape_type = "Rectangle"
            P_total = q_peak * beam_length
            
        elif not is_one_way and is_short_span:
            # Pola: SEGITIGA
            # Luas = 0.5 * alas * tinggi
            shape_type = "Triangle"
            P_total = 0.5 * beam_length * q_peak
            
        elif not is_one_way:
            # Pola: TRAPESIUM
            # Luas = q_peak * (L_total - 0.5 * L_flat_ramp)
            # Secara geometris: Luas Trapesium 45 derajat = q_peak * (L - 0.5 * Lx)
            # Karena bagian miringnya memakan jarak Lx/2 di kiri dan kanan.
            shape_type = "Trapezoid"
            
            # Safety check: Jika panjang balok anehnya lebih kecil dari Lx
            calc_len = beam_length if beam_length > Lx else Lx 
            P_total = q_peak * (calc_len - 0.5 * Lx)

    return {
        "pattern": "Liveload_Assign",
        "load_shape_origin": shape_type, # Info bentuk aslinya
        "is_edge": is_edge,
        "q_peak_dist": round(q_peak, 4),      # Beban distribusi max (N/mm) - sekedar info
        "point_load_N": round(P_total, 2),    # BEBAN TITIK FINAL (N)
        "location_ratio": 0.5                 # Posisi di tengah bentang (0.5 L)
    }
    
# ===================================================
# FUNGSI BANTUAN KONVERSI (HELPER)
# ===================================================
def val2mpa(val):
    """Convert Revit Internal Pressure (PSF) to MPa"""
    if val is None: return 0.0
    # 1 PSF = 47.88 Pa = 0.00004788 MPa
    return round(val * 47.880258 / 1000000.0, 1)

def val2kgmm3(val):
    """Convert Revit Internal Density (PCF) to kg/mm3"""
    if val is None: return 0.0
    # 1 PCF = 16.018 kg/m3 = 1.6e-8 kg/mm3
    return val * 16.018463 * 1.0e-9

def val2invC(val):
    """Convert Revit Internal Thermal (1/F) to 1/C"""
    if val is None: return 0.0
    # 1/F * 1.8 = 1/C
    return float("{:.2e}".format(val * 1.8))

# ===================================================
# LOGIKA UTAMA (Sesuai Referensi + Parameter Ekstra)
# ===================================================
def get_material_data(element, doc):
    # 1. Inisialisasi dengan nilai 0.0 dan Parameter LENGKAP
    mat_data = {
        "Name": "Default_Steel",
        "Class": "Steel",
        "E_MPa": 205000.0,
        "G_MPa": 80000.0,
        "Nu": 0.3,
        "Fy_MPa": 275.0,
        "Fu_MPa": 430.0,
        "Rho_kg/mm3": 7.85e-6,
        "Alpha_C": 1.2e-5
    }

    try:
        # 2. Cari Material ID (Instance -> Type)
        mat_id = None
        p_mat = element.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
        if p_mat and p_mat.HasValue: mat_id = p_mat.AsElementId()
        
        if not mat_id or mat_id == ElementId.InvalidElementId:
            elem_type = doc.GetElement(element.GetTypeId())
            if elem_type:
                p_mat_type = elem_type.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                if p_mat_type and p_mat_type.HasValue: mat_id = p_mat_type.AsElementId()

        # 3. Ekstraksi Asset (Pendekatan Referensi)
        if mat_id and mat_id != ElementId.InvalidElementId:
            mat_elem = doc.GetElement(mat_id)
            if mat_elem:
                mat_data["Name"] = mat_elem.Name
                
                # Cek Class untuk data tambahan
                if mat_elem.MaterialClass == MaterialClass.Concrete:
                    mat_data["Class"] = "Concrete"
                else:
                    mat_data["Class"] = "Steel"

                struc_asset_id = mat_elem.StructuralAssetId
                if struc_asset_id != ElementId.InvalidElementId:
                    pse = doc.GetElement(struc_asset_id) # PropertySetElement
                    if pse:
                        asset = pse.GetStructuralAsset()
                        if asset:
                            # --- A. MECHANICAL (E, G, Nu) ---
                            # Menggunakan try/except untuk properti Vector (.X) vs Scalar
                            try: e_val = asset.YoungModulus.X
                            except: e_val = asset.YoungModulus
                            
                            try: g_val = asset.ShearModulus.X
                            except: g_val = asset.ShearModulus

                            try: nu_val = asset.PoissonRatio.X
                            except: nu_val = asset.PoissonRatio
                            
                            mat_data["E_MPa"] = val2mpa(e_val)
                            mat_data["G_MPa"] = val2mpa(g_val)
                            mat_data["Nu"]    = round(nu_val, 3)

                            # --- B. DENSITY & THERMAL ---
                            mat_data["Rho_kg/mm3"] = val2kgmm3(asset.Density)
                            
                            try: a_val = asset.ThermalExpansionCoefficient.X
                            except: a_val = asset.ThermalExpansionCoefficient
                            mat_data["Alpha_C"] = val2invC(a_val)

                            # --- C. STRENGTH (Fy, Fu) ---
                            # Try block khusus karena Beton tidak punya YieldStress
                            try:
                                mat_data["Fy_MPa"] = val2mpa(asset.MinimumYieldStress)
                                mat_data["Fu_MPa"] = val2mpa(asset.MinimumTensileStrength)
                            except:
                                # Fallback untuk Beton (ambil fc' sebagai Fy)
                                try:
                                    mat_data["Fy_MPa"] = val2mpa(asset.ConcreteCompressionStrength)
                                except: pass

    except Exception as e:
        # Optional: print error untuk debug
        pass
        
    return mat_data

def get_section_properties(element, doc):
    props = {
        "Area_mm2": 0.0, "d_mm": 0.0, "b_mm": 0.0, "tf_mm": 0.0, "tw_mm": 0.0,
        "Ix_mm4": 0.0, "Iy_mm4": 0.0, "Zx_mm3": 0.0, "Sx_mm3": 0.0, "J_mm4": 0.0
    }
    try:
        elem_type = doc.GetElement(element.GetTypeId())
        if not elem_type: return props
        def find_val(bip_list, str_list):
            for name in bip_list:
                if hasattr(BuiltInParameter, name):
                    p = elem_type.get_Parameter(getattr(BuiltInParameter, name))
                    if p and p.HasValue and p.StorageType == StorageType.Double: return p.AsDouble()
            for s in str_list:
                p = elem_type.LookupParameter(s)
                if p and p.HasValue and p.StorageType == StorageType.Double: return p.AsDouble()
            return 0.0

        props["Area_mm2"] = sqft2sqmm(find_val(["STRUCTURAL_SECTION_AREA"], ["Section Area", "Area"]))
        props["d_mm"]     = ft2mm(find_val(["STRUCTURAL_SECTION_DEPTH", "FAMILY_HEIGHT_PARAM"], ["Height", "Depth", "d", "h"]))
        props["b_mm"]     = ft2mm(find_val(["STRUCTURAL_SECTION_WIDTH", "FAMILY_WIDTH_PARAM"], ["Width", "b"]))
        props["tf_mm"]    = ft2mm(find_val(["STRUCTURAL_SECTION_FLANGE_THICKNESS"], ["Flange Thickness", "tf"]))
        props["tw_mm"]    = ft2mm(find_val(["STRUCTURAL_SECTION_WEB_THICKNESS"], ["Web Thickness", "tw"]))
        props["Ix_mm4"]   = ft42mm4(find_val(["STRUCTURAL_SECTION_IX"], ["Ix", "Ixx"]))
        props["Iy_mm4"]   = ft42mm4(find_val(["STRUCTURAL_SECTION_IY"], ["Iy", "Iyy"]))
        props["Zx_mm3"]   = ft32mm3(find_val(["STRUCTURAL_SECTION_PLASTIC_MODULUS_STRONG_AXIS"], ["Zx"]))
        props["Sx_mm3"]   = ft32mm3(find_val(["STRUCTURAL_SECTION_ELASTIC_MODULUS_STRONG_AXIS"], ["Sx"]))
        props["J_mm4"]    = ft42mm4(find_val(["STRUCTURAL_SECTION_J"], ["J"]))

        d, b, tf, tw = props["d_mm"], props["b_mm"], props["tf_mm"], props["tw_mm"]
        if d > 0 and b > 0:
            hw = d - 2 * tf
            if props["Area_mm2"] == 0: props["Area_mm2"] = (2 * b * tf) + (hw * tw)
            if props["Ix_mm4"] == 0: props["Ix_mm4"] = (b*d**3 - (b-tw)*hw**3)/12.0
            if props["Iy_mm4"] == 0: props["Iy_mm4"] = (2*tf*b**3 + hw*tw**3)/12.0
            if props["J_mm4"] == 0: props["J_mm4"] = (1.0/3.0) * (2 * b * math.pow(tf, 3) + (d - tf) * math.pow(tw, 3))

        for k in props: 
            if isinstance(props[k], float): props[k] = round(props[k], 2)
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

    # 2. Susun Dictionary Data
    data = {
        "id": element.Id.IntegerValue,
        # Deteksi Type berdasarkan Category ID
        "type": "Column" if element.Category.Id.IntegerValue == int(BuiltInCategory.OST_StructuralColumns) else "Beam",
        
        # --- UPDATE DISINI ---
        "family": full_display_name, 
        
        "section": get_section_properties(element, doc),
        "material": get_material_data(element, doc),
        "topology": get_topology_ref(element, doc)
    }
    
    # 3. Hitung Beban (Khusus Beam)
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

t = Transaction(doc, "Generate Model")
t.Start()

try:
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
    col_sym = find_structural_family(BuiltInCategory.OST_StructuralColumns, SEARCH_TERMS_COL)
    beam_sym = find_structural_family(BuiltInCategory.OST_StructuralFraming, SEARCH_TERMS_BEAM)
    
    if not col_sym.IsActive: col_sym.Activate()
    if not beam_sym.IsActive: beam_sym.Activate()

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
                cols_to_process.append({'el':c, 'lb':lb, 'lt':lt})
                created_ids.append(c.Id)

        # B. Create Beams
        def mk_bm(p_start, p_end):
            b = doc.Create.NewFamilyInstance(Line.CreateBound(p_start, p_end), beam_sym, lt, StructuralType.Beam)
            set_beam_alignment_safe(b)
            return b.Id
        
        # Balok Arah X (Horizontal)
        for j in range(BAY_Y_COUNT + 1):
            for i in range(BAY_X_COUNT):
                p_start = get_pt(i, j, z_top)
                p_end   = get_pt(i+1, j, z_top)
                created_ids.append(mk_bm(p_start, p_end))
        
        # Balok Arah Y (Vertikal)
        for i in range(BAY_X_COUNT + 1):
            for j in range(BAY_Y_COUNT):
                p_start = get_pt(i, j, z_top)
                p_end   = get_pt(i, j+1, z_top)
                created_ids.append(mk_bm(p_start, p_end))
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

    t.Commit()
except Exception as e:
    if t.GetStatus() == TransactionStatus.Started: t.RollBack()
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
t_analytical = Transaction(doc, "Automation: Physical to Analytical")
t_analytical.Start()

try:
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
    
    print("✅ Total Model Analitik Aktif: {}".format(count_processed_successfully))

    t_analytical.Commit()

except Exception as e:
    t_analytical.RollBack()
    print("❌ Error Fatal: " + str(e))

# ============================================================================
# ATUR VISIBILITY GRAPHIC (LOGIKA PROPERTY DIRECT)
# ============================================================================
# Referensi User: view.AreAnalyticalModelCategoriesHidden
# Target: Mengubah nilai properti tersebut menjadi False (agar TIDAK Hidden / Muncul)

t_view = Transaction(doc, "Fix Analytical Visibility")
t_view.Start()

try:
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

    t_view.Commit()
    print("✅ Pengaturan Tampilan Selesai.")

except Exception as e:
    t_view.RollBack()
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
    # 1. Load Pressure Global
    try: globals()['LOAD_LIVE_OFFICE_MPA'] = float(LOAD_LIVE_OFFICE_MPA) 
    except: globals()['LOAD_LIVE_OFFICE_MPA'] = 0.0

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
        # DISINI LIST GLOBALNYA
        "global_pressure_loads": [globals()['LOAD_LIVE_OFFICE_MPA']], 
        
        "unit_system": "Revit Converted (mm, N, MPa)",
        "model_elements": final_elements_list
    }

    d_dir = os.path.dirname(OUTPUT_PATH)
    if not os.path.exists(d_dir): os.makedirs(d_dir)

    with open(OUTPUT_PATH, 'w') as f:
        json.dump(final_output, f, indent=2)
        
    json_success = True
    print("✅ Export Selesai.")
    print("   Global Load List: [ {} MPa ]".format(globals()['LOAD_LIVE_OFFICE_MPA']))
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
                        out.print_md("# 📑 LAPORAN HASIL ANALISIS STRUKTUR")
                        out.print_md("Berikut adalah hasil untuk 3 skenario pembebanan:")

                        # Daftar Kasus yang akan ditampilkan (Key di JSON, Judul Tampilan)
                        load_cases = [
                            ("SelfWeight", "🏗️ KASUS 1: BEBAN MATI (Self Weight)"),
                            ("LiveLoad", "🚶 KASUS 2: BEBAN HIDUP (Live Load)"),
                            ("Combination", "⚖️ KASUS 3: KOMBINASI (SW + LL)")
                        ]

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
                                            data_reac.append([
                                                nid, 
                                                round(reac[0], 2), round(reac[1], 2), round(reac[2], 2), 
                                                round(reac[3], 2), round(reac[4], 2), round(reac[5], 2)
                                            ])
                                    
                                    if data_reac:
                                        data_reac.sort(key=lambda x: int(x[0]))
                                        print_center_table(
                                            output=out,
                                            data=data_reac,
                                            columns=["Node ID", "Fx (N)", "Fy (N)", "Fz (N)", "Mx", "My", "Mz"],
                                            title="Reaksi Tumpuan ({})".format(case_key)
                                        )
                                    else:
                                        out.print_md("> _Info: Tidak ada data reaksi (Check tumpuan)._")

                                # ===========================================================
                                # C. TABEL GAYA DALAM & SUMMARY (DENGAN FILTER ID)
                                # ===========================================================
                                data_elem = []
                                
                                # Reset Variabel Max Stats
                                max_stats = {
                                    "Col_Axial": 0.0, "Col_Axial_ID": "-", 
                                    "Col_Moment": 0.0, "Col_Moment_ID": "-",
                                    "Beam_Moment": 0.0, "Beam_Moment_ID": "-", 
                                    "Beam_Axial": 0.0, "Beam_Axial_ID": "-"
                                }

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
                                            
                                            axial = val.get('axial', 0.0)
                                            m_i = val.get('moment_i', 0.0)  # FIX: was moment_z_i
                                            m_j = val.get('moment_j', 0.0)  # FIX: was moment_z_j
                                            
                                            # Cari Momen Maksimum absolut di elemen ini
                                            max_m_elem = max(abs(m_i), abs(m_j)) 
                                            id_display = "[{}] {}".format(eid, elem_name)

                                            # --- UPDATE STATISTIK MAKSIMUM ---
                                            if category_type == "Column":
                                                if abs(axial) > abs(max_stats["Col_Axial"]):
                                                    max_stats["Col_Axial"] = axial
                                                    max_stats["Col_Axial_ID"] = id_display
                                                if max_m_elem > abs(max_stats["Col_Moment"]):
                                                    max_stats["Col_Moment"] = max_m_elem
                                                    max_stats["Col_Moment_ID"] = id_display
                                                    
                                            elif category_type == "Beam":
                                                if max_m_elem > abs(max_stats["Beam_Moment"]):
                                                    max_stats["Beam_Moment"] = max_m_elem
                                                    max_stats["Beam_Moment_ID"] = id_display
                                                if abs(axial) > abs(max_stats["Beam_Axial"]):
                                                    max_stats["Beam_Axial"] = axial
                                                    max_stats["Beam_Axial_ID"] = id_display

                                            # --- MASUKKAN KE LIST TABEL ---
                                            data_elem.append([
                                                str(eid),
                                                elem_name,
                                                round(axial, 2),
                                                round(m_i, 2),
                                                round(m_j, 2)
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
                                            columns=["ID", "Family & Type", "Axial (N)", "Momen i (Nmm)", "Momen j (Nmm)"],
                                            title="Detail Gaya Dalam Elemen Asli ({})".format(case_key)
                                        )
                                    else:
                                        out.print_md("> _Tidak ada elemen Revit yang cocok dengan hasil analisis._")

                                    # TAMPILKAN SUMMARY
                                    summary_data = [
                                        ["Momen Balok Max", "{} Nmm".format(round(max_stats['Beam_Moment'], 2)), max_stats['Beam_Moment_ID']],
                                        ["Aksial Kolom Max", "{} N".format(round(max_stats['Col_Axial'], 2)), max_stats['Col_Axial_ID']],
                                        ["Momen Kolom Max", "{} Nmm".format(round(max_stats['Col_Moment'], 2)), max_stats['Col_Moment_ID']],
                                        ["Aksial Balok Max", "{} N".format(round(max_stats['Beam_Axial'], 2)), max_stats['Beam_Axial_ID']]
                                    ]
                                    
                                    print_center_table(
                                        output=out,
                                        data=summary_data,
                                        columns=["Kriteria", "Nilai Terbesar", "Elemen Penyebab"],
                                        title="📊 Ringkasan Maksimum ({})".format(case_key)
                                    )

                            else:
                                out.print_md("❌ **Analisis Gagal**")
                                out.print_md("**Pesan:** " + str(results.get("message", "Unknown Error")))

                except Exception as e:
                    out.print_md("## ❌ Gagal Membaca Output JSON")
                    out.print_md(str(e))
            else:
                TaskDialog.Show("Error", "File output Analysis.json tidak terbentuk.")
            
    except Exception as e:
        TaskDialog.Show("System Error", str(e))
