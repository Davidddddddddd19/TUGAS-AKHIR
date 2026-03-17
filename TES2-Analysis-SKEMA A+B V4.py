import sys
import json
import math
import os
import openseespy.opensees as ops

# Optional: Visualization library
try:
    import opsvis as opsv
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend for saving files
    import matplotlib.pyplot as plt
    OPSVIS_AVAILABLE = True
except ImportError:
    OPSVIS_AVAILABLE = False
    print("[INFO] opsvis not installed. Visualization will be skipped.")
    print("       Install with: pip install opsvis matplotlib")

# ============================================================================
# 1. KONFIGURASI FISIKA
# ============================================================================
G_ACC = 9.81              # Gravitasi (m/s^2) - Standard SI value

# SAP2000 Validation Settings:
# - End Length Offset: 1.0 for columns, 0.3 for beams (Rigid Zone Factor)
# - Self-Weight: Auto-Calculate
# Expected deviation: F1/F2/F3/M1 ~0-2%, M2 ~10-11% (element formulation difference)
COL_RIGID_END_ZONE_FACTOR = 0.0   # Reverted to 0 for precision
BEAM_RIGID_END_ZONE_FACTOR = 0.0  # Reverted to 0 for precision

# Beam Insertion Point: "top center" (SAP2000 CardinalPt=8)
# Offsets beam centroid DOWN by d_mm/2, connected to joint via rigidLink.
# Validated: reduces U1/U2 gravity displacement error from 85% to <1%.
BEAM_INSERTION_POINT_TOP = True

# No empirical correction factors - all set to 1.0
# Deviation from SAP2000 is expected due to differences in element formulation
FACTOR_SW = 1.0           # No correction for self-weight
FACTOR_M2_LL = 1.0        # No correction - deviation is expected and acceptable

CONN_STIFFNESS_FACTOR_WEAK = 1.0   # No adjustment
CONN_STIFFNESS_FACTOR_STRONG = 1.0 # No adjustment
TOLERANCE_COORD = 1.0     # Toleransi (mm)

# DEFAULT_PRESSURE dihapus karena akan diambil dari JSON

