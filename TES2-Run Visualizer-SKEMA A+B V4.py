#! python3
# pyrevit: nosplash nooutput
import clr
import json
import os
import subprocess
import sys
import uuid

try:
    if sys.stdout and not hasattr(sys.stdout, "flush"):
        sys.stdout.flush = lambda: None
except Exception:
    pass

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import *             # noqa: F401,F403
from Autodesk.Revit.UI import TaskDialog
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.UI.Events import IdlingEventArgs
from System import EventHandler             # noqa (IronPython .NET bridge)
from pyrevit import revit

__title__ = "Visualize"
__author__ = "ROIDA"
__doc__ = "Open force diagram window for selected Revit structural elements"

# ===================================================================
# PATHS
# ===================================================================
SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR          = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
CREATE_DIR         = os.path.join(PANEL_DIR, "Create.pushbutton")
DESIGN_DIR         = os.path.join(PANEL_DIR, "Design.pushbutton", "Steel Design Engine")

RESULT_PATH        = os.path.join(CREATE_DIR, "Result.json")
DESIGN_RESULT_PATH = os.path.join(DESIGN_DIR, "Design Result.json")

# Auto-Select _New files (dipakai jika tersedia)
AUTOSELECT_DIR         = os.path.join(PANEL_DIR, "Auto Select.pushbutton")
RESULT_NEW_PATH        = os.path.join(AUTOSELECT_DIR, "Result_New.json")
DESIGN_RESULT_NEW_PATH = os.path.join(AUTOSELECT_DIR, "Design_Result_New.json")

ENGINE_PATH        = os.path.join(SCRIPT_DIR, "Visualizer Engine", "Visualizer Engine.py")
SENTINEL_PATH      = os.path.join(SCRIPT_DIR, "_roida_next_elem.flag")
RESPONSE_PATH      = os.path.join(SCRIPT_DIR, "_roida_elem_response.json")
PID_PATH           = os.path.join(SCRIPT_DIR, "_roida_visualizer.pid")
SESSION_ID_PATH    = os.path.join(SCRIPT_DIR, "_roida_session.id")

# Unique ID untuk sesi ini — dipakai handler untuk deteksi diri sendiri sudah stale
_SESSION_ID = str(uuid.uuid4())


def _find_python():
    candidates = [
        r"C:\Users\hp\AppData\Local\Programs\Python\Python312\pythonw.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python311\pythonw.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python310\pythonw.exe",
        r"C:\Python312\pythonw.exe",
        r"C:\Python311\pythonw.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python312\python.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python311\python.exe",
        r"C:\Users\hp\AppData\Local\Programs\Python\Python310\python.exe",
        r"C:\Python312\python.exe",
        r"C:\Python311\python.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "pythonw"


PYTHON_EXE       = _find_python()
CREATE_NO_WINDOW = 0x08000000

# ===================================================================
# MODULE-LEVEL STATE  (tidak ter-GC setelah main() return)
# ===================================================================
_G = {
    "uiapp":      None,
    "uidoc":      None,
    "design_arg": "",
    "delegate":   None,
}

# ===================================================================
# HELPERS
# ===================================================================

def _kill_existing():
    """Kill Visualizer yang sedang berjalan (cegah window ganda)."""
    if not os.path.exists(PID_PATH):
        return
    try:
        with open(PID_PATH, "r") as f:
            pid = int(f.read().strip())
        import signal
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        os.remove(PID_PATH)
    except Exception:
        pass


#UNTUK_SAMBUNGAN_BAJA — Pattern deteksi data source (Create vs Auto Select) — Connection Engine harus replika logika ini
def _get_active_paths():
    """Return (result_path, design_result_path).

    Pilih berdasarkan timestamp terbaru:
    - Jika Auto Select Result_New.json LEBIH BARU dari Create Result.json → pakai Auto Select
    - Sebaliknya → pakai Create (default)
    Ini mencegah file Auto Select basi menimpa hasil Create yang baru.
    """
    create_exists = os.path.exists(RESULT_PATH)
    auto_exists = (os.path.exists(RESULT_NEW_PATH)
                   and os.path.exists(DESIGN_RESULT_NEW_PATH))

    if auto_exists and create_exists:
        # Pakai yang lebih baru
        if os.path.getmtime(RESULT_NEW_PATH) > os.path.getmtime(RESULT_PATH):
            return RESULT_NEW_PATH, DESIGN_RESULT_NEW_PATH
        return RESULT_PATH, DESIGN_RESULT_PATH
    elif auto_exists:
        return RESULT_NEW_PATH, DESIGN_RESULT_NEW_PATH
    return RESULT_PATH, DESIGN_RESULT_PATH


def _revit_to_analytical(revit_ids):
    """Map Revit element IDs → analytical element IDs via Result.json.

    Kolom multi-story: satu Revit element_id = beberapa analytical element
    (revit_id*1000 + story_index). Balok: 1:1 mapping langsung.
    """
    result_path, _ = _get_active_paths()
    if not os.path.exists(result_path):
        return revit_ids  # fallback: return as-is

    try:
        with open(result_path, "r") as f:
            data = json.load(f)
    except Exception:
        return revit_ids

    model_elems = data.get("model_data", {}).get("model_elements", [])

    # Kumpulkan semua analytical ID
    all_analytical = set()
    for e in model_elems:
        try:
            all_analytical.add(int(e["id"]))
        except (KeyError, ValueError):
            continue

    # Identifikasi composite bases: kolom dengan >1 segmen sharing base
    col_base_count = {}
    for e in model_elems:
        if e.get("type", "").lower() != "column":
            continue
        try:
            cid = int(e["id"])
        except (KeyError, ValueError):
            continue
        base = cid // 1000
        col_base_count[base] = col_base_count.get(base, 0) + 1
    composite_bases = {b for b, cnt in col_base_count.items() if cnt > 1}

    # Bangun segment_map hanya untuk composite bases
    segment_map = {}
    for e in model_elems:
        if e.get("type", "").lower() != "column":
            continue
        try:
            cid = int(e["id"])
        except (KeyError, ValueError):
            continue
        base = cid // 1000
        if base in composite_bases:
            segment_map.setdefault(base, []).append(cid)

    resolved = []
    for rid in revit_ids:
        rid_int = int(rid)
        if rid_int in all_analytical:
            # Direct match (balok, atau single-story tanpa suffix)
            resolved.append(str(rid_int))
        elif rid_int in segment_map:
            # Kolom multi-story: expand ke semua segmen
            for seg_id in sorted(segment_map[rid_int]):
                resolved.append(str(seg_id))

    # Jika tidak ada ID yang cocok (misal: Auto Select punya ID berbeda),
    # kirim semua analytical IDs agar Visualizer tetap bisa menampilkan data
    if not resolved:
        resolved = [str(aid) for aid in sorted(all_analytical)]

    return resolved


def _launch(elem_ids, design_arg):
    _kill_existing()

    result_path, _ = _get_active_paths()

    cmd = [
        PYTHON_EXE, ENGINE_PATH,
        "--result",   result_path,
        "--elements", ",".join(str(x) for x in elem_ids),
        "--sentinel", SENTINEL_PATH,
        "--response", RESPONSE_PATH,
    ]
    if design_arg:
        cmd += ["--design", design_arg]
    proc = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)

    try:
        with open(PID_PATH, "w") as f:
            f.write(str(proc.pid))
    except Exception:
        pass

    return proc


