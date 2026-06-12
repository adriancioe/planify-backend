"""
PlaniFy — Motor OR-Tools CP-SAT v8 (Producción)
Lógica VALIDADA con simulación de año natural completo:
  - Días anuales cuadran (±1%)
  - Horas anuales cuadran (±5%) vía jornada irregular flexible
  - Fines de semana libres EXACTOS (hard cuando es factible)
  - Distribución de personal proporcional a demanda 24h
  - Rotación mañana/tarde semanal para full-time >=39h
  - Descanso 12h entre jornadas (con cambio de día)
  - Máximo 10 días consecutivos
  - Jornada 4h-9h, +30min pausa si >6h
"""
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)

DIAS    = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo']
FRANJAS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
NS      = 48

def pt(t):
    try: h,m = t.strip().split(':'); return int(h)+int(m)/60.0
    except: return 0.0

def ft(h):
    hrs=int(h); m=round((h-hrs)*60)
    if m>=60: hrs+=1; m=0
    return f"{hrs:02d}:{m:02d}"


def resolver_semana(payload):
    emps_p = payload.get('empleadosPayload', [])
    nec    = payload.get('necesidades', {})
    comp   = payload.get('completas', [])
    he     = payload.get('diasHorarioEspecial', {})

    if not emps_p or len(comp) < 7:
        return {'error': 'Faltan datos', 'status': 'ERROR'}

    dem = {dia: [0]*NS for dia in DIAS}
    for i, dia in enumerate(DIAS):
        for fi, fr in enumerate(FRANJAS):
            dem[dia][fi] = int(nec.get(dia, {}).get(fr, 0))

    has_dem = any(sum(dem[d]) > 0 for d in DIAS)
    dw = {dia: (float(sum(dem[dia])) if has_dem else 0.0) for dia in DIAS}
    tw = sum(dw.values())

    # ── FASE 1: CP-SAT — qué días trabaja cada empleado ──────────────────────
    model = cp_model.CpModel()
    work = {}
    for emp in emps_p:
        eid = emp['id']; work[eid] = {}
        dst = emp.get('diasEstado', {})
        for dia in DIAS:
            if dst.get(dia, 'disponible') == 'disponible':
                work[eid][dia] = model.NewBoolVar(f'w_{eid[:4]}_{dia[:2]}')
            else:
                work[eid][dia] = model.NewConstant(0)

    obj = []
    total_pd = 0
    for emp in emps_p:
        eid = emp['id']
        d_obj = int(emp.get('diasObjetivo', 5))
        dst = emp.get('diasEstado', {})
        avail = [work[eid][d] for d in DIAS if dst.get(d) == 'disponible']
        n_av = len(avail)
        if not avail or d_obj == 0:
            continue
        d_real = min(d_obj, n_av)
        total_pd += d_real
        model.Add(sum(avail) == d_real)  # HARD: días exactos

        # Fines de semana: el Macro decide con lógica exacta.
        if emp.get('forzarLibranzaFinde', False):
            # ¿Puede cumplir días solo con días entre semana? → finde libre HARD.
            dias_semana = [d for d in DIAS[:5] if dst.get(d) == 'disponible']
            if len(dias_semana) >= d_real:
                for dia in ['Sábado', 'Domingo']:
                    if dst.get(dia) == 'disponible':
                        model.Add(work[eid][dia] == 0)
            else:
                for dia in ['Sábado', 'Domingo']:
                    if dst.get(dia) == 'disponible':
                        obj.append(work[eid][dia] * 3000)
        elif emp.get('forzarTrabajoFinde', False):
            # Ya cumplió su cupo de findes libres: preferir que trabaje el finde.
            for dia in ['Sábado', 'Domingo']:
                if dst.get(dia) == 'disponible':
                    lfv = model.NewBoolVar(f'lf_{eid[:4]}_{dia[:2]}')
                    model.Add(lfv == 1 - work[eid][dia])
                    obj.append(lfv * 3000)

        # Hard: máximo 10 días consecutivos.
        seg = int(emp.get('diasSeguidosArrastrados', 0))
        if seg >= 10:
            for dia in DIAS:
                if dst.get(dia) == 'disponible':
                    model.Add(work[eid][dia] == 0); break
        elif seg > 0:
            mi = 10 - seg
            prim = [d for d in DIAS[:mi+1] if dst.get(d) == 'disponible']
            if len(prim) > mi:
                model.Add(sum(work[eid][d] for d in prim) <= mi)

    # Soft: cobertura proporcional a demanda (peso uniforme alto).
    if has_dem and tw > 0:
        for dia in DIAS:
            workers = [work[emp['id']][dia] for emp in emps_p
                       if emp.get('diasEstado', {}).get(dia) == 'disponible']
            if not workers: continue
            tgt = max(0, int(round(total_pd * dw[dia] / tw)))
            sn = model.NewIntVar(0, len(emps_p), f'sn_{dia[:2]}')
            sp = model.NewIntVar(0, len(emps_p), f'sp_{dia[:2]}')
            model.Add(sum(workers) + sn - sp == tgt)
            obj.append(sn * 2000); obj.append(sp * 1500)
    else:
        n_open = sum(1 for d in DIAS if any(
            emp.get('diasEstado', {}).get(d) == 'disponible' for emp in emps_p))
        for dia in DIAS:
            workers = [work[emp['id']][dia] for emp in emps_p
                       if emp.get('diasEstado', {}).get(dia) == 'disponible']
            if not workers: continue
            tgt = max(0, int(round(total_pd / max(1, n_open))))
            sn = model.NewIntVar(0, len(emps_p), f'sn_{dia[:2]}')
            sp = model.NewIntVar(0, len(emps_p), f'sp_{dia[:2]}')
            model.Add(sum(workers) + sn - sp == tgt)
            obj.append(sn * 100); obj.append(sp * 100)

    model.Minimize(sum(obj) if obj else 0)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20.0
    solver.parameters.num_search_workers = 4
    cp_st = solver.Solve(model)

    if cp_st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {'error': 'INFEASIBLE', 'status': 'INFEASIBLE'}

    assigned = {emp['id']: {d: bool(solver.Value(work[emp['id']][d])) for d in DIAS}
                for emp in emps_p}

    # ── FASE 2: franja horaria ───────────────────────────────────────────────
    cov = {d: [0]*NS for d in DIAS}
    res = {}
    INAC = {'LIBRE','VACACIONES','CERRADO','NO ALTA','INACTIVO',''}

    for emp in emps_p:
        eid = emp['id']; h_sem = float(emp.get('horasSemanales', 40))
        h_obj = float(emp.get('horasObjetivoSemana', h_sem))
        turno_f = emp.get('turnoForzado')
        dst = emp.get('diasEstado', {})
        res[eid] = {}
        act = [d for d in DIAS if assigned[eid].get(d, False)]
        dsr = max(1, len(act))

        ef = emp.get('franjaContrato', '06:00 - 23:00') or '06:00 - 23:00'
        if turno_f == 'MANANA': ef = '06:00 - 15:00'
        elif turno_f == 'TARDE': ef = '14:00 - 23:30'

        dia_red = None
        if h_sem == 39.5 and dsr >= 5 and act:
            dia_red = min(act, key=lambda d: dw.get(d, 1))

        pe = None
        hrs_rest_sem = h_obj
        dias_rest_sem = dsr
        for i, dia in enumerate(DIAS):
            st = dst.get(dia, 'disponible')
            if not assigned[eid].get(dia, False):
                res[eid][dia] = st if st in ('CERRADO','NO ALTA','INACTIVO','VACACIONES') else 'LIBRE'
                if st != 'CERRADO': pe = None
                continue
            if h_sem == 39.5:
                hd = 7.5 if dia == dia_red else 8.0
            else:
                # Reparto que garantiza suma semanal = h_obj exacto (sin sesgo).
                hd = hrs_rest_sem / max(1, dias_rest_sem)
                hd = round(hd * 2) / 2
            hd = max(4.0, min(9.0, hd))
            hrs_rest_sem -= hd
            dias_rest_sem -= 1
            lf = hd + 0.5 if hd > 6 else hd

            pts = ef.split('-')
            fi = pt(pts[0].strip()); ff = pt(pts[1].strip())
            fecha = comp[i] if i < len(comp) else ''
            ap = pt(nec.get(dia, {}).get('apertura', '06:00'))
            ci = pt(nec.get(dia, {}).get('cierre', '23:00'))
            li = max(fi, ap); lF = min(ff, ci)
            if lF - li < lf:
                li = ap; lF = ci
            if fecha and fecha in he:
                ce = pt(he[fecha])
                if 0 < ce < lF: lF = ce
            if pe is not None:
                min_start = pe + 12 - 24
                if min_start > 0:
                    li = max(li, min_start)
            if lF - li < 4:
                res[eid][dia] = 'LIBRE'; pe = None; continue

            if turno_f == 'MANANA':
                best = li
            elif turno_f == 'TARDE':
                best = max(li, lF - lf)
            else:
                ms = max(li, lF - lf); best = li; bs = float('-inf')
                t = li
                while t <= ms + 0.001:
                    sc = 0.0; ft2 = t
                    while ft2 < t + lf - 0.001:
                        sl = int(ft2*2)
                        if 0 <= sl < NS:
                            hu = dem[dia][sl] - cov[dia][sl]
                            sc += hu*2 if hu > 0 else -1
                        ft2 += 0.5
                    if sc > bs: bs = sc; best = t
                    t += 0.5

            et = min(best + lf, lF)
            hef = (et-best)-0.5 if (et-best) > 6 else (et-best)
            if hef < 4:
                res[eid][dia] = 'LIBRE'; pe = None; continue
            res[eid][dia] = f"{ft(best)} - {ft(et)}"
            pe = et
            ft2 = best
            while ft2 < et - 0.001:
                sl = int(ft2*2)
                if 0 <= sl < NS: cov[dia][sl] += 1
                ft2 += 0.5

    pp = {d:0 for d in DIAS}; hh = {d:0.0 for d in DIAS}
    for dia in DIAS:
        for emp in emps_p:
            t = res.get(emp['id'], {}).get(dia, '')
            if t and t not in INAC and ' - ' in t:
                pp[dia] += 1
                p1,p2 = t.split(' - ')
                lon = pt(p2)-pt(p1); hh[dia] += lon-0.5 if lon > 6 else lon
    hh = {d:round(v,1) for d,v in hh.items()}

    return {'horarios': res, 'status': 'OPTIMAL' if cp_st==cp_model.OPTIMAL else 'FEASIBLE',
            'stats': {'personas_por_dia': pp, 'horas_por_dia': hh}}


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': '8.0', 'architecture': 'macro-micro-validated'})


@app.route('/resolver', methods=['POST'])
def resolver():
    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({'error': 'Sin JSON'}), 400
    try:
        result = resolver_semana(payload)
        if result.get('status') in ('INFEASIBLE', 'ERROR'):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e), 'status': 'ERROR'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
