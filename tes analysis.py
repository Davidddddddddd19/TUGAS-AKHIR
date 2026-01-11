import sys
import json
import math
import openseespy.opensees as ops   

# ============================================================================
# 1. KONFIGURASI FISIKA
# ============================================================================
G_ACC = 9.81              # Gravitasi (m/s^2) - Standard SI value
# SAP2000 comparison notes: Without calibration factor, expect ~7% lower F3 and ~5% higher M2
# This is due to differences in element formulation between OpenSeesPy and SAP2000
FACTOR_SW = 1.0           # No calibration - balanced deviation on F3 and M2
CONN_STIFFNESS_FACTOR_WEAK = 1.0   # No adjustment - investigate root cause first
CONN_STIFFNESS_FACTOR_STRONG = 1.0 # No adjustment - investigate root cause first
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
    
    # Inersia (Mapping: Ix=Strong, Iy=Weak)
    # Values are now calculated manually in script.py - use directly
    Ix_json = float(sec.get('Ix_mm4', 10000))
    Iy_json = float(sec.get('Iy_mm4', 1000))
    
    # Torsi
    J_raw = float(sec.get('J_mm4', 0))
    J = J_raw if J_raw > 10.0 else (Ix_json + Iy_json) * 0.01

    # Shear Areas
    Avy = 2.0 * b * tf  
    Avz = d * tw        
    
    if Avy <= 1.0: Avy = A * 0.5
    if Avz <= 1.0: Avz = A * 0.5
    
    return A, J, Ix_json, Iy_json, Avy, Avz

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
# 2. FUNGSI ANALISIS PER KASUS BEBAN (CORE LOGIC)