def _pick():
    """PickObjects → list str ID, atau [] jika Escape."""
    try:
        refs = _G["uidoc"].Selection.PickObjects(
            ObjectType.Element,
            "Pilih elemen struktural, lalu Enter  —  Esc untuk batal")
        return [str(r.ElementId.IntegerValue) for r in refs]
    except Exception:
        return []


def _unregister():
    if _G["delegate"] is not None:
        try:
            _G["uiapp"].Idling -= _G["delegate"]
        except Exception:
            pass
        _G["delegate"] = None


def _register():
    _unregister()
    dg = EventHandler[IdlingEventArgs](_idling_handler)
    _G["delegate"] = dg
    try:
        _G["uiapp"].Idling += dg
    except Exception:
        _G["delegate"] = None


# ===================================================================
# IDLING HANDLER
# ===================================================================

def _idling_handler(_sender, _args):
    # Cek apakah sesi ini masih aktif.
    # Jika session ID file berisi ID berbeda, berarti ada run baru —
    # handler ini sudah stale, unregister diri sendiri lalu keluar.
    try:
        with open(SESSION_ID_PATH, "r") as f:
            active_id = f.read().strip()
        if active_id != _SESSION_ID:
            _unregister()
            return
    except Exception:
        _unregister()
        return

    if not os.path.exists(SENTINEL_PATH):
        return

    try:
        os.remove(SENTINEL_PATH)
    except Exception:
        pass

    if os.path.exists(RESPONSE_PATH):
        try:
            os.remove(RESPONSE_PATH)
        except Exception:
            pass

    new_ids = _pick()
    # Resolve Revit IDs → analytical IDs
    new_ids = _revit_to_analytical(new_ids) if new_ids else new_ids

    try:
        with open(RESPONSE_PATH, "w") as f:
            json.dump({"element_ids": new_ids}, f)
    except Exception:
        pass


# ===================================================================
# MAIN
# ===================================================================

def main():
    if not os.path.exists(RESULT_PATH):
        TaskDialog.Show("ROIDA — Visualize",
                        "Result.json tidak ditemukan.\nJalankan Create terlebih dahulu.")
        return

    if not os.path.exists(ENGINE_PATH):
        TaskDialog.Show("ROIDA — Visualize",
                        "Visualizer Engine tidak ditemukan.\nPath: {}".format(ENGINE_PATH))
        return

    _G["uiapp"]      = revit.HOST_APP.uiapp
    _G["uidoc"]      = revit.uidoc

    # Design result — hanya kirim jika ada DAN tidak lebih lama dari Result
    # (mencegah DCR basi dari run sebelumnya tampil setelah Create baru)
    active_result, active_design = _get_active_paths()
    if (os.path.exists(active_design) and os.path.exists(active_result)
            and os.path.getmtime(active_design) >= os.path.getmtime(active_result)):
        _G["design_arg"] = active_design
    else:
        _G["design_arg"] = ""

    # Tulis session ID — handler lama akan mendeteksi ini dan unregister diri sendiri
    try:
        with open(SESSION_ID_PATH, "w") as f:
            f.write(_SESSION_ID)
    except Exception:
        pass

    _kill_existing()
    for stale in (SENTINEL_PATH, RESPONSE_PATH):
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except Exception:
                pass

    elem_ids = _pick()
    if not elem_ids:
        return

    # Resolve Revit IDs → analytical IDs (kolom multi-story di-expand)
    elem_ids = _revit_to_analytical(elem_ids)

    try:
        _launch(elem_ids, _G["design_arg"])
    except Exception as e:
        TaskDialog.Show("ROIDA — Visualize",
                        "Gagal membuka Visualizer:\n{}".format(str(e)))
        return

    _register()


main()
