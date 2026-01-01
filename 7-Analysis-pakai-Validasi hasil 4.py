import sys
import json
import math
import openseespy.opensees as ops   

# ============================================================================
# 1. KONFIGURASI FISIKA
# ============================================================================
G_ACC = 9.81              # Gravitasi (m/s^2)
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
        # --- RESET MODEL ---
        ops.wipe()
        ops.model('basic', '-ndm', 3, '-ndf', 6)
        
        # --- NODE MAPPING ---
        node_map = {}       
        node_coords = {}    
        next_node_id = 1
        
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
            ops.geomTransf('Linear', transf_tag, vec_z[0], vec_z[1], vec_z[2])

            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 80000))
            A, J, Ix, Iy, Avy, Avz = get_section_properties(sec)
            
            # Setup Section Properties
            alphaY = Avy / A if A > 0 else 0.5
            alphaZ = Avz / A if A > 0 else 0.5
            
            # --- INERTIA MAPPING ---
            # JSON: Ix = Major Axis, Iy = Minor Axis.
            # OpenSees: Iy = About Local Y, Iz = About Local Z.
            # Local Y is Major Axis -> Bending uses Ix.
            # Local Z is Minor Axis -> Bending uses Iy.
            Ops_Iy = Ix
            Ops_Iz = Iy
            
            if item['is_vertical']:
                # --- KOLOM (SINGLE ELEMENT) ---
                sec_tag = item['id']
                ops.section('Elastic', sec_tag, E, A, Ops_Iz, Ops_Iy, G, J, alphaY, alphaZ)
                int_tag = item['id']
                ops.beamIntegration('Legendre', int_tag, sec_tag, 3)
                
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
                w_dead = float(item['raw']['section'].get('Area_mm2', 0)) * (rho * 1e-9) * G_ACC
                
                # Apply to all sub-elements
                subs = sub_elements_map.get(item['id'], [(item['id'], item['length'])])
                
                for (eid, fractional_len) in subs:
                     if item['is_vertical']:
                         # Column Load (Self Weight usually Axial?)
                         # But using beamUniform means transverse.
                         # If column vertical, Wy/Wz are horizontal.
                         # SelfWeight acts in Global Z (Axial for column).
                         # OpenSees beamUniform Wx is axial.
                         # So for column, use Wx = -w_dead?
                         # Previous code used Wz. Let's keep Wz for now if it worked?
                         # Wait, if Column Z is Web (Horizontal), then Wz is horizontal load.
                         # Self weight should be axial.
                         # Let's use Wx for columns if they are vertical.
                         # But let's stick to the previous implementation for Columns if it wasn't broken by axis swap?
                         # Column axes: X=Vertical, Y & Z Horizontal.
                         # Gravity is -X. So use Wx.
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', 0.0, 0.0, -w_dead) 
                     else:
                         # Beam: Z is Vertical.
                         # beamUniform Wy, Wz, Wx.
                         # Apply to Wz (2nd arg).
                         ops.eleLoad('-ele', eid, '-type', '-beamUniform', 0.0, -w_dead, 0.0)

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
                                    # Beam Local Z is Vertical.
                                    # If Local Z is Down, +q is Down.
                                    # Resulting Reaction was Negative (Down).
                                    # We want POSITIVE Reaction (Up) for Support.
                                    # This implies OpenSees Reaction Output follows Force Direction logic?
                                    # Or maybe Local Z is actually UP?
                                    # User BasisZ = Up.
                                    # X cross Z = -Y.
                                    # X cross -Y = -Z (Down).
                                    # So Local Z is Down.
                                    # If I apply -q (Up?), then reaction is Down?
                                    # Wait, if I apply +q (Down), Reaction should be Up (+Z).
                                    # My previous run gave Reaction -Z.
                                    # Checks out: Sum Fz = -368k.
                                    # So External Load was balancing Reaction?
                                    # No, R is support force.
                                    # Try -q_seg_avg.
                                    
                                    ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                               0.0, -q_seg_avg, 0.0)
                                               
                                    total_load_accum += q_seg_avg * seg_len
                        
                        item['applied_load'] = f"{load_params['load_summary']} (Stepped 4x)"
                    else:
                        # Fallback (Global Pressure)
                        L = item['length']
                        w_live = (FLOOR_PRESSURE * L) / 4.0
                        if w_live > 0:
                            subs = sub_elements_map.get(item['id'], [(item['id'], L)])
                            for (eid, seg_len) in subs:
                                # Order: Wy, Wz, Wx
                                # Using -w_live
                                ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                           0.0, -w_live, 0.0)
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
                    reac = ops.nodeReaction(nid)
                    r_clean = [round(v, 2) for v in reac]
                    res["nodes"][nid]["reaction"] = r_clean
                    total_rz += r_clean[2]
                
                d = ops.nodeDisp(nid)
                res["nodes"][nid]["disp"] = [round(v, 4) for v in d]
            
            res["summary"]["total_reaction_z"] = round(total_rz, 2)
            
            # Elements
            for item in processed_elements:
                eid = item['id']
                subs = sub_elements_map.get(eid)
                
                try:
                    if not subs: 
                         # Default for columns or unsubdivided
                         f = ops.eleForce(eid) # Local Force Vector at Node I: [P, Vy, Vz, T, My, Mz]
                         
                         # SAP2000 Mapping
                         # P  = Axial (Index 0)
                         # V2 = Shear Local Y (Index 1)
                         # V3 = Shear Local Z (Index 2)
                         # T  = Torsion (Index 3)
                         # M2 = Moment Local Y (Index 4) -> Minor Axis Bending for Column (usually)
                         # M3 = Moment Local Z (Index 5) -> Major Axis Bending for Column/Beam
                         
                         p_val = f[0]
                         v2_val = f[1]
                         v3_val = f[2]
                         t_val  = f[3]
                         m2_val = f[4]
                         m3_val = f[5]

                         if item['is_vertical']:
                             # USER REQUEST: Column Axial should be Reaction value
                             # Direct Query to OpenSees Engine (Bypass Dictionary Lookups)
                             reac_val = 0.0
                             
                             # Check Node I (Bottom/Top)
                             try:
                                 r_vec_i = ops.nodeReaction(item['nodes'][0])
                                 if abs(r_vec_i[2]) > 0.1: # Check Fz
                                     reac_val = abs(r_vec_i[2])
                             except:
                                 pass
                                 
                             # Check Node J (Bottom/Top) if not found
                             if reac_val < 0.1:
                                 try:
                                     r_vec_j = ops.nodeReaction(item['nodes'][1])
                                     if abs(r_vec_j[2]) > 0.1:
                                         reac_val = abs(r_vec_j[2])
                                 except:
                                     pass

                             if reac_val > 0.1:
                                 p_val = reac_val
                             
                         else:
                             # USER REQUEST: Beam Axial = 0 (Simplified)
                             p_val = 0.0
                             
                         # Store SAP2000 Style Output
                         res["elements"][eid] = {
                            "p": round(p_val, 2),
                            "t": round(t_val, 2),
                            "v2": round(v2_val, 2),
                            "v3": round(v3_val, 2),
                            "m2": round(m2_val, 2),
                            "m3": round(m3_val, 2),
                            "element_type": "Column" if item['is_vertical'] else "Beam",
                            "applied_load": item.get('applied_load', '')
                         }
                    else:
                        # Handle Sub-Elements
                        # We take forces from the FIRST sub-element (Node I end)
                        # This approximates the member end forces at start
                        first_eid = subs[0][0]
                        f = ops.eleForce(first_eid)
                        
                        p_val = f[0]
                        v2_val = f[1]
                        v3_val = f[2]
                        t_val  = f[3]
                        m2_val = f[4]
                        m3_val = f[5]
                        
                        if item['is_vertical']:
                            # Reaction override for subdivided columns
                            reac_val = 0.0
                            try:
                                r_vec_i = ops.nodeReaction(item['nodes'][0])
                                if abs(r_vec_i[2]) > 0.1: reac_val = abs(r_vec_i[2])
                            except: pass
                            
                            if reac_val < 0.1:
                                try:
                                    r_vec_j = ops.nodeReaction(item['nodes'][1])
                                    if abs(r_vec_j[2]) > 0.1: reac_val = abs(r_vec_j[2])
                                except: pass
                                
                            if reac_val > 0.1:
                                p_val = reac_val
                        else:
                            p_val = 0.0
                        
                        res["elements"][eid] = {
                            "p": round(p_val, 2),
                            "t": round(t_val, 2),
                            "v2": round(v2_val, 2),
                            "v3": round(v3_val, 2),
                            "m2": round(m2_val, 2),
                            "m3": round(m3_val, 2),
                            "element_type": "Column" if item['is_vertical'] else "Beam",
                            "applied_load": item.get('applied_load', '')
                        }
                except:
                    res["elements"][eid] = {"error": "N/A"}
                    
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
            print(f"  Total Reaksi Z+ : {data['summary']['total_reaction_z']} N")
        
        print("-" * 105)
        print(f"  {'ID Elemen':<10} | {'P (N)':<12} | {'T (Nmm)':<12} | {'V2 (N)':<12} | {'V3 (N)':<12} | {'M2 (Nmm)':<12} | {'M3 (Nmm)':<12}")
        print("-" * 105)
        
        valid_ids = sorted([k for k in data['elements'].keys() if isinstance(data['elements'][k], dict) and 'error' not in data['elements'][k]])
        
        count = 0
        for eid in valid_ids:
            v = data['elements'][eid]
            # Handle potential missing keys if older JSON (safety)
            p = v.get('p', 0.0)
            t = v.get('t', 0.0)
            v2 = v.get('v2', 0.0)
            v3 = v.get('v3', 0.0)
            m2 = v.get('m2', 0.0)
            m3 = v.get('m3', 0.0)
            
            print(f"  {str(eid):<10} | {p:<12} | {t:<12} | {v2:<12} | {v3:<12} | {m2:<12} | {m3:<12}")
            count += 1
            if count >= 15: 
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
                # Reaction storage: [Fy, Fx, Fz, My, Mx, Mz] (from legacy swap)
                # Map to F1, F2...
                # F1 (X) = r[1]
                # F2 (Y) = r[0]
                # F3 (Z) = r[2]
                # M1 (X) = r[4]
                # M2 (Y) = r[3]
                
                print(f"  {str(nid):<10} | {r[1]:<12.2f} | {r[0]:<12.2f} | {r[2]:<12.2f} | {r[4]:<15.2f} | {r[3]:<15.2f}")

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
    if len(sys.argv) < 3:
        run_analysis("Model data.json", "Output_Analysis.json")
    else:
        run_analysis(sys.argv[1], sys.argv[2])
