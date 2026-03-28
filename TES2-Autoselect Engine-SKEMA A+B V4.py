"""
Auto-Select Engine — ROIDA (Full Pipeline Rewrite)
====================================================
Standalone Python 3.12 engine with FULL AISC 360-22 calculations.

Imports Steel Design Engine classes directly via importlib, performs
GROUP-BASED optimization (Kolom, Balok Induk, Balok Anak) using numpy,
then re-runs Analysis (OpenSees) + Design Check to verify.

Pipeline:
  Phase 1: Quick optimization — find optimal section per group using
           original forces from Design Result.json
  Phase 2: Re-analysis — update model data → OpenSees → Steel Design Engine
           to verify with new internal forces

Output files (NEVER modifies originals):
  - Result_New.json          — model + analysis after section change
  - Design_Result_New.json   — design check after section change
  - Auto-Select Result.json  — summary + recommendations

Usage:
    python "Autoselect Engine.py" <design_result.json> <result.json> <output_dir>
"""

import json
import math
import sys
import os
import copy
import subprocess
import importlib.util
from datetime import datetime

import numpy as np

# ═══════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

PYTHON_EXE = r"C:\Users\hp\AppData\Local\Programs\Python\Python312\python.exe"
ANALYSIS_SCRIPT = os.path.join(PANEL_DIR, "Create.pushbutton", "Analysis", "Analysis.py")
DESIGN_ENGINE_SCRIPT = os.path.join(PANEL_DIR, "Design.pushbutton",
                                     "Steel Design Engine", "Steel Design Engine.py")

# ═══════════════════════════════════════════════════════════════════
# IMPORT STEEL DESIGN ENGINE via importlib (filename has spaces)
# ═══════════════════════════════════════════════════════════════════
_spec = importlib.util.spec_from_file_location("steel_design_engine",
                                                DESIGN_ENGINE_SCRIPT)
SDE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SDE)

# Imported classes:
#   SDE.SectionProperties, SDE.SectionClassification,
#   SDE.CompressionCapacity, SDE.TensionCapacity,
#   SDE.FlexureCapacity, SDE.ShearCapacity,
#   SDE.PMMCheck, SDE.DesignForces, SDE.DesignCapacity

# ═══════════════════════════════════════════════════════════════════
# IWF DATABASE PER GROUP
# ═══════════════════════════════════════════════════════════════════
#
#   User mengatur profil kandidat per group di sini.
#   Auto-Select hanya akan memilih dari profil dalam group masing-masing.
#   Format: {name: {d, bf, tw, tf, r}} — mm
#
#   Tips:
#   - Kolom biasanya butuh profil wide-flange (bf ≈ d) untuk stabilitas
#   - Balok Induk biasanya lebih besar dari Balok Anak
#   - Urutkan dari ringan ke berat untuk optimasi tercepat
#
# ───────────────────────────────────────────────────────────────────

IWF_DB_KOLOM = {
    # ── Narrow-flange (bf << d) ──
    "IWF194x150x6x9":          {"d": 194,   "bf": 150,   "tw": 6,    "tf": 9,    "r": 11},
    "IWF200x100x5.5x8":        {"d": 200,   "bf": 100,   "tw": 5.5,  "tf": 8,    "r": 11},
    "IWF248x124x5x8":          {"d": 248,   "bf": 124,   "tw": 5,    "tf": 8,    "r": 12},
    "IWF250x125x6x9":          {"d": 250,   "bf": 125,   "tw": 6,    "tf": 9,    "r": 12},
    "IWF294x200x8x12":         {"d": 294,   "bf": 200,   "tw": 8,    "tf": 12,   "r": 13},
    "IWF298x149x5.5x8":        {"d": 298,   "bf": 149,   "tw": 5.5,  "tf": 8,    "r": 11},
    "IWF300x150x6.5x9":        {"d": 300,   "bf": 150,   "tw": 6.5,  "tf": 9,    "r": 13},
    "IWF303.4x165x6x10.2":     {"d": 303.4, "bf": 165,   "tw": 6,    "tf": 10.2, "r": 8.9},
    "IWF346x174x6x9":          {"d": 346,   "bf": 174,   "tw": 6,    "tf": 9,    "r": 13},
    "IWF350x175x7x11":         {"d": 350,   "bf": 175,   "tw": 7,    "tf": 11,   "r": 13},
    "IWF396x199x7x11":         {"d": 396,   "bf": 199,   "tw": 7,    "tf": 11,   "r": 13},
    "IWF400x200x8x13":         {"d": 400,   "bf": 200,   "tw": 8,    "tf": 13,   "r": 16},
    "IWF446x199x8x12":         {"d": 446,   "bf": 199,   "tw": 8,    "tf": 12,   "r": 16},
    "IWF450x200x9x14":         {"d": 450,   "bf": 200,   "tw": 9,    "tf": 14,   "r": 16},
    "IWF496x199x9x14":         {"d": 496,   "bf": 199,   "tw": 9,    "tf": 14,   "r": 16},
    "IWF500x200x10x16":        {"d": 500,   "bf": 200,   "tw": 10,   "tf": 16,   "r": 20},
    "IWF506x201x11x19":        {"d": 506,   "bf": 201,   "tw": 11,   "tf": 19,   "r": 20},
    "IWF596x199x10x15":        {"d": 596,   "bf": 199,   "tw": 10,   "tf": 15,   "r": 22},
    "IWF600x200x11x17":        {"d": 600,   "bf": 200,   "tw": 11,   "tf": 17,   "r": 22},
    "IWF606x201x12x20":        {"d": 606,   "bf": 201,   "tw": 12,   "tf": 20,   "r": 22},
    "IWF612x202x13x23":        {"d": 612,   "bf": 202,   "tw": 13,   "tf": 23,   "r": 22},
    # ── Wide-flange (bf ≈ d) — cocok untuk kolom ──
    "IWF200x200x8x12":         {"d": 200,   "bf": 200,   "tw": 8,    "tf": 12,   "r": 13},
    "IWF244x175x7x11":         {"d": 244,   "bf": 175,   "tw": 7,    "tf": 11,   "r": 11},
    "IWF250x250x9x14":         {"d": 250,   "bf": 250,   "tw": 9,    "tf": 14,   "r": 16},
    "IWF300x300x10x15":        {"d": 300,   "bf": 300,   "tw": 10,   "tf": 15,   "r": 18},
    "IWF300x305x15x15":        {"d": 300,   "bf": 305,   "tw": 15,   "tf": 15,   "r": 18},
    "IWF307.9x305.3x9.9x15.4": {"d": 307.9, "bf": 305.3, "tw": 9.9,  "tf": 15.4, "r": 15.2},
    "IWF340x250x9x14":         {"d": 340,   "bf": 250,   "tw": 9,    "tf": 14,   "r": 16},
    "IWF344x348x10x16":        {"d": 344,   "bf": 348,   "tw": 10,   "tf": 16,   "r": 20},
    "IWF350x350x12x19":        {"d": 350,   "bf": 350,   "tw": 12,   "tf": 19,   "r": 20},
    "IWF386x299x9x14":         {"d": 386,   "bf": 299,   "tw": 9,    "tf": 14,   "r": 22},
    "IWF390x300x10x16":        {"d": 390,   "bf": 300,   "tw": 10,   "tf": 16,   "r": 22},
    "IWF394x398x11x18":        {"d": 394,   "bf": 398,   "tw": 11,   "tf": 18,   "r": 22},
    "IWF400x400x13x21":        {"d": 400,   "bf": 400,   "tw": 13,   "tf": 21,   "r": 22},
    "IWF406x403x16x24":        {"d": 406,   "bf": 403,   "tw": 16,   "tf": 24,   "r": 22},
    "IWF414x405x18x28":        {"d": 414,   "bf": 405,   "tw": 18,   "tf": 28,   "r": 22},
    "IWF440x300x11x18":        {"d": 440,   "bf": 300,   "tw": 11,   "tf": 18,   "r": 18},
    "IWF482x300x11x15":        {"d": 482,   "bf": 300,   "tw": 11,   "tf": 15,   "r": 22},
    "IWF488x300x11x18":        {"d": 488,   "bf": 300,   "tw": 11,   "tf": 18,   "r": 22},
    "IWF582x300x12x17":        {"d": 582,   "bf": 300,   "tw": 12,   "tf": 17,   "r": 28},
    "IWF588x300x12x20":        {"d": 588,   "bf": 300,   "tw": 12,   "tf": 20,   "r": 28},
    "IWF594x302x14x23":        {"d": 594,   "bf": 302,   "tw": 14,   "tf": 23,   "r": 28},
    "IWF692x300x13x20":        {"d": 692,   "bf": 300,   "tw": 13,   "tf": 20,   "r": 28},
    "IWF700x300x13x24":        {"d": 700,   "bf": 300,   "tw": 13,   "tf": 24,   "r": 28},
    "IWF792x300x14x22":        {"d": 792,   "bf": 300,   "tw": 14,   "tf": 22,   "r": 28},
    "IWF800x300x14x26":        {"d": 800,   "bf": 300,   "tw": 14,   "tf": 26,   "r": 28},
    "IWF890x299x15x23":        {"d": 890,   "bf": 299,   "tw": 15,   "tf": 23,   "r": 28},
    "IWF900x300x16x28":        {"d": 900,   "bf": 300,   "tw": 16,   "tf": 28,   "r": 28},
}

IWF_DB_BALOK_INDUK = {
    # ── Narrow-flange only (d > 250, bf ≤ ~200) ──
    # Wide-flange (bf >> 200) dihapus: beam pakai narrow-flange,
    # wide-flange hanya untuk kolom. Beam wide-flange merusak SCWB
    # karena Zy besar meningkatkan Mbe minor-axis.
    "IWF294x200x8x12":         {"d": 294,   "bf": 200,   "tw": 8,    "tf": 12,   "r": 13},
    "IWF298x149x5.5x8":        {"d": 298,   "bf": 149,   "tw": 5.5,  "tf": 8,    "r": 11},
    "IWF300x150x6.5x9":        {"d": 300,   "bf": 150,   "tw": 6.5,  "tf": 9,    "r": 13},
    "IWF303.4x165x6x10.2":     {"d": 303.4, "bf": 165,   "tw": 6,    "tf": 10.2, "r": 8.9},
    "IWF346x174x6x9":          {"d": 346,   "bf": 174,   "tw": 6,    "tf": 9,    "r": 13},
    "IWF350x175x7x11":         {"d": 350,   "bf": 175,   "tw": 7,    "tf": 11,   "r": 13},
    "IWF396x199x7x11":         {"d": 396,   "bf": 199,   "tw": 7,    "tf": 11,   "r": 13},
    "IWF400x200x8x13":         {"d": 400,   "bf": 200,   "tw": 8,    "tf": 13,   "r": 16},
    "IWF446x199x8x12":         {"d": 446,   "bf": 199,   "tw": 8,    "tf": 12,   "r": 16},
    "IWF450x200x9x14":         {"d": 450,   "bf": 200,   "tw": 9,    "tf": 14,   "r": 16},
    "IWF496x199x9x14":         {"d": 496,   "bf": 199,   "tw": 9,    "tf": 14,   "r": 16},
    "IWF500x200x10x16":        {"d": 500,   "bf": 200,   "tw": 10,   "tf": 16,   "r": 20},
    "IWF506x201x11x19":        {"d": 506,   "bf": 201,   "tw": 11,   "tf": 19,   "r": 20},
    "IWF596x199x10x15":        {"d": 596,   "bf": 199,   "tw": 10,   "tf": 15,   "r": 22},
    "IWF600x200x11x17":        {"d": 600,   "bf": 200,   "tw": 11,   "tf": 17,   "r": 22},
    "IWF606x201x12x20":        {"d": 606,   "bf": 201,   "tw": 12,   "tf": 20,   "r": 22},
    "IWF612x202x13x23":        {"d": 612,   "bf": 202,   "tw": 13,   "tf": 23,   "r": 22},
}