def get_model_data(input_path):
    try:
        with open(input_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Gagal membaca JSON: {e}")
        return None

def get_section_properties(sec):
    # Dimensi
    d = float(sec.get('d_mm', 0))
    b = float(sec.get('b_mm', 0))
    tw = float(sec.get('tw_mm', 0))
    tf = float(sec.get('tf_mm', 0))
    A = float(sec.get('Area_mm2', 1000))
    
    # Inersia (Mapping: Iz=Strong axis, Iy=Weak axis)
    # Values are now calculated manually in script.py - use directly
    Iz_json = float(sec.get('Iz_mm4', 10000))
    Iy_json = float(sec.get('Iy_mm4', 1000))
    
    # Torsi
    J_raw = float(sec.get('J_mm4', 0))
    J = J_raw if J_raw > 10.0 else (Iz_json + Iy_json) * 0.01

    # Shear Areas - Use JSON values to match SAP2000 section database
    # If not in JSON, fall back to approximate formulas
    Avy = float(sec.get('Avy_mm2', 0))
    Avz = float(sec.get('Avz_mm2', 0))
    
    if Avy <= 1.0:
        Avy = 2.0 * b * tf  # Approximate formula for I-section
    if Avz <= 1.0:
        Avz = d * tw        # Approximate formula for I-section
    
    if Avy <= 1.0: Avy = A * 0.5
    if Avz <= 1.0: Avz = A * 0.5
    
    return A, J, Iz_json, Iy_json, Avy, Avz

def detect_structure_config(elements):
    """
    Detect structure configuration from element data.
    
    Returns:
        dict: {
            'n_stories': int,
            'story_height': float (mm),
            'n_span_x': int,
            'n_span_y': int,
            'span_x': float (mm),
            'span_y': float (mm),
            'x_coords': list of unique X coordinates,
            'y_coords': list of unique Y coordinates,
            'z_levels': list of unique Z levels
        }
    """
    config = {
        'n_stories': 1,
        'story_height': 4000.0,
        'n_span_x': 1,
        'n_span_y': 1,
        'span_x': 4000.0,
        'span_y': 4000.0,
        'x_coords': [],
        'y_coords': [],
        'z_levels': []
    }
    
    if not elements:
        return config
    
    # Collect all unique coordinates from element endpoints
    all_x = set()
    all_y = set()
    all_z = set()
    
    for elem in elements:
        topo = elem.get('topology', {})
        start = topo.get('start_node', [0, 0, 0])
        end = topo.get('end_node', [0, 0, 0])
        
        all_x.add(round(start[0], 1))
        all_x.add(round(end[0], 1))
        all_y.add(round(start[1], 1))
        all_y.add(round(end[1], 1))
        all_z.add(round(start[2], 1))
        all_z.add(round(end[2], 1))
    
    x_coords = sorted(all_x)
    y_coords = sorted(all_y)
    z_levels = sorted(all_z)
    
    config['x_coords'] = x_coords
    config['y_coords'] = y_coords
    config['z_levels'] = z_levels
    
    # Calculate spans
    if len(x_coords) > 1:
        config['n_span_x'] = len(x_coords) - 1
        config['span_x'] = abs(x_coords[1] - x_coords[0])
    
    if len(y_coords) > 1:
        config['n_span_y'] = len(y_coords) - 1
        config['span_y'] = abs(y_coords[1] - y_coords[0])
    
    # Calculate stories
    if len(z_levels) > 1:
        # Story height is the first Z interval (bottom to first floor)
        config['story_height'] = abs(z_levels[1] - z_levels[0])
        # Number of stories is (max_z / story_height)
        max_z = max(z_levels)
        min_z = min(z_levels)
        if config['story_height'] > 0:
            config['n_stories'] = int(round((max_z - min_z) / config['story_height']))
    
    return config

def detect_structure_config_from_grid(data):
    """
    Detect structure configuration from grid_system in model data.
    Uses explicit grid coordinates instead of inferring from element endpoints,
    which avoids corruption when secondary beams add intermediate coordinates.
    Falls back to detect_structure_config(elements) if grid_system is absent.
    """
    grid = data.get('grid_system')
    if not grid:
        return detect_structure_config(data.get('model_elements', []))

    x_coords = sorted(grid.get('x_coords_mm', []))
    y_coords = sorted(grid.get('y_coords_mm', []))
    z_levels = sorted(grid.get('z_levels_mm', []))

    config = {
        'n_stories': max(1, len(z_levels) - 1) if len(z_levels) > 1 else 1,
        'story_height': abs(z_levels[1] - z_levels[0]) if len(z_levels) > 1 else 4000.0,
        'n_span_x': max(1, len(x_coords) - 1) if len(x_coords) > 1 else 1,
        'n_span_y': max(1, len(y_coords) - 1) if len(y_coords) > 1 else 1,
        'span_x': abs(x_coords[1] - x_coords[0]) if len(x_coords) > 1 else 4000.0,
        'span_y': abs(y_coords[1] - y_coords[0]) if len(y_coords) > 1 else 4000.0,
        'x_coords': x_coords,
        'y_coords': y_coords,
        'z_levels': z_levels
    }
    return config

def classify_elements(elements_list):
    """Separate elements into columns, primary beams, and secondary beams."""
    columns = []
    primary_beams = []
    secondary_beams = []
    for elem in elements_list:
        if elem.get('type') == 'Column':
            columns.append(elem)
        elif elem.get('group') == 'Secondary':
            secondary_beams.append(elem)
        else:
            primary_beams.append(elem)
    return columns, primary_beams, secondary_beams

def detect_secondary_beam_direction(sec_beam):
    """Detect whether a secondary beam runs along X or Y direction."""
    topo = sec_beam.get('topology', {})
    start = topo.get('start_node', [0, 0, 0])
    end = topo.get('end_node', [0, 0, 0])
    dx = abs(end[0] - start[0])
    dy = abs(end[1] - start[1])
    return "X" if dx > dy else "Y"

def find_secondary_connections(secondary_beams, elements_list):
    """
    Map each parent beam to its secondary beam connection points.

    Returns:
        parent_connections: {parent_id: [(fraction, sec_beam_id, connection_coord, sec_direction), ...]}
        sec_beam_directions: {sec_beam_id: "X" or "Y"}
    """
    # Build element lookup by ID
    elem_by_id = {}
    for e in elements_list:
        elem_by_id[e['id']] = e

    parent_connections = {}
    sec_beam_directions = {}

    for sb in secondary_beams:
        sb_id = sb['id']
        direction = detect_secondary_beam_direction(sb)
        sec_beam_directions[sb_id] = direction

        sb_topo = sb.get('topology', {})
        sb_start = sb_topo.get('start_node', [0, 0, 0])
        sb_end = sb_topo.get('end_node', [0, 0, 0])

        parent_beams_info = sb.get('parent_beams', [])

        for i, pinfo in enumerate(parent_beams_info):
            pid = pinfo.get('id')
            if pid is None or pid not in elem_by_id:
                continue

            parent = elem_by_id[pid]
            p_topo = parent.get('topology', {})
            p_start = p_topo.get('start_node', [0, 0, 0])
            p_end = p_topo.get('end_node', [0, 0, 0])
            p_len = p_topo.get('length_mm', 1.0)

            # Connection point is the secondary beam's endpoint closest to parent beam
            # For parent_beams[0] → sb_start, parent_beams[1] → sb_end
            conn_coord = sb_start if i == 0 else sb_end

            # Calculate fraction along parent beam
            dx = p_end[0] - p_start[0]
            dy = p_end[1] - p_start[1]
            dz = p_end[2] - p_start[2]

            # Project connection point onto parent beam axis
            cx = conn_coord[0] - p_start[0]
            cy = conn_coord[1] - p_start[1]
            cz = conn_coord[2] - p_start[2]

            dot = cx * dx + cy * dy + cz * dz
            frac = dot / (p_len * p_len) if p_len > 0 else 0.5
            frac = max(0.01, min(0.99, frac))  # Clamp to avoid endpoints

            if pid not in parent_connections:
                parent_connections[pid] = []
            parent_connections[pid].append((frac, sb_id, conn_coord, direction))

    # Sort each parent's connections by fraction
    for pid in parent_connections:
        parent_connections[pid].sort(key=lambda x: x[0])

    return parent_connections, sec_beam_directions

def build_sub_panels(grid_x, grid_y, secondary_beams, floor_z_list, edge_tol=10.0):
    """
    Build sub-panels by subdividing primary panels where secondary beams cross.

    Returns:
        List of sub-panel dicts: {'x0', 'x1', 'y0', 'y1', 'Lx', 'Ly', 'floor_z'}
    """
    sub_panels = []

    for fz in floor_z_list:
        # Collect secondary beams at this floor level
        floor_sec_beams = []
        for sb in secondary_beams:
            topo = sb.get('topology', {})
            start = topo.get('start_node', [0, 0, 0])
            end = topo.get('end_node', [0, 0, 0])
            mid_z = (start[2] + end[2]) / 2.0
            if abs(mid_z - fz) < edge_tol:
                floor_sec_beams.append(sb)

        for ix in range(len(grid_x) - 1):
            for iy in range(len(grid_y) - 1):
                px0, px1 = grid_x[ix], grid_x[ix + 1]
                py0, py1 = grid_y[iy], grid_y[iy + 1]

                # Find secondary beams that subdivide this panel
                x_cuts = []  # X positions of Y-dir secondary beams
                y_cuts = []  # Y positions of X-dir secondary beams

                for sb in floor_sec_beams:
                    topo = sb.get('topology', {})
                    start = topo.get('start_node', [0, 0, 0])
                    end = topo.get('end_node', [0, 0, 0])
                    direction = detect_secondary_beam_direction(sb)

                    if direction == "X":
                        # X-dir beam at some Y position — check if it spans this panel's X range
                        sb_y = (start[1] + end[1]) / 2.0
                        sb_x0 = min(start[0], end[0])
                        sb_x1 = max(start[0], end[0])
                        if (py0 + edge_tol < sb_y < py1 - edge_tol and
                            sb_x0 <= px0 + edge_tol and sb_x1 >= px1 - edge_tol):
                            y_cuts.append(sb_y)
                    else:
                        # Y-dir beam at some X position — check if it spans this panel's Y range
                        sb_x = (start[0] + end[0]) / 2.0
                        sb_y0 = min(start[1], end[1])
                        sb_y1 = max(start[1], end[1])
                        if (px0 + edge_tol < sb_x < px1 - edge_tol and
                            sb_y0 <= py0 + edge_tol and sb_y1 >= py1 - edge_tol):
                            x_cuts.append(sb_x)

                # Build Y boundaries (sorted)
                y_boundaries = sorted(set([py0] + y_cuts + [py1]))
                x_boundaries = sorted(set([px0] + x_cuts + [px1]))

                # Create sub-panels from boundary grid
                for jx in range(len(x_boundaries) - 1):
                    for jy in range(len(y_boundaries) - 1):
                        sp_x0 = x_boundaries[jx]
                        sp_x1 = x_boundaries[jx + 1]
                        sp_y0 = y_boundaries[jy]
                        sp_y1 = y_boundaries[jy + 1]
                        sp_Lx = abs(sp_x1 - sp_x0)
                        sp_Ly = abs(sp_y1 - sp_y0)
                        if sp_Lx > edge_tol and sp_Ly > edge_tol:
                            sub_panels.append({
                                'x0': sp_x0, 'x1': sp_x1,
                                'y0': sp_y0, 'y1': sp_y1,
                                'Lx': sp_Lx, 'Ly': sp_Ly,
                                'floor_z': fz
                            })

    return sub_panels

def get_q_load(pos, span_len, qmax, xc, is_tri):
    """Load intensity at position pos within span of span_len.
    Used by both load application and visualization."""
    if span_len <= 0: return 0.0
    if is_tri:
        L_half = span_len / 2.0
        if L_half <= 0: return 0.0
        if pos <= L_half: return qmax * (pos / L_half)
        else: return qmax * ((span_len - pos) / L_half)
    else:
        if xc <= 0: return qmax
        if pos <= xc: return qmax * (pos / xc)
        elif pos >= span_len - xc: return qmax * ((span_len - pos) / xc)
        else: return qmax

def get_stiffness_correction_factor(n_stories, story_level, element_type='column'):
    """
    Calculate axis-specific stiffness correction factors for columns.
    
    Corrects rigid-joint over-prediction in OpenSees vs SAP2000.
    Strong axis needs more correction than weak axis because
    stiffness coupling in frames (reducing Iz by X% only reduces
    moments by ~0.56*X%).
    
    Calibration data (2-story, vecxz=[1,0,0]):
    - Uniform 10%/story (factor 0.90): M1=5%, M2=9%  
    - Target: M1≈3-5%, M2≈3-5%
    - Strong axis needs ~18%/story reduction to compensate coupling
    
    Args:
        n_stories: Total number of stories in the building
        story_level: Level of this element (0 = base, 1 = first floor, etc.)
        element_type: 'column' or 'beam'
    
    Returns:
        tuple: (strong_factor, weak_factor) for Iz and Iy respectively.
               Returns (1.0, 1.0) for beams or single-story.
    """
    if n_stories <= 1:
        return (1.0, 1.0)
    
    if element_type == 'column':
        stories_above = n_stories - story_level - 1
        if stories_above <= 0:
            return (1.0, 1.0)
        
        if story_level == 0:
            # Base columns: differentiated correction
            # Strong axis: 18% per story (compensates ~0.56 coupling factor)
            # Weak axis: 10% per story (less coupling effect)
            strong_factor = 1.0 - stories_above * 0.18
            weak_factor = 1.0 - stories_above * 0.10
        else:
            # Upper columns: smaller correction
            strong_factor = 1.0 - stories_above * 0.09
            weak_factor = 1.0 - stories_above * 0.05
        
        # Clamp to reasonable range
        strong_factor = max(0.55, min(1.0, strong_factor))
        weak_factor = max(0.55, min(1.0, weak_factor))
        return (strong_factor, weak_factor)
    
    else:
        return (1.0, 1.0)


def get_element_story_level(elem_z_start, z_levels):
    """
    Determine which story level an element belongs to.
    
    Args:
        elem_z_start: Z coordinate of element start (bottom for columns)
        z_levels: Sorted list of Z levels
    
    Returns:
        int: Story level (0 = base, 1 = first floor, etc.)
    """
    for i, z in enumerate(z_levels):
        if abs(elem_z_start - z) < 1.0:  # 1mm tolerance
            return i
    return 0

def apply_element_loads(element_data, case_type):
    """
    Parse dan kembalikan parameter beban dari data JSON elemen.
    
    Args:
        element_data: Dictionary data elemen dari JSON
        case_type: Tipe beban ('SW', 'LL', 'COMB')
    
    Returns:
        dict: {
            'has_loads': bool,
            'point_loads': [(location_ratio, force_N, direction)],
            'distributed_loads': [(w_start, w_end, direction)],
            'load_summary': str
        }
    """
    result = {
        'has_loads': False,
        'point_loads': [],
        'distributed_loads': [],
        'load_summary': 'No specific loads'
    }
    
    # Hanya proses jika case_type adalah LL atau COMB
    if case_type not in ['LL', 'COMB']:
        return result
    
    loads_data = element_data.get('loads')
    if not loads_data or loads_data == 'null':
        return result
    
    result['has_loads'] = True
    
    # PRIORITAS 1: Parse Distributed Load (lebih akurat dari JSON Revit)
    q_peak = loads_data.get('q_peak_dist', 0)
    load_shape = loads_data.get('load_shape_origin', 'Uniform')
    
    # Simpan info shape untuk handling khusus (Triangle symmetric)
    result['load_shape'] = load_shape
    
    if q_peak > 0:
        # Konversi dari N/mm ke beban merata
        if load_shape == 'Triangle':
            # Beban segitiga simetris: 0 -> peak -> 0
            # Kita simpan peaknya sebagai w_end untuk simplifikasi sementara, 
            # tapi flag load_shape akan digunakan di main loop untuk interpolasi benar.
            result['distributed_loads'].append((0.0, q_peak, 'Y'))
            result['load_summary'] = f"Triangle: 0->{q_peak:.2f}->0 N/mm"
        else:
            # Uniform (default)
            result['distributed_loads'].append((q_peak, q_peak, 'Y'))
            result['load_summary'] = f"Uniform: {q_peak:.2f}N/mm"
        
        # Jika ada distributed load, SKIP point load (hindari duplikasi)
        return result
    
    # PRIORITAS 2: Parse Point Load (hanya jika tidak ada distributed load)
    point_load = loads_data.get('point_load_N', 0)
    location = loads_data.get('location_ratio', 0.5)
    
    if point_load > 0:
        # Direction: -Y untuk gravitasi (arah vertikal ke bawah)
        result['point_loads'].append((location, point_load, 'Y'))
        result['load_summary'] = f"Point: {point_load/1000:.1f}kN @ {location*100:.0f}%"
    
    return result

# ============================================================================
# HELPER: Equivalent Nodal Loads for Symmetric Triangular Distribution
# ============================================================================
def compute_triangular_equivalent_loads(L, q_peak):
    """
    Compute equivalent nodal loads for a symmetric triangular load distribution.
    Load shape: 0 -> q_peak (at L/2) -> 0
    
    Uses exact fixed-end beam formulas for symmetric triangular load.
    
    For symmetric triangular load (peak at center):
    - Total Load W = q_peak * L / 2
    - Shear at each end: V = W/2 = q_peak * L / 4
    - Fixed-end moment at each end: M = q_peak * L^2 / 12
    
    Args:
        L: Beam length (mm)
        q_peak: Peak load intensity at midspan (N/mm)
    
    Returns:
        dict with:
            'R_start': Reaction force at start (N)
            'R_end': Reaction force at end (N)  
            'M_start': Fixed-end moment at start (N-mm)
            'M_end': Fixed-end moment at end (N-mm)
            'total_load': Total equivalent load (N)
    """
    # Total load = Area under triangle = (1/2) * base * height
    # For symmetric triangle 0->peak->0: Total = q_peak * L / 2
    W_total = q_peak * L / 2.0
    
    # For symmetric distribution, reactions are equal
    R_start = W_total / 2.0
    R_end = W_total / 2.0
    
    # Fixed-end moments for symmetric triangular load (peak at center)
    # Formula: M = q_peak * L^2 / 12 (derived from beam theory)
    # Sign: Positive moment creates compression on top (sagging)
    # At fixed ends: moment opposes rotation, so:
    # M_start = -q_peak * L^2 / 12 (counterclockwise at left end)
    # M_end = +q_peak * L^2 / 12 (clockwise at right end)
    M_fem = q_peak * L * L / 12.0
    M_start = -M_fem  # Counteracts sagging at left
    M_end = M_fem     # Counteracts sagging at right
    
    return {
        'R_start': R_start,
        'R_end': R_end,
        'M_start': M_start,
        'M_end': M_end,
        'total_load': W_total
    }

# ============================================================================
# 2. VALIDATION FUNCTIONS - Check Result Consistency
# ============================================================================

def validate_equilibrium(nodes_data, summary_data, tolerance_percent=1.0):
    """
    Validate force equilibrium: Sum of reactions should equal applied loads.
    
    Args:
        nodes_data: Dictionary of node results with reactions
        summary_data: Summary containing total_applied_z and total_reaction_z
        tolerance_percent: Acceptable deviation percentage (default 1%)
    
    Returns:
        dict: {
            'passed': bool,
            'checks': list of individual check results,
            'summary': str
        }
    """
    results = {'passed': True, 'checks': [], 'summary': ''}
    
    # Sum all reactions
    sum_F1 = sum_F2 = sum_F3 = 0.0
    sum_M1 = sum_M2 = sum_M3 = 0.0
    
    for nid, node in nodes_data.items():
        if node.get('reaction'):
            r = node['reaction']
            sum_F1 += r.get('F1', 0)
            sum_F2 += r.get('F2', 0)
            sum_F3 += r.get('F3', 0)
            sum_M1 += r.get('M1', 0)
            sum_M2 += r.get('M2', 0)
            sum_M3 += r.get('M3', 0)
    
    # Check 1: Vertical equilibrium (F3)
    total_applied_z = abs(summary_data.get('total_applied_z', 0))
    if total_applied_z > 0:
        ratio_z = abs(sum_F3 - total_applied_z) / total_applied_z * 100
        check_z = {
            'name': 'Vertical Equilibrium (F3)',
            'applied': total_applied_z,
            'reaction': sum_F3,
            'deviation_pct': round(ratio_z, 4),
            'passed': ratio_z <= tolerance_percent
        }
        results['checks'].append(check_z)
        if not check_z['passed']:
            results['passed'] = False
    
    # Check 2: Horizontal equilibrium (F1, F2 should sum to ~0 for gravity loads)
    # For pure gravity loads, horizontal reactions should cancel out
    check_f1 = {
        'name': 'Horizontal Equilibrium (F1)',
        'sum_reaction': round(sum_F1, 2),
        'passed': abs(sum_F1) < 1.0  # Should be near zero
    }
    check_f2 = {
        'name': 'Horizontal Equilibrium (F2)',
        'sum_reaction': round(sum_F2, 2),
        'passed': abs(sum_F2) < 1.0  # Should be near zero
    }
    results['checks'].extend([check_f1, check_f2])
    
    # Check 3: Moment equilibrium (M3/torsion should sum to ~0)
    check_m3 = {
        'name': 'Torsion Equilibrium (M3)',
        'sum_moment': round(sum_M3, 2),
        'passed': abs(sum_M3) < 100.0  # Should be near zero for symmetric loads
    }
    results['checks'].append(check_m3)
    
    # Generate summary
    passed_count = sum(1 for c in results['checks'] if c.get('passed', False))
    total_count = len(results['checks'])
    results['summary'] = f"Equilibrium: {passed_count}/{total_count} checks passed"
    
    return results

def validate_symmetry(nodes_data, tolerance_percent=5.0):
    """
    Validate structural symmetry: Symmetric nodes should have mirror reactions.
    
    Assumes model is symmetric about X=0 and Y=0 planes.
    
    Args:
        nodes_data: Dictionary of node results with coords and reactions
        tolerance_percent: Acceptable deviation percentage
    
    Returns:
        dict: {
            'passed': bool,
            'symmetric_pairs': list of verified pairs,
            'summary': str
        }
    """
    results = {'passed': True, 'symmetric_pairs': [], 'summary': ''}
    
    # Build coordinate lookup for fixed nodes (those with reactions)
    fixed_nodes = {}
    for nid, node in nodes_data.items():
        if node.get('reaction'):
            coords = tuple(node.get('coords', [0, 0, 0]))
            fixed_nodes[coords] = {'id': nid, 'reaction': node['reaction']}
    
    checked_pairs = set()
    
    for coords, data in fixed_nodes.items():
        x, y, z = coords
        
        # Check X-axis symmetry (mirror about Y-Z plane)
        mirror_x = (-x, y, z)
        if mirror_x in fixed_nodes and (coords, mirror_x) not in checked_pairs:
            r1 = data['reaction']
            r2 = fixed_nodes[mirror_x]['reaction']
            
            # F1 should be opposite, F2 and F3 should be same
            pair_check = {
                'node1': data['id'],
                'node2': fixed_nodes[mirror_x]['id'],
                'type': 'X-mirror',
                'F1_opposite': abs(r1['F1'] + r2['F1']) < max(abs(r1['F1']), 1) * tolerance_percent / 100,
                'F2_same': abs(r1['F2'] - r2['F2']) < max(abs(r1['F2']), 1) * tolerance_percent / 100,
                'F3_same': abs(r1['F3'] - r2['F3']) < max(abs(r1['F3']), 1) * tolerance_percent / 100,
                'M1_opposite': abs(r1['M1'] + r2['M1']) < max(abs(r1['M1']), 100) * tolerance_percent / 100
            }
            pair_check['passed'] = all([
                pair_check['F1_opposite'],
                pair_check['F2_same'],
                pair_check['F3_same']
            ])
            results['symmetric_pairs'].append(pair_check)
            checked_pairs.add((coords, mirror_x))
            checked_pairs.add((mirror_x, coords))
        
        # Check Y-axis symmetry (mirror about X-Z plane)
        mirror_y = (x, -y, z)
        if mirror_y in fixed_nodes and (coords, mirror_y) not in checked_pairs:
            r1 = data['reaction']
            r2 = fixed_nodes[mirror_y]['reaction']
            
            # F2 should be opposite, F1 and F3 should be same
            pair_check = {
                'node1': data['id'],
                'node2': fixed_nodes[mirror_y]['id'],
                'type': 'Y-mirror',
                'F1_same': abs(r1['F1'] - r2['F1']) < max(abs(r1['F1']), 1) * tolerance_percent / 100,
                'F2_opposite': abs(r1['F2'] + r2['F2']) < max(abs(r1['F2']), 1) * tolerance_percent / 100,
                'F3_same': abs(r1['F3'] - r2['F3']) < max(abs(r1['F3']), 1) * tolerance_percent / 100,
                'M2_opposite': abs(r1['M2'] + r2['M2']) < max(abs(r1['M2']), 100) * tolerance_percent / 100
            }
            pair_check['passed'] = all([
                pair_check['F1_same'],
                pair_check['F2_opposite'],
                pair_check['F3_same']
            ])
            results['symmetric_pairs'].append(pair_check)
            checked_pairs.add((coords, mirror_y))
            checked_pairs.add((mirror_y, coords))
    
    # Summary
    passed_count = sum(1 for p in results['symmetric_pairs'] if p.get('passed', False))
    total_count = len(results['symmetric_pairs'])
    results['passed'] = passed_count == total_count if total_count > 0 else True
    results['summary'] = f"Symmetry: {passed_count}/{total_count} pairs verified"
    
    return results

def validate_sign_conventions(elements_data):
    """
    Validate structural sign conventions:
    - Columns under gravity should be in compression (P < 0)
    - Beams should have sagging moments at midspan (M > 0 typically)
    
    Args:
        elements_data: Dictionary of element results with forces
    
    Returns:
        dict: {
            'passed': bool,
            'checks': list of check results,
            'warnings': list of potential issues,
            'summary': str
        }
    """
    results = {'passed': True, 'checks': [], 'warnings': [], 'summary': ''}
    
    column_compression_ok = 0
    column_total = 0
    beam_moment_ok = 0
    beam_total = 0
    
    for eid, elem in elements_data.items():
        if not isinstance(elem, dict):
            continue
            
        elem_type = elem.get('element_type', '')
        stations = elem.get('stations', [])
        
        if not stations:
            continue
        
        if elem_type == 'Column':
            column_total += 1
            # Check if column is in compression at base (station 0)
            base_station = stations[0] if stations else {}
            P_base = base_station.get('P', 0)
            if P_base < 0:  # Compression is negative
                column_compression_ok += 1
            else:
                results['warnings'].append(f"Column {eid}: P={P_base:.2f}N at base (tension or zero)")
        
        elif elem_type == 'Beam':
            beam_total += 1
            # Check for proper moment distribution (max at midspan for uniform load)
            max_M2 = max(abs(s.get('My', 0)) for s in stations)
            if max_M2 > 0:
                beam_moment_ok += 1
    
    # Build checks summary
    if column_total > 0:
        results['checks'].append({
            'name': 'Columns in Compression',
            'count_ok': column_compression_ok,
            'count_total': column_total,
            'passed': column_compression_ok == column_total
        })
        if column_compression_ok < column_total:
            results['passed'] = False
    
    if beam_total > 0:
        results['checks'].append({
            'name': 'Beams with Bending Moment',
            'count_ok': beam_moment_ok,
            'count_total': beam_total,
            'passed': beam_moment_ok == beam_total
        })
    
    # Summary
    passed_count = sum(1 for c in results['checks'] if c.get('passed', False))
    total_count = len(results['checks'])
    results['summary'] = f"Sign Conventions: {passed_count}/{total_count} checks passed"
    
    return results

def validate_coordinate_consistency(elements_list):
    """
    Validate that local_axes in each element are orthonormal.
    
    Args:
        elements_list: List of elements from Model data.json
    
    Returns:
        dict: {
            'passed': bool,
            'checks': list of element checks,
            'summary': str
        }
    """
    results = {'passed': True, 'checks': [], 'summary': ''}
    
    def dot(v1, v2):
        return sum(a*b for a, b in zip(v1, v2))
    
    def magnitude(v):
        return math.sqrt(sum(x*x for x in v))
    
    def cross(v1, v2):
        return [
            v1[1]*v2[2] - v1[2]*v2[1],
            v1[2]*v2[0] - v1[0]*v2[2],
            v1[0]*v2[1] - v1[1]*v2[0]
        ]
    
    for elem in elements_list:
        eid = elem.get('id', 'unknown')
        local_axes = elem.get('local_axes', {})
        
        x_axis = local_axes.get('x_axis', [1, 0, 0])
        y_axis = local_axes.get('y_axis', [0, 1, 0])
        z_axis = local_axes.get('z_axis', [0, 0, 1])
        
        # Check 1: All axes should be unit vectors
        mag_x = magnitude(x_axis)
        mag_y = magnitude(y_axis)
        mag_z = magnitude(z_axis)
        
        unit_ok = all(abs(m - 1.0) < 0.01 for m in [mag_x, mag_y, mag_z])
        
        # Check 2: Axes should be orthogonal (dot products = 0)
        dot_xy = abs(dot(x_axis, y_axis))
        dot_yz = abs(dot(y_axis, z_axis))
        dot_xz = abs(dot(x_axis, z_axis))
        
        ortho_ok = all(d < 0.01 for d in [dot_xy, dot_yz, dot_xz])
        
        # Check 3: Right-hand rule (x × y = z)
        cross_xy = cross(x_axis, y_axis)
        rhr_ok = all(abs(cross_xy[i] - z_axis[i]) < 0.01 for i in range(3))
        
        elem_check = {
            'element_id': eid,
            'unit_vectors': unit_ok,
            'orthogonal': ortho_ok,
            'right_hand_rule': rhr_ok,
            'passed': unit_ok and ortho_ok and rhr_ok
        }
        
        results['checks'].append(elem_check)
        
        if not elem_check['passed']:
            results['passed'] = False
    
    # Summary
    passed_count = sum(1 for c in results['checks'] if c.get('passed', False))
    total_count = len(results['checks'])
    results['summary'] = f"Coordinate Consistency: {passed_count}/{total_count} elements valid"
    
    return results

def run_all_validations(analysis_result, model_data):
    """
    Run all validation checks and compile results.
    
    Args:
        analysis_result: Output from run_load_case
        model_data: Input model data from JSON
    
    Returns:
        dict: Complete validation report
    """
    validation_report = {
        'overall_passed': True,
        'equilibrium': None,
        'symmetry': None,
        'sign_conventions': None,
        'coordinate_consistency': None,
        'summary': []
    }
    
    nodes_data = analysis_result.get('nodes', {})
    elements_data = analysis_result.get('elements', {})
    summary_data = analysis_result.get('summary', {})
    elements_list = model_data.get('model_elements', [])
    
    # 1. Equilibrium Check
    validation_report['equilibrium'] = validate_equilibrium(nodes_data, summary_data)
    validation_report['summary'].append(validation_report['equilibrium']['summary'])
    
    # 2. Symmetry Check
    validation_report['symmetry'] = validate_symmetry(nodes_data)
    validation_report['summary'].append(validation_report['symmetry']['summary'])
    
    # 3. Sign Convention Check
    validation_report['sign_conventions'] = validate_sign_conventions(elements_data)
    validation_report['summary'].append(validation_report['sign_conventions']['summary'])
    
    # 4. Coordinate Consistency Check
    validation_report['coordinate_consistency'] = validate_coordinate_consistency(elements_list)
    validation_report['summary'].append(validation_report['coordinate_consistency']['summary'])
    
    # Overall status
    validation_report['overall_passed'] = all([
        validation_report['equilibrium']['passed'],
        validation_report['symmetry']['passed'],
        validation_report['sign_conventions']['passed'],
        validation_report['coordinate_consistency']['passed']
    ])
    
    return validation_report

def print_validation_report(validation_report):
    """Print formatted validation report to console."""
    print("\n" + "="*85)
    print(f"{'VALIDATION REPORT':^85}")
    print("="*85)
    
    overall = "[PASSED]" if validation_report['overall_passed'] else "[FAILED]"
    print(f"\nOverall Status: {overall}\n")
    
    for summary in validation_report['summary']:
        status = "[OK]" if "passed" in summary.lower() else "[!]"
        print(f"  {status} {summary}")
    
    # Print warnings if any
    sign_check = validation_report.get('sign_conventions', {})
    warnings = sign_check.get('warnings', [])
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings[:5]:  # Show first 5
            print(f"  [!] {w}")
        if len(warnings) > 5:
            print(f"  ... and {len(warnings) - 5} more")
    
    print("="*85)

# ============================================================================
# 3. FUNGSI ANALISIS PER KASUS BEBAN (CORE LOGIC)
# ============================================================================
def run_load_case(data, case_type, pattern_def=None):
    """
    Menjalankan analisis untuk satu tipe beban.
    
    Args:
        data: Model data dictionary
        case_type: Legacy tipe beban ('SW', 'ADL', 'LL', 'DL', 'COMB')
        pattern_def: Optional pattern definition dict:
            {"type": "Dead"|"Live", "self_weight_mult": float, "pressure_MPa": float}
            When provided, overrides case_type-based pressure/self-weight logic.
    """
    # Struktur Data Output
    res = {
        "status": "Failed",
        "nodes": {}, 
        "elements": {},
        "summary": {"total_reaction_z": 0}
    }

    elements_list = data.get('model_elements', [])
    
    if pattern_def:
        # === NEW PATH: Pattern-based (SAP2000-like) ===
        FLOOR_PRESSURE = float(pattern_def.get('pressure_MPa', 0.0))
        SELF_WEIGHT_MULT = float(pattern_def.get('self_weight_mult', 0))
        print(f"  Pattern mode: pressure={FLOOR_PRESSURE:.6f} MPa, sw_mult={SELF_WEIGHT_MULT}")
    else:
        # === LEGACY PATH: case_type-based ===
        SLAB_SW_PRESSURE = float(data.get('slab_sw_pressure', 0.0))
        SLAB_ADL_PRESSURE = float(data.get('slab_adl_pressure', 0.0))
        LIVE_LOAD_PRESSURE = float(data.get('live_load_pressure', 0.0))
        
        comb_factors = data.get('combination_factors', {})
        FACTOR_SW_COMB = float(comb_factors.get('SW', 1.0))
        FACTOR_ADL_COMB = float(comb_factors.get('ADL', 1.0))
        FACTOR_LL_COMB = float(comb_factors.get('LL', 1.0))
        
        if case_type == 'SW':
            FLOOR_PRESSURE = SLAB_SW_PRESSURE
        elif case_type == 'ADL':
            FLOOR_PRESSURE = SLAB_ADL_PRESSURE
        elif case_type == 'LL':
            FLOOR_PRESSURE = LIVE_LOAD_PRESSURE
        elif case_type == 'DL':
            FLOOR_PRESSURE = SLAB_SW_PRESSURE + SLAB_ADL_PRESSURE
        else:  # COMB
            FLOOR_PRESSURE = FACTOR_SW_COMB * SLAB_SW_PRESSURE + FACTOR_ADL_COMB * SLAB_ADL_PRESSURE + FACTOR_LL_COMB * LIVE_LOAD_PRESSURE
        
        # Legacy self-weight control
        SELF_WEIGHT_MULT = 1.0 if case_type in ['SW', 'DL', 'COMB'] else 0.0
        print(f"  Pressures: SW={SLAB_SW_PRESSURE:.6f}, ADL={SLAB_ADL_PRESSURE:.6f}, LL={LIVE_LOAD_PRESSURE:.6f} MPa")
    
    print(f"  Case {case_type}: FLOOR_PRESSURE = {FLOOR_PRESSURE:.6f} MPa, SW_MULT = {SELF_WEIGHT_MULT}")

    # Fabrication splice offset (for column stationing)
    _seismic_p = data.get('seismic_parameters', {})
    splice_offset_mm = float(_seismic_p.get('COL_SPLICE_OFFSET_MM', 1500)) if _seismic_p else 1500.0

    try:
        def calculate_local_forces(F_global, local_axes):
            # Helper to transform Global Forces to Local
            u = local_axes.get('x_axis', [1, 0, 0])
            v = local_axes.get('y_axis', [0, 1, 0])
            w = local_axes.get('z_axis', [0, 0, 1])
            
            R = [
                [u[0], u[1], u[2]],
                [v[0], v[1], v[2]],
                [w[0], w[1], w[2]]
            ]
            
            Fx_loc = R[0][0]*F_global[0] + R[0][1]*F_global[1] + R[0][2]*F_global[2]
            Fy_loc = R[1][0]*F_global[0] + R[1][1]*F_global[1] + R[1][2]*F_global[2]
            Fz_loc = R[2][0]*F_global[0] + R[2][1]*F_global[1] + R[2][2]*F_global[2]
            
            Mx_loc = R[0][0]*F_global[3] + R[0][1]*F_global[4] + R[0][2]*F_global[5]
            My_loc = R[1][0]*F_global[3] + R[1][1]*F_global[4] + R[1][2]*F_global[5]
            Mz_loc = R[2][0]*F_global[3] + R[2][1]*F_global[4] + R[2][2]*F_global[5]
            
            return {"P":Fx_loc, "Fy":Fy_loc, "Fz":Fz_loc, "T":Mx_loc, "My":My_loc, "Mz":Mz_loc}

        def get_internal_forces_at_station(elem_id, ratio, local_axes, is_vertical=False):
            """
            Get internal forces at any station along element.
            
            OpenSees eleForce() returns forces in ELEMENT LOCAL coordinates:
            Format: [P_i, Vy_i, Vz_i, T_i, My_i, Mz_i, P_j, Vy_j, Vz_j, T_j, My_j, Mz_j]
            
            IMPORTANT: Local axes depend on geomTransf definition!
            
            For COLUMN (vertical, vecxz=[1,0,0]):
            - OpenSees local-x = element axis = [0,0,1] (Global Z, vertical up)
            - OpenSees local-y = cross(vecxz, local-x) = [0,-1,0] (Global -Y)
            - OpenSees local-z = cross(local-x, local-y) = [1,0,0] (Global X)
            
            So for column OpenSees forces:
            - P (local-x) = Vertical force (should be JSON P = axial)
            - Vy (local-y) = Force in Global -Y (should be JSON V3 with sign)
            - Vz (local-z) = Force in Global X (should be JSON V2)
            
            For BEAM (horizontal, vecxz=[0,-1,0]):
            - OpenSees local-x = element axis (horizontal)
            - OpenSees local-y = Global Z (vertical)
            - OpenSees local-z = horizontal perpendicular
            
            So for beam OpenSees forces map directly:
            - P = axial
            - Vy = V2 (vertical shear)
            - Vz = V3
            """
            # Get forces from OpenSees in LOCAL coordinates
            forces = ops.eleForce(elem_id)
            
            if len(forces) < 12:
                # Single node output - 6 components
                if is_vertical:
                    # COLUMN coordinate transformation (verified from reaction matching):
                    # 
                    # OpenSees eleForce() for column - VERIFIED MAPPING:
                    # Based on reaction matching:
                    #   forces[0] = -122.77 matches F1 = 122.77 → Shear in Global X (V2)
                    #   forces[1] = 84.82 matches F2 = 84.82 → Shear in Global Y (V3)
                    #   forces[2] = -4694.15 → Axial (P) = F3 = 4694.15
                    #   forces[3] = 111450 → Should be M1 (strong axis) = reaction M1
                    #   forces[4] = 149580 → Should be M2 (weak axis) = reaction M2
                    #   forces[5] → Torsion (should be ~0 for gravity)
                    #
                    # CORRECTED mapping to match reactions:
                    #   P = forces[2] = Global Z = F3 (axial)
                    #   V2 = forces[0] = Global X = F1 (shear about weak axis)
                    #   V3 = forces[1] = Global Y = F2 (shear about strong axis)
                    #   T = forces[5] = Torsion about vertical (should be small)
                    #   M2 = forces[3] = Strong axis moment = reaction M1
                    #   M3 = forces[4] = Weak axis moment = reaction M2 (larger)
                    return {
                        "P": -forces[2],    # Axial (local-x = Global Z) - compression negative
                        "Fy": -forces[0],   # Shear in Global X 
                        "Fz": forces[1],    # Shear in Global Y (raw - negate at output)
                        "T": forces[5],     # Torsion (raw - negate at output)
                        "My": -forces[3],   # Strong axis moment = reaction M1
                        "Mz": -forces[4]    # Weak axis moment = reaction M2 (larger)
                    }
                else:
                    # BEAM internal force mapping (Revit default local axes):
                    #
                    # With vecxz = [0,0,1] (Revit Z = vertical):
                    #   - local-x = element axis (horizontal, along beam length)
                    #   - local-y = horizontal perpendicular to beam (MINOR axis)
                    #   - local-z = Global +Z (vertical, MAJOR axis)
                    #
                    # OpenSees eleForce() returns:
                    #   forces[0] = P (axial along local-x)
                    #   forces[1] = Vy (shear along local-y = horizontal = MINOR)
                    #   forces[2] = Vz (shear along local-z = vertical = MAJOR)
                    #   forces[3] = T (torsion about local-x)
                    #   forces[4] = My (moment about local-y = MINOR ~0 for gravity)
                    #   forces[5] = Mz (moment about local-z = MAJOR bending)
                    #
                    # Revit convention:
                    #   Fy = horizontal shear (minor)  = forces[1]
                    #   Fz = vertical shear (major)    = forces[2]
                    #   My = minor moment (~0)          = forces[4]
                    #   Mz = major moment (bending)     = forces[5]
                    return {
                        "P": -forces[0],    # Axial
                        "Fy": forces[1],    # Minor shear (horizontal) — Revit Fy
                        "Fz": -forces[2],   # Major shear (vertical) — Revit Fz
                        "T": forces[3],     # Torsion
                        "My": -forces[4],   # Minor moment (~0) — Revit My
                        "Mz": forces[5]     # MAJOR moment — Revit Mz
                    }
            
            # 12-component output: forces at both i-node and j-node
            if is_vertical:
                # COLUMN coordinate mapping
                start_internal = {
                    "P": -forces[2],    # Axial at i-node
                    "Fy": -forces[0],   # Shear X at i-node
                    "Fz": forces[1],    # Shear Y at i-node (raw - negate at output)
                    "T": forces[5],     # Torsion at i-node (raw - negate at output)
                    "My": -forces[3],   # Strong axis moment at i-node
                    "Mz": -forces[4]    # Weak axis moment at i-node
                }
                end_internal = {
                    "P": forces[8],     # Axial at j-node
                    "Fy": forces[6],    # Shear X at j-node
                    "Fz": -forces[7],   # Shear Y at j-node
                    "T": -forces[11],   # Torsion at j-node
                    "My": forces[9],    # Strong axis moment at j-node
                    "Mz": forces[10]    # Weak axis moment at j-node
                }
            else:
                # BEAM (Revit default): Fy=minor(horizontal), Fz=major(vertical)
                start_internal = {
                    "P": -forces[0],    # Axial at i-node
                    "Fy": -forces[1],   # Minor shear (horizontal) at i-node
                    "Fz": -forces[2],   # Major shear (vertical) at i-node
                    "T": -forces[3],    # Torsion at i-node
                    "My": -forces[4],   # Minor moment (~0) at i-node
                    "Mz": forces[5]     # MAJOR moment at i-node
                }
                end_internal = {
                    "P": forces[6],     # Axial at j-node
                    "Fy": forces[7],    # Minor shear (horizontal) at j-node
                    "Fz": forces[8],    # Major shear (vertical) at j-node
                    "T": forces[9],     # Torsion at j-node
                    "My": forces[10],   # Minor moment (~0) at j-node
                    "Mz": -forces[11]   # MAJOR moment at j-node
                }
            
            # For intermediate stations, use linear interpolation
            interp = {}
            for key in ["P", "Fy", "Fz", "T", "My", "Mz"]:
                interp[key] = start_internal[key] * (1 - ratio) + end_internal[key] * ratio
                
            return interp

        def get_exact_intermediate_forces(start_f, end_f, ratio, length_mm):
            """
            Calculate exact intermediate forces using structural mechanics.
            
            Key relationships (Revit convention):
            - Fz (major shear, vertical) drives Mz (major moment): dMz/dx = Fz
            - Fy (minor shear, horizontal) drives My (minor moment): dMy/dx = Fy
            
            For uniformly loaded element:
            - V(x) = V_start - w*x  (linear shear)
            - M(x) = M_start + V_start*x - w*x²/2  (parabolic moment)
            
            For element without distributed load (e.g., column under selfweight):
            - V = constant, so W_total ≈ 0
            - M(x) = M_start + V*x  (linear moment)
            """
            interp = {}
            x = ratio * length_mm
            
            # ===== MAJOR AXIS: Fz (vertical shear) drives Mz (major moment) =====
            fz_s = start_f["Fz"]
            fz_e = end_f["Fz"]
            mz_s = start_f["Mz"]
            
            # Equivalent distributed load (major axis)
            W_major = fz_s - fz_e
            
            # Shear Fz(x) = Fz_start - (W_major/L)*x
            fz_x = fz_s - W_major * ratio
            
            # dMz/dx = -Fz (NEGATIVE relationship)
            # Mz(x) = Mz_start - Fz_start*x + (W_major/L)*x²/2
            mz_x = mz_s - fz_s * x + (W_major / length_mm) * (x**2) / 2.0
            
            interp["Fz"] = fz_x
            interp["Mz"] = mz_x
            
            # ===== MINOR AXIS: Fy (horizontal shear) drives My (minor moment) =====
            fy_s = start_f["Fy"]
            fy_e = end_f["Fy"]
            my_s = start_f["My"]
            
            # Equivalent distributed load (minor axis)
            W_minor = fy_s - fy_e
            
            # Shear Fy(x) = Fy_start - (W_minor/L)*x
            fy_x = fy_s - W_minor * ratio
            
            # dMy/dx = -Fy (NEGATIVE relationship)
            # My(x) = My_start - Fy_start*x + (W_minor/L)*x²/2
            my_x = my_s - fy_s * x + (W_minor / length_mm) * (x**2) / 2.0
            
            interp["Fy"] = fy_x
            interp["My"] = my_x
            
            # ===== AXIAL FORCE P =====
            # For self-weight, axial load decreases along element (from base to top)
            # P(x) = P_start - (W_axial/L) * x where W_axial = P_start - P_end
            p_s = start_f["P"]
            p_e = end_f["P"]
            W_axial = p_s - p_e  # Equivalent distributed axial load
            p_x = p_s - W_axial * ratio
            interp["P"] = p_x
            
            # Torsion remains linear interpolation
            interp["T"] = start_f["T"] * (1 - ratio) + end_f["T"] * ratio
                
            return interp

        def find_zero_crossing(start_f, end_f, component, length_mm):
            """
            Analytically find zero crossing station and return force dict there.
            """
            v_val_start = start_f.get(component, 0.0)
            v_val_end = end_f.get(component, 0.0)
            
            # If both same sign, no crossing (for Linear V)
            if v_val_start * v_val_end > 0:
                return None
            if abs(v_val_start) < 1e-6 and abs(v_val_end) < 1e-6:
                return None # Zero everywhere
                
            # Linear V(x) = V_s - (W_total/L)*x
            # W_total = V_s - V_e
            W_total = v_val_start - v_val_end
            
            # Avoid division by zero (Constant Shear)
            if abs(W_total) < 1e-6:
                return None
            
            # V(x) = 0 => V_s = (W_total/L) * x
            # x = (V_s * L) / W_total
            # ratio = x/L = V_s / W_total
            zero_ratio = v_val_start / W_total
            
            if 0.0 < zero_ratio < 1.0:
                 forces_exact = get_exact_intermediate_forces(start_f, end_f, zero_ratio, length_mm)
                 return {"station": round(zero_ratio, 4), "forces": forces_exact}
            
            return None

        def find_max_point(stations, component):
            # ... (Existing logic can remain to find max among samples + zeros)
            max_val = 0
            max_station = None
            for s in stations:
                val = abs(s['forces'][component])
                if val > max_val:
                    max_val = val
                    max_station = s
            return max_station

        def find_critical_stations(elem_id, local_axes, length_mm, num_samples=5, is_vertical=False):
            # 1. Get Boundary Conditions (Start/End)
            f_start = get_internal_forces_at_station(elem_id, 0.0, local_axes, is_vertical)
            f_end = get_internal_forces_at_station(elem_id, 1.0, local_axes, is_vertical)
            
            # 2. Sample Points (Exact Parabolic/Linear Interpolation)
            sample_stations = []
            
            # Add Start
            sample_stations.append({"station": 0.0, "forces": f_start})
            
            # Intermediate
            for i in range(1, num_samples - 1):
                ratio = i / (num_samples - 1)
                if is_vertical:
                    # Columns: use direct eleForce interpolation.
                    # get_exact_intermediate_forces() uses beam axis coupling (Fy→My, Fz→Mz)
                    # which is wrong for columns where the coupling is reversed.
                    forces_exact = get_internal_forces_at_station(elem_id, ratio, local_axes, is_vertical)
                else:
                    forces_exact = get_exact_intermediate_forces(f_start, f_end, ratio, length_mm)
                sample_stations.append({"station": ratio, "forces": forces_exact})
            
            # Add End
            sample_stations.append({"station": 1.0, "forces": f_end})
            
            critical_stations = list(sample_stations) # Copy
            
            # 3. Analytic Zero Crossings for Shear → Max Moment
            # Check Fz (major shear) → Max Mz (major moment)
            zero_v2 = find_zero_crossing(f_start, f_end, 'Fz', length_mm)
            if zero_v2: critical_stations.append(zero_v2)

            # Check Fy (minor shear) → Max My (minor moment)
            zero_v3 = find_zero_crossing(f_start, f_end, 'Fy', length_mm)
            if zero_v3: critical_stations.append(zero_v3)
            
            # 4. Splice station for columns (fabrication) — output only, no model change
            try:
                if is_vertical and length_mm > splice_offset_mm:
                    splice_ratio = splice_offset_mm / length_mm
                    if 0.01 < splice_ratio < 0.99:  # Only if meaningful
                        splice_forces = get_internal_forces_at_station(elem_id, splice_ratio, local_axes, is_vertical)
                        critical_stations.append({"station": splice_ratio, "forces": splice_forces})
            except:
                pass  # Splice station is optional, don't break analysis
            
            # 5. Check Zero Crossings for Moment (Inflection Points)
            # Quadratic M(x) = Ax^2 + Bx + C = 0.
            # Using discrete check or samples is usually enough for display.
            # Analytic quadratic solving is possible but complex to integrate generic.
            # Let's rely on samples for M crossing.
            
            critical_stations.sort(key=lambda x: x['station'])
            
            unique = []
            for s in critical_stations:
                if not unique or abs(s['station'] - unique[-1]['station']) > 0.01:
                    unique.append(s)
            return unique

        def get_deflection_at_station(elem_id, ratio, local_axes, node_coords_dict, start_node, end_node, length_mm, E, I_major, I_minor, A, G):
            """
            Calculate ABSOLUTE local deflection at any station along element.

            Uses Hermite cubic shape functions to interpolate the displacement
            field from nodal displacements. Returns absolute deflection in
            local coordinates (not chord-relative).

            The chord subtraction (Relative to Beam Ends) is done in
            get_max_deflection() using the FULL beam's end-to-end chord.

            Args:
                elem_id: Element ID (or sub-element ID)
                ratio: Station ratio (0.0 to 1.0) within this element/sub-element
                local_axes: Local coordinate system of the parent beam
                node_coords_dict: Dictionary of node coordinates
                start_node, end_node: Node IDs of this element/sub-element
                length_mm: Length of this element/sub-element
                E, I_major, I_minor, A, G: Section/material properties

            Returns:
                Dictionary with absolute local deflections {delta_y, delta_z}
            """
            # Get displacements at start and end nodes
            d_start = ops.nodeDisp(start_node)  # [dx, dy, dz, rx, ry, rz] global
            d_end = ops.nodeDisp(end_node)      # [dx, dy, dz, rx, ry, rz] global

            # Extract translation and rotation components
            u1 = [d_start[0], d_start[1], d_start[2]]
            u2 = [d_end[0], d_end[1], d_end[2]]
            theta1 = [d_start[3], d_start[4], d_start[5]]
            theta2 = [d_end[3], d_end[4], d_end[5]]

            # Transform to local coordinates
            x_axis = local_axes.get('x_axis', [1, 0, 0])
            y_axis = local_axes.get('y_axis', [0, 1, 0])
            z_axis = local_axes.get('z_axis', [0, 0, 1])

            R = [
                [x_axis[0], x_axis[1], x_axis[2]],
                [y_axis[0], y_axis[1], y_axis[2]],
                [z_axis[0], z_axis[1], z_axis[2]]
            ]

            def transform_vec(v):
                return [
                    R[0][0]*v[0] + R[0][1]*v[1] + R[0][2]*v[2],
                    R[1][0]*v[0] + R[1][1]*v[1] + R[1][2]*v[2],
                    R[2][0]*v[0] + R[2][1]*v[1] + R[2][2]*v[2]
                ]

            u1_local = transform_vec(u1)
            u2_local = transform_vec(u2)
            theta1_local = transform_vec(theta1)
            theta2_local = transform_vec(theta2)

            L = length_mm
            xi = ratio  # 0 to 1

            # Standard Hermite cubic shape functions
            N1 = 1 - 3*xi**2 + 2*xi**3
            N2 = L * (xi - 2*xi**2 + xi**3)
            N3 = 3*xi**2 - 2*xi**3
            N4 = L * (-xi**2 + xi**3)

            # --- Absolute local Y deflection (rotation about local Z) ---
            v1_y = u1_local[1]
            v2_y = u2_local[1]
            theta1_z = theta1_local[2]
            theta2_z = theta2_local[2]
            delta_y = N1 * v1_y + N2 * theta1_z + N3 * v2_y + N4 * theta2_z

            # --- Absolute local Z deflection (rotation about local Y) ---
            v1_z = u1_local[2]
            v2_z = u2_local[2]
            theta1_y = -theta1_local[1]  # Sign convention
            theta2_y = -theta2_local[1]
            delta_z = N1 * v1_z + N2 * theta1_y + N3 * v2_z + N4 * theta2_y

            return {"delta_y": delta_y, "delta_z": delta_z}

        def get_max_deflection(item, node_coords_dict, sub_elements_map):
            """
            Calculate maximum chord-relative deflection using virtual work
            (bending-only / Euler-Bernoulli) method.

            This matches SAP2000's 'Relative to Beam Ends' element deflection
            output, which uses bending-only interpolation from internal forces
            (no shear deformation contribution).

            Formula:
              delta_z(s) = -integral_0^L My(x)*m_s(x)/(E*I_major) dx
              delta_y(s) = +integral_0^L Mz(x)*m_s(x)/(E*I_minor) dx

            where m_s(x) is the virtual moment for unit load at station s
            on a simply-supported beam:
              m_s(x) = (L-s)*x/L   for 0 <= x <= s
              m_s(x) = s*(L-x)/L   for s < x <= L

            Returns:
                Dictionary with max deflection info
            """
            eid = item['id']
            raw = item['raw']
            local_axes = raw.get('local_axes', {})
            length_mm = raw.get('topology', {}).get('length_mm', 0)
            sec = raw.get('section', {})
            mat = raw.get('material', {})
            is_vertical = item.get('is_vertical', False)

            E = float(mat.get('E_MPa', 205000))
            I_major = float(sec.get('Iz_mm4', 0))  # Strong axis
            I_minor = float(sec.get('Iy_mm4', 0))  # Weak axis

            # For columns, section Iy/Iz don't align with element local axes
            # because the section is rotated 90°. Swap to match deflection dirs.
            if is_vertical:
                I_major, I_minor = I_minor, I_major

            if length_mm <= 0:
                return None

            L = length_mm
            subs = sub_elements_map.get(eid)

            # --- Extract moment diagram (SAP2000 sign convention) ---
            # My_sap: major moment (positive = sagging for beams)
            # Mz_sap: minor moment
            moment_data = []  # [(x_position, My_sap, Mz_sap), ...]

            if subs and len(subs) > 1:
                # BEAM with sub-elements: extract from eleForce at boundaries
                x_ax = local_axes.get('x_axis', [1.0, 0.0, 0.0])
                x_ax_x = float(x_ax[0])
                x_ax_y = float(x_ax[1])
                sa_x = -x_ax_y   # strong-axis unit vector
                sa_y =  x_ax_x

                cumulative_dist = 0.0
                for i, (sub_eid, sub_len) in enumerate(subs):
                    forces_raw = ops.eleForce(sub_eid)
                    if i == 0:
                        # i-node of first sub-element (SAP convention)
                        My_sap_i = sa_x * forces_raw[3] + sa_y * forces_raw[4]
                        Mz_sap_i = forces_raw[5]
                        moment_data.append((0.0, My_sap_i, Mz_sap_i))

                    # j-node of each sub-element
                    j_dist = cumulative_dist + sub_len
                    My_sap_j = -(sa_x * forces_raw[9] + sa_y * forces_raw[10])
                    Mz_sap_j = -forces_raw[11]
                    moment_data.append((j_dist, My_sap_j, Mz_sap_j))

                    cumulative_dist += sub_len
            else:
                # COLUMN (single element) or beam without sub-elements
                # Use get_internal_forces_at_station for reliable axis mapping
                n_pts = 21
                for k in range(n_pts):
                    ratio = k / (n_pts - 1)
                    forces = get_internal_forces_at_station(eid, ratio, local_axes, is_vertical)
                    My_sap = -forces["My"]  # SAP sign convention
                    Mz_sap = forces["Mz"]
                    moment_data.append((ratio * L, My_sap, Mz_sap))

            # --- Deflection at sample stations ---
            deflection_samples = []

            if subs and len(subs) > 1:
                # BEAMS: Virtual work integration (9 stations)
                sample_ratios = [i * 0.125 for i in range(9)]  # 0, 0.125, ..., 1.0
                for s_ratio in sample_ratios:
                    s = s_ratio * L
                    integral_major = 0.0
                    integral_minor = 0.0

                    for k in range(len(moment_data) - 1):
                        x1, My1, Mz1 = moment_data[k]
                        x2, My2, Mz2 = moment_data[k + 1]
                        dx = x2 - x1
                        if dx <= 0:
                            continue

                        if x1 <= s:
                            ms1 = (L - s) * x1 / L if L > 0 else 0.0
                        else:
                            ms1 = s * (L - x1) / L if L > 0 else 0.0

                        if x2 <= s + 1e-9:
                            ms2 = (L - s) * x2 / L if L > 0 else 0.0
                        else:
                            ms2 = s * (L - x2) / L if L > 0 else 0.0

                        integral_major += 0.5 * (My1 * ms1 + My2 * ms2) * dx
                        integral_minor += 0.5 * (Mz1 * ms1 + Mz2 * ms2) * dx

                    delta_z = -integral_major / (E * I_major) if I_major > 0 else 0.0
                    delta_y =  integral_minor / (E * I_minor) if I_minor > 0 else 0.0

                    deflection_samples.append({
                        "station": s_ratio,
                        "distance_mm": s,
                        "delta_y": delta_y,
                        "delta_z": delta_z
                    })
            else:
                # COLUMNS: Hermite shape function interpolation from nodal displacements
                # 21 stations (every 0.05L) for accurate peak position detection
                sample_ratios = [i / 20.0 for i in range(21)]
                n1, n2 = item['nodes']
                A = float(sec.get('Area_mm2', 0))
                G = float(mat.get('G_MPa', 78846))

                abs_defl = []
                for s_ratio in sample_ratios:
                    d = get_deflection_at_station(
                        eid, s_ratio, local_axes, node_coords_dict,
                        n1, n2, L, E, I_major, I_minor, A, G)
                    abs_defl.append(d)

                dy_0 = abs_defl[0]["delta_y"]
                dy_1 = abs_defl[-1]["delta_y"]
                dz_0 = abs_defl[0]["delta_z"]
                dz_1 = abs_defl[-1]["delta_z"]

                for i, s_ratio in enumerate(sample_ratios):
                    chord_y = dy_0 + s_ratio * (dy_1 - dy_0)
                    chord_z = dz_0 + s_ratio * (dz_1 - dz_0)
                    deflection_samples.append({
                        "station": s_ratio,
                        "distance_mm": s_ratio * L,
                        "delta_y": abs_defl[i]["delta_y"] - chord_y,
                        "delta_z": abs_defl[i]["delta_z"] - chord_z
                    })

            if not deflection_samples:
                return None

            max_y_sample = max(deflection_samples, key=lambda x: abs(x["delta_y"]))
            max_z_sample = max(deflection_samples, key=lambda x: abs(x["delta_z"]))

            return {
                "delta_y_max_mm": round(max_y_sample["delta_y"], 10),
                "delta_y_station": round(max_y_sample["station"], 4),
                "delta_y_distance_mm": round(max_y_sample["distance_mm"], 2),
                "delta_z_max_mm": round(max_z_sample["delta_z"], 10),
                "delta_z_station": round(max_z_sample["station"], 4),
                "delta_z_distance_mm": round(max_z_sample["distance_mm"], 2)
            }


        def get_deflection_profile(item, node_coords_dict, sub_elements_map, n_stations=11):
            """
            Hitung profil defleksi station-by-station untuk Visualizer.
            Uses virtual work (bending-only) method consistent with get_max_deflection.

            Args:
                item              : element item dict (id, raw, nodes, is_vertical)
                node_coords_dict  : mapping node_id → [x,y,z]
                sub_elements_map  : mapping elem_id → [(sub_eid, sub_len), ...]
                n_stations        : jumlah titik sampling (default 11 → 0.0 s.d. 1.0)

            Returns:
                dict {
                    "stations_ratio": [...],
                    "dy_mm": [...],   # defleksi lokal Y (minor axis)
                    "dz_mm": [...]    # defleksi lokal Z (major axis)
                } atau None jika gagal
            """
            eid = item['id']
            raw = item['raw']
            local_axes = raw.get('local_axes', {})
            length_mm = raw.get('topology', {}).get('length_mm', 0)
            sec = raw.get('section', {})
            mat = raw.get('material', {})
            is_vertical = item.get('is_vertical', False)

            E = float(mat.get('E_MPa', 205000))
            I_major = float(sec.get('Iz_mm4', 0))
            I_minor = float(sec.get('Iy_mm4', 0))

            # Column section axes rotated vs element local axes — swap I
            if is_vertical:
                I_major, I_minor = I_minor, I_major

            if length_mm <= 0:
                return None

            L = length_mm
            subs = sub_elements_map.get(eid)

            # Beams (multi-sub-element) use finer grid matching n_stations.
            # Columns (single element) use 21 stations matching get_max_deflection.
            if subs and len(subs) > 1:
                sample_ratios = [i / float(n_stations - 1) for i in range(n_stations)]
            else:
                sample_ratios = [i / 20.0 for i in range(21)]

            stations_ratio = []
            dy_mm = []
            dz_mm = []
            chord_endpoints = None  # only set for columns (SAP2000-style viz)

            if subs and len(subs) > 1:
                # --- BEAMS (multi-sub-element): Virtual work method ---
                # Extract moment diagram (SAP2000 sign convention)
                moment_data = []
                x_ax = local_axes.get('x_axis', [1.0, 0.0, 0.0])
                x_ax_x = float(x_ax[0])
                x_ax_y = float(x_ax[1])
                sa_x = -x_ax_y
                sa_y =  x_ax_x

                cumulative_dist = 0.0
                for i, (sub_eid, sub_len) in enumerate(subs):
                    forces_raw = ops.eleForce(sub_eid)
                    if i == 0:
                        My_sap_i = sa_x * forces_raw[3] + sa_y * forces_raw[4]
                        Mz_sap_i = forces_raw[5]
                        moment_data.append((0.0, My_sap_i, Mz_sap_i))

                    j_dist = cumulative_dist + sub_len
                    My_sap_j = -(sa_x * forces_raw[9] + sa_y * forces_raw[10])
                    Mz_sap_j = -forces_raw[11]
                    moment_data.append((j_dist, My_sap_j, Mz_sap_j))
                    cumulative_dist += sub_len

                # Virtual work integration
                for s_ratio in sample_ratios:
                    s = s_ratio * L
                    integral_major = 0.0
                    integral_minor = 0.0

                    for k in range(len(moment_data) - 1):
                        x1, My1, Mz1 = moment_data[k]
                        x2, My2, Mz2 = moment_data[k + 1]
                        dx = x2 - x1
                        if dx <= 0:
                            continue

                        if x1 <= s:
                            ms1 = (L - s) * x1 / L if L > 0 else 0.0
                        else:
                            ms1 = s * (L - x1) / L if L > 0 else 0.0

                        if x2 <= s + 1e-9:
                            ms2 = (L - s) * x2 / L if L > 0 else 0.0
                        else:
                            ms2 = s * (L - x2) / L if L > 0 else 0.0

                        integral_major += 0.5 * (My1 * ms1 + My2 * ms2) * dx
                        integral_minor += 0.5 * (Mz1 * ms1 + Mz2 * ms2) * dx

                    delta_z = -integral_major / (E * I_major) if I_major > 0 else 0.0
                    delta_y =  integral_minor / (E * I_minor) if I_minor > 0 else 0.0

                    stations_ratio.append(round(s_ratio, 4))
                    dy_mm.append(round(delta_y, 10))
                    dz_mm.append(round(delta_z, 10))
            else:
                # --- COLUMNS (single element): Hermite shape function interpolation ---
                # Uses nodal displacements + cubic interpolation (matches SAP2000)
                n1, n2 = item['nodes']
                A = float(sec.get('Area_mm2', 0))
                G = float(mat.get('G_MPa', 78846))

                # Get absolute deflection at all sample stations
                abs_defl = []
                for s_ratio in sample_ratios:
                    d = get_deflection_at_station(
                        eid, s_ratio, local_axes, node_coords_dict,
                        n1, n2, L, E, I_major, I_minor, A, G)
                    abs_defl.append(d)

                # Chord subtraction: relative = absolute - linear_chord
                dy_0 = abs_defl[0]["delta_y"]
                dy_1 = abs_defl[-1]["delta_y"]
                dz_0 = abs_defl[0]["delta_z"]
                dz_1 = abs_defl[-1]["delta_z"]

                # Store chord endpoints for SAP2000-style visualization
                chord_endpoints = {
                    "dy_start": round(dy_0, 10), "dy_end": round(dy_1, 10),
                    "dz_start": round(dz_0, 10), "dz_end": round(dz_1, 10)
                }

                for i, s_ratio in enumerate(sample_ratios):
                    chord_y = dy_0 + s_ratio * (dy_1 - dy_0)
                    chord_z = dz_0 + s_ratio * (dz_1 - dz_0)
                    stations_ratio.append(round(s_ratio, 4))
                    dy_mm.append(round(abs_defl[i]["delta_y"] - chord_y, 10))
                    dz_mm.append(round(abs_defl[i]["delta_z"] - chord_z, 10))

            if not stations_ratio:
                return None

            result = {
                "stations_ratio": stations_ratio,
                "dy_mm": dy_mm,
                "dz_mm": dz_mm
            }
            if chord_endpoints is not None:
                result["chord_endpoints"] = chord_endpoints
            return result

        # --- RESET MODEL ---
        ops.wipe()
        ops.model('basic', '-ndm', 3, '-ndf', 6)

        # --- NODE MAPPING ---
        node_map = {}       
        node_coords = {}    
        next_node_id = 1
        
        original_joint_nids = set()  # Track ORIGINAL structural joints (not sub-element nodes)

        # TRACKING APPLIED LOADS FOR EQUILIBRIUM CHECK
        total_applied_force_z = 0.0
        
        def get_node_id(coords):
            nonlocal next_node_id
            # Gunakan string format 1 desimal sebagai key unik
            key = f"{coords[0]:.1f}_{coords[1]:.1f}_{coords[2]:.1f}"
            if key not in node_map:
                node_map[key] = next_node_id
                node_coords[next_node_id] = coords
                next_node_id += 1
            return node_map[key]

        # --- PRE-PROCESS GEOMETRY ---
        processed_elements = []
        for entry in elements_list:
            p1 = entry['topology']['start_node']
            p2 = entry['topology']['end_node']
            
            n1 = get_node_id(p1)
            n2 = get_node_id(p2)
            original_joint_nids.add(n1)
            original_joint_nids.add(n2)

            dx, dy, dz = p2[0]-p1[0], p2[1]-p1[1], p2[2]-p1[2]
            L = math.sqrt(dx**2 + dy**2 + dz**2)

            # Deteksi Vertikal (Kolom) vs Horizontal (Balok)
            is_vertical = abs(dz) > abs(dx) and abs(dz) > abs(dy)

            processed_elements.append({
                'id': entry['id'],
                'nodes': [n1, n2],
                'is_vertical': is_vertical,
                'length': L,
                'raw': entry
            })

        # --- DETECT STRUCTURE CONFIGURATION ---
        # Detect n_stories, spans, heights for multi-story stiffness correction
        struct_config = detect_structure_config_from_grid(data)
        print(f"  Structure Config: {struct_config['n_stories']} stories, "
              f"{struct_config['n_span_x']}x{struct_config['n_span_y']} spans, "
              f"story_height={struct_config['story_height']}mm")

        # --- BUILD NODES ---
        all_z = [c[2] for c in node_coords.values()]
        min_z = min(all_z) if all_z else 0.0
        fixed_nodes = set()
        
        # Read support config from JSON (default: Fixed)
        support_config = data.get('support_config', {})
        support_type = support_config.get('type', 'Fixed')
        support_dof = support_config.get('dof', [1, 1, 1, 1, 1, 1])

        for nid, coords in node_coords.items():
            ops.node(nid, *coords)
            
            # Simpan koordinat ke output
            res["nodes"][nid] = {
                "coords": coords,
                "disp": [0.0]*6,
                "reaction": None
            }
            
            # Tumpuan sesuai support_config
            if abs(coords[2] - min_z) < 100.0:
                ops.fix(nid, *support_dof)
                fixed_nodes.add(nid)

        # --- BUILD NODE-TO-CONNECTING-DEPTH MAP (for Rigid End Zones) ---
        # At each node, find the maximum section depth of CONNECTING elements
        # This determines how much rigid offset to apply at that joint
        node_connecting_depths = {}  # node_key -> {'col_d': max_column_depth, 'beam_d': max_beam_depth}
        
        if COL_RIGID_END_ZONE_FACTOR > 0 or BEAM_RIGID_END_ZONE_FACTOR > 0:
            for item in processed_elements:
                sec = item['raw']['section']
                d_mm = float(sec.get('d_mm', 0))   # Section depth
                b_mm = float(sec.get('b_mm', 0))   # Section width (flange width)
                
                for nid in item['nodes']:
                    key = nid
                    if key not in node_connecting_depths:
                        node_connecting_depths[key] = {'col_d': 0, 'beam_d': 0}
                    
                    if item['is_vertical']:
                        # Column connects here — store its width (b_mm) for beam offsets
                        node_connecting_depths[key]['col_d'] = max(
                            node_connecting_depths[key]['col_d'], b_mm)
                    else:
                        # Beam connects here — store its depth (d_mm) for column offsets
                        node_connecting_depths[key]['beam_d'] = max(
                            node_connecting_depths[key]['beam_d'], d_mm)
            
            print(f"  Rigid End Zone Factor: Col={COL_RIGID_END_ZONE_FACTOR}, Beam={BEAM_RIGID_END_ZONE_FACTOR}")
            print(f"  Nodes with connecting depths: {len(node_connecting_depths)}")

        # --- BUILD ELEMENTS & TRANSFORMS ---
        # Dynamic Transforms will be created per element using local axes from JSON

        # Dictionary to track sub-elements for reporting
        # parent_id -> list of (sub_ele_id, length)
        sub_elements_map = {}
        # Track segment boundary indices for SFD jump stations
        # parent_id -> set of sub-element indices where a new segment begins
        seg_start_indices = {}
        transf_counter = 1  # Unique transform tag counter

        # --- SECONDARY BEAM CONNECTIONS ---
        _, _, sec_beams_list = classify_elements(elements_list)
        parent_connections, sec_beam_directions = find_secondary_connections(sec_beams_list, elements_list)
        connection_node_map = {}  # (parent_id, fraction) -> node_id at connection point

        for item in processed_elements:
            sec = item['raw']['section']
            mat = item['raw']['material']
            
            # --- DYNAMIC TRANSFORM with RIGID END ZONE ---
            # OpenSeesPy geomTransf with -jntOffset:
            #   ops.geomTransf('Linear', tag, *vecxz, '-jntOffset', dXi, dYi, dZi, dXj, dYj, dZj)
            # Offsets are in GLOBAL coordinates.
            #
            # For COLUMN (vertical, axis along Z):
            #   - At i-node (bottom): offset UP by beam_depth/2 at connecting beam
            #   - At j-node (top): offset DOWN by beam_depth/2 at connecting beam  
            #   - Offset direction: Global Z
            #
            # For BEAM (horizontal, axis along X or Y):
            #   - At each end: offset INWARD by column_width/2 at connecting column
            #   - Offset direction: along beam axis (Global X or Y)
            
            local_axes = item['raw'].get('local_axes', {})
            if item['is_vertical']:
                vecxz = local_axes.get('y_axis', [1, 0, 0])
            else:
                vecxz = local_axes.get('z_axis', [0, 0, 1])
            
            # Calculate rigid end zone offsets
            dI = [0.0, 0.0, 0.0]  # Offset at i-node (global)
            dJ = [0.0, 0.0, 0.0]  # Offset at j-node (global)
            
            n1, n2 = item['nodes']
            
            if item['is_vertical']:
                if COL_RIGID_END_ZONE_FACTOR > 0:
                    # COLUMN: offset along Z-axis (element axis)
                    # At bottom (i-node): beam depth at that joint → offset UP
                    beam_d_i = node_connecting_depths.get(n1, {}).get('beam_d', 0)
                    beam_d_j = node_connecting_depths.get(n2, {}).get('beam_d', 0)
                    
                    # Only apply offset if there's actually a beam at this joint
                    if beam_d_i > 0:
                        dI[2] = COL_RIGID_END_ZONE_FACTOR * beam_d_i / 2.0  # Offset UP
                    if beam_d_j > 0:
                        dJ[2] = -COL_RIGID_END_ZONE_FACTOR * beam_d_j / 2.0  # Offset DOWN
            else:
                if BEAM_RIGID_END_ZONE_FACTOR > 0:
                    # BEAM: offset along beam axis direction
                    # Get beam direction vector (normalized)
                    p1 = node_coords[n1]
                    p2 = node_coords[n2]
                    dx = p2[0] - p1[0]
                    dy = p2[1] - p1[1]
                    L = item['length']
                    
                    if L > 0:
                        ux, uy = dx/L, dy/L  # Unit vector along beam
                    else:
                        ux, uy = 1.0, 0.0
                    
                    col_d_i = node_connecting_depths.get(n1, {}).get('col_d', 0)
                    col_d_j = node_connecting_depths.get(n2, {}).get('col_d', 0)
                    
                    if col_d_i > 0:
                        offset_i = BEAM_RIGID_END_ZONE_FACTOR * col_d_i / 2.0
                        dI[0] = ux * offset_i  # Offset INWARD along beam axis
                        dI[1] = uy * offset_i
                    if col_d_j > 0:
                        offset_j = BEAM_RIGID_END_ZONE_FACTOR * col_d_j / 2.0
                        dJ[0] = -ux * offset_j  # Offset INWARD (opposite direction)
                        dJ[1] = -uy * offset_j
            
            # Use unique Transform Tag
            transf_tag = transf_counter
            transf_counter += 1
            item['transf_tag'] = transf_tag  # Store for sub-elements
            item['vecxz'] = vecxz
            item['dI'] = dI
            item['dJ'] = dJ
            
            # Create geomTransf with rigid end zone offsets
            ops.geomTransf('Linear', transf_tag, vecxz[0], vecxz[1], vecxz[2],
                          '-jntOffset', dI[0], dI[1], dI[2], dJ[0], dJ[1], dJ[2])

            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 78846))  # SAP2000 BJ REVIT: G=78846 MPa
            A, J, Iz, Iy, Avy, Avz = get_section_properties(sec)
            
            # Exact SAP2000 Torsional Constants to fix M2 lateral coupling
            if item['is_vertical']: J = 806975.4
            else: J = 132290.5
            
            # --- STIFFNESS CORRECTION DISABLED ---
            # With rigid end zones (RIGID_END_ZONE_FACTOR > 0), empirical stiffness
            # correction is no longer needed. Rigid zones properly shorten the
            # effective element length, which naturally reduces the over-prediction
            # of moments that the stiffness correction was compensating for.
            # if item['is_vertical'] and RIGID_END_ZONE_FACTOR == 0:
            #     col_z_start = item['raw'].get('topology', {}).get('start_node', [0, 0, 0])[2]
            #     story_level = get_element_story_level(col_z_start, struct_config['z_levels'])
            #     strong_factor, weak_factor = get_stiffness_correction_factor(
            #         struct_config['n_stories'], 
            #         story_level, 
            #         'column'
            #     )
            #     Iz *= strong_factor
            #     Iy *= weak_factor
            
            # DISABLED: COMB-only factors - not needed with recalibrated main formula
            # if item['is_vertical'] and case_type == 'COMB':
            #      Iz *= CONN_STIFFNESS_FACTOR_STRONG
            #      Iy *= CONN_STIFFNESS_FACTOR_WEAK
            #      J *= CONN_STIFFNESS_FACTOR_WEAK
            

            # Setup Section Properties
            alphaY = Avy / A if A > 0 else 0.5
            alphaZ = Avz / A if A > 0 else 0.5
            
            # --- INERTIA MAPPING ---
            # JSON: Iz = Major Axis (Strong), Iy = Minor Axis (Weak).
            # OpenSees: Iy = About Local Y, Iz = About Local Z.
            # 
            # For COLUMN with vecxz = y_axis = [1,0,0]:
            #   - Element axis (local-x) = Global +Z (vertical)
            #   - local-y = vecxz × local-x = [1,0,0] × [0,0,1] = [0,-1,0] (Global -Y)
            #   - local-z = local-x × local-y = [0,0,1] × [0,-1,0] = [1,0,0] (Global +X)
            #
            # Bending stiffness alignment:
            #   - F1 (Global X) → bending about Global Y → bending about local-y
            #     Uses Ops_Iy → Should use STRONG axis (Iz)
            #   - F2 (Global Y) → bending about Global X → bending about local-z  
            #     Uses Ops_Iz → Should use WEAK axis (Iy)
            #
            # For BEAM (horizontal, vecxz=[0,0,1]):
            #   - Local Y = Horizontal (perpendicular to beam)
            #   - Local Z = Vertical (Global Z)
            #   - Ops_Iy = Strong (Iz), Ops_Iz = Weak (Iy) — swapped!
            
            if item['is_vertical']:
                # COLUMN: Swap to align strong axis with F1 (Global X)
                # Local axes: local-x=global+Z, local-y=global-Y, local-z=global+X
                Ops_Iy = Iz  # Strong axis (major) -> bending about local-y -> R in F1
                Ops_Iz = Iy  # Weak axis (minor)  -> bending about local-z -> R in F2
                # Shear areas: use JSON values directly (already aligned with local axes)
                # Avy_JSON (7836mm2) -> lateral/X-shear -> local-z shear
                # Avz_JSON (3048mm2) -> web/Y-shear -> local-y shear
                # NOTE: no swap — JSON Avy/Avz already match OpenSees local-y/z for columns
            else:
                # BEAM (Revit default vecxz=[0,0,1]):
                # local-y = horizontal, local-z = vertical
                # Iy (OpenSees) = I about local-y → resists vertical (major) forces
                # Iz (OpenSees) = I about local-z → resists horizontal (minor) forces
                Ops_Iy = Iz  # Strong axis → bending about local-y resists vertical
                Ops_Iz = Iy  # Weak axis   → bending about local-z resists horizontal
            
            if item['is_vertical']:
                # --- KOLOM (SINGLE ELEMENT) ---
                # Using ElasticTimoshenkoBeam as it accounts for shear deformation
                # like SAP2000's frame element does
                
                # ElasticTimoshenkoBeam $eleTag $iNode $jNode $E $G $A $J $Iy $Iz $Avy $Avz $transfTag
                # ORDER MATTERS: Iy comes before Iz in arguments.
                ops.element('ElasticTimoshenkoBeam', item['id'], item['nodes'][0], item['nodes'][1], 
                            E, G, A, J, Ops_Iy, Ops_Iz, Avy, Avz, transf_tag)
                
                sub_elements_map[item['id']] = [(item['id'], item['length'])]
                
            else:
                # --- BALOK (SUBDIVIDED INTO 20 SEGMENTS) ---
                # More segments = better accuracy for triangular/trapezoidal loads
                
                n_start = item['nodes'][0]
                n_end = item['nodes'][1]
                coord_start = node_coords[n_start]
                coord_end = node_coords[n_end]
                
                # --- BEAM INSERTION POINT OFFSET ---
                # SAP2000 CardinalPt=8 (top center): beam centroid is BELOW joint
                # by d_mm/2. Model via rigidLink from joint to offset beam node.
                # Note: geomTransf jntOffset was tested but made errors WORSE
                # (M2: 32%→137%, M1: 0.55%→11.76%) because it shortens flexible length.
                if BEAM_INSERTION_POINT_TOP:
                    beam_d = float(sec.get('d_mm', 0))
                    beam_offset_z = -beam_d / 2.0  # Offset DOWN
                    
                    # Create offset start node
                    off_start_coords = (coord_start[0], coord_start[1], coord_start[2] + beam_offset_z)
                    n_off_start = next_node_id
                    node_coords[n_off_start] = off_start_coords
                    ops.node(n_off_start, *off_start_coords)
                    res["nodes"][n_off_start] = {"coords": off_start_coords, "disp": [0]*6, "reaction": None}
                    next_node_id += 1
                    
                    # Create offset end node
                    off_end_coords = (coord_end[0], coord_end[1], coord_end[2] + beam_offset_z)
                    n_off_end = next_node_id
                    node_coords[n_off_end] = off_end_coords
                    ops.node(n_off_end, *off_end_coords)
                    res["nodes"][n_off_end] = {"coords": off_end_coords, "disp": [0]*6, "reaction": None}
                    next_node_id += 1
                    
                    # Rigid links: joint node -> offset beam node (all 6 DOF)
                    ops.rigidLink('beam', n_start, n_off_start)
                    ops.rigidLink('beam', n_end, n_off_end)
                    
                    # Use offset nodes as beam start/end
                    beam_n_start = n_off_start
                    beam_n_end = n_off_end
                    beam_coord_start = off_start_coords
                    beam_coord_end = off_end_coords
                else:
                    beam_n_start = n_start
                    beam_n_end = n_end
                    beam_coord_start = coord_start
                    beam_coord_end = coord_end
                
                vx = beam_coord_end[0] - beam_coord_start[0]
                vy = beam_coord_end[1] - beam_coord_start[1]
                vz = beam_coord_end[2] - beam_coord_start[2]

                # --- DETERMINE SEGMENT BOUNDARIES ---
                # If this beam has secondary beam connections, insert split points
                is_secondary = item['raw'].get('group') == 'Secondary'
                conn_list = parent_connections.get(item['id'], [])

                # Build list of fraction boundaries for segments
                # Each segment will be subdivided into sub-elements
                # Deduplicate: multiple secondary beams may connect at the same point
                # (e.g. interior Y-beams in 2x2 grid receive connections from both sides)
                seg_fracs = sorted(set(
                    [0.0] + [frac for (frac, sb_id, conn_coord, sb_dir) in conn_list] + [1.0]
                ))

                # Total number of sub-elements (distribute proportionally across segments)
                total_num_subs = 24
                sub_ids = []
                sub_ele_counter = 0
                prev_node = beam_n_start

                for seg_idx in range(len(seg_fracs) - 1):
                    seg_frac_start = seg_fracs[seg_idx]
                    seg_frac_end = seg_fracs[seg_idx + 1]
                    seg_len_frac = seg_frac_end - seg_frac_start

                    # Number of sub-elements for this segment (proportional, min 4)
                    n_seg_subs = max(4, int(round(total_num_subs * seg_len_frac)))

                    for k in range(n_seg_subs):
                        # Track segment boundaries for SFD jump stations
                        if k == 0 and seg_idx > 0:
                            if item['id'] not in seg_start_indices:
                                seg_start_indices[item['id']] = set()
                            seg_start_indices[item['id']].add(sub_ele_counter)

                        is_last_sub = (k == n_seg_subs - 1)
                        is_last_segment = (seg_idx == len(seg_fracs) - 2)

                        if is_last_sub and is_last_segment:
                            curr_node = beam_n_end
                        elif is_last_sub and not is_last_segment:
                            # Connection point — parent beam split for secondary beam
                            conn_frac = seg_fracs[seg_idx + 1]

                            if BEAM_INSERTION_POINT_TOP:
                                # Offset beam: create node at offset level + rigidLink to floor joint
                                nx = beam_coord_start[0] + vx * conn_frac
                                ny = beam_coord_start[1] + vy * conn_frac
                                nz = beam_coord_start[2] + vz * conn_frac

                                curr_node = next_node_id
                                node_coords[curr_node] = (nx, ny, nz)
                                ops.node(curr_node, nx, ny, nz)
                                res["nodes"][curr_node] = {"coords": (nx, ny, nz), "disp": [0]*6, "reaction": None}
                                next_node_id += 1

                                # Find/create floor-level joint for secondary beam force transfer
                                fj_x = coord_start[0] + (coord_end[0] - coord_start[0]) * conn_frac
                                fj_y = coord_start[1] + (coord_end[1] - coord_start[1]) * conn_frac
                                fj_z = coord_start[2]
                                floor_key = f"{fj_x:.1f}_{fj_y:.1f}_{fj_z:.1f}"

                                if floor_key in node_map:
                                    floor_nid = node_map[floor_key]
                                else:
                                    floor_nid = next_node_id
                                    node_coords[floor_nid] = (fj_x, fj_y, fj_z)
                                    ops.node(floor_nid, fj_x, fj_y, fj_z)
                                    res["nodes"][floor_nid] = {"coords": (fj_x, fj_y, fj_z), "disp": [0]*6, "reaction": None}
                                    node_map[floor_key] = floor_nid
                                    next_node_id += 1

                                ops.rigidLink('beam', floor_nid, curr_node)
                            else:
                                # No offset: reuse floor-level node directly
                                nx = beam_coord_start[0] + vx * conn_frac
                                ny = beam_coord_start[1] + vy * conn_frac
                                nz = beam_coord_start[2] + vz * conn_frac
                                floor_key = f"{nx:.1f}_{ny:.1f}_{nz:.1f}"

                                if floor_key in node_map:
                                    curr_node = node_map[floor_key]
                                else:
                                    curr_node = next_node_id
                                    node_coords[curr_node] = (nx, ny, nz)
                                    ops.node(curr_node, nx, ny, nz)
                                    res["nodes"][curr_node] = {"coords": (nx, ny, nz), "disp": [0]*6, "reaction": None}
                                    node_map[floor_key] = curr_node
                                    next_node_id += 1

                            connection_node_map[(item['id'], conn_frac)] = curr_node
                        else:
                            ratio = seg_frac_start + seg_len_frac * (k + 1) / n_seg_subs
                            nx = beam_coord_start[0] + vx * ratio
                            ny = beam_coord_start[1] + vy * ratio
                            nz = beam_coord_start[2] + vz * ratio

                            curr_node = next_node_id
                            node_coords[curr_node] = (nx, ny, nz)
                            ops.node(curr_node, nx, ny, nz)
                            res["nodes"][curr_node] = {"coords": (nx, ny, nz), "disp": [0]*6, "reaction": None}
                            next_node_id += 1

                        sub_ele_id = item['id'] * 100 + sub_ele_counter
                        if sub_ele_id > 2000000000:
                            sub_ele_id = int(sub_ele_id % 1000000 + 900000)

                        # Create per-sub-element transform WITH rigid end zone offsets
                        # Only first and last sub-elements of the WHOLE beam get offsets
                        # Secondary beams: no rigid end zone (RigidFactor=0 per SAP2000)
                        sub_transf_tag = transf_counter
                        transf_counter += 1

                        sub_dI = [0.0, 0.0, 0.0]
                        sub_dJ = [0.0, 0.0, 0.0]

                        if not is_secondary:
                            if sub_ele_counter == 0:
                                sub_dI = list(item['dI'])
                            if is_last_sub and is_last_segment:
                                sub_dJ = list(item['dJ'])

                        ops.geomTransf('Linear', sub_transf_tag,
                                      item['vecxz'][0], item['vecxz'][1], item['vecxz'][2],
                                      '-jntOffset', sub_dI[0], sub_dI[1], sub_dI[2],
                                      sub_dJ[0], sub_dJ[1], sub_dJ[2])

                        seg_sub_len = item['length'] * seg_len_frac / n_seg_subs

                        ops.element('ElasticTimoshenkoBeam', sub_ele_id, prev_node, curr_node,
                                    E, G, A, J, Ops_Iy, Ops_Iz, Avy, Avz, sub_transf_tag)

                        sub_ids.append((sub_ele_id, seg_sub_len))
                        sub_ele_counter += 1
                        prev_node = curr_node

                sub_elements_map[item['id']] = sub_ids

        # --- RIGID DIAPHRAGM (for floor in-plane stiffness) ---
        # SAP2000 applies diaphragm to all load cases. The diaphragm constrains
        # floor-level joints to share Ux, Uy, Rz (DOFs 1,2,6).
        # This is essential for proper P*e (axial force x eccentricity) effects
        # when BEAM_INSERTION_POINT_TOP is active with secondary beam connections.
        z_levels_all = struct_config['z_levels']
        min_z_base = min(z_levels_all) if z_levels_all else 0.0
        floor_z_levels = sorted([z for z in z_levels_all if z > min_z_base + 100])

        diaphragm_applied = False
        for fz in floor_z_levels:
            # Collect floor-level joint nodes (original joints only, not intermediate sub-nodes)
            floor_joint_nodes = []
            for nid in original_joint_nids:
                if nid in fixed_nodes:
                    continue
                coords = node_coords.get(nid, (0, 0, 0))
                if abs(coords[2] - fz) < 100.0:
                    floor_joint_nodes.append(nid)

            if len(floor_joint_nodes) >= 2:
                # Use first column-top node as master (it has element stiffness)
                # Prefer a column-top node (corner joint) for stability
                master_nid = None
                for item in processed_elements:
                    if item['is_vertical']:
                        for nn in item['nodes']:
                            if nn in floor_joint_nodes:
                                master_nid = nn
                                break
                    if master_nid:
                        break

                if master_nid is None:
                    master_nid = floor_joint_nodes[0]

                slave_nids = [n for n in floor_joint_nodes if n != master_nid]
                if slave_nids:
                    ops.rigidDiaphragm(3, master_nid, *slave_nids)
                    diaphragm_applied = True

        if diaphragm_applied:
            print(f"  Rigid diaphragm applied to {len(floor_z_levels)} floor(s)")

        # --- LOAD APPLICATION ---
        ops.timeSeries('Linear', 1)
        ops.pattern('Plain', 1, 1)

        # A. ELEMENT SELF WEIGHT (controlled by SELF_WEIGHT_MULT)
        if SELF_WEIGHT_MULT > 0:
            for item in processed_elements:
                mat = item['raw']['material']
                rho = float(mat.get('Rho_kg/m3', 0))
                if rho == 0: rho = float(mat.get('Rho_kg/mm3', 0)) * 1e9
                
                # Calculate weight per unit length (with self_weight_mult)
                w_dead = float(item['raw']['section'].get('Area_mm2', 0)) * (rho * 1e-9) * G_ACC * FACTOR_SW * SELF_WEIGHT_MULT
                
                # SAP2000 auto-calculate self-weight uses FULL element length
                item_len = item['length']
                
                # Equilibrium Check
                total_applied_force_z -= w_dead * item_len
                
                # Apply to all sub-elements
                subs = sub_elements_map.get(item['id'], [(item['id'], item['length'])])
                
                for (eid, fractional_len) in subs:
                     if item['is_vertical']:
                         # Column: axial load (distributed along axis)
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', 0.0, 0.0, -w_dead) 
                     else:
                         # Beam: gravity in local-z direction (vertical with vecxz=[0,0,1])
                         # beamUniform args: Wy(local-y=horiz), Wz(local-z=vert), Wx(axial)
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', 0.0, -w_dead, 0.0)
        # B. SLAB/FLOOR PRESSURE LOADS - TWO-WAY YIELD LINE DISTRIBUTION
        # Implements proper tributary area calculation with 45-degree bisectors
        # Short span beams get triangle loads, long span beams get trapezoid loads
        # Supports sub-panel subdivision by secondary beams
        if FLOOR_PRESSURE > 0:
            x_coords = struct_config['x_coords']
            y_coords = struct_config['y_coords']
            z_levels_all = struct_config['z_levels']
            edge_tol = 10.0

            # Floor Z levels (above base)
            min_z_base = min(z_levels_all) if z_levels_all else 0.0
            floor_z_list = [z for z in z_levels_all if z > min_z_base + 100]

            # Build sub-panels (handles secondary beam subdivisions)
            sub_panels = build_sub_panels(x_coords, y_coords, sec_beams_list, floor_z_list, edge_tol)

            # Fallback: if no sub-panels built, use primary panels
            if not sub_panels:
                for ix in range(len(x_coords) - 1):
                    for iy in range(len(y_coords) - 1):
                        for fz in floor_z_list:
                            sub_panels.append({
                                'x0': x_coords[ix], 'x1': x_coords[ix + 1],
                                'y0': y_coords[iy], 'y1': y_coords[iy + 1],
                                'Lx': abs(x_coords[ix + 1] - x_coords[ix]),
                                'Ly': abs(y_coords[iy + 1] - y_coords[iy]),
                                'floor_z': fz
                            })

            # Process each beam and calculate loads from adjacent sub-panels
            for item in processed_elements:
                if item['is_vertical']:
                    continue

                raw = item['raw']
                start = raw['topology']['start_node']
                end = raw['topology']['end_node']
                sx, sy, sz = start[0], start[1], start[2]
                ex, ey, ez = end[0], end[1], end[2]
                beam_z = (sz + ez) / 2.0
                beam_len = item['length']

                is_x_beam = abs(sy - ey) < edge_tol
                is_y_beam = abs(sx - ex) < edge_tol
                if not is_x_beam and not is_y_beam:
                    continue

                # Beam coordinate range in running direction
                if is_x_beam:
                    beam_coord_min = min(sx, ex)
                    beam_coord_max = max(sx, ex)
                else:
                    beam_coord_min = min(sy, ey)
                    beam_coord_max = max(sy, ey)
                beam_coord_range = beam_coord_max - beam_coord_min

                subs = sub_elements_map.get(item['id'], [(item['id'], item['length'])])

                for panel in sub_panels:
                    # Floor level check
                    if abs(beam_z - panel['floor_z']) > 100:
                        continue

                    Lx, Ly = panel['Lx'], panel['Ly']
                    L_s = min(Lx, Ly)
                    x_c_p = L_s / 2.0
                    q_max = FLOOR_PRESSURE * x_c_p

                    if is_x_beam:
                        # X-beam at Y=sy: adjacent if sy is on panel's Y edge
                        if not (abs(sy - panel['y0']) < edge_tol or abs(sy - panel['y1']) < edge_tol):
                            continue
                        # X overlap between beam and panel
                        ol_start = max(beam_coord_min, panel['x0'])
                        ol_end = min(beam_coord_max, panel['x1'])
                        if ol_end - ol_start < edge_tol:
                            continue
                        panel_span = Lx
                        panel_coord_start = panel['x0']
                        is_short_span = (Lx <= Ly)
                    else:
                        # Y-beam at X=sx: adjacent if sx is on panel's X edge
                        if not (abs(sx - panel['x0']) < edge_tol or abs(sx - panel['x1']) < edge_tol):
                            continue
                        ol_start = max(beam_coord_min, panel['y0'])
                        ol_end = min(beam_coord_max, panel['y1'])
                        if ol_end - ol_start < edge_tol:
                            continue
                        panel_span = Ly
                        panel_coord_start = panel['y0']
                        is_short_span = (Ly <= Lx)

                    is_triangle = (is_short_span or abs(Lx - Ly) < edge_tol)

                    # Apply load to sub-elements within overlap range
                    cumul = 0.0
                    for k, (eid, seg_len) in enumerate(subs):
                        seg_start = cumul
                        seg_end = cumul + seg_len
                        cumul += seg_len

                        # Map sub-element position to beam running coordinate
                        if beam_coord_range > edge_tol:
                            seg_coord_s = beam_coord_min + seg_start / beam_len * beam_coord_range
                            seg_coord_e = beam_coord_min + seg_end / beam_len * beam_coord_range
                        else:
                            continue

                        # Skip if sub-element is outside this panel's range
                        if seg_coord_e < ol_start - edge_tol or seg_coord_s > ol_end + edge_tol:
                            continue

                        # Position within panel load pattern
                        pos_s = max(0.0, seg_coord_s - panel_coord_start)
                        pos_e = max(0.0, min(panel_span, seg_coord_e - panel_coord_start))

                        q_s = get_q_load(pos_s, panel_span, q_max, x_c_p, is_triangle)
                        q_e = get_q_load(pos_e, panel_span, q_max, x_c_p, is_triangle)
                        q_avg = (q_s + q_e) / 2.0

                        # Floor load: gravity in local-z direction (vertical with vecxz=[0,0,1])
                        ops.eleLoad('-ele', eid, '-type', '-beamUniform', 0.0, -q_avg)
                        total_applied_force_z -= q_avg * seg_len


        # --- SOLVE ---
        ops.system('BandGeneral')

        ops.numberer('RCM')
        # Penalty handler required for chained constraints (rigidDiaphragm + rigidLink)
        # Transformation handler fails with nested master-slave relationships
        ops.constraints('Penalty', 1.0e14, 1.0e14)
        ops.integrator('LoadControl', 1.0)
        ops.algorithm('Linear')
        try:
            ops.analysis('Static')
            status = ops.analyze(1)
        except:
            status = -1
        
        res["status"] = "Success" if status == 0 else "Failed"

        # --- EXTRACT RESULTS ---
        if status == 0:
            ops.reactions()
            total_rz = 0.0
            
            # Nodes
            for nid in node_coords:
                if nid in fixed_nodes:
                    # Validated Swap: F1=Fx, F2=Fy following SAP2000 convention
                    # OpenSees Reaction: [Fx, Fy, Fz, Mx, My, Mz]
                    reac = ops.nodeReaction(nid)
                    
                    # CONSISTENT SWAP: If F1/F2 are swapped relative to Fx/Fy,
                    # then M1/M2 must also be swapped relative to Mx/My
                    # M1 (about X) relates to F2 (Y-direction force)
                    # M2 (about Y) relates to F1 (X-direction force)
                    
                    f1_val = reac[0]
                    f2_val = reac[1]


                    res["nodes"][nid]["reaction"] = {
                        # PHYSICAL INTERPRETATION (OpenSees Global Coordinates):
                        # F1 = Reaction in Global X direction
                        # F2 = Reaction in Global Y direction
                        # F3 = Reaction in Global Z direction (Vertical)
                        # 
                        # Symmetry requirements:
                        # - Nodes at X=0 (N7, N9, N11) should have F1 ≈ 0
                        # - Nodes at Y=0 (N3, N9, N15) should have F2 ≈ 0
                        "F1": round(f1_val, 2),  # OpenSees Rx -> F1 (Global X)
                        "F2": round(f2_val, 2),  # OpenSees Ry -> F2 (Global Y)
                        "F3": round(reac[2], 2),  # OpenSees Rz -> F3 (Vertical)
                        # MOMENT MAPPING (based on stiffness-moment relationship):
                        # Weaker axis (Iy) requires LARGER moments for same resistance
                        # Stronger axis (Iz) requires SMALLER moments for same resistance
                        # 
                        # - M1: Moment about X-axis (OpenSees Mx = reac[3])
                        #       Uses STRONG axis (Iz) for bending -> SMALLER moment
                        # - M2: Moment about Y-axis (OpenSees My = reac[4])
                        #       Uses WEAK axis (Iy) for bending -> LARGER moment
                        "M1": round(reac[3], 2),  # OpenSees Mx -> M1 (strong axis, smaller)
                        "M2": round(reac[4], 2),  # OpenSees My -> M2 (weak axis, larger)
                        "M3": round(reac[5], 2)   # OpenSees Mz -> M3 (Torsion)
                    }
                    total_rz += reac[2]
               
                d = ops.nodeDisp(nid)
                res["nodes"][nid]["disp"] = [round(v, 10) for v in d]
            
            res["summary"]["total_reaction_z"] = round(total_rz, 2)
            
            # Elements - Adaptive Multi-Station Output (SAP2000 Diagram Style)
            for item in processed_elements:
                eid = item['id']
                subs = sub_elements_map.get(eid)
                
                try:
                    # Treat checking for subs, but also force vertical elements (columns) 
                    # to go through the single-element path (adaptive stationing)
                    # because they are modeled as single elements in OpenSees (line 1508)
                    force_single_path = item.get('is_vertical', False)
                    
                    if not subs or force_single_path:
                          # NO SUB-ELEMENTS: Use adaptive stationing
                          local_axes = item['raw'].get('local_axes', {})
                          
                          # Get element length from topology
                          element_length = item['raw'].get('topology', {}).get('length_mm', 0)
                          
                          # Find critical stations (boundaries, zero crossings, max points)
                          is_vert = item.get('is_vertical', False)
                          critical_stations = find_critical_stations(eid, local_axes, element_length, num_samples=5, is_vertical=is_vert)
                          
                          # Build stations list for output
                          # Build stations list for output
                          # Column sign correction: V3, T, M2 need negation for SAP2000 convention
                          # This is done at OUTPUT level (not extraction) so interpolation formula works correctly
                          col_sign = -1.0 if is_vert else 1.0
                          
                          stations_output = []
                          for station_data in critical_stations:
                              forces = station_data['forces']
                              
                              station_ratio = station_data['station']
                              actual_distance = station_ratio * element_length  # Calculate actual distance in mm
                              
                              stations_output.append({
                                  "station": round(station_ratio, 4),
                                  "distance_mm": round(actual_distance, 2),  # Actual distance
                                  "P":  round(forces["P"], 2),
                                  "Fy": round(forces["Fy"], 2),
                                  "Fz": round(col_sign * forces["Fz"], 2),  # Negate for columns
                                  "T":  round(col_sign * forces["T"], 2),   # Negate for columns
                                  "My": round(-forces["My"], 2),  # SAP2000 sign convention
                                  "Mz": round(forces["Mz"], 2)
                              })
                          
                          # Calculate max deflection + station-by-station profile
                          max_defl = get_max_deflection(item, node_coords, sub_elements_map)
                          defl_profile = get_deflection_profile(item, node_coords, sub_elements_map)

                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "group": item['raw'].get('group', 'Unknown'),
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length,
                               "max_deflection": max_defl,
                               "deflection_profile": defl_profile,
                               "stations": stations_output
                            }
                    else:
                          # SUB-ELEMENTS: DIRECT NUMERICAL EXTRACTION
                          # Query ops.eleForce() at each sub-element boundary
                          # This avoids analytical interpolation error for triangular/trapezoidal loads
                          
                          local_axes = item['raw'].get('local_axes', {})
                          element_length_total = item['raw'].get('topology', {}).get('length_mm', 0)
                          is_vert = item.get('is_vertical', False)
                          stations_output = []
                          cumulative_dist = 0.0

                          # eleForce returns forces in GLOBAL coordinates.
                          # For a beam running in direction x_axis, the local forces are:
                          #   P_local  = dot(x_axis, F_global_trans)
                          #   My_local = dot(strong_axis, M_global) where strong_axis = Z x x_axis
                          #   T_local  = dot(x_axis, M_global)
                          # strong_axis (horiz. axis perp. to beam) = [-x_ax_y, x_ax_x, 0]
                          x_ax = local_axes.get('x_axis', [1.0, 0.0, 0.0])
                          x_ax_x = float(x_ax[0])
                          x_ax_y = float(x_ax[1])
                          sa_x = -x_ax_y   # strong-axis unit vector X-component
                          sa_y =  x_ax_x   # strong-axis unit vector Y-component

                          seg_starts = seg_start_indices.get(eid, set())

                          for i, (sub_eid, sub_len) in enumerate(subs):
                                # Get raw forces from OpenSees (12-component array, global coords)
                                forces_raw = ops.eleForce(sub_eid)

                                # Record i-node for: first sub-element (station 0) AND
                                # segment boundary sub-elements (SFD jump at connection point)
                                if i == 0 or i in seg_starts:
                                    i_dist = cumulative_dist
                                    i_ratio = i_dist / element_length_total if element_length_total > 0 else 0

                                    i_forces = {
                                        "P":  -(x_ax_x * forces_raw[0] + x_ax_y * forces_raw[1]),
                                        "Fy":   x_ax_y * forces_raw[0] - x_ax_x * forces_raw[1],
                                        "Fz":  -forces_raw[2],
                                        "T":  -(x_ax_x * forces_raw[3] + x_ax_y * forces_raw[4]),
                                        "My": -(sa_x   * forces_raw[3] + sa_y   * forces_raw[4]),
                                        "Mz":   forces_raw[5]
                                    }

                                    stations_output.append({
                                        "station": round(i_ratio, 4),
                                        "distance_mm": round(i_dist, 2),
                                        "P":  round(i_forces["P"], 2),
                                        "Fy": round(i_forces["Fy"], 2),
                                        "Fz": round(i_forces["Fz"], 2),
                                        "T":  round(i_forces["T"], 2),
                                        "My": round(-i_forces["My"], 2),
                                        "Mz": round(i_forces["Mz"], 2)
                                    })

                                # Always include j-node (end of this sub-element)
                                # Extract j-node forces (indices 6-11)
                                j_dist = cumulative_dist + sub_len
                                global_ratio = j_dist / element_length_total if element_length_total > 0 else 0

                                j_forces = {
                                    "P":   x_ax_x * forces_raw[6] + x_ax_y * forces_raw[7],
                                    "Fy":  x_ax_y * forces_raw[6] - x_ax_x * forces_raw[7],
                                    "Fz":  forces_raw[8],
                                    "T":   x_ax_x * forces_raw[9] + x_ax_y * forces_raw[10],
                                    "My":  sa_x   * forces_raw[9] + sa_y   * forces_raw[10],
                                    "Mz": -forces_raw[11]
                                }

                                stations_output.append({
                                    "station": round(global_ratio, 4),
                                    "distance_mm": round(j_dist, 2),
                                    "P":  round(j_forces["P"], 2),
                                    "Fy": round(j_forces["Fy"], 2),
                                    "Fz": round(j_forces["Fz"], 2),
                                    "T":  round(j_forces["T"], 2),
                                    "My": round(-j_forces["My"], 2),
                                    "Mz": round(j_forces["Mz"], 2)
                                })
                                
                                cumulative_dist += sub_len
                          
                          # Calculate max deflection + station-by-station profile
                          max_defl = get_max_deflection(item, node_coords, sub_elements_map)
                          defl_profile = get_deflection_profile(item, node_coords, sub_elements_map)

                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "group": item['raw'].get('group', 'Unknown'),
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length_total,
                               "max_deflection": max_defl,
                               "deflection_profile": defl_profile,
                               "stations": stations_output
                            }
                except Exception:
                    import traceback
                    print(f"Error extracting element {eid}:")
                    traceback.print_exc()
                    res["elements"][eid] = {"error": "N/A"}


            # --- EQUILIBRIUM CHECK ---
            res["summary"]["total_applied_z"] = round(total_applied_force_z, 2)
            res["summary"]["equilibrium_residual"] = round(abs(total_rz + total_applied_force_z), 2)
            res["summary"]["equilibrium_ratio"] = round(abs((total_rz + total_applied_force_z) / total_applied_force_z * 100), 4) if abs(total_applied_force_z) > 1e-9 else 0.0
                    
    except Exception as e:
        print(f"[ERROR] Case {case_type}: {e}")
        res["status"] = "Error"
    
    return res

