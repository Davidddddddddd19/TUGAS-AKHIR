"""
Visualizer Engine — ROIDA
==========================
Standalone PyQt5 + Matplotlib window for structural force diagrams.
Reads Result.json (from Create.pushbutton) for analysis data, and
optionally Design Result.json for DCR information.

Layout (portrait, scrollable):
  ┌─ Header (blue bar): Label | Section | Type | L ─────────┐
  ├─ Info bar: element details ────────────────────────────  ┤
  ├─ Controls: Load Case | Axis toggle ────────────────────  ┤
  ├─ Scroll Area ─────────────────────────────────────────   ┤
  │  ├─ SFD panel (title | canvas | max info)               │
  │  ├─ BMD panel                                           │
  │  ├─ NFD panel                                           │
  │  └─ Deflection panel                                    │
  ├─ DCR bar (only if Design Result loaded) ───────────────  ┤
  └─ Buttons: [Select Next Element]  [Export PNG] ──────────┘

Force unit convention in Result.json (raw OpenSeesPy output):
    P  (N)    -- axial; negative = compression
    Fy (N)    -- minor-axis shear
    Fz (N)    -- major-axis shear
    My (N.mm) -- minor-axis moment
    Mz (N.mm) -- major-axis moment
Display units: kN and kN.m
"""

import argparse
import json
import os
import sys

# PyQt5
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QRadioButton, QButtonGroup, QPushButton,
    QFileDialog, QSizePolicy, QScrollArea, QFrame,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

# Matplotlib
import matplotlib
matplotlib.use("Qt5Agg")
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.ticker as ticker


# ===================================================================
# DATA LOADING
# ===================================================================

_PRIVATE_KEYS = {"_plots", "_validation", "_modal"}


def load_result(result_path):
    with open(result_path, "r") as f:
        data = json.load(f)
    model_elems      = data.get("model_data", {}).get("model_elements", [])
    analysis_results = data.get("analysis_results", {})
    return model_elems, analysis_results


def load_design(design_path):
    if not design_path or not os.path.exists(design_path):
        return {}
    with open(design_path, "r") as f:
        data = json.load(f)
    return {int(e["element_id"]): e
            for e in data.get("elements", [])
            if "element_id" in e}


def build_element_index(model_elems):
    return {int(e["id"]): e for e in model_elems}


def get_load_cases(analysis_results):
    return sorted(
        k for k in analysis_results
        if not k.startswith("_") and k not in _PRIVATE_KEYS
    )


# ===================================================================
# PER-ELEMENT DATA ACCESSORS
# ===================================================================

def _elem_lc(analysis_results, lc_name, eid_str):
    lc = analysis_results.get(lc_name, {})
    return lc.get("elements", {}).get(eid_str, {})


def get_station_data(analysis_results, lc_name, eid_str):
    return _elem_lc(analysis_results, lc_name, eid_str).get("stations", [])


def get_deflection_profile(analysis_results, lc_name, eid_str):
    return _elem_lc(analysis_results, lc_name, eid_str).get("deflection_profile", None)


def get_max_deflection(analysis_results, lc_name, eid_str):
    return _elem_lc(analysis_results, lc_name, eid_str).get("max_deflection", None)


def get_element_type(analysis_results, eid_str):
    for lc_data in analysis_results.values():
        if not isinstance(lc_data, dict):
            continue
        elem = lc_data.get("elements", {}).get(eid_str)
        if elem:
            return elem.get("element_type", "Beam")
    return "Beam"


def get_element_length(analysis_results, eid_str):
    for lc_data in analysis_results.values():
        if not isinstance(lc_data, dict):
            continue
        elem = lc_data.get("elements", {}).get(eid_str)
        if elem:
            return elem.get("element_length_mm", 0)
    return 0


# ===================================================================
# FORCE / DEFLECTION EXTRACTION
# ===================================================================

def get_force_keys(local_axes, axis="Major"):
    """Determine JSON keys for shear, moment, deflection based on element local axes.

    Coupled pairs (beam mechanics):
      (Fz, My, dz) = bending in x-z plane
      (Fy, Mz, dy) = bending in x-y plane

    For horizontal elements (beams): the local axis closer to gravity
    determines major shear  →  Major = (Fz, My) when z is vertical.
    For vertical elements (columns): SAP2000 local-2 = our local-y
    →  Major = (Fy, Mz).
    """
    x_ax = local_axes.get("x_axis", [1, 0, 0])
    z_ax = local_axes.get("z_axis", [0, 0, 1])
    y_ax = local_axes.get("y_axis", [0, 1, 0])

    is_vertical = abs(x_ax[2]) > 0.7  # column: x_axis ≈ [0,0,1]

    if is_vertical:
        # Column: SAP2000 Major (V2, M3) = our (Fy, Mz)
        major = ("Fy", "Mz", "dy_mm", "delta_y")
        minor = ("Fz", "My", "dz_mm", "delta_z")
    else:
        # Beam: axis closer to gravity carries major shear
        z_vert = abs(z_ax[2])
        y_vert = abs(y_ax[2])
        if z_vert >= y_vert:
            major = ("Fz", "My", "dz_mm", "delta_z")
            minor = ("Fy", "Mz", "dy_mm", "delta_y")
        else:
            major = ("Fy", "Mz", "dy_mm", "delta_y")
            minor = ("Fz", "My", "dz_mm", "delta_z")

    return major if axis == "Major" else minor


