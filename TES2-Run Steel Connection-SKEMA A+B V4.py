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
from Autodesk.Revit.DB.Structure import (
    StructuralType, StructuralFramingUtils,
    StructuralConnectionHandler, StructuralConnectionHandlerType
)
from Autodesk.Revit.UI import TaskDialog
from pyrevit import script, HOST_APP, revit

# Get active document (compatible with all pyRevit versions)
doc = HOST_APP.doc

# ============================================================
# KONFIGURASI & KONSTANTA
#UNTUK_SAMBUNGAN_BAJA — Nama tipe sambungan Revit dan toleransi geometri
# ============================================================

CONNECTION_TYPE_A = "Double side end plate with safety bolt"
CONNECTION_TYPE_B = "Moment end plate"
CONNECTION_TYPE_C = "Clip angle"
CONNECTION_TYPE_D = "Splice joint"

# Toleransi geometric (mm) untuk deteksi titik di atas segmen balok
GEO_TOLERANCE_MM = 5.0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR  = os.path.dirname(SCRIPT_DIR)
RESULT_JSON = os.path.join(PANEL_DIR, "Create.pushbutton", "Result.json")
OUTPUT_JSON = os.path.join(SCRIPT_DIR, "Connection Result.json")

# ============================================================
# FUNGSI 1: load_result_json
#UNTUK_SAMBUNGAN_BAJA — Load data elemen (section, topology, group, axes) dari Result.json
# ============================================================

def load_result_json(path):
    """Baca Result.json → return (model_elements, seismic_params, group_names)."""
    if not os.path.exists(path):
        raise RuntimeError(
            "Result.json tidak ditemukan di:\n{}\n"
            "Jalankan 'Create' terlebih dahulu.".format(path)
        )
    with open(path, "r") as f:
        data = json.load(f)
    model_data    = data.get("model_data", {})
    elements      = model_data.get("model_elements", [])
    seismic_params = model_data.get("seismic_parameters", {})
    gn = model_data.get("group_names", {})
    group_names = {
        "kolom":       gn.get("kolom",       "Kolom"),
        "balok_induk": gn.get("balok_induk", "Balok Induk"),
        "balok_anak":  gn.get("balok_anak",  "Balok Anak"),
    }
    if not elements:
        raise RuntimeError("model_elements kosong di Result.json.")
    return elements, seismic_params, group_names

# ============================================================
# FUNGSI 2: load_connection_types
#UNTUK_SAMBUNGAN_BAJA — Load tipe sambungan (A/B/C/D) dari Revit project
# ============================================================

def load_connection_types(doc):
    """Cari connection types A, B, C, D di project Revit."""
    collector = FilteredElementCollector(doc).OfClass(StructuralConnectionHandlerType)
    types = {"A": None, "B": None, "C": None, "D": None}
    found_names = []
    for ct in collector:
        name = ct.Name
        found_names.append(name)
        if name == CONNECTION_TYPE_A:
            types["A"] = ct.Id
        elif name == CONNECTION_TYPE_B:
            types["B"] = ct.Id
        elif name == CONNECTION_TYPE_C:
            types["C"] = ct.Id
        elif name == CONNECTION_TYPE_D:
            types["D"] = ct.Id

    missing = [
        "{} ({})".format(label, name)
        for label, name in [
            ("A", CONNECTION_TYPE_A), ("B", CONNECTION_TYPE_B),
            ("C", CONNECTION_TYPE_C), ("D", CONNECTION_TYPE_D),
        ]
        if types[label] is None
    ]
    if missing:
        raise RuntimeError(
            "Connection type tidak ditemukan di project:\n  {}\n\n"
            "Type yang ada: {}\n\n"
            "Pastikan sudah di-load ke Revit project.".format(
                "\n  ".join(missing),
                ", ".join(found_names) if found_names else "(tidak ada)"
            )
        )
    return types