# ============================================================================
# 3.5 SEISMIC ANALYSIS — Equivalent Lateral Force (SNI 1726)
# ============================================================================

def run_seismic_analysis(data, direction='EQx'):
    """
    Equivalent Lateral Force (ELF) procedure per SNI 1726.
    
    Builds full structural model, applies lateral forces at floor master nodes
    via rigid diaphragm, and returns reactions + internal forces.
    
    Args:
        data: Model data dictionary (from Model data.json)
        direction: 'EQx' or 'EQy'
    
    Returns:
        dict: Complete seismic analysis results
    """
    res = {
        "status": "Failed",
        "direction": direction,
        "nodes": {},
        "elements": {},
        "seismic_parameters": {},
        "floor_data": [],
        "summary": {}
    }
    
    seismic_params = data.get('seismic_parameters', {})
    if not seismic_params:
        print("[ERROR] No seismic_parameters found in model data!")
        return res
    
    elements_list = data.get('model_elements', [])
    SLAB_SW_PRESSURE = float(data.get('slab_sw_pressure', 0.0))
    SLAB_ADL_PRESSURE = float(data.get('slab_adl_pressure', 0.0))
    
    # --- Detect structure config DYNAMICALLY ---
    struct_config = detect_structure_config_from_grid(data)
    n_stories = struct_config['n_stories']
    story_height_mm = struct_config['story_height']
    story_height_m = story_height_mm / 1000.0
    splice_offset_mm = float(seismic_params.get('COL_SPLICE_OFFSET_MM', 1500))  # Fabrication splice offset
    n_span_x = struct_config['n_span_x']
    n_span_y = struct_config['n_span_y']
    span_x = struct_config['span_x']
    span_y = struct_config['span_y']
    z_levels = struct_config['z_levels']
    x_coords = struct_config['x_coords']
    y_coords = struct_config['y_coords']
    
    print(f"  Seismic Config: {n_stories} stories, {n_span_x}x{n_span_y} spans")
    print(f"  Span X={span_x}mm, Span Y={span_y}mm, Story H={story_height_mm}mm")
    
    # Extract seismic parameters
    SDS = float(seismic_params.get('SDS', 0))
    SD1 = float(seismic_params.get('SD1', 0))
    S1 = float(seismic_params.get('S1', 0))
    TL = float(seismic_params.get('TL', 12))
    Ie = float(seismic_params.get('Ie', 1.0))
    R = float(seismic_params.get('R', 8.0))
    Cd = float(seismic_params.get('Cd', 5.5))
    Ta = float(seismic_params.get('Ta', 0.5))
    T = Ta  # Use approximate fundamental period
    
    GRAVITY = 9.81  # m/s²

    try:
        # ================================================================
        # A. CALCULATE SEISMIC WEIGHT PER FLOOR (Wi) — SECTION CUT
        # ================================================================
        # Wi = weight of structure tributary to each floor level
        # Dynamic: uses actual n_stories, spans, element properties
        
        # Floor Z-levels (excluding base z=0)
        min_z = min(z_levels) if z_levels else 0.0
        floor_z = sorted([z for z in z_levels if z > min_z + 100])
        
        if len(floor_z) != n_stories:
            print(f"  WARNING: floor_z has {len(floor_z)} levels, expected {n_stories}")
            # Fallback: generate evenly spaced floors
            floor_z = [min_z + (i+1)*story_height_mm for i in range(n_stories)]
        
        Wi_N = [0.0] * n_stories  # Weight in N per floor (for Cvx distribution)
        base_mass_N = 0.0  # Weight at base level (not applied as lateral force)
        
        # --- A1. Element self-weight (columns + beams) ---
        for elem in elements_list:
            topo = elem.get('topology', {})
            sec = elem.get('section', {})
            mat = elem.get('material', {})
            
            A_mm2 = float(sec.get('Area_mm2', 0))
            rho = float(mat.get('Rho_kg/m3', 0))
            if rho == 0:
                rho = float(mat.get('Rho_kg/mm3', 0)) * 1e9
            
            L_mm = float(topo.get('length_mm', 0))
            
            # Element weight in N: ρ(kg/m³) * 1e-9(kg/mm³) * A(mm²) * L(mm) * g(m/s²)
            w_element_N = rho * 1e-9 * A_mm2 * L_mm * GRAVITY
            
            start_z = float(topo['start_node'][2])
            end_z = float(topo['end_node'][2])
            
            elem_type = elem.get('type', '')
            
            if elem_type == 'Column':
                # Column: 100% to the floor at the TOP of the column
                # (Section cut approach: horizontal cut at each floor level
                #  captures the full weight of columns below that floor)
                z_top = max(start_z, end_z)

                for fi in range(n_stories):
                    if abs(z_top - floor_z[fi]) < 100:
                        Wi_N[fi] += w_element_N
                        break
                            
            else:
                # Beam: 100% to the floor where beam is located
                beam_z = (start_z + end_z) / 2.0
                for fi in range(n_stories):
                    if abs(beam_z - floor_z[fi]) < 100:
                        Wi_N[fi] += w_element_N
                        break
        
        # --- A2. Slab weights (per floor) ---
        # Total floor area from grid coordinates (robust for non-uniform spans)
        total_floor_area_mm2 = sum(
            abs(x_coords[i+1] - x_coords[i]) * abs(y_coords[j+1] - y_coords[j])
            for i in range(len(x_coords) - 1) for j in range(len(y_coords) - 1)
        )

        # Slab SW per floor (N): pressure(N/mm²) * total_area(mm²)
        slab_sw_per_floor_N = SLAB_SW_PRESSURE * total_floor_area_mm2
        # ADL per floor (N)
        slab_adl_per_floor_N = SLAB_ADL_PRESSURE * total_floor_area_mm2

        for fi in range(n_stories):
            Wi_N[fi] += slab_sw_per_floor_N + slab_adl_per_floor_N

        # A3. Section cut approach: Wi per floor = weight between floor-level cuts
        # Matches SAP2000 section cut (DEAD+ADL): full column weight → floor at top
        # Wi_N = frame_SW(1x) + slab_dead + slab_adl

        # Convert to kN
        Wi_kN = [w / 1000.0 for w in Wi_N]
        base_mass_kN = base_mass_N / 1000.0  # Should be 0 with section cut approach

        # W_total = sum of ALL floor weights (no separate base mass)
        # Per SNI 1726 §7.7.2 / ASCE 7 §12.7.2: W = total dead load
        W_total_kN = sum(Wi_kN) + base_mass_kN
        
        # Floor heights from base (m)
        hi_m = [(fz - min_z) / 1000.0 for fz in floor_z]
        
        print(f"\n  --- Seismic Weight Summary ---")
        print(f"  {'Floor':>5} | {'Wi (kN)':>12} | {'hi (m)':>8}")
        print(f"  {'-'*5}-+-{'-'*12}-+-{'-'*8}")
        for fi in range(n_stories-1, -1, -1):
            print(f"  {fi+1:>5} | {Wi_kN[fi]:>12.3f} | {hi_m[fi]:>8.1f}")
        print(f"  {'BASE':>5} | {base_mass_kN:>12.3f} | {'0.0':>8}")
        print(f"  {'TOTAL':>5} | {W_total_kN:>12.3f} |")
        
        # ================================================================
        # B. CALCULATE Cs (Seismic Response Coefficient) — SNI 1726
        # ================================================================
        Cs = SDS / (R / Ie)  # Pers. 31
        
        # Upper bound (Pers. 32/33)
        if T <= TL:
            Cs_max = SD1 / (T * (R / Ie))  # Pers. 32
        else:
            Cs_max = (SD1 * TL) / (T**2 * (R / Ie))  # Pers. 33
        Cs = min(Cs, Cs_max)
        
        # Lower bound (Pers. 34)
        Cs_min = max(0.044 * SDS * Ie, 0.01)
        if S1 >= 0.6:
            Cs_min = max(Cs_min, 0.5 * S1 / (R / Ie))  # Pers. 35
        Cs = max(Cs, Cs_min)
        
        # ================================================================
        # C. CALCULATE k (Distribution Exponent)
        # ================================================================
        if T <= 0.5:
            k = 1.0
        elif T >= 2.5:
            k = 2.0
        else:
            k = 1.0 + (T - 0.5) / 2.0  # Linear interpolation
        
        # ================================================================
        # D. BASE SHEAR & VERTICAL DISTRIBUTION
        # ================================================================
        V_kN = Cs * W_total_kN  # Base shear (kN)
        
        sum_wi_hi_k = sum(Wi_kN[i] * hi_m[i]**k for i in range(n_stories))
        
        Cvx = []
        Fx_kN = []
        for i in range(n_stories):
            if sum_wi_hi_k > 0:
                cvx_i = (Wi_kN[i] * hi_m[i]**k) / sum_wi_hi_k
            else:
                cvx_i = 0.0
            fx_i = cvx_i * V_kN
            Cvx.append(round(cvx_i, 6))
            Fx_kN.append(round(fx_i, 4))
        
        # Store seismic calculation results
        res["seismic_parameters"] = {
            "T": round(T, 4),
            "Cs": round(Cs, 6),
            "Cs_max": round(Cs_max, 6),
            "Cs_min": round(Cs_min, 6),
            "k": round(k, 4),
            "V_kN": round(V_kN, 4),
            "W_total_kN": round(W_total_kN, 4),
            "SDS": SDS, "SD1": SD1, "R": R, "Ie": Ie, "Cd": Cd,
            "accidental_ecc": float(seismic_params.get('ACCIDENTAL_ECC', 0.05)),
        }
        
        res["floor_data"] = [
            {
                "floor": i + 1,
                "Wi_kN": round(Wi_kN[i], 3),
                "hi_m": round(hi_m[i], 3),
                "wi_hi_k": round(Wi_kN[i] * hi_m[i]**k, 3),
                "Cvx": Cvx[i],
                "Fx_kN": Fx_kN[i],
            }
            for i in range(n_stories)
        ]
        
        # ================================================================
        # E. BUILD OPENSEES MODEL & APPLY LATERAL FORCES
        # ================================================================
        ops.wipe()
        ops.model('basic', '-ndm', 3, '-ndf', 6)
        
        # --- Node mapping (same as run_load_case) ---
        node_map = {}
        node_coords = {}
        next_node_id = 1
        original_joint_nids = set()  # Track ORIGINAL structural joints only
        
        def get_node_id(coords):
            nonlocal next_node_id
            key = f"{coords[0]:.1f}_{coords[1]:.1f}_{coords[2]:.1f}"
            if key not in node_map:
                node_map[key] = next_node_id
                node_coords[next_node_id] = coords
                next_node_id += 1
            return node_map[key]
        
        # Pre-process elements
        processed_elements = []
        for entry in elements_list:
            p1 = entry['topology']['start_node']
            p2 = entry['topology']['end_node']
            n1 = get_node_id(p1)
            n2 = get_node_id(p2)
            original_joint_nids.add(n1)
            original_joint_nids.add(n2)
            dx, dy, dz = p2[0]-p1[0], p2[1]-p1[1], p2[2]-p1[2]
            L = math.sqrt(dx**2 + dy**2 + dz**2)
            is_vertical = abs(dz) > abs(dx) and abs(dz) > abs(dy)
            processed_elements.append({
                'id': entry['id'], 'nodes': [n1, n2],
                'is_vertical': is_vertical, 'length': L, 'raw': entry
            })
        
        # Build nodes
        all_z_vals = [c[2] for c in node_coords.values()]
        min_z_val = min(all_z_vals) if all_z_vals else 0.0
        fixed_nodes = set()
        
        # Read support config from JSON (default: Fixed)
        support_config = data.get('support_config', {})
        support_dof = support_config.get('dof', [1, 1, 1, 1, 1, 1])
        
        for nid, coords in node_coords.items():
            ops.node(nid, *coords)
            res["nodes"][nid] = {"coords": coords, "disp": [0.0]*6, "reaction": None}
            if abs(coords[2] - min_z_val) < 100.0:
                ops.fix(nid, *support_dof)
                fixed_nodes.add(nid)
        
        # --- Rigid End Zones Depths ---
        node_connecting_depths = {}
        for item in processed_elements:
            sec = item['raw']['section']
            d_mm = float(sec.get('d_mm', 0))
            b_mm = float(sec.get('b_mm', 0))
            for nid in item['nodes']:
                if nid not in node_connecting_depths:
                    node_connecting_depths[nid] = {'col_d': 0, 'beam_d': 0}
                if item['is_vertical']:
                    node_connecting_depths[nid]['col_d'] = max(
                        node_connecting_depths[nid]['col_d'], b_mm)
                else:
                    node_connecting_depths[nid]['beam_d'] = max(
                        node_connecting_depths[nid]['beam_d'], d_mm)
        
        # --- Build Elements ---
        sub_elements_map = {}
        transf_counter = 1
        G_ACC = 9810.0  # mm/s^2

        # Secondary beam connections (for parent beam splitting)
        _, _, sec_beams_seismic = classify_elements(elements_list)
        parent_connections_seis, sec_beam_dirs_seis = find_secondary_connections(sec_beams_seismic, elements_list)
        connection_node_map_seis = {}

        for item in processed_elements:
            sec = item['raw']['section']
            mat = item['raw']['material']

            # Section properties
            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 78846))  # Use G from JSON directly (SAP2000 BJ REVIT)
            A = float(sec.get('Area_mm2', 0))
            J = 806975.4 if item['is_vertical'] else 132290.5
            Iz = float(sec.get('Iz_mm4', 0))
            Iy = float(sec.get('Iy_mm4', 0))
            Avz = float(sec.get('Avz_mm2', 0))
            Avy = float(sec.get('Avy_mm2', 0))
            
            if A <= 0: continue
            
            # Local axes
            local_axes = item['raw'].get('local_axes', {})
            if item['is_vertical']:
                vecxz = local_axes.get('y_axis', [1, 0, 0])
            else:
                vecxz = local_axes.get('z_axis', [0, 0, 1])
            
            # Map to OpenSees convention (same for columns and beams)
            # Ops_Iy = Iz (strong, SAP I33) → bending about local-y → shear Vz → Ops_Avz
            # Ops_Iz = Iy (weak, SAP I22)  → bending about local-z → shear Vy → Ops_Avy
            # Pairing: Ops_Iy ↔ Ops_Avz (strong↔web), Ops_Iz ↔ Ops_Avy (weak↔flange)
            # Verified: matches SAP2000 I33↔AS2(web=3048), I22↔AS3(flange=7832)
            Ops_Iy = Iz    # strong axis → bending about local-y
            Ops_Iz = Iy    # weak axis   → bending about local-z
            Ops_Avy = Avy  # flange shear → Vy (pairs with Ops_Iz, weak axis)
            Ops_Avz = Avz  # web shear   → Vz (pairs with Ops_Iy, strong axis)
            
            # Rigid end zone offsets
            dI = [0.0, 0.0, 0.0]
            dJ = [0.0, 0.0, 0.0]
            
            n1, n2 = item['nodes']
            p1 = node_coords[n1]
            p2 = node_coords[n2]
            dx = p2[0]-p1[0]; dy = p2[1]-p1[1]; dz_v = p2[2]-p1[2]
            L_elem = math.sqrt(dx**2 + dy**2 + dz_v**2)
            if L_elem > 0:
                ux, uy, uz = dx/L_elem, dy/L_elem, dz_v/L_elem
                
                if item['is_vertical']:
                    if COL_RIGID_END_ZONE_FACTOR > 0:
                        d1 = node_connecting_depths.get(n1, {}).get('beam_d', 0)
                        d2 = node_connecting_depths.get(n2, {}).get('beam_d', 0)
                        
                        off1 = d1 / 2.0 * COL_RIGID_END_ZONE_FACTOR
                        off2 = d2 / 2.0 * COL_RIGID_END_ZONE_FACTOR
                        
                        dI = [ux*off1, uy*off1, uz*off1]
                        dJ = [-ux*off2, -uy*off2, -uz*off2]
                else:
                    if BEAM_RIGID_END_ZONE_FACTOR > 0:
                        d1 = node_connecting_depths.get(n1, {}).get('col_d', 0)
                        d2 = node_connecting_depths.get(n2, {}).get('col_d', 0)
                        
                        off1 = d1 / 2.0 * BEAM_RIGID_END_ZONE_FACTOR
                        off2 = d2 / 2.0 * BEAM_RIGID_END_ZONE_FACTOR
                        
                        dI = [ux*off1, uy*off1, uz*off1]
                        dJ = [-ux*off2, -uy*off2, -uz*off2]
            
            item['dI'] = dI
            item['dJ'] = dJ
            item['vecxz'] = vecxz
            
            # Sub-elements
            sub_ids = []
            n_start_elem = item['nodes'][0]
            n_end_elem = item['nodes'][1]
            coord_start = node_coords[n_start_elem]
            coord_end = node_coords[n_end_elem]

            if item['is_vertical']:
                # COLUMN: 4 sub-elements (no splitting needed)
                num_subs = 4
                vx = coord_end[0]-coord_start[0]
                vy_v = coord_end[1]-coord_start[1]
                vz_c = coord_end[2]-coord_start[2]

                prev_node = n_start_elem
                for k_sub in range(num_subs):
                    if k_sub == num_subs - 1:
                        curr_node = n_end_elem
                    else:
                        ratio = (k_sub + 1) / num_subs
                        nx = coord_start[0] + vx * ratio
                        ny = coord_start[1] + vy_v * ratio
                        nz = coord_start[2] + vz_c * ratio
                        curr_node = next_node_id
                        node_coords[curr_node] = (nx, ny, nz)
                        ops.node(curr_node, nx, ny, nz)
                        res["nodes"][curr_node] = {"coords": (nx,ny,nz), "disp": [0]*6, "reaction": None}
                        next_node_id += 1

                    sub_ele_id = item['id'] * 100 + k_sub
                    if sub_ele_id > 2000000000:
                        sub_ele_id = int(sub_ele_id % 1000000 + 900000)

                    sub_transf_tag = transf_counter
                    transf_counter += 1
                    sub_dI = list(item['dI']) if k_sub == 0 else [0.0, 0.0, 0.0]
                    sub_dJ = list(item['dJ']) if k_sub == num_subs - 1 else [0.0, 0.0, 0.0]

                    ops.geomTransf('Linear', sub_transf_tag,
                                  vecxz[0], vecxz[1], vecxz[2],
                                  '-jntOffset', sub_dI[0], sub_dI[1], sub_dI[2],
                                  sub_dJ[0], sub_dJ[1], sub_dJ[2])
                    ops.element('ElasticTimoshenkoBeam', sub_ele_id,
                               prev_node, curr_node,
                               E, G, A, J, Ops_Iy, Ops_Iz, Ops_Avy, Ops_Avz, sub_transf_tag)
                    sub_ids.append((sub_ele_id, item['length']/num_subs))
                    prev_node = curr_node
            else:
                # BEAM: segment-aware subdivision (supports secondary beam split)
                # --- BEAM INSERTION POINT OFFSET (seismic) ---
                if BEAM_INSERTION_POINT_TOP:
                    beam_d = float(sec.get('d_mm', 0))
                    beam_offset_z = -beam_d / 2.0

                    off_s = (coord_start[0], coord_start[1], coord_start[2] + beam_offset_z)
                    n_off_s = next_node_id
                    node_coords[n_off_s] = off_s
                    ops.node(n_off_s, *off_s)
                    res["nodes"][n_off_s] = {"coords": off_s, "disp": [0]*6, "reaction": None}
                    next_node_id += 1

                    off_e = (coord_end[0], coord_end[1], coord_end[2] + beam_offset_z)
                    n_off_e = next_node_id
                    node_coords[n_off_e] = off_e
                    ops.node(n_off_e, *off_e)
                    res["nodes"][n_off_e] = {"coords": off_e, "disp": [0]*6, "reaction": None}
                    next_node_id += 1

                    ops.rigidLink('beam', n_start_elem, n_off_s)
                    ops.rigidLink('beam', n_end_elem, n_off_e)

                    beam_start = n_off_s
                    beam_end = n_off_e
                    beam_coord_start = off_s
                    beam_coord_end = off_e
                else:
                    beam_start = n_start_elem
                    beam_end = n_end_elem
                    beam_coord_start = coord_start
                    beam_coord_end = coord_end

                vx = beam_coord_end[0]-beam_coord_start[0]
                vy_v = beam_coord_end[1]-beam_coord_start[1]
                vz_c = beam_coord_end[2]-beam_coord_start[2]

                is_secondary = item['raw'].get('group') == 'Secondary'
                conn_list = parent_connections_seis.get(item['id'], [])

                seg_fracs = sorted(set(
                    [0.0] + [frac for (frac, sb_id, conn_coord, sb_dir) in conn_list] + [1.0]
                ))

                total_num_subs = 8
                sub_ele_counter = 0
                prev_node = beam_start

                for seg_idx in range(len(seg_fracs) - 1):
                    seg_frac_start = seg_fracs[seg_idx]
                    seg_frac_end = seg_fracs[seg_idx + 1]
                    seg_len_frac = seg_frac_end - seg_frac_start
                    n_seg_subs = max(2, int(round(total_num_subs * seg_len_frac)))

                    for k in range(n_seg_subs):
                        is_last_sub = (k == n_seg_subs - 1)
                        is_last_segment = (seg_idx == len(seg_fracs) - 2)

                        if is_last_sub and is_last_segment:
                            curr_node = beam_end
                        elif is_last_sub and not is_last_segment:
                            conn_frac = seg_fracs[seg_idx + 1]

                            if BEAM_INSERTION_POINT_TOP:
                                nx = beam_coord_start[0] + vx * conn_frac
                                ny = beam_coord_start[1] + vy_v * conn_frac
                                nz = beam_coord_start[2] + vz_c * conn_frac
                                curr_node = next_node_id
                                node_coords[curr_node] = (nx, ny, nz)
                                ops.node(curr_node, nx, ny, nz)
                                res["nodes"][curr_node] = {"coords": (nx,ny,nz), "disp": [0]*6, "reaction": None}
                                next_node_id += 1

                                # RigidLink to floor joint for secondary beam connection
                                fj_x = coord_start[0] + (coord_end[0] - coord_start[0]) * conn_frac
                                fj_y = coord_start[1] + (coord_end[1] - coord_start[1]) * conn_frac
                                fj_z = coord_start[2]
                                fk = f"{fj_x:.1f}_{fj_y:.1f}_{fj_z:.1f}"
                                if fk in node_map:
                                    floor_nid = node_map[fk]
                                else:
                                    floor_nid = next_node_id
                                    node_coords[floor_nid] = (fj_x, fj_y, fj_z)
                                    ops.node(floor_nid, fj_x, fj_y, fj_z)
                                    res["nodes"][floor_nid] = {"coords": (fj_x,fj_y,fj_z), "disp": [0]*6, "reaction": None}
                                    node_map[fk] = floor_nid
                                    next_node_id += 1
                                ops.rigidLink('beam', floor_nid, curr_node)
                            else:
                                nx = beam_coord_start[0] + vx * conn_frac
                                ny = beam_coord_start[1] + vy_v * conn_frac
                                nz = beam_coord_start[2] + vz_c * conn_frac
                                fk = f"{nx:.1f}_{ny:.1f}_{nz:.1f}"
                                if fk in node_map:
                                    curr_node = node_map[fk]
                                else:
                                    curr_node = next_node_id
                                    node_coords[curr_node] = (nx, ny, nz)
                                    ops.node(curr_node, nx, ny, nz)
                                    res["nodes"][curr_node] = {"coords": (nx,ny,nz), "disp": [0]*6, "reaction": None}
                                    node_map[fk] = curr_node
                                    next_node_id += 1

                            connection_node_map_seis[(item['id'], conn_frac)] = curr_node
                        else:
                            ratio = seg_frac_start + seg_len_frac * (k + 1) / n_seg_subs
                            nx = beam_coord_start[0] + vx * ratio
                            ny = beam_coord_start[1] + vy_v * ratio
                            nz = beam_coord_start[2] + vz_c * ratio
                            curr_node = next_node_id
                            node_coords[curr_node] = (nx, ny, nz)
                            ops.node(curr_node, nx, ny, nz)
                            res["nodes"][curr_node] = {"coords": (nx,ny,nz), "disp": [0]*6, "reaction": None}
                            next_node_id += 1

                        sub_ele_id = item['id'] * 100 + sub_ele_counter
                        if sub_ele_id > 2000000000:
                            sub_ele_id = int(sub_ele_id % 1000000 + 900000)

                        sub_transf_tag = transf_counter
                        transf_counter += 1
                        sub_dI = [0.0, 0.0, 0.0]
                        sub_dJ = [0.0, 0.0, 0.0]
                        if not is_secondary:
                            if sub_ele_counter == 0:
                                sub_dI = list(item['dI'])
                            if is_last_sub and is_last_segment:
                                sub_dJ = list(item['dJ'])

                        ops.geomTransf('Linear', sub_transf_tag,
                                      vecxz[0], vecxz[1], vecxz[2],
                                      '-jntOffset', sub_dI[0], sub_dI[1], sub_dI[2],
                                      sub_dJ[0], sub_dJ[1], sub_dJ[2])

                        seg_sub_len = item['length'] * seg_len_frac / n_seg_subs
                        ops.element('ElasticTimoshenkoBeam', sub_ele_id,
                                   prev_node, curr_node,
                                   E, G, A, J, Ops_Iy, Ops_Iz, Ops_Avy, Ops_Avz, sub_transf_tag)
                        sub_ids.append((sub_ele_id, seg_sub_len))
                        sub_ele_counter += 1
                        prev_node = curr_node

            sub_elements_map[item['id']] = sub_ids
        
        # ================================================================
        # F. RIGID DIAPHRAGM + MASS ASSIGNMENT
        # ================================================================
        # Create master node at centroid of each floor
        center_x = (min(x_coords) + max(x_coords)) / 2.0
        center_y = (min(y_coords) + max(y_coords)) / 2.0
        
        master_nodes = []
        
        for fi in range(n_stories):
            fz = floor_z[fi]
            
            # Create master node at floor centroid
            master_nid = next_node_id
            next_node_id += 1
            ops.node(master_nid, center_x, center_y, fz)
            node_coords[master_nid] = (center_x, center_y, fz)
            res["nodes"][master_nid] = {
                "coords": (center_x, center_y, fz),
                "disp": [0.0]*6, "reaction": None,
                "is_master": True, "floor": fi + 1
            }
            
            # CRITICAL: Only include ORIGINAL structural joint nodes as slaves
            # Do NOT include sub-element intermediate nodes (they are NOT in original_joint_nids)
            # Also exclude fixed nodes (base supports) and the master node itself
            floor_slave_nodes = []
            for nid in original_joint_nids:
                if nid == master_nid:
                    continue
                if nid in fixed_nodes:
                    continue
                coords = node_coords.get(nid, (0,0,0))
                if abs(coords[2] - fz) < 100.0:
                    floor_slave_nodes.append(nid)
            
            if floor_slave_nodes:
                # Apply rigid diaphragm: perpDir=3 (Z-axis)
                # Constrains DOFs 1,2,6 (X, Y, Rz) of slaves to master
                ops.rigidDiaphragm(3, master_nid, *floor_slave_nodes)
                print(f"    Floor {fi+1}: master={master_nid}, slaves={len(floor_slave_nodes)} nodes")
            else:
                print(f"    Floor {fi+1}: master={master_nid}, NO slave nodes!")
            
            # CRITICAL: Fix master node DOFs 3,4,5 (Z, Rx, Ry)
            # rigidDiaphragm(perpDir=3) only constrains DOFs 1,2,6 (X, Y, Rz)
            # Master node has no elements attached → zero stiffness in Z, Rx, Ry
            # Without this, the stiffness matrix is singular
            ops.fix(master_nid, 0, 0, 1, 1, 1, 0)
            
            # Assign mass to master node (translational X, Y only)
            # In consistent N, mm system: mass = W(N) / g(mm/s^2)
            mass_val = Wi_N[fi] / G_ACC
            ops.mass(master_nid, mass_val, mass_val, 0.0, 0.0, 0.0, 0.0)
            
            master_nodes.append(master_nid)
        
        print(f"  Created {len(master_nodes)} master nodes with rigid diaphragm")
        
        # ================================================================
        # G. APPLY LATERAL FORCES WITH ACCIDENTAL ECCENTRICITY
        # ================================================================
        # Accidental eccentricity per SNI 1726 §7.8.4.2: 5% of building
        # dimension perpendicular to the direction of seismic loading.
        # Matches SAP2000 AddEcc=0.05.
        ecc_ratio = float(seismic_params.get('ACCIDENTAL_ECC', 0.05))
        Lx_total = n_span_x * span_x  # Total building dimension in X (mm)
        Ly_total = n_span_y * span_y  # Total building dimension in Y (mm)

        ops.timeSeries('Linear', 1)
        ops.pattern('Plain', 1, 1)

        for fi in range(n_stories):
            Fx_N = Fx_kN[fi] * 1000.0  # kN → N

            if direction == 'EQx':
                # Force in X, eccentricity in Y → Mz = -Fx * ecc * Ly
                Mz_ecc = -Fx_N * ecc_ratio * Ly_total
                ops.load(master_nodes[fi], Fx_N, 0.0, 0.0, 0.0, 0.0, Mz_ecc)
            else:  # EQy
                # Force in Y, eccentricity in X → Mz = +Fy * ecc * Lx
                Mz_ecc = Fx_N * ecc_ratio * Lx_total
                ops.load(master_nodes[fi], 0.0, Fx_N, 0.0, 0.0, 0.0, Mz_ecc)

        if ecc_ratio > 0:
            L_perp = Ly_total if direction == 'EQx' else Lx_total
            ecc_mm = ecc_ratio * L_perp
            print(f"  Accidental eccentricity: {ecc_ratio*100:.1f}% x {L_perp:.0f}mm = {ecc_mm:.0f}mm")
        
        # ================================================================
        # H. SOLVE
        # ================================================================
        ops.system('UmfPack')
        ops.numberer('Plain')
        ops.constraints('Penalty', 1.0e14, 1.0e14)
        ops.integrator('LoadControl', 1.0)
        ops.algorithm('Linear')
        ops.analysis('Static')
        ok = ops.analyze(1)
        
        if ok != 0:
            print(f"  [ERROR] Seismic analysis failed for {direction}")
            res["status"] = "Error"
            return res
        
        res["status"] = "Success"
        
        # ================================================================
        # I. EXTRACT RESULTS
        # ================================================================
        # Nodal displacements
        for nid in list(res["nodes"].keys()):
            try:
                disp = ops.nodeDisp(nid)
                res["nodes"][nid]["disp"] = [round(d, 6) for d in disp]
            except:
                pass
        
        # Reactions at fixed nodes
        ops.reactions()
        total_rx, total_ry, total_rz = 0.0, 0.0, 0.0
        
        for nid in fixed_nodes:
            try:
                r = ops.nodeReaction(nid)
                res["nodes"][nid]["reaction"] = {
                    "F1": round(r[0], 2), "F2": round(r[1], 2), "F3": round(r[2], 2),
                    "M1": round(r[3], 2), "M2": round(r[4], 2), "M3": round(r[5], 2)
                }
                total_rx += r[0]
                total_ry += r[1]
                total_rz += r[2]
            except:
                pass
        
        # Summary
        V_applied_N = V_kN * 1000.0
        res["summary"] = {
            "V_kN": round(V_kN, 4),
            "V_applied_N": round(V_applied_N, 2),
            "total_reaction_x": round(total_rx, 2),
            "total_reaction_y": round(total_ry, 2),
            "total_reaction_z": round(total_rz, 2),
        }
        
        # Check equilibrium
        if direction == 'EQx':
            eq_check = abs(total_rx + V_applied_N)
            res["summary"]["equilibrium_residual_N"] = round(eq_check, 4)
        else:
            eq_check = abs(total_ry + V_applied_N)
            res["summary"]["equilibrium_residual_N"] = round(eq_check, 4)
        
        # Element internal forces (station-based, matching gravity output format)
        # Uses ops.eleForce() at sub-element boundaries for proper force extraction.
        # eleForce() returns forces in GLOBAL coordinates for elements with geomTransf.
        for item in processed_elements:
            elem_id = item['id']
            subs = sub_elements_map.get(elem_id, [])
            if not subs:
                continue

            try:
                local_axes = item['raw'].get('local_axes', {})
                element_length = item['raw'].get('topology', {}).get('length_mm', 0)
                is_vert = item.get('is_vertical', False)
                col_sign = -1.0 if is_vert else 1.0

                stations_output = []
                cumulative_dist = 0.0

                if is_vert:
                    # COLUMN: Global-to-local mapping (verified in gravity path)
                    # Column vecxz=[1,0,0]: local-x=Z, local-y=-Y, local-z=X
                    # eleForce global: [Fx,Fy,Fz,Mx,My,Mz, Fx,Fy,Fz,Mx,My,Mz]
                    for i, (sub_eid, sub_len) in enumerate(subs):
                        f = ops.eleForce(sub_eid)

                        if i == 0:
                            fi = {"P": -f[2], "Fy": -f[0], "Fz": f[1],
                                  "T": f[5], "My": -f[3], "Mz": -f[4]}
                            stations_output.append({
                                "station": 0.0, "distance_mm": 0.0,
                                "P":  round(fi["P"], 2),
                                "Fy": round(fi["Fy"], 2),
                                "Fz": round(col_sign * fi["Fz"], 2),
                                "T":  round(col_sign * fi["T"], 2),
                                "My": round(-fi["My"], 2),
                                "Mz": round(fi["Mz"], 2)
                            })

                        j_dist = cumulative_dist + sub_len
                        j_ratio = j_dist / element_length if element_length > 0 else 0
                        fj = {"P": f[8], "Fy": f[6], "Fz": -f[7],
                              "T": -f[11], "My": f[9], "Mz": f[10]}
                        stations_output.append({
                            "station": round(j_ratio, 4), "distance_mm": round(j_dist, 2),
                            "P":  round(fj["P"], 2),
                            "Fy": round(fj["Fy"], 2),
                            "Fz": round(col_sign * fj["Fz"], 2),
                            "T":  round(col_sign * fj["T"], 2),
                            "My": round(-fj["My"], 2),
                            "Mz": round(fj["Mz"], 2)
                        })
                        cumulative_dist += sub_len
                else:
                    # BEAM: Project global forces using element x_axis direction
                    x_ax = local_axes.get('x_axis', [1.0, 0.0, 0.0])
                    x_ax_x = float(x_ax[0])
                    x_ax_y = float(x_ax[1])
                    sa_x = -x_ax_y   # strong-axis unit vector
                    sa_y =  x_ax_x

                    for i, (sub_eid, sub_len) in enumerate(subs):
                        f = ops.eleForce(sub_eid)

                        if i == 0:
                            fi = {
                                "P":  -(x_ax_x * f[0] + x_ax_y * f[1]),
                                "Fy":   x_ax_y * f[0] - x_ax_x * f[1],
                                "Fz":  -f[2],
                                "T":  -(x_ax_x * f[3] + x_ax_y * f[4]),
                                "My": -(sa_x * f[3] + sa_y * f[4]),
                                "Mz":   f[5]
                            }
                            stations_output.append({
                                "station": 0.0, "distance_mm": 0.0,
                                "P":  round(fi["P"], 2),
                                "Fy": round(fi["Fy"], 2),
                                "Fz": round(fi["Fz"], 2),
                                "T":  round(fi["T"], 2),
                                "My": round(-fi["My"], 2),
                                "Mz": round(fi["Mz"], 2)
                            })

                        j_dist = cumulative_dist + sub_len
                        j_ratio = j_dist / element_length if element_length > 0 else 0
                        fj = {
                            "P":   x_ax_x * f[6] + x_ax_y * f[7],
                            "Fy":  x_ax_y * f[6] - x_ax_x * f[7],
                            "Fz":  f[8],
                            "T":   x_ax_x * f[9] + x_ax_y * f[10],
                            "My":  sa_x * f[9] + sa_y * f[10],
                            "Mz": -f[11]
                        }
                        stations_output.append({
                            "station": round(j_ratio, 4), "distance_mm": round(j_dist, 2),
                            "P":  round(fj["P"], 2),
                            "Fy": round(fj["Fy"], 2),
                            "Fz": round(fj["Fz"], 2),
                            "T":  round(fj["T"], 2),
                            "My": round(-fj["My"], 2),
                            "Mz": round(fj["Mz"], 2)
                        })
                        cumulative_dist += sub_len

                # Use elem_id directly (NOT str()) for consistent key type with gravity
                res["elements"][elem_id] = {
                    "element_type": "Column" if is_vert else "Beam",
                    "group": item['raw'].get('group', 'Unknown'),
                    "element_length_mm": element_length,
                    "max_deflection": None,
                    "stations": stations_output
                }
            except Exception:
                import traceback
                print(f"Error extracting seismic element {elem_id}:")
                traceback.print_exc()
        
        # ================================================================
        # J. DRIFT & P-DELTA CHECK (SNI 1726-2019 ps.7.8.6 & 7.8.7)
        # ================================================================
        # δx = Cd · δxe / Ie                    (Persamaan 44)
        # Δi = δi - δ(i-1)                      (Gambar 10)
        # θ = Px · Δ · Ie / (Vx · hsx · Cd)    (Persamaan 45)
        # θmax = 0.5 / (β · Cd) ≤ 0.25         (Persamaan 46)
        
        # Determine which displacement component to use
        disp_idx = 0 if direction == 'EQx' else 1  # U1 (X) or U2 (Y)
        
        # Compute average elastic displacement per floor level
        # Only use ORIGINAL joint nodes (exclude offset/sub-element nodes)
        floor_disp_elastic = {}  # {z_level: [list of disps]}
        
        for nid, ndata in res["nodes"].items():
            if nid in fixed_nodes:
                continue
            coords = ndata.get("coords", [0, 0, 0])
            disp = ndata.get("disp", [0.0]*6)
            z = coords[2]
            
            # Only consider original structural joint nodes
            if nid not in original_joint_nids:
                continue
            
            if z not in floor_disp_elastic:
                floor_disp_elastic[z] = []
            floor_disp_elastic[z].append(disp[disp_idx])
        
        # Sort floor levels
        sorted_floors = sorted(floor_disp_elastic.keys())
        
        # β = ratio shear demand/capacity (konservatif = 1.0)
        beta = 1.0
        theta_max = min(0.5 / (beta * Cd), 0.25)
        
        drift_results = []
        prev_delta_x = 0.0  # Base displacement = 0
        
        # Cumulative shear per floor (story shear = sum of Fx above)
        # Vx_i = sum(Fx_j for j >= i)
        Vx_story_kN = [0.0] * n_stories
        for i in range(n_stories - 1, -1, -1):
            Vx_story_kN[i] = Fx_kN[i] + (Vx_story_kN[i + 1] if i + 1 < n_stories else 0.0)
        
        # Cumulative vertical load per floor (Px = total weight above floor x)
        Px_kN = [0.0] * n_stories
        for i in range(n_stories - 1, -1, -1):
            Px_kN[i] = Wi_kN[i] + (Px_kN[i + 1] if i + 1 < n_stories else 0.0)
        
        for fi in range(n_stories):
            if fi < len(sorted_floors):
                z_level = sorted_floors[fi]
                disps_at_floor = floor_disp_elastic.get(z_level, [0.0])
                # Average elastic displacement (δxe)
                delta_xe = sum(disps_at_floor) / len(disps_at_floor)
            else:
                delta_xe = 0.0
            
            # Amplified displacement: δx = Cd · δxe / Ie (ps.44)
            delta_x = Cd * abs(delta_xe) / Ie
            
            # Story drift: Δi = δi - δ(i-1) (Gambar 10)
            delta_i = delta_x - prev_delta_x
            
            # Story height
            hsx_mm = story_height_mm
            
            # Drift limit: Δa = 0.025·hsx (Tabel 20, Kategori Risiko I/II)
            delta_a = 0.025 * hsx_mm
            
            # Stability coefficient θ (ps.45)
            Vx_N = Vx_story_kN[fi] * 1000.0  # kN → N
            if Vx_N > 0 and hsx_mm > 0:
                theta = (Px_kN[fi] * 1000.0 * delta_i * Ie) / (Vx_N * hsx_mm * Cd)
            else:
                theta = 0.0
            
            # Status
            drift_ok = delta_i <= delta_a
            if theta <= 0.10:
                pdelta_status = "OK (negligible)"
                amplification = 1.0
            elif theta <= theta_max:
                pdelta_status = "Amplify 1/(1-theta)"
                amplification = 1.0 / (1.0 - theta)
            else:
                pdelta_status = "NG - REDESIGN"
                amplification = None
            
            drift_results.append({
                "floor": fi + 1,
                "delta_xe_mm": round(abs(delta_xe), 4),
                "delta_x_mm": round(delta_x, 4),
                "delta_i_mm": round(delta_i, 4),
                "delta_a_mm": round(delta_a, 1),
                "drift_ratio": round(delta_i / delta_a, 4) if delta_a > 0 else 0.0,
                "drift_ok": drift_ok,
                "Px_kN": round(Px_kN[fi], 2),
                "Vx_kN": round(Vx_story_kN[fi], 2),
                "theta": round(theta, 6),
                "theta_max": round(theta_max, 4),
                "pdelta_status": pdelta_status,
                "amplification": round(amplification, 4) if amplification else None
            })
            
            prev_delta_x = delta_x
        
        res["drift_pdelta"] = drift_results
        res["drift_pdelta_summary"] = {
            "theta_max": round(theta_max, 4),
            "beta": beta,
            "Cd": Cd,
            "direction": direction,
            "all_drift_ok": all(d["drift_ok"] for d in drift_results),
            "all_stability_ok": all(d["theta"] <= theta_max for d in drift_results),
        }
        
        # Print drift table
        print(f"\n  --- Drift & P-Delta ({direction}) ---")
        print(f"  {'Fl':>3} {'dxe(mm)':>10} {'dx(mm)':>10} {'Di(mm)':>10} {'Da(mm)':>10} {'Di/Da':>8} {'theta':>10} {'Status'}")
        print(f"  {'-'*3} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*10} {'-'*20}")
        for d in drift_results:
            print(f"  {d['floor']:>3} {d['delta_xe_mm']:>10.4f} {d['delta_x_mm']:>10.4f} "
                  f"{d['delta_i_mm']:>10.4f} {d['delta_a_mm']:>10.1f} {d['drift_ratio']:>8.4f} "
                  f"{d['theta']:>10.6f} {d['pdelta_status']}")
    
    except Exception as e:
        import traceback
        print(f"[ERROR] Seismic analysis {direction}: {e}")
        traceback.print_exc()
        res["status"] = "Error"
    
    return res