def extract_forces(stations, shear_key="Fz", moment_key="My"):
    """Extract station forces using the specified JSON keys."""
    x_mm = []; shear = []; moment = []; axial = []
    for st in stations:
        x_mm.append(st.get("distance_mm", 0.0))
        axial.append(st.get("P", 0.0) / 1000.0)
        shear.append(st.get(shear_key, 0.0) / 1000.0)
        moment.append(st.get(moment_key, 0.0) / 1.0e6)
    return x_mm, shear, moment, axial


def extract_deflection(defl_profile, max_defl, L_mm,
                       defl_key="dz_mm", defl_max_prefix="delta_z"):
    """Extract deflection using the specified JSON keys.

    Returns (x_vals, d_vals, chord) where *chord* is (start, end) absolute
    displacement tuple for columns, or None for beams.
    """
    if defl_profile is not None:
        ratios = defl_profile.get("stations_ratio", [])
        d_vals = defl_profile.get(defl_key, [0.0] * len(ratios))
        x_vals = [r * L_mm for r in ratios]
        # Chord endpoints for SAP2000-style column visualization
        chord = None
        ce = defl_profile.get("chord_endpoints")
        if ce:
            prefix = defl_key[:2]  # "dy" or "dz"
            chord = (ce.get(prefix + "_start", 0.0), ce.get(prefix + "_end", 0.0))
        return x_vals, d_vals, chord
    if max_defl is None:
        return [0.0, L_mm], [0.0, 0.0], None
    d_max = max_defl.get(defl_max_prefix + "_max_mm", 0.0)
    d_x   = max_defl.get(defl_max_prefix + "_distance_mm", L_mm / 2.0)
    return [0.0, d_x, L_mm], [0.0, d_max, 0.0], None


# ===================================================================
# DIAGRAM DRAWING
# ===================================================================

_DIAGRAM_COLORS = {
    "SFD":  ("#1565C0", "#BBDEFB"),
    "BMD":  ("#B71C1C", "#FFCDD2"),
    "NFD":  ("#1B5E20", "#C8E6C9"),
    "Defl": ("#4A148C", "#E1BEE7"),
}


def _cubic_spline_resample(xp, yp, n_out=51):
    """Resample sparse data onto n_out points using natural cubic spline.

    Pure-Python implementation (no numpy/scipy needed).
    Returns (x_smooth, y_smooth) lists.
    """
    n = len(xp)
    if n < 3 or n_out < n:
        return list(xp), list(yp)

    # --- Build tridiagonal system for natural cubic spline ---
    h = [xp[i + 1] - xp[i] for i in range(n - 1)]

    # Right-hand side
    alpha = [0.0] * n
    for i in range(1, n - 1):
        alpha[i] = (3.0 / h[i] * (yp[i + 1] - yp[i])
                     - 3.0 / h[i - 1] * (yp[i] - yp[i - 1]))

    # Thomas algorithm (forward sweep)
    l = [1.0] * n
    mu = [0.0] * n
    z = [0.0] * n
    for i in range(1, n - 1):
        l[i] = 2.0 * (xp[i + 1] - xp[i - 1]) - h[i - 1] * mu[i - 1]
        if abs(l[i]) < 1e-30:
            l[i] = 1e-30
        mu[i] = h[i] / l[i]
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]

    # Back substitution → spline coefficients b, c, d
    b = [0.0] * (n - 1)
    c = [0.0] * n
    d = [0.0] * (n - 1)
    for j in range(n - 2, -1, -1):
        c[j] = z[j] - mu[j] * c[j + 1]
        b[j] = (yp[j + 1] - yp[j]) / h[j] - h[j] * (c[j + 1] + 2.0 * c[j]) / 3.0
        d[j] = (c[j + 1] - c[j]) / (3.0 * h[j])

    # --- Evaluate at n_out equally-spaced points ---
    x_out = [xp[0] + i * (xp[-1] - xp[0]) / (n_out - 1) for i in range(n_out)]
    y_out = []
    seg = 0
    for x in x_out:
        # Advance segment index
        while seg < n - 2 and x > xp[seg + 1]:
            seg += 1
        dx = x - xp[seg]
        y_out.append(yp[seg] + b[seg] * dx + c[seg] * dx ** 2 + d[seg] * dx ** 3)
    return x_out, y_out