# ============================================================
# FUNGSI 3: cleanup_existing_connections
# ============================================================

def cleanup_existing_connections(doc):
    """Hapus semua StructuralConnectionHandler dari model."""
    collector = FilteredElementCollector(doc).OfClass(StructuralConnectionHandler)
    conn_ids = [c.Id for c in collector]
    if not conn_ids:
        print("  Tidak ada sambungan lama.")
        return 0
    for cid in conn_ids:
        doc.Delete(cid)
    print("  {} sambungan lama dihapus.".format(len(conn_ids)))
    return len(conn_ids)

# ============================================================
# FUNGSI 4: build_node_map
#UNTUK_SAMBUNGAN_BAJA — Index spasial: node_key → {cols, beams} untuk deteksi semua tipe joint
# ============================================================

def _node_key(coords):
    """Round koordinat mm ke int tuple untuk matching."""
    return (int(round(coords[0])), int(round(coords[1])), int(round(coords[2])))

def build_node_map(model_elements, group_names=None):
    """
    Pisahkan elemen ke columns / primary_beams / secondary_beams.
    Bangun node_map: node_key → {"cols": [elem], "beams": [elem]}
    """
    if group_names is None:
        group_names = {"kolom": "Kolom", "balok_induk": "Balok Induk", "balok_anak": "Balok Anak"}

    grp_kolom = group_names["kolom"]
    grp_induk = group_names["balok_induk"]
    grp_anak  = group_names["balok_anak"]

    columns        = []
    primary_beams  = []
    secondary_beams = []

    node_map = {}  # {(x,y,z): {"cols": [], "beams": []}}

    def _ensure(key):
        if key not in node_map:
            node_map[key] = {"cols": [], "beams": []}

    for elem in model_elements:
        grp = elem.get("group", "")
        topo = elem.get("topology", {})
        start = topo.get("start_node", [0, 0, 0])
        end   = topo.get("end_node",   [0, 0, 0])

        if grp == grp_kolom:
            columns.append(elem)
            for node in [start, end]:
                k = _node_key(node)
                _ensure(k)
                node_map[k]["cols"].append(elem)

        elif grp == grp_induk:
            primary_beams.append(elem)
            for node in [start, end]:
                k = _node_key(node)
                _ensure(k)
                node_map[k]["beams"].append(elem)

        elif grp == grp_anak:
            secondary_beams.append(elem)
            # Secondary tidak masuk node_map utama (endpoint di midspan)

    return node_map, columns, primary_beams, secondary_beams

# ============================================================
# FUNGSI 5: detect_column_beam_joints
#UNTUK_SAMBUNGAN_BAJA — Deteksi joint balok-kolom → moment end-plate (Tipe B)
# ============================================================

def detect_column_beam_joints(node_map, columns):
    """
    Deteksi joints balok-kolom di story paling bawah.
    Return list[{"col": elem, "beams": [elem,...], "node": tuple}]
    """
    # Tentukan elevasi z minimum dari semua end_node kolom
    col_end_z_vals = []
    for col in columns:
        end = col.get("topology", {}).get("end_node", [0, 0, 0])
        col_end_z_vals.append(int(round(end[2])))

    if not col_end_z_vals:
        return []

    min_col_z = min(col_end_z_vals)

    joints = []
    seen_col_ids = set()

    for col in columns:
        end = col.get("topology", {}).get("end_node", [0, 0, 0])
        col_z = int(round(end[2]))

        # Hanya story paling bawah
        if col_z != min_col_z:
            continue

        col_id = col.get("id")
        if col_id in seen_col_ids:
            continue
        seen_col_ids.add(col_id)

        node_key = _node_key(end)
        entry = node_map.get(node_key, {})
        beams_at_node = entry.get("beams", [])

        if not beams_at_node:
            continue  # Kolom tanpa balok di joint ini

        joints.append({
            "col":   col,
            "beams": beams_at_node,
            "node":  node_key,
        })

    return joints

