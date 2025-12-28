import sys
import json
import math
import openseespy.opensees as ops

# ============================================================================
# KONFIGURASI FISIKA
# ============================================================================
G_ACC = 9.81            
TOLERANCE_Z0 = 100.0    

def get_model_data(input_path):
    try:
        with open(input_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Reading JSON: {e}")
        return None

def get_section_properties(sec):
    # Dimensi
    d = float(sec.get('d_mm', 0))
    b = float(sec.get('b_mm', 0))
    tw = float(sec.get('tw_mm', 0))
    tf = float(sec.get('tf_mm', 0))
    A = float(sec.get('Area_mm2', 1000))
    
    # Inersia
    # Ix (JSON) = Strong Axis -> OpenSees Iz (Local Z)
    # Iy (JSON) = Weak Axis   -> OpenSees Iy (Local Y)
    Ix_json = float(sec.get('Ix_mm4', 10000)) 
    Iy_json = float(sec.get('Iy_mm4', 1000))
    
    Ops_Iz = Ix_json 
    Ops_Iy = Iy_json 
    
    # Torsi
    J_raw = float(sec.get('J_mm4', 0))
    J = J_raw if J_raw > 100.0 else (Ops_Iy + Ops_Iz) * 0.01

    # Shear Areas
    # Avy (Lateral) -> Flange
    # Avz (Vertical) -> Web
    Avy = 2.0 * b * tf  
    Avz = d * tw        
    
    if Avy <= 1.0: Avy = A * 0.5
    if Avz <= 1.0: Avz = A * 0.5
    
    return A, J, Ops_Iy, Ops_Iz, Avy, Avz

def run_load_case(elements_list, case_type):
    # Struktur Default Return
    res = {
        "status": "Failed",
        "nodes": {}, 
        "elements": {},
        "summary": {"total_reaction_z": 0}
    }

    try:
        # --- 1. SETUP MODEL ---
        ops.wipe()
        ops.model('basic', '-ndm', 3, '-ndf', 6)
        
        # --- 2. PRE-PROCESSING (SPLIT BEAM) ---
        node_map = {}       
        node_coords = {}    
        next_node_id = 1
        final_elements = [] 
        element_segments_map = {} 

        def get_or_create_node(coords):
            nonlocal next_node_id
            pt = tuple(round(c, 4) for c in coords)
            if pt not in node_map:
                node_map[pt] = next_node_id
                node_coords[next_node_id] = pt
                next_node_id += 1
            return node_map[pt]

        # Loop Elements & Build Geometry Memory
        for entry in elements_list:
            original_id = entry['id']
            p1 = entry['topology']['start_node']
            p2 = entry['topology']['end_node']
            fam_type = entry.get('type', 'Beam')
            element_segments_map[original_id] = []

            P_total = 0.0
            if case_type in ['LL', 'COMB'] and 'loads' in entry and entry['loads']:
                P_total = float(entry['loads'].get('point_load_N', 0.0))

            # Split Logic (Beam -> 4 Segmen)
            if 'Beam' in fam_type:
                dx = p2[0] - p1[0]
                dy = p2[1] - p1[1]
                dz = p2[2] - p1[2]
                
                nodes_temp = []
                for i in range(5):
                    ratio = i * 0.25
                    coord = (p1[0] + ratio*dx, p1[1] + ratio*dy, p1[2] + ratio*dz)
                    nodes_temp.append(get_or_create_node(coord))
                
                for i in range(4):
                    seg_id = original_id + (i * 1000000)
                    applied_load = P_total if i == 1 else 0.0
                    applied_node = nodes_temp[2] if i == 1 else None # Node tengah
                    
                    seg_data = {
                        'id': seg_id, 
                        'nodes': [nodes_temp[i], nodes_temp[i+1]], 
                        'type': fam_type, 
                        'raw': entry, 
                        'load_val': applied_load,
                        'load_node': applied_node
                    }
                    final_elements.append(seg_data)
                    element_segments_map[original_id].append(seg_id)
            else:
                # Kolom Utuh
                n1 = get_or_create_node(p1)
                n2 = get_or_create_node(p2)
                seg_data = {
                    'id': original_id,
                    'nodes': [n1, n2],
                    'type': fam_type, 
                    'raw': entry, 
                    'load_val': 0, 
                    'load_node': None
                }
                final_elements.append(seg_data)
                element_segments_map[original_id].append(original_id)

        # --- [CRITICAL STEP] POPULATE NODES JSON BEFORE ANALYSIS ---
        # Ini menjamin 'coords' selalu ada meskipun analisis gagal
        all_z = []
        for nid, coords in node_coords.items():
            all_z.append(coords[2])
            res["nodes"][nid] = {
                "coords": coords,
                "disp": [0.0]*6, # Default 0
                "reaction": None
            }
        
        # --- 3. BUILD OPENSEES MODEL ---
        if not node_coords: return res
        min_z = min(all_z) if all_z else 0.0
        fixed_nodes = set()

        # Create Nodes & Fixity
        for nid, (x, y, z) in node_coords.items():
            ops.node(nid, x, y, z)
            if abs(z - min_z) < TOLERANCE_Z0:
                ops.fix(nid, 1, 1, 1, 1, 1, 1)
                fixed_nodes.add(nid)

        # Transformasi
        ops.geomTransf('Linear', 1, 1, 0, 0) # Col (Local Z = Global X)
        ops.geomTransf('Linear', 2, 0, 0, 1) # Beam (Local Z = Global Z)

        # Create Elements
        for item in final_elements:
            el_id = item['id']
            nodes = item['nodes']
            raw = item['raw']
            mat = raw['material']
            sec = raw['section']
            
            E = float(mat.get('E_MPa', 205000))
            G_mod = float(mat.get('G_MPa', 80000))
            A, J, Ops_Iy, Ops_Iz, Avy, Avz = get_section_properties(sec)
            
            transf_tag = 1 if 'Column' in item['type'] else 2
            
            ops.element('ElasticTimoshenkoBeam', el_id, nodes[0], nodes[1], 
                        E, G_mod, A, J, Ops_Iy, Ops_Iz, Avy, Avz, transf_tag)

        # --- 4. LOADS ---
        ops.timeSeries('Linear', 1)
        ops.pattern('Plain', 1, 1)

        # Self Weight
        if case_type in ['SW', 'COMB']:
            for item in final_elements:
                raw = item['raw']
                mat = raw['material']
                sec = raw['section']
                
                rho = float(mat.get('Rho_kg/mm3', 0))
                if rho == 0 and 'Rho_kg/m3' in mat: rho = float(mat['Rho_kg/m3']) * 1e-9
                
                A_curr = float(sec.get('Area_mm2', 0))
                w_grav = rho * A_curr * G_ACC 
                
                if w_grav > 1e-12:
                    n_i, n_j = item['nodes']
                    xi, yi, zi = node_coords[n_i]
                    xj, yj, zj = node_coords[n_j]
                    L = math.sqrt((xj-xi)**2 + (yj-yi)**2 + (zj-zi)**2)
                    F_node = (w_grav * L) / 2.0
                    ops.load(n_i, 0.0, 0.0, -F_node, 0.0, 0.0, 0.0)
                    ops.load(n_j, 0.0, 0.0, -F_node, 0.0, 0.0, 0.0)

        # Live Load
        if case_type in ['LL', 'COMB']:
            for item in final_elements:
                P = item.get('load_val', 0.0)
                nid = item.get('load_node')
                if P > 0 and nid is not None:
                    ops.load(nid, 0.0, 0.0, -P, 0.0, 0.0, 0.0)

        # --- 5. ANALYZE ---
        ops.system('BandGeneral') 
        ops.numberer('RCM')
        ops.constraints('Transformation') 
        ops.integrator('LoadControl', 1.0)
        ops.algorithm('Linear')
        ops.analysis('Static')
        
        status = ops.analyze(1)
        res["status"] = "Success" if status == 0 else "Failed"
        
        # --- 6. EXTRACT RESULTS (ONLY IF SUCCESS) ---
        if status == 0:
            ops.reactions()
            total_rz = 0.0
            
            # Update Nodes Data
            for nid in node_coords:
                d = ops.nodeDisp(nid)
                res["nodes"][nid]["disp"] = [round(v, 4) for v in d]
                
                if nid in fixed_nodes:
                    reac = ops.nodeReaction(nid)
                    total_rz += reac[2]
                    res["nodes"][nid]["reaction"] = [round(val, 2) for val in reac]

            res["summary"]["total_reaction_z"] = round(total_rz, 2)

            # Process Elements (Scanning Segments)
            for original_id, segments in element_segments_map.items():
                try:
                    max_M_abs = 0.0
                    
                    # Force data ujung
                    f_start = ops.eleForce(segments[0]) 
                    f_end = ops.eleForce(segments[-1])
                    
                    # Scanning Max Moment
                    for seg_id in segments:
                        forces = ops.eleForce(seg_id)
                        # Check My & Mz
                        for idx in [4, 5, 10, 11]:
                            if abs(forces[idx]) > max_M_abs: 
                                max_M_abs = abs(forces[idx])

                    res["elements"][original_id] = {
                        "axial": round(f_start[0], 2),
                        "shear_v": round(f_start[1], 2),
                        "moment_i": round(f_start[5], 2) if abs(f_start[5]) > abs(f_start[4]) else round(f_start[4], 2),
                        "moment_j": round(f_end[11], 2) if abs(f_end[11]) > abs(f_end[10]) else round(f_end[10], 2),
                        "moment_max": round(max_M_abs, 2)
                    }
                except:
                    res["elements"][original_id] = {"error": "NoData"}
        
    except Exception as e:
        print(f"[ERROR] Case {case_type}: {str(e)}")
        res["status"] = "Error"
        res["error_msg"] = str(e)

    return res

def print_styled_report(final_output):
    print("\n" + "="*80)
    print(f"{'LAPORAN ANALISIS STRUKTUR':^80}")
    print("="*80)

    case_map = {
        "SelfWeight": "BEBAN MATI (SW)",
        "LiveLoad": "BEBAN HIDUP (LL)",
        "Combination": "KOMBINASI (1.0D + 1.0L)"
    }

    for key, title in case_map.items():
        data = final_output.get(key)
        if not data: continue
        
        print(f"\n[{title}]")
        if data['status'] != 'Success':
            print(f"  Status: {data['status']} - {data.get('error_msg', '')}")
            continue

        print(f"  Total Reaksi Vertikal: {data['summary']['total_reaction_z']} N")
        print("-" * 80)
        print(f"  {'Elem ID':<10} | {'Axial (N)':<12} | {'Momen I':<12} | {'Momen J':<12} | {'MAX MOMEN':<12}")
        print("-" * 80)
        
        count = 0
        sorted_keys = sorted([k for k in data['elements'].keys() if isinstance(data['elements'][k], dict)])
        for eid in sorted_keys:
            vals = data['elements'][eid]
            if 'error' not in vals:
                print(f"  {str(eid):<10} | {vals['axial']:<12} | {vals['moment_i']:<12} | {vals['moment_j']:<12} | {vals['moment_max']:<12}")
            else:
                print(f"  {str(eid):<10} | ERROR")
            
            count += 1
            if count >= 10: 
                print("  ... (sisa elemen disembunyikan)")
                break
    print("\n" + "="*80)

def run_analysis(input_path, output_path):
    root_data = get_model_data(input_path)
    if not root_data: 
        # Tulis JSON kosong valid agar reader tidak error fatal
        with open(output_path, 'w') as f: json.dump({"error": "File Read Error"}, f)
        return

    elements_list = root_data.get('model_elements', [])
    
    results = {
        "SelfWeight": run_load_case(elements_list, 'SW'),
        "LiveLoad": run_load_case(elements_list, 'LL'),
        "Combination": run_load_case(elements_list, 'COMB')
    }

    with open(output_path, 'w') as f:
        json.dump(results, f, indent=4)

    print_styled_report(results)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        run_analysis("Model data.json", "Output_Analysis.json")
    else:
        run_analysis(sys.argv[1], sys.argv[2])