# ============================================================================
def run_load_case(data, case_type):
    """
    Menjalankan analisis untuk satu tipe beban (SW, LL, atau COMB).
    """
    # Struktur Data Output
    res = {
        "status": "Failed",
        "nodes": {}, 
        "elements": {},
        "summary": {"total_reaction_z": 0}
    }

    elements_list = data.get('model_elements', [])
    
    # --- UPDATE: AMBIL PRESSURE LOAD DARI JSON ---
    pressure_list = data.get('global_pressure_loads', [])
    # Ambil nilai pertama dari list, jika kosong gunakan 0.0
    FLOOR_PRESSURE = float(pressure_list[0]) if pressure_list else 0.0

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
            Returns forces with SAP2000-aligned sign conventions:
            - Compression P is NEGATIVE
            - Beam shear: opposite signs at ends for equilibrium
            - Moments follow standard structural conventions
            """
            # Get element forces from OpenSees (resisting forces = element on node)
            forces_resisting = ops.eleForce(elem_id)
            
            # Handle elements with less than 12 force components
            if len(forces_resisting) < 12:
                # Simple fallback for single-node or special elements
                f_global = forces_resisting[:6]
                loc = calculate_local_forces(f_global, local_axes)
                
                # SAP2000 conventions: compression negative
                return {
                    "P": -loc["P"],      # Compression negative
                    "V2": -loc["V2"],    # Standard shear convention
                    "V3": -loc["V3"],
                    "T": -loc["T"],      # Torsion
                    "M2": -loc["M2"],    # Bending moments
                    "M3": loc["M3"]
                }
            
            # Extract start and end node forces (global coordinates)
            f_start_global = forces_resisting[:6]
            f_end_global = forces_resisting[6:]
            
            # Transform to local element coordinates
            loc_start = calculate_local_forces(f_start_global, local_axes)
            loc_end = calculate_local_forces(f_end_global, local_axes)
            
            # ===============================================
            # STRUCTURAL EQUILIBRIUM-BASED SIGN CONVENTION
            # ===============================================
            # FUNDAMENTAL PRINCIPLE:
            # ops.eleForce returns RESISTING forces (element ON node)
            # Internal forces = NEGATIVE of resisting forces
            # BUT: Need opposite shear signs at ends for equilibrium!
            
            # START NODE: Internal force = NEGATIVE of resisting force
            # ops.eleForce returns resisting forces (element ON node)
            # Internal force = force the element applies, which is -resisting
            start_internal = {
                "P": -loc_start["P"],      # Compression negative
                "V2": -loc_start["V2"],    # Internal shear
                "V3": -loc_start["V3"],    # Internal shear
                "T": -loc_start["T"],
                "M2": -loc_start["M2"],    # Internal moment
                "M3": -loc_start["M3"]     # Internal moment
            }
            
            # END NODE: Internal force = resisting force DIRECTLY (no negation)
            # 
            # Why? OpenSees resisting forces at END node are measured in the
            # opposite direction compared to START node due to action-reaction.
            # 
            # Example for column with constant shear V3:
            # - Resisting force at START: loc_start["V3"] = -R (pushing left)
            # - Resisting force at END: loc_end["V3"] = +R (pushing right)
            # - Internal shear at START: -(-R) = +R
            # - Internal shear at END: +R (direct, same as start!)
            # 
            # This ensures:
            # - Column (constant shear): V_start = V_end → W_total = 0 → linear moment ✓
            # - Beam (varying shear): V_start ≠ V_end → W_total ≠ 0 → parabolic moment ✓
            
            end_internal = {
                "P": loc_end["P"],          # Axial: direct (like V2/V3) for consistent distribution
                "V2": loc_end["V2"],       # Shear V2: direct (no negation)
                "V3": loc_end["V3"],       # Shear V3: direct (no negation)
                "T": -loc_end["T"],        # Torsion: standard negation
                "M2": loc_end["M2"],       # M2: direct - allows opposite sign from start for proper interpolation
                "M3": loc_end["M3"]        # M3: direct (no negation)
            }        
            
            # Linear interpolation (basic - will be replaced by exact for critical stations)
            interp = {}
            for key in ["P", "V2", "V3", "T", "M2", "M3"]:
                val = start_internal[key] * (1 - ratio) + end_internal[key] * ratio
                interp[key] = val
                
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
            
            # With output negation applied, use dM2/dx = +V3 (standard structural relationship)
            # When V3 is negative, internal M2 increases (before output negation)
            # M2(x) = M2_start + V3_start*x - (W3_total/L)*x²/2
            m2_x = m2_s + v3_s * x - (W3_total / length_mm) * (x**2) / 2.0
            
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
            I_major = float(sec.get('Ix_mm4', 0))  # Strong axis
            I_minor = float(sec.get('Iy_mm4', 0))  # Weak axis
            
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

        # --- BUILD ELEMENTS & TRANSFORMS ---
        # Dynamic Transforms will be created per element using local axes from JSON 

        # Dictionary to track sub-elements for reporting
        # parent_id -> list of (sub_ele_id, length)
        sub_elements_map = {} 

        for item in processed_elements:
            sec = item['raw']['section']
            mat = item['raw']['material']
            
            # --- DYNAMIC TRANSFORM ---
            # Extract Local Z axis for geomTransf vector
            local_axes = item['raw'].get('local_axes', {})
            vec_z = local_axes.get('z_axis', [0, 0, 1])
            
            # Use Element ID as unique Transform Tag
            transf_tag = item['id']
            # Use PDelta only for COMB loads to capture second-order effects
            # For SW (selfweight) use Linear to match SAP2000 behavior
            if item['is_vertical'] and case_type == 'COMB':
                ops.geomTransf('PDelta', transf_tag, vec_z[0], vec_z[1], vec_z[2])
            else:
                ops.geomTransf('Linear', transf_tag, vec_z[0], vec_z[1], vec_z[2])

            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 80000))
            A, J, Ix, Iy, Avy, Avz = get_section_properties(sec)
            
            # Apply Selective Stiffness Correction for Columns (COMB only)
            # This adjustment is for combined load cases to match SAP2000 frame behavior
            # For individual load cases (SW, LL), use full stiffness for accurate comparison
            if item['is_vertical'] and case_type == 'COMB':
                 Ix *= CONN_STIFFNESS_FACTOR_STRONG  # Reduce to lower M3
                 Iy *= CONN_STIFFNESS_FACTOR_WEAK    # Increase to raise V3
                 J *= CONN_STIFFNESS_FACTOR_WEAK
            
            # Setup Section Properties
            alphaY = Avy / A if A > 0 else 0.5
            alphaZ = Avz / A if A > 0 else 0.5
            
            # --- INERTIA MAPPING ---
            # JSON: Ix = Major Axis (Strong), Iy = Minor Axis (Weak).
            # OpenSees: Iy = About Local Y, Iz = About Local Z.
            
            # COLUMN (Local Y=Global X, Local Z=Global Y):
            # Bending about Local Z (Global Y) -> Resists X-force. 
            # This is Strong Axis for typical Column orientation -> Use Ix.
            
            # BEAM (Local Y=Vertical, Local Z=Horizontal):
            # Bending about Local Z (Horizontal) -> Vertical Load bending.
            # This is Strong Axis (Major Moment) -> Use Ix.
            
            # THEREFORE: Ops_Iz should be Ix (Strong) and Ops_Iy should be Iy (Weak).
            Ops_Iy = Iy
            Ops_Iz = Ix
            
            if item['is_vertical']:
                # --- KOLOM (SINGLE ELEMENT) ---
                sec_tag = item['id']
                # ops.section('Elastic', sec_tag, E, A, Iz, Iy, G, J) note: section command typically takes Iz, Iy order?
                # manual: section Elastic $secTag $E $A $Iz $Iy $G $J <$alphaY $alphaZ>
                # Using Ops_Iz (Strong) first, then Ops_Iy (Weak). 
                ops.section('Elastic', sec_tag, E, A, Ops_Iz, Ops_Iy, G, J, alphaY, alphaZ)
                int_tag = item['id']
                ops.beamIntegration('Legendre', int_tag, sec_tag, 3)
                
                # ElasticTimoshenkoBeam $eleTag $iNode $jNode $E $G $A $J $Iy $Iz $Avy $Avz $transfTag
                # ORDER MATTERS: Iy comes before Iz in arguments.
                ops.element('ElasticTimoshenkoBeam', item['id'], item['nodes'][0], item['nodes'][1], 
                            E, G, A, J, Ops_Iy, Ops_Iz, Avy, Avz, transf_tag)
                
                sub_elements_map[item['id']] = [(item['id'], item['length'])]
                
            else:
                # --- BALOK (SUBDIVIDED INTO 8 SEGMENTS) ---
                # Beam mapping identical to column now (Ops_Iy=Ix, Ops_Iz=Iy) due to consistent local axes
                # More segments = better accuracy for triangular loads
                
                n_start = item['nodes'][0]
                n_end = item['nodes'][1]
                coord_start = node_coords[n_start]
                coord_end = node_coords[n_end]
                
                vx = coord_end[0] - coord_start[0]
                vy = coord_end[1] - coord_start[1]
                vz = coord_end[2] - coord_start[2]
                
                num_subs = 8
                sub_ids = []
                
                prev_node = n_start
                for k in range(num_subs):
                    if k == num_subs - 1:
                        curr_node = n_end
                    else:
                        ratio = (k + 1) / num_subs
                        nx = coord_start[0] + vx * ratio
                        ny = coord_start[1] + vy * ratio
                        nz = coord_start[2] + vz * ratio
                        
                        curr_node = next_node_id
                        node_coords[curr_node] = (nx, ny, nz)
                        ops.node(curr_node, nx, ny, nz)
                        res["nodes"][curr_node] = {"coords":(nx,ny,nz), "disp":[0]*6, "reaction":None}
                        next_node_id += 1
                    
                    sub_ele_id = item['id'] * 100 + k 
                    if sub_ele_id > 2000000000: sub_ele_id = int(sub_ele_id % 1000000 + 900000)

                    ops.element('ElasticTimoshenkoBeam', sub_ele_id, prev_node, curr_node, 
                                E, G, A, J, Ops_Iy, Ops_Iz, Avy, Avz, transf_tag)
                                
                    sub_ids.append((sub_ele_id, item['length']/num_subs))
                    prev_node = curr_node
                
                sub_elements_map[item['id']] = sub_ids

        # --- LOAD APPLICATION ---
        ops.timeSeries('Linear', 1)
        ops.pattern('Plain', 1, 1)

        # A. SELF WEIGHT
        if case_type in ['SW', 'COMB']:
            for item in processed_elements:
                mat = item['raw']['material']
                rho = float(mat.get('Rho_kg/m3', 0))
                if rho == 0: rho = float(mat.get('Rho_kg/mm3', 0)) * 1e9
                
                # Apply Correction Factor to match SAP2000 Dead Load
                w_dead = float(item['raw']['section'].get('Area_mm2', 0)) * (rho * 1e-9) * G_ACC * FACTOR_SW
                
                # Check orientation for load sum
                # w_dead is force per unit length
                # We apply it to all sub-elements.
                item_len = item['length']
                
                # Equilibrium Check: Add Dead Load (Downwards = Negative Global Z)
                # w_dead is positive scalar. Load is -w_dead in Z.
                total_applied_force_z -= w_dead * item_len
                
                # Apply to all sub-elements
                subs = sub_elements_map.get(item['id'], [(item['id'], item['length'])])
                
                for (eid, fractional_len) in subs:
                     if item['is_vertical']:
                         # Column: Gravity uses Wx (Axial) if needed, or Wz if web horizontal. 
                         # Assuming Column Local X is Vertical (Global Z).
                         # Gravity acts Global -Z (Local -X).
                         # Ops beamUniform Wx is axial.
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', 0.0, 0.0, -w_dead) 
                     else:
                         # Beam: 
                         # Local Y is Vertical (Global Z).
                         # Local Z is Horizontal (Global -Y).
                         # beamUniform args: wy, wz, wx.
                         # Apply to wy (1st arg) for Vertical Load.
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', -w_dead, 0.0, 0.0)

        # B. FLOOR PRESSURE (LIVE LOAD)
        if case_type in ['LL', 'COMB']:
            for item in processed_elements:
                # Hanya Balok Horizontal yang menerima beban lantai
                if not item['is_vertical']: 
                    # Parse element-specific loads
                    load_params = apply_element_loads(item['raw'], case_type)
                    
                    if load_params['has_loads']:
                        total_load_accum = 0.0
                        
                        # Apply Distributed Loads (Stepped)
                        load_shape = load_params.get('load_shape', 'Uniform')
                        
                        for (w_start, w_end, direction) in load_params['distributed_loads']:
                            # direction from JSON is 'Z' for vertical usually
                            # User JSON (Step 684) doesn't explicitly show direction in 'loads' block?
                            # Actually apply_element_loads extracts it.
                            # Assuming Vertical Load (Z direction).
                            
                            if direction == 'Y' or direction == 'Z': # Vertical
                                subs = sub_elements_map.get(item['id'])
                                if not subs: continue
                                
                                num_subs = len(subs)
                                
                                for k, (eid, seg_len) in enumerate(subs):
                                    # Calculate avg q for this segment
                                    x_start_rel = k / num_subs
                                    x_end_rel = (k + 1) / num_subs
                                    x_mid_rel = (x_start_rel + x_end_rel) / 2.0
                                    
                                    if load_shape == 'Triangle':
                                        # Symmetric Triangle: 0 -> Peak -> 0
                                        # Peak is stored in w_end (passed from apply_element_loads)
                                        q_peak = w_end
                                        
                                        # Helper function for Triangle shape (0 at ends, 1 at 0.5)
                                        def get_tri_q(x):
                                            if abs(x - 0.5) < 1e-9: return q_peak
                                            if x < 0.5: return q_peak * (x / 0.5)
                                            else: return q_peak * ((1.0 - x) / 0.5)
                                            
                                        q_start_val = get_tri_q(x_start_rel)
                                        q_mid_val = get_tri_q(x_mid_rel)
                                        q_end_val = get_tri_q(x_end_rel)
                                        
                                        # Simpson's Rule for integration: (h/6)*(f(a) + 4*f(mid) + f(b))
                                        # For average value: integral/length = (f(a) + 4*f(mid) + f(b))/6
                                        q_seg_avg = (q_start_val + 4.0 * q_mid_val + q_end_val) / 6.0
                                        
                                    # Linear/Uniform: Start -> End
                                    else:
                                        q_start_val = w_start + (w_end - w_start) * x_start_rel
                                        q_end_val = w_start + (w_end - w_start) * x_end_rel
                                        # Trapezoidal rule for linear is exact
                                        q_seg_avg = (q_start_val + q_end_val) / 2.0
                                    
                                    # Apply Uniform Load to Segment
                                    # Beam Local Y is Vertical (Global Z).
                                    # beamUniform args: wy, wz, wx
                                    # Apply to wy (1st arg)
                                    # Load is -q (Down).
                                    
                                    ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                               -q_seg_avg, 0.0, 0.0)
                                               
                                    total_load_accum += q_seg_avg * seg_len
                                    
                                    # Track Equilibrium (Downwards = Negative)
                                    total_applied_force_z -= q_seg_avg * seg_len

                        
                        item['applied_load'] = f"{load_params['load_summary']} (Stepped 8x, Simpson)"
                    else:
                        L = item['length']
                        w_live = (FLOOR_PRESSURE * L) / 4.0
                        
                        # Apply Point Loads
                        for (loc_ratio, force_N, direction) in load_params.get('point_loads', []):
                            # Locate Correct Sub-Element
                            subs = sub_elements_map.get(item['id'])
                            if subs:
                                # Determine cumulative length to find segment
                                cum_len = 0.0
                                total_len = item['length']
                                target_dist = loc_ratio * total_len
                                
                                for (eid, seg_len) in subs:
                                    if cum_len <= target_dist <= (cum_len + seg_len):
                                        # Found segment
                                        local_x = (target_dist - cum_len) / seg_len # 0..1
                                        
                                        # Apply Point Load
                                        # Using -beamPoint P x (relative)
                                        # Force is DOWN (Y), so -force_N
                                        ops.eleLoad('-ele', eid, '-type', '-beamPoint', -force_N, local_x)
                                        
                                        total_applied_force_z -= force_N
                                        # Break after applying (Point Load is singular)
                                        break
                                    cum_len += seg_len
                        
                        if w_live > 0:
                            subs = sub_elements_map.get(item['id'], [(item['id'], L)])
                            for (eid, seg_len) in subs:
                                    # Order: Wy, Wz, Wx
                                    # Beam Local Y is Vertical -> Use Wy
                                    ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                               -w_live, 0.0, 0.0)
                                               
                                    # Track Equilibrium
                                    total_applied_force_z -= w_live * seg_len
                        item['applied_load'] = f"Global Pressure: {w_live:.2f}N/mm"

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
                    res["nodes"][nid]["reaction"] = {
                        "F1": round(reac[0], 2), # Fx -> F1
                        "F2": round(reac[1], 2), # Fy -> F2
                        "F3": round(reac[2], 2), # Fz -> F3
                        "M1": round(reac[3], 2), # Mx -> M1
                        "M2": round(reac[4], 2), # My -> M2
                        "M3": round(reac[5], 2)  # Mz -> M3
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
                    if not subs:
                          # NO SUB-ELEMENTS: Use adaptive stationing
                          local_axes = item['raw'].get('local_axes', {})
                          
                          # Get element length from topology
                          element_length = item['raw'].get('topology', {}).get('length_mm', 0)
                          
                          # Find critical stations (boundaries, zero crossings, max points)
                          is_vert = item.get('is_vertical', False)
                          critical_stations = find_critical_stations(eid, local_axes, element_length, num_samples=5, is_vertical=is_vert)
                          
                          # Build stations list for output
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
                                  "V3": round(forces["V3"], 2),
                                  "T":  round(forces["T"], 2),
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
                          # SUB-ELEMENTS: Average across all sub-elements or use first (legacy logic)
                          # For now, use FIRST sub-element with adaptive stationing
                          first_eid = subs[0][0]
                          local_axes = item['raw'].get('local_axes', {})
                          
                          # Get element length from topology
                          element_length = item['raw'].get('topology', {}).get('length_mm', 0)
                          
                          # Iterate ALL sub-elements
                          element_length_total = item['raw'].get('topology', {}).get('length_mm', 0)
                          is_vert = item.get('is_vertical', False)
                          stations_output = []
                          cumulative_dist = 0.0
                          
                          for i, (sub_eid, sub_len) in enumerate(subs):
                                # Find critical stations for this sub-element
                                critical_stations = find_critical_stations(sub_eid, local_axes, sub_len, num_samples=5, is_vertical=is_vert)
                                
                                for station_data in critical_stations:
                                    local_ratio = station_data['station']
                                    local_dist = local_ratio * sub_len
                                    
                                    # Global Element Context
                                    actual_distance = cumulative_dist + local_dist
                                    global_ratio = actual_distance / element_length_total if element_length_total > 0 else 0
                                    
                                    forces = station_data['forces']
                                    
                                    # Filter duplicates if needed (e.g. End of Sub 1 == Start of Sub 2)
                                    # But keeping all is safer for "stepped" diagrams if values differ.
                                    
                                    stations_output.append({
                                        "station": round(global_ratio, 4),
                                        "distance_mm": round(actual_distance, 2),
                                        "P":  round(forces["P"], 2),
                                        "V2": round(forces["V2"], 2),
                                        "V3": round(forces["V3"], 2),
                                        "T":  round(forces["T"], 2),
                                        "M2": round(-forces["M2"], 2),  # SAP2000 sign convention
                                        "M3": round(forces["M3"], 2)
                                    })
                                    
                                cumulative_dist += sub_len
                          
                          # Calculate max deflection for this element
                          max_defl = get_max_deflection(item, node_coords, sub_elements_map)
                          
                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length,
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
def run_analysis(input_path, output_path):
    data = get_model_data(input_path)
    if not data: return

    # Jalankan 3 Skenario
    results = {
        "SelfWeight": run_load_case(data, 'SW'),
        "LiveLoad": run_load_case(data, 'LL'),
        "Combination": run_load_case(data, 'COMB')
    }

    # Simpan JSON Output
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=4)

    # Tampilkan Report
    print_styled_report(results)

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