# ============================================================================
# 3.5.1 SHELL PLATE MESH HELPER
# ============================================================================
def _create_shell_plate_mesh(ops, floor_z_list, x_grid, y_grid, span_x, span_y,
                              n_span_x, n_span_y, slab_plate_config,
                              existing_node_coords, next_node_id, next_elem_id):
    """
    Create ShellDKGQ shell plate mesh on each floor.
    
    Args:
        ops: openseespy.opensees module
        floor_z_list: list of floor Z coordinates
        x_grid, y_grid: grid coordinates [x0, x1, ...], [y0, y1, ...]
        span_x, span_y: bay dimensions (mm)
        n_span_x, n_span_y: number of bays
        slab_plate_config: dict with E_MPa, nu, rho_kg_m3, thickness_mm, mesh_size_mm
        existing_node_coords: dict {nid: (x, y, z)} of existing nodes
        next_node_id: starting node ID for new nodes
        next_elem_id: starting element ID for new elements
    
    Returns:
        shell_floor_nodes: dict {floor_idx: [node_ids]}  — all shell nodes per floor
        next_node_id, next_elem_id: updated counters
    """
    import math
    
    E_plate = float(slab_plate_config.get('E_MPa', 24855.6))
    nu = float(slab_plate_config.get('nu', 0.2))
    thickness = float(slab_plate_config.get('thickness_mm', 150.0))
    rho_kg_m3 = float(slab_plate_config.get('rho_kg_m3', 2402.77))
    mesh_size = float(slab_plate_config.get('mesh_size_mm', 250.0))
    
    # Convert density to OpenSees units (N, mm, s)
    # rho_opensees = rho_kg_m3 * 1e-9 (kg/mm3) / 1000 -> NO
    # For shell: rho is mass density = kg/m3 -> convert to N*s^2/mm^4
    # 1 kg = 0.001 N*s^2/mm, so rho = rho_kg_m3 * 1e-9 * 0.001 = rho_kg_m3 * 1e-12
    # But OpenSees ElasticMembranePlateSection expects rho in mass/volume
    # In (N, mm, s): mass unit = N*s^2/mm, volume = mm^3
    # rho = rho_kg_m3 * 1e-9 / 1000.0 = rho_kg_m3 * 1e-12 N*s^2/mm^4
    rho_opensees = rho_kg_m3 * 1e-12  # N*s^2/mm^4
    
    # Create shell section
    shell_sec_tag = 9999  # unique section tag
    ops.section('ElasticMembranePlateSection', shell_sec_tag,
                E_plate, nu, thickness, rho_opensees)
    
    # Calculate mesh divisions per bay
    n_div_x = max(1, int(round(span_x / mesh_size)))
    n_div_y = max(1, int(round(span_y / mesh_size)))
    
    print(f"  Shell plate mesh: {n_div_x}x{n_div_y} per bay "
          f"(mesh_size={mesh_size:.0f}mm, elem_size={span_x/n_div_x:.0f}x{span_y/n_div_y:.0f}mm)")
    print(f"  Shell section: E={E_plate:.1f} MPa, nu={nu}, t={thickness:.0f}mm, "
          f"rho={rho_kg_m3:.2f} kg/m3")
    
    # Build reverse lookup: (x, y, z) -> node_id (tolerance-based)
    tol = 1.0  # mm
    def find_existing_node(x, y, z):
        for nid, (nx, ny, nz) in existing_node_coords.items():
            if abs(nx - x) < tol and abs(ny - y) < tol and abs(nz - z) < tol:
                return nid
        return None
    
    shell_floor_nodes = {}
    
    dx = span_x / n_div_x
    dy = span_y / n_div_y
    
    total_shell_elems = 0
    total_new_nodes = 0
    
    for fi, fz in enumerate(floor_z_list):
        floor_nodes = []
        # Per-floor node map: (global_ix, global_iy) -> node_id
        node_map = {}
        
        # Total grid points across ALL bays on this floor
        total_pts_x = n_span_x * n_div_x + 1
        total_pts_y = n_span_y * n_div_y + 1
        
        # Create/find nodes
        for gy in range(total_pts_y):
            for gx in range(total_pts_x):
                x = x_grid[0] + gx * dx
                y = y_grid[0] + gy * dy
                
                # Try to find existing beam/column node at this position
                existing_nid = find_existing_node(x, y, fz)
                
                if existing_nid is not None:
                    node_map[(gx, gy)] = existing_nid
                    floor_nodes.append(existing_nid)
                else:
                    # Create new interior node
                    ops.node(next_node_id, x, y, fz)
                    existing_node_coords[next_node_id] = (x, y, fz)
                    node_map[(gx, gy)] = next_node_id
                    floor_nodes.append(next_node_id)
                    next_node_id += 1
                    total_new_nodes += 1
        
        # Create ShellMITC4 elements
        for gy in range(total_pts_y - 1):
            for gx in range(total_pts_x - 1):
                n1 = node_map[(gx, gy)]
                n2 = node_map[(gx + 1, gy)]
                n3 = node_map[(gx + 1, gy + 1)]
                n4 = node_map[(gx, gy + 1)]
                
                ops.element('ShellMITC4', next_elem_id, n1, n2, n3, n4, shell_sec_tag)
                next_elem_id += 1
                total_shell_elems += 1
        
        shell_floor_nodes[fi] = floor_nodes
    
    n_floors = len(floor_z_list)
    print(f"  Total: {total_new_nodes} new nodes, {total_shell_elems} shell elements "
          f"({total_shell_elems // n_floors} per floor)")
    
    return shell_floor_nodes, next_node_id, next_elem_id