def draw_diagram(ax, x_mm, values, ylabel, color_key, smooth=True):
    """Draw one force/deflection diagram on the given Axes. Returns max abs value."""
    line_col, fill_col = _DIAGRAM_COLORS[color_key]
    ax.set_facecolor("#FAFAFA")
    ax.axhline(0, color="#888", linewidth=0.8, linestyle="--", zorder=1)

    if not x_mm or not values:
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                transform=ax.transAxes, fontsize=8, color="#999")
        ax.set_ylabel(ylabel, fontsize=7)
        ax.tick_params(axis="both", labelsize=7)
        return 0.0

    # Smooth deflection curves (beams only; columns use straight segments)
    if color_key == "Defl" and len(x_mm) >= 3 and smooth:
        x_plot, v_plot = _cubic_spline_resample(x_mm, values, 51)
    else:
        x_plot, v_plot = x_mm, values

    ax.plot(x_plot, v_plot, color=line_col, linewidth=1.8, zorder=3)
    ax.fill_between(x_plot, 0, v_plot, color=fill_col, alpha=0.55, zorder=2)

    v_max = max(values, key=abs)
    if abs(v_max) > 1e-9:
        idx_max = values.index(v_max)
        offset  = (6, 5) if v_max >= 0 else (6, -14)
        ax.annotate("{:.3f}".format(v_max),
                    xy=(x_mm[idx_max], v_max),
                    xytext=offset, textcoords="offset points",
                    fontsize=7, color=line_col,
                    arrowprops=dict(arrowstyle="-", color=line_col, lw=0.4))

    ax.set_ylabel(ylabel, fontsize=7)
    ax.set_xlabel("x (mm)", fontsize=7)
    ax.tick_params(axis="both", labelsize=7)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.2f"))
    ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.5)
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)

    # SAP2000 convention: positive moment/deflection plotted downward
    if color_key in ("BMD", "Defl"):
        ax.invert_yaxis()

    return v_max


# ===================================================================
# DIAGRAM PANEL WIDGET
# ===================================================================

def _interp(x, xp, fp):
    """Simple linear interpolation (no numpy needed)."""
    if not xp or not fp or x <= xp[0]:
        return fp[0] if fp else 0.0
    if x >= xp[-1]:
        return fp[-1]
    for i in range(len(xp) - 1):
        if xp[i] <= x <= xp[i + 1]:
            dx = xp[i + 1] - xp[i]
            if abs(dx) < 1e-12:
                return fp[i]
            t = (x - xp[i]) / dx
            return fp[i] + t * (fp[i + 1] - fp[i])
    return fp[-1]


