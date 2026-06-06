"""
PlaniFy — Solver Micro v5 (Arquitectura Híbrida Macro/Micro)
=============================================================
React (Macro) decide CUÁNTOS días trabaja cada empleado y con qué restricciones.
Python (Micro) solo decide EN QUÉ días concretos y a qué hora, ajustándose
a la curva de demanda 24h recibida en el payload.

Payload esperado:
  empleadosPayload  — lista de {id, diasObjetivo, diasObjetivoMin, forzarLibranzaFinde,
                                turnoForzado, diasSeguidosArrastrados, diasEstado,
                                horasSemanales, franjaContrato, tipoJornada}
  necesidades       — {dia: {"06:00": 2, "06:30": 3, ..., apertura, cierre}}
  completas         — ['2026-01-05', ..., '2026-01-11']
  diasCierre        — ['2026-01-01', ...]
  domingosApertura  — ['2026-01-11', ...]
  diasHorarioEspecial — {'2026-12-24': '19:00'}
"""
import os, datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)

DIAS    = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo']
FRANJAS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
NS      = 48    # slots de 30 min

def pt(t):
    try: h,m=t.strip().split(':'); return int(h)+int(m)/60.0
    except: return 0.0

def ft(h):
    hrs=int(h); m=round((h-hrs)*60)
    if m>=60: hrs+=1; m=0
    return f"{hrs:02d}:{m:02d}"


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status':'ok','version':'5.0','architecture':'macro-micro'})