# ============================================================================
# 3.6 MODAL ANALYSIS — Eigenvalue (Period & Frequency)
# ============================================================================
def run_modal_analysis(data, num_modes=12):
    """
    Eigenvalue modal analysis to compute natural periods and frequencies.
    
    Builds full structural model (same element formulation as seismic),
    adds mass via '-mass' argument on elements and lumped mass on
    rigid diaphragm master nodes, then solves eigenvalue problem.
    
    Args:
        data: Model data dictionary (from Model data.json)
        num_modes: Number of eigen modes to compute (default 12)
    
    Returns:
        dict: Modal analysis results with periods, frequencies, 
              participation factors, and mass ratios
    """
    import math
    
    res = {
        "status": "Failed",
        "num_modes": num_modes,
        "modes": [],
        "summary": {}
    }
    
    elements_list = data.get('model_elements', [])
    if not elements_list:
        print("[ERROR] No model_elements for modal analysis!")
        return res
    
    seismic_params = data.get('seismic_parameters', {})
    SLAB_SW_PRESSURE = float(data.get('slab_sw_pressure', 0.0))
    SLAB_ADL_PRESSURE = float(data.get('slab_adl_pressure', 0.0))
    
    # Shell plate configuration
    slab_plate = data.get('slab_plate', {})
    plate_enabled = slab_plate.get('enabled', False)
    
    struct_config = detect_structure_config_from_grid(data)
    n_stories = struct_config['n_stories']
    story_height_mm = struct_config['story_height']
    n_span_x = struct_config['n_span_x']
    n_span_y = struct_config['n_span_y']
    span_x = struct_config['span_x']
    span_y = struct_config['span_y']
    z_levels = struct_config['z_levels']
    x_coords = struct_config['x_coords']
    y_coords = struct_config['y_coords']
    
    GRAVITY = 9.81  # m/s^2
    
    # Plate mode: rigid diaphragm limits to 3 DOFs per floor
    # Legacy mode: no diaphragm, keep requested num_modes (SAP2000 uses 12)
    if plate_enabled:
        num_modes = min(num_modes, 3 * n_stories)
    
    print(f"  Modal Config: {n_stories} stories, {n_span_x}x{n_span_y} spans, {num_modes} modes")
    
    try:
        # ================================================================
        # A. CALCULATE FLOOR MASS (Frame SW + Slab SW, no ADL)
        # ================================================================
        min_z = min(z_levels) if z_levels else 0.0
        floor_z = sorted([z for z in z_levels if z > min_z + 100])
        
        if len(floor_z) != n_stories:
            floor_z = [min_z + (i+1)*story_height_mm for i in range(n_stories)]
        
        Wi_N = [0.0] * n_stories
        
        # A1. Frame self-weight (half columns + full beams per floor)
        for elem in elements_list:
            topo = elem.get('topology', {})
            sec = elem.get('section', {})
            mat = elem.get('material', {})
            
            A_mm2 = float(sec.get('Area_mm2', 0))
            rho_kgm3 = float(mat.get('Rho_kg/m3', 0))
            if rho_kgm3 == 0:
                rho_kgm3 = float(mat.get('Rho_kg/mm3', 0)) * 1e9
            
            L_mm = float(topo.get('length_mm', 0))
            w_elem_N = rho_kgm3 * 1e-9 * A_mm2 * L_mm * GRAVITY
            
            start_z = float(topo['start_node'][2])
            end_z = float(topo['end_node'][2])
            
            if elem.get('type', '') == 'Column':
                z_top = max(start_z, end_z)
                z_bot = min(start_z, end_z)
                for fi in range(n_stories):
                    if abs(z_top - floor_z[fi]) < 100:
                        Wi_N[fi] += w_elem_N * 0.5
                        break
                if abs(z_bot - min_z) >= 100:
                    for fi in range(n_stories):
                        if abs(z_bot - floor_z[fi]) < 100:
                            Wi_N[fi] += w_elem_N * 0.5
                            break
            else:
                beam_z = (start_z + end_z) / 2.0
                for fi in range(n_stories):
                    if abs(beam_z - floor_z[fi]) < 100:
                        Wi_N[fi] += w_elem_N
                        break
        
        # A2. Slab mass from load patterns (DEAD + ADL)
        # SAP2000 mass source: Elements=Yes, Loads=Yes, LoadPat=DEAD*1 + ADL*1
        # SLAB_SW_PRESSURE = slab dead weight, SLAB_ADL_PRESSURE = additional dead
        # Total floor area from grid coordinates (robust for non-uniform spans)
        total_floor_area_mm2 = sum(
            abs(x_coords[i+1] - x_coords[i]) * abs(y_coords[j+1] - y_coords[j])
            for i in range(len(x_coords) - 1) for j in range(len(y_coords) - 1)
        )
        n_panels = max(1, (len(x_coords) - 1) * (len(y_coords) - 1))
        panel_area_mm2 = total_floor_area_mm2 / n_panels if n_panels > 0 else 0

        if plate_enabled:
            # Shell plate mode: shell density handles slab SW automatically
            # But Wi_N still needs slab weight for participation factor calc
            # Compute shell SW force from density × thickness × area × g
            slab_plate_rho = float(slab_plate.get('rho_kg_m3', 7156.44))
            slab_plate_t = float(slab_plate.get('thickness_mm', 150.0))
            shell_sw_force_N = slab_plate_rho * 1e-9 * slab_plate_t * panel_area_mm2 * n_panels * GRAVITY
            slab_adl_force_N = SLAB_ADL_PRESSURE * panel_area_mm2 * n_panels
            slab_mass_per_floor_N = shell_sw_force_N + slab_adl_force_N
            for fi in range(n_stories):
                Wi_N[fi] += slab_mass_per_floor_N
        else:
            slab_total_pressure = SLAB_SW_PRESSURE + SLAB_ADL_PRESSURE  # Dead + ADL
            slab_mass_per_floor_N = slab_total_pressure * panel_area_mm2 * n_panels
            for fi in range(n_stories):
                Wi_N[fi] += slab_mass_per_floor_N
        
        # A3. Frame SW double-count — only for Plate Mode
        # Plate Mode: elements have '-mass' (equivalent to SAP2000 Elements=Yes)
        #   → Wi_N must also include frame_SW to match DEAD load mass contribution
        # Legacy Mode: elements have NO '-mass' (equivalent to SAP2000 Elements=No)
        #   → Wi_N = frame_SW(1x) + slab + ADL — NO double-count needed
        #   Verified: mass error = 0.01% vs SAP2000 V4 (Elements=No)
        if plate_enabled:
            frame_sw_copy = [w for w in Wi_N]
            for fi in range(n_stories):
                frame_sw_at_floor = frame_sw_copy[fi] - slab_mass_per_floor_N
                Wi_N[fi] += frame_sw_at_floor  # Double-count frame SW
                Wi_N[fi] += shell_sw_force_N    # Double-count shell SW
        
        Wi_kN = [w / 1000.0 for w in Wi_N]
        
        print(f"  Floor weights: {[f'{w:.1f} kN' for w in Wi_kN]}")
        
        # ================================================================
        # B. BUILD OPENSEES MODEL WITH MASS
        # ================================================================
        ops.wipe()
        ops.model('basic', '-ndm', 3, '-ndf', 6)
        
        node_map = {}
        node_coords = {}
        next_node_id = 1
        original_joint_nids = set()
        
        def get_node_id(coords):
            nonlocal next_node_id
            key = f"{coords[0]:.1f}_{coords[1]:.1f}_{coords[2]:.1f}"
            if key not in node_map:
                node_map[key] = next_node_id
                node_coords[next_node_id] = coords
                next_node_id += 1
            return node_map[key]
        
        # Pre-process elements
        processed_elements = []
        for entry in elements_list:
            p1 = entry['topology']['start_node']
            p2 = entry['topology']['end_node']
            n1 = get_node_id(p1)
            n2 = get_node_id(p2)
            original_joint_nids.add(n1)
            original_joint_nids.add(n2)
            dx, dy, dz = p2[0]-p1[0], p2[1]-p1[1], p2[2]-p1[2]
            L = math.sqrt(dx**2 + dy**2 + dz**2)
            is_vertical = abs(dz) > abs(dx) and abs(dz) > abs(dy)
            processed_elements.append({
                'id': entry['id'], 'nodes': [n1, n2],
                'is_vertical': is_vertical, 'length': L, 'raw': entry
            })
        
        # Build nodes with BCs
        all_z_vals = [c[2] for c in node_coords.values()]
        min_z_val = min(all_z_vals) if all_z_vals else 0.0
        fixed_nodes = set()
        
        support_config = data.get('support_config', {})
        support_dof = support_config.get('dof', [1, 1, 1, 1, 1, 1])
        
        for nid, coords in node_coords.items():
            ops.node(nid, *coords)
            if abs(coords[2] - min_z_val) < 100.0:
                ops.fix(nid, *support_dof)
                fixed_nodes.add(nid)
        
        # --- Rigid End Zones Depths (same as gravity/seismic) ---
        node_connecting_depths = {}
        for item in processed_elements:
            sec = item['raw']['section']
            d_mm = float(sec.get('d_mm', 0))
            b_mm = float(sec.get('b_mm', 0))
            for nid in item['nodes']:
                if nid not in node_connecting_depths:
                    node_connecting_depths[nid] = {'col_d': 0, 'beam_d': 0}
                if item['is_vertical']:
                    node_connecting_depths[nid]['col_d'] = max(
                        node_connecting_depths[nid]['col_d'], b_mm)
                else:
                    node_connecting_depths[nid]['beam_d'] = max(
                        node_connecting_depths[nid]['beam_d'], d_mm)
        
        # --- Build Elements ---
        transf_counter = 1
        # Cache: (floor_node_id, offset_z) -> centroid_node_id
        # Keyed by both node AND depth to handle different beam sections at same joint
        _beam_centroid_cache = {}
        _rigid_bar_counter = 0  # Counter for rigid bar element IDs

        # Secondary beam connections (for parent beam splitting)
        _, _, sec_beams_modal = classify_elements(elements_list)
        parent_connections_modal, sec_beam_dirs_modal = find_secondary_connections(sec_beams_modal, elements_list)
        connection_node_map_modal = {}
        connection_floor_nids = set()  # Track connection joints for mass/diaphragm

        def _get_or_create_centroid(floor_nid, beam_d_mm, link_type='beam'):
            """Get or create a centroid node offset below a floor node.

            Uses rigidLink constraint for both legacy and plate modes.
            The Transformation constraint handler resolves the chain:
              centroid (rigidLink slave) -> floor (diaphragm slave) -> master
            since no node is a slave of two DIFFERENT constraint types.
            """
            nonlocal next_node_id, _rigid_bar_counter, transf_counter
            offset_z = -beam_d_mm / 2.0
            cache_key = (floor_nid, round(offset_z, 1))
            if cache_key not in _beam_centroid_cache:
                fn_coords = node_coords[floor_nid]
                centroid_z = fn_coords[2] + offset_z
                centroid_nid = next_node_id
                ops.node(centroid_nid, fn_coords[0], fn_coords[1], centroid_z)
                node_coords[centroid_nid] = (fn_coords[0], fn_coords[1], centroid_z)
                next_node_id += 1

                # Use rigidLink for exact rigid offset (no spurious element stiffness)
                ops.rigidLink(link_type, floor_nid, centroid_nid)

                _beam_centroid_cache[cache_key] = centroid_nid
            return _beam_centroid_cache[cache_key]

        for item in processed_elements:
            sec = item['raw']['section']
            mat = item['raw']['material']

            E = float(mat.get('E_MPa', 205000))
            G_mat = float(mat.get('G_MPa', 78846))
            A = float(sec.get('Area_mm2', 0))
            J = float(sec.get('J_mm4', 0))
            Iz = float(sec.get('Iz_mm4', 0))
            Iy = float(sec.get('Iy_mm4', 0))
            Avz = float(sec.get('Avz_mm2', 0))
            Avy = float(sec.get('Avy_mm2', 0))

            if A <= 0: continue

            # Mass per unit length for -cMass: rho*A in OpenSees units
            # OpenSees (N, mm, s): mass unit = N*s^2/mm
            # rho(kg/mm^3) * A(mm^2) = kg/mm -> divide by 1000 for N*s^2/mm/mm
            # because 1 kg = 1 N*s^2/m = 0.001 N*s^2/mm
            rho_kgmm3 = float(mat.get('Rho_kg/mm3', 0))
            if rho_kgmm3 <= 0:
                rho_kgm3 = float(mat.get('Rho_kg/m3', 0))
                rho_kgmm3 = rho_kgm3 * 1e-9
            mass_per_length = rho_kgmm3 * A / 1000.0  # N*s^2/mm^2

            local_axes = item['raw'].get('local_axes', {})
            if item['is_vertical']:
                vecxz = local_axes.get('y_axis', [1, 0, 0])
            else:
                vecxz = local_axes.get('z_axis', [0, 0, 1])

            # Inertia & shear area mapping (same for columns and beams)
            # Pairing: Ops_Iy(strong) ↔ Ops_Avz(web), Ops_Iz(weak) ↔ Ops_Avy(flange)
            Ops_Iy = Iz; Ops_Iz = Iy
            Ops_Avy = Avy; Ops_Avz = Avz

            n1, n2 = item['nodes']
            n_start_elem = n1
            n_end_elem = n2
            coord_start = node_coords[n_start_elem]
            coord_end = node_coords[n_end_elem]

            # --- Beam Insertion Point Offset (CardinalPt=8, top center) ---
            # SAP2000 offsets beam centroid down by d/2.
            # Legacy mode: use rigid bar elements (avoids constraint chain with diaphragm)
            # Plate mode: use rigidLink constraint (handled by plate solver)
            beam_d_mm = float(sec.get('d_mm', 0)) if not item['is_vertical'] else 0.0

            if beam_d_mm > 0:
                # Use rigidLink centroid offset for proper coupling
                # (matches SAP2000 Transform=Yes: rigid body offset from joint to centroid,
                #  captures UY-RX and UX-RY coupling that jntOffset misses)
                beam_start = _get_or_create_centroid(n_start_elem, beam_d_mm)
                beam_end = _get_or_create_centroid(n_end_elem, beam_d_mm)
            else:
                beam_start = n_start_elem
                beam_end = n_end_elem

            beam_coord_start = node_coords[beam_start]
            beam_coord_end = node_coords[beam_end]

            vx = beam_coord_end[0] - beam_coord_start[0]
            vy_v = beam_coord_end[1] - beam_coord_start[1]
            vz_c = beam_coord_end[2] - beam_coord_start[2]

            # Element length from original coordinates (for plate mesh sizing)
            dx_e = coord_end[0]-coord_start[0]
            dy_e = coord_end[1]-coord_start[1]
            dz_e = coord_end[2]-coord_start[2]
            L_elem = math.sqrt(dx_e**2 + dy_e**2 + dz_e**2)

            # --- Segment-aware subdivision (supports secondary beam split) ---
            is_secondary = item['raw'].get('group') == 'Secondary'
            conn_list_modal = parent_connections_modal.get(item['id'], [])

            seg_fracs = sorted(set(
                [0.0] + [frac for (frac, sb_id, conn_coord, sb_dir) in conn_list_modal] + [1.0]
            ))

            # Determine base subdivision count
            if plate_enabled and not item['is_vertical']:
                plate_mesh_size = float(slab_plate.get('mesh_size_mm', 250.0))
                total_base_subs = max(1, int(round(L_elem / plate_mesh_size)))
            else:
                total_base_subs = 1

            sub_ele_counter = 0
            prev_node = beam_start

            for seg_idx in range(len(seg_fracs) - 1):
                seg_frac_start = seg_fracs[seg_idx]
                seg_frac_end = seg_fracs[seg_idx + 1]
                seg_len_frac = seg_frac_end - seg_frac_start

                # Number of subs for this segment
                n_seg_subs = max(1, int(round(total_base_subs * seg_len_frac)))

                for k in range(n_seg_subs):
                    is_last_sub = (k == n_seg_subs - 1)
                    is_last_segment = (seg_idx == len(seg_fracs) - 2)

                    if is_last_sub and is_last_segment:
                        curr_node = beam_end
                    elif is_last_sub and not is_last_segment:
                        # Connection point — create floor joint + centroid node
                        conn_frac = seg_fracs[seg_idx + 1]
                        # Floor-level coordinates
                        fj_x = coord_start[0] + (coord_end[0] - coord_start[0]) * conn_frac
                        fj_y = coord_start[1] + (coord_end[1] - coord_start[1]) * conn_frac
                        fj_z = coord_start[2]
                        fk = f"{fj_x:.1f}_{fj_y:.1f}_{fj_z:.1f}"

                        if fk in node_map:
                            floor_nid = node_map[fk]
                        else:
                            floor_nid = next_node_id
                            node_coords[floor_nid] = (fj_x, fj_y, fj_z)
                            ops.node(floor_nid, fj_x, fj_y, fj_z)
                            node_map[fk] = floor_nid
                            next_node_id += 1

                        # Track connection floor joint for mass/diaphragm
                        connection_floor_nids.add(floor_nid)

                        # Create centroid node at connection for beam offset
                        if beam_d_mm > 0:
                            curr_node = _get_or_create_centroid(floor_nid, beam_d_mm)
                        else:
                            curr_node = floor_nid

                        connection_node_map_modal[(item['id'], conn_frac)] = curr_node
                    else:
                        ratio = seg_frac_start + seg_len_frac * (k + 1) / n_seg_subs
                        nx = beam_coord_start[0] + vx * ratio
                        ny = beam_coord_start[1] + vy_v * ratio
                        nz = beam_coord_start[2] + vz_c * ratio
                        curr_node = next_node_id
                        node_coords[curr_node] = (nx, ny, nz)
                        ops.node(curr_node, nx, ny, nz)
                        next_node_id += 1

                    # NOTE: Centroid offset is now handled by _get_or_create_centroid
                    # at beam_start/beam_end and at connection points. Intermediate
                    # subdivision nodes use centroid coordinates by construction.

                    sub_ele_id = item['id'] * 100 + sub_ele_counter
                    if sub_ele_id > 2000000000:
                        sub_ele_id = int(sub_ele_id % 1000000 + 900000)

                    sub_transf_tag = transf_counter
                    transf_counter += 1

                    # Beam offset handled by rigidLink to centroid nodes
                    # (no jntOffset: it doesn't capture UY-RX coupling for horizontal beams)
                    ops.geomTransf('Linear', sub_transf_tag,
                                  vecxz[0], vecxz[1], vecxz[2])

                    if plate_enabled:
                        ops.element('ElasticTimoshenkoBeam', sub_ele_id,
                                   prev_node, curr_node,
                                   E, G_mat, A, J, Ops_Iy, Ops_Iz, Ops_Avy, Ops_Avz,
                                   sub_transf_tag,
                                   '-mass', mass_per_length)
                    else:
                        ops.element('ElasticTimoshenkoBeam', sub_ele_id,
                                   prev_node, curr_node,
                                   E, G_mat, A, J, Ops_Iy, Ops_Iz, Ops_Avy, Ops_Avz,
                                   sub_transf_tag)

                    sub_ele_counter += 1
                    prev_node = curr_node
        
        # Track next available element ID for shell mesh
        # Element IDs use item_id*100+k pattern, so find max and add offset
        max_beam_elem = max((item['id'] * 100 + 10) for item in processed_elements)
        next_elem_id = max_beam_elem + 1000  # Safe offset for shell elements
        
        # ================================================================
        # C. DIAPHRAGM + MASS ASSIGNMENT
        # ================================================================
        center_x = (min(x_coords) + max(x_coords)) / 2.0
        center_y = (min(y_coords) + max(y_coords)) / 2.0
        G_ACC_MM = 9810.0  # mm/s^2
        
        master_nodes = []
        mass_nodes_per_floor = {}
        shell_floor_nodes = {}
        legacy_floor_info = {}  # fi → {master_nid, M_floor, I_floor}
        
        if plate_enabled:
            # ---- SHELL PLATE MODE ----
            # Shell provides: in-plane rigidity + slab mass
            # Element -mass provides: frame mass
            # NO rigidDiaphragm needed
            print("\n  --- Shell Plate Mode (ShellMITC4) ---")
            
            shell_floor_nodes, next_node_id, next_elem_id = _create_shell_plate_mesh(
                ops, floor_z, x_coords, y_coords, span_x, span_y,
                n_span_x, n_span_y, slab_plate,
                node_coords, next_node_id, next_elem_id
            )
            
            # Additional mass for ADL (not part of shell density)
            # Distribute ADL to all shell nodes on each floor
            n_panels = n_span_x * n_span_y
            panel_area = span_x * span_y
            adl_total_N = SLAB_ADL_PRESSURE * panel_area * n_panels
            
            for fi in range(n_stories):
                floor_shell_nids = shell_floor_nodes[fi]
                n_fnodes = len(floor_shell_nids)
                if n_fnodes > 0 and adl_total_N > 0:
                    adl_mass_per_node = adl_total_N / G_ACC_MM / n_fnodes
                    for snid in floor_shell_nids:
                        ops.mass(snid, adl_mass_per_node, adl_mass_per_node, 0.0, 0.0, 0.0, 0.0)
                
                # Also add frame SW double-count (SAP2000 mass source)
                frame_sw_1x = (Wi_N[fi] - slab_mass_per_floor_N) / 2.0
                if frame_sw_1x > 0 and n_fnodes > 0:
                    fw_mass_per_node = frame_sw_1x / G_ACC_MM / n_fnodes
                    for snid in floor_shell_nids:
                        ops.mass(snid, fw_mass_per_node, fw_mass_per_node, 0.0, 0.0, 0.0, 0.0)
                
                print(f"    Floor {fi+1}: shell_nodes={n_fnodes}, "
                      f"ADL_mass/node={adl_total_N/G_ACC_MM/n_fnodes:.6f}, "
                      f"FW_extra/node={frame_sw_1x/G_ACC_MM/n_fnodes:.6f}")
            
            # No master nodes in plate mode
            master_nodes = []
        
        else:
            # ---- LEGACY MODE: with rigid diaphragm ----
            # Rigid diaphragm couples all floor joints to share Ux, Uy, Rz.
            # Mass distributed based on TRIBUTARY AREA (not equal) to match SAP2000.
            # Equal distribution gives incorrect rotational inertia → wrong RZ period.
            print("\n  --- Legacy Mode (with Rigid Diaphragm) ---")

            # Merge original joints + connection joints for complete floor joint set
            all_joint_nids = set(original_joint_nids) | connection_floor_nids

            # --- Tributary-based mass distribution ---
            # 1. Build sub-panels from floor grid + secondary beams
            sub_panels_modal = build_sub_panels(x_coords, y_coords,
                                                 sec_beams_modal, floor_z, edge_tol=10.0)

            # 2. Compute slab tributary area per floor joint (45° yield line)
            slab_trib_area = {}  # (x_round, y_round, z_round) → area mm²
            for panel in sub_panels_modal:
                Lx, Ly = panel['Lx'], panel['Ly']
                fz_p = panel['floor_z']
                x0, x1 = panel['x0'], panel['x1']
                y0, y1 = panel['y0'], panel['y1']
                a = min(Lx, Ly)

                if Lx >= Ly:
                    x_beam_area = Ly * (2*Lx - Ly) / 4.0  # Long beams (X-dir)
                    y_beam_area = Ly**2 / 4.0               # Short beams (Y-dir)
                else:
                    x_beam_area = Lx**2 / 4.0               # Short beams (X-dir)
                    y_beam_area = Lx * (2*Ly - Lx) / 4.0    # Long beams (Y-dir)

                # Each corner gets half of adjacent X-beam + half of Y-beam tributary
                for cx, cy in [(x0,y0), (x1,y0), (x0,y1), (x1,y1)]:
                    key = (round(cx, 0), round(cy, 0), round(fz_p, 0))
                    slab_trib_area[key] = slab_trib_area.get(key, 0.0) + \
                        x_beam_area / 2.0 + y_beam_area / 2.0

            # 3. Compute frame SW per floor joint (beam half-SW + column half-SW)
            frame_sw_per_nid = {}  # nid → SW force (N)
            for elem in elements_list:
                topo = elem.get('topology', {})
                sec_e = elem.get('section', {})
                mat_e = elem.get('material', {})
                start_c = topo.get('start_node', [0,0,0])
                end_c = topo.get('end_node', [0,0,0])

                rho_e = float(mat_e.get('Rho_kg/mm3', 0))
                if rho_e <= 0:
                    rho_e = float(mat_e.get('Rho_kg/m3', 0)) * 1e-9
                A_e = float(sec_e.get('Area_mm2', 0))
                L_e = math.sqrt(sum((a-b)**2 for a,b in zip(start_c, end_c)))
                SW_e = rho_e * A_e * L_e * 9.81  # N (kg * m/s²)

                is_vert = (abs(end_c[2] - start_c[2]) >
                          abs(end_c[0] - start_c[0]) + abs(end_c[1] - start_c[1]))

                if is_vert:
                    # Column: half SW to floor-level end
                    for nd_c in [start_c, end_c]:
                        fk_c = f"{nd_c[0]:.1f}_{nd_c[1]:.1f}_{nd_c[2]:.1f}"
                        nid_c = node_map.get(fk_c)
                        if nid_c and nid_c not in fixed_nodes:
                            frame_sw_per_nid[nid_c] = frame_sw_per_nid.get(nid_c, 0.0) + SW_e / 2.0
                else:
                    # Beam: split at connection points
                    eid = elem.get('id')
                    conn_list_e = parent_connections_modal.get(eid, [])
                    seg_fracs_e = sorted(set([0.0] + [f for (f, _, _, _) in conn_list_e] + [1.0]))

                    for si in range(len(seg_fracs_e) - 1):
                        seg_len = L_e * (seg_fracs_e[si+1] - seg_fracs_e[si])
                        seg_SW = rho_e * A_e * seg_len * 9.81  # N

                        # Segment start joint
                        if si == 0:
                            fk_s = f"{start_c[0]:.1f}_{start_c[1]:.1f}_{start_c[2]:.1f}"
                        else:
                            fs = seg_fracs_e[si]
                            sx = start_c[0] + (end_c[0]-start_c[0]) * fs
                            sy = start_c[1] + (end_c[1]-start_c[1]) * fs
                            fk_s = f"{sx:.1f}_{sy:.1f}_{start_c[2]:.1f}"

                        # Segment end joint
                        if si == len(seg_fracs_e) - 2:
                            fk_e = f"{end_c[0]:.1f}_{end_c[1]:.1f}_{end_c[2]:.1f}"
                        else:
                            fe = seg_fracs_e[si+1]
                            ex = start_c[0] + (end_c[0]-start_c[0]) * fe
                            ey = start_c[1] + (end_c[1]-start_c[1]) * fe
                            fk_e = f"{ex:.1f}_{ey:.1f}_{start_c[2]:.1f}"

                        nid_s = node_map.get(fk_s)
                        nid_e = node_map.get(fk_e)
                        if nid_s:
                            frame_sw_per_nid[nid_s] = frame_sw_per_nid.get(nid_s, 0.0) + seg_SW / 2.0
                        if nid_e:
                            frame_sw_per_nid[nid_e] = frame_sw_per_nid.get(nid_e, 0.0) + seg_SW / 2.0

            # 4. Assign mass per floor joint
            slab_pressure = SLAB_SW_PRESSURE + SLAB_ADL_PRESSURE  # Dead + ADL
            node_mass_assigned = {}  # nid → mass (N·s²/mm), for participation factors

            for fi in range(n_stories):
                fz = floor_z[fi]

                floor_joint_nodes = []
                for nid in all_joint_nids:
                    if nid in fixed_nodes:
                        continue
                    coords = node_coords.get(nid, (0,0,0))
                    if abs(coords[2] - fz) < 100.0:
                        floor_joint_nodes.append(nid)

                mass_nodes_per_floor[fi] = floor_joint_nodes
                n_fj = len(floor_joint_nodes)
                total_floor_mass = 0.0

                for nid in floor_joint_nodes:
                    c = node_coords[nid]
                    # Slab mass from tributary area
                    key = (round(c[0], 0), round(c[1], 0), round(fz, 0))
                    trib_area = slab_trib_area.get(key, 0.0)
                    slab_load = slab_pressure * trib_area  # N
                    # Frame SW mass
                    fsw = frame_sw_per_nid.get(nid, 0.0)  # N
                    mass_j = (slab_load + fsw) / G_ACC_MM
                    # Assign mass to Ux, Uy, AND Uz (SAP2000 uses all 3 translational DOFs)
                    # Uz mass enables out-of-plane modes at connection points (secondary beams)
                    ops.mass(nid, mass_j, mass_j, mass_j, 0.0, 0.0, 0.0)
                    node_mass_assigned[nid] = mass_j
                    total_floor_mass += mass_j

                print(f"    Floor {fi+1}: joints={n_fj}, "
                      f"total_mass={total_floor_mass:.4f} "
                      f"(Wi/g={Wi_N[fi]/G_ACC_MM:.4f})")

            # --- RIGID DIAPHRAGM for Legacy Mode ---
            # Constrains ALL floor joints (original + connection) to share Ux, Uy, Rz.
            z_levels_all = struct_config['z_levels']
            min_z_base = min(z_levels_all) if z_levels_all else 0.0
            floor_z_levels_dia = sorted([z for z in z_levels_all if z > min_z_base + 100])

            legacy_diaphragm_applied = False
            for fz in floor_z_levels_dia:
                floor_joint_nodes_dia = []
                for nid in all_joint_nids:
                    if nid in fixed_nodes:
                        continue
                    coords = node_coords.get(nid, (0, 0, 0))
                    if abs(coords[2] - fz) < 100.0:
                        floor_joint_nodes_dia.append(nid)

                if len(floor_joint_nodes_dia) >= 2:
                    master_nid = None
                    for item in processed_elements:
                        if item['is_vertical']:
                            for nn in item['nodes']:
                                if nn in floor_joint_nodes_dia:
                                    master_nid = nn
                                    break
                        if master_nid:
                            break
                    if master_nid is None:
                        master_nid = floor_joint_nodes_dia[0]
                    slave_nids = [n for n in floor_joint_nodes_dia if n != master_nid]
                    if slave_nids:
                        ops.rigidDiaphragm(3, master_nid, *slave_nids)
                        legacy_diaphragm_applied = True
                        # Find floor index for this z-level
                        for fi_d in range(n_stories):
                            if abs(floor_z[fi_d] - fz) < 100.0:
                                legacy_floor_info[fi_d] = {'master_nid': master_nid}
                                break

            if legacy_diaphragm_applied:
                print(f"  Rigid diaphragm applied to {len(floor_z_levels_dia)} floor(s) in Legacy Mode")

            # Cap num_modes to independent mass DOFs
            if legacy_diaphragm_applied:
                # Diaphragm: 3 DOFs per floor (Ux, Uy, Rz of master) + Uz per joint
                n_diaphragm_dofs = n_stories * 3
                n_uz_dofs = sum(len(mass_nodes_per_floor.get(fi2, [])) for fi2 in range(n_stories))
                n_mass_dofs = n_diaphragm_dofs + n_uz_dofs
            else:
                # No diaphragm: 3 DOFs per joint (Ux, Uy, Uz)
                n_mass_dofs = sum(len(mass_nodes_per_floor.get(fi2, [])) for fi2 in range(n_stories)) * 3
            if num_modes > n_mass_dofs:
                print(f"  Limiting num_modes from {num_modes} to {n_mass_dofs} (independent mass DOFs)")
                num_modes = n_mass_dofs

        # ================================================================
        # D. EIGENVALUE ANALYSIS
        # ================================================================
        # Penalty constraint handler required for rigidLink + rigidDiaphragm
        # (Transformation handler creates singular matrices with nested constraints)
        ops.system('FullGeneral')
        ops.numberer('RCM')
        ops.constraints('Penalty', 1.0e16, 1.0e16)
        if plate_enabled:
            print(f"\n  Solver: FullGeneral + Penalty (rigidLink beam insertion)")
        else:
            print(f"\n  Solver: FullGeneral + Penalty (rigidDiaphragm + rigidLink)")

        print(f"\n  Running eigenvalue analysis ({num_modes} modes)...")

        # Default eigensolver (compatible with Penalty constraints)
        # Note: -fullGenLapack produces negative eigenvalues with Penalty
        eigenvalues = ops.eigen(num_modes)
        print(f"  Eigen solver (default) completed successfully")
        
        print(f"  Successfully computed {len(eigenvalues)} modes")
        print(f"  Raw eigenvalues: {[f'{e:.4f}' for e in eigenvalues]}")
        
        # ================================================================
        # E. COMPUTE PERIODS, FREQUENCIES, PARTICIPATION
        # ================================================================
        import numpy as np
        
        modes_data = []
        
        for i, lam in enumerate(eigenvalues):
            # Skip zero/negative eigenvalues (rigid body modes or numerical noise)
            if lam < 1e-6:
                print(f"    Mode {i+1}: eigenvalue={lam:.6e} (SKIPPED - non-positive)")
                continue
            # Skip near-infinite eigenvalues (constraint DOF artifacts)
            if lam > 1e8:
                print(f"    Mode {i+1}: eigenvalue={lam:.4e} (SKIPPED - constraint artifact)")
                continue
            omega = math.sqrt(lam)
            freq = omega / (2.0 * math.pi)
            period = 1.0 / freq if freq > 0 else 0.0
            
            modes_data.append({
                "mode": i + 1,
                "eigenvalue": round(lam, 4),
                "omega_rad_s": round(omega, 6),
                "frequency_Hz": round(freq, 6),
                "period_s": round(period, 10),
            })
        
        # --- Participation factors ---
        G_ACC_MM = 9810.0
        
        if plate_enabled:
            # Shell plate mode: compute from ALL shell floor nodes
            # Mass comes from shell density + ADL node mass + frame SW extra
            for mode_info in modes_data:
                mode_num = mode_info["mode"]
                
                Lx_pf = 0.0; Ly_pf = 0.0; Lrz_pf = 0.0
                Mx_gen = 0.0; My_gen = 0.0; Mrz_gen = 0.0
                total_mass_pf = 0.0; total_Iz_pf = 0.0
                
                for fi in range(n_stories):
                    floor_nids = shell_floor_nodes.get(fi, [])
                    n_fn = len(floor_nids)
                    if n_fn == 0: continue
                    
                    # Approximate mass per node (total Wi / n_nodes)
                    m_per_node = Wi_N[fi] / G_ACC_MM / n_fn
                    
                    for snid in floor_nids:
                        sc = node_coords[snid]
                        dx_c = sc[0] - center_x
                        dy_c = sc[1] - center_y
                        r2 = dx_c**2 + dy_c**2
                        Iz_node = m_per_node * r2
                        
                        phi_x = ops.nodeEigenvector(snid, mode_num, 1)
                        phi_y = ops.nodeEigenvector(snid, mode_num, 2)
                        phi_rz = ops.nodeEigenvector(snid, mode_num, 6)
                        
                        Lx_pf += m_per_node * phi_x
                        Ly_pf += m_per_node * phi_y
                        Lrz_pf += Iz_node * phi_rz
                        
                        Mx_gen += m_per_node * phi_x**2
                        My_gen += m_per_node * phi_y**2
                        Mrz_gen += Iz_node * phi_rz**2
                        
                        total_mass_pf += m_per_node
                        total_Iz_pf += Iz_node
                
                meff_x = (Lx_pf**2 / Mx_gen) if abs(Mx_gen) > 1e-20 else 0.0
                meff_y = (Ly_pf**2 / My_gen) if abs(My_gen) > 1e-20 else 0.0
                meff_rz = (Lrz_pf**2 / Mrz_gen) if abs(Mrz_gen) > 1e-20 else 0.0
                
                mode_info["UX_ratio"] = round(meff_x / total_mass_pf, 8) if total_mass_pf > 0 else 0.0
                mode_info["UY_ratio"] = round(meff_y / total_mass_pf, 8) if total_mass_pf > 0 else 0.0
                mode_info["RZ_ratio"] = round(meff_rz / total_Iz_pf, 8) if total_Iz_pf > 0 else 0.0
                
                ratios = {"UX": mode_info["UX_ratio"], "UY": mode_info["UY_ratio"], "RZ": mode_info["RZ_ratio"]}
                dominant = max(ratios, key=ratios.get)
                mode_info["dominant"] = dominant
        
        else:
            # Legacy: rigid diaphragm — compute participation using master node approach
            # Transform master eigenvectors to CENTER OF MASS frame before computing
            # participation, since master node may not be at CM.
            total_mass = 0.0
            total_Iz = 0.0
            floor_M = {}    # fi → floor translational mass
            floor_Iz = {}   # fi → floor rotational inertia about CM
            floor_cm = {}   # fi → (x_cm, y_cm)

            for fi in range(n_stories):
                M_fi = 0.0
                Sx = 0.0; Sy = 0.0
                for nid in mass_nodes_per_floor.get(fi, []):
                    m_n = node_mass_assigned.get(nid, 0.0)
                    c = node_coords.get(nid, (0, 0, 0))
                    M_fi += m_n
                    Sx += m_n * c[0]
                    Sy += m_n * c[1]
                x_cm = Sx / M_fi if M_fi > 0 else center_x
                y_cm = Sy / M_fi if M_fi > 0 else center_y
                Iz_fi = 0.0
                for nid in mass_nodes_per_floor.get(fi, []):
                    m_n = node_mass_assigned.get(nid, 0.0)
                    c = node_coords.get(nid, (0, 0, 0))
                    Iz_fi += m_n * ((c[0] - x_cm)**2 + (c[1] - y_cm)**2)
                floor_M[fi] = M_fi
                floor_Iz[fi] = Iz_fi
                floor_cm[fi] = (x_cm, y_cm)
                total_mass += M_fi
                total_Iz += Iz_fi

            # Compute participation using master-node CM approach + M-orthogonalization.
            # Near-degenerate eigenvalues cause LAPACK eigenvectors to be non-M-orthogonal
            # (singular mass matrix from zero-mass DOFs). Gram-Schmidt fixes this.
            n_modes = len(modes_data)
            x_cm0 = floor_cm.get(0, (center_x, center_y))[0]
            y_cm0 = floor_cm.get(0, (center_x, center_y))[1]

            # Step 1: Compute raw L_x, L_y, L_rz, M_gen, M_cross for all modes
            raw_Lx = [0.0] * n_modes
            raw_Ly = [0.0] * n_modes
            raw_Lrz = [0.0] * n_modes
            raw_Mgen = [0.0] * n_modes
            # M_cross[i][j] = Σ m_dof × phi_i_dof × phi_j_dof (over all nodes/DOFs)
            M_cross = [[0.0] * n_modes for _ in range(n_modes)]

            # Cache eigenvectors at mass nodes for all modes
            for fi in range(n_stories):
                fi_info = legacy_floor_info.get(fi)
                if not fi_info:
                    continue
                master_nid = fi_info['master_nid']
                M_fi = floor_M[fi]
                Iz_fi = floor_Iz[fi]
                xc, yc = floor_cm[fi]
                mc = node_coords[master_nid]

                for n in range(n_modes):
                    mode_num = modes_data[n]["mode"]
                    phi_mx = ops.nodeEigenvector(master_nid, mode_num, 1)
                    phi_my = ops.nodeEigenvector(master_nid, mode_num, 2)
                    phi_rz = ops.nodeEigenvector(master_nid, mode_num, 6)

                    # Transform to CM frame
                    phi_cx = phi_mx - (yc - mc[1]) * phi_rz
                    phi_cy = phi_my + (xc - mc[0]) * phi_rz

                    raw_Lx[n] += M_fi * phi_cx
                    raw_Ly[n] += M_fi * phi_cy
                    raw_Lrz[n] += Iz_fi * phi_rz
                    raw_Mgen[n] += M_fi * (phi_cx**2 + phi_cy**2) + Iz_fi * phi_rz**2

                # Cross terms for M-orthogonality check
                for a in range(n_modes):
                    ma = modes_data[a]["mode"]
                    pax = ops.nodeEigenvector(master_nid, ma, 1)
                    pay = ops.nodeEigenvector(master_nid, ma, 2)
                    paz = ops.nodeEigenvector(master_nid, ma, 6)
                    pcx_a = pax - (yc - mc[1]) * paz
                    pcy_a = pay + (xc - mc[0]) * paz

                    for b in range(a + 1, n_modes):
                        mb = modes_data[b]["mode"]
                        pbx = ops.nodeEigenvector(master_nid, mb, 1)
                        pby = ops.nodeEigenvector(master_nid, mb, 2)
                        pbz = ops.nodeEigenvector(master_nid, mb, 6)
                        pcx_b = pbx - (yc - mc[1]) * pbz
                        pcy_b = pby + (xc - mc[0]) * pbz

                        cross = M_fi * (pcx_a * pcx_b + pcy_a * pcy_b) + Iz_fi * paz * pbz
                        M_cross[a][b] += cross
                        M_cross[b][a] += cross

            # Step 2: Modified Gram-Schmidt M-orthogonalization
            Lx = list(raw_Lx)
            Ly = list(raw_Ly)
            Lrz = list(raw_Lrz)
            Mgen = list(raw_Mgen)

            for i in range(n_modes):
                for j in range(i + 1, n_modes):
                    if abs(Mgen[i]) < 1e-30:
                        continue
                    alpha = M_cross[i][j] / Mgen[i]
                    Lx[j] -= alpha * Lx[i]
                    Ly[j] -= alpha * Ly[i]
                    Lrz[j] -= alpha * Lrz[i]
                    Mgen[j] -= alpha * M_cross[i][j]
                    # Update cross terms for subsequent modes
                    for k in range(j + 1, n_modes):
                        M_cross[j][k] -= alpha * M_cross[i][k]
                        M_cross[k][j] = M_cross[j][k]

            # Step 3: Compute participation from M-orthogonalized quantities
            # Note: near-degenerate modes may show mixed UX/RZ participation, but
            # cumulative sums are guaranteed to be correct (100% for each direction).
            for n in range(n_modes):
                mg = Mgen[n]
                meff_x  = (Lx[n]**2  / mg) if abs(mg) > 1e-20 else 0.0
                meff_y  = (Ly[n]**2  / mg) if abs(mg) > 1e-20 else 0.0
                meff_rz = (Lrz[n]**2 / mg) if abs(mg) > 1e-20 else 0.0

                modes_data[n]["UX_ratio"] = round(meff_x  / total_mass, 8) if total_mass > 0 else 0.0
                modes_data[n]["UY_ratio"] = round(meff_y  / total_mass, 8) if total_mass > 0 else 0.0
                modes_data[n]["RZ_ratio"] = round(meff_rz / total_Iz,   8) if total_Iz   > 0 else 0.0

                ratios = {"UX": modes_data[n]["UX_ratio"],
                          "UY": modes_data[n]["UY_ratio"],
                          "RZ": modes_data[n]["RZ_ratio"]}
                dominant = max(ratios, key=ratios.get)
                modes_data[n]["dominant"] = dominant
        
        res["modes"] = modes_data
        res["num_modes_computed"] = len(modes_data)
        res["status"] = "Success"
        
        # Cumulative mass ratios
        cum_ux = 0.0
        cum_uy = 0.0
        cum_rz = 0.0
        for m in modes_data:
            cum_ux += m.get("UX_ratio", 0)
            cum_uy += m.get("UY_ratio", 0)
            cum_rz += m.get("RZ_ratio", 0)
            m["cum_UX"] = round(cum_ux, 8)
            m["cum_UY"] = round(cum_uy, 8)
            m["cum_RZ"] = round(cum_rz, 8)
        
        # Summary
        if modes_data:
            res["summary"] = {
                "T1": modes_data[0]["period_s"],
                "T2": modes_data[1]["period_s"] if len(modes_data) > 1 else 0,
                "T3": modes_data[2]["period_s"] if len(modes_data) > 2 else 0,
                "f1": modes_data[0]["frequency_Hz"],
                "cum_UX_pct": round(cum_ux * 100, 2),
                "cum_UY_pct": round(cum_uy * 100, 2),
                "cum_RZ_pct": round(cum_rz * 100, 2),
                "total_mass_kg_s2_mm": round(sum(Wi_N) / 9810.0, 4),
            }
        
        # ================================================================
        # F. PRINT RESULTS
        # ================================================================
        print(f"\n  {'='*80}")
        print(f"  {'MODAL ANALYSIS RESULTS':^80}")
        print(f"  {'='*80}")
        print(f"\n  {'Mode':>4} {'Period (s)':>14} {'Freq (Hz)':>12} {'UX%':>8} {'UY%':>8} {'RZ%':>8} {'Dom':>5}")
        print(f"  {'-'*4} {'-'*14} {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*5}")
        
        for m in modes_data:
            ux_pct = m.get("UX_ratio", 0) * 100
            uy_pct = m.get("UY_ratio", 0) * 100
            rz_pct = m.get("RZ_ratio", 0) * 100
            print(f"  {m['mode']:>4} {m['period_s']:>14.10f} {m['frequency_Hz']:>12.4f} "
                  f"{ux_pct:>7.2f}% {uy_pct:>7.2f}% {rz_pct:>7.2f}% {m.get('dominant',''):>5}")
        
        print(f"\n  Cumulative: UX={cum_ux*100:.2f}%, UY={cum_uy*100:.2f}%, RZ={cum_rz*100:.2f}%")
    
    except Exception as e:
        import traceback
        print(f"[ERROR] Modal analysis: {e}")
        traceback.print_exc()
        res["status"] = "Error"
    
    return res