class DiagramPanel(QFrame):
    """
    One diagram panel: [Title label | Matplotlib canvas | Max-value info].
    Laid out horizontally inside a fixed-height frame.
    Interactive crosshair shows value at cursor position.
    """

    def __init__(self, color_key, parent=None):
        super().__init__(parent)
        self.color_key = color_key
        self._x_data = []
        self._y_data = []
        self._ylabel = ""
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "QFrame { background:white; border-bottom:1px solid #E0E0E0; }"
        )
        self.setFixedHeight(210)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(8)

        # --- Left: diagram title ---
        self.lbl_title = QLabel("")
        self.lbl_title.setFixedWidth(100)
        self.lbl_title.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.lbl_title.setWordWrap(True)
        f = QFont(); f.setBold(True); f.setPointSize(9)
        self.lbl_title.setFont(f)
        self.lbl_title.setStyleSheet(
            "color: {}; background: transparent; padding-top:4px;".format(
                _DIAGRAM_COLORS[color_key][0]))
        outer.addWidget(self.lbl_title)

        # --- Center: matplotlib canvas ---
        self.figure = Figure(figsize=(5.5, 1.8), dpi=96)
        self.figure.set_facecolor("white")
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self.canvas, stretch=1)

        # --- Right: max value info ---
        self.lbl_info = QLabel("")
        self.lbl_info.setFixedWidth(130)
        self.lbl_info.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet(
            "font-size:8pt; color:#333; background:transparent; padding-top:4px;")
        outer.addWidget(self.lbl_info)

        # --- Crosshair artists (created once, updated on mouse move) ---
        self._crosshair_line = None
        self._crosshair_dot = None
        self._crosshair_ann = None
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        self.canvas.mpl_connect("axes_leave_event", self._on_mouse_leave)

    def update(self, title, x_mm, values, ylabel, smooth=True, extra_info=""):
        self.lbl_title.setText(title)
        # For deflection panels, store smoothed data so crosshair follows the curve
        if self.color_key == "Defl" and len(x_mm) >= 3 and smooth:
            xs, ys = _cubic_spline_resample(x_mm, values, 51)
            self._x_data = xs
            self._y_data = ys
        else:
            self._x_data = list(x_mm)
            self._y_data = list(values)
        self._ylabel = ylabel

        self.figure.clear()
        ax = self.figure.add_subplot(1, 1, 1)
        v_max = draw_diagram(ax, x_mm, values, ylabel, self.color_key, smooth)
        self.figure.subplots_adjust(left=0.14, right=0.97, top=0.92, bottom=0.22)

        # Pre-create crosshair artists (hidden)
        line_col = _DIAGRAM_COLORS[self.color_key][0]
        self._crosshair_line = ax.axvline(0, color="#999", linewidth=0.7,
                                          linestyle=":", visible=False, zorder=5)
        self._crosshair_dot, = ax.plot([], [], "o", color=line_col,
                                       markersize=5, visible=False, zorder=6)
        self._crosshair_ann = ax.annotate(
            "", xy=(0, 0), xytext=(10, 10), textcoords="offset points",
            fontsize=7, color="#333", visible=False, zorder=7,
            bbox=dict(boxstyle="round,pad=0.3", fc="#FFFFFFDD", ec="#CCC",
                      lw=0.6))

        self.canvas.draw()

        if abs(v_max) > 1e-9:
            self.lbl_info.setStyleSheet(
                "font-size:8pt; color:{}; font-weight:bold; "
                "background:transparent; padding-top:4px;".format(line_col))
            text = "Max:\n{:.3f}\n{}".format(v_max, ylabel)
            if extra_info:
                text += "\n" + extra_info
            self.lbl_info.setText(text)
        else:
            self.lbl_info.setStyleSheet(
                "font-size:8pt; color:#999; background:transparent; padding-top:4px;")
            self.lbl_info.setText("Max:\n\u2014")

    # --- Interactive crosshair -------------------------------------------

    def _on_mouse_move(self, event):
        if (event.inaxes is None or not self._x_data
                or self._crosshair_line is None):
            self._hide_crosshair()
            return

        x = event.xdata
        if x is None or x < self._x_data[0] or x > self._x_data[-1]:
            self._hide_crosshair()
            return

        y = _interp(x, self._x_data, self._y_data)

        self._crosshair_line.set_xdata([x, x])
        self._crosshair_line.set_visible(True)

        self._crosshair_dot.set_data([x], [y])
        self._crosshair_dot.set_visible(True)

        label = "x = {:.0f} mm\n{} = {:.3f}".format(x, self._ylabel, y)
        self._crosshair_ann.set_text(label)
        self._crosshair_ann.xy = (x, y)
        # Flip annotation side near right edge
        ax = event.inaxes
        x_mid = (ax.get_xlim()[0] + ax.get_xlim()[1]) / 2
        offset_x = -80 if x > x_mid else 10
        self._crosshair_ann.xyann = (offset_x, 10)
        self._crosshair_ann.set_visible(True)

        self.canvas.draw_idle()

    def _on_mouse_leave(self, event):
        self._hide_crosshair()

    def _hide_crosshair(self):
        changed = False
        if self._crosshair_line is not None and self._crosshair_line.get_visible():
            self._crosshair_line.set_visible(False)
            changed = True
        if self._crosshair_dot is not None and self._crosshair_dot.get_visible():
            self._crosshair_dot.set_visible(False)
            changed = True
        if self._crosshair_ann is not None and self._crosshair_ann.get_visible():
            self._crosshair_ann.set_visible(False)
            changed = True
        if changed:
            self.canvas.draw_idle()


# ===================================================================
# MAIN WINDOW
# ===================================================================

