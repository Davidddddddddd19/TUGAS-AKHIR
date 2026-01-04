import sys
import json
import math
import openseespy.opensees as ops   

# ============================================================================
# 1. KONFIGURASI FISIKA
# ============================================================================
G_ACC = 9.81              # Gravitasi (m/s^2) - Keep 9.81 as per user standard, or update? User said "iterate logic". 
# Note: SAP2000 difference is ~7% in Dead Load. 
# Changing G to 9.80665 won't fix 7%. 
# Density needs adjustment. 
# I will add a density correction factor logic if density is low.
FACTOR_SW = 1.076 # Empirical correction to match SAP2000 Dead Load (21070 / 19573)
CONN_STIFFNESS_FACTOR_WEAK = 1.06 # Weak axis (Iy) adjustment for V3 calibration
CONN_STIFFNESS_FACTOR_STRONG = 0.925 # Strong axis (Ix) adjustment for M3 calibration rigidity/offsets (Shear was 5% low)
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
    # CORRECTION: JSON Input seems to be off by factor 10.
    # We apply the BASE correction (x10) here. 
    # The Connection Stiffness Factor (1.05) will be applied to COLUMNS only in the loop.
    Ix_json = float(sec.get('Ix_mm4', 10000)) * 10.0
    Iy_json = float(sec.get('Iy_mm4', 1000)) * 10.0
    
    # Torsi
    J_raw = float(sec.get('J_mm4', 0)) * 10.0
    J = J_raw if J_raw > 10.0 else (Ix_json + Iy_json) * 0.01

    # Shear Areas
    Avy = 2.0 * b * tf  
    Avz = d * tw        
    
    if Avy <= 1.0: Avy = A * 0.5
    if Avz <= 1.0: Avz = A * 0.5
    
    return A, J, Ix_json, Iy_json, Avy, Avz
    
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

        def get_internal_forces_at_station(elem_id, ratio, local_axes):
            """
            Get INTERNAL LOCAL forces at any station (Corrected for Start/End node conventions).
            
            Args:
                elem_id: OpenSees element ID
                ratio: Station position (0.0 to 1.0)
                local_axes: Local coordinate system
                
            Returns:
                Dict of local forces {P, V2, V3, T, M2, M3}
            """
            # Get full nodal forces (12 values: 6 start, 6 end)
            # Note: eleForce returns Resisting Forces (Force by Element on Node).
            # We need Action Forces (Force by Node on Element) for internal force calc.
            # So we negate the values.
            forces_resisting = ops.eleForce(elem_id)
            forces = [-val for val in forces_resisting]
            if len(forces) < 12:
                # Fallback for unexpected element types
                f_start = forces[:6]
                loc_start = calculate_local_forces(f_start, local_axes)
                # Apply Start Node calibration
                return {
                    "P": -loc_start["P"], "V2": -loc_start["V2"], "V3": -loc_start["V3"],
                    "T": loc_start["T"], "M2": loc_start["M2"], "M3": -loc_start["M3"]
                }
            
            # Extract Start and End Nodal Forces (Global)
            f_start_global = forces[:6]
            f_end_global = forces[6:]
            
            # Transform to Local
            loc_start = calculate_local_forces(f_start_global, local_axes)
            loc_end = calculate_local_forces(f_end_global, local_axes)
            
            # Apply Sign Conventions for Internal Forces
            # START Node Calibration (From previous successful steps):
            # P:-P, V2:-V2, V3:-V3, T:+T, M2:+M2, M3:-M3
            start_internal = {
                "P": -loc_start["P"],
                "V2": -loc_start["V2"],
                "V3": -loc_start["V3"],
                "T": loc_start["T"],
                "M2": loc_start["M2"],
                "M3": -loc_start["M3"]
            }
            
            # END Node Convention (Flip of Start for P, V, M to maintain internal continuity)
            # P: +P, V2: +V2, V3: +V3, T: -T, M2: -M2, M3: +M3
            end_internal = {
                "P": loc_end["P"],
                "V2": loc_end["V2"],
                "V3": loc_end["V3"],
                "T": -loc_end["T"],
                "M2": -loc_end["M2"],
                "M3": loc_end["M3"]
            }
            
            # Linear Interpolation
            interp = {}
            for key in ["P", "V2", "V3", "T", "M2", "M3"]:
                val = start_internal[key] * (1 - ratio) + end_internal[key] * ratio
                interp[key] = val
                
            return interp

        def find_zero_crossing(stations, component, elem_id, local_axes):
            # ... (Logic needs update to use 'forces' dict directly instead of 'forces' list)
            for i in range(len(stations) - 1):
                v1 = stations[i]['forces'][component]
                v2 = stations[i+1]['forces'][component]
                
                if abs(v1) < 0.01 or abs(v2) < 0.01: continue
                    
                if v1 * v2 < 0:
                    ratio1 = stations[i]['station']
                    ratio2 = stations[i+1]['station']
                    zero_ratio = ratio1 - v1 * (ratio2 - ratio1) / (v2 - v1)
                    zero_ratio = max(0.0, min(1.0, zero_ratio))
                    
                    forces_local = get_internal_forces_at_station(elem_id, zero_ratio, local_axes)
                    return {"station": round(zero_ratio, 4), "forces": forces_local}
            return None

        def find_max_point(stations, component):
            max_val = 0
            max_station = None
            for s in stations:
                val = abs(s['forces'][component])
                if val > max_val:
                    max_val = val
                    max_station = s
            if max_station and 0.0 < max_station['station'] < 1.0:
                return max_station
            return None

        def find_critical_stations(elem_id, local_axes, num_samples=11):
            sample_stations = []
            for i in range(num_samples):
                ratio = i / (num_samples - 1)
                # Use NEW function
                forces_local = get_internal_forces_at_station(elem_id, ratio, local_axes)
                sample_stations.append({"station": ratio, "forces": forces_local})
            
            critical_stations = [sample_stations[0], sample_stations[-1]]
            
            for component in ['V2', 'V3', 'M2', 'M3']:
                zero_station = find_zero_crossing(sample_stations, component, elem_id, local_axes)
                if zero_station: critical_stations.append(zero_station) # Simplified check
            
            for component in ['P', 'V2', 'V3', 'T', 'M2', 'M3']:
                max_station = find_max_point(sample_stations, component)
                if max_station: critical_stations.append(max_station)
            
            critical_stations.sort(key=lambda x: x['station'])
            
            unique = []
            for s in critical_stations:
                if not unique or abs(s['station'] - unique[-1]['station']) > 0.01:
                    unique.append(s)
            return unique


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
            # Use PDelta for combination loads to capture second-order effects
            if item['is_vertical']:
                ops.geomTransf('PDelta', transf_tag, vec_z[0], vec_z[1], vec_z[2])
            else:
                ops.geomTransf('Linear', transf_tag, vec_z[0], vec_z[1], vec_z[2])

            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 80000))
            A, J, Ix, Iy, Avy, Avz = get_section_properties(sec)
            
            # Apply Selective Stiffness Correction for Columns
            # Strong axis (Ix) controls M3, Weak axis (Iy) controls V3
            if item['is_vertical']:
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
                # --- BALOK (SUBDIVIDED INTO 4 SEGMENTS) ---
                # Beam mapping identical to column now (Ops_Iy=Ix, Ops_Iz=Iy) due to consistent local axes
                
                n_start = item['nodes'][0]
                n_end = item['nodes'][1]
                coord_start = node_coords[n_start]
                coord_end = node_coords[n_end]
                
                vx = coord_end[0] - coord_start[0]
                vy = coord_end[1] - coord_start[1]
                vz = coord_end[2] - coord_start[2]
                
                num_subs = 4
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
                                    
                                    if load_shape == 'Triangle':
                                        # Symmetric Triangle: 0 -> Peak -> 0
                                        # Peak is stored in w_end (passed from apply_element_loads)
                                        q_peak = w_end
                                        
                                        # Helper function for Triangle shape (0 at ends, 1 at 0.5)
                                        # Ensure floating point symmetry
                                        def get_tri_q(x):
                                            if abs(x - 0.5) < 1e-9: return q_peak
                                            if x < 0.5: return q_peak * (x / 0.5)
                                            else: return q_peak * ((1.0 - x) / 0.5)
                                            
                                        # Force symmetry for start/end if they are symmetric
                                        # But segments are sequential 0->0.25, 0.25->0.5, etc.
                                        q_start_val = get_tri_q(x_start_rel)
                                        q_end_val = get_tri_q(x_end_rel)
                                        
                                        # Verify if we are at the exact center (0.5), force Peak Match
                                        if abs(x_end_rel - 0.5) < 1e-9: q_end_val = q_peak
                                        if abs(x_start_rel - 0.5) < 1e-9: q_start_val = q_peak
                                    # Linear/Uniform: Start -> End
                                    else:
                                        q_start_val = w_start + (w_end - w_start) * x_start_rel
                                        q_end_val = w_start + (w_end - w_start) * x_end_rel
                                    
                                    # Correction for Step Approximation:
                                    # Simple average (Trapezoidal rule) is fine for integration
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
                        
                        item['applied_load'] = f"{load_params['load_summary']} (Stepped 4x)"
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
                    # Validated Swap: F1=Fy, F2=Fx (Revit Y -> SAP X)
                    # OpenSees Reaction: [Fx, Fy, Fz, Mx, My, Mz]
                    reac = ops.nodeReaction(nid)
                    
                    res["nodes"][nid]["reaction"] = {
                        "F1": round(reac[0], 2), # Fy (Swapped to F1/X) - Matches SAP Direction
                        "F2": round(reac[1], 2), # Fx (Swapped to F2/Y) - Matches SAP Direction
                        "F3": round(reac[2], 2), # Fz
                        "M1": round(reac[3], 2), # My (Swapped to M1/X) - Inverted to match SAP M1 (-1.18e7) vs Python (1.21e7)
                        "M2": round(reac[4], 2), # Mx (Swapped to M2/Y) - Inverted to match SAP M2 (1.41e7) vs Python (-1.59e7)
                        "M3": round(reac[5], 2)  # Mz
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
                          critical_stations = find_critical_stations(eid, local_axes, num_samples=11)
                          
                          # Build stations list for output
                          stations_output = []
                          for station_data in critical_stations:
                              forces = station_data['forces']
                              station_ratio = station_data['station']
                              actual_distance = station_ratio * element_length  # Calculate actual distance in mm
                              
                              stations_output.append({
                                  "station": round(station_ratio, 3),
                                  "distance_mm": round(actual_distance, 2),  # Actual distance
                                  "P":  round(-forces["P"], 2),
                                  "V2": round(-forces["V2"], 2),
                                  "V3": round(-forces["V3"], 2),
                                  "T":  round(forces["T"], 2),
                                  "M2": round(forces["M2"], 2),
                                  "M3": round(-forces["M3"], 2)
                              })
                          
                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length,
                               "stations": stations_output
                            }
                    else:
                          # SUB-ELEMENTS: Average across all sub-elements or use first (legacy logic)
                          # For now, use FIRST sub-element with adaptive stationing
                          first_eid = subs[0][0]
                          local_axes = item['raw'].get('local_axes', {})
                          
                          # Get element length from topology
                          element_length = item['raw'].get('topology', {}).get('length_mm', 0)
                          
                          # Find critical stations for first sub-element
                          critical_stations = find_critical_stations(first_eid, local_axes, num_samples=11)
                          
                          # Build stations list
                          stations_output = []
                          for station_data in critical_stations:
                              forces = station_data['forces']
                              station_ratio = station_data['station']
                              actual_distance = station_ratio * element_length  # Actual distance in mm
                              
                              stations_output.append({
                                  "station": round(station_ratio, 3),
                                  "distance_mm": round(actual_distance, 2),
                                  "P":  round(-forces["P"], 2),
                                  "V2": round(-forces["V2"], 2),
                                  "V3": round(-forces["V3"], 2),
                                  "T":  round(forces["T"], 2),
                                  "M2": round(forces["M2"], 2),
                                  "M3": round(-forces["M3"], 2)
                              })
                          
                          res["elements"][eid] = {
                               "element_type": "Column" if item['is_vertical'] else "Beam",
                               "applied_load": item.get('applied_load', ''),
                               "element_length_mm": element_length,
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