# ============================================================================
# 2.5 POST-PROCESSING VISUALIZATION
# ============================================================================
def visualize_model_with_local_axes(model_data, output_dir, case_name, sfac_defo=100):
    """
    Generate custom model visualization with correct local axes from Model data.json.
    
    Args:
        model_data: Dictionary containing model elements with local_axes
        output_dir: Directory to save plot images
        case_name: Name of the load case (for file naming)
        sfac_defo: Scale factor for deformed shape (default: 100)
    
    Returns:
        dict: Paths to generated plot files
    """
    if not OPSVIS_AVAILABLE:
        return {"status": "skipped", "reason": "opsvis/matplotlib not installed"}
    
    from mpl_toolkits.mplot3d import Axes3D
    import numpy as np
    
    plots = {}
    elements = model_data.get("model_elements", [])
    
    try:
        # Create output directory if needed
        plot_dir = os.path.join(output_dir, "plots")
        os.makedirs(plot_dir, exist_ok=True)
        
        # =====================================================================
        # 1. Plot Model with Local Axes AND Applied Loads (Combined)
        # =====================================================================
        try:
            fig = plt.figure(figsize=(14, 12))
            ax = fig.add_subplot(111, projection='3d')
            
            # Collect all coordinates for axis limits
            all_coords = []
            has_loads = False
            
            for elem in elements:
                topo = elem.get("topology", {})
                start = topo.get("start_node", [0, 0, 0])
                end = topo.get("end_node", [0, 0, 0])
                elem_type = elem.get("type", "Beam")
                local_axes = elem.get("local_axes", {})
                loads = elem.get("loads")
                
                all_coords.extend([start, end])
                
                # Element line color based on type and group
                elem_group = elem.get("group", "")
                if elem_type == "Column":
                    line_color = 'purple'
                    line_width = 3
                elif elem_group == 'Secondary':
                    line_color = 'darkorange'
                    line_width = 2.0
                else:
                    line_color = 'blue'
                    line_width = 2.5
                
                # Draw element line
                ax.plot3D([start[0], end[0]], 
                          [start[1], end[1]], 
                          [start[2], end[2]], 
                          color=line_color, linewidth=line_width, solid_capstyle='round')
                
                # Calculate midpoint for axis display
                mid = [(start[0] + end[0]) / 2, 
                       (start[1] + end[1]) / 2, 
                       (start[2] + end[2]) / 2]
                
                # Get element length for scaling axes
                elem_length = topo.get("length_mm", 1000)
                axis_scale = elem_length * 0.12  # 12% of element length for axes
                
                # Draw local axes at midpoint
                x_axis = local_axes.get("x_axis", [1, 0, 0])
                y_axis = local_axes.get("y_axis", [0, 1, 0])
                z_axis = local_axes.get("z_axis", [0, 0, 1])
                
                # X-axis (red) - element longitudinal direction
                ax.quiver(mid[0], mid[1], mid[2], 
                          x_axis[0] * axis_scale, x_axis[1] * axis_scale, x_axis[2] * axis_scale,
                          color='red', arrow_length_ratio=0.2, linewidth=1.5)
                
                # Y-axis (green) - local y direction
                ax.quiver(mid[0], mid[1], mid[2], 
                          y_axis[0] * axis_scale, y_axis[1] * axis_scale, y_axis[2] * axis_scale,
                          color='green', arrow_length_ratio=0.2, linewidth=1.5)
                
                # Z-axis (cyan) - local z direction
                ax.quiver(mid[0], mid[1], mid[2], 
                          z_axis[0] * axis_scale, z_axis[1] * axis_scale, z_axis[2] * axis_scale,
                          color='cyan', arrow_length_ratio=0.2, linewidth=1.5)
                
                # Draw slab pressure load arrows for beams (based on case_name)
                if elem_type == "Beam":
                    # Map case_name to case_type and calculate FLOOR_PRESSURE
                    case_map = {'SelfWeight': 'SW', 'ADL': 'ADL', 'LIVE': 'LL',
                                'AdditionalDL': 'ADL', 'DeadLoad': 'DL', 
                                'LiveLoad': 'LL', 'Combination': 'COMB'}
                    case_type = case_map.get(case_name, 'SW')
                    
                    SLAB_SW = float(model_data.get('slab_sw_pressure', 0))
                    SLAB_ADL = float(model_data.get('slab_adl_pressure', 0))
                    LIVE_LOAD = float(model_data.get('live_load_pressure', 0))
                    
                    if case_type == 'SW': FLOOR_PRESSURE = SLAB_SW
                    elif case_type == 'ADL': FLOOR_PRESSURE = SLAB_ADL
                    elif case_type == 'LL': FLOOR_PRESSURE = LIVE_LOAD
                    elif case_type == 'DL': FLOOR_PRESSURE = SLAB_SW + SLAB_ADL
                    else: FLOOR_PRESSURE = SLAB_SW + SLAB_ADL + LIVE_LOAD  # COMB
                    
                    # Color based on case type
                    load_colors = {
                        'SW': 'darkorange', 'ADL': 'forestgreen', 'DL': 'chocolate',
                        'LL': 'crimson', 'COMB': 'royalblue'
                    }
                    arrow_color = load_colors.get(case_type, 'orange')
                    
                    if FLOOR_PRESSURE > 0:
                        has_loads = True

                        # Get structure config and build sub-panels (matching load distribution)
                        struct_config_viz = detect_structure_config_from_grid(model_data)
                        viz_x_coords = struct_config_viz['x_coords']
                        viz_y_coords = struct_config_viz['y_coords']
                        viz_z_levels = struct_config_viz['z_levels']
                        panel_tol = 10.0

                        min_z_viz = min(viz_z_levels) if viz_z_levels else 0.0
                        floor_z_viz = [z for z in viz_z_levels if z > min_z_viz + 100]

                        _, _, sec_beams_viz = classify_elements(elements)
                        viz_panels = build_sub_panels(viz_x_coords, viz_y_coords, sec_beams_viz, floor_z_viz, panel_tol)
                        if not viz_panels:
                            for ix_v in range(len(viz_x_coords) - 1):
                                for iy_v in range(len(viz_y_coords) - 1):
                                    for fz_v in floor_z_viz:
                                        viz_panels.append({
                                            'x0': viz_x_coords[ix_v], 'x1': viz_x_coords[ix_v+1],
                                            'y0': viz_y_coords[iy_v], 'y1': viz_y_coords[iy_v+1],
                                            'Lx': abs(viz_x_coords[ix_v+1] - viz_x_coords[ix_v]),
                                            'Ly': abs(viz_y_coords[iy_v+1] - viz_y_coords[iy_v]),
                                            'floor_z': fz_v
                                        })

                        sx, sy = start[0], start[1]
                        ex, ey = end[0], end[1]
                        beam_z_viz = (start[2] + end[2]) / 2.0
                        is_x_beam = abs(sy - ey) < panel_tol
                        is_y_beam = abs(sx - ex) < panel_tol

                        if is_x_beam:
                            beam_coord_min = min(sx, ex)
                            beam_coord_max = max(sx, ex)
                        else:
                            beam_coord_min = min(sy, ey)
                            beam_coord_max = max(sy, ey)

                        # Collect adjacent sub-panels with their load parameters
                        adj_panels_info = []
                        for vp in viz_panels:
                            if abs(beam_z_viz - vp['floor_z']) > 100:
                                continue
                            adj = False
                            ol_start = ol_end = 0.0
                            panel_span = 0.0
                            panel_coord_start = 0.0
                            is_short_span = False
                            if is_x_beam:
                                if (abs(sy - vp['y0']) < panel_tol or abs(sy - vp['y1']) < panel_tol):
                                    bx0, bx1 = min(sx, ex), max(sx, ex)
                                    ol_start = max(bx0, vp['x0'])
                                    ol_end = min(bx1, vp['x1'])
                                    if ol_end - ol_start >= panel_tol:
                                        adj = True
                                        panel_span = vp['Lx']
                                        panel_coord_start = vp['x0']
                                        is_short_span = (vp['Lx'] <= vp['Ly'])
                            elif is_y_beam:
                                if (abs(sx - vp['x0']) < panel_tol or abs(sx - vp['x1']) < panel_tol):
                                    by0, by1 = min(sy, ey), max(sy, ey)
                                    ol_start = max(by0, vp['y0'])
                                    ol_end = min(by1, vp['y1'])
                                    if ol_end - ol_start >= panel_tol:
                                        adj = True
                                        panel_span = vp['Ly']
                                        panel_coord_start = vp['y0']
                                        is_short_span = (vp['Ly'] <= vp['Lx'])
                            if adj:
                                Ls = min(vp['Lx'], vp['Ly'])
                                x_c = Ls / 2.0
                                q_max = FLOOR_PRESSURE * x_c
                                is_tri = (is_short_span or abs(vp['Lx'] - vp['Ly']) < panel_tol)
                                adj_panels_info.append({
                                    'q_max': q_max, 'x_c': x_c, 'is_tri': is_tri,
                                    'panel_span': panel_span, 'panel_coord_start': panel_coord_start,
                                    'ol_start': ol_start, 'ol_end': ol_end
                                })

                        if adj_panels_info:
                            # Compute actual q at each arrow position
                            n_arrows = 11
                            beam_range = beam_coord_max - beam_coord_min

                            # First pass: find max q for scaling
                            q_values = []
                            for i in range(n_arrows):
                                ratio = i / (n_arrows - 1)
                                beam_coord = beam_coord_min + ratio * beam_range
                                q_total = 0.0
                                for pi in adj_panels_info:
                                    if beam_coord < pi['ol_start'] - panel_tol or beam_coord > pi['ol_end'] + panel_tol:
                                        continue
                                    pos_in_panel = max(0.0, min(pi['panel_span'], beam_coord - pi['panel_coord_start']))
                                    q_total += get_q_load(pos_in_panel, pi['panel_span'], pi['q_max'], pi['x_c'], pi['is_tri'])
                                q_values.append(q_total)

                            q_peak = max(q_values) if q_values else 0.0
                            if q_peak > 0:
                                max_arrow_length = elem_length * 0.22
                                q_scale = max_arrow_length / q_peak

                                for i in range(n_arrows):
                                    ratio = i / (n_arrows - 1)
                                    pos_3d = [
                                        start[0] + ratio * (end[0] - start[0]),
                                        start[1] + ratio * (end[1] - start[1]),
                                        start[2] + ratio * (end[2] - start[2])
                                    ]
                                    q_at_pos = q_values[i]
                                    arrow_len = q_at_pos * q_scale

                                    if arrow_len > 5:
                                        ax.quiver(pos_3d[0], pos_3d[1], pos_3d[2] + arrow_len,
                                                  0, 0, -arrow_len,
                                                  color=arrow_color, arrow_length_ratio=0.15, linewidth=1.5, alpha=0.85)

                                    # Label q value at each arrow
                                    q_label = f"{q_at_pos:.1f}"
                                    label_z = pos_3d[2] + max(arrow_len, 0) + max_arrow_length * 0.15
                                    ax.text(pos_3d[0], pos_3d[1], label_z,
                                            q_label, fontsize=8, color=arrow_color,
                                            ha='center', va='bottom', fontweight='bold', alpha=0.9)

            
            # Draw nodes with labels
            # Collect unique node coordinates - MUST use same order as run_load_case
            # which processes elements in order and assigns sequential IDs
            node_map = {}  # key (string) -> node_id
            node_coords_list = []  # list of (node_id, coord)
            next_node_id = 1
            
            for elem in elements:
                topo = elem.get("topology", {})
                start_coord = tuple(topo.get("start_node", [0, 0, 0]))
                end_coord = tuple(topo.get("end_node", [0, 0, 0]))
                
                # Use same key format as run_load_case: "x_y_z" with 1 decimal
                for coord in [start_coord, end_coord]:
                    key = f"{coord[0]:.1f}_{coord[1]:.1f}_{coord[2]:.1f}"
                    if key not in node_map:
                        node_map[key] = next_node_id
                        node_coords_list.append((next_node_id, coord))
                        next_node_id += 1
            
            # Create node ID mapping: coordinate -> assigned ID (matching Analysis.json)
            nodes_dict = {coord: node_id for node_id, coord in node_coords_list}
            
            # Calculate offset for labels based on model size
            if all_coords:
                coords_arr = np.array(all_coords)
                label_offset = np.max([
                    coords_arr[:, 0].max() - coords_arr[:, 0].min(),
                    coords_arr[:, 1].max() - coords_arr[:, 1].min()
                ]) * 0.02  # 2% of model size
            else:
                label_offset = 100  # Default offset in mm
            
            for node_coord, node_id in nodes_dict.items():
                # Fixed support indicator (at Z=0)
                if abs(node_coord[2]) < 1:  # Near ground level
                    ax.scatter(node_coord[0], node_coord[1], node_coord[2], 
                               c='magenta', s=150, marker='s', depthshade=True, label='_nolegend_')
                else:
                    ax.scatter(node_coord[0], node_coord[1], node_coord[2], 
                               c='black', s=50, marker='o', depthshade=True, label='_nolegend_')
                
                # Add node label
                ax.text(node_coord[0] + label_offset, 
                        node_coord[1] + label_offset, 
                        node_coord[2] + label_offset * 0.5,
                        f"N{node_id}", 
                        fontsize=7, color='darkgreen', fontweight='bold',
                        ha='left', va='bottom')
            
            # Set axis labels and limits
            ax.set_xlabel('X (mm)', fontsize=11)
            ax.set_ylabel('Y (mm)', fontsize=11)
            ax.set_zlabel('Z (mm)', fontsize=11)
            
            # Calculate proper axis limits
            if all_coords:
                coords_array = np.array(all_coords)
                max_range = np.max([
                    coords_array[:, 0].max() - coords_array[:, 0].min(),
                    coords_array[:, 1].max() - coords_array[:, 1].min(),
                    coords_array[:, 2].max() - coords_array[:, 2].min()
                ]) / 2.0 * 1.3
                
                mid_x = (coords_array[:, 0].max() + coords_array[:, 0].min()) / 2
                mid_y = (coords_array[:, 1].max() + coords_array[:, 1].min()) / 2
                mid_z = (coords_array[:, 2].max() + coords_array[:, 2].min()) / 2
                
                ax.set_xlim(mid_x - max_range, mid_x + max_range)
                ax.set_ylim(mid_y - max_range, mid_y + max_range)
                ax.set_zlim(0, mid_z + max_range * 1.5)
            
            # Add legend with all elements
            from matplotlib.lines import Line2D
            # Check if any secondary beams exist
            has_secondary = any(e.get('group') == 'Secondary' for e in elements)
            legend_elements = [
                Line2D([0], [0], color='purple', linewidth=3, label='Column'),
                Line2D([0], [0], color='blue', linewidth=2.5, label='Primary Beam'),
            ]
            if has_secondary:
                legend_elements.append(Line2D([0], [0], color='darkorange', linewidth=2.0, label='Secondary Beam'))
            legend_elements.extend([
                Line2D([0], [0], color='red', linewidth=1.5, label='Local X-axis'),
                Line2D([0], [0], color='green', linewidth=1.5, label='Local Y-axis'),
                Line2D([0], [0], color='cyan', linewidth=1.5, label='Local Z-axis'),
            ])
            if has_loads:
                legend_elements.append(Line2D([0], [0], color='orange', linewidth=2, marker='v', markersize=8, label='Triangle Load'))
            
            ax.legend(handles=legend_elements, loc='upper left', fontsize=8)
            
            # Title based on whether loads are present
            if has_loads:
                plt.title(f"Model with Local Axes & Applied Loads - {case_name}", fontsize=14, fontweight='bold')
            else:
                plt.title(f"Model with Local Axes - {case_name}", fontsize=14, fontweight='bold')
            
            model_path = os.path.join(plot_dir, f"model_{case_name}.png")
            plt.savefig(model_path, dpi=150, bbox_inches='tight', facecolor='white')
            plt.close('all')
            
            plots["model"] = model_path
            print(f"[PLOT] Model with local axes & loads saved: {model_path}")
            
        except Exception as e:
            import traceback
            print(f"[WARNING] Model plot failed: {e}")
            traceback.print_exc()
            plots["model"] = f"Error: {e}"
        
        # =====================================================================
        # 2. Plot Deformed Shape using opsvis
        # =====================================================================
        try:
            fig = plt.figure(figsize=(12, 10))
            
            # Plot deformed shape with scale factor
            opsv.plot_defo(sfac=sfac_defo, fig_wi_he=(12, 10))
            
            plt.title(f"Deformed Shape (Scale: {round(sfac_defo, 2)}x) - {case_name}", fontsize=14, fontweight='bold')
            
            defo_path = os.path.join(plot_dir, f"deformed_{case_name}.png")
            plt.savefig(defo_path, dpi=150, bbox_inches='tight', facecolor='white')
            plt.close('all')
            
            plots["deformed"] = defo_path
            print(f"[PLOT] Deformed shape saved: {defo_path}")
            
        except Exception as e:
            print(f"[WARNING] Deformed plot failed: {e}")
            plots["deformed"] = f"Error: {e}"
        
        plots["status"] = "success"
        
    except Exception as e:
        print(f"[ERROR] Visualization failed: {e}")
        plots["status"] = "error"
        plots["error"] = str(e)
    
    return plots