IWF_DB_BALOK_ANAK = {
    # ── Narrow-flange (profil ringan, d ≤ 350) ──
    "IWF100x50x5x7":           {"d": 100,   "bf": 50,    "tw": 5,    "tf": 7,    "r": 7},
    "IWF125x60x6x8":           {"d": 125,   "bf": 60,    "tw": 6,    "tf": 8,    "r": 8},
    "IWF148x100x6x9":          {"d": 148,   "bf": 100,   "tw": 6,    "tf": 9,    "r": 11},
    "IWF150x75x5x7":           {"d": 150,   "bf": 75,    "tw": 5,    "tf": 7,    "r": 8},
    "IWF175x90x5x8":           {"d": 175,   "bf": 90,    "tw": 5,    "tf": 8,    "r": 9},
    "IWF194x150x6x9":          {"d": 194,   "bf": 150,   "tw": 6,    "tf": 9,    "r": 11},
    "IWF198x99x4.5x7":         {"d": 198,   "bf": 99,    "tw": 4.5,  "tf": 7,    "r": 11},
    "IWF200x100x4.5x7":        {"d": 200,   "bf": 100,   "tw": 4.5,  "tf": 7,    "r": 11},
    "IWF200x100x5.5x8":        {"d": 200,   "bf": 100,   "tw": 5.5,  "tf": 8,    "r": 11},
    "IWF248x124x5x8":          {"d": 248,   "bf": 124,   "tw": 5,    "tf": 8,    "r": 12},
    "IWF250x125x6x9":          {"d": 250,   "bf": 125,   "tw": 6,    "tf": 9,    "r": 12},
    # ── Wide-flange (d ≤ 250) ──
    "IWF100x100x6x8":          {"d": 100,   "bf": 100,   "tw": 6,    "tf": 8,    "r": 8},
    "IWF125x125x6.5x9":        {"d": 125,   "bf": 125,   "tw": 6.5,  "tf": 9,    "r": 8.5},
    "IWF150x150x7x10":         {"d": 150,   "bf": 150,   "tw": 7,    "tf": 10,   "r": 11},
    "IWF175x175x7.5x11":       {"d": 175,   "bf": 175,   "tw": 7.5,  "tf": 11,   "r": 11},
    "IWF200x200x8x12":         {"d": 200,   "bf": 200,   "tw": 8,    "tf": 12,   "r": 13},
    "IWF244x175x7x11":         {"d": 244,   "bf": 175,   "tw": 7,    "tf": 11,   "r": 11},
    "IWF250x250x9x14":         {"d": 250,   "bf": 250,   "tw": 9,    "tf": 14,   "r": 16},
}

# Default group names (overridden from Result.json group_names if available)
_GRP_KOLOM = "Kolom"
_GRP_BALOK_INDUK = "Balok Induk"
_GRP_BALOK_ANAK = "Balok Anak"
VALID_GROUPS = {_GRP_KOLOM, _GRP_BALOK_INDUK, _GRP_BALOK_ANAK}

# Mapping group name → database (updated by _load_group_names)
IWF_GROUP_DB = {
    _GRP_KOLOM:       IWF_DB_KOLOM,
    _GRP_BALOK_INDUK: IWF_DB_BALOK_INDUK,
    _GRP_BALOK_ANAK:  IWF_DB_BALOK_ANAK,
}

# Combined database (for backward compat / general lookups)
IWF_DATABASE = {}
IWF_DATABASE.update(IWF_DB_KOLOM)
IWF_DATABASE.update(IWF_DB_BALOK_INDUK)
IWF_DATABASE.update(IWF_DB_BALOK_ANAK)

# Material default (BJ 41)
Fy_DEFAULT = 250.0     # MPa
Fu_DEFAULT = 400.0     # MPa
E_STEEL    = 200000.0  # MPa
G_STEEL    = 77200.0   # MPa
RHO_STEEL  = 7850.0    # kg/m³

# DCR target range
DCR_MIN = 0.5
DCR_MAX = 0.95


# ═══════════════════════════════════════════════════════════════════
# SECTION PROPERTIES CALCULATOR
# ═══════════════════════════════════════════════════════════════════

def calc_section_props(d, bf, tw, tf):
    """
    Compute IWF section properties from dimensions.
    Uses Euler finite-width correction for J (matches SAP2000).
    Returns dict with all properties in mm units.
    """
    h_web = d - 2.0 * tf
    Area = 2.0 * bf * tf + h_web * tw

    Iz = (bf * d**3 - (bf - tw) * h_web**3) / 12.0
    Iy = (2.0 * tf * bf**3 + h_web * tw**3) / 12.0

    Sz = Iz / (d / 2.0) if d > 0 else 0.0
    Sy = Iy / (bf / 2.0) if bf > 0 else 0.0

    Zz = bf * tf * (d - tf) + tw * h_web**2 / 4.0
    Zy = (bf**2 * tf) / 2.0 + (h_web * tw**2) / 4.0

    rz = math.sqrt(Iz / Area) if Area > 0 else 0.0
    ry = math.sqrt(Iy / Area) if Area > 0 else 0.0

    # Torsional constant — Euler finite-width rectangle correction
    corr_f = 1.0 - 0.63 * (tf / bf) + 0.052 * (tf / bf)**5 if bf > 0 else 1.0
    corr_w = 1.0 - 0.63 * (tw / h_web) + 0.052 * (tw / h_web)**5 if h_web > 0 else 1.0
    J = (2.0 * bf * tf**3 * corr_f + h_web * tw**3 * corr_w) / 3.0

    # Warping constant
    h0 = d - tf
    Cw = Iy * (h0**2) / 4.0

    # Shear areas (matches script.py formulas)
    Avz = d * tw
    Avy = 2.0 * bf * tf * (5.0 / 6.0)

    Weight = Area * RHO_STEEL * 1e-6  # kg/m

    return {
        "Area": Area, "d": d, "bf": bf, "tw": tw, "tf": tf,
        "Iz": Iz, "Iy": Iy,
        "Sz": Sz, "Sy": Sy,
        "Zz": Zz, "Zy": Zy,
        "rz": rz, "ry": ry,
        "J": J, "Cw": Cw,
        "Avz": Avz, "Avy": Avy,
        "h_web": h_web, "Weight": Weight,
    }


def build_sde_section(name, dims, Fy=Fy_DEFAULT, Fu=Fu_DEFAULT):
    """Build SDE.SectionProperties from dimension dict."""
    sp = calc_section_props(dims["d"], dims["bf"], dims["tw"], dims["tf"])
    return SDE.SectionProperties(
        name=name,
        d=sp["d"], bf=sp["bf"], tf=sp["tf"], tw=sp["tw"],
        A=sp["Area"],
        Ix=sp["Iz"], Iy=sp["Iy"],
        Sx=sp["Sz"], Sy=sp["Sy"],
        Zx=sp["Zz"], Zy=sp["Zy"],
        rx=sp["rz"], ry=sp["ry"],
        J=sp["J"], Cw=sp["Cw"],
        Fy=Fy, Fu=Fu, E=E_STEEL, G=G_STEEL,
    )


# Pre-compute section properties and sort by weight — PER GROUP
_DB_SEC = {}
for _name, _dims in IWF_DATABASE.items():
    _DB_SEC[_name] = build_sde_section(_name, _dims)

def _sort_by_weight(db):
    return sorted(db.keys(),
                  key=lambda n: calc_section_props(
                      db[n]["d"], db[n]["bf"],
                      db[n]["tw"], db[n]["tf"])["Weight"])

_DB_SORTED_PER_GROUP = {
    _GRP_KOLOM:       _sort_by_weight(IWF_DB_KOLOM),
    _GRP_BALOK_INDUK: _sort_by_weight(IWF_DB_BALOK_INDUK),
    _GRP_BALOK_ANAK:  _sort_by_weight(IWF_DB_BALOK_ANAK),
}

# Fallback: all sections sorted
_DB_SORTED = _sort_by_weight(IWF_DATABASE)


# ═══════════════════════════════════════════════════════════════════
# GROUP MAPPING (defaults defined above, near IWF_GROUP_DB)
# ═══════════════════════════════════════════════════════════════════


def _load_group_names(data):
    """Read group_names from Result.json model_data and update module-level names.
    Also rebuilds IWF_GROUP_DB and _DB_SORTED_PER_GROUP so per-group lookups
    use the updated keys."""
    global _GRP_KOLOM, _GRP_BALOK_INDUK, _GRP_BALOK_ANAK, VALID_GROUPS
    global IWF_GROUP_DB, _DB_SORTED_PER_GROUP
    model_data = data.get("model_data", data)  # Accept both Result.json and raw model_data
    gn = model_data.get("group_names", {})
    _GRP_KOLOM       = gn.get("kolom",       "Kolom")
    _GRP_BALOK_INDUK = gn.get("balok_induk", "Balok Induk")
    _GRP_BALOK_ANAK  = gn.get("balok_anak",  "Balok Anak")
    VALID_GROUPS = {_GRP_KOLOM, _GRP_BALOK_INDUK, _GRP_BALOK_ANAK}

    # Rebuild per-group dicts with updated keys
    IWF_GROUP_DB = {
        _GRP_KOLOM:       IWF_DB_KOLOM,
        _GRP_BALOK_INDUK: IWF_DB_BALOK_INDUK,
        _GRP_BALOK_ANAK:  IWF_DB_BALOK_ANAK,
    }
    _DB_SORTED_PER_GROUP = {
        _GRP_KOLOM:       _sort_by_weight(IWF_DB_KOLOM),
        _GRP_BALOK_INDUK: _sort_by_weight(IWF_DB_BALOK_INDUK),
        _GRP_BALOK_ANAK:  _sort_by_weight(IWF_DB_BALOK_ANAK),
    }


def classify_group(elem):
    """Determine autoselect group from element data."""
    # Group field langsung dari Create/Design Result
    group_raw = elem.get("group", "")
    if group_raw in VALID_GROUPS:
        return group_raw

    # Fallback: infer from design_type
    design_type = elem.get("design_type", "Beam")
    if design_type == "Column":
        return _GRP_KOLOM

    return _GRP_BALOK_INDUK


# ═══════════════════════════════════════════════════════════════════
# ELEMENT DATA EXTRACTION
# ═══════════════════════════════════════════════════════════════════

def extract_element_data(elem):
    """
    Extract forces and parameters from a Design Result element.
    Returns dict with forces (N, N·mm), K factors, lengths, material.
    """
    pmm = elem.get("pmm_detail", {})
    shear_det = elem.get("shear_detail", {})

    # Forces in N and N·mm
    Pr_N = abs(pmm.get("Pr_kN", 0.0)) * 1000.0
    MrMaj_Nmm = abs(pmm.get("MrMajor_kNm", 0.0)) * 1e6
    MrMin_Nmm = abs(pmm.get("MrMinor_kNm", 0.0)) * 1e6
    Vr_N = abs(shear_det.get("VrMajDsgn_kN", 0.0)) * 1000.0
    is_compression = pmm.get("Pr_kN", 0.0) <= 0.0

    # K factors (from augmented field or default)
    k_data = elem.get("K_factors", {})
    Kx = k_data.get("Kx", 1.0)
    Ky = k_data.get("Ky", 1.0)
    Kz = k_data.get("Kz", 1.0)

    # Lengths (from augmented field or estimate from station_details)
    l_data = elem.get("lengths", {})
    if l_data:
        Lx = l_data.get("Lx_mm", 3000.0)
        Ly = l_data.get("Ly_mm", 3000.0)
        Lb = l_data.get("Lb_mm", 3000.0)
        Lcz = l_data.get("Lcz_mm", 3000.0)
    else:
        # Fallback: estimate from max station location
        stations = elem.get("station_details", [])
        L_est = 0.0
        for st in stations:
            loc = st.get("location_mm", 0.0)
            if loc > L_est:
                L_est = loc
        L_est = L_est if L_est > 0 else 3000.0
        Lx = Ly = Lb = Lcz = L_est

    # Material (from augmented field or default)
    mat = elem.get("material", {})
    Fy = mat.get("Fy_MPa", Fy_DEFAULT)
    Fu = mat.get("Fu_MPa", Fu_DEFAULT)
    E = mat.get("E_MPa", E_STEEL)

    # Cb from original design
    cap = elem.get("capacity", {})
    Cb = cap.get("Cb", 1.0)

    return {
        "Pr_N": Pr_N, "MrMaj_Nmm": MrMaj_Nmm,
        "MrMin_Nmm": MrMin_Nmm, "Vr_N": Vr_N,
        "is_compression": is_compression,
        "Kx": Kx, "Ky": Ky, "Kz": Kz,
        "Lx": Lx, "Ly": Ly, "Lb": Lb, "Lcz": Lcz,
        "Cb": Cb, "Fy": Fy, "Fu": Fu, "E": E,
    }


# ═══════════════════════════════════════════════════════════════════
# DCR COMPUTATION (uses Steel Design Engine classes)
# ═══════════════════════════════════════════════════════════════════

