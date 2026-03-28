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
    QDialog, QTextEdit,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QCursor

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


def _is_internal_key(k):
    """Keys that are internal helpers (not user-facing load cases)."""
    return k.startswith("_") or k in _PRIVATE_KEYS or k.startswith("LIVE_ZONE_")


def get_load_cases(analysis_results):
    """Return load cases in logical order: patterns first, then seismic, then combos."""
    _ORDER = [
        "SelfWeight", "ADL", "LIVE",
        "SeismicX", "SeismicY",
    ]
    keys = [k for k in analysis_results if not _is_internal_key(k)]
    def _sort_key(k):
        try:
            return (0, _ORDER.index(k))
        except ValueError:
            return (1, k)
    return sorted(keys, key=_sort_key)


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
    """Draw one force/deflection diagram on the given Axes.

    Returns (v_max, x_at_max) — the extreme value and its position in mm.
    """
    line_col, fill_col = _DIAGRAM_COLORS[color_key]
    ax.set_facecolor("#FAFAFA")
    ax.axhline(0, color="#888", linewidth=0.8, linestyle="--", zorder=1)

    if not x_mm or not values:
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                transform=ax.transAxes, fontsize=8, color="#999")
        ax.set_ylabel(ylabel, fontsize=7)
        ax.tick_params(axis="both", labelsize=7)
        return 0.0, 0.0

    # Smooth deflection curves
    if color_key == "Defl" and len(x_mm) >= 3 and smooth:
        x_plot, v_plot = _cubic_spline_resample(x_mm, values, 51)
    else:
        x_plot, v_plot = x_mm, values

    ax.plot(x_plot, v_plot, color=line_col, linewidth=1.8, zorder=3)
    ax.fill_between(x_plot, 0, v_plot, color=fill_col, alpha=0.55, zorder=2)

    # When multiple stations share the same max, pick the last one (end of span)
    idx_max = max(range(len(values)), key=lambda i: (abs(values[i]), i))
    v_max = values[idx_max]
    x_at_max = x_mm[idx_max]
    if abs(v_max) > 1e-9:
        offset  = (6, 5) if v_max >= 0 else (6, -14)
        ax.annotate("{:.3f}".format(v_max),
                    xy=(x_at_max, v_max),
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

    return v_max, x_at_max


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
        v_max, x_at_max = draw_diagram(ax, x_mm, values, ylabel, self.color_key, smooth)
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
            text = "Max:\n{:.3f}\n{}\nat {:.0f} mm".format(v_max, ylabel, x_at_max)
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
# CLICKABLE LABEL
# ===================================================================

class ClickableLabel(QLabel):
    """QLabel that emits *clicked* on mouse press."""
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


# ===================================================================
# SECTION DRAWING HELPER
# ===================================================================

def _draw_section(fig, d, bf, tf, tw, sec_name=""):
    """Draw an I-section cross-section on *fig* with dimension annotations.

    Style follows SAP2000 section illustration: dashed centre axes,
    tf bracket at left flange edge, tw arrows at web centre, hw on left,
    d on right, bf at bottom.
    """
    from matplotlib.patches import Rectangle

    fig.clear()
    ax = fig.add_axes([0.08, 0.06, 0.84, 0.86])
    ax.set_aspect("equal")
    ax.axis("off")

    if d <= 0 or bf <= 0 or tf <= 0 or tw <= 0:
        ax.text(0.5, 0.5, "No section\ndata", ha="center", va="center",
                fontsize=10, color="#999")
        if sec_name:
            fig.suptitle(sec_name, fontsize=9, fontweight="bold", y=0.97)
        return

    hw = d - 2 * tf  # clear web height
    fill_color = "#B0C4DE"
    edge_color = "#333"
    lw = 1.2

    # --- Draw I-section ---
    ax.add_patch(Rectangle((0, 0), bf, tf, fc=fill_color, ec=edge_color, lw=lw))
    ax.add_patch(Rectangle(((bf - tw) / 2, tf), tw, hw,
                           fc=fill_color, ec=edge_color, lw=lw))
    ax.add_patch(Rectangle((0, tf + hw), bf, tf,
                           fc=fill_color, ec=edge_color, lw=lw))

    pad = d * 0.14
    ax.set_xlim(-pad * 3.0, bf + pad * 3.0)
    ax.set_ylim(-pad * 1.8, d + pad * 1.5)

    # --- Dashed centre axes (x, y) ---
    cx, cy = bf / 2, d / 2
    ax.plot([cx, cx], [-pad * 0.8, d + pad * 0.8],
            ls="--", lw=0.7, color="#666", zorder=0)
    ax.plot([-pad * 0.8, bf + pad * 0.8], [cy, cy],
            ls="--", lw=0.7, color="#666", zorder=0)
    ax.text(cx + pad * 0.2, d + pad * 1.0, "x", fontsize=7,
            color="#666", ha="center", style="italic")
    ax.text(bf + pad * 1.2, cy + pad * 0.2, "y", fontsize=7,
            color="#666", va="center", style="italic")

    ann_kw = dict(fontsize=7, ha="center", va="center",
                  color="#0D47A1", fontweight="bold")
    arr_kw = dict(arrowstyle="<->", color="#0D47A1", lw=0.9,
                  shrinkA=0, shrinkB=0)

    # --- d — total depth (right side) ---
    xd = bf + pad * 1.5
    ax.annotate("", xy=(xd, 0), xytext=(xd, d), arrowprops=arr_kw)
    ax.text(xd + pad * 0.7, d / 2,
            "d={:.1f}".format(d), rotation=90, **ann_kw)

    # --- bf — flange width (bottom) ---
    ybf = -pad * 1.0
    ax.annotate("", xy=(0, ybf), xytext=(bf, ybf), arrowprops=arr_kw)
    ax.text(bf / 2, ybf - pad * 0.5,
            "bf={:.1f}".format(bf), **ann_kw)

    # --- hw — clear web height (left side) ---
    xhw = -pad * 1.5
    ax.annotate("", xy=(xhw, tf), xytext=(xhw, tf + hw), arrowprops=arr_kw)
    ax.text(xhw - pad * 0.7, tf + hw / 2,
            "hw={:.1f}".format(hw), rotation=90, **ann_kw)

    # --- tf — flange thickness (left edge, top flange, bracket style) ---
    xtf = -pad * 0.15
    y_tf_bot = tf + hw
    y_tf_top = d
    # small horizontal ticks to form bracket
    tick = pad * 0.35
    ax.plot([xtf - tick, xtf + tick], [y_tf_bot, y_tf_bot],
            lw=0.9, color="#0D47A1")
    ax.plot([xtf - tick, xtf + tick], [y_tf_top, y_tf_top],
            lw=0.9, color="#0D47A1")
    ax.annotate("", xy=(xtf, y_tf_bot), xytext=(xtf, y_tf_top),
                arrowprops=arr_kw)
    ax.text(xtf - pad * 0.9, y_tf_bot + tf / 2,
            "tf={:.1f}".format(tf), fontsize=6.5, rotation=90,
            ha="center", va="center", color="#0D47A1", fontweight="bold")

    # --- tw — web thickness (at centroid, with leader lines) ---
    y_tw = cy
    x_wl = (bf - tw) / 2
    x_wr = (bf + tw) / 2
    # leader lines from web edges up/down for clarity
    ax.plot([x_wl, x_wl], [y_tw - pad * 0.3, y_tw + pad * 0.3],
            lw=0.7, color="#0D47A1")
    ax.plot([x_wr, x_wr], [y_tw - pad * 0.3, y_tw + pad * 0.3],
            lw=0.7, color="#0D47A1")
    ax.annotate("", xy=(x_wl, y_tw), xytext=(x_wr, y_tw),
                arrowprops=arr_kw)
    ax.text(cx, y_tw - pad * 0.65,
            "tw={:.1f}".format(tw), fontsize=6.5,
            ha="center", va="center", color="#0D47A1", fontweight="bold")

    if sec_name:
        fig.suptitle(sec_name, fontsize=9, fontweight="bold", y=0.97)


# ===================================================================
# STRESS CHECK REPORT BUILDER
# ===================================================================

def _v(val, dec=2):
    """Format a number for display."""
    if val is None or val == 0:
        return "0"
    if abs(val) >= 1e8:
        return "{:.3E}".format(val)
    if dec == 0:
        return "{:.0f}".format(val)
    return "{:.{d}f}".format(val, d=dec)


# ── Color palette for report sections ──────────────────────────────
_C = {
    "hdr":     "#0D47A1",   # header / general info (dark blue)
    "prop":    "#37474F",   # section properties (blue-grey)
    "msg_ok":  "#2E7D32",   # design message OK (green)
    "msg_ng":  "#C62828",   # design message error (red)
    "force":   "#E65100",   # forces & moments (deep orange)
    "pmm":     "#6A1B9A",   # PMM interaction (purple)
    "compact": "#4E342E",   # compactness (brown)
    "axial":   "#00695C",   # axial design (teal)
    "moment":  "#1565C0",   # moment design (blue)
    "shear":   "#BF360C",   # shear check (burnt orange)
    "seismic": "#880E4F",   # seismic checks (dark pink)
    "subtle":  "#757575",   # secondary text (grey)
}

_CSS = """
body { font-family: 'Segoe UI', Tahoma, sans-serif; font-size: 10pt;
       margin: 0 3px; padding: 0; background: #FAFAFA; color: #212121; }
h2   { margin: 8px 0 2px 0; padding: 2px 5px; font-size: 10.5pt;
       border-left: 4px solid; }
table { border-collapse: collapse; margin: 1px 0 5px 0; font-size: 9.5pt; }
td, th { padding: 1px 6px 1px 3px; text-align: right; white-space: nowrap;
         vertical-align: middle; }
th    { font-weight: 600; border-bottom: 1.5px solid #BDBDBD; }
td.lbl { text-align: left; font-weight: 500; white-space: nowrap; width: 1%; }
td.lbl2 { text-align: left; color: #757575; white-space: nowrap; width: 1%; }
.kv  { font-family: Consolas, monospace; font-size: 9pt;
       margin: 0 0 1px 5px; }
.tag-ok { color: #2E7D32; font-weight: 700; }
.tag-ng { color: #C62828; font-weight: 700; }
.eq  { font-family: Consolas, monospace; font-size: 9pt;
       color: #6A1B9A; margin: 0 0 0 8px; }
.sub { color: #757575; font-size: 9pt; }
p    { margin: 1px 0; }
"""


def _h2(title, color):
    return '<h2 style="color:{c}; border-color:{c};">{t}</h2>'.format(
        c=color, t=title)


def _row(*cells, cls=None):
    """Build a <tr> from cells.  Odd cells (0, 2, …) get class *cls*."""
    parts = []
    for i, c in enumerate(cells):
        tag_cls = ' class="{}"'.format(cls) if (cls and i % 2 == 0) else ""
        parts.append("<td{}>{}</td>".format(tag_cls, c))
    return "<tr>{}</tr>".format("".join(parts))


def _th(*cells):
    return "<tr>{}</tr>".format("".join("<th>{}</th>".format(c) for c in cells))


def _build_stress_check_report(dr, sec_data=None, sec_name=""):
    """Build HTML-formatted Steel Stress Check report.

    *dr*       — element dict from Design Result.json
    *sec_data* — section dict from Result.json (d_mm, b_mm, …)
    *sec_name* — short section name (e.g. "IWF303.4x165x6x10.2")
    """
    if sec_data is None:
        sec_data = {}

    cap = dr.get("capacity", {})
    pmm = dr.get("pmm_detail", {})
    shear = dr.get("shear_detail", {})
    smf = dr.get("smf_checks") or {}
    kf = dr.get("K_factors") or {}
    lengths = dr.get("lengths") or {}
    mat = dr.get("material") or {}
    stations = dr.get("station_details") or []
    C = _C

    h = []
    a = h.append

    a("<html><head><style>{}</style></head><body>".format(_CSS))

    # ── Title ──────────────────────────────────────────────────────
    a('<div style="text-align:center; margin:2px 0 4px 0;">')
    a('<b style="font-size:10.5pt; color:{};">ROIDA \u2014 Steel Section Check</b>'.format(
        C["hdr"]))
    a('<br><span class="sub">AISC 360-22 LRFD | kN, mm, MPa</span>')
    a("</div>")

    # ── General Info ───────────────────────────────────────────────
    label   = dr.get("label_name", dr.get("frame_label", "?"))
    shape   = sec_name or dr.get("design_section", "?")
    dtype   = dr.get("design_type", "?")
    status  = dr.get("status", "?")
    combo   = dr.get("governing_combo", "\u2014")
    loc_mm  = dr.get("governing_location_mm", 0)
    dcr     = dr.get("governing_ratio", 0)
    rtype   = dr.get("ratio_type", "PMM")
    group   = dr.get("group", "")
    L_val   = lengths.get("Lx_mm") or lengths.get("Ly_mm") or 0

    # Derive clean status label from raw status string
    is_overstressed = dcr > 1.0 or status.startswith("Overstressed")
    is_smf_ng = "SMF-NG" in status
    is_scwb_ng = "SCWB-NG" in status
    is_scwb = rtype.startswith("SCWB")
    if status == "OK":
        status_label = "OK"
        status_cls = "tag-ok"
    elif is_scwb_ng and is_smf_ng:
        status_label = "NG (SCWB + Seismic)"
        status_cls = "tag-ng"
    elif is_scwb_ng:
        status_label = "NG (SCWB)"
        status_cls = "tag-ng"
    elif is_overstressed and is_smf_ng:
        status_label = "NG (Strength + Seismic)"
        status_cls = "tag-ng"
    elif is_overstressed:
        status_label = "NG (Overstressed)"
        status_cls = "tag-ng"
    elif is_smf_ng:
        status_label = "NG (Seismic)"
        status_cls = "tag-ng"
    else:
        status_label = status
        status_cls = "tag-ng"

    a(_h2("General Information", C["hdr"]))
    a('<table width="100%">')
    a(_row("Frame", label, "Shape", shape, cls="lbl"))
    a(_row("Type", dtype,
           "Group", group if group else "\u2014", cls="lbl"))
    a(_row("Length", "{} mm".format(_v(L_val, 1)),
           "Gov. Combo", "<b>{}</b>".format(combo), cls="lbl"))
    a(_row("Location", "{} mm".format(_v(loc_mm, 1)),
           "Gov. By", rtype, cls="lbl"))
    a(_row("D/C Ratio", "<b>{}</b>".format(_v(dcr, 4)),
           "Status", '<span class="{}">{}</span>'.format(
               status_cls, status_label),
           cls="lbl"))
    a("</table>")

    # ── Material & Provision ───────────────────────────────────────
    Fy = mat.get("Fy_MPa", 0)
    Fu = mat.get("Fu_MPa", 0)
    E  = mat.get("E_MPa", 0)
    a(_h2("Material &amp; Provision", C["prop"]))
    a('<table width="100%">')
    a(_row("E", "{} MPa".format(_v(E, 0)),
           "\u03c6B (Bending)", "0.9", cls="lbl"))
    a(_row("Fy", "{} MPa".format(_v(Fy, 1)),
           "\u03c6C (Compression)", "0.9", cls="lbl"))
    a(_row("Fu", "{} MPa".format(_v(Fu, 1)),
           "\u03c6V (Shear)", "0.9", cls="lbl"))
    a(_row("", "",
           "\u03c6TY (Yield)", "0.9", cls="lbl"))
    a(_row("", "",
           "\u03c6TF (Fracture)", "0.75", cls="lbl"))
    a("</table>")

    # ── Section Properties ─────────────────────────────────────────
    a(_h2("Section Properties", C["prop"]))
    a('<table width="100%">')
    a(_th("Property", "Value", "Property", "Value"))
    props = [
        ("A",   _v(sec_data.get("Area_mm2", 0), 1) + " mm\u00b2",
         "J",   _v(sec_data.get("J_mm4", 0), 1) + " mm\u2074"),
        ("I33", _v(sec_data.get("Iz_mm4", 0), 1) + " mm\u2074",
         "I22", _v(sec_data.get("Iy_mm4", 0), 1) + " mm\u2074"),
        ("S33", _v(sec_data.get("Sz_mm3", 0), 1) + " mm\u00b3",
         "S22", _v(sec_data.get("Sy_mm3", 0), 1) + " mm\u00b3"),
        ("Z33", _v(sec_data.get("Zz_mm3", 0), 1) + " mm\u00b3",
         "Z22", _v(sec_data.get("Zy_mm3", 0), 1) + " mm\u00b3"),
        ("r33", _v(sec_data.get("rz_mm", 0), 3) + " mm",
         "r22", _v(sec_data.get("ry_mm", 0), 3) + " mm"),
        ("Cw",  _v(sec_data.get("Cw_mm6", 0), 1) + " mm\u2076",
         "Av3", _v(sec_data.get("Avz_mm2", 0), 1) + " mm\u00b2"),
        ("d",   _v(sec_data.get("d_mm", 0), 1) + " mm",
         "Av2", _v(sec_data.get("Avy_mm2", 0), 1) + " mm\u00b2"),
        ("bf",  _v(sec_data.get("b_mm", 0), 1) + " mm",
         "tf",  _v(sec_data.get("tf_mm", 0), 2) + " mm"),
        ("tw",  _v(sec_data.get("tw_mm", 0), 2) + " mm",
         "", ""),
    ]
    for k1, v1, k2, v2 in props:
        a(_row(k1, v1, k2, v2, cls="lbl"))
    a("</table>")

    # ── Design Messages ────────────────────────────────────────────
    msg_color = C["msg_ok"] if status == "OK" else C["msg_ng"]
    a(_h2("Design Messages", msg_color))
    msgs = []

    # Strength check
    if is_scwb_ng:
        scwb_plane = "minor" if "minor" in rtype else "major"
        scwb_data = smf.get("scwb_" + scwb_plane, {})
        msgs.append(("Error",
                      "SCWB NG \u2014 \u03a3M*pc/\u03a3M*be = {} < 1.0 "
                      "({} axis, AISC 341-22 E3.4a)".format(
                          _v(scwb_data.get("ratio_E3_1", 0), 4),
                          scwb_plane), "tag-ng"))
    if is_overstressed and not is_scwb_ng:
        msgs.append(("Error", "Overstressed \u2014 DCR = {} > 1.0 "
                      "(AISC 360-22)".format(_v(dcr, 4)), "tag-ng"))
    elif not is_overstressed and not is_scwb_ng:
        msgs.append(("OK",
                      "Strength adequate \u2014 DCR = {}".format(_v(dcr, 4)),
                      "tag-ok"))

    # SCWB OK message (if checked but passes)
    if smf and not is_scwb_ng:
        for plane in ("major", "minor"):
            sc = smf.get("scwb_" + plane)
            if sc and sc.get("ok", True):
                msgs.append(("OK",
                    "SCWB {} \u2014 \u03a3M*pc/\u03a3M*be = {} > 1.0".format(
                        plane, _v(sc.get("ratio_E3_1", 0), 4)),
                    "tag-ok"))

    # Seismic checks (AISC 341-22)
    if smf:
        if not smf.get("Lr_ok", True):
            msgs.append(("Error",
                "L/r = {} > 60 (AISC 341-22 E3.4c(b))".format(
                    _v(smf.get("Lr_max", 0))), "tag-ng"))
        if not smf.get("Lb_ry_ok", True):
            msgs.append(("Error",
                "Lb/ry = {} > {} (AISC 341-22 D1.2b)".format(
                    _v(smf.get("Lb_ry", 0)),
                    _v(smf.get("Lb_ry_limit", 0))), "tag-ng"))
        sd = smf.get("seismic_ductility") or {}
        if sd and not sd.get("ok", True):
            parts = []
            if not sd.get("flange_ok", True):
                parts.append("flange b/2tf={} > {}".format(
                    _v(sd.get("lambda_flange", 0), 3),
                    _v(sd.get("lambda_hd_flange", 0), 3)))
            if not sd.get("web_ok", True):
                parts.append("web h/tw={} > {}".format(
                    _v(sd.get("lambda_web", 0), 3),
                    _v(sd.get("lambda_hd_web", 0), 3)))
            msgs.append(("Error",
                "Seismic HD NG \u2014 {} (AISC 341-22 D1.1)".format(
                    ", ".join(parts)), "tag-ng"))

    for tag, msg, cls in msgs:
        a('<p style="margin:1px 0 1px 12px;">'
          '<span class="{}">[{}]</span> {}</p>'.format(cls, tag, msg))

    # ── Compactness ────────────────────────────────────────────────
    a(_h2("Compactness", C["compact"]))
    a('<table width="100%">')
    a(_th("Classification", "\u03bb", "\u03bb_hd", "Class"))
    a(_row("Axial", "\u2014", "\u2014",
           cap.get("section_class_axial", "?"), cls="lbl"))
    a(_row("Flexure (Flange)", "\u2014", "\u2014",
           cap.get("section_class_flexure_flange", "?"), cls="lbl"))
    a(_row("Flexure (Web)", "\u2014", "\u2014",
           cap.get("section_class_flexure_web", "?"), cls="lbl"))
    sd = smf.get("seismic_ductility") or {}
    if sd:
        sc = sd.get("seismic_class", "")
        if "lambda_flange" in sd:
            ok = sd.get("flange_ok", True)
            a(_row("Seismic / Flange",
                   _v(sd.get("lambda_flange", 0), 3),
                   _v(sd.get("lambda_hd_flange", 0), 3),
                   '<span class="{}">{} [{}]</span>'.format(
                       "tag-ok" if ok else "tag-ng", sc,
                       "OK" if ok else "NG"), cls="lbl"))
        if "lambda_web" in sd:
            ok = sd.get("web_ok", True)
            a(_row("Seismic / Web",
                   _v(sd.get("lambda_web", 0), 3),
                   _v(sd.get("lambda_hd_web", 0), 3),
                   '<span class="{}">{} [{}]</span>'.format(
                       "tag-ok" if ok else "tag-ng", sc,
                       "OK" if ok else "NG"), cls="lbl"))
    a("</table>")

    # ── Stress Check Forces ────────────────────────────────────────
    Pu   = pmm.get("Pr_kN", 0)
    Mu33 = pmm.get("MrMajor_kNm", 0)
    Mu22 = pmm.get("MrMinor_kNm", 0)
    Vr   = shear.get("VrMajDsgn_kN", 0)

    a(_h2("Governing Forces &amp; Moments", C["force"]))
    a('<table width="100%">')
    a(_th("", "Value", "Unit", "Combo"))
    a(_row("Pu", _v(Pu), "kN", combo, cls="lbl"))
    a(_row("Mu33", _v(Mu33), "kN\u00b7m", combo, cls="lbl"))
    a(_row("Mu22", _v(Mu22), "kN\u00b7m", combo, cls="lbl"))
    sh_combo = shear.get("VMajorCombo", "\u2014")
    a(_row("Vu (Major)", _v(Vr), "kN", sh_combo, cls="lbl"))
    a("</table>")
    a('<p class="kv" style="color:{};">PMM at {} mm &nbsp;|&nbsp; '
      'Shear at {} mm</p>'.format(
          C["subtle"], _v(loc_mm, 1),
          _v(shear.get("VMajorLocation_mm", 0), 1)))

    # ── PMM Interaction ────────────────────────────────────────────
    eq    = pmm.get("equation", "?")
    total = pmm.get("TotalRatio", 0)
    pr_r  = pmm.get("PRatio", 0)
    mm33  = pmm.get("MMajRatio", 0)
    mm22  = pmm.get("MMinRatio", 0)

    a(_h2("PMM Interaction \u2014 {}".format(eq), C["pmm"]))
    if eq == "H1-1a":
        a('<p class="eq">DCR = (Pr/Pc) + (8/9)(Mr33/Mc33 + Mr22/Mc22)</p>')
    else:
        a('<p class="eq">DCR = (1/2)(Pr/Pc) + (Mr33/Mc33 + Mr22/Mc22)</p>')
    a('<table width="100%">')
    a(_th("Component", "Demand", "Capacity", "Ratio"))
    a(_row("Axial (Pr / \u03c6Pn)",
           "{} kN".format(_v(Pu)),
           "{} kN".format(_v(cap.get("PcComp_kN", 0))),
           _v(pr_r, 4), cls="lbl"))
    a(_row("Major (Mr33 / \u03c6Mn33)",
           "{} kN\u00b7m".format(_v(Mu33)),
           "{} kN\u00b7m".format(_v(cap.get("McMajor_kNm", 0))),
           _v(mm33, 4), cls="lbl"))
    a(_row("Minor (Mr22 / \u03c6Mn22)",
           "{} kN\u00b7m".format(_v(Mu22)),
           "{} kN\u00b7m".format(_v(cap.get("McMinor_kNm", 0))),
           _v(mm22, 4), cls="lbl"))
    a('<tr><td class="lbl" style="border-top:1.5px solid {};"><b>D/C Total</b></td>'
      '<td colspan="2" style="border-top:1.5px solid {};"></td>'
      '<td style="border-top:1.5px solid {};"><b>{}</b></td></tr>'.format(
          C["pmm"], C["pmm"], C["pmm"], _v(total, 4)))
    a("</table>")

    # ── Axial Strength Detail ─────────────────────────────────────
    Pnc = cap.get("PcComp_kN", 0)
    Pnt = cap.get("PcTension_kN", 0)

    a(_h2("Axial Strength", C["axial"]))
    a('<table width="100%">')
    a(_th("", "Pu (kN)", "\u03c6Pn (kN)", "Ratio"))
    Pnc_r = abs(Pu) / Pnc if Pnc > 0 else 0
    Pnt_r = abs(Pu) / Pnt if Pnt > 0 else 0
    a(_row("Compression", _v(abs(Pu)), _v(Pnc), _v(Pnc_r, 4), cls="lbl"))
    a(_row("Tension", _v(abs(Pu)), _v(Pnt), _v(Pnt_r, 4), cls="lbl"))
    a("</table>")

    # ── Effective Lengths & K-factors ──────────────────────────────
    Kx = kf.get("Kx", 0) if kf else 0
    Ky = kf.get("Ky", 0) if kf else 0
    Kz = kf.get("Kz", 0) if kf else 0
    Lx = lengths.get("Lx_mm", 0) if lengths else 0
    Ly = lengths.get("Ly_mm", 0) if lengths else 0
    Lb_val = lengths.get("Lb_mm", 0) if lengths else cap.get("Lb_governing_mm", 0)
    Lcz = lengths.get("Lcz_mm", 0) if lengths else 0

    if kf or lengths:
        a(_h2("Effective Lengths &amp; K-Factors", C["axial"]))
        a('<table width="100%">')
        a(_th("Axis", "K", "L (mm)", "KL (mm)"))
        a(_row("Major (x)", _v(Kx, 3), _v(Lx, 1), _v(Kx * Lx, 1), cls="lbl"))
        a(_row("Minor (y)", _v(Ky, 3), _v(Ly, 1), _v(Ky * Ly, 1), cls="lbl"))
        a(_row("Torsion (z)", _v(Kz, 3), _v(Lcz, 1), _v(Kz * Lcz, 1), cls="lbl"))
        a(_row("LTB (Lb)", "", _v(Lb_val, 1), "", cls="lbl"))
        a("</table>")
        a('<p class="kv" style="color:{};">Cb = {}</p>'.format(
            C["subtle"], _v(cap.get("Cb", 0), 4)))

    # ── Flexural Strength ──────────────────────────────────────────
    Mn33 = cap.get("McMajor_kNm", 0)
    Mn22 = cap.get("McMinor_kNm", 0)
    Mn33_r = abs(Mu33) / Mn33 if Mn33 > 0 else 0
    Mn22_r = abs(Mu22) / Mn22 if Mn22 > 0 else 0

    a(_h2("Flexural Strength", C["moment"]))
    a('<table width="100%">')
    a(_th("Axis", "Mu (kN\u00b7m)", "\u03c6Mn (kN\u00b7m)", "Ratio"))
    a(_row("Major (33)", _v(Mu33), _v(Mn33), _v(Mn33_r, 4), cls="lbl"))
    a(_row("Minor (22)", _v(Mu22), _v(Mn22), _v(Mn22_r, 4), cls="lbl"))
    a("</table>")
    a('<p class="kv" style="color:{};">Lb (governing) = {} mm</p>'.format(
        C["subtle"], _v(cap.get("Lb_governing_mm", 0), 1)))

    # ── Shear Check ────────────────────────────────────────────────
    Vn   = shear.get("PhiVnMajor_kN", 0)
    Vrat = shear.get("VMajorRatio", 0)
    sh_ok = Vrat <= 1.0
    sh_loc = shear.get("VMajorLocation_mm", 0)

    a(_h2("Shear Check", C["shear"]))
    a('<table width="100%">')
    a(_th("", "Vu (kN)", "\u03c6Vn (kN)", "Ratio", "Status"))
    a(_row("Major Shear", _v(Vr), _v(Vn), _v(Vrat, 4),
           '<span class="{}">{}</span>'.format(
               "tag-ok" if sh_ok else "tag-ng",
               "OK" if sh_ok else "NG"), cls="lbl"))
    a("</table>")
    a('<p class="kv" style="color:{};">Combo: {} &nbsp;|&nbsp; '
      'Location: {} mm</p>'.format(C["subtle"], sh_combo, _v(sh_loc, 1)))

    # ── Seismic Checks ────────────────────────────────────────────
    if smf:
        a(_h2("Seismic Checks \u2014 AISC 341-22", C["seismic"]))
        a('<table width="100%">')
        a(_th("Check", "Value", "Limit", "Status"))
        if "Lr_max" in smf:
            ok = smf.get("Lr_ok", True)
            a(_row("L/r max", _v(smf.get("Lr_max", 0)),
                   "60", '<span class="{}">{}</span>'.format(
                       "tag-ok" if ok else "tag-ng",
                       "OK" if ok else "NG"), cls="lbl"))
        if "Lb_ry" in smf:
            ok = smf.get("Lb_ry_ok", True)
            a(_row("Lb / ry", _v(smf.get("Lb_ry", 0)),
                   _v(smf.get("Lb_ry_limit", 0)),
                   '<span class="{}">{}</span>'.format(
                       "tag-ok" if ok else "tag-ng",
                       "OK" if ok else "NG"), cls="lbl"))
        a("</table>")
        if sd and "Ca" in sd:
            a('<p class="kv">Ca = {} (Pr / Fy\u00b7Ag)</p>'.format(
                _v(sd.get("Ca", 0), 4)))

        # ── SCWB Detail (AISC 341-22 E3.4a) ─────────────────────
        scwb_maj = smf.get("scwb_major")
        scwb_min = smf.get("scwb_minor")
        if scwb_maj or scwb_min:
            a(_h2("SCWB \u2014 AISC 341-22 E3.4a", C["seismic"]))
            a('<p class="eq">\u03a3M*pc / \u03a3M*be > 1.0 '
              '&nbsp; (Eq. E3-1)</p>')
            a('<table width="100%">')
            a(_th("Plane", "\u03a3M*pc (kN\u00b7m)",
                  "\u03a3M*be (kN\u00b7m)", "Ratio", "DCR", "Status"))
            for plane, sc in [("Major", scwb_maj), ("Minor", scwb_min)]:
                if not sc:
                    continue
                ok = sc.get("ok", True)
                a(_row(plane,
                       _v(sc.get("sum_Mpc_kNm", 0), 2),
                       _v(sc.get("sum_Mbe_kNm", 0), 2),
                       _v(sc.get("ratio_E3_1", 0), 4),
                       _v(sc.get("dcr", 0), 4),
                       '<span class="{}">{}</span>'.format(
                           "tag-ok" if ok else "tag-ng",
                           "OK" if ok else "NG"),
                       cls="lbl"))
            a("</table>")
            # Show which node governs
            gov_sc = scwb_min if scwb_min else scwb_maj
            if gov_sc:
                a('<p class="kv" style="color:{};">Governing joint: '
                  'Node {}</p>'.format(
                      C["subtle"], gov_sc.get("node_id", "?")))

    # ── Station Summary (Top 5 Critical) ──────────────────────────
    if stations:
        top = sorted(stations, key=lambda s: s.get("ratio", 0), reverse=True)[:5]
        a(_h2("Station Summary \u2014 Top 5 Critical", C["subtle"]))
        a('<table width="100%">')
        a(_th("Combo", "Loc (mm)", "Eq.", "Axial", "Maj", "Min", "DCR"))
        for s in top:
            sr = s.get("ratio", 0)
            scls = "tag-ok" if sr <= 1.0 else "tag-ng"
            a("<tr>"
              '<td class="lbl">{}</td>'
              "<td>{}</td>"
              "<td>{}</td>"
              "<td>{}</td>"
              "<td>{}</td>"
              "<td>{}</td>"
              '<td><span class="{}">{}</span></td>'
              "</tr>".format(
                  s.get("combo", "?"), _v(s.get("location_mm", 0), 0),
                  s.get("equation", "?"),
                  _v(s.get("axl", 0), 4), _v(s.get("b_maj", 0), 4),
                  _v(s.get("b_min", 0), 4), scls, _v(sr, 4)))
        a("</table>")

    a("</body></html>")
    return "".join(h)


# ===================================================================
# STRESS CHECK DIALOG
# ===================================================================

class StressCheckDialog(QDialog):
    """ROIDA Steel Stress Check Data dialog — section drawing + HTML report."""

    def __init__(self, dr, sec_data, sec_name="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROIDA \u2014 Steel Stress Check Data")
        self.setFixedWidth(900)
        self.setMinimumHeight(640)
        self.resize(880, 720)
        self.setWindowFlags(
            self.windowFlags()
            & ~Qt.WindowContextHelpButtonHint
            & ~Qt.WindowMaximizeButtonHint)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Left: section illustration
        fig = Figure(figsize=(2.6, 4.0), dpi=100)
        fig.patch.set_facecolor("white")
        canvas = FigureCanvas(fig)
        canvas.setFixedWidth(240)
        _draw_section(fig, sec_data.get("d_mm", 0), sec_data.get("b_mm", 0),
                      sec_data.get("tf_mm", 0), sec_data.get("tw_mm", 0),
                      sec_name)
        canvas.draw()
        layout.addWidget(canvas)

        # Right: HTML report
        report_html = _build_stress_check_report(dr, sec_data, sec_name)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet(
            "QTextEdit { background:#FAFAFA; border:1px solid #CCC;"
            "            padding: 0px; }")
        txt.setHtml(report_html)
        txt.document().setDocumentMargin(0)
        txt.setViewportMargins(0, 0, 0, 0)
        fmt = txt.document().rootFrame().frameFormat()
        fmt.setMargin(0)
        fmt.setPadding(0)
        txt.document().rootFrame().setFrameFormat(fmt)
        layout.addWidget(txt, stretch=1)


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
        """Resolve Revit element IDs to analysis element IDs.

        - Balok / single-story: Revit ID = analysis ID (exact match)
        - Kolom multi-story: analysis ID = revit_id * 1000 + story_index
          Satu Revit ID di-expand ke semua segmen story-nya.
        """
        analysis_ids = set()
        for lc_data in self.analysis.values():
            if not isinstance(lc_data, dict):
                continue
            for k in lc_data.get("elements", {}):
                try:
                    analysis_ids.add(int(k))
                except ValueError:
                    pass

        # Identifikasi composite bases: kolom dengan >1 segmen sharing base
        col_base_count = {}
        for me in self.model_elems:
            if me.get("type", "").lower() != "column":
                continue
            try:
                cid = int(me["id"])
            except (KeyError, ValueError):
                continue
            base = cid // 1000
            col_base_count[base] = col_base_count.get(base, 0) + 1
        composite_bases = {b for b, cnt in col_base_count.items() if cnt > 1}

        # Bangun segment_map hanya untuk composite bases
        segment_map = {}
        for me in self.model_elems:
            if me.get("type", "").lower() != "column":
                continue
            try:
                cid = int(me["id"])
            except (KeyError, ValueError):
                continue
            base = cid // 1000
            if base in composite_bases:
                segment_map.setdefault(base, []).append(cid)

        resolved = []
        for sid in selected_ids:
            if sid in analysis_ids:
                # Direct match (balok, atau elemen single-story)
                if sid not in resolved:
                    resolved.append(sid)
            elif sid in segment_map:
                # Multi-story column: expand ke semua segmen story
                for comp_id in sorted(segment_map[sid]):
                    if comp_id not in resolved:
                        resolved.append(comp_id)

        # Fallback: jika tidak ada match (misal Auto Select punya ID berbeda),
        # tampilkan semua elemen dari analysis agar data tetap bisa dilihat
        if not resolved and analysis_ids:
            resolved = sorted(analysis_ids)

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

        # ── DCR bar (clickable) ────────────────────────────────────
        self.lbl_dcr = ClickableLabel("")
        self.lbl_dcr.setAlignment(Qt.AlignCenter)
        self.lbl_dcr.setWordWrap(True)
        self.lbl_dcr.setStyleSheet(
            "padding:6px 12px; background:#F5F5F5; "
            "border-top:1px solid #BDBDBD; font-size:9pt;")
        self.lbl_dcr.clicked.connect(self._on_dcr_click)
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

    def _on_dcr_click(self):
        """Open Steel Stress Check Data dialog for the current element."""
        eid = self.cur_eid
        dr = self.design_lookup.get(eid)
        if not dr:
            return
        me = self.elem_index.get(eid, {})
        sec_data = me.get("section", {})
        sec_name = _short_fam(me.get("family", ""))
        dlg = StressCheckDialog(dr, sec_data, sec_name, parent=self)
        dlg.show()

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

        s_tag = "(Major)" if axis == "Major" else "(Minor)"
        s_ylabel = "{} (kN)".format(shear_key)
        m_ylabel = "{} (kN\u00b7m)".format(moment_key)
        d_ylabel = "\u03b4{} (mm)".format(defl_key[1])

        draw_diagram(axes[0], x_mm,  shear,  s_ylabel, "SFD")
        draw_diagram(axes[1], x_mm,  moment, m_ylabel, "BMD")
        draw_diagram(axes[2], x_mm,  axial,  "P (kN)",  "NFD")
        draw_diagram(axes[3], xd,    d,      d_ylabel,  "Defl")
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
            combo   = dr.get("governing_combo", "\u2014")
            rtype   = dr.get("ratio_type", "PMM")
            eq      = dr.get("pmm_detail", {}).get("equation", "")
            pr_pc   = dr.get("pmm_detail", {}).get("PRatio", None)
            mr_maj  = dr.get("pmm_detail", {}).get("MMajRatio", None)
            color   = "#B71C1C" if status != "OK" else "#1B5E20"

            detail = ""
            if rtype.startswith("SCWB"):
                # SCWB governs — show SCWB info instead of PMM
                scwb_plane = "minor" if "minor" in rtype else "major"
                scwb_d = (dr.get("smf_checks") or {}).get(
                    "scwb_" + scwb_plane, {})
                detail = "  Ratio(E3-1)={:.4f}".format(
                    scwb_d.get("ratio_E3_1", 0))
                eq = "E3-1 ({})".format(scwb_plane)
            else:
                if pr_pc is not None:
                    detail += "  Pr/Pc={:.3f}".format(pr_pc)
                if mr_maj is not None:
                    detail += "  MrMaj/Mc={:.3f}".format(mr_maj)

            self.lbl_dcr.setStyleSheet(
                "padding:6px 12px; background:#F5F5F5; "
                "border-top:1px solid #BDBDBD; font-size:9pt; "
                "color:{}; text-decoration:underline;".format(color))
            self.lbl_dcr.setCursor(QCursor(Qt.PointingHandCursor))
            self.lbl_dcr.setToolTip("Klik untuk detail Steel Stress Check")
            self.lbl_dcr.setText(
                "AISC 360-22 LRFD  |  DCR = {:.3f}  |  Status: {}  |  "
                "Combo: {}  |  Eq: {}{}".format(
                    dcr, status, combo, eq, detail))
        else:
            self.lbl_dcr.setStyleSheet(
                "padding:6px 12px; background:#F5F5F5; "
                "border-top:1px solid #BDBDBD; font-size:9pt; color:#888;")
            self.lbl_dcr.setCursor(QCursor(Qt.ArrowCursor))
            self.lbl_dcr.setToolTip("")
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

        s_ylabel = "{} (kN)".format(shear_key)
        m_ylabel = "{} (kN·m)".format(moment_key)
        d_ylabel = "\u03b4{} (mm)".format(defl_key[1])

        self.panel_sfd.update("SFD\n{}".format(s_tag), x_mm,  shear,  s_ylabel)
        self.panel_bmd.update("BMD\n{}".format(s_tag), x_mm,  moment, m_ylabel)
        self.panel_nfd.update("NFD\n(Axial)",           x_mm,  axial,  "P (kN)")
        self.panel_defl.update("Deflection\n{}".format(s_tag), xd, d, d_ylabel,
                               extra_info=dir_note)

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
    parser.add_argument("--elements", default="")
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
        raw = args.elements.strip()
        if raw.startswith("["):
            selected_ids = [int(x) for x in json.loads(raw)]
        else:
            selected_ids = [int(x) for x in raw.split(",") if x.strip()]
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