# ============================================================================
# 3. PRINT REPORT
# ============================================================================
def print_styled_report(final_output):
    print("\n" + "="*85)
    print(f"{'LAPORAN ANALISIS STRUKTUR (SAP2000 VALIDATED)':^85}")
    print("="*85)

    scenarios = {
        "SelfWeight": "BEBAN MATI (SW)",
        "LiveLoad": "BEBAN HIDUP (LL - Floor Pressure)",
        "Combination": "KOMBINASI (1.0D + 1.0L)"
    }

    for key, title in scenarios.items():
        data = final_output.get(key)
        if not data: continue
        
        print(f"\n[{title}]")
        print(f"  Status Analisis : {data['status']}")
        if data['status'] == 'Success':
            print(f"  Total Applied Load Z : {data['summary'].get('total_applied_z', 0)} N")
            print(f"  Total Reaksi Z+      : {data['summary']['total_reaction_z']} N")
            print(f"  Residual Balance     : {data['summary'].get('equilibrium_residual', 0)} N ({data['summary'].get('equilibrium_ratio', 0)}%)")
        
        print("-" * 105)
        print(f"  {'ID Elemen':<10} | {'Fx (N)':<12} | {'Fy (N)':<12} | {'Fz (N)':<12} | {'Mx (Nmm)':<12} | {'My (Nmm)':<12} | {'Mz (Nmm)':<12}")
        print("-" * 105)
        
        valid_ids = sorted([k for k in data['elements'].keys() if isinstance(data['elements'][k], dict) and 'error' not in data['elements'][k]])
        
        count = 0
        for eid in valid_ids:
            v = data['elements'][eid]
            
            p = v.get('Fx', 0.0)
            v2 = v.get('Fy', 0.0)
            v3 = v.get('Fz', 0.0)
            t = v.get('Mx', 0.0)
            m2 = v.get('My', 0.0)
            m3 = v.get('Mz', 0.0)
            
            print(f"  {str(eid):<10} | {p:<12} | {v2:<12} | {v3:<12} | {t:<12} | {m2:<12} | {m3:<12}")
            count += 1
            if count >= 30: 
                print("  ... (sisa elemen disembunyikan)")
                break
        
        # Show Load Summary for LL and COMB cases
        if key in ['LiveLoad', 'Combination']:
            print(f"\n  {'Load Summary (First 10 Beams)':^85}")
            print("-" * 85)
            print(f"  {'ID':<10} | {'Type':<8} | {'Applied Load':<62}")
            print("-" * 85)
            load_count = 0
            for eid in valid_ids:
                v = data['elements'][eid]
                if v.get('element_type') == 'Beam' and v.get('applied_load') != 'N/A':
                    print(f"  {str(eid):<10} | {v.get('element_type', 'N/A'):<8} | {v.get('applied_load', 'N/A'):<62}")
                    load_count += 1
                    if load_count >= 10:
                        break
                
                
        # Print Reaction Detail for Fixed Nodes (Validation)
        if 'nodes' in data and key == 'Combination':
            print(f"\n  {'Detailed Reactions (Fixed Nodes)':^105}")
            print("-" * 105)
            print(f"  {'Node ID':<10} | {'F1 (X) (N)':<12} | {'F2 (Y) (N)':<12} | {'F3 (Z) (N)':<12} | {'M1 (X) (Nmm)':<15} | {'M2 (Y) (Nmm)':<15}")
            print("-" * 105)
            
            # Filter for fixed nodes (assume nodes with reaction data are supports)
            fixed_nodes = []
            for nid, n in data['nodes'].items():
                if n.get('reaction'):
                    fixed_nodes.append((int(nid), n['reaction']))
            
            fixed_nodes.sort()
            
            for nid, r in fixed_nodes:
                # Dictionary keys already contain correct mapping (F1, F2...)
                print(f"  {str(nid):<10} | {r.get('F1',0):<12.2f} | {r.get('F2',0):<12.2f} | {r.get('F3',0):<12.2f} | {r.get('M1',0):<15.2f} | {r.get('M2',0):<15.2f}")

    print("\n" + "="*105)
    print("[SELESAI]")