class VisualizerWindow(QMainWindow):
    """ROIDA Force Diagram Viewer — portrait layout, scrollable diagrams."""

    def __init__(self, model_elems, analysis_results, design_lookup, selected_ids,
                 sentinel_path=None, response_path=None):
        super().__init__()
        self.model_elems   = model_elems
        self.analysis      = analysis_results
        self.design_lookup = design_lookup
        self.elem_index    = build_element_index(model_elems)
        self.load_cases    = get_load_cases(analysis_results)
        self.avail_ids     = self._resolve_ids(selected_ids)
        self.sentinel_path = sentinel_path
        self.response_path = response_path

        self.cur_eid  = self.avail_ids[0] if self.avail_ids else None
        self.cur_lc   = self.load_cases[0] if self.load_cases else ""
        self.cur_axis = "Major"

        # QTimer: polling response file setiap 400ms
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._check_response)

        self.setWindowTitle("ROIDA — Element Forces Viewer")
        self.setFixedSize(820, 920)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowMaximizeButtonHint)
        self._build_ui()
        self._refresh_header()
        self._refresh_plots()

    # ------------------------------------------------------------------
    def _resolve_ids(self, selected_ids):
        analysis_ids = set()
        for lc_data in self.analysis.values():
            if not isinstance(lc_data, dict):
                continue
            for k in lc_data.get("elements", {}):
                try:
                    analysis_ids.add(int(k))
                except ValueError:
                    pass

        resolved = []
        for sid in selected_ids:
            if sid in analysis_ids:
                resolved.append(sid)
            else:
                sid_str = str(sid)
                for aid in sorted(analysis_ids):
                    if str(aid).startswith(sid_str) and aid not in resolved:
                        resolved.append(aid)

        if not resolved:
            resolved = sorted(list(analysis_ids))[:20]
        return resolved

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(0)
        vbox.setContentsMargins(0, 0, 0, 0)

        # ── Header bar ──────────────────────────────────────────────
        self.lbl_header = QLabel("—")
        self.lbl_header.setAlignment(Qt.AlignCenter)
        hf = QFont(); hf.setBold(True); hf.setPointSize(10)
        self.lbl_header.setFont(hf)
        self.lbl_header.setStyleSheet(
            "background:#0D47A1; color:white; padding:8px 12px; min-height:36px;")
        vbox.addWidget(self.lbl_header)

        # ── Element info bar ────────────────────────────────────────
        self.lbl_elem_info = QLabel("")
        self.lbl_elem_info.setAlignment(Qt.AlignCenter)
        self.lbl_elem_info.setStyleSheet(
            "background:#E3F2FD; color:#0D47A1; padding:4px 12px; font-size:9pt;")
        vbox.addWidget(self.lbl_elem_info)

        # ── Controls (2 baris agar tidak tindih) ────────────────────
        ctrl_w = QWidget()
        ctrl_w.setStyleSheet(
            "background:#F5F5F5; border-bottom:1px solid #BDBDBD;")
        ctrl_vbox = QVBoxLayout(ctrl_w)
        ctrl_vbox.setContentsMargins(10, 6, 10, 6)
        ctrl_vbox.setSpacing(4)

        # Baris 1: Elemen dropdown (full width)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        if len(self.avail_ids) > 1:
            row1.addWidget(_mk_label("Elemen:"))
            self.cmb_elem = QComboBox()
            self.cmb_elem.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            for eid in self.avail_ids:
                self.cmb_elem.addItem(self._elem_display(eid), eid)
            self.cmb_elem.currentIndexChanged.connect(self._on_elem_change)
            row1.addWidget(self.cmb_elem, stretch=1)
        else:
            self.cmb_elem = None
            row1.addStretch()
        ctrl_vbox.addLayout(row1)

        # Baris 2: Load Case + Sumbu
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(_mk_label("Load Case:"))
        self.cmb_lc = QComboBox()
        self.cmb_lc.setMinimumWidth(160)
        self.cmb_lc.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for lc in self.load_cases:
            self.cmb_lc.addItem(lc)
        self.cmb_lc.currentTextChanged.connect(self._on_lc_change)
        row2.addWidget(self.cmb_lc, stretch=1)

        row2.addSpacing(12)

        # Axis toggle
        self.rb_major = QRadioButton("Major")
        self.rb_minor = QRadioButton("Minor")
        self.rb_major.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self.rb_major)
        bg.addButton(self.rb_minor)
        self.rb_major.toggled.connect(self._on_axis_toggle)
        row2.addWidget(_mk_label("Sumbu:"))
        row2.addWidget(self.rb_major)
        row2.addWidget(self.rb_minor)
        ctrl_vbox.addLayout(row2)

        vbox.addWidget(ctrl_w)

        # ── Scrollable diagram area ─────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background:white;")

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background:white;")
        sc_vbox = QVBoxLayout(scroll_content)
        sc_vbox.setSpacing(0)
        sc_vbox.setContentsMargins(0, 0, 0, 0)

        self.panel_sfd  = DiagramPanel("SFD")
        self.panel_bmd  = DiagramPanel("BMD")
        self.panel_nfd  = DiagramPanel("NFD")
        self.panel_defl = DiagramPanel("Defl")

        sc_vbox.addWidget(self.panel_sfd)
        sc_vbox.addWidget(self.panel_bmd)
        sc_vbox.addWidget(self.panel_nfd)
        sc_vbox.addWidget(self.panel_defl)
        sc_vbox.addStretch()

        scroll.setWidget(scroll_content)
        vbox.addWidget(scroll, stretch=1)

        # ── DCR bar ─────────────────────────────────────────────────
        self.lbl_dcr = QLabel("")
        self.lbl_dcr.setAlignment(Qt.AlignCenter)
        self.lbl_dcr.setWordWrap(True)
        self.lbl_dcr.setStyleSheet(
            "padding:6px 12px; background:#F5F5F5; "
            "border-top:1px solid #BDBDBD; font-size:9pt;")
        vbox.addWidget(self.lbl_dcr)

        # ── Bottom button bar ────────────────────────────────────────
        btn_w = QWidget()
        btn_w.setStyleSheet(
            "background:#EEEEEE; border-top:1px solid #BDBDBD;")
        btn_layout = QHBoxLayout(btn_w)
        btn_layout.setContentsMargins(12, 8, 12, 8)
        btn_layout.setSpacing(10)

        self.btn_next = QPushButton("\U0001f504  Select Next Element")
        self.btn_next.setFixedHeight(34)
        self.btn_next.setCursor(Qt.PointingHandCursor)
        self.btn_next.setStyleSheet(
            "QPushButton { background:#1565C0; color:white; font-weight:bold;"
            "  font-size:9pt; border-radius:4px; padding:0 16px; }"
            "QPushButton:hover { background:#0D47A1; }"
            "QPushButton:pressed { background:#0A3A7E; }")
        self.btn_next.clicked.connect(self._on_select_next)
        btn_layout.addWidget(self.btn_next)

        btn_layout.addStretch()

        btn_png = QPushButton("Export PNG")
        btn_png.setFixedHeight(34)
        btn_png.setFixedWidth(110)
        btn_png.setCursor(Qt.PointingHandCursor)
        btn_png.setStyleSheet(
            "QPushButton { font-size:9pt; border-radius:4px; padding:0 12px;"
            "  background:#E0E0E0; color:#222; }"
            "QPushButton:hover { background:#BDBDBD; }"
            "QPushButton:pressed { background:#9E9E9E; }")
        btn_png.clicked.connect(self._on_export_png)
        btn_layout.addWidget(btn_png)

        vbox.addWidget(btn_w)

    # ------------------------------------------------------------------
    # Event handlers

    def _on_elem_change(self, idx):
        self.cur_eid = self.cmb_elem.itemData(idx)
        self._refresh_header()
        self._refresh_plots()

    def _on_lc_change(self, text):
        self.cur_lc = text
        self._refresh_plots()

    def _on_axis_toggle(self):
        self.cur_axis = "Major" if self.rb_major.isChecked() else "Minor"
        self._refresh_plots()

    def _on_export_png(self):
        """Export all 4 diagrams combined into one PNG."""
        default = "diagram_{}_{}_{}.png".format(
            self.cur_eid, self.cur_lc, self.cur_axis)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PNG", default, "PNG Image (*.png)")
        if not path:
            return

        # Render all 4 panels into one combined figure
        fig, axes = __import__("matplotlib.pyplot", fromlist=["subplots"]).subplots(
            4, 1, figsize=(9, 10))

        eid = self.cur_eid
        lc  = self.cur_lc
        if eid is None or not lc:
            return

        eid_str = str(eid)
        L_mm    = get_element_length(self.analysis, eid_str) or 1.0
        axis    = self.cur_axis
        me      = self.elem_index.get(eid, {})
        lbl     = me.get("label_name", me.get("frame_label", str(eid)))

        local_axes = me.get("local_axes", {})
        shear_key, moment_key, defl_key, defl_prefix = get_force_keys(local_axes, axis)

        stations = get_station_data(self.analysis, lc, eid_str)
        x_mm, shear, moment, axial = (
            extract_forces(stations, shear_key, moment_key) if stations
            else ([], [], [], []))
        dp   = get_deflection_profile(self.analysis, lc, eid_str)
        md   = get_max_deflection(self.analysis, lc, eid_str)
        xd, d, _chord = extract_deflection(dp, md, L_mm, defl_key, defl_prefix)

        # SAP2000 convention: direction note before abs(), then always positive
        raw_max = max(d, key=abs) if d else 0.0
        if abs(raw_max) > 1e-9:
            sign_str = "-" if raw_max < 0 else "+"
            dir_note = "Positive in {}{} direction".format(sign_str, defl_key[1])
        else:
            dir_note = ""
        d = [abs(v) for v in d]
        is_column = get_element_type(self.analysis, eid_str) == "Column"

        # Column: triangular tent using max_deflection from Result.json
        if is_column and md:
            d_peak = abs(md.get(defl_prefix + "_max_mm", 0.0))
            d_x    = md.get(defl_prefix + "_distance_mm", L_mm / 2.0)
            xd = [0.0, d_x, L_mm]
            d  = [0.0, d_peak, 0.0]

        s_tag = "(Major)" if axis == "Major" else "(Minor)"
        s_ylabel = "{} (kN)".format(shear_key)
        m_ylabel = "{} (kN\u00b7m)".format(moment_key)
        d_ylabel = "\u03b4{} (mm)".format(defl_key[1])

        draw_diagram(axes[0], x_mm,  shear,  s_ylabel, "SFD")
        draw_diagram(axes[1], x_mm,  moment, m_ylabel, "BMD")
        draw_diagram(axes[2], x_mm,  axial,  "P (kN)",  "NFD")
        draw_diagram(axes[3], xd,    d,      d_ylabel,  "Defl", smooth=not is_column)
        axes[0].set_title("SFD {}".format(s_tag), fontsize=9, fontweight="bold")
        axes[1].set_title("BMD {}".format(s_tag), fontsize=9, fontweight="bold")
        axes[2].set_title("NFD (Axial)", fontsize=9, fontweight="bold")
        axes[3].set_title("Deflection {}".format(s_tag), fontsize=9, fontweight="bold")
        if dir_note:
            axes[3].text(0.98, 0.02, dir_note, transform=axes[3].transAxes,
                         fontsize=7, ha="right", va="bottom", color="#666",
                         fontstyle="italic")

        fig.suptitle("{} — {}   [{}]".format(lbl, lc, axis),
                     fontsize=11, fontweight="bold")
        fig.tight_layout(rect=[0, 0, 1, 0.96])
        fig.savefig(path, dpi=150, bbox_inches="tight")
        __import__("matplotlib.pyplot", fromlist=["close"]).close(fig)

    def _on_select_next(self):
        """Tulis sentinel → bawa Revit ke depan → mulai polling response (window tetap terbuka)."""
        # Tulis sentinel agar Revit Idling handler tahu harus PickObjects
        if self.sentinel_path:
            try:
                with open(self.sentinel_path, "w") as f:
                    f.write("next")
            except Exception:
                pass

        # Tampilkan status "menunggu pilihan" dan minimize ke taskbar
        self.lbl_header.setText("⏳  Pilih elemen di Revit, lalu tekan Enter...")
        self.lbl_header.setStyleSheet(
            "background:#E65100; color:white; padding:8px 12px; min-height:36px;")
        self.btn_next.setEnabled(False)
        self.showMinimized()   # minimize ke taskbar (tidak hilang, tetap ada)

        # Bawa Revit ke foreground
        try:
            import ctypes, ctypes.wintypes
            found = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _cb(hwnd, _):
                if ctypes.windll.user32.IsWindowVisible(hwnd):
                    n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
                        if "Revit" in buf.value:
                            found.append(hwnd)
                return True
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
            if found:
                ctypes.windll.user32.ShowWindow(found[0], 9)
                ctypes.windll.user32.SetForegroundWindow(found[0])
        except Exception:
            pass

        # Mulai polling response file setiap 400ms
        if self.response_path:
            self._poll_timer.start(400)

    def _check_response(self):
        """Dipanggil QTimer — cek apakah response file sudah ada."""
        if not self.response_path or not os.path.exists(self.response_path):
            return

        self._poll_timer.stop()

        # Baca response
        try:
            with open(self.response_path, "r") as f:
                data = json.load(f)
            os.remove(self.response_path)
        except Exception:
            self._restore_header()
            return

        new_ids_raw = data.get("element_ids", [])
        if not new_ids_raw:
            # Dibatalkan (Escape) — kembalikan ke kondisi semula
            self._restore_header()
            return

        # Resolve IDs
        new_ids = self._resolve_ids([int(x) for x in new_ids_raw])
        if not new_ids:
            self._restore_header()
            return

        # Update state
        self.avail_ids = new_ids
        self.cur_eid   = new_ids[0]

        # Update dropdown elemen jika ada
        if self.cmb_elem is not None:
            self.cmb_elem.blockSignals(True)
            self.cmb_elem.clear()
            for eid in self.avail_ids:
                self.cmb_elem.addItem(self._elem_display(eid), eid)
            self.cmb_elem.blockSignals(False)

        # Restore window dan refresh diagram
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self._restore_header()
        self._refresh_header()
        self._refresh_plots()

    def _restore_header(self):
        """Kembalikan warna header biru dan aktifkan tombol Next Element."""
        self.lbl_header.setStyleSheet(
            "background:#0D47A1; color:white; padding:8px 12px; min-height:36px;")
        self.btn_next.setEnabled(True)

    # ------------------------------------------------------------------
    # Helpers

    def _elem_display(self, eid):
        me  = self.elem_index.get(eid, {})
        lbl = me.get("label_name", me.get("frame_label", str(eid)))
        sec = _short_fam(me.get("family", ""))
        return "{} — {}".format(lbl, sec) if sec else lbl

    def _refresh_header(self):
        eid = self.cur_eid
        if eid is None:
            self.lbl_header.setText("Tidak ada elemen dipilih")
            self.lbl_elem_info.setText("")
            self.lbl_dcr.setText("")
            return

        me    = self.elem_index.get(eid, {})
        lbl   = me.get("label_name", me.get("frame_label", str(eid)))
        sec   = _short_fam(me.get("family", ""))
        etype = get_element_type(self.analysis, str(eid))
        L_mm  = get_element_length(self.analysis, str(eid))

        self.lbl_header.setText(
            "{lbl}    |    {sec}    |    {etype}    |    L = {L} mm".format(
                lbl=lbl, sec=sec, etype=etype, L=L_mm))
        self.lbl_elem_info.setText(
            "ID: {}{}".format(
                eid,
                "    (Pilih elemen di Revit → klik Visualize untuk ganti)" if self.cmb_elem is None else ""))

        # DCR badge
        dr = self.design_lookup.get(eid, {})
        if dr:
            dcr     = dr.get("governing_ratio", 0.0)
            status  = dr.get("status", "?")
            combo   = dr.get("governing_combo", "—")
            eq      = dr.get("governing_equation", "")
            pr_pc   = dr.get("pmm_detail", {}).get("Pr_Pc", None)
            mr_maj  = dr.get("pmm_detail", {}).get("MrMaj_Mc", None)
            color   = "#B71C1C" if status != "OK" else "#1B5E20"

            detail = ""
            if pr_pc is not None:
                detail += "  Pr/Pc={:.3f}".format(pr_pc)
            if mr_maj is not None:
                detail += "  MrMaj/Mc={:.3f}".format(mr_maj)

            self.lbl_dcr.setStyleSheet(
                "padding:6px 12px; background:#F5F5F5; "
                "border-top:1px solid #BDBDBD; font-size:9pt; "
                "color:{};".format(color))
            self.lbl_dcr.setText(
                "AISC 360-22 LRFD  |  DCR = {:.3f}  |  Status: {}  |  "
                "Combo: {}  |  Eq: {}{}".format(
                    dcr, status, combo, eq, detail))
        else:
            self.lbl_dcr.setStyleSheet(
                "padding:6px 12px; background:#F5F5F5; "
                "border-top:1px solid #BDBDBD; font-size:9pt; color:#888;")
            self.lbl_dcr.setText("Design Result tidak tersedia — jalankan Design Check untuk melihat DCR")

    def _refresh_plots(self):
        eid = self.cur_eid
        lc  = self.cur_lc
        axis = self.cur_axis
        s_tag = "(Major)" if axis == "Major" else "(Minor)"

        if eid is None or not lc:
            for p in (self.panel_sfd, self.panel_bmd, self.panel_nfd, self.panel_defl):
                p.update("—", [], [], "—")
            return

        eid_str = str(eid)
        L_mm    = get_element_length(self.analysis, eid_str) or 1.0

        # Determine force keys from element local axes
        me = self.elem_index.get(eid, {})
        local_axes = me.get("local_axes", {})
        shear_key, moment_key, defl_key, defl_prefix = get_force_keys(local_axes, axis)

        stations = get_station_data(self.analysis, lc, eid_str)
        x_mm, shear, moment, axial = (
            extract_forces(stations, shear_key, moment_key) if stations
            else ([], [], [], [])
        )

        dp   = get_deflection_profile(self.analysis, lc, eid_str)
        md   = get_max_deflection(self.analysis, lc, eid_str)
        xd, d, _chord = extract_deflection(dp, md, L_mm, defl_key, defl_prefix)

        # SAP2000 convention: direction note before abs(), then always positive
        raw_max = max(d, key=abs) if d else 0.0
        if abs(raw_max) > 1e-9:
            sign_str = "-" if raw_max < 0 else "+"
            dir_note = "Positive in {}{} dir".format(sign_str, defl_key[1])
        else:
            dir_note = ""
        d = [abs(v) for v in d]
        is_column = get_element_type(self.analysis, eid_str) == "Column"

        # Column: triangular tent using max_deflection from Result.json
        if is_column and md:
            d_peak = abs(md.get(defl_prefix + "_max_mm", 0.0))
            d_x    = md.get(defl_prefix + "_distance_mm", L_mm / 2.0)
            xd = [0.0, d_x, L_mm]
            d  = [0.0, d_peak, 0.0]

        s_ylabel = "{} (kN)".format(shear_key)
        m_ylabel = "{} (kN·m)".format(moment_key)
        d_ylabel = "\u03b4{} (mm)".format(defl_key[1])

        self.panel_sfd.update("SFD\n{}".format(s_tag), x_mm,  shear,  s_ylabel)
        self.panel_bmd.update("BMD\n{}".format(s_tag), x_mm,  moment, m_ylabel)
        self.panel_nfd.update("NFD\n(Axial)",           x_mm,  axial,  "P (kN)")
        self.panel_defl.update("Deflection\n{}".format(s_tag), xd, d, d_ylabel,
                               smooth=not is_column, extra_info=dir_note)

        # Update window subtitle
        lbl = me.get("label_name", me.get("frame_label", str(eid)))
        self.setWindowTitle("ROIDA — {} — {} [{}]".format(lbl, lc, axis))