def compute_dcr_sde(sec, elem_data, design_type):
    """
    Compute DCR for one element using SDE (rigorous AISC 360-22).

    Args:
        sec: SDE.SectionProperties for the trial section
        elem_data: dict from extract_element_data()
        design_type: "Column" or "Beam"

    Returns:
        (dcr, dcr_type) tuple
    """
    # Classification
    cls = SDE.SectionClassification(sec)
    axial_class = cls.classify_axial()
    flex_class = cls.classify_flexure()

    # Capacities
    PcComp = SDE.CompressionCapacity.design(
        sec,
        Kx=elem_data["Kx"], Ky=elem_data["Ky"],
        Lx=elem_data["Lx"], Ly=elem_data["Ly"],
        axial_class=axial_class,
        Kz=elem_data["Kz"], Lcz=elem_data["Lcz"]
    )
    PcTen = SDE.TensionCapacity.design(sec)
    McMaj = SDE.FlexureCapacity.design_major(sec, Lb=elem_data["Lb"],
                                              Cb=elem_data["Cb"],
                                              flex_class=flex_class)
    McMin = SDE.FlexureCapacity.design_minor(sec, flex_class=flex_class)
    VnMaj = SDE.ShearCapacity.design_major(sec)

    # Build DesignCapacity
    cap = SDE.DesignCapacity(
        PcComp=PcComp, PcTension=PcTen,
        McMajor=McMaj, McMinor=McMin,
        PhiVnMajor=VnMaj, PhiVnMinor=0.0,
        section_class_axial=axial_class["overall"],
        section_class_flexure_flange=flex_class["flange"],
        section_class_flexure_web=flex_class["web"],
        Cb=elem_data["Cb"],
    )

    # Build DesignForces
    P_sign = -elem_data["Pr_N"] if elem_data["is_compression"] else elem_data["Pr_N"]
    forces = SDE.DesignForces(
        P=P_sign,
        Mz=elem_data["MrMaj_Nmm"],
        My=elem_data["MrMin_Nmm"],
        Fz=elem_data["Vr_N"],
    )

    # PMM check
    pmm = SDE.PMMCheck.check(forces, cap)
    dcr_pmm = pmm.TotalRatio

    # Shear check
    dcr_shear = elem_data["Vr_N"] / max(VnMaj, 1.0)

    # Always use PMM + Shear for ALL element types.
    # Steel Design Engine checks H1-1 (PMM) for both columns and beams,
    # so the optimizer must use the same criterion to match verified DCR.
    # PMM naturally reduces to pure flexure when axial is negligible.
    dcr = max(dcr_pmm, dcr_shear)
    dcr_type = "PMM+V"

    return dcr, dcr_type


# ═══════════════════════════════════════════════════════════════════
# SMF-AWARE SECTION SELECTION (fixes convergence oscillation)
# ═══════════════════════════════════════════════════════════════════
#
# When SCWB or SMF fails, naive "next-heavier-by-weight" causes oscillation
# because weight order does not correlate with ry, bf/2tf, or Zx.
#
# Fix: pre-filter candidates by SMF geometric requirements (ry, flange HD,
# web HD), then select lightest that satisfies DCR.
# ───────────────────────────────────────────────────────────────────

_Ry_SMF = 1.1  # Expected yield stress ratio (AISC 341 Table A3.2)


def _compute_smf_thresholds(g_name, elements, Fy=Fy_DEFAULT):
    """Compute minimum section requirements for SMF compliance.

    For columns: ry_min from L/r <= 60 AND Lb/ry limit
    For beams:   ry_min from Lb/ry <= 0.086*E/(Ry*Fy)
    For both:    bf/2tf and h/tw HD limits (AISC 341 Table D1.1b)
    """
    Ry = _Ry_SMF
    E = E_STEEL

    max_L = 0.0    # max member length (for L/r check on columns)
    max_Lb = 0.0   # max lateral-torsional unbraced length

    for elem in elements:
        l_data = elem.get("lengths", {})
        Lx = l_data.get("Lx_mm", 0)
        Ly = l_data.get("Ly_mm", 0)
        Lb = l_data.get("Lb_mm", 0)
        max_L = max(max_L, Lx, Ly)
        max_Lb = max(max_Lb, Lb)

    # Fallback from station details
    if max_L == 0 or max_Lb == 0:
        for elem in elements:
            stations = elem.get("station_details", [])
            for st in stations:
                loc = st.get("location_mm", 0)
                if loc > 0:
                    if max_L == 0:
                        max_L = max(max_L, loc)
                    if max_Lb == 0:
                        max_Lb = max(max_Lb, loc)

    Lb_ry_limit = 0.086 * E / (Ry * Fy)   # ~62.5 for BJ41
    Lr_limit = 60.0                         # L/r limit for SMF columns

    if g_name == _GRP_KOLOM:
        ry_min_lr = max_L / Lr_limit if max_L > 0 else 0.0
        ry_min_lb = max_Lb / Lb_ry_limit if max_Lb > 0 else 0.0
        ry_min = max(ry_min_lr, ry_min_lb)
    else:
        ry_min = max_Lb / Lb_ry_limit if max_Lb > 0 else 0.0

    flange_limit = 0.32 * math.sqrt(E / (Ry * Fy))   # bf/(2*tf) HD limit
    web_limit = 2.57 * math.sqrt(E / (Ry * Fy))       # h/tw HD limit (beam)

    return {
        "ry_min": ry_min,
        "max_L": max_L,
        "max_Lb": max_Lb,
        "flange_hd_limit": flange_limit,
        "web_hd_limit": web_limit,
    }


def _filter_smf_sections(g_name, thresholds, exclude=None,
                          check_flange=True, check_web=True):
    """Filter group DB to sections passing SMF geometric checks.

    Always checks ry >= ry_min.  Optionally checks flange/web HD.
    Returns list of section names sorted by weight (lightest first).
    """
    exclude = exclude or set()
    sorted_names = _DB_SORTED_PER_GROUP.get(g_name, _DB_SORTED)

    ry_min = thresholds["ry_min"]
    flange_limit = thresholds["flange_hd_limit"]
    web_limit = thresholds["web_hd_limit"]

    filtered = []
    for name in sorted_names:
        if name in exclude:
            continue
        sec = _DB_SEC[name]
        # Must have ry >= ry_min (fixes L/r and Lb/ry)
        if sec.ry < ry_min:
            continue
        # Flange HD: bf/(2*tf)
        if check_flange and sec.tf > 0:
            if sec.bf / (2.0 * sec.tf) > flange_limit:
                continue
        # Web HD: h_web/tw
        if check_web and sec.tw > 0:
            h_web = sec.d - 2.0 * sec.tf
            if h_web / sec.tw > web_limit:
                continue
        filtered.append(name)
    return filtered


def _compute_group_max_dcr(sec_name, g_elems, g_name):
    """Compute max DCR across all group elements for a trial section."""
    sec = _DB_SEC[sec_name]
    max_dcr = 0.0
    dt = "Column" if g_name == _GRP_KOLOM else None

    for elem in g_elems:
        ed = extract_element_data(elem)
        design_type = dt or elem.get("design_type", "Beam")
        trial_sec = SDE.SectionProperties(
            name=sec.name, d=sec.d, bf=sec.bf, tf=sec.tf, tw=sec.tw,
            A=sec.A, Ix=sec.Ix, Iy=sec.Iy, Sx=sec.Sx, Sy=sec.Sy,
            Zx=sec.Zx, Zy=sec.Zy, rx=sec.rx, ry=sec.ry,
            J=sec.J, Cw=sec.Cw,
            Fy=ed["Fy"], Fu=ed["Fu"], E=ed["E"],
        )
        dcr_val, _ = compute_dcr_sde(trial_sec, ed, design_type)
        max_dcr = max(max_dcr, dcr_val)
    return max_dcr


def _smf_aware_select(g_name, design_new_data, group_results,
                       tried_sections=None, scwb_result=None):
    """Select a section that satisfies SMF/SCWB requirements AND DCR target.

    Tiered approach:
      Tier 1: Full SMF filter (ry + flange HD + web HD) + DCR
      Tier 2: ry-only filter (fixes L/r and Lb/ry, relaxes HD) + DCR
    If Tier 1 has no candidates, Tier 2 result is marked as best-effort.

    Returns:
        (section_name, reason_str, is_best_effort)
        is_best_effort=True means no full-SMF section exists; caller should
        stop re-upsizing for HD issues in subsequent iterations.
    """
    tried = tried_sections or set()

    all_elems = design_new_data.get("elements", [])
    g_elems = [e for e in all_elems if classify_group(e) == g_name]
    if not g_elems:
        return None, "no elements", True

    Fy = g_elems[0].get("material", {}).get("Fy_MPa", Fy_DEFAULT)
    thresholds = _compute_smf_thresholds(g_name, g_elems, Fy)

    print("    SMF thresholds: ry >= {:.1f}mm, bf/2tf <= {:.2f}, "
          "h/tw <= {:.1f}".format(
              thresholds["ry_min"], thresholds["flange_hd_limit"],
              thresholds["web_hd_limit"]))

    # Build candidate lists
    tier1 = _filter_smf_sections(g_name, thresholds, exclude=tried,
                                  check_flange=True, check_web=True)
    tier2 = _filter_smf_sections(g_name, thresholds, exclude=tried,
                                  check_flange=False, check_web=False)

    # SCWB Z filter for Kolom: if SCWB failed, estimate min Z needed.
    # The SDE checks major & minor axes separately.  When the minor
    # axis governs (common for narrow-flange columns), the filter
    # must use Zy instead of Zx.
    scwb_Z_min = 0.0
    scwb_Z_attr = "Zx"  # attribute name on _DB_SEC[n]
    if (scwb_result and not scwb_result.get("passed", True)
            and g_name == _GRP_KOLOM):
        # Prefer SDE per-plane ratio (authoritative); fall back to
        # joint-based ratio from check_scwb_all_joints.
        sde_ratio = scwb_result.get("sde_min_ratio")
        own_ratio = scwb_result.get("min_ratio")
        gov_plane = scwb_result.get("sde_governing_plane", "major")
        min_ratio = sde_ratio if sde_ratio is not None else own_ratio

        col_sec_name = scwb_result.get("column_section", "")
        if col_sec_name in _DB_SEC and min_ratio is not None and 0 < min_ratio < 1.0:
            if gov_plane == "minor":
                cur_Z = _DB_SEC[col_sec_name].Zy
                scwb_Z_attr = "Zy"
            else:
                cur_Z = _DB_SEC[col_sec_name].Zx
                scwb_Z_attr = "Zx"
            scwb_Z_min = cur_Z / min_ratio * 1.1   # 10% safety margin
            print("    SCWB col {} filter: {} >= {:.0f} mm3 (cur={:.0f}, "
                  "ratio={:.4f}, plane={})".format(
                      gov_plane, scwb_Z_attr, scwb_Z_min, cur_Z,
                      min_ratio, gov_plane))

    # SCWB-beam coordination for Balok Induk:
    # Limit beam Zx to preserve SCWB compliance.
    # Mbe is proportional to beam Zx → if beam grows, SCWB ratio drops.
    # max_beam_Zx ≈ cur_beam_Zx * scwb_ratio keeps ratio ≥ 1.0
    max_beam_Zx = float('inf')
    if (g_name == _GRP_BALOK_INDUK and scwb_result
            and scwb_result.get("min_ratio") is not None):
        scwb_min_ratio = scwb_result["min_ratio"]
        beam_sec_name = scwb_result.get("beam_section", "")
        if beam_sec_name in _DB_SEC and scwb_min_ratio > 0:
            cur_beam_Zx = _DB_SEC[beam_sec_name].Zx
            max_beam_Zx = cur_beam_Zx * scwb_min_ratio
            print("    SCWB-beam limit: Zx <= {:.0f} mm3 "
                  "(cur beam Zx={:.0f}, SCWB ratio={:.4f})".format(
                      max_beam_Zx, cur_beam_Zx, scwb_min_ratio))

    tiers = [("full-SMF", tier1, False), ("ry-only", tier2, True)]

    for tier_label, candidates, is_best_effort in tiers:
        if not candidates:
            print("    {} filter: 0 candidates".format(tier_label))
            continue

        # Apply SCWB-beam Zx limit (Balok Induk: don't grow beyond SCWB capacity)
        if max_beam_Zx < float('inf'):
            zx_ok = [n for n in candidates
                     if _DB_SEC[n].Zx <= max_beam_Zx]
            if zx_ok:
                print("    {} + SCWB beam limit: {} >> {} candidates".format(
                    tier_label, len(candidates), len(zx_ok)))
                candidates = zx_ok
            else:
                print("    {} + SCWB beam limit: 0 candidates "
                      "(all beam Zx > {:.0f} -- conflict)".format(
                          tier_label, max_beam_Zx))
                continue  # Try next tier

        # Apply SCWB col Z filter (Kolom: must be big enough for SCWB)
        # Uses Zx for major-axis SCWB, Zy for minor-axis SCWB.
        if scwb_Z_min > 0:
            scwb_filtered = [n for n in candidates
                             if getattr(_DB_SEC[n], scwb_Z_attr)
                             >= scwb_Z_min]
            if scwb_filtered:
                print("    {} + SCWB col ({}): {} >> {} candidates".format(
                    tier_label, scwb_Z_attr,
                    len(candidates), len(scwb_filtered)))
                candidates = scwb_filtered
            else:
                print("    {} + SCWB col ({}): no candidates with "
                      "{} >= {:.0f} (using all {} candidates)".format(
                          tier_label, scwb_Z_attr, scwb_Z_attr,
                          scwb_Z_min, len(candidates)))

        print("    {} filter: {} candidates".format(tier_label,
                                                     len(candidates)))

        # Iterate lightest to heaviest; return first with DCR <= target
        best_name = None
        best_dcr = float('inf')

        for sec_name in candidates:
            dcr = _compute_group_max_dcr(sec_name, g_elems, g_name)
            if dcr <= DCR_MAX:
                return sec_name, "{} DCR={:.4f}".format(
                    tier_label, dcr), is_best_effort
            if dcr < best_dcr:
                best_dcr = dcr
                best_name = sec_name

        # No candidate satisfies DCR -- return lowest-DCR (most capacity)
        if best_name:
            return best_name, "{} DCR={:.4f} (pending)".format(
                tier_label, best_dcr), is_best_effort

    return None, "no SMF-compliant sections available", True


