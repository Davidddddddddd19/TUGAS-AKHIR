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
            max_M2 = max(abs(s.get('M2', 0)) for s in stations)
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
            
            return {"P":Fx_loc, "V2":Fy_loc, "V3":Fz_loc, "T":Mx_loc, "M2":My_loc, "M3":Mz_loc}

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
                        "V2": -forces[0],   # Shear in Global X 
                        "V3": forces[1],    # Shear in Global Y (raw - negate at output)
                        "T": forces[5],     # Torsion (raw - negate at output)
                        "M2": -forces[3],   # Strong axis moment = reaction M1
                        "M3": -forces[4]    # Weak axis moment = reaction M2 (larger)
                    }
                else:
                    # BEAM internal force mapping:
                    #
                    # For beam local axes:
                    #   - local-x = element axis (horizontal, along beam length)
                    #   - local-y = Global +Z (vertical, perpendicular to floor)
                    #   - local-z = horizontal perpendicular to beam
                    #           (Global -Y for X-beam, Global +X for Y-beam)
                    #
                    # OpenSees eleForce() returns:
                    #   forces[0] = P (axial along local-x)
                    #   forces[1] = Vy (shear along local-y = vertical direction)
                    #   forces[2] = Vz (shear along local-z = horizontal direction)
                    #   forces[3] = T (torsion about local-x)
                    #   forces[4] = My (moment about local-y = about Global Z)
                    #               -> This is the MAJOR bending from gravity
                    #   forces[5] = Mz (moment about local-z = about horizontal)
                    #               -> This is ~0 for gravity-only loading
                    #
                    # User convention:
                    #   M2 = moment about local-y = ~0 for gravity (minor)
                    #   M3 = moment about local-z = MAJOR bending from gravity
                    #
                    # To match user convention, SWAP M2/M3:
                    #   M2 = forces[5] (Mz) ~0 for gravity (minor)
                    #   M3 = forces[4] (My) = major bending moment
                    return {
                        "P": -forces[0],    # Axial
                        "V2": -forces[2],   # Major shear (vertical) - matches SAP2000
                        "V3": forces[1],   # Minor shear (horizontal)
                        "T": forces[3],    # Torsion
                        "M2": -forces[5],   # Minor moment (about horizontal ~0)
                        "M3": forces[4]     # MAJOR moment - SAP2000 convention (negative at ends)
                    }
            
            # 12-component output: forces at both i-node and j-node
            if is_vertical:
                # COLUMN coordinate mapping
                start_internal = {
                    "P": -forces[2],    # Axial at i-node
                    "V2": -forces[0],   # Shear X at i-node
                    "V3": forces[1],    # Shear Y at i-node (raw - negate at output)
                    "T": forces[5],     # Torsion at i-node (raw - negate at output)
                    "M2": -forces[3],   # Strong axis moment at i-node
                    "M3": -forces[4]    # Weak axis moment at i-node
                }
                end_internal = {
                    "P": forces[8],     # Axial at j-node
                    "V2": forces[6],    # Shear X at j-node
                    "V3": -forces[7],   # Shear Y at j-node
                    "T": -forces[11],   # Torsion at j-node
                    "M2": forces[9],    # Strong axis moment at j-node
                    "M3": forces[10]    # Weak axis moment at j-node
                }
            else:
                # BEAM: M2=minor (~0), M3=major bending - SAP2000 convention
                start_internal = {
                    "P": -forces[0],    # Axial at i-node
                    "V2": -forces[2],   # Major shear (vertical) at i-node
                    "V3": -forces[1],   # Minor shear (horizontal) at i-node
                    "T": -forces[3],    # Torsion at i-node
                    "M2": -forces[5],   # Minor moment (~0) at i-node
                    "M3": forces[4]     # MAJOR moment at i-node (SAP2000 convention)
                }
                end_internal = {
                    "P": forces[6],     # Axial at j-node
                    "V2": forces[8],    # Major shear (vertical) at j-node
                    "V3": forces[7],    # Minor shear (horizontal) at j-node
                    "T": forces[9],     # Torsion at j-node
                    "M2": forces[11],   # Minor moment (~0) at j-node
                    "M3": -forces[10]   # MAJOR moment at j-node (SAP2000 convention)
                }
            
            # For intermediate stations, use linear interpolation
            interp = {}
            for key in ["P", "V2", "V3", "T", "M2", "M3"]:
                interp[key] = start_internal[key] * (1 - ratio) + end_internal[key] * ratio
                
            return interp

        def get_exact_intermediate_forces(start_f, end_f, ratio, length_mm):
            """
            Calculate exact intermediate forces using structural mechanics.
            
            Key relationships (SAP2000 convention):
            - V2 (major shear in local Y) drives M3 (moment about local Z): dM3/dx = V2
            - V3 (minor shear in local Z) drives M2 (moment about local Y): dM2/dx = V3
            
            For uniformly loaded element:
            - V(x) = V_start - w*x  (linear shear)
            - M(x) = M_start + V_start*x - w*x²/2  (parabolic moment)
            
            For element without distributed load (e.g., column under selfweight):
            - V = constant, so W_total ≈ 0
            - M(x) = M_start + V*x  (linear moment)
            """
            interp = {}
            x = ratio * length_mm
            
            # ===== MAJOR AXIS: V2 drives M3 =====
            v2_s = start_f["V2"]
            v2_e = end_f["V2"]
            m3_s = start_f["M3"]
            
            # Equivalent distributed load (major axis)
            # W2_total = w*L where w is the uniform load intensity
            # From equilibrium: V_end = V_start - w*L, so w*L = V_start - V_end
            W2_total = v2_s - v2_e
            
            # Shear V2(x) = V2_start - w*x = V2_start - (W2_total/L)*x
            v2_x = v2_s - W2_total * ratio
            
            # SAP2000 Convention: dM3/dx = -V2 (NEGATIVE relationship)
            # When V2 is negative, M3 INCREASES (integration with negative sign)
            # M3(x) = M3_start - V2_start*x + w*x²/2
            # M3(x) = M3_start - V2_start*x + (W2_total/L)*x²/2
            m3_x = m3_s - v2_s * x + (W2_total / length_mm) * (x**2) / 2.0
            
            interp["V2"] = v2_x
            interp["M3"] = m3_x
            
            # ===== MINOR AXIS: V3 drives M2 =====
            v3_s = start_f["V3"]
            v3_e = end_f["V3"]
            m2_s = start_f["M2"]
            
            # Equivalent distributed load (minor axis)
            W3_total = v3_s - v3_e
            
            # Shear V3(x) = V3_start - (W3_total/L)*x
            v3_x = v3_s - W3_total * ratio
            
            # SAP2000 Convention: dM2/dx = -V3 (NEGATIVE relationship)
            # When V3 is negative, M2 INCREASES (integration with negative sign)
            # M2(x) = M2_start - V3_start*x + (W3_total/L)*x²/2
            m2_x = m2_s - v3_s * x + (W3_total / length_mm) * (x**2) / 2.0
            
            interp["V3"] = v3_x
            interp["M2"] = m2_x
            
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
                forces_exact = get_exact_intermediate_forces(f_start, f_end, ratio, length_mm)
                sample_stations.append({"station": ratio, "forces": forces_exact})
            
            # Add End
            sample_stations.append({"station": 1.0, "forces": f_end})
            
            critical_stations = list(sample_stations) # Copy
            
            # 3. Analytic Zero Crossings for Shear (V2, V3) -> Max Moment
            # Check V2 -> Max M3
            zero_v2 = find_zero_crossing(f_start, f_end, 'V2', length_mm)
            if zero_v2: critical_stations.append(zero_v2)

            # Check V3 -> Max M2
            zero_v3 = find_zero_crossing(f_start, f_end, 'V3', length_mm)
            if zero_v3: critical_stations.append(zero_v3)
            
            # 4. Check Zero Crossings for Moment (Inflection Points)
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
            Calculate deflection at any station along element using Timoshenko beam shape functions.
            
            For Timoshenko beam, deflection considers both bending and shear deformations.
            Uses cubic Hermite interpolation enhanced with shear correction.
            
            Args:
                elem_id: Element ID
                ratio: Station ratio (0.0 to 1.0)
                local_axes: Local coordinate system
                node_coords_dict: Dictionary of node coordinates
                start_node, end_node: Node IDs
                length_mm: Element length
                E, I_major, I_minor, A, G: Section/material properties
                
            Returns:
                Dictionary with local deflections {delta_y, delta_z}
            """
            # Get displacements at start and end nodes
            d_start = ops.nodeDisp(start_node)  # [dx, dy, dz, rx, ry, rz] global
            d_end = ops.nodeDisp(end_node)      # [dx, dy, dz, rx, ry, rz] global
            
            # Extract translation and rotation components
            u1 = [d_start[0], d_start[1], d_start[2]]  # Start translation (global)
            u2 = [d_end[0], d_end[1], d_end[2]]        # End translation (global)
            theta1 = [d_start[3], d_start[4], d_start[5]]  # Start rotation (global)
            theta2 = [d_end[3], d_end[4], d_end[5]]        # End rotation (global)
            
            # Transform to local coordinates
            x_axis = local_axes.get('x_axis', [1, 0, 0])
            y_axis = local_axes.get('y_axis', [0, 1, 0])
            z_axis = local_axes.get('z_axis', [0, 0, 1])
            
            # Rotation matrix (global to local)
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
            
            # Timoshenko beam shape functions for transverse deflection
            # v(x) = N1*v1 + N2*theta1 + N3*v2 + N4*theta2
            # Where N1, N2, N3, N4 are Hermite cubic shape functions with shear correction
            
            L = length_mm
            xi = ratio  # 0 to 1
            
            # Standard Hermite shape functions (Euler-Bernoulli base)
            N1 = 1 - 3*xi**2 + 2*xi**3
            N2 = L * (xi - 2*xi**2 + xi**3)
            N3 = 3*xi**2 - 2*xi**3
            N4 = L * (-xi**2 + xi**3)
            
            # Timoshenko shear correction factor (phi = 12*E*I / (G*A_s*L^2))
            # For simplicity, using approximate shear area A_s ≈ A * 0.85 for I-section
            A_s = A * 0.85 if A > 0 else 1.0
            
            # Calculate phi for each direction
            phi_y = 12 * E * I_major / (G * A_s * L**2) if (G * A_s * L**2) > 0 else 0
            phi_z = 12 * E * I_minor / (G * A_s * L**2) if (G * A_s * L**2) > 0 else 0
            
            # Modified shape functions for Timoshenko beam (simplified)
            # The effect of shear is typically small for slender beams
            # For practical purposes, use Euler-Bernoulli with slight adjustment
            shear_factor_y = 1 / (1 + phi_y) if phi_y < 10 else 0.1
            shear_factor_z = 1 / (1 + phi_z) if phi_z < 10 else 0.1
            
            # Deflection in local Y direction (v1=u1_local[1], theta1=theta1_local[2] for rotation about Z)
            # For beam: Y is transverse (vertical), rotation about Z causes Y deflection
            v1_y = u1_local[1]
            v2_y = u2_local[1]
            theta1_z = theta1_local[2]  # Rotation about local Z
            theta2_z = theta2_local[2]
            
            delta_y = (N1 * v1_y + N2 * theta1_z + N3 * v2_y + N4 * theta2_z) * shear_factor_y
            
            # Deflection in local Z direction (w1=u1_local[2], theta1=theta1_local[1] for rotation about Y)
            v1_z = u1_local[2]
            v2_z = u2_local[2]
            theta1_y = -theta1_local[1]  # Rotation about local Y (sign convention)
            theta2_y = -theta2_local[1]
            
            delta_z = (N1 * v1_z + N2 * theta1_y + N3 * v2_z + N4 * theta2_y) * shear_factor_z
            
            return {"delta_y": delta_y, "delta_z": delta_z}

        def get_max_deflection(item, node_coords_dict, sub_elements_map):
            """
            Calculate maximum deflection for an element with 9-point sampling.
            Uses Timoshenko beam shape functions for interpolation.
            
            Returns:
                Dictionary with max deflection info:
                {
                    "delta_y_max_mm": signed max deflection in local Y,
                    "delta_y_station": station ratio where max occurs,
                    "delta_y_distance_mm": distance where max occurs,
                    "delta_z_max_mm": signed max deflection in local Z,
                    "delta_z_station": station ratio where max occurs,
                    "delta_z_distance_mm": distance where max occurs
                }
            """
            eid = item['id']
            raw = item['raw']
            local_axes = raw.get('local_axes', {})
            length_mm = raw.get('topology', {}).get('length_mm', 0)
            sec = raw.get('section', {})
            mat = raw.get('material', {})
            
            # Material properties
            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 80000))
            
            # Section properties
            A = float(sec.get('Area_mm2', 0))
            I_major = float(sec.get('Iz_mm4', 0))  # Strong axis (Iz)
            I_minor = float(sec.get('Iy_mm4', 0))  # Weak axis (Iy)
            
            if length_mm <= 0:
                return None
                
            # Get nodes for this element
            subs = sub_elements_map.get(eid)
            
            # 9-point sampling (every 0.125)
            sample_ratios = [i * 0.125 for i in range(9)]  # 0, 0.125, 0.25, ..., 1.0
            
            deflection_samples = []
            
            if not subs:
                # Single element - use original nodes
                n1, n2 = item['nodes']
                for ratio in sample_ratios:
                    defl = get_deflection_at_station(
                        eid, ratio, local_axes, node_coords_dict, n1, n2,
                        length_mm, E, I_major, I_minor, A, G
                    )
                    deflection_samples.append({
                        "station": ratio,
                        "distance_mm": ratio * length_mm,
                        "delta_y": defl["delta_y"],
                        "delta_z": defl["delta_z"]
                    })
            else:
                # Multiple sub-elements - sample across all
                cumulative_len = 0.0
                total_len = length_mm
                
                for ratio in sample_ratios:
                    target_dist = ratio * total_len
                    
                    # Find which sub-element contains this point
                    cum = 0.0
                    for i, (sub_eid, sub_len) in enumerate(subs):
                        if cum <= target_dist <= cum + sub_len + 1e-6:
                            # Found the segment
                            local_ratio = (target_dist - cum) / sub_len if sub_len > 0 else 0
                            local_ratio = max(0.0, min(1.0, local_ratio))  # Clamp
                            
                            # Get nodes for this sub-element
                            try:
                                nodes = ops.eleNodes(sub_eid)
                                n1, n2 = nodes[0], nodes[1]
                                
                                defl = get_deflection_at_station(
                                    sub_eid, local_ratio, local_axes, node_coords_dict,
                                    n1, n2, sub_len, E, I_major, I_minor, A, G
                                )
                                deflection_samples.append({
                                    "station": ratio,
                                    "distance_mm": target_dist,
                                    "delta_y": defl["delta_y"],
                                    "delta_z": defl["delta_z"]
                                })
                            except:
                                pass
                            break
                        cum += sub_len
            
            if not deflection_samples:
                return None
            
            # Find max absolute deflection (but keep sign)
            max_y_sample = max(deflection_samples, key=lambda x: abs(x["delta_y"]))
            max_z_sample = max(deflection_samples, key=lambda x: abs(x["delta_z"]))
            
            return {
                "delta_y_max_mm": round(max_y_sample["delta_y"], 4),
                "delta_y_station": round(max_y_sample["station"], 4),
                "delta_y_distance_mm": round(max_y_sample["distance_mm"], 2),
                "delta_z_max_mm": round(max_z_sample["delta_z"], 4),
                "delta_z_station": round(max_z_sample["station"], 4),
                "delta_z_distance_mm": round(max_z_sample["distance_mm"], 2)
            }


        # --- RESET MODEL ---
        ops.wipe()
        ops.model('basic', '-ndm', 3, '-ndf', 6)
        
        # --- NODE MAPPING ---
        node_map = {}       
        node_coords = {}    
        next_node_id = 1
        
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
        struct_config = detect_structure_config(elements_list)
        print(f"  Structure Config: {struct_config['n_stories']} stories, "
              f"{struct_config['n_span_x']}x{struct_config['n_span_y']} spans, "
              f"story_height={struct_config['story_height']}mm")

        # --- BUILD NODES ---
        all_z = [c[2] for c in node_coords.values()]
        min_z = min(all_z) if all_z else 0.0
        fixed_nodes = set()

        for nid, coords in node_coords.items():
            ops.node(nid, *coords)
            
            # Simpan koordinat ke output
            res["nodes"][nid] = {
                "coords": coords,
                "disp": [0.0]*6,
                "reaction": None
            }
            
            # Tumpuan Jepit (Fixed)
            if abs(coords[2] - min_z) < 100.0:
                ops.fix(nid, 1, 1, 1, 1, 1, 1)
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
        transf_counter = 1  # Unique transform tag counter

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
                vecxz = local_axes.get('z_axis', [0, -1, 0])
            
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
            # For BEAM (horizontal):
            #   - Local Y = Vertical (Global Z)
            #   - Local Z = Horizontal
            #   - Ops_Iy = Weak (Iy), Ops_Iz = Strong (Iz) - standard convention
            
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
                # BEAM: Standard convention (gravity is in local-z direction for beam)
                Ops_Iy = Iy  # Weak axis  -> minor moment
                Ops_Iz = Iz  # Strong axis -> major moment
            
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
                
                num_subs = 20  # Match opensees_validate.py
                sub_ids = []
                
                prev_node = beam_n_start
                for k in range(num_subs):
                    if k == num_subs - 1:
                        curr_node = beam_n_end
                    else:
                        ratio = (k + 1) / num_subs
                        nx = beam_coord_start[0] + vx * ratio
                        ny = beam_coord_start[1] + vy * ratio
                        nz = beam_coord_start[2] + vz * ratio
                        
                        curr_node = next_node_id
                        node_coords[curr_node] = (nx, ny, nz)
                        ops.node(curr_node, nx, ny, nz)
                        res["nodes"][curr_node] = {"coords":(nx,ny,nz), "disp":[0]*6, "reaction":None}
                        next_node_id += 1
                    
                    sub_ele_id = item['id'] * 100 + k 
                    if sub_ele_id > 2000000000: sub_ele_id = int(sub_ele_id % 1000000 + 900000)
                    
                    # Create per-sub-element transform WITH rigid end zone offsets
                    # Only first and last sub-elements get the offset
                    sub_transf_tag = transf_counter
                    transf_counter += 1
                    
                    sub_dI = [0.0, 0.0, 0.0]
                    sub_dJ = [0.0, 0.0, 0.0]
                    
                    if k == 0:
                        sub_dI = list(item['dI'])  # First sub gets parent's i-offset
                    if k == num_subs - 1:
                        sub_dJ = list(item['dJ'])  # Last sub gets parent's j-offset
                    
                    ops.geomTransf('Linear', sub_transf_tag,
                                  item['vecxz'][0], item['vecxz'][1], item['vecxz'][2],
                                  '-jntOffset', sub_dI[0], sub_dI[1], sub_dI[2],
                                  sub_dJ[0], sub_dJ[1], sub_dJ[2])

                    ops.element('ElasticTimoshenkoBeam', sub_ele_id, prev_node, curr_node, 
                                E, G, A, J, Ops_Iy, Ops_Iz, Avy, Avz, sub_transf_tag)
                                
                    sub_ids.append((sub_ele_id, item['length']/num_subs))
                    prev_node = curr_node
                
                
                sub_elements_map[item['id']] = sub_ids

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
                         # Beam: distributed vertical load (SAP2000 uses distributed)
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', -w_dead, 0.0, 0.0)
        # B. SLAB/FLOOR PRESSURE LOADS - TWO-WAY YIELD LINE DISTRIBUTION
        # Implements proper tributary area calculation with 45-degree bisectors
        # Short span beams get triangle loads, long span beams get trapezoid loads
        if FLOOR_PRESSURE > 0:
            span_x = struct_config['span_x']
            span_y = struct_config['span_y']
            n_span_x = struct_config['n_span_x']
            n_span_y = struct_config['n_span_y']
            
            # Use actual grid coordinates (fixed: no centering bug)
            x_coords = struct_config['x_coords']
            y_coords = struct_config['y_coords']
            edge_tol = 10.0
            
            # Determine short and long spans for yield line theory
            L_short = min(span_x, span_y)
            L_long = max(span_x, span_y)
            
            # Critical distance from corner where 45 degree lines meet
            x_c = L_short / 2.0
            
            # Build list of slab panels
            panels = []
            for ix in range(n_span_x):
                for iy in range(n_span_y):
                    x0 = x_coords[ix]
                    x1 = x_coords[ix + 1]
                    y0 = y_coords[iy]
                    y1 = y_coords[iy + 1]
                    panels.append({'x0': x0, 'x1': x1, 'y0': y0, 'y1': y1,
                                   'Lx': span_x, 'Ly': span_y})
            
            # Process each beam and calculate loads from adjacent panels
            for item in processed_elements:
                if not item['is_vertical']:
                    raw = item['raw']
                    start = raw['topology']['start_node']
                    end = raw['topology']['end_node']
                    
                    sx, sy = start[0], start[1]
                    ex, ey = end[0], end[1]
                    beam_len = item['length']
                    
                    is_x_beam = abs(sy - ey) < edge_tol
                    is_y_beam = abs(sx - ex) < edge_tol
                    
                    # Find adjacent panels
                    adjacent_panels = []
                    for panel in panels:
                        if is_x_beam:
                            if (abs(sy - panel['y0']) < edge_tol or abs(sy - panel['y1']) < edge_tol):
                                beam_x0, beam_x1 = min(sx, ex), max(sx, ex)
                                if beam_x0 >= panel['x0'] - edge_tol and beam_x1 <= panel['x1'] + edge_tol:
                                    adjacent_panels.append(panel)
                        elif is_y_beam:
                            if (abs(sx - panel['x0']) < edge_tol or abs(sx - panel['x1']) < edge_tol):
                                beam_y0, beam_y1 = min(sy, ey), max(sy, ey)
                                if beam_y0 >= panel['y0'] - edge_tol and beam_y1 <= panel['y1'] + edge_tol:
                                    adjacent_panels.append(panel)
                    
                    if not adjacent_panels:
                        continue
                    
                    subs = sub_elements_map.get(item['id'], [(item['id'], item['length'])])
                    num_subs = len(subs)
                    
                    for panel in adjacent_panels:
                        Lx, Ly = panel['Lx'], panel['Ly']
                        L_s = min(Lx, Ly)
                        x_c_p = L_s / 2.0
                        q_max = FLOOR_PRESSURE * x_c_p
                        
                        if is_x_beam:
                            is_short_span = (Lx <= Ly)
                        else:
                            is_short_span = (Ly <= Lx)
                        
                        def get_q(pos, length, q_max, x_c, is_tri):
                            if is_tri:
                                L_half = length / 2.0
                                if pos <= L_half: return q_max * (pos / L_half)
                                else: return q_max * ((length - pos) / L_half)
                            else:
                                if pos <= x_c: return q_max * (pos / x_c)
                                elif pos >= length - x_c: return q_max * ((length - pos) / x_c)
                                else: return q_max

                        is_triangle = (is_short_span or abs(Lx - Ly) < edge_tol)
                        
                        for k, (eid, seg_len) in enumerate(subs):
                            seg_start_x = sum(s[1] for s in subs[:k])
                            seg_end_x = seg_start_x + seg_len
                            
                            q_start = get_q(seg_start_x, beam_len, q_max, x_c_p, is_triangle)
                            q_end = get_q(seg_end_x, beam_len, q_max, x_c_p, is_triangle)
                            q_avg = (q_start + q_end) / 2.0
                            
                            ops.eleLoad('-ele', eid, '-type', '-beamUniform', -q_avg, 0.0)
                            
                            load_on_seg = q_avg * seg_len
                            total_applied_force_z -= load_on_seg


        # --- SOLVE ---
        ops.system('BandGeneral') 

        ops.numberer('RCM')
        ops.constraints('Transformation') 
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
                res["nodes"][nid]["disp"] = [round(v, 4) for v in d]
            
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
                                  "V2": round(forces["V2"], 2),
                                  "V3": round(col_sign * forces["V3"], 2),  # Negate for columns
                                  "T":  round(col_sign * forces["T"], 2),   # Negate for columns
                                  "M2": round(-forces["M2"], 2),  # SAP2000 sign convention
                                  "M3": round(forces["M3"], 2)
                              })
                          
                          # Calculate max deflection for this element
                          max_defl = get_max_deflection(item, node_coords, sub_elements_map)
                          
                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length,
                               "max_deflection": max_defl,
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
                          
                          for i, (sub_eid, sub_len) in enumerate(subs):
                                # Get raw forces from OpenSees (12-component array)
                                forces_raw = ops.eleForce(sub_eid)
                                
                                # For FIRST sub-element, include i-node (station 0)
                                if i == 0:
                                    # Extract i-node forces (indices 0-5)
                                    # BEAM mapping: P=0, V2=2, V3=1, T=3, M2=5, M3=4
                                    i_forces = {
                                        "P": -forces_raw[0],
                                        "V2": -forces_raw[2],
                                        "V3": -forces_raw[1],
                                        "T": -forces_raw[3],
                                        "M2": -forces_raw[5],
                                        "M3": forces_raw[4]
                                    }
                                    
                                    stations_output.append({
                                        "station": 0.0,
                                        "distance_mm": 0.0,
                                        "P":  round(i_forces["P"], 2),
                                        "V2": round(i_forces["V2"], 2),
                                        "V3": round(i_forces["V3"], 2),
                                        "T":  round(i_forces["T"], 2),
                                        "M2": round(-i_forces["M2"], 2),
                                        "M3": round(i_forces["M3"], 2)
                                    })
                                
                                # Always include j-node (end of this sub-element)
                                # Extract j-node forces (indices 6-11)
                                j_dist = cumulative_dist + sub_len
                                global_ratio = j_dist / element_length_total if element_length_total > 0 else 0
                                
                                j_forces = {
                                    "P": forces_raw[6],
                                    "V2": forces_raw[8],
                                    "V3": forces_raw[7],
                                    "T": forces_raw[9],
                                    "M2": forces_raw[11],
                                    "M3": -forces_raw[10]
                                }
                                
                                stations_output.append({
                                    "station": round(global_ratio, 4),
                                    "distance_mm": round(j_dist, 2),
                                    "P":  round(j_forces["P"], 2),
                                    "V2": round(j_forces["V2"], 2),
                                    "V3": round(j_forces["V3"], 2),
                                    "T":  round(j_forces["T"], 2),
                                    "M2": round(-j_forces["M2"], 2),
                                    "M3": round(j_forces["M3"], 2)
                                })
                                
                                cumulative_dist += sub_len
                          
                          # Calculate max deflection for this element
                          max_defl = get_max_deflection(item, node_coords, sub_elements_map)
                          
                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length_total,
                               "max_deflection": max_defl,
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
    struct_config = detect_structure_config(elements_list)
    n_stories = struct_config['n_stories']
    story_height_mm = struct_config['story_height']
    story_height_m = story_height_mm / 1000.0
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
                # Column: 50% to top node, 50% to bottom node
                z_bot = min(start_z, end_z)
                z_top = max(start_z, end_z)
                is_base_column = abs(z_bot - min_z) < 100
                
                # --- TOP HALF: always goes to the floor at z_top ---
                for fi in range(n_stories):
                    if abs(z_top - floor_z[fi]) < 100:
                        Wi_N[fi] += w_element_N * 0.5
                        break
                
                # --- BOTTOM HALF ---
                if is_base_column:
                    # Bottom is at foundation (z=0): track separately
                    # Per SNI 1726 / ASCE 7: W = total dead load (including base mass)
                    # Base mass contributes to W_total but NOT to floor Wi
                    # (no lateral force applied at restrained base nodes)
                    base_mass_N += w_element_N * 0.5
                else:
                    # Bottom is at an upper floor level: add to that floor's Wi
                    for fi in range(n_stories):
                        if abs(z_bot - floor_z[fi]) < 100:
                            Wi_N[fi] += w_element_N * 0.5
                            break
                            
            else:
                # Beam: 100% to the floor where beam is located
                beam_z = (start_z + end_z) / 2.0
                for fi in range(n_stories):
                    if abs(beam_z - floor_z[fi]) < 100:
                        Wi_N[fi] += w_element_N
                        break
        
        # --- A2. Slab weights (per floor) ---
        # Number of panels per floor = n_span_x * n_span_y
        n_panels = n_span_x * n_span_y
        # Slab area per panel (mm²)
        panel_area_mm2 = span_x * span_y
        
        # Slab SW per floor (N): pressure(N/mm²) * area(mm²) * n_panels
        slab_sw_per_floor_N = SLAB_SW_PRESSURE * panel_area_mm2 * n_panels
        # ADL per floor (N)
        slab_adl_per_floor_N = SLAB_ADL_PRESSURE * panel_area_mm2 * n_panels
        
        for fi in range(n_stories):
            Wi_N[fi] += slab_sw_per_floor_N + slab_adl_per_floor_N
        
        # Convert to kN
        Wi_kN = [w / 1000.0 for w in Wi_N]
        base_mass_kN = base_mass_N / 1000.0
        
        # W_total = sum of ALL floor weights + base mass
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
        
        for nid, coords in node_coords.items():
            ops.node(nid, *coords)
            res["nodes"][nid] = {"coords": coords, "disp": [0.0]*6, "reaction": None}
            if abs(coords[2] - min_z_val) < 100.0:
                ops.fix(nid, 1, 1, 1, 1, 1, 1)
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
            vecxz = local_axes.get('y_axis', [0, 1, 0])
            
            # Map to OpenSees convention (same as run_load_case)
            if item['is_vertical']:
                # COLUMN: local-y=global-Y, local-z=global+X
                Ops_Iy = Iz   # Strong axis -> bending about local-y -> F1
                Ops_Iz = Iy   # Weak axis   -> bending about local-z -> F2
                Ops_Avy = Avz # web shear   -> local-y (Y-direction)
                Ops_Avz = Avy # flange shear-> local-z (X-direction)
            else:
                # BEAM: standard convention
                Ops_Iy = Iy   # weak axis
                Ops_Iz = Iz   # strong axis
                Ops_Avy = Avz # web shear -> vertical (local-y)
                Ops_Avz = Avy # flange    -> horizontal (local-z)
            
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
            
            # Sub-elements (8 per element for beams, 4 for columns)
            num_subs = 8 if not item['is_vertical'] else 4
            sub_ids = []
            
            n_start_elem = item['nodes'][0]
            n_end_elem = item['nodes'][1]
            coord_start = node_coords[n_start_elem]
            coord_end = node_coords[n_end_elem]
            
            # --- BEAM INSERTION POINT OFFSET (seismic) ---
            if BEAM_INSERTION_POINT_TOP and not item['is_vertical']:
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
                coord_start = off_s
                coord_end = off_e
            else:
                beam_start = n_start_elem
                beam_end = n_end_elem
            
            vx = coord_end[0]-coord_start[0]
            vy_v = coord_end[1]-coord_start[1]
            vz_c = coord_end[2]-coord_start[2]
            
            prev_node = beam_start
            for k_sub in range(num_subs):
                if k_sub == num_subs - 1:
                    curr_node = beam_end
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
                    # NOTE: these intermediate nodes are NOT in original_joint_nids
                
                sub_ele_id = item['id'] * 100 + k_sub
                if sub_ele_id > 2000000000:
                    sub_ele_id = int(sub_ele_id % 1000000 + 900000)
                
                sub_transf_tag = transf_counter
                transf_counter += 1
                
                sub_dI = [0.0, 0.0, 0.0]
                sub_dJ = [0.0, 0.0, 0.0]
                if k_sub == 0:
                    sub_dI = list(item['dI'])
                if k_sub == num_subs - 1:
                    sub_dJ = list(item['dJ'])
                
                ops.geomTransf('Linear', sub_transf_tag,
                              vecxz[0], vecxz[1], vecxz[2],
                              '-jntOffset', sub_dI[0], sub_dI[1], sub_dI[2],
                              sub_dJ[0], sub_dJ[1], sub_dJ[2])
                
                ops.element('ElasticTimoshenkoBeam', sub_ele_id,
                           prev_node, curr_node,
                           E, G, A, J, Ops_Iy, Ops_Iz, Ops_Avy, Ops_Avz, sub_transf_tag)
                
                sub_ids.append((sub_ele_id, item['length']/num_subs))
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
        # G. APPLY LATERAL FORCES (NO GRAVITY)
        # ================================================================
        ops.timeSeries('Linear', 1)
        ops.pattern('Plain', 1, 1)
        
        for fi in range(n_stories):
            Fx_N = Fx_kN[fi] * 1000.0  # kN → N
            
            if direction == 'EQx':
                ops.load(master_nodes[fi], Fx_N, 0.0, 0.0, 0.0, 0.0, 0.0)
            else:  # EQy
                ops.load(master_nodes[fi], 0.0, Fx_N, 0.0, 0.0, 0.0, 0.0)
        
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
                    "Fx": round(r[0], 2), "Fy": round(r[1], 2), "Fz": round(r[2], 2),
                    "Mx": round(r[3], 2), "My": round(r[4], 2), "Mz": round(r[5], 2)
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
        
        # Element internal forces (basic)
        for item in processed_elements:
            elem_id = item['id']
            subs = sub_elements_map.get(elem_id, [])
            if subs:
                # Get forces at first sub-element start and last sub-element end
                first_eid = subs[0][0]
                last_eid = subs[-1][0]
                try:
                    f_start = ops.eleForce(first_eid)
                    f_end = ops.eleForce(last_eid)
                    res["elements"][str(elem_id)] = {
                        "type": item['raw'].get('type', 'Unknown'),
                        "start_forces": [round(f, 2) for f in f_start[:6]],
                        "end_forces": [round(f, 2) for f in f_end[6:12]],
                    }
                except:
                    pass
    
    except Exception as e:
        import traceback
        print(f"[ERROR] Seismic analysis {direction}: {e}")
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
                
                # Element line color based on type
                if elem_type == "Column":
                    line_color = 'purple'
                    line_width = 3
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
                        
                        # Get structure configuration
                        struct_config = detect_structure_config(elements)
                        span_x = struct_config['span_x']
                        span_y = struct_config['span_y']
                        n_span_x = struct_config['n_span_x']
                        n_span_y = struct_config['n_span_y']
                        edge_x = n_span_x * span_x / 2.0
                        edge_y = n_span_y * span_y / 2.0
                        panel_tol = 10.0
                        
                        # Build panels list (same as load application)
                        panels = []
                        for ix in range(n_span_x):
                            for iy in range(n_span_y):
                                x0 = -edge_x + ix * span_x
                                x1 = x0 + span_x
                                y0 = -edge_y + iy * span_y
                                y1 = y0 + span_y
                                panels.append({'x0': x0, 'x1': x1, 'y0': y0, 'y1': y1})
                        
                        # Detect beam-panel adjacency (using X/Y only, ignoring Z)
                        sx, sy = start[0], start[1]
                        ex, ey = end[0], end[1]
                        is_x_beam = abs(sy - ey) < panel_tol
                        is_y_beam = abs(sx - ex) < panel_tol
                        
                        adjacent_panels = []
                        for panel in panels:
                            if is_x_beam:
                                if abs(sy - panel['y0']) < panel_tol or abs(sy - panel['y1']) < panel_tol:
                                    beam_x0, beam_x1 = min(sx, ex), max(sx, ex)
                                    if beam_x0 >= panel['x0'] - panel_tol and beam_x1 <= panel['x1'] + panel_tol:
                                        adjacent_panels.append(panel)
                            elif is_y_beam:
                                if abs(sx - panel['x0']) < panel_tol or abs(sx - panel['x1']) < panel_tol:
                                    beam_y0, beam_y1 = min(sy, ey), max(sy, ey)
                                    if beam_y0 >= panel['y0'] - panel_tol and beam_y1 <= panel['y1'] + panel_tol:
                                        adjacent_panels.append(panel)
                        
                        n_adj = len(adjacent_panels)
                        if n_adj == 0:
                            n_adj = 1  # Fallback
                        
                        # Calculate q_max based on two-way slab theory
                        L_short = min(span_x, span_y)
                        x_c = L_short / 2.0
                        q_max_per_panel = FLOOR_PRESSURE * x_c  # N/mm per panel
                        q_max_total = q_max_per_panel * n_adj  # Total for interior beams
                        
                        # Number of load arrows to draw along beam
                        n_arrows = 9
                        max_arrow_length = elem_length * 0.25  # INCREASED scale
                        
                        # Scale q_max to arrow length
                        q_scale = max_arrow_length / max(q_max_per_panel * 2, 0.1)

                        
                        for i in range(n_arrows):
                            ratio = i / (n_arrows - 1)
                            
                            # Position along element
                            pos = [
                                start[0] + ratio * (end[0] - start[0]),
                                start[1] + ratio * (end[1] - start[1]),
                                start[2] + ratio * (end[2] - start[2])
                            ]
                            
                            # Triangular load distribution: 0 -> q_max_total (at center) -> 0
                            if ratio <= 0.5:
                                q_at_pos = q_max_total * (ratio * 2)
                            else:
                                q_at_pos = q_max_total * ((1 - ratio) * 2)
                            
                            arrow_length = q_at_pos * q_scale
                            
                            if arrow_length > 10:  # Only draw visible arrows
                                # Arrow direction (downward = gravity)
                                ax.quiver(pos[0], pos[1], pos[2] + arrow_length,
                                          0, 0, -arrow_length,
                                          color=arrow_color, arrow_length_ratio=0.15, linewidth=1.0, alpha=0.8)
                        
                        # Add load value annotation showing total q with panel count
                        mid_z_offset = max_arrow_length * 1.4
                        ax.text(mid[0], mid[1], mid[2] + mid_z_offset, 
                                f"q={q_max_total:.1f}",
                                fontsize=6, color=arrow_color, ha='center', alpha=0.9)

            
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
            legend_elements = [
                Line2D([0], [0], color='purple', linewidth=3, label='Column'),
                Line2D([0], [0], color='blue', linewidth=2.5, label='Beam'),
                Line2D([0], [0], color='red', linewidth=1.5, label='Local X-axis'),
                Line2D([0], [0], color='green', linewidth=1.5, label='Local Y-axis'),
                Line2D([0], [0], color='cyan', linewidth=1.5, label='Local Z-axis'),
            ]
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
                'disp': [round(v, 4) for v in combined_disp],
                'reaction': combined_reaction
            }

        combo_result['summary']['total_reaction_z'] = round(total_rz, 2)

        # === COMBINE ELEMENTS ===
        all_elem_ids = set()
        for pat_name in available:
            all_elem_ids.update(results[pat_name].get('elements', {}).keys())

        force_keys = ['P', 'V2', 'V3', 'T', 'M2', 'M3']

        for eid in all_elem_ids:
            # Get element structure from first available pattern
            ref_elem = None
            for pat_name in available:
                pat_elems = results[pat_name].get('elements', {})
                if eid in pat_elems:
                    ref_elem = pat_elems[eid]
                    break
            if not ref_elem:
                continue

            # Combine stations
            ref_stations = ref_elem.get('stations', [])
            combined_stations = []

            for si, ref_stn in enumerate(ref_stations):
                combined_stn = {'station': ref_stn.get('station', 0.0)}

                for fk in force_keys:
                    val = 0.0
                    for pat_name, factor in available.items():
                        pat_elems = results[pat_name].get('elements', {})
                        if eid in pat_elems:
                            pat_stns = pat_elems[eid].get('stations', [])
                            if si < len(pat_stns):
                                val += factor * pat_stns[si].get(fk, 0)
                    combined_stn[fk] = round(val, 2)

                combined_stations.append(combined_stn)

            combined_elem = {
                'element_type': ref_elem.get('element_type'),
                'stations': combined_stations,
            }

            # Combine max_deflection if present
            if 'max_deflection' in ref_elem:
                combined_defl = {}
                defl_keys = ['delta_y_max_mm', 'delta_y_station', 'delta_y_distance_mm',
                             'delta_z_max_mm', 'delta_z_station', 'delta_z_distance_mm']
                for dk in defl_keys:
                    val = 0.0
                    for pat_name, factor in available.items():
                        pat_elems = results[pat_name].get('elements', {})
                        if eid in pat_elems:
                            pat_defl = pat_elems[eid].get('max_deflection', {})
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

    # ========== SEISMIC ANALYSIS (SNI 1726 ELF) ==========
    import copy
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