@app.route('/resolver', methods=['POST'])
def resolver():
    d = request.get_json(force=True, silent=True)
    if not d:
        return jsonify({'error':'Sin JSON'}), 400

    emps_p  = d.get('empleadosPayload', [])
    nec     = d.get('necesidades', {})
    comp    = d.get('completas', [])
    he      = d.get('diasHorarioEspecial', {})

    if not emps_p or len(comp) < 7:
        return jsonify({'error':'Faltan empleadosPayload o completas[7]'}), 400

    # ── Demanda 24h ─────────────────────────────────────────────────────────
    dem = {dia: [0]*NS for dia in DIAS}
    for i,dia in enumerate(DIAS):
        for fi,fr in enumerate(FRANJAS):
            dem[dia][fi] = int(nec.get(dia,{}).get(fr,0))

    has_dem = any(sum(dem[d])>0 for d in DIAS)
    dw = {}
    for dia in DIAS:
        w = float(sum(dem[dia])) if has_dem else 1.0
        dw[dia] = max(1.0, w)
    tw = max(1.0, sum(dw.values()))

    # ════════════════════════════════════════════════════════════════════════
    # FASE 1 — CP-SAT: EN QUÉ DÍAS trabaja cada empleado
    # React ya decidió cuántos días (diasObjetivo). Python decide cuáles.
    # ════════════════════════════════════════════════════════════════════════
    model  = cp_model.CpModel()
    solver = cp_model.CpSolver()

    work = {}
    for emp in emps_p:
        eid  = emp['id']
        work[eid] = {}
        dias_st = emp.get('diasEstado', {})
        for dia in DIAS:
            st = dias_st.get(dia, 'disponible')
            if st == 'disponible':
                work[eid][dia] = model.NewBoolVar(f'w_{eid[:4]}_{dia[:2]}')
            else:
                work[eid][dia] = model.NewConstant(0)

    obj = []

    for emp in emps_p:
        eid         = emp['id']
        d_obj       = int(emp.get('diasObjetivo', 5))
        d_min       = int(emp.get('diasObjetivoMin', max(0, d_obj-1)))
        lib_finde   = bool(emp.get('forzarLibranzaFinde', False))
        turno_forz  = emp.get('turnoForzado')        # 'MANANA' | 'TARDE' | None
        dias_seg    = int(emp.get('diasSeguidosArrastrados', 0))
        dias_st     = emp.get('diasEstado', {})

        avail = [work[eid][d] for d in DIAS if dias_st.get(d) == 'disponible']
        n_avail = len(avail)
        if not avail or d_obj == 0:
            continue

        # ── Hard: días exactos (con slack para no romper si hay pocos disponibles) ──
        d_real = min(d_obj, n_avail)
        d_real_min = min(d_min, n_avail)
        # slack: penalización altísima si no llega a d_real
        slack = model.NewIntVar(0, max(1, d_real-d_real_min), f'sl_{eid[:4]}')
        model.Add(sum(avail) + slack == d_real)
        obj.append(slack * 50000)  # Muro casi infranqueable

        # ── Hard: forzar libre finde ──────────────────────────────────────────
        if lib_finde:
            for dia in ['Sábado','Domingo']:
                if dias_st.get(dia) == 'disponible':
                    model.Add(work[eid][dia] == 0)

        # ── Hard: máximo 10 días consecutivos ────────────────────────────────
        if dias_seg >= 10:
            # Primer día disponible de la semana → forzar libre
            for dia in DIAS:
                if dias_st.get(dia) == 'disponible':
                    model.Add(work[eid][dia] == 0)
                    break
        elif dias_seg > 0:
            max_ini = 10 - dias_seg
            primeros = [d for d in DIAS[:max_ini+1] if dias_st.get(d) == 'disponible']
            if len(primeros) > max_ini:
                model.Add(sum(work[eid][d] for d in primeros) <= max_ini)

        # ── Hard: turno forzado (mañana/tarde para fulltime) ─────────────────
        # El turno se aplica en Fase 2, pero aquí podemos bloquear días donde
        # el descanso de 12h sería imposible si venimos del domingo anterior.
        # (Simplificado: la restricción de 12h se gestiona en fase 2 por franja)

    # ── Soft: distribución proporcional a demanda ─────────────────────────────
    total_pd = sum(min(emp.get('diasObjetivo',5), sum(1 for d in DIAS if emp.get('diasEstado',{}).get(d)=='disponible')) for emp in emps_p)

    for dia in DIAS:
        workers = [work[emp['id']][dia] for emp in emps_p
                   if emp.get('diasEstado',{}).get(dia) == 'disponible']
        if not workers:
            continue
        tgt = max(0, int(round(total_pd * dw[dia] / tw)))
        sn  = model.NewIntVar(0, len(emps_p), f'sn_{dia[:2]}')
        sp  = model.NewIntVar(0, len(emps_p), f'sp_{dia[:2]}')
        model.Add(sum(workers) + sn - sp == tgt)
        wi  = int(dw[dia] / tw * 1000)
        obj.append(sn * wi * 3)   # déficit: peor que exceso
        obj.append(sp * wi * 1)

    model.Minimize(sum(obj) if obj else 0)

    solver.parameters.max_time_in_seconds  = 20.0
    solver.parameters.num_search_workers   = 2
    solver.parameters.log_search_progress  = False
    cp_st = solver.Solve(model)

    if cp_st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return jsonify({'error':'Sin solución factible','status':'INFEASIBLE'}), 400

    assigned = {
        emp['id']: {dia: bool(solver.Value(work[emp['id']][dia])) for dia in DIAS}
        for emp in emps_p
    }

    # ════════════════════════════════════════════════════════════════════════
    # FASE 2 — Asignación de franja horaria según demanda 24h
    # ════════════════════════════════════════════════════════════════════════
    cov = {d: [0]*NS for d in DIAS}
    res = {}
    INAC = {'LIBRE','VACACIONES','CERRADO','NO ALTA','INACTIVO',''}

    for emp in emps_p:
        eid      = emp['id']
        h_sem    = float(emp.get('horasSemanales', 40))
        es_irr   = emp.get('tipoJornada') == 'Irregular'
        turno_f  = emp.get('turnoForzado')   # 'MANANA' | 'TARDE' | None
        dias_st  = emp.get('diasEstado', {})
        res[eid] = {}

        act  = [d for d in DIAS if assigned[eid].get(d, False)]
        dsr  = max(1, len(act))

        # Franja según turno forzado o contrato
        ef = emp.get('franjaContrato', '06:00 - 23:00') or '06:00 - 23:00'
        if turno_f == 'MANANA': ef = '06:00 - 15:00'
        elif turno_f == 'TARDE': ef = '14:00 - 23:30'

        # 39.5h: día de menor demanda → 7.5h
        dia_red = None
        if h_sem == 39.5 and dsr >= 5 and act:
            dia_red = min(act, key=lambda d: dw.get(d, 1))

        pe = None  # fin del turno anterior (para 12h de descanso)

        for i, dia in enumerate(DIAS):
            st = dias_st.get(dia, 'disponible')
            if not assigned[eid].get(dia, False):
                res[eid][dia] = st if st in ('CERRADO','NO ALTA','INACTIVO','VACACIONES') else 'LIBRE'
                if st != 'CERRADO': pe = None
                continue

            # Horas efectivas del día
            if h_sem == 39.5:
                hd = 7.5 if dia == dia_red else 8.0
            elif es_irr and has_dem:
                dd  = float(sum(dem[dia]))
                dt  = max(1.0, sum(sum(dem[d]) for d in act))
                hd  = max(4.0, min(10.0, round(h_sem*(dd/dt)*dsr*2)/2))
            else:
                hd = h_sem / dsr
            hd = max(4.0, hd)

            lf = hd + 0.5 if hd > 6 else hd  # +30 min pausa si >6h efectivas

            # Ventana horaria
            pts = ef.split('-')
            fi  = pt(pts[0].strip()); ff = pt(pts[1].strip())
            fecha = comp[i] if i < len(comp) else ''
            ap  = pt(nec.get(dia,{}).get('apertura','06:00'))
            ci  = pt(nec.get(dia,{}).get('cierre','23:00'))
            li  = max(fi, ap); lF = min(ff, ci)

            if fecha and fecha in he:
                ce = pt(he[fecha])
                if 0 < ce < lF: lF = ce

            # Descanso mínimo 12h entre jornadas
            if pe is not None:
                li = max(li, pe + 12)

            if lF - li < 4:
                res[eid][dia] = 'LIBRE'; pe = None; continue

            # Mejor inicio: maximizar cobertura de demanda sin cubrir
            ms   = max(li, lF - lf)
            best = li; bs = float('-inf')
            t = li
            while t <= ms + 0.001:
                sc = 0.0; ft2 = t
                while ft2 < t + lf - 0.001:
                    sl = int(ft2 * 2)
                    if 0 <= sl < NS:
                        hu = dem[dia][sl] - cov[dia][sl]
                        sc += hu * 2 if hu > 0 else -1
                    ft2 += 0.5
                if sc > bs: bs = sc; best = t
                t += 0.5

            et  = min(best + lf, lF)
            hef = (et-best)-0.5 if (et-best)>6 else (et-best)
            if hef < 4:
                res[eid][dia] = 'LIBRE'; pe = None; continue

            res[eid][dia] = f"{ft(best)} - {ft(et)}"
            pe = et

            ft2 = best
            while ft2 < et - 0.001:
                sl = int(ft2 * 2)
                if 0 <= sl < NS: cov[dia][sl] += 1
                ft2 += 0.5

    # Stats
    pp={d:0 for d in DIAS}; hh={d:0.0 for d in DIAS}
    for dia in DIAS:
        for emp in emps_p:
            t = res.get(emp['id'],{}).get(dia,'')
            if t and t not in INAC and ' - ' in t:
                pp[dia]+=1
                p1,p2=t.split(' - ')
                lon=pt(p2)-pt(p1); hh[dia]+=lon-0.5 if lon>6 else lon
    hh={d:round(v,1) for d,v in hh.items()}

    return jsonify({
        'horarios': res,
        'status':   'OPTIMAL' if cp_st==cp_model.OPTIMAL else 'FEASIBLE',
        'stats':    {'personas_por_dia':pp,'horas_por_dia':hh}
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