# ═══════════════════════════════════════════════════════════════════
# GROUP OPTIMIZATION (numpy vectorized)
# ═══════════════════════════════════════════════════════════════════

def optimize_group(_group_name, elements, design_type_override=None,
                   exclude_sections=None):
    """
    Find the lightest IWF section where max DCR across all group
    elements is <= DCR_MAX.

    Args:
        group_name: "Kolom", "Balok Induk", or "Balok Anak"
        elements: list of element dicts from Design Result
        design_type_override: force design_type for all elements
        exclude_sections: set of section names to skip (anti-oscillation)

    Returns:
        dict with optimization result
    """
    if not elements:
        return {"section": None, "max_dcr": 0.0, "status": "empty"}

    # Extract element data
    elem_data_list = []
    for elem in elements:
        ed = extract_element_data(elem)
        dt = design_type_override or elem.get("design_type", "Beam")
        ed["design_type"] = dt
        elem_data_list.append(ed)

    # Get original section name
    orig_sections = set()
    for elem in elements:
        sec_str = elem.get("design_section", "")
        if ":" in sec_str:
            sec_str = sec_str.split(":")[-1].strip()
        orig_sections.add(sec_str)
    original_section = ", ".join(sorted(orig_sections))

    n_elems = len(elem_data_list)
    best_section = None
    best_dcr = float("inf")
    best_dcr_array = None

    # Select database for this group (fallback to all sections)
    sorted_list = _DB_SORTED_PER_GROUP.get(_group_name, _DB_SORTED)
    _exclude = exclude_sections or set()

    # Iterate sections from lightest to heaviest
    for sec_name in sorted_list:
        if sec_name in _exclude:
            continue
        sec = _DB_SEC[sec_name]

        # Compute DCR for each element in group
        dcr_array = np.zeros(n_elems)
        for i, ed in enumerate(elem_data_list):
            # Update material on trial section
            trial_sec = SDE.SectionProperties(
                name=sec.name, d=sec.d, bf=sec.bf, tf=sec.tf, tw=sec.tw,
                A=sec.A, Ix=sec.Ix, Iy=sec.Iy, Sx=sec.Sx, Sy=sec.Sy,
                Zx=sec.Zx, Zy=sec.Zy, rx=sec.rx, ry=sec.ry,
                J=sec.J, Cw=sec.Cw,
                Fy=ed["Fy"], Fu=ed["Fu"], E=ed["E"],
            )
            dcr_val, _ = compute_dcr_sde(trial_sec, ed, ed["design_type"])
            dcr_array[i] = dcr_val

        max_dcr = float(np.max(dcr_array))

        if max_dcr <= DCR_MAX:
            best_section = sec_name
            best_dcr = max_dcr
            best_dcr_array = dcr_array
            break

    # If no section found with dcr <= DCR_MAX, take the largest in this group
    if best_section is None:
        non_excluded = [s for s in sorted_list if s not in _exclude]
        if not non_excluded:
            return {
                "section": None, "max_dcr": 0.0, "status": "empty",
                "original_section": original_section,
                "note": "All candidate sections excluded",
            }
        last_sec_name = non_excluded[-1]
        sec = _DB_SEC[last_sec_name]
        dcr_array = np.zeros(n_elems)
        for i, ed in enumerate(elem_data_list):
            trial_sec = SDE.SectionProperties(
                name=sec.name, d=sec.d, bf=sec.bf, tf=sec.tf, tw=sec.tw,
                A=sec.A, Ix=sec.Ix, Iy=sec.Iy, Sx=sec.Sx, Sy=sec.Sy,
                Zx=sec.Zx, Zy=sec.Zy, rx=sec.rx, ry=sec.ry,
                J=sec.J, Cw=sec.Cw,
                Fy=ed["Fy"], Fu=ed["Fu"], E=ed["E"],
            )
            dcr_val, _ = compute_dcr_sde(trial_sec, ed, ed["design_type"])
            dcr_array[i] = dcr_val

        best_dcr = float(np.max(dcr_array))
        best_dcr_array = dcr_array

        if best_dcr <= DCR_MAX:
            best_section = last_sec_name
        else:
            return {
                "section": last_sec_name,
                "max_dcr": round(best_dcr, 6),
                "status": "unresolved",
                "original_section": original_section,
                "note": "No section in {} database satisfies DCR <= {:.2f} (best: {:.4f})".format(
                    _group_name, DCR_MAX, best_dcr),
                "element_dcrs": [round(float(d), 6) for d in dcr_array],
            }

    # Build per-element detail
    elem_details = []
    for i, elem in enumerate(elements):
        elem_details.append({
            "element_id": elem.get("element_id", 0),
            "label_name": elem.get("label_name",
                                    elem.get("frame_label", "?")),
            "design_type": elem.get("design_type", "?"),
            "dcr_initial": round(float(best_dcr_array[i]), 6),
        })

    in_target = DCR_MIN <= best_dcr <= DCR_MAX

    return {
        "section": best_section,
        "max_dcr": round(best_dcr, 6),
        "status": "ok",
        "original_section": original_section,
        "in_target_range": in_target,
        "element_count": n_elems,
        "elements": elem_details,
    }


# ═══════════════════════════════════════════════════════════════════
# PHASE 1: QUICK OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════

def phase1_optimize(design_data):
    """
    Phase 1: Find optimal section per group using original forces.

    Returns:
        dict {group_name: optimization_result}
    """
    all_elements = design_data.get("elements", [])

    # Group elements
    groups = {}  # {group_name: [elem, ...]}
    for elem in all_elements:
        g = classify_group(elem)
        groups.setdefault(g, []).append(elem)

    results = {}
    for g_name, g_elems in groups.items():
        dt_override = "Column" if g_name == _GRP_KOLOM else None
        results[g_name] = optimize_group(g_name, g_elems, dt_override)
        print("  [Phase 1] {} -> {} (max DCR = {:.4f}, status = {})".format(
            g_name,
            results[g_name].get("section", "?"),
            results[g_name].get("max_dcr", 0),
            results[g_name].get("status", "?")))

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 2: RE-ANALYSIS PIPELINE
# ═══════════════════════════════════════════════════════════════════

def update_model_data(result_json, group_results):
    """
    Clone model_data from Result.json and update section properties
    for each element based on group optimization results.

    Returns:
        updated model_data dict (deep copy, original untouched)
    """
    model_data = copy.deepcopy(result_json.get("model_data", {}))

    # Build group→section mapping from group_results
    # We need to know which elements belong to which group
    # Use the same classification logic
    all_elements = model_data.get("model_elements", [])

    # Build element_id → group mapping from design_data (already classified in Phase 1)
    # Instead, re-classify from model_data element type/group
    for elem in all_elements:
        elem_type = elem.get("type", "")
        group_field = elem.get("group", "")

        # Determine autoselect group (langsung dari field group)
        if group_field in VALID_GROUPS:
            as_group = group_field
        elif elem_type == "Column":
            as_group = _GRP_KOLOM
        else:
            as_group = _GRP_BALOK_INDUK

        # Get recommended section for this group
        g_result = group_results.get(as_group, {})
        new_sec_name = g_result.get("section")
        if not new_sec_name or g_result.get("status") == "empty":
            continue

        # Get section dimensions
        if new_sec_name not in IWF_DATABASE:
            continue
        dims = IWF_DATABASE[new_sec_name]
        sp = calc_section_props(dims["d"], dims["bf"], dims["tw"], dims["tf"])

        # Update element "section" dict (key used by Analysis.py & Steel Design Engine)
        sec_dict = elem.get("section", {})
        sec_dict["section_name"] = new_sec_name
        sec_dict["d_mm"] = sp["d"]
        sec_dict["b_mm"] = sp["bf"]
        sec_dict["tf_mm"] = sp["tf"]
        sec_dict["tw_mm"] = sp["tw"]
        sec_dict["Area_mm2"] = sp["Area"]
        sec_dict["Iz_mm4"] = sp["Iz"]
        sec_dict["Iy_mm4"] = sp["Iy"]
        sec_dict["Sz_mm3"] = sp["Sz"]
        sec_dict["Sy_mm3"] = sp["Sy"]
        sec_dict["Zz_mm3"] = sp["Zz"]
        sec_dict["Zy_mm3"] = sp["Zy"]
        sec_dict["J_mm4"] = sp["J"]
        sec_dict["Cw_mm6"] = sp["Cw"]
        sec_dict["rz_mm"] = sp["rz"]
        sec_dict["ry_mm"] = sp["ry"]
        sec_dict["Avz_mm2"] = sp["Avz"]
        sec_dict["Avy_mm2"] = sp["Avy"]
        elem["section"] = sec_dict

        # Update "family" field (used by Steel Design Engine for design_section name)
        old_family = elem.get("family", "")
        family_prefix = old_family.split(":")[0].strip() if ":" in old_family else "M_W Shapes"
        elem["family"] = "{} : {}".format(family_prefix, new_sec_name)

    return model_data


