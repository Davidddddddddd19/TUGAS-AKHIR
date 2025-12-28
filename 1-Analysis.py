import sys
import json
import math
import openseespy.opensees as ops

# ============================================================================
# KONFIGURASI FISIKA
# ============================================================================
G_ACC = 9.81            
TOLERANCE_Z0 = 10.0     
STIFFNESS_FACTOR = 1.0 

def get_model_data(input_path):
    try:
        with open(input_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        return None

def get_shear_areas(sec):
    d = float(sec.get('d_mm', 0))
    b = float(sec.get('b_mm', 0))
    tw = float(sec.get('tw_mm', 0))
    tf = float(sec.get('tf_mm', 0))
    A_gross = float(sec.get('Area_mm2', 0))
    
    Avy = d * tw       
    Avz = 2.0 * b * tf 
    
    if Avy <= 1.0: Avy = A_gross * 0.5
    if Avz <= 1.0: Avz = A_gross * 0.5
    return Avy, Avz

def run_load_case(elements_data, case_type):
    # --- 1. SETUP MODEL ---
    ops.wipe()
    ops.model('basic', '-ndm', 3, '-ndf', 6)
    
    # --- 2. PRE-PROCESSING: SPLIT ELEMEN ---
    node_map = {}       
    node_coords = {}    
    next_node_id = 1
    final_elements = [] 
    column_node_coords = set()
    element_segments_map = {} 

    def get_or_create_node(coords):
        nonlocal next_node_id
        pt = tuple(round(c, 4) for c in coords)
        if pt not in node_map:
            node_map[pt] = next_node_id
            node_coords[next_node_id] = pt
            next_node_id += 1
        return node_map[pt]

    for entry in elements_data:
        original_id = entry['id']
        p1 = entry['topology']['start_node']
        p2 = entry['topology']['end_node']
        fam_type = entry.get('type', 'Beam')
        element_segments_map[original_id] = []

        # Ambil data Live Load (jika ada)
        P_total = 0.0
        if case_type in ['LL', 'COMB'] and 'loads' in entry and entry['loads']:
            P_total = float(entry['loads'].get('point_load_N', 0.0))

        # --- PERBAIKAN LOGIKA DISINI ---
        # KITA SELALU SPLIT BALOK (BEAM) MENJADI 4 SEGMEN
        # Agar Self-Weight terdistribusi di tengah bentang dan menciptakan momen.
        
        if 'Beam' in fam_type:
            dx = p2[0] - p1[0]
            dy = p2[1] - p1[1]
            dz = p2[2] - p1[2]
            
            # Buat Node Internal (0%, 25%, 50%, 75%, 100%)
            nodes = []
            for i in range(5):
                ratio = i * 0.25
                coord = (p1[0] + ratio*dx, p1[1] + ratio*dy, p1[2] + ratio*dz)
                nodes.append(get_or_create_node(coord))
            
            # Distribusi Beban Live Load (jika ada)
            loads = [0, 0.25*P_total, 0.50*P_total, 0.25*P_total] 
            
            # Buat 4 Elemen Kecil
            for i in range(4):
                seg_id = original_id + (i * 100000) # ID unik
                
                load_val = loads[i] 
                load_node = nodes[i] if load_val > 0 else None
                
                seg_data = {
                    'id': seg_id, 
                    'nodes': [nodes[i], nodes[i+1]], 
                    'type': 'Beam', 
                    'raw': entry, 
                    'load_node': load_node, 
                    'load_val': load_val,
                    'parent_id': original_id
                }
                final_elements.append(seg_data)
                element_segments_map[original_id].append(seg_id)
            
        else:
            # KOLOM TIDAK DI-SPLIT
            column_node_coords.add(tuple(round(c, 4) for c in p1))
            column_node_coords.add(tuple(round(c, 4) for c in p2))
            
            n1 = get_or_create_node(p1)
            n2 = get_or_create_node(p2)
            
            seg_data = {
                'id': original_id,
                'nodes': [n1, n2],
                'type': fam_type, 
                'raw': entry, 
                'load_node': None, 
                'load_val': 0,
                'parent_id': original_id
            }
            final_elements.append(seg_data)
            element_segments_map[original_id].append(original_id)

    # --- 3. NODE & FIXITY ---
    all_z = [c[2] for c in node_coords.values()]
    min_z = min(all_z) if all_z else 0.0
    fixed_nodes = set()

    for nid, (x, y, z) in node_coords.items():
        ops.node(nid, x, y, z)
        is_col_node = (x, y, z) in column_node_coords
        if abs(z - min_z) < TOLERANCE_Z0 and is_col_node:
            ops.fix(nid, 1, 1, 1, 1, 1, 1)
            fixed_nodes.add(nid)

    # --- 4. TRANSFORMASI ---
    ops.geomTransf('Linear', 1, 1, 0, 0) # Kolom (Strong Axis X)
    ops.geomTransf('Linear', 2, 0, 0, 1) # Balok (Normal)

    # --- 5. ELEMEN ---
    for item in final_elements:
        el_id = item['id']
        nodes = item['nodes']
        raw = item['raw']
        sec = raw['section']
        mat = raw['material']
        
        A = float(sec.get('Area_mm2', 1000)) * STIFFNESS_FACTOR
        E = float(mat.get('E_MPa', 205000))
        G_mod = float(mat.get('G_MPa', 80000))
        Iy_val = float(sec.get('Iy_mm4', 10000)) * STIFFNESS_FACTOR 
        Iz_val = float(sec.get('Ix_mm4', 100000)) * STIFFNESS_FACTOR 
        J_raw = float(sec.get('J_mm4', 0))
        J = J_raw if J_raw > 1.0 else (Iy_val + Iz_val) * 0.1
        Avy, Avz = get_shear_areas(sec) 
        
        transf_tag = 1 if 'Column' in item['type'] else 2
        
        try:
            ops.element('ElasticTimoshenkoBeam', el_id, nodes[0], nodes[1], 
                        E, G_mod, A, J, Iy_val, Iz_val, Avy, Avz, transf_tag)
        except:
            ops.element('elasticBeamColumn', el_id, nodes[0], nodes[1], 
                        A, E, G_mod, J, Iy_val, Iz_val, transf_tag)

    # --- 6. PEMBEBANAN ---
    ops.timeSeries('Linear', 1)
    ops.pattern('Plain', 1, 1)

    # A. SELF WEIGHT (Diaplikasikan ke tiap node split)
    if case_type in ['SW', 'COMB']:
        for item in final_elements:
            raw = item['raw']
            mat = raw['material']
            sec = raw['section']
            rho = float(mat.get('Rho_kg/mm3', 0))
            if rho == 0 and 'Rho_kg/m3' in mat: rho = float(mat['Rho_kg/m3']) * 1e-9
            
            A_curr = float(sec.get('Area_mm2', 0))
            w_grav = rho * A_curr * G_ACC 
            
            if w_grav > 1e-9:
                node_i, node_j = item['nodes']
                xi, yi, zi = node_coords[node_i]
                xj, yj, zj = node_coords[node_j]
                L_actual = math.sqrt((xj-xi)**2 + (yj-yi)**2 + (zj-zi)**2)
                
                # Beban node ekuivalen
                F_node = (w_grav * L_actual) / 2.0
                ops.load(node_i, 0.0, 0.0, -F_node, 0.0, 0.0, 0.0)
                ops.load(node_j, 0.0, 0.0, -F_node, 0.0, 0.0, 0.0)

    # B. LIVE LOAD
    if case_type in ['LL', 'COMB']:
        for item in final_elements:
            P = item.get('load_val', 0.0)
            nid = item.get('load_node')
            if P > 0 and nid is not None:
                ops.load(nid, 0.0, 0.0, -P, 0.0, 0.0, 0.0)

    # --- 7. ANALISIS ---
    ops.system('BandSPD')
    ops.numberer('RCM')
    ops.constraints('Plain')
    ops.integrator('LoadControl', 1.0)
    ops.algorithm('Linear')
    ops.analysis('Static')
    
    status = ops.analyze(1)
    
    # --- 8. OUTPUT (MERGE SEGMEN KEMBALI) ---
    res = {"status": "Success", "nodes": {}, "elements": {}}
    
    if status == 0:
        ops.reactions()
        # Nodes
        for nid in node_coords:
            d = ops.nodeDisp(nid)
            r = None
            if nid in fixed_nodes:
                reac = ops.nodeReaction(nid)
                r = [round(val, 2) if abs(val)>0.1 else 0.0 for val in reac]
            
            res["nodes"][nid] = {
                "coords": node_coords[nid],
                "disp": [round(v, 5) for v in d],
                "reaction": r
            }
        
        # Elements - Gabung Segmen
        for original_id, segments in element_segments_map.items():
            try:
                # Ambil segmen ujung kiri dan kanan
                first_seg_id = segments[0]
                last_seg_id = segments[-1]
                
                f_start = ops.eleForce(first_seg_id) # Node i
                f_end = ops.eleForce(last_seg_id)    # Node j
                
                # Logika Axis: Pilih momen terbesar (Y atau Z)
                mz_i, my_i = f_start[5], f_start[4]
                final_m_i = mz_i if abs(mz_i) >= abs(my_i) else my_i
                
                mz_j, my_j = f_end[11], f_end[10]
                final_m_j = mz_j if abs(mz_j) >= abs(my_j) else my_j
                
                axial = f_start[0]

                res["elements"][original_id] = {
                    "axial": round(axial, 2),
                    "moment_z_i": round(final_m_i, 2),
                    "moment_z_j": round(final_m_j, 2),
                    "shear_major": round(f_start[1], 2)
                }
            except:
                res["elements"][original_id] = {
                    "axial": 0.0, "moment_z_i": 0.0, "moment_z_j": 0.0
                }

    return res

def run_analysis(input_path, output_path):
    elements_data = get_model_data(input_path)
    if not elements_data: return

    final_output = {
        "SelfWeight": run_load_case(elements_data, 'SW'),
        "LiveLoad": run_load_case(elements_data, 'LL'),
        "Combination": run_load_case(elements_data, 'COMB')
    }

    with open(output_path, 'w') as f:
        json.dump(final_output, f, indent=4)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python Analysis.py <input.json> <output.json>")
    else:
        run_analysis(sys.argv[1], sys.argv[2])