# ===================================================================
# HELPERS
# ===================================================================

def _short_fam(family_str):
    if not family_str:
        return ""
    return family_str.split(":")[-1].strip() if ":" in family_str else family_str.strip()


def _mk_label(text):
    lbl = QLabel(text)
    lbl.setStyleSheet("font-size:9pt; background:transparent;")
    return lbl


# ===================================================================
# ENTRY POINT
# ===================================================================

def main():
    parser = argparse.ArgumentParser(description="ROIDA Visualizer")
    parser.add_argument("--result",   required=True)
    parser.add_argument("--design",   default=None)
    parser.add_argument("--elements", default="[]")
    parser.add_argument("--sentinel", default=None,
                        help="Path ke sentinel file (ditulis Visualizer → dibaca Revit)")
    parser.add_argument("--response", default=None,
                        help="Path ke response file (ditulis Revit → dibaca Visualizer)")
    args = parser.parse_args()

    if not os.path.exists(args.result):
        print("ERROR: Result.json tidak ditemukan: {}".format(args.result))
        sys.exit(1)

    model_elems, analysis_results = load_result(args.result)
    design_lookup                 = load_design(args.design)

    try:
        selected_ids = [int(x) for x in json.loads(args.elements)]
    except Exception:
        selected_ids = []

    app    = QApplication.instance() or QApplication(sys.argv)
    window = VisualizerWindow(model_elems, analysis_results, design_lookup,
                              selected_ids,
                              sentinel_path=args.sentinel,
                              response_path=args.response)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