def merge_model_and_analysis(model_data, analysis_data, height_mm=4000, z_levels=None):
    """
    Merge model_data + analysis_data into Result.json format.
    Simplified version of Create.pushbutton/script.py merge_to_result_json().
    z_levels: sorted list of floor elevations [0, h1, h1+h2, ...] for non-uniform heights.
    """
    # Build coord→node_id mapping from SelfWeight
    coord_to_node_id = {}
    sw_data = analysis_data.get("SelfWeight", {})
    if "nodes" in sw_data:
        for nid, nval in sw_data["nodes"].items():
            coords = nval.get("coords", [])
            if isinstance(coords, dict):
                coords = [coords.get("X", 0), coords.get("Y", 0), coords.get("Z", 0)]
            if len(coords) == 3:
                key = (int(round(coords[0])), int(round(coords[1])), int(round(coords[2])))
                coord_to_node_id[key] = nid

    # Re-label elements
    col_counter = 0
    beam_counter = 0
    for elem in model_data.get("model_elements", []):
        elem_type = elem.get("type", "")
        if elem_type == "Column":
            col_counter += 1
            elem["frame_label"] = "C{}".format(col_counter)
        elif elem_type == "Beam":
            beam_counter += 1
            elem["frame_label"] = "B{}".format(beam_counter)
        else:
            elem["frame_label"] = "E{}".format(elem.get("id", "?"))

        topo = elem.get("topology", {})
        start = topo.get("start_node", [0, 0, 0])
        end = topo.get("end_node", [0, 0, 0])

        start_key = (int(round(start[0])), int(round(start[1])), int(round(start[2])))
        end_key = (int(round(end[0])), int(round(end[1])), int(round(end[2])))

        topo["start_node_id"] = coord_to_node_id.get(start_key, "-")
        topo["end_node_id"] = coord_to_node_id.get(end_key, "-")

        def _node_desc(z_val):
            z_rounded = int(round(z_val))
            if z_rounded == 0:
                return "Support Node (Fixed)"
            else:
                # Use z_levels for non-uniform story heights
                if z_levels and len(z_levels) >= 2:
                    lantai = 0
                    for li in range(1, len(z_levels)):
                        if abs(z_rounded - z_levels[li]) < 1:
                            lantai = li
                            break
                    if lantai == 0:
                        lantai = int(round(z_rounded / height_mm))
                else:
                    lantai = int(round(z_rounded / height_mm))
                return "Joint Node (Lantai {})".format(lantai)

        topo["start_node_desc"] = _node_desc(start[2])
        topo["end_node_desc"] = _node_desc(end[2])

    # Node classification
    support_nodes = {}
    floor_joint_nodes = {}
    subdivision_nodes = {}

    if "nodes" in sw_data:
        for nid, nval in sw_data["nodes"].items():
            coords = nval.get("coords", [0, 0, 0])
            if isinstance(coords, dict):
                coords = [coords.get("X", 0), coords.get("Y", 0), coords.get("Z", 0)]
            z_val = coords[2] if len(coords) == 3 else 0
            has_reaction = nval.get("reaction") is not None
            z_rounded = int(round(z_val))
            # Use z_levels for non-uniform story heights
            if z_levels and len(z_levels) >= 2:
                is_floor_level = any(abs(z_rounded - int(round(z))) < 1 for z in z_levels)
            else:
                is_floor_level = (z_rounded % int(height_mm) == 0) if height_mm > 0 else False

            if has_reaction and z_rounded == 0:
                category = "support"
            elif is_floor_level:
                category = "floor_joint"
            else:
                category = "subdivision"

            node_info = {
                "coords": {"X": coords[0], "Y": coords[1], "Z": coords[2]},
                "category": category,
            }
            if category == "support":
                support_nodes[nid] = node_info
            elif category == "floor_joint":
                floor_joint_nodes[nid] = node_info
            else:
                subdivision_nodes[nid] = node_info

    node_classification = {
        "summary": {
            "total_nodes": len(sw_data.get("nodes", {})),
            "support_count": len(support_nodes),
            "floor_joint_count": len(floor_joint_nodes),
            "subdivision_count": len(subdivision_nodes),
        },
        "support_nodes": support_nodes,
        "floor_joint_nodes": floor_joint_nodes,
        "subdivision_nodes": subdivision_nodes,
    }

    # Relabel coords/disp in analysis data
    load_case_keys = [
        k for k in analysis_data.keys()
        if not k.startswith("_") and isinstance(analysis_data[k], dict)
        and "nodes" in analysis_data[k]
    ]
    for case_key in load_case_keys:
        case_data = analysis_data.get(case_key, {})
        if "nodes" not in case_data:
            continue
        for nid, nval in case_data["nodes"].items():
            old_coords = nval.get("coords", [0, 0, 0])
            if isinstance(old_coords, list) and len(old_coords) == 3:
                nval["coords"] = {"X": old_coords[0], "Y": old_coords[1],
                                  "Z": old_coords[2]}
            old_disp = nval.get("disp", [0, 0, 0, 0, 0, 0])
            if isinstance(old_disp, list) and len(old_disp) == 6:
                nval["disp"] = {"ux": old_disp[0], "uy": old_disp[1],
                                "uz": old_disp[2], "rx": old_disp[3],
                                "ry": old_disp[4], "rz": old_disp[5]}

    return {
        "model_data": model_data,
        "node_classification": node_classification,
        "analysis_results": analysis_data,
    }


def phase2_reanalysis(result_json_path, group_results, output_dir):
    """
    Phase 2: Re-analysis pipeline.
    1. Load original Result.json
    2. Clone model_data, update sections per group
    3. Save Model_data_new.json
    4. Run Analysis.py → Analysis_New.json
    5. Merge → Result_New.json
    6. Run Steel Design Engine → Design_Result_New.json

    Returns:
        (result_new_path, design_result_new_path, success)
    """
    print("\n=== PHASE 2: RE-ANALYSIS ===")

    # Paths
    model_data_new_path = os.path.join(output_dir, "Model_data_new.json")
    analysis_new_path = os.path.join(output_dir, "Analysis_New.json")
    result_new_path = os.path.join(output_dir, "Result_New.json")
    design_result_new_path = os.path.join(output_dir, "Design_Result_New.json")

    os.makedirs(output_dir, exist_ok=True)

    # 1. Load original Result.json
    print("  Loading Result.json...")
    with open(result_json_path, "r") as f:
        result_data = json.load(f)

    # Detect height_mm and z_levels from model data
    model_data_orig = result_data.get("model_data", {})
    height_mm = 4000  # default
    z_levels_list = None  # For non-uniform story heights
    structure_config = model_data_orig.get("structure_config", {})
    grid_sys = model_data_orig.get("grid_system", {})
    # Priority: grid_system z_levels > structure_config z_levels > height_mm
    if grid_sys and "z_levels_mm" in grid_sys:
        z_levels_list = sorted(grid_sys["z_levels_mm"])
        if len(z_levels_list) >= 2:
            height_mm = z_levels_list[1] - z_levels_list[0]
    elif "z_levels" in structure_config:
        z_levels_list = sorted(structure_config["z_levels"])
        if len(z_levels_list) >= 2:
            height_mm = z_levels_list[1] - z_levels_list[0]
    elif "height_mm" in structure_config:
        height_mm = structure_config["height_mm"]

    # 2. Update model_data with new sections
    print("  Updating model data with new sections...")
    updated_model_data = update_model_data(result_data, group_results)

    # 3. Save Model_data_new.json
    with open(model_data_new_path, "w") as f:
        json.dump(updated_model_data, f, indent=2)
    print("  Saved: {}".format(model_data_new_path))

    # 4. Run Analysis.py
    print("  Running OpenSees analysis...")
    if not os.path.exists(PYTHON_EXE):
        print("  ERROR: Python executable not found: {}".format(PYTHON_EXE))
        return result_new_path, design_result_new_path, False

    if not os.path.exists(ANALYSIS_SCRIPT):
        print("  ERROR: Analysis script not found: {}".format(ANALYSIS_SCRIPT))
        return result_new_path, design_result_new_path, False

    # Detect analysis method from original Result.json
    analysis_method = "ELM"  # default
    # Check if DAM was used by looking at original data
    if "_analysis_method" in model_data_orig:
        analysis_method = model_data_orig["_analysis_method"]
    elif "seismic_parameters" in model_data_orig:
        analysis_method = model_data_orig.get("seismic_parameters", {}).get(
            "analysis_method", "ELM")

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    analysis_args = [
        PYTHON_EXE, ANALYSIS_SCRIPT,
        model_data_new_path, analysis_new_path,
        "--method={}".format(analysis_method)
    ]

    proc = subprocess.Popen(
        analysis_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=startupinfo
    )
    _, stderr = proc.communicate()

    if proc.returncode != 0:
        print("  ERROR: Analysis failed (returncode={})".format(proc.returncode))
        if stderr:
            print("  STDERR: {}".format(stderr.decode("utf-8", errors="replace")[:2000]))
        return result_new_path, design_result_new_path, False

    if not os.path.exists(analysis_new_path):
        print("  ERROR: Analysis output not found: {}".format(analysis_new_path))
        return result_new_path, design_result_new_path, False

    print("  Analysis complete: {}".format(analysis_new_path))

    # 5. Merge model_data_new + analysis_new → Result_New.json
    print("  Merging to Result_New.json...")
    with open(analysis_new_path, "r") as f:
        analysis_new_data = json.load(f)

    merged = merge_model_and_analysis(updated_model_data, analysis_new_data,
                                       height_mm=height_mm,
                                       z_levels=z_levels_list)
    with open(result_new_path, "w") as f:
        json.dump(merged, f, indent=2)
    print("  Saved: {}".format(result_new_path))

    # 6. Run Steel Design Engine → Design_Result_New.json
    print("  Running Steel Design Engine...")
    if not os.path.exists(DESIGN_ENGINE_SCRIPT):
        print("  ERROR: Design engine not found: {}".format(DESIGN_ENGINE_SCRIPT))
        return result_new_path, design_result_new_path, False

    design_args = [
        PYTHON_EXE, DESIGN_ENGINE_SCRIPT,
        result_new_path, design_result_new_path,
        "--method={}".format(analysis_method)
    ]

    proc2 = subprocess.Popen(
        design_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=startupinfo
    )
    _, stderr2 = proc2.communicate()

    if proc2.returncode != 0:
        print("  ERROR: Design engine failed (returncode={})".format(proc2.returncode))
        if stderr2:
            print("  STDERR: {}".format(stderr2.decode("utf-8", errors="replace")[:2000]))
        return result_new_path, design_result_new_path, False

    if not os.path.exists(design_result_new_path):
        print("  ERROR: Design result not found: {}".format(design_result_new_path))
        return result_new_path, design_result_new_path, False

    print("  Design check complete: {}".format(design_result_new_path))
    return result_new_path, design_result_new_path, True


# ═══════════════════════════════════════════════════════════════════
# VERIFIED DCR EXTRACTION
# ═══════════════════════════════════════════════════════════════════

def extract_verified_dcr(design_result_new_path, group_results):
    """
    Read Design_Result_New.json and extract verified (post re-analysis)
    max DCR per group AND SMF status per group.

    Returns:
        updated group_results with:
        - 'max_dcr_verified', 'elements_verified'
        - 'smf_ng_elements': list of elements with SMF-NG status
        - 'smf_status': "OK" or "SMF-NG" (worst status in group)
    """
    if not os.path.exists(design_result_new_path):
        return group_results

    with open(design_result_new_path, "r") as f:
        design_new = json.load(f)

    verified_elements = design_new.get("elements", [])

    # Group verified elements
    for g_name in group_results:
        group_results[g_name]["max_dcr_verified"] = None
        group_results[g_name]["elements_verified"] = []
        group_results[g_name]["smf_ng_elements"] = []
        group_results[g_name]["smf_status"] = "OK"

    for elem in verified_elements:
        g = classify_group(elem)
        if g not in group_results:
            continue

        dcr = elem.get("governing_ratio", 0.0)
        status = elem.get("status", "OK")

        group_results[g]["elements_verified"].append({
            "element_id": elem.get("element_id", 0),
            "label_name": elem.get("label_name",
                                    elem.get("frame_label", "?")),
            "dcr_verified": round(dcr, 6),
            "status": status,
        })

        # Track SMF-NG elements
        if "SMF-NG" in status or "HD-NG" in status:
            group_results[g]["smf_ng_elements"].append({
                "element_id": elem.get("element_id", 0),
                "label_name": elem.get("label_name",
                                        elem.get("frame_label", "?")),
                "status": status,
            })

    # Compute max verified DCR per group + SMF status
    for g_name, g_data in group_results.items():
        vlist = g_data.get("elements_verified", [])
        if vlist:
            max_v = max(e["dcr_verified"] for e in vlist)
            g_data["max_dcr_verified"] = round(max_v, 6)

        if g_data["smf_ng_elements"]:
            g_data["smf_status"] = "SMF-NG"

    return group_results


def extract_scwb_from_sde(design_result_new_path):
    """Extract per-plane SCWB failure info from Steel Design Engine output.

    The SDE checks SCWB per bending plane (major & minor separately).
    The minor axis often governs because Zy << Zx for narrow-flange columns.
    This function reads the SDE's authoritative result so the autoselect
    optimisation loop uses the correct scwb_failed flag and governing plane.

    Returns:
        dict: {
            "failed": bool,
            "min_ratio": float or None,
            "governing_plane": "major" | "minor" | None,
            "column_section": str,
            "worst_dcr": float,
        }
    """
    if not os.path.exists(design_result_new_path):
        return {"failed": False, "min_ratio": None,
                "governing_plane": None, "column_section": "",
                "worst_dcr": 0.0}

    with open(design_result_new_path, "r") as f:
        data = json.load(f)

    scwb_failed = False
    min_ratio = None
    worst_dcr = 0.0
    governing_plane = None
    column_section = ""

    for elem in data.get("elements", []):
        if classify_group(elem) != _GRP_KOLOM:
            continue

        smf = elem.get("smf_checks") or {}
        for plane_key, plane_name in [("scwb_major", "major"),
                                       ("scwb_minor", "minor")]:
            sd = smf.get(plane_key, {})
            if not sd:
                continue

            ok = sd.get("ok", True)
            ratio = sd.get("ratio_E3_1")
            dcr = sd.get("dcr", 0.0)

            if not ok:
                scwb_failed = True

            if ratio is not None and (min_ratio is None or ratio < min_ratio):
                min_ratio = ratio
                governing_plane = plane_name
                # Section name: try design_section "Family : Name" first
                ds = elem.get("design_section", "")
                if " : " in ds:
                    column_section = ds.split(" : ", 1)[1].strip()
                else:
                    column_section = (elem.get("section", {})
                                      .get("section_name", ds))

            if dcr > worst_dcr:
                worst_dcr = dcr

    return {
        "failed": scwb_failed,
        "min_ratio": round(min_ratio, 4) if min_ratio is not None else None,
        "governing_plane": governing_plane,
        "column_section": column_section,
        "worst_dcr": round(worst_dcr, 4),
    }