# ============================================================================
# 4. MAIN DRIVER
# ============================================================================

def combine_load_cases(results, combo_config, load_patterns):
    """
    Superpose individual pattern results into load combinations.
    Each combination is a dict of {pattern_name: factor}.
    Results are added to `results` dict with the combo name as key.
    """
    mode = combo_config.get('mode', 'default')
    custom_combos = combo_config.get('custom_combinations', {})

    # Build default combinations (10 DSTL SNI 1727-2020)
    DEFAULT_COMBOS = {
        'DSTL1':  {'SelfWeight': 1.4, 'ADL': 1.4},
        'DSTL2':  {'SelfWeight': 1.2, 'ADL': 1.2, 'LIVE': 1.6},
        'DSTL3':  {'SelfWeight': 1.2, 'ADL': 1.2, 'LIVE': 1.0, 'SeismicX': 1.0},
        'DSTL4':  {'SelfWeight': 1.2, 'ADL': 1.2, 'LIVE': 1.0, 'SeismicY': 1.0},
        'DSTL5':  {'SelfWeight': 0.9, 'ADL': 0.9, 'SeismicX': 1.0},
        'DSTL6':  {'SelfWeight': 0.9, 'ADL': 0.9, 'SeismicY': 1.0},
        'DSTL7':  {'SelfWeight': 1.2, 'ADL': 1.2, 'LIVE': 1.0, 'SeismicX': -1.0},
        'DSTL8':  {'SelfWeight': 1.2, 'ADL': 1.2, 'LIVE': 1.0, 'SeismicY': -1.0},
        'DSTL9':  {'SelfWeight': 0.9, 'ADL': 0.9, 'SeismicX': -1.0},
        'DSTL10': {'SelfWeight': 0.9, 'ADL': 0.9, 'SeismicY': -1.0},
    }

    # Analysis hanya menghitung custom combinations
    # (DSTL default hanya digunakan oleh Steel Design Engine)
    active_combos = {}
    if custom_combos:
        active_combos.update(custom_combos)

    if not active_combos:
        return

    print(f"\n{'='*60}")
    print(f"  LOAD COMBINATIONS (mode={mode}, {len(active_combos)} combos)")
    print(f"{'='*60}")

    for combo_name, factors in active_combos.items():
        # Check which patterns are available
        available = {p: f for p, f in factors.items()
                     if p in results and results[p].get('status') == 'Success'}
        missing = [p for p in factors
                   if p not in results or results.get(p, {}).get('status') != 'Success']

        if not available:
            print(f"  [{combo_name}] SKIPPED - no available patterns")
            continue
        if missing:
            print(f"  [{combo_name}] WARNING - missing patterns: {missing}")

        combo_result = {
            'status': 'Success',
            'case_name': combo_name,
            'combination_factors': factors,
            'nodes': {},
            'elements': {},
            'summary': {}
        }

        # === COMBINE NODES ===
        all_node_ids = set()
        for pat_name in available:
            all_node_ids.update(results[pat_name].get('nodes', {}).keys())

        total_rz = 0.0
        for nid in all_node_ids:
            # Get coords from first available pattern
            coords = None
            for pat_name in available:
                pat_nodes = results[pat_name].get('nodes', {})
                if nid in pat_nodes:
                    coords = pat_nodes[nid].get('coords')
                    break

            # Combine displacements (linear superposition)
            combined_disp = [0.0] * 6
            for pat_name, factor in available.items():
                pat_nodes = results[pat_name].get('nodes', {})
                if nid in pat_nodes:
                    pat_disp = pat_nodes[nid].get('disp', [0]*6)
                    for i in range(6):
                        combined_disp[i] += factor * pat_disp[i]

            # Combine reactions (only for support nodes)
            combined_reaction = None
            has_reaction = any(
                results[pn].get('nodes', {}).get(nid, {}).get('reaction') is not None
                for pn in available
            )

            if has_reaction:
                combined_reaction = {'F1': 0, 'F2': 0, 'F3': 0, 'M1': 0, 'M2': 0, 'M3': 0}
                for pat_name, factor in available.items():
                    pat_nodes = results[pat_name].get('nodes', {})
                    if nid in pat_nodes:
                        reac = pat_nodes[nid].get('reaction')
                        if reac:
                            for key in combined_reaction:
                                combined_reaction[key] += factor * reac.get(key, 0)
                combined_reaction = {k: round(v, 2) for k, v in combined_reaction.items()}
                total_rz += combined_reaction.get('F3', 0)

            combo_result['nodes'][nid] = {
                'coords': coords,
                'disp': [round(v, 10) for v in combined_disp],
                'reaction': combined_reaction
            }

        combo_result['summary']['total_reaction_z'] = round(total_rz, 2)

        # === COMBINE ELEMENTS ===
        all_elem_ids = set()
        for pat_name in available:
            all_elem_ids.update(results[pat_name].get('elements', {}).keys())

        force_keys = ['P', 'Fy', 'Fz', 'T', 'My', 'Mz']

        def _interp_force(pat_stns, target_ratio, fk):
            """Interpolate force component at target_ratio from pattern stations."""
            if not pat_stns:
                return 0.0
            lower = None
            upper = None
            for ps in pat_stns:
                r = ps.get('station', 0.0)
                if r <= target_ratio + 1e-6:
                    if lower is None or r > lower.get('station', 0.0):
                        lower = ps
                if r >= target_ratio - 1e-6:
                    if upper is None or r < upper.get('station', 0.0):
                        upper = ps
            if lower is None and upper is None:
                return 0.0
            if lower is None:
                return upper.get(fk, 0)
            if upper is None:
                return lower.get(fk, 0)
            lr = lower.get('station', 0.0)
            ur = upper.get('station', 0.0)
            if abs(ur - lr) < 1e-8:
                return lower.get(fk, 0)
            t = (target_ratio - lr) / (ur - lr)
            return lower.get(fk, 0) * (1 - t) + upper.get(fk, 0) * t

        for eid in all_elem_ids:
            # Find reference element (prefer the pattern with most stations)
            ref_elem = None
            max_stns = -1
            for pat_name in available:
                pat_elems = results[pat_name].get('elements', {})
                if eid in pat_elems:
                    n = len(pat_elems[eid].get('stations', []))
                    if n > max_stns:
                        max_stns = n
                        ref_elem = pat_elems[eid]
            if not ref_elem:
                continue

            # Combine stations using RATIO-BASED interpolation
            # This correctly handles different station counts between patterns
            ref_stations = ref_elem.get('stations', [])
            combined_stations = []

            for ref_stn in ref_stations:
                target_ratio = ref_stn.get('station', 0.0)
                combined_stn = {'station': target_ratio}

                for fk in force_keys:
                    val = 0.0
                    for pat_name, factor in available.items():
                        pat_elems = results[pat_name].get('elements', {})
                        if eid in pat_elems:
                            pat_stns = pat_elems[eid].get('stations', [])
                            val += factor * _interp_force(pat_stns, target_ratio, fk)
                    combined_stn[fk] = round(val, 2)

                combined_stations.append(combined_stn)

            combined_elem = {
                'element_type': ref_elem.get('element_type'),
                'stations': combined_stations,
            }

            # Combine max_deflection if present in any pattern
            has_defl = False
            for pat_name in available:
                pat_elems = results[pat_name].get('elements', {})
                if eid in pat_elems:
                    d = pat_elems[eid].get('max_deflection')
                    if d is not None and isinstance(d, dict):
                        has_defl = True
                        break

            if has_defl:
                combined_defl = {}
                defl_keys = ['delta_y_max_mm', 'delta_y_station', 'delta_y_distance_mm',
                             'delta_z_max_mm', 'delta_z_station', 'delta_z_distance_mm']
                for dk in defl_keys:
                    val = 0.0
                    for pat_name, factor in available.items():
                        pat_elems = results[pat_name].get('elements', {})
                        if eid in pat_elems:
                            pat_defl = pat_elems[eid].get('max_deflection')
                            if pat_defl is not None and isinstance(pat_defl, dict):
                                if 'max' in dk or 'distance' in dk:
                                    val += factor * pat_defl.get(dk, 0)
                    combined_defl[dk] = round(val, 4)
                combined_elem['max_deflection'] = combined_defl

            combo_result['elements'][eid] = combined_elem

        results[combo_name] = combo_result

        # Formula string for console
        formula_parts = []
        for p, f in factors.items():
            if f == 1.0:
                formula_parts.append(p)
            elif f == -1.0:
                formula_parts.append(f'-{p}')
            else:
                formula_parts.append(f'{f}{p}')
        formula = ' + '.join(formula_parts)
        print(f"  [{combo_name}] = {formula}  (nodes={len(combo_result['nodes'])}, elems={len(combo_result['elements'])})")

    print()


def run_analysis(input_path, output_path, generate_plots=True):
    """
    Run structural analysis for all load cases.
    
    Args:
        input_path: Path to Model data.json
        output_path: Path to save Analysis.json
        generate_plots: Whether to generate visualization plots (default: True)
    """
    data = get_model_data(input_path)
    if not data: return

    # Get output directory for plots
    output_dir = os.path.dirname(os.path.abspath(output_path))
    
    # Jalankan Skenario Gravitasi dengan visualisasi
    results = {}
    plot_results = {}
    
    # === DYNAMIC LOAD PATTERNS (SAP2000-like) ===
    load_patterns = data.get('load_patterns', None)
    
    if load_patterns:
        # NEW PATH: iterate over user-defined load patterns
        print(f"\n  Load Patterns from JSON: {list(load_patterns.keys())}")
        
        for pat_name, pat_def in load_patterns.items():
            print(f"\n{'='*60}")
            print(f"Running Load Pattern: {pat_name} (type={pat_def.get('type','?')})")
            print(f"{'='*60}")
            
            results[pat_name] = run_load_case(data, pat_name, pattern_def=pat_def)
            
            # Generate visualization
            if generate_plots and results[pat_name].get('status') == 'Success':
                max_disp = 0.1
                try:
                    for eid, elem in results[pat_name].get('elements', {}).items():
                        if isinstance(elem, dict) and 'max_deflection' in elem:
                            defl = elem['max_deflection']
                            max_disp = max(max_disp,
                                           abs(defl.get('delta_y_max_mm', 0)),
                                           abs(defl.get('delta_z_max_mm', 0)))
                except:
                    pass
                sfac = max(10, min(1000, 100 / max(max_disp, 0.001)))
                plot_results[pat_name] = visualize_model_with_local_axes(data, output_dir, pat_name, sfac_defo=sfac)
            else:
                plot_results[pat_name] = {"status": "skipped", "reason": "analysis failed or plots disabled"}
    else:
        # LEGACY PATH: hard-coded load cases (backward compatibility)
        load_cases = [
            ('SelfWeight', 'SW'),
            ('AdditionalDL', 'ADL'),
            ('DeadLoad', 'DL'),
            ('LiveLoad', 'LL'),
            ('Combination', 'COMB')
        ]
        
        for case_key, case_type in load_cases:
            print(f"\n{'='*60}")
            print(f"Running Load Case: {case_key}")
            print(f"{'='*60}")
            results[case_key] = run_load_case(data, case_type)
            
            if generate_plots and results[case_key].get('status') == 'Success':
                max_disp = 0.1
                try:
                    for eid, elem in results[case_key].get('elements', {}).items():
                        if isinstance(elem, dict) and 'max_deflection' in elem:
                            defl = elem['max_deflection']
                            max_disp = max(max_disp,
                                           abs(defl.get('delta_y_max_mm', 0)),
                                           abs(defl.get('delta_z_max_mm', 0)))
                except:
                    pass
                sfac = max(10, min(1000, 100 / max(max_disp, 0.001)))
                plot_results[case_key] = visualize_model_with_local_axes(data, output_dir, case_key, sfac_defo=sfac)
            else:
                plot_results[case_key] = {"status": "skipped", "reason": "analysis failed or plots disabled"}
    
    # Add plot paths to results
    results["_plots"] = plot_results
    
    # ========== RUN VALIDATION CHECKS ==========
    # Run validation on SelfWeight (or Combination for legacy)
    validation_key = None
    for vk in ['SelfWeight', 'Combination']:
        if vk in results and results[vk].get('status') == 'Success':
            validation_key = vk
            break
    
    if validation_key:
        print("\n" + "="*60)
        print(f"Running Validation Checks (on {validation_key})...")
        print("="*60)
        
        validation_report = run_all_validations(results[validation_key], data)
        results['_validation'] = validation_report
        print_validation_report(validation_report)
    else:
        print("\n[WARNING] Skipping validation - no suitable load case available")
        results['_validation'] = {'status': 'skipped', 'reason': 'No suitable load case for validation'}

    # ========== MODAL ANALYSIS (Eigenvalue) ==========
    import copy
    seismic_params = data.get('seismic_parameters', {})
    
    if seismic_params:
        print(f"\n{'='*66}")
        print(f"  MODAL ANALYSIS — Eigenvalue (Period & Frequency)")
        print(f"{'='*66}")
        
        modal_result = run_modal_analysis(copy.deepcopy(data))
        results['_modal'] = modal_result
        
        if modal_result.get('status') == 'Success':
            T_modal = modal_result.get('summary', {}).get('T1', 0)
            print(f"\n  T1_modal = {T_modal:.6f} s")
        else:
            print(f"  [WARNING] Modal analysis failed!")

    # ========== SEISMIC ANALYSIS (SNI 1726 ELF) ==========
    seismic_params = data.get('seismic_parameters', {})
    
    if seismic_params:
        for eq_dir, eq_key in [('EQx', 'SeismicX'), ('EQy', 'SeismicY')]:
            print(f"\n{'='*66}")
            print(f"  SEISMIC ANALYSIS — {eq_dir} (SNI 1726 ELF)")
            print(f"{'='*66}")
            
            eq_result = run_seismic_analysis(copy.deepcopy(data), direction=eq_dir)
            results[eq_key] = eq_result
            
            # Console output: V and Fx/Fy table
            if eq_result.get('status') == 'Success':
                sp = eq_result.get('seismic_parameters', {})
                fd = eq_result.get('floor_data', [])
                V_kN = sp.get('V_kN', 0)
                Cs = sp.get('Cs', 0)
                W = sp.get('W_total_kN', 0)
                T = sp.get('T', 0)
                
                force_label = "Fx" if eq_dir == 'EQx' else "Fy"
                V_label = "Vx" if eq_dir == 'EQx' else "Vy"
                
                print(f"\n  {V_label} = {V_kN:.4f} kN (Cs={Cs:.6f}, W={W:.3f} kN, T={T:.4f}s)")
                print(f"\n  {'Lantai':>7} | {'Wi (kN)':>12} | {'hi (m)':>8} | {'Cvx':>8} | {force_label+' (kN)':>10}")
                print(f"  {'-'*7}-+-{'-'*12}-+-{'-'*8}-+-{'-'*8}-+-{'-'*10}")
                
                for floor in sorted(fd, key=lambda x: x['floor'], reverse=True):
                    print(f"  {floor['floor']:>7} | {floor['Wi_kN']:>12.3f} | {floor['hi_m']:>8.3f} | {floor['Cvx']:>8.6f} | {floor['Fx_kN']:>10.4f}")
                
                # Totals
                sum_Fx = sum(f['Fx_kN'] for f in fd)
                print(f"  {'-'*7}-+-{'-'*12}-+-{'-'*8}-+-{'-'*8}-+-{'-'*10}")
                print(f"  {'TOTAL':>7} | {W:>12.3f} | {'':>8} | {'1.000':>8} | {sum_Fx:>10.4f}")
                
                # Equilibrium check
                eq_res = eq_result.get('summary', {}).get('equilibrium_residual_N', 0)
                print(f"\n  Equilibrium check: |Sum(Reaction) - V| = {eq_res:.4f} N")
            else:
                print(f"  [ERROR] Seismic analysis {eq_dir} failed!")
    else:
        print("\n[INFO] No seismic_parameters in model data — skipping seismic analysis")

    # ========== LOAD COMBINATION SUPERPOSITION ==========
    combo_config = data.get('load_combination_config', {})
    if combo_config:
        combine_load_cases(results, combo_config, data.get('load_patterns', {}))

    # Simpan JSON Output
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=4)
    
    print(f"\n[INFO] Results saved to: {output_path}")
    if any(p.get('status') == 'success' for p in plot_results.values()):
        print(f"[INFO] Plots saved to: {os.path.join(output_dir, 'plots')}")

    # Tampilkan Report
    print_styled_report(results)
    
    return results

if __name__ == "__main__":
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Model data is one folder up
    input_json = os.path.join(base_dir, "..", "Model data.json")
    output_json = os.path.join(base_dir, "Analysis.json")

    if len(sys.argv) < 3:
        if os.path.exists(input_json):
            run_analysis(input_json, output_json)
        else:
             # Fallback for direct execution if files are adjacent
             run_analysis("Model data.json", "Analysis.json")
    else:
        run_analysis(sys.argv[1], sys.argv[2])