# ============================================================
# HELPER: Arah balok dari local_axes
# ============================================================

def _revit_id(elem):
    """Konversi JSON element ID ke physical Revit ElementId integer.
    Kolom punya fabricated ID (physical_id * 1000 + story_num).
    Balok punya physical ID langsung.
    """
    eid = elem.get("id", 0)
    if elem.get("type") == "Column":
        return eid // 1000
    return eid

def _get_beam_dir(beam_elem):
    """Return (dx, dy, dz) arah longitudinal balok dari local_axes."""
    axes = beam_elem.get("local_axes", {})
    x = axes.get("x_axis", [1, 0, 0])
    return (x[0], x[1], x[2])

def _dot(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

# ============================================================
# FUNGSI 6: classify_joint
#UNTUK_SAMBUNGAN_BAJA — Klasifikasi web vs flange connection berdasar orientasi kolom → Tipe A atau B
# ============================================================

def classify_joint(doc, joint):
    """
    Kelompokkan balok per sumbu kolom (web vs flange) dan return
    list sub-connections [{"type":"A"|"B", "beam_ids":[int]}].
    """
    col_elem = joint["col"]
    col_id_int = _revit_id(col_elem)
    beams = joint["beams"]

    # Ambil orientasi kolom dari Revit FamilyInstance.GetTransform()
    revit_col = doc.GetElement(ElementId(col_id_int))

    col_basis_x = None  # flange direction
    col_basis_y = None  # web direction

    if revit_col is not None:
        try:
            tf = revit_col.GetTransform()
            col_basis_x = (tf.BasisX.X, tf.BasisX.Y, tf.BasisX.Z)
            col_basis_y = (tf.BasisY.X, tf.BasisY.Y, tf.BasisY.Z)
        except Exception:
            pass

    # Fallback: gunakan local_axes dari JSON
    if col_basis_x is None:
        axes = col_elem.get("local_axes", {})
        col_basis_x = tuple(axes.get("y_axis", [1, 0, 0]))  # major (flange)
        col_basis_y = tuple(axes.get("z_axis", [0, 1, 0]))  # minor (web)

    web_beams    = []
    flange_beams = []

    for b in beams:
        beam_dir = _get_beam_dir(b)
        dot_flange = abs(_dot(beam_dir, col_basis_x))
        dot_web    = abs(_dot(beam_dir, col_basis_y))

        if dot_web > dot_flange:
            web_beams.append(b)
        else:
            flange_beams.append(b)

    sub_conns = []

    # Web: 2 balok pair → Tipe A; 1 balok → Tipe B
    if len(web_beams) == 2:
        sub_conns.append({"type": "A", "beam_ids": [b["id"] for b in web_beams]})
    elif len(web_beams) == 1:
        sub_conns.append({"type": "B", "beam_ids": [web_beams[0]["id"]]})
    elif len(web_beams) > 2:
        # Lebih dari 2: ambil pair pertama sebagai A, sisanya B
        sub_conns.append({"type": "A", "beam_ids": [web_beams[0]["id"], web_beams[1]["id"]]})
        for b in web_beams[2:]:
            sub_conns.append({"type": "B", "beam_ids": [b["id"]]})

    # Flange: setiap balok → Tipe B
    for b in flange_beams:
        sub_conns.append({"type": "B", "beam_ids": [b["id"]]})

    return sub_conns

# ============================================================
# FUNGSI 7: create_connection
#UNTUK_SAMBUNGAN_BAJA — Buat StructuralConnectionHandler di Revit (primary + secondary elements)
# ============================================================

def create_connection(doc, primary_id, secondary_ids, type_id):
    """
    Buat 1 sambungan Revit.
    primary_id     : int (kolom atau balok induk)
    secondary_ids  : list[int] (balok-balok yang disambung)
    type_id        : ElementId tipe sambungan
    """
    ids = List[ElementId]()
    ids.Add(ElementId(primary_id))
    for sid in secondary_ids:
        ids.Add(ElementId(sid))
    conn = StructuralConnectionHandler.Create(doc, ids, type_id)
    return conn.Id.IntegerValue

# ============================================================
# FUNGSI 8: detect_secondary_beam_joints
#UNTUK_SAMBUNGAN_BAJA — Deteksi joint balok anak-balok induk → clip angle (Tipe C)
# ============================================================

def _point_on_segment(P, A, B, tol_mm):
    """
    True jika titik P berada di atas segmen [A,B] dalam toleransi tol_mm.
    Menggunakan: |PA| + |PB| ≈ |AB|
    """
    def dist(u, v):
        return math.sqrt(sum((ui - vi)**2 for ui, vi in zip(u, v)))

    AB = dist(A, B)
    PA = dist(P, A)
    PB = dist(P, B)
    return abs(PA + PB - AB) < tol_mm

def detect_secondary_beam_joints(secondary_beams, primary_beams):
    """
    Untuk setiap balok anak, cari balok induk yang terhubung.
    Prioritas: field 'parent_beams' di JSON.
    Fallback : geometric check |PA|+|PB| ≈ |AB|.
    Return list[{"secondary_id": int, "primary_ids": [int], "endpoints": [(x,y,z),...]}]
    """
    # Index primary beams by id for quick lookup
    primary_by_id = {b["id"]: b for b in primary_beams}

    joints = []

    for sec in secondary_beams:
        sec_id = sec["id"]
        topo   = sec.get("topology", {})
        sec_start = tuple(topo.get("start_node", [0, 0, 0]))
        sec_end   = tuple(topo.get("end_node",   [0, 0, 0]))

        # --- Coba field parent_beams terlebih dahulu ---
        parent_beams_field = sec.get("parent_beams", None)
        if parent_beams_field:
            parent_ids = [pb["id"] for pb in parent_beams_field
                          if pb["id"] in primary_by_id]
            if parent_ids:
                joints.append({
                    "secondary_id": sec_id,
                    "primary_ids":  parent_ids,
                    "endpoints":    [sec_start, sec_end],
                })
                continue

        # --- Fallback: geometric check ---
        matched_primary_ids = []
        for endpoint in [sec_start, sec_end]:
            for prim in primary_beams:
                prim_topo  = prim.get("topology", {})
                prim_start = tuple(prim_topo.get("start_node", [0, 0, 0]))
                prim_end   = tuple(prim_topo.get("end_node",   [0, 0, 0]))
                if _point_on_segment(endpoint, prim_start, prim_end, GEO_TOLERANCE_MM):
                    if prim["id"] not in matched_primary_ids:
                        matched_primary_ids.append(prim["id"])

        if matched_primary_ids:
            joints.append({
                "secondary_id": sec_id,
                "primary_ids":  matched_primary_ids,
                "endpoints":    [sec_start, sec_end],
            })
        else:
            print("  [WARN] Balok anak id={} tidak menemukan balok induk.".format(sec_id))

    return joints

# ============================================================
# FUNGSI 9: detect_column_splice_joints
#UNTUK_SAMBUNGAN_BAJA — Deteksi titik splice kolom antar lantai → column splice (Tipe D)
# ============================================================

def detect_column_splice_joints(node_map):
    """
    Deteksi titik splice kolom-kolom.
    Splice terjadi di node dimana satu kolom berakhir (end_node)
    dan kolom lain dimulai (start_node) dengan physical Revit ID berbeda.
    Ini terjadi jika fabrikasi kolom melebihi batas panjang (misal 12m).
    Return list[{"lower_col": elem, "upper_col": elem, "node": tuple}]
    """
    splices = []
    seen = set()

    for node_key, entry in node_map.items():
        cols_at_node = entry.get("cols", [])
        if len(cols_at_node) < 2:
            continue

        # Pisahkan kolom yang berakhir vs dimulai di node ini
        ending = []
        starting = []

        for col in cols_at_node:
            topo = col.get("topology", {})
            end_key = _node_key(topo.get("end_node", [0, 0, 0]))
            start_key = _node_key(topo.get("start_node", [0, 0, 0]))

            if end_key == node_key:
                ending.append(col)
            if start_key == node_key:
                starting.append(col)

        # Splice = kolom bawah berakhir + kolom atas dimulai, physical ID berbeda
        for lower_col in ending:
            for upper_col in starting:
                lower_phys = _revit_id(lower_col)
                upper_phys = _revit_id(upper_col)

                if lower_phys == upper_phys:
                    continue

                pair_key = (lower_phys, upper_phys)
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                splices.append({
                    "lower_col": lower_col,
                    "upper_col": upper_col,
                    "node":      node_key,
                })

    return splices

# ============================================================
# FUNGSI 10: copy_to_upper_levels
#UNTUK_SAMBUNGAN_BAJA — Copy sambungan story bawah ke story atas via ElementTransformUtils
# ============================================================

def _mm_to_ft(mm):
    return mm / 304.8

def copy_to_upper_levels(doc, base_conn_ids, sorted_z_mm):
    """
    Copy semua sambungan dari story bawah ke story atas.
    sorted_z_mm: list elevasi z (mm) dari setiap story, ascending.
    """
    if len(sorted_z_mm) < 2:
        return []

    base_z = sorted_z_mm[0]
    ids_to_copy = List[ElementId]()
    for cid in base_conn_ids:
        ids_to_copy.Add(ElementId(cid))
    copied = []

    for upper_z in sorted_z_mm[1:]:
        dz_ft = _mm_to_ft(upper_z - base_z)
        translation = XYZ(0, 0, dz_ft)
        new_ids = ElementTransformUtils.CopyElements(doc, ids_to_copy, translation)
        copied.extend([nid.IntegerValue for nid in new_ids])
        print("  Copied {} sambungan ke elevasi {:.1f}m".format(
            len(list(new_ids)), upper_z / 1000.0))

    return copied

# ============================================================
# FUNGSI 11: run_connection_design — Orkestrasi Utama
#UNTUK_SAMBUNGAN_BAJA — Pipeline utama: load JSON → detect joints → classify → create → copy → export
# ============================================================

def run_connection_design(doc):
    out = script.get_output()
    out.print_md("# Steel Connection — Fase 1\n\n---")

    # ---- 1. Load JSON ----
    print("[1/9] Membaca Result.json ...")
    try:
        model_elements, seismic_params, group_names = load_result_json(RESULT_JSON)
    except RuntimeError as e:
        out.print_md("**ERROR**: {}".format(str(e)))
        return

    n_story   = seismic_params.get("N_STORY", 1)
    h_mm      = seismic_params.get("HEIGHT_MM", 4000)
    frame_type = seismic_params.get("frame_type", "?")
    print("  N_STORY={}, H={}mm, frame_type={}".format(n_story, h_mm, frame_type))

    # ---- 2. Load connection types ----
    print("[2/9] Mencari tipe sambungan di Revit ...")
    try:
        conn_types = load_connection_types(doc)
    except RuntimeError as e:
        out.print_md("**ERROR**: {}".format(str(e)))
        return

    print("  Tipe A ditemukan: {}".format(CONNECTION_TYPE_A))
    print("  Tipe B ditemukan: {}".format(CONNECTION_TYPE_B))
    print("  Tipe C ditemukan: {}".format(CONNECTION_TYPE_C))
    print("  Tipe D ditemukan: {}".format(CONNECTION_TYPE_D))

    # ---- 3. Cleanup ----
    print("[3/9] Menghapus sambungan lama ...")
    with revit.Transaction("Delete Old Connections"):
        cleanup_existing_connections(doc)

    # ---- 4. Build node map ----
    print("[4/9] Membangun node map ...")
    node_map, columns, primary_beams, secondary_beams = build_node_map(model_elements, group_names)
    print("  Kolom      : {}".format(len(columns)))
    print("  Balok induk: {}".format(len(primary_beams)))
    print("  Balok anak : {}".format(len(secondary_beams)))
    secondary_present = len(secondary_beams) > 0

    # ---- 5. Detect joints balok-kolom ----
    print("[5/9] Mendeteksi joints balok-kolom ...")
    joints = detect_column_beam_joints(node_map, columns)
    print("  {} joints terdeteksi di story bawah.".format(len(joints)))

    if not joints and not secondary_present:
        out.print_md("**WARN**: Tidak ada joint yang terdeteksi. "
                     "Periksa Result.json dan model Revit.")
        return

    # ---- 6. Detect splice joints ----
    splice_conn_ids = []
    print("[6/9] Mendeteksi splice kolom-kolom ...")
    splice_joints = detect_column_splice_joints(node_map)
    print("  {} splice terdeteksi.".format(len(splice_joints)))

    # ---- 7. Create semua sambungan (1 transaksi) ----
    print("[7/9] Membuat sambungan ...")
    all_connections  = []
    base_conn_ids    = []

    with revit.Transaction("Create Steel Connections"):

        # 7a. Sambungan balok-kolom (Tipe A & B)
        for joint in joints:
            col_id = _revit_id(joint["col"])
            sub_conns = classify_joint(doc, joint)

            for sc in sub_conns:
                type_key = sc["type"]
                beam_ids = sc["beam_ids"]
                type_id  = conn_types[type_key]

                try:
                    conn_int_id = create_connection(doc, col_id, beam_ids, type_id)
                    base_conn_ids.append(conn_int_id)
                    all_connections.append({
                        "type":      "Tipe {}".format(type_key),
                        "col_id":    col_id,
                        "beam_ids":  beam_ids,
                        "conn_id":   conn_int_id,
                        "level":     "story bawah",
                        "joint_node": list(joint["node"]),
                    })
                    print("  [OK] Tipe {} | col={} beam={}".format(
                        type_key, col_id, beam_ids))
                except Exception as ex:
                    print("  [ERR] Tipe {} | col={} beam={} | {}".format(
                        type_key, col_id, beam_ids, ex))

        # 7b. Sambungan balok anak (Tipe C)
        if secondary_present:
            sec_joints = detect_secondary_beam_joints(secondary_beams, primary_beams)
            print("  {} joints balok anak terdeteksi.".format(len(sec_joints)))

            for sj in sec_joints:
                sec_id     = sj["secondary_id"]
                prim_ids   = sj["primary_ids"]
                type_id_c  = conn_types["C"]

                for prim_id in prim_ids:
                    try:
                        conn_int_id = create_connection(doc, prim_id, [sec_id], type_id_c)
                        base_conn_ids.append(conn_int_id)
                        all_connections.append({
                            "type":       "Tipe C",
                            "primary_id": prim_id,
                            "beam_ids":   [sec_id],
                            "conn_id":    conn_int_id,
                            "level":      "story bawah",
                        })
                        print("  [OK] Tipe C | primary={} secondary={}".format(
                            prim_id, sec_id))
                    except Exception as ex:
                        print("  [ERR] Tipe C | primary={} secondary={} | {}".format(
                            prim_id, sec_id, ex))

        # 7c. Sambungan splice kolom-kolom (Tipe D)
        if splice_joints and conn_types.get("D"):
            type_id_d = conn_types["D"]
            for sj in splice_joints:
                lower_id = _revit_id(sj["lower_col"])
                upper_id = _revit_id(sj["upper_col"])

                try:
                    conn_int_id = create_connection(doc, lower_id, [upper_id], type_id_d)
                    splice_conn_ids.append(conn_int_id)
                    all_connections.append({
                        "type":        "Tipe D",
                        "lower_col_id": lower_id,
                        "upper_col_id": upper_id,
                        "conn_id":      conn_int_id,
                        "level":        "z={:.0f}mm".format(sj["node"][2]),
                        "joint_node":   list(sj["node"]),
                    })
                    print("  [OK] Tipe D | lower={} upper={} z={}mm".format(
                        lower_id, upper_id, sj["node"][2]))
                except Exception as ex:
                    print("  [ERR] Tipe D | lower={} upper={} | {}".format(
                        lower_id, upper_id, ex))
        elif splice_joints and not conn_types.get("D"):
            print("  [WARN] Splice terdeteksi tapi tipe '{}' belum di-load.".format(
                CONNECTION_TYPE_D))
    print("  {} sambungan story bawah, {} splice.".format(
        len(base_conn_ids), len(splice_conn_ids)))

    # ---- 8. Copy ke story atas (hanya A/B/C, bukan splice) ----
    copied_ids = []
    if n_story > 1 and base_conn_ids:
        print("[8/9] Copy sambungan ke story atas ...")
        col_end_z_set = set()
        for col in columns:
            z = col.get("topology", {}).get("end_node", [0, 0, 0])[2]
            col_end_z_set.add(int(round(z)))
        sorted_z_mm = sorted(col_end_z_set)

        with revit.Transaction("Copy Connections to Upper Levels"):
            copied_ids = copy_to_upper_levels(doc, base_conn_ids, sorted_z_mm)
        print("  {} sambungan di-copy ke {} story atas.".format(
            len(copied_ids), len(sorted_z_mm) - 1))
    else:
        print("[8/9] Hanya 1 story, skip copy.")

    # ---- 9. Export & Display ----
    print("[9/9] Ekspor Connection Result.json ...")
    output_data = {
        "frame_type":    frame_type,
        "n_story":       n_story,
        "total":         len(base_conn_ids) + len(splice_conn_ids),
        "copied_total":  len(copied_ids),
        "splice_total":  len(splice_conn_ids),
        "connections":   all_connections,
    }
    with open(OUTPUT_JSON, "w") as f:
        json.dump(output_data, f, indent=2)
    print("  Saved: {}".format(OUTPUT_JSON))

    # Display summary
    count_a = sum(1 for c in all_connections if c["type"] == "Tipe A")
    count_b = sum(1 for c in all_connections if c["type"] == "Tipe B")
    count_c = sum(1 for c in all_connections if c["type"] == "Tipe C")
    count_d = sum(1 for c in all_connections if c["type"] == "Tipe D")

    copy_row = ""
    if copied_ids:
        copy_row = "\n| Copy ke story atas | — | {} sambungan × {} story |".format(
            len(base_conn_ids), n_story - 1)

    summary_md = (
        "## Hasil Sambungan\n\n"
        "| Tipe | Nama Sambungan | Jumlah |\n"
        "|:----:|----------------|:------:|\n"
        "| **A** | {type_a} | {cnt_a} |\n"
        "| **B** | {type_b} | {cnt_b} |\n"
        "| **C** | {type_c} | {cnt_c} |\n"
        "| **D** | {type_d} | {cnt_d} |\n"
        "| **Total** | *(story bawah + splice)* | **{total}** |"
        "{copy_row}\n\n"
        "Output: `{output}`"
    ).format(
        type_a   = CONNECTION_TYPE_A,
        type_b   = CONNECTION_TYPE_B,
        type_c   = CONNECTION_TYPE_C,
        type_d   = CONNECTION_TYPE_D,
        cnt_a    = count_a,
        cnt_b    = count_b,
        cnt_c    = count_c,
        cnt_d    = count_d,
        total    = len(base_conn_ids) + len(splice_conn_ids),
        copy_row = copy_row,
        output   = OUTPUT_JSON,
    )
    out.print_md(summary_md)

# ============================================================
# ENTRY POINT
# ============================================================

run_connection_design(doc)