# ═══════════════════════════════════════════════════════════════════
# AISC 341-22 E3.4a — STRONG COLUMN-WEAK BEAM (SCWB) CHECK
# ═══════════════════════════════════════════════════════════════════

SCWB_Ry = 1.1       # Expected yield stress ratio (hot-rolled, AISC 341 Table A3.2)
SCWB_ALPHA_S = 1.0   # LRFD


def build_scwb_joint_map(result_new_data, design_result_new_data):
    """
    Build a map of floor joints → {columns, beams} for SCWB check.

    Uses Result_New.json for topology (start_node, end_node, node_id)
    and Design_Result_New.json for forces (Pr_kN per element).

    Only includes:
      - Floor joints (not support/base, not subdivision nodes)
      - Columns (group = Kolom)
      - Balok Induk (group = Balok Induk) — Balok Anak excluded karena
        tidak menyambung langsung ke kolom

    Returns:
        dict: {node_key: {
            "node_id": str,
            "coords": {"X": float, "Y": float, "Z": float},
            "floor": int,
            "columns_above": [elem_dict, ...],
            "columns_below": [elem_dict, ...],
            "beams": [elem_dict, ...],
        }}
    """
    model_data = result_new_data.get("model_data", {})
    elements = model_data.get("model_elements", [])
    node_class = result_new_data.get("node_classification", {})
    floor_joints = node_class.get("floor_joint_nodes", {})

    # Detect height and z_levels for floor numbering
    structure_config = model_data.get("structure_config", {})
    height_mm = structure_config.get("height_mm", 4000)
    grid_sys = model_data.get("grid_system", {})
    _z_lvl = sorted(grid_sys.get("z_levels_mm", [])) if grid_sys else []
    if not _z_lvl or len(_z_lvl) < 2:
        _z_lvl_sc = structure_config.get("z_levels", [])
        if len(_z_lvl_sc) >= 2:
            _z_lvl = sorted(_z_lvl_sc)

    # Build element_id → Pr_kN mapping from Design Result
    # Priority: SCWB per-column Pr (max |Pr| from seismic combos, correct per E3-2)
    # Fallback: pmm_detail Pr (governing PMM combo, may be non-seismic)
    design_elems = design_result_new_data.get("elements", [])
    pr_map = {}  # element_id → Pr_kN

    # 1. First pass: collect SCWB per-column Pr from smf_checks
    scwb_pr_map = {}  # element_id → max Pr_kN from SCWB seismic analysis
    for de in design_elems:
        smf = de.get("smf_checks") or {}
        for plane_key in ["scwb_major", "scwb_minor"]:
            scwb_d = smf.get(plane_key, {})
            for cbd in scwb_d.get("columns", []):
                cbd_eid = cbd.get("element_id", 0)
                cbd_pr = abs(float(cbd.get("Pr_kN", 0)))
                # Keep max across planes
                if cbd_pr > scwb_pr_map.get(cbd_eid, 0):
                    scwb_pr_map[cbd_eid] = cbd_pr

    # 2. Build final map: prefer SCWB Pr, fallback to pmm_detail Pr
    for de in design_elems:
        eid = de.get("element_id", 0)
        if eid in scwb_pr_map:
            pr_map[eid] = scwb_pr_map[eid]
        else:
            pr_kn = de.get("pmm_detail", {}).get("Pr_kN", 0.0)
            pr_map[eid] = pr_kn

    # Build node_id → node_key mapping from floor joints
    nid_to_key = {}   # "N123" → (x, y, z)
    nid_to_info = {}  # "N123" → {coords, floor}
    for nid, nval in floor_joints.items():
        coords = nval.get("coords", {})
        x = int(round(coords.get("X", 0)))
        y = int(round(coords.get("Y", 0)))
        z = int(round(coords.get("Z", 0)))
        key = (x, y, z)
        nid_to_key[str(nid)] = key
        # Use z_levels for non-uniform story heights
        floor_num = 0
        if _z_lvl and len(_z_lvl) >= 2:
            for li in range(1, len(_z_lvl)):
                if abs(z - int(round(_z_lvl[li]))) < 1:
                    floor_num = li
                    break
            else:
                floor_num = int(round(z / height_mm)) if height_mm > 0 else 0
        elif height_mm > 0:
            floor_num = int(round(z / height_mm))
        nid_to_info[str(nid)] = {
            "coords": coords,
            "floor": floor_num,
        }

    # Initialize joint map
    joint_map = {}
    for nid, key in nid_to_key.items():
        if key not in joint_map:
            info = nid_to_info[nid]
            joint_map[key] = {
                "node_id": nid,
                "coords": info["coords"],
                "floor": info["floor"],
                "columns_above": [],
                "columns_below": [],
                "beams": [],
            }

    # Classify elements into joints
    for elem in elements:
        group = elem.get("group", "")
        topo = elem.get("topology", {})
        start_nid = str(topo.get("start_node_id", ""))
        end_nid = str(topo.get("end_node_id", ""))
        eid = elem.get("id", 0)

        if group == _GRP_KOLOM:
            # Column: end_node (top) at joint → this column is BELOW the joint
            #         start_node (bottom) at joint → this column is ABOVE the joint
            # Convention: column start = bottom, end = top (z_start < z_end)
            start_z = topo.get("start_node", [0, 0, 0])
            end_z = topo.get("end_node", [0, 0, 0])
            if isinstance(start_z, list):
                start_z = start_z[2]
            elif isinstance(start_z, dict):
                start_z = start_z.get("Z", 0)
            if isinstance(end_z, list):
                end_z = end_z[2]
            elif isinstance(end_z, dict):
                end_z = end_z.get("Z", 0)

            col_info = {
                "element_id": eid,
                "label": elem.get("frame_label", elem.get("label_name", "C?")),
                "section": elem.get("section", {}),
                "material": elem.get("material", {}),
                "local_axes": elem.get("local_axes", {}),
                "Pr_kN": pr_map.get(eid, 0.0),
                "length_mm": topo.get("length_mm", abs(end_z - start_z)),
            }

            # Top of column → column is below this joint
            if end_nid in nid_to_key:
                key = nid_to_key[end_nid]
                if key in joint_map:
                    joint_map[key]["columns_below"].append(col_info)

            # Bottom of column → column is above this joint
            if start_nid in nid_to_key:
                key = nid_to_key[start_nid]
                if key in joint_map:
                    joint_map[key]["columns_above"].append(col_info)

        elif group == _GRP_BALOK_INDUK:
            # Beam: both endpoints may be at floor joints
            # Compute horizontal beam direction for per-plane SCWB filtering
            s_node = topo.get("start_node", [0, 0, 0])
            e_node = topo.get("end_node", [0, 0, 0])
            if isinstance(s_node, dict):
                s_node = [s_node.get("X", 0), s_node.get("Y", 0), s_node.get("Z", 0)]
            if isinstance(e_node, dict):
                e_node = [e_node.get("X", 0), e_node.get("Y", 0), e_node.get("Z", 0)]
            bdx = e_node[0] - s_node[0]
            bdy = e_node[1] - s_node[1]
            blen_h = (bdx**2 + bdy**2) ** 0.5
            beam_dir = (bdx / blen_h, bdy / blen_h) if blen_h > 0.01 else (1.0, 0.0)

            beam_info = {
                "element_id": eid,
                "label": elem.get("frame_label", elem.get("label_name", "B?")),
                "section": elem.get("section", {}),
                "material": elem.get("material", {}),
                "length_mm": topo.get("length_mm", 0),
                "beam_dir": beam_dir,
            }

            for nid in [start_nid, end_nid]:
                if nid in nid_to_key:
                    key = nid_to_key[nid]
                    if key in joint_map:
                        # Avoid duplicate (beam counted once per joint)
                        existing_ids = [b["element_id"]
                                        for b in joint_map[key]["beams"]]
                        if eid not in existing_ids:
                            joint_map[key]["beams"].append(beam_info)

    # Filter: only joints with at least 1 column AND 1 beam
    # AISC 341-22 E3.4a Exception: top-story joints exempt
    # (joints where columns end but no column starts above)
    valid_joints = {}
    for key, jdata in joint_map.items():
        n_cols_above = len(jdata["columns_above"])
        n_cols_below = len(jdata["columns_below"])
        n_cols = n_cols_above + n_cols_below
        n_beams = len(jdata["beams"])
        if n_cols > 0 and n_beams > 0:
            # Top-story exemption: skip if columns below exist but none above
            if n_cols_below > 0 and n_cols_above == 0:
                continue  # Roof joint — exempt per AISC 341-22 E3.4a
            valid_joints[key] = jdata

    return valid_joints


