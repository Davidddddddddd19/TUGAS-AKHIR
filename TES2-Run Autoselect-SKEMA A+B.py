#! python3
import clr
import json
import os
import subprocess
import sys

try:
    if sys.stdout and not hasattr(sys.stdout, "flush"):
        sys.stdout.flush = lambda: None
except Exception:
    pass

# Load Revit API
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import *  # noqa: F401,F403
from pyrevit import script

"""
Auto-Select Pushbutton — ROIDA
==============================
Launches Autoselect Engine.py via subprocess.
Reads Design Result.json, iterates IWF_DATABASE to find adequate sections,
displays recommendation table in pyRevit console.
"""

__title__ = "Auto\nSelect"
__author__ = "ROIDA"
__doc__ = "Auto-select adequate IWF sections for NG elements (AISC 360-22 LRFD)"

# ═══════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR    = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
DESIGN_DIR   = os.path.join(PANEL_DIR, "Design.pushbutton", "Run Design Check")

DESIGN_RESULT_PATH    = os.path.join(DESIGN_DIR, "Design Result.json")
ENGINE_PATH           = os.path.join(SCRIPT_DIR, "Autoselect Engine.py")
AUTOSELECT_RESULT_PATH = os.path.join(SCRIPT_DIR, "Auto-Select Result.json")
HISTORY_PATH          = os.path.join(SCRIPT_DIR, "autoselect_history.json")


def _find_python():
    """Locate a valid Python 3 executable (not Revit.exe)."""
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

# ═══════════════════════════════════════════════════════════════════
# PYREVIT OUTPUT
# ═══════════════════════════════════════════════════════════════════
out = script.get_output()


def print_table(data, columns, title=""):
    """Render a Markdown table via pyRevit output."""
    if not data:
        return
    if title:
        out.print_md("### " + title)
    md  = "| " + " | ".join(columns) + " |\n"
    md += "| " + " | ".join([":---:" for _ in columns]) + " |\n"
    for row in data:
        md += "| " + " | ".join([str(x) for x in row]) + " |\n"
    out.print_md(md)
    out.print_md("---")


# ═══════════════════════════════════════════════════════════════════
# ENGINE RUNNER
# ═══════════════════════════════════════════════════════════════════

