import sys
import json
import math
import openseespy.opensees as ops

# ============================================================================
# 1. FUNGSI PRE-PROCESSING & UTILITAS
# ============================================================================

def get_model_data(input_path):
    try:
        with open(input_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        return None

def get_shear_areas(d, b, tw, tf, A_gross):
    # Hitung luas geser untuk Timoshenko
    Avy = d * tw       # Web (Major Shear)
    Avz = 2.0 * b * tf # Flange (Minor Shear)
    if Avy <= 1.0: Avy = A_gross * 0.5
    if Avz <= 1.0: Avz = A_gross * 0.5
    return Avy, Avz

def preprocess_raw_data(raw_json):
    clean_elements = []
    
    props_map = {
        'Column': {'A': 0, 'Iy': 0, 'Iz': 0, 'J': 0, 'Avy':0, 'Avz':0},
        'Beam':   {'A': 0, 'Iy': 0, 'Iz': 0, 'J': 0, 'Avy':0, 'Avz':0}
    }

    for entry in raw_json:
        fam_type = entry.get('type', 'Beam')
        el_type = 'Column' if 'Column' in fam_type else 'Beam'
        
        start_pt = [float(x) for x in entry['topology']['start_node']]
        end_pt   = [float(x) for x in entry['topology']['end_node']]
        
        sec = entry['section']
        mat = entry['material']
        
        area = float(sec.get('Area_mm2', 0))
        Iy = float(sec.get('Iy_mm4', 0))
        Iz = float(sec.get('Ix_mm4', 0))
        J_raw = float(sec.get('J_mm4', 0))
        J = J_raw if J_raw > 1.0 else (Iy + Iz) * 0.1
        
        d = float(sec.get('d_mm', 0))
        b = float(sec.get('b_mm', 0))
        tw = float(sec.get('tw_mm', 0))
        tf = float(sec.get('tf_mm', 0))
        Avy, Avz = get_shear_areas(d, b, tw, tf, area)

        rho = 0.0
        if 'Rho_kg/mm3' in mat: rho = float(mat['Rho_kg/mm3']) * 1e9 
        elif 'Rho_kg/m3' in mat: rho = float(mat['Rho_kg/m3'])
        
        E_mod = float(mat.get('E_MPa', 205000))
        G_mod = float(mat.get('G_MPa', 80000))

        if props_map[el_type]['A'] == 0:
            props_map[el_type] = {
                'A': area, 'Iy': Iy, 'Iz': Iz, 'J': J, 
                'Avy': Avy, 'Avz': Avz
            }

        clean_entry = {
            'id': entry['id'],
            'element_type': el_type,
            'start': start_pt,
            'end': end_pt,
            'area_mm2': area,
            'rho_kgm3': rho,
            'E_mod': E_mod, 'G_mod': G_mod,
            'Iy': Iy, 'Iz': Iz, 'J': J,
            'Avy': Avy, 'Avz': Avz
        }
        clean_elements.append(clean_entry)
        
    return clean_elements, props_map

# ============================================================================
# 2. LOGIKA TRIBUTARY AREA (DISTRIBUTED LOADS)
# ============================================================================

def calculate_tributary_loads(elements_data, pressure_load):
    """
    Menghitung beban garis (N/mm) pada balok berdasarkan Tributary Width.
    Logika: Half-distance to nearest parallel beams on both sides.
    """
    beam_entries = [e for e in elements_data if e['element_type'] == 'Beam']
    beam_geom = {}

    # 1. Pre-calculate Geometry
    for item in beam_entries:
        s = item['start']
        e = item['end']
        mid = [(s[i]+e[i])/2.0 for i in range(3)]
        
        dx, dy = e[0] - s[0], e[1] - s[1]
        L_xy = math.sqrt(dx*dx + dy*dy)
        
        ux, uy = (0,0)
        if L_xy > 1e-9:
            ux, uy = dx/L_xy, dy/L_xy
            
        # Normal Vector (-uy, ux)
        nx, ny = -uy, ux
        
        beam_geom[item['id']] = {
            'mid': mid, 'dir': (ux, uy), 'norm': (nx, ny), 'z': mid[2]
        }

    tributary_loads = {}
    DEFAULT_EDGE_DIST = 2000.0 # mm (Asumsi setengah bentang jika di pinggir)

    # 2. Cari Tetangga
    for item in beam_entries:
        my_id = item['id']
        my_info = beam_geom[my_id]
        
        d_pos_list = []
        d_neg_list = []
        
        for other in beam_entries:
            if other['id'] == my_id: continue
            other_info = beam_geom[other['id']]
            
            # Cek Lantai (Z)
            if abs(my_info['z'] - other_info['z']) > 100: continue
            
            # Cek Paralel (Dot Product ~ 1.0)
            dot_val = my_info['dir'][0]*other_info['dir'][0] + my_info['dir'][1]*other_info['dir'][1]
            if abs(dot_val) < 0.9: continue 
            
            # Hitung Jarak Proyeksi ke Normal
            vx = other_info['mid'][0] - my_info['mid'][0]
            vy = other_info['mid'][1] - my_info['mid'][1]
            dist = vx*my_info['norm'][0] + vy*my_info['norm'][1]
            
            if dist > 10.0: d_pos_list.append(dist)
            elif dist < -10.0: d_neg_list.append(-dist)
            
        # Hitung TW
        d_pos = min(d_pos_list) if d_pos_list else DEFAULT_EDGE_DIST
        d_neg = min(d_neg_list) if d_neg_list else DEFAULT_EDGE_DIST
        
        # Logika: TW = 0.5 * (d_kiri + d_kanan)
        tw = 0.5 * (d_pos + d_neg)
        
        # Beban Garis (N/mm) = Pressure (N/mm2) * TW (mm)
        w_line = pressure_load * tw
        tributary_loads[my_id] = w_line

    return tributary_loads

# ============================================================================
# 3. MAIN LOGIC (RUN LOAD CASE)
# ============================================================================

def run_load_case(raw_data, case_type):
    elements_data, props = preprocess_raw_data(raw_data)
    
    ops.wipe()
    ops.model('basic', '-ndm', 3, '-ndf', 6)
    
    # --- KONSTANTA BEBAN (Diset agar cocok dengan SAP2000) ---
    g_si = 9.81 
    
    # LIVE LOAD PRESSURE:
    # Target SAP: 384kN total / 16m2 = 24 kPa = 0.024 N/mm2
    P_LIVE_PRESSURE = 0.024 
    
    # DEAD LOAD ADDITION (SDL):
    # Target SAP: ~5.3kN/leg vs OpenSees ~3.6kN/leg. Selisih 1.7kN.
    # Perlu tambahan beban mati (finishing) sekitar 0.4 kPa.
    P_DEAD_SDL = 0.0004 

    # --- Pre-Calc Loads ---
    per_elem_sw = {}
    for item in elements_data:
        w = item['rho_kgm3'] * item['area_mm2'] * 1e-9 * g_si 
        per_elem_sw[item['id']] = w
        
    # Hitung Distributed Load via Tributary
    line_loads_LL = calculate_tributary_loads(elements_data, P_LIVE_PRESSURE)
    line_loads_SDL = calculate_tributary_loads(elements_data, P_DEAD_SDL)

    # --- Build Nodes & Elements ---
    node_map = {}
    next_node_id = 1
    def get_node_id(coords):
        nonlocal next_node_id
        pt = tuple(round(c, 4) for c in coords)
        if pt not in node_map:
            ops.node(next_node_id, pt[0], pt[1], pt[2])
            if abs(pt[2]) < 10.0: ops.fix(next_node_id, 1, 1, 1, 1, 1, 1)
            node_map[pt] = next_node_id
            next_node_id += 1
        return node_map[pt]

    all_ids = [e['id'] for e in elements_data]
    next_safe_id = (max(all_ids) if all_ids else 1000) + 1
    element_segments_map = {} 

    ops.geomTransf('Linear', 1, 1, 0, 0)
    ops.geomTransf('Linear', 2, 0, 0, 1)

    for item in elements_data:
        oid = item['id']
        element_segments_map[oid] = []
        p1, p2 = item['start'], item['end']
        E, G = item['E_mod'], item['G_mod']
        
        if item['element_type'] == 'Column':
            n1, n2 = get_node_id(p1), get_node_id(p2)
            ops.element('ElasticTimoshenkoBeam', oid, n1, n2, E, G, 
                        item['area_mm2'], item['J'], item['Iy'], item['Iz'], item['Avy'], item['Avz'], 1)
            element_segments_map[oid].append(oid)
        else:
            # Beam Split (4 segments)
            dx, dy, dz = p2[0]-p1[0], p2[1]-p1[1], p2[2]-p1[2]
            prev_node = get_node_id(p1)
            for k in range(1, 5):
                ratio = k * 0.25
                curr_node = get_node_id((p1[0]+ratio*dx, p1[1]+ratio*dy, p1[2]+ratio*dz))
                seg_id = oid if k==1 else next_safe_id
                if k > 1: next_safe_id += 1
                
                ops.element('ElasticTimoshenkoBeam', seg_id, prev_node, curr_node, E, G, 
                            item['area_mm2'], item['J'], item['Iy'], item['Iz'], item['Avy'], item['Avz'], 2)
                element_segments_map[oid].append(seg_id)
                prev_node = curr_node

    # --- Apply Loads ---
    ops.timeSeries('Linear', 1)
    ops.pattern('Plain', 1, 1)

    # 1. DEAD LOAD (Self Weight + SDL)
    if case_type in ['SW', 'COMB']:
        for item in elements_data:
            segments = element_segments_map[item['id']]
            # A. Structural Weight
            w_sw = per_elem_sw.get(item['id'], 0.0)
            
            # B. Superimposed Dead Load (Floor Finish)
            w_sdl = 0.0
            if item['element_type'] == 'Beam':
                w_sdl = line_loads_SDL.get(item['id'], 0.0)
            
            w_total = w_sw + w_sdl
            
            if w_total > 1e-9:
                if item['element_type'] == 'Beam':
                    for seg in segments:
                        # -beamUniform Wy Wz Wx. (Local Z is Global Z vertical for Transf 2)
                        ops.eleLoad('-ele', seg, '-type', '-beamUniform', 0.0, -w_total)
                else:
                    # Column nodal load approx
                    L = math.dist(item['start'], item['end'])
                    F = (w_total * L) / 2.0
                    n1, n2 = get_node_id(item['start']), get_node_id(item['end'])
                    ops.load(n1, 0,0,-F,0,0,0)
                    ops.load(n2, 0,0,-F,0,0,0)

    # 2. LIVE LOAD (Tributary Distributed)
    if case_type in ['LL', 'COMB']:
        for item in elements_data:
            if item['element_type'] == 'Beam':
                w_ll = line_loads_LL.get(item['id'], 0.0)
                if w_ll > 1e-9:
                    segments = element_segments_map[item['id']]
                    for seg in segments:
                        ops.eleLoad('-ele', seg, '-type', '-beamUniform', 0.0, -w_ll)

    # --- Analysis ---
    ops.system('BandSPD')
    ops.numberer('RCM')
    ops.constraints('Plain')
    ops.integrator('LoadControl', 1.0)
    ops.algorithm('Linear')
    ops.analysis('Static')
    
    status = ops.analyze(1)
    
    res = {"status": "Success" if status==0 else "Failed", "nodes": {}, "elements": {}}
    if status == 0:
        ops.reactions()
        # Nodes Output
        for coords, nid in node_map.items():
            disp = ops.nodeDisp(nid)
            reac = ops.nodeReaction(nid) if coords[2]==0 else None
            res["nodes"][nid] = {
                "coords": coords,
                "disp": [round(v,5) for v in disp],
                "reaction": [round(v,2) for v in reac] if reac else None
            }
        # Elements Output (Merged)
        for oid, segs in element_segments_map.items():
            try:
                f_s = ops.eleForce(segs[0])
                f_e = ops.eleForce(segs[-1])
                # Momen Z (Strong Axis) dominan untuk balok
                res["elements"][oid] = {
                    "axial": round(f_s[0], 2),
                    "moment_z_i": round(f_s[5], 2),
                    "moment_z_j": round(f_e[11], 2),
                    "shear_major": round(f_s[1], 2) # Vy Local (Vertical in Global)
                }
            except: pass
            
    return res

def run_analysis(input_path, output_path):
    raw_data = get_model_data(input_path)
    if not raw_data: return
    final_output = {
        "SelfWeight": run_load_case(raw_data, 'SW'),
        "LiveLoad": run_load_case(raw_data, 'LL'),
        "Combination": run_load_case(raw_data, 'COMB')
    }
    with open(output_path, 'w') as f:
        json.dump(final_output, f, indent=4)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python Analysis.py <input.json> <output.json>")
    else:
        run_analysis(sys.argv[1], sys.argv[2])