def check_scwb_all_joints(joint_map, col_section_name, group_results):
    """
    Run AISC 341-22 E3.4a SCWB check at every joint in joint_map.

    Per-plane check: major and minor bending planes are checked separately.
    Beams are filtered by their direction relative to column local axes.
    The governing (worst) plane determines the result.

    Uses SDE.StrongColumnWeakBeam.check() imported from Steel Design Engine.
    """
    # Get column section properties
    if col_section_name not in IWF_DATABASE:
        return {"passed": True, "min_ratio": float('inf'),
                "joints_checked": 0, "details": [],
                "error": "Column section not in database"}

    col_dims = IWF_DATABASE[col_section_name]
    col_sp = calc_section_props(col_dims["d"], col_dims["bf"],
                                col_dims["tw"], col_dims["tf"])

    # Get beam section name from Balok Induk group
    beam_section_name = group_results.get(_GRP_BALOK_INDUK, {}).get("section", "")

    details = []
    joints_passed = 0
    joints_failed = 0
    min_ratio = float('inf')
    min_ratio_joint = ""
    governing_plane = None

    for _key, jdata in joint_map.items():
        node_id = jdata["node_id"]
        floor_num = jdata["floor"]

        # Get column local axes for per-plane beam filtering
        all_cols = jdata["columns_above"] + jdata["columns_below"]
        col_local_axes = {}
        for col in all_cols:
            la = col.get("local_axes", {})
            if la:
                col_local_axes = la
                break
        y_axis = col_local_axes.get("y_axis", [1, 0, 0])
        z_axis = col_local_axes.get("z_axis", [0, 1, 0])

        # ── Build columns_at_joint for SDE ──
        columns_at_joint_major = []
        columns_at_joint_minor = []
        col_detail_list = []

        for col in all_cols:
            mat = col.get("material", {})
            Fy_col = mat.get("Fy_MPa", Fy_DEFAULT)
            Fu_col = mat.get("Fu_MPa", Fu_DEFAULT)

            # Major plane: column uses Zx (strong axis plastic modulus)
            sec_sde_major = build_sde_section(col_section_name, col_dims,
                                               Fy=Fy_col, Fu=Fu_col)
            # Minor plane: column uses Zy — create modified section with Zx=Zy
            sec_sde_minor = build_sde_section(col_section_name, col_dims,
                                               Fy=Fy_col, Fu=Fu_col)
            sec_sde_minor = SDE.SectionProperties(
                name=sec_sde_minor.name,
                d=sec_sde_minor.d, bf=sec_sde_minor.bf,
                tw=sec_sde_minor.tw, tf=sec_sde_minor.tf,
                A=sec_sde_minor.A,
                Ix=sec_sde_minor.Iy, Iy=sec_sde_minor.Ix,
                Sx=sec_sde_minor.Sy, Sy=sec_sde_minor.Sx,
                Zx=sec_sde_minor.Zy, Zy=sec_sde_minor.Zx,
                rx=sec_sde_minor.ry, ry=sec_sde_minor.rx,
                J=sec_sde_minor.J, Cw=sec_sde_minor.Cw,
                Fy=Fy_col, Fu=Fu_col,
                E=sec_sde_minor.E, G=sec_sde_minor.G,
            )

            Pr_N = abs(col["Pr_kN"]) * 1000.0
            columns_at_joint_major.append((sec_sde_major, Pr_N))
            columns_at_joint_minor.append((sec_sde_minor, Pr_N))

            is_above = col in jdata["columns_above"]
            pos_label = "atas" if is_above else "bawah"

            Zc = col_sp["Zz"]
            Ag = col_sp["Area"]
            Fyc_minus = Fy_col - SCWB_ALPHA_S * Pr_N / Ag
            Mpc_Nmm = max(Zc * Fyc_minus, 0.0)

            col_detail_list.append({
                "label": "{} ({})".format(col["label"], pos_label),
                "element_id": col["element_id"],
                "Pr_kN": round(col["Pr_kN"], 2),
                "Pr_N": round(Pr_N, 1),
                "Zc_mm3": round(Zc, 1),
                "Ag_mm2": round(Ag, 1),
                "Fyc_MPa": Fy_col,
                "alpha_s": SCWB_ALPHA_S,
                "Pr_over_Ag": round(Pr_N / Ag, 2) if Ag > 0 else 0,
                "Fyc_minus_Pr_Ag": round(Fyc_minus, 2),
                "Mpc_Nmm": round(Mpc_Nmm, 1),
                "Mpc_kNm": round(Mpc_Nmm / 1e6, 2),
            })

        # ── Build beams_at_joint, split by bending plane ──
        beams_major = []  # Beams in major bending plane (run along y_axis)
        beams_minor = []  # Beams in minor bending plane (run along z_axis)
        beam_detail_list = []

        for beam in jdata["beams"]:
            sec_beam = beam.get("section", {})
            mat_beam = beam.get("material", {})
            Fy_beam = mat_beam.get("Fy_MPa", Fy_DEFAULT)
            Fu_beam = mat_beam.get("Fu_MPa", Fu_DEFAULT)

            d_beam = sec_beam.get("d_mm", 0)
            bf_beam = sec_beam.get("b_mm", 0)
            tw_beam = sec_beam.get("tw_mm", 0)
            tf_beam = sec_beam.get("tf_mm", 0)
            L_beam = beam.get("length_mm", 0)

            if d_beam <= 0 or L_beam <= 0:
                continue

            beam_sp = calc_section_props(d_beam, bf_beam, tw_beam, tf_beam)
            sec_name_beam = sec_beam.get("section_name", "?")
            sec_sde_beam = SDE.SectionProperties(
                name=sec_name_beam,
                d=d_beam, bf=bf_beam, tw=tw_beam, tf=tf_beam,
                A=beam_sp["Area"],
                Ix=beam_sp["Iz"], Iy=beam_sp["Iy"],
                Sx=beam_sp["Sz"], Sy=beam_sp["Sy"],
                Zx=beam_sp["Zz"], Zy=beam_sp["Zy"],
                rx=beam_sp["rz"], ry=beam_sp["ry"],
                J=beam_sp["J"], Cw=beam_sp["Cw"],
                Fy=Fy_beam, Fu=Fu_beam,
                E=E_STEEL, G=G_STEEL,
            )

            # Classify beam by bending plane using direction
            bdir = beam.get("beam_dir", (1.0, 0.0))
            proj_y = abs(bdir[0] * y_axis[0] + bdir[1] * y_axis[1])
            proj_z = abs(bdir[0] * z_axis[0] + bdir[1] * z_axis[1])
            if proj_y >= proj_z:
                beams_major.append((sec_sde_beam, L_beam))
                plane_label = "major"
            else:
                beams_minor.append((sec_sde_beam, L_beam))
                plane_label = "minor"

            Zx_b = beam_sp["Zz"]
            db = d_beam
            Mpr = 1.1 * SCWB_Ry * Fy_beam * Zx_b
            sh = db
            Lh = L_beam - 2.0 * sh
            if Lh <= 0:
                Lh = L_beam * 0.5
            Vpr = 2.0 * Mpr / Lh
            Mv = Vpr * sh
            Mbe = Mpr + SCWB_ALPHA_S * Mv

            beam_detail_list.append({
                "label": beam["label"],
                "element_id": beam["element_id"],
                "section_name": sec_name_beam,
                "plane": plane_label,
                "Zx_mm3": round(Zx_b, 1),
                "Fy_MPa": Fy_beam,
                "Ry": SCWB_Ry,
                "db_mm": round(db, 1),
                "L_mm": round(L_beam, 1),
                "Mpr_Nmm": round(Mpr, 1),
                "Mpr_kNm": round(Mpr / 1e6, 2),
                "sh_mm": round(sh, 1),
                "Lh_mm": round(Lh, 1),
                "Vpr_N": round(Vpr, 1),
                "Vpr_kN": round(Vpr / 1000, 2),
                "Mv_Nmm": round(Mv, 1),
                "Mv_kNm": round(Mv / 1e6, 2),
                "Mbe_Nmm": round(Mbe, 1),
                "Mbe_kNm": round(Mbe / 1e6, 2),
            })

        if not columns_at_joint_major:
            continue

        # ── Per-plane SCWB check ──
        # Check each plane that has beams; take the worst ratio
        best_ratio = float('inf')
        best_ok = True
        best_result = None
        best_plane = None

        for plane_name, cols_for_plane, beams_for_plane in [
            ("major", columns_at_joint_major, beams_major),
            ("minor", columns_at_joint_minor, beams_minor),
        ]:
            if not beams_for_plane:
                continue  # No beams in this plane → skip (not applicable)

            result = SDE.StrongColumnWeakBeam.check(
                cols_for_plane, beams_for_plane,
                Ry=SCWB_Ry, alpha_s=SCWB_ALPHA_S
            )
            r = result["ratio"]
            if r < best_ratio:
                best_ratio = r
                best_ok = result["ok"]
                best_result = result
                best_plane = plane_name

        if best_result is None:
            continue  # No beams in any plane

        ratio = best_ratio
        ok = best_ok

        if ok:
            joints_passed += 1
        else:
            joints_failed += 1

        if ratio < min_ratio:
            min_ratio = ratio
            min_ratio_joint = node_id
            governing_plane = best_plane

        sum_Mpc_kNm = round(best_result["sum_Mpc_Nmm"] / 1e6, 2)
        sum_Mbe_kNm = round(best_result["sum_Mbe_Nmm"] / 1e6, 2)

        details.append({
            "node_id": node_id,
            "coords": jdata["coords"],
            "floor": floor_num,
            "governing_plane": best_plane,
            "n_columns": len(columns_at_joint_major),
            "n_beams_major": len(beams_major),
            "n_beams_minor": len(beams_minor),
            "columns": col_detail_list,
            "sum_Mpc_kNm": sum_Mpc_kNm,
            "sum_Mpc_Nmm": round(best_result["sum_Mpc_Nmm"], 1),
            "beams": beam_detail_list,
            "sum_Mbe_kNm": sum_Mbe_kNm,
            "sum_Mbe_Nmm": round(best_result["sum_Mbe_Nmm"], 1),
            "ratio": round(ratio, 4),
            "required": "> 1.0 (AISC 341-22 Eq. E3-1)",
            "status": "PASS" if ok else "FAIL",
        })

    joints_checked = joints_passed + joints_failed
    all_passed = joints_failed == 0 and joints_checked > 0

    return {
        "standard": "AISC 341-22 E3.4a",
        "parameters": {
            "Ry": SCWB_Ry,
            "alpha_s": SCWB_ALPHA_S,
            "sh_method": "db (unreinforced connection, AISC 358)",
        },
        "column_section": col_section_name,
        "beam_section": beam_section_name,
        "passed": all_passed,
        "min_ratio": round(min_ratio, 4) if min_ratio < float('inf') else None,
        "min_ratio_joint": min_ratio_joint,
        "governing_plane": governing_plane,
        "joints_checked": joints_checked,
        "joints_passed": joints_passed,
        "joints_failed": joints_failed,
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ═══════════════════════════════════════════════════════════════════

MAX_ITERATIONS = 5


def _reoptimize_group(g_name, design_new_data, old_original_section,
                      exclude_sections=None):
    """Re-optimize a single overstressed group using forces from Design_Result_New.

    Extracts elements belonging to `g_name` from the latest design result,
    then runs `optimize_group()` to find the lightest section that satisfies
    DCR_MAX with the updated forces.

    Returns:
        Updated group result dict (same structure as optimize_group output)
    """
    all_elems = design_new_data.get("elements", [])
    g_elems = [e for e in all_elems if classify_group(e) == g_name]

    if not g_elems:
        return None

    dt_override = "Column" if g_name == _GRP_KOLOM else None
    result = optimize_group(g_name, g_elems, dt_override,
                            exclude_sections=exclude_sections)

    # Preserve original_section from the very first Phase 1 run
    result["original_section"] = old_original_section
    return result


def run_autoselect(design_result_path, result_json_path, output_dir):
    """
    Main entry point. Full auto-select pipeline with ITERATIVE convergence.

    Flow:
      Phase 1: Quick optimization using original forces
      Loop (max MAX_ITERATIONS):
        Phase 2: Re-analysis (OpenSees + Steel Design Engine)
        Check: any group overstressed?
          No  → converged, break
          Yes → re-optimize overstressed groups with new forces, repeat

    Args:
        design_result_path: Path to Design Result.json (Phase 1 input)
        result_json_path:   Path to Result.json (Phase 2 input, model data)
        output_dir:         Directory for output files

    Returns:
        Auto-Select Result dict
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # ── Phase 1: Quick Optimization (original forces) ──
    print("=== PHASE 1: QUICK OPTIMIZATION ===")
    with open(design_result_path, "r") as f:
        design_data = json.load(f)

    design_info = design_data.get("design_info", {})
    group_results = phase1_optimize(design_data)

    # Save original sections (never overwritten during iterations)
    original_sections = {}
    for g_name, g_data in group_results.items():
        original_sections[g_name] = g_data.get("original_section", "?")

    # Check if any group has changes
    has_changes = any(
        g.get("status") in ("ok", "unresolved") and g.get("section")
        for g in group_results.values()
    )

    if not has_changes:
        print("\nNo optimization needed -- all groups empty or unchanged.")

    # ── Iterative Re-Analysis Loop ──
    result_new_path = os.path.join(output_dir, "Result_New.json")
    design_result_new_path = os.path.join(output_dir, "Design_Result_New.json")
    reanalysis_success = False
    total_iterations = 0

    scwb_result = None  # Will be populated after re-analysis
    tried_per_group = {g: set() for g in group_results}
    smf_best_effort = set()  # Groups where no full-SMF section exists

    if has_changes and os.path.exists(result_json_path):
        # Load group names from Result.json for downstream classification
        with open(result_json_path, "r") as f:
            _result_data_tmp = json.load(f)
        _load_group_names(_result_data_tmp)
        del _result_data_tmp

        for iteration in range(MAX_ITERATIONS):
            total_iterations = iteration + 1
            print("\n=== ITERATION {}/{} ===".format(
                total_iterations, MAX_ITERATIONS))

            # ── Phase 2: Re-analysis with current sections ──
            result_new_path, design_result_new_path, reanalysis_success = \
                phase2_reanalysis(result_json_path, group_results, output_dir)

            if not reanalysis_success:
                print("  Re-analysis failed -- stopping iterations.")
                break

            # ── Extract verified DCR ──
            group_results = extract_verified_dcr(
                design_result_new_path, group_results)

            # ── Check for overstressed groups (DCR) ──
            overstressed = []
            for g_name, g_data in group_results.items():
                dcr_v = g_data.get("max_dcr_verified")
                if dcr_v is not None and dcr_v > DCR_MAX:
                    overstressed.append(g_name)

            # ── SCWB Check (AISC 341-22 E3.4a) ──
            # Use SDE's per-plane result (authoritative) for the
            # scwb_failed flag.  The SDE checks major & minor axes
            # separately; minor often governs because Zy << Zx for
            # narrow-flange columns.  The autoselect's joint-based
            # check is kept for reporting only.
            scwb_failed = False
            col_section = group_results.get(_GRP_KOLOM, {}).get("section", "")

            # 1. Authoritative per-plane SCWB from SDE output
            sde_scwb = extract_scwb_from_sde(design_result_new_path)

            # Set scwb_failed from SDE FIRST (works even without
            # result_new_path for the joint-based report).
            if sde_scwb["failed"]:
                scwb_failed = True
                if _GRP_KOLOM not in overstressed:
                    overstressed.append(_GRP_KOLOM)

            if col_section and os.path.exists(result_new_path):
                print("\n  SCWB Check (AISC 341-22 E3.4a)...")

                # 2. Joint-based check for reporting / detail output
                with open(result_new_path, "r") as f:
                    result_new_data = json.load(f)
                with open(design_result_new_path, "r") as f:
                    design_new_data_scwb = json.load(f)

                joint_map = build_scwb_joint_map(result_new_data,
                                                  design_new_data_scwb)
                scwb_result = check_scwb_all_joints(
                    joint_map, col_section, group_results)

                # Attach SDE per-plane info so _smf_aware_select can
                # use the correct axis (Zx vs Zy) and ratio.
                scwb_result["sde_governing_plane"] = sde_scwb.get(
                    "governing_plane")
                scwb_result["sde_min_ratio"] = sde_scwb.get("min_ratio")

                if scwb_result["joints_checked"] > 0:
                    print("    Joints checked : {}".format(
                        scwb_result["joints_checked"]))
                    print("    Joints passed  : {}".format(
                        scwb_result["joints_passed"]))
                    print("    Joints failed  : {}".format(
                        scwb_result["joints_failed"]))
                    min_r = scwb_result.get("min_ratio")
                    if min_r is not None:
                        print("    Min ratio (joint) : {:.4f}".format(
                            min_r))

                if scwb_failed:
                    print("    SDE per-plane    : FAILED "
                          "(plane={}, ratio={})".format(
                              sde_scwb.get("governing_plane", "?"),
                              sde_scwb.get("min_ratio", "?")))
                    print("    >> SCWB FAILED -- kolom needs upsize")
                else:
                    print("    >> SCWB PASSED")

            elif scwb_failed:
                # No joint-based check, but SDE flagged SCWB failure.
                # Build minimal scwb_result for _smf_aware_select.
                scwb_result = {
                    "passed": False,
                    "min_ratio": sde_scwb.get("min_ratio"),
                    "column_section": sde_scwb.get("column_section",
                                                    col_section),
                    "sde_governing_plane": sde_scwb.get(
                        "governing_plane"),
                    "sde_min_ratio": sde_scwb.get("min_ratio"),
                    "joints_checked": 0, "joints_passed": 0,
                    "joints_failed": 0, "details": [],
                }
                print("\n  SCWB Check: FAILED from SDE "
                      "(plane={}, ratio={})".format(
                          sde_scwb.get("governing_plane", "?"),
                          sde_scwb.get("min_ratio", "?")))

            # ── SMF Status Check (AISC 341-22 D1.1b, D1.2b, E3.4c) ──
            smf_failed = False
            smf_failed_groups = []

            print("\n  SMF Compliance Check...")
            for g_name, g_data in group_results.items():
                smf_ng = g_data.get("smf_ng_elements", [])
                if smf_ng:
                    if g_name in smf_best_effort:
                        # Best-effort: no full-SMF section in database
                        print("    {}: SMF-NG ({} elem) "
                              "[best-effort accepted]".format(
                                  g_name, len(smf_ng)))
                    else:
                        smf_failed = True
                        smf_failed_groups.append(g_name)
                        if g_name not in overstressed:
                            overstressed.append(g_name)
                        print("    {}: SMF-NG ({} element(s))".format(
                            g_name, len(smf_ng)))
                        for se in smf_ng[:3]:
                            print("      {} -- {}".format(
                                se.get("label_name", "?"),
                                se.get("status", "?")))
                        if len(smf_ng) > 3:
                            print("      ... and {} more".format(
                                len(smf_ng) - 3))
                else:
                    print("    {}: SMF OK".format(g_name))

            # ── Convergence check ──
            if not overstressed and not scwb_failed and not smf_failed:
                print("\n  ALL CHECKS PASSED (DCR + SCWB + SMF) -- converged "
                      "in {} iteration(s).".format(total_iterations))
                break

            # Last iteration -- can't re-optimize further
            if iteration == MAX_ITERATIONS - 1:
                warnings = []
                if overstressed:
                    dcr_over = [g for g in overstressed
                                if g not in smf_failed_groups]
                    if dcr_over:
                        warnings.append("DCR overstressed: {}".format(
                            ", ".join(dcr_over)))
                if scwb_failed:
                    warnings.append("SCWB ratio < 1.0")
                if smf_failed:
                    warnings.append("SMF-NG: {}".format(
                        ", ".join(smf_failed_groups)))
                print("\n  WARNING: not converged after {} iterations -- {}".format(
                    MAX_ITERATIONS, "; ".join(warnings)))
                break

            # ── Re-optimize overstressed groups with NEW forces ──
            print("\n  Overstressed groups: {} -- re-optimizing...".format(
                ", ".join(overstressed)))

            with open(design_result_new_path, "r") as f:
                design_new_data = json.load(f)

            for g_name in overstressed:
                old_section = group_results[g_name].get("section", "?")
                old_orig = original_sections.get(g_name, "?")

                # Mark current section as tried (it failed verification)
                if old_section and old_section != "?":
                    tried_per_group.setdefault(g_name, set()).add(
                        old_section)

                # Determine failure type
                # For Kolom when SCWB failed: ALWAYS use SCWB-aware selection
                # (governing_ratio is PMM/Shear only; SCWB failure is
                #  detected via extract_scwb_from_sde / smf_checks)
                dcr_v = group_results[g_name].get("max_dcr_verified")
                dcr_ok = dcr_v is not None and dcr_v <= DCR_MAX
                needs_smf_scwb = (
                    (scwb_failed and g_name == _GRP_KOLOM) or
                    (dcr_ok and g_name in smf_failed_groups)
                )

                if needs_smf_scwb:
                    # ── SMF/SCWB-aware section selection ──
                    print("    {}: SMF/SCWB-aware selection...".format(
                        g_name))
                    new_sec, reason, is_best_effort = _smf_aware_select(
                        g_name, design_new_data, group_results,
                        tried_sections=tried_per_group.get(g_name, set()),
                        scwb_result=scwb_result)

                    if new_sec:
                        group_results[g_name]["section"] = new_sec
                        print("    {}: {} >> {} ({})".format(
                            g_name, old_section, new_sec, reason))
                        if is_best_effort:
                            smf_best_effort.add(g_name)
                    else:
                        print("    {}: no viable section ({})".format(
                            g_name, reason))
                        smf_best_effort.add(g_name)
                else:
                    # ── Pure DCR re-optimization ──
                    new_result = _reoptimize_group(
                        g_name, design_new_data, old_orig,
                        exclude_sections=tried_per_group.get(
                            g_name, set()))

                    if new_result is None:
                        print("    {}: no elements found -- "
                              "skipping".format(g_name))
                        continue

                    new_section = new_result.get("section", "?")
                    new_dcr = new_result.get("max_dcr", 0)

                    print("    {}: {} >> {} (DCR {:.4f})".format(
                        g_name, old_section, new_section, new_dcr))

                    group_results[g_name] = new_result

    # ── Build Output ──
    sections_needed = {}
    for g_name, g_data in group_results.items():
        sec_name = g_data.get("section")
        if sec_name and sec_name in IWF_DATABASE:
            sections_needed[sec_name] = IWF_DATABASE[sec_name]

    # SMF summary for output
    smf_summary = {}
    for g_name, g_data in group_results.items():
        ng_elems = g_data.get("smf_ng_elements", [])
        smf_summary[g_name] = {
            "status": g_data.get("smf_status", "OK"),
            "best_effort": g_name in smf_best_effort,
            "ng_count": len(ng_elems),
            "ng_elements": ng_elems,
        }

    output = {
        "timestamp": timestamp,
        "dcr_target": {"min": DCR_MIN, "max": DCR_MAX},
        "design_info": design_info,
        "reanalysis_success": reanalysis_success,
        "iterations": total_iterations,
        "groups": {},
        "scwb_check": scwb_result,
        "smf_check": smf_summary,
        "sections_needed": sections_needed,
        "result_new_path": result_new_path if reanalysis_success else None,
        "design_result_new_path": (design_result_new_path
                                   if reanalysis_success else None),
    }

    for g_name, g_data in group_results.items():
        # Use verified DCR for in_target_range (not stale Phase 1 value)
        dcr_verified = g_data.get("max_dcr_verified")
        if dcr_verified is not None:
            in_range = DCR_MIN <= dcr_verified <= DCR_MAX
        else:
            in_range = g_data.get("in_target_range", False)
        output["groups"][g_name] = {
            "original_section": original_sections.get(g_name, "?"),
            "recommended_section": g_data.get("section"),
            "max_dcr_initial": g_data.get("max_dcr", 0),
            "max_dcr_verified": dcr_verified,
            "in_target_range": in_range,
            "status": g_data.get("status", "?"),
            "smf_status": g_data.get("smf_status", "OK"),
            "element_count": g_data.get("element_count", 0),
            "elements": g_data.get("elements", []),
            "elements_verified": g_data.get("elements_verified", []),
        }

    # Write Auto-Select Result.json
    autoselect_path = os.path.join(output_dir, "Auto-Select Result.json")
    with open(autoselect_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # ── Print Summary ──
    print("\n" + "=" * 60)
    print("  AUTO-SELECT SUMMARY")
    print("=" * 60)
    print("  Timestamp  : {}".format(timestamp))
    print("  DCR Target : {:.2f} - {:.2f}".format(DCR_MIN, DCR_MAX))
    print("  Iterations : {}".format(total_iterations))
    print("  Re-analysis: {}".format(
        "SUCCESS" if reanalysis_success else "SKIPPED/FAILED"))
    print("")

    hdr = "  {:<14} {:<26} {:<26} {:>8} {:>8}".format(
        "Group", "Section Asal", "Section Baru", "DCR Init", "DCR Verif")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for g_name, g_data in output["groups"].items():
        dcr_v = g_data.get("max_dcr_verified")
        dcr_v_str = "{:.4f}".format(dcr_v) if dcr_v is not None else "N/A"
        print("  {:<14} {:<26} {:<26} {:>8.4f} {:>8}".format(
            g_name,
            g_data.get("original_section", "?")[:26],
            (g_data.get("recommended_section") or "?")[:26],
            g_data.get("max_dcr_initial", 0),
            dcr_v_str))

    # ── SCWB Summary ──
    if scwb_result and scwb_result.get("joints_checked", 0) > 0:
        print("")
        print("  SCWB (AISC 341-22 E3.4a):")
        print("    Column : {}".format(scwb_result.get("column_section", "?")))
        print("    Beam   : {}".format(scwb_result.get("beam_section", "?")))
        print("    Joints : {}/{} passed".format(
            scwb_result["joints_passed"], scwb_result["joints_checked"]))
        min_r = scwb_result.get("min_ratio")
        if min_r is not None:
            print("    Min ratio: {:.4f} at {} (req. > 1.0) >> {}".format(
                min_r, scwb_result.get("min_ratio_joint", "?"),
                "PASS" if scwb_result["passed"] else "FAIL"))

    # ── SMF Summary ──
    has_smf_ng = any(s.get("ng_count", 0) > 0 and not s.get("best_effort")
                     for s in smf_summary.values())
    has_best_effort = any(s.get("best_effort") for s in smf_summary.values())
    print("")
    print("  SMF Compliance (AISC 341-22):")
    for g_name, s_data in smf_summary.items():
        ng_count = s_data.get("ng_count", 0)
        be = s_data.get("best_effort", False)
        if ng_count > 0 and be:
            print("    {}: SMF-NG ({} elem) [best-effort -- "
                  "no full-SMF section in database]".format(
                      g_name, ng_count))
        elif ng_count > 0:
            print("    {}: SMF-NG ({} element(s))".format(g_name, ng_count))
        else:
            print("    {}: OK".format(g_name))
    if has_best_effort:
        print("    Note: best-effort groups use the lightest section "
              "satisfying L/r and Lb/ry; flange/web HD may not be met "
              "by available standard sections.")
    print("    Overall: {}".format("FAIL" if has_smf_ng else "PASS"))

    print("")
    print("  Output: {}".format(autoselect_path))
    if reanalysis_success:
        print("  Result_New: {}".format(result_new_path))
        print("  Design_Result_New: {}".format(design_result_new_path))

    return output


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python \"Autoselect Engine.py\" "
              "<design_result.json> <result.json> <output_dir>")
        print("")
        print("  design_result.json : Input Design Result.json (Phase 1)")
        print("  result.json        : Input Result.json (Phase 2, model data)")
        print("  output_dir         : Output directory for new files")
        sys.exit(1)

    _design_path = sys.argv[1]
    _result_path = sys.argv[2]
    _output_dir  = sys.argv[3]

    if not os.path.exists(_design_path):
        print("ERROR: Design Result not found: {}".format(_design_path))
        sys.exit(1)

    if not os.path.exists(_result_path):
        print("ERROR: Result.json not found: {}".format(_result_path))
        sys.exit(1)

    run_autoselect(_design_path, _result_path, _output_dir)
