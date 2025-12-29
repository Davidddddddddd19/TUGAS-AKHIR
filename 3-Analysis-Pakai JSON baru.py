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
    
    if q_peak > 0:
        # Konversi dari N/mm ke beban merata
        if load_shape == 'Triangle':
            # Beban segitiga: mulai dari 0, puncak di tengah/ujung
            # q_peak adalah nilai maksimum, NOT averaged
            result['distributed_loads'].append((0.0, q_peak, 'Y'))
            result['load_summary'] = f"Triangle: 0->{q_peak:.2f}N/mm"
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
        # Tag 1 (Kolom): Local Z aligns Global X
        ops.geomTransf('Linear', 1, 1, 0, 0)
        # Tag 2 (Balok): Local Z aligns Global Z
        ops.geomTransf('Linear', 2, 0, 0, 1) 

        for item in processed_elements:
            sec = item['raw']['section']
            mat = item['raw']['material']
            
            E = float(mat.get('E_MPa', 205000))
            G = float(mat.get('G_MPa', 80000))
            A, J, Ix, Iy, Avy, Avz = get_section_properties(sec)
            
            # Mapping Stiffness Matriks
            if item['is_vertical']:
                transf_tag = 1
                # Kolom: Iz=Strong(Ix), Iy=Weak(Iy)
                Ops_Iz = Ix
                Ops_Iy = Iy
            else:
                transf_tag = 2
                # Balok: Iy=Strong(Ix) -> menahan gravitasi
                Ops_Iy = Ix 
                Ops_Iz = Iy 
            
            ops.element('ElasticTimoshenkoBeam', item['id'], item['nodes'][0], item['nodes'][1], 
                        E, G, A, J, Ops_Iy, Ops_Iz, Avy, Avz, transf_tag)

        # --- LOAD APPLICATION ---
        ops.timeSeries('Linear', 1)
        ops.pattern('Plain', 1, 1)

        # A. SELF WEIGHT
        if case_type in ['SW', 'COMB']:
            for item in processed_elements:
                mat = item['raw']['material']
                rho = float(mat.get('Rho_kg/m3', 0))
                if rho == 0: rho = float(mat.get('Rho_kg/mm3', 0)) * 1e9
                
                # Berat per mm (N/mm)
                w_dead = float(item['raw']['section'].get('Area_mm2', 0)) * (rho * 1e-9) * G_ACC
                
                if item['is_vertical']:
                    ops.eleLoad('-ele', item['id'], '-type', '-beamUniform', 0.0, 0.0, -w_dead)
                else:
                    ops.eleLoad('-ele', item['id'], '-type', '-beamUniform', 0.0, -w_dead, 0.0)

        # B. FLOOR PRESSURE (LIVE LOAD)
        if case_type in ['LL', 'COMB']:
            for item in processed_elements:
                # Hanya Balok Horizontal yang menerima beban lantai
                if not item['is_vertical']: 
                    # Parse element-specific loads
                    load_params = apply_element_loads(item['raw'], case_type)
                    
                    if load_params['has_loads']:
                        # ---- METODE 1: Gunakan Data Loads dari JSON ----
                        eid = item['id']
                        L = item['length']
                        total_applied_load = 0.0
                        
                        # Apply Point Loads (Convert to equivalent uniform for Timoshenko)
                        for (loc_ratio, force, direction) in load_params['point_loads']:
                            if direction == 'Y':
                                # ElasticTimoshenkoBeam tidak support beamPoint
                                # Konversi point load ke ekuivalen uniform distributed load
                                # w_equiv = P / L (distribusi merata ekuivalen)
                                w_equiv = force / L
                                ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                           0.0, -w_equiv, 0.0)
                                total_applied_load += force
                        
                        # Apply Distributed Loads
                        for (w_start, w_end, direction) in load_params['distributed_loads']:
                            if direction == 'Y':
                                # Distributed load sepanjang balok
                                if w_start == w_end:
                                    # Uniform load
                                    ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                               0.0, -w_start, 0.0)
                                    total_applied_load += w_start * L
                                else:
                                    # Linearly varying load (triangle, trapezoid)
                                    # NOTE: ElasticTimoshenkoBeam DOES NOT support -beamLinear
                                    # Fallback: Use averaged uniform load
                                    w_avg = (w_start + w_end) / 2.0
                                    ops.eleLoad('-ele', eid, '-type', '-beamUniform', 
                                               0.0, -w_avg, 0.0)
                                    total_applied_load += w_avg * L
                        
                        # Store load info untuk reporting
                        item['applied_load'] = f"{load_params['load_summary']} (Total: {total_applied_load/1000:.1f}kN)"
                    else:
                        # ---- METODE 2: Fallback ke Global Pressure (Original) ----
                        L = item['length']
                        # Ekuivalen Beban Merata dari Amplop 2 Arah
                        w_live = (FLOOR_PRESSURE * L) / 4.0
                        
                        if w_live > 0:
                            ops.eleLoad('-ele', item['id'], '-type', '-beamUniform', 
                                       0.0, -w_live, 0.0)
                        
                        item['applied_load'] = f"Global Pressure: {w_live:.2f}N/mm"

        # --- SOLVE ---
        ops.system('BandGeneral') 
        ops.numberer('RCM')
        ops.constraints('Transformation') 
        ops.integrator('LoadControl', 1.0)
        ops.algorithm('Linear')
        ops.analysis('Static')
        
        status = ops.analyze(1)
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
            
            #Elements
            for item in processed_elements:
                eid = item['id']
                try:
                    f = ops.eleForce(eid)
                    
                    if item['is_vertical']:
                        # Kolom
                        axial = f[0]
                        shear = max(abs(f[1]), abs(f[2]))
                        # Momen kolom: f[3]=Mx-i, f[4]=My-i, f[5]=Mz-i
                        # Ambil momen dominan (biasanya My atau Mz)
                        mi = max(abs(f[3]), abs(f[4]), abs(f[5]))
                        mj = max(abs(f[9]), abs(f[10]), abs(f[11]))
                        # Keep sign from dominant component
                        if abs(f[4]) >= abs(f[3]) and abs(f[4]) >= abs(f[5]):
                            mi = f[4]
                            mj = f[10]
                        elif abs(f[5]) >= abs(f[3]):
                            mi = f[5]
                            mj = f[11]
                        else:
                            mi = f[3]
                            mj = f[9]
                    else:
                        # Balok
                        axial = f[0]
                        shear = max(abs(f[1]), abs(f[2]))  # Geser (bisa Y atau Z)
                        # Momen balok: f[3]=Mx-i, f[4]=My-i, f[5]=Mz-i
                        # Balok arah X: lentur dominan di My atau Mz  
                        # Balok arah Y: lentur dominan di Mx atau Mz
                        mi = max(abs(f[3]), abs(f[4]), abs(f[5]))
                        mj = max(abs(f[9]), abs(f[10]), abs(f[11]))
                        # Keep sign from dominant component
                        if abs(f[3]) >= abs(f[4]) and abs(f[3]) >= abs(f[5]):
                            mi = f[3]
                            mj = f[9]
                        elif abs(f[4]) >= abs(f[5]):
                            mi = f[4]
                            mj = f[10]
                        else:
                            mi = f[5]
                            mj = f[11]
                    
                    m_max = max(abs(mi), abs(mj))

                    res["elements"][eid] = {
                        "axial": round(axial, 2),
                        "shear": round(shear, 2),
                        "moment_i": round(mi, 2),
                        "moment_j": round(mj, 2),
                        "moment_max_abs": round(m_max, 2),
                        "element_type": "Column" if item['is_vertical'] else "Beam",
                        "applied_load": item.get('applied_load', 'N/A')
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
        
        print("-" * 85)
        print(f"  {'ID Elemen':<10} | {'Axial (N)':<12} | {'Geser (N)':<12} | {'Momen I':<12} | {'Momen J':<12}")
        print("-" * 85)
        
        valid_ids = sorted([k for k in data['elements'].keys() if isinstance(data['elements'][k], dict) and 'error' not in data['elements'][k]])
        
        count = 0
        for eid in valid_ids:
            v = data['elements'][eid]
            print(f"  {str(eid):<10} | {v['axial']:<12} | {v['shear']:<12} | {v['moment_i']:<12} | {v['moment_j']:<12}")
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
                
    print("\n" + "="*85)
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