def run_engine():
    """Invoke Autoselect Engine as a subprocess."""
    out.print_md("# ROIDA — AUTO-SELECT PENAMPANG")
    out.print_md("**Basis:** AISC 360-22 LRFD | DCR target ≤ 0.95")

    if not os.path.exists(DESIGN_RESULT_PATH):
        out.print_md("> **ERROR:** Design Result.json tidak ditemukan.")
        out.print_md("> Path: `{}`".format(DESIGN_RESULT_PATH))
        out.print_md("> Jalankan **Design Check** terlebih dahulu.")
        return False

    if not os.path.exists(ENGINE_PATH):
        out.print_md("> **ERROR:** Autoselect Engine.py tidak ditemukan.")
        out.print_md("> Path: `{}`".format(ENGINE_PATH))
        return False

    out.print_md("Menjalankan Autoselect Engine...")
    out.print_md("- Python : `{}`".format(PYTHON_EXE))
    out.print_md("- Engine : `{}`".format(ENGINE_PATH))
    out.print_md("- Input  : `{}`".format(DESIGN_RESULT_PATH))
    out.print_md("- Output : `{}`".format(AUTOSELECT_RESULT_PATH))

    try:
        proc = subprocess.run(
            [PYTHON_EXE, ENGINE_PATH, DESIGN_RESULT_PATH, AUTOSELECT_RESULT_PATH],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            out.print_md("> **ERROR:** Autoselect Engine gagal (return code {})".format(
                proc.returncode))
            if proc.stderr:
                print(proc.stderr)
            return False

        if proc.stdout:
            print(proc.stdout)

        return True

    except subprocess.TimeoutExpired:
        out.print_md("> **ERROR:** Timeout (>120 detik)")
        return False
    except Exception as e:
        out.print_md("> **ERROR:** {}".format(str(e)))
        return False


# ═══════════════════════════════════════════════════════════════════
# DISPLAY RESULTS
# ═══════════════════════════════════════════════════════════════════

def display_results():
    """Read Auto-Select Result.json and display recommendation tables."""
    if not os.path.exists(AUTOSELECT_RESULT_PATH):
        out.print_md("> **ERROR:** Auto-Select Result.json tidak ditemukan.")
        return

    with open(AUTOSELECT_RESULT_PATH, "r") as f:
        data = json.load(f)

    summary    = data.get("summary",       {})
    changes    = data.get("changes",       [])
    unresolved = data.get("not_resolved",  [])
    ok_elems   = data.get("ok_elements",   [])
    timestamp  = data.get("timestamp",     "")

    # ── Summary header ─────────────────────────────────────────────
    out.print_md("## HASIL AUTO-SELECT")
    out.print_md("**Waktu:** {}".format(timestamp))

    summary_rows = [
        ["Total Elemen",     str(summary.get("total_elements", 0))],
        ["Elemen OK",        str(summary.get("ok_elements", 0))],
        ["Elemen NG",        str(summary.get("ng_elements", 0))],
        ["Berhasil dipilih", str(summary.get("changed", 0))],
        ["Tidak teratasi",   str(summary.get("not_resolved", 0))],
    ]
    print_table(summary_rows, ["Keterangan", "Jumlah"], title="Ringkasan")

    # ── Recommended changes ────────────────────────────────────────
    if changes:
        rows = []
        for c in changes:
            rows.append([
                c.get("label_name",  "?"),
                c.get("design_type", "?"),
                c["before"]["section"],
                "{:.3f}".format(c["before"]["dcr"]),
                c["after"]["section"],
                "{:.3f}".format(c["after"]["dcr"]),
                "{:+.1f}%".format(c.get("weight_increase_pct", 0.0)),
            ])
        print_table(
            rows,
            ["Label", "Tipe", "Section Asal", "DCR Asal",
             "Rekomendasi", "DCR Baru", "ΔBerat%"],
            title="Rekomendasi Penampang (Elemen NG → Selesai)",
        )

        out.print_md(
            "> **Instruksi:** Update `SECTION_COL` / `SECTION_BEAM_EXT` "
            "di `Create.pushbutton/script.py` sesuai rekomendasi di atas, "
            "lalu klik **Create** ulang untuk memperbarui model."
        )

    # ── Unresolved ─────────────────────────────────────────────────
    if unresolved:
        rows = []
        for nc in unresolved:
            rows.append([
                nc.get("label_name",  "?"),
                nc.get("design_type", "?"),
                nc.get("section",     "?"),
                "{:.3f}".format(nc.get("dcr", 0.0)),
                nc.get("note", ""),
            ])
        print_table(
            rows,
            ["Label", "Tipe", "Section Asal", "DCR", "Keterangan"],
            title="⚠ Tidak Dapat Diselesaikan (DCR terlalu tinggi untuk semua IWF dalam database)",
        )

    # ── OK elements (already passing) ─────────────────────────────
    if ok_elems:
        rows = []
        for e in ok_elems:
            rows.append([
                e.get("label_name",  "?"),
                e.get("design_type", "?"),
                e.get("section",     "?"),
                "{:.3f}".format(e.get("dcr", 0.0)),
                "OK",
            ])
        print_table(
            rows,
            ["Label", "Tipe", "Section", "DCR", "Status"],
            title="Elemen OK (tidak perlu diubah)",
        )

    # ── History hint ───────────────────────────────────────────────
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r") as f:
                hist = json.load(f)
            n_runs = len(hist.get("runs", []))
            out.print_md(
                "> **History:** {} iterasi tersimpan di `autoselect_history.json`".format(n_runs)
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    success = run_engine()
    if success:
        display_results()


main()
