"""
PlaniFy — Motor OR-Tools CP-SAT v2
====================================
Fase 1: CP-SAT minimiza la DESVIACIÓN entre cobertura real y objetivo proporcional
Fase 2: Asignación determinista de franja horaria según demanda 24h
"""

import os, math, datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)

DIAS_SEMANA   = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo']
FRANJAS_30MIN = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0,30)]
N_SLOTS       = 48


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_time(t):
    try:
        h,m = t.strip().split(':'); return int(h)+int(m)/60.0
    except: return 0.0

def float_to_time(h):
    hrs=int(h); mins=round((h-hrs)*60)
    if mins>=60: hrs+=1; mins=0
    return f"{hrs:02d}:{mins:02d}"

def get_peso_mes(fecha, niveles):
    try:
        m=int(fecha.split('-')[1])-1
        n=niveles[m] if 0<=m<len(niveles) else 'Medio'
        return 2 if n=='Alto' else 0 if n=='Bajo' else 1
    except: return 1

def is_dom_cerrado(fecha, dom_apertura):
    try: return datetime.date.fromisoformat(fecha).weekday()==6 and fecha not in dom_apertura
    except: return False

def is_vac(emp_id, fecha, vacs):
    return any(v.get('inicio','')<=fecha<=v.get('fin','') for v in vacs.get(emp_id,[]))

def day_status(emp, dia, fecha, dias_cierre, dom_ap, vacs, solics):
    if not fecha: return 'CERRADO'
    eid=emp['id']; tipo=emp.get('tipoContrato','Indefinido')
    if tipo=='Temporal':
        fin=emp.get('fechaFinContrato','')
        if fin and fecha>fin: return 'NO ALTA'
    if tipo=='Fijo Discontinuo':
        ok=any(p.get('inicio','')<=fecha<=p.get('fin','') for p in emp.get('periodosActividad',[]))
        return 'disponible' if ok else 'INACTIVO'
    if fecha<emp.get('fechaInicio','2000-01-01'): return 'NO ALTA'
    if fecha in dias_cierre or is_dom_cerrado(fecha,dom_ap): return 'CERRADO'
    if is_vac(eid,fecha,vacs) or any(s.get('empId')==eid and s.get('estado')=='APROBADO' and s.get('fecha')==fecha for s in solics):
        return 'VACACIONES'
    if dia not in emp.get('diasDisponibles',DIAS_SEMANA): return 'LIBRE_FIJO'
    return 'disponible'


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status':'ok','version':'2.0'})


# ─── Solver ───────────────────────────────────────────────────────────────────

@app.route('/resolver', methods=['POST'])
def resolver():
    data=request.get_json(force=True,silent=True)
    if not data: return jsonify({'error':'Sin JSON'}),400

    empleados        = data.get('empleados',[])
    completas        = data.get('completas',[])
    dias_cierre      = data.get('diasCierre',[])
    dom_ap           = data.get('domingosApertura',[])
    dias_he          = data.get('diasHorarioEspecial',{})
    niveles          = data.get('nivelesMeses',['Medio']*12)
    necesidades      = data.get('necesidades',{})
    vacs             = data.get('vacaciones',{})
    solics           = data.get('diasSolicitados',[])
    sem_ant          = data.get('horariosSemAnt',{})
    saldos           = data.get('saldos',{})
    sems_efe         = data.get('semsEfectivas',{})

    if not empleados or len(completas)<7:
        return jsonify({'error':'Se necesitan empleados y 7 fechas'}),400

    fecha_ref = completas[0]
    peso_mes  = get_peso_mes(fecha_ref, niveles)

    # ── Demanda ───────────────────────────────────────────────────────────────
    demand = {d:[0]*N_SLOTS for d in DIAS_SEMANA}
    for i,dia in enumerate(DIAS_SEMANA):
        for fi,fr in enumerate(FRANJAS_30MIN):
            demand[dia][fi]=int(necesidades.get(dia,{}).get(fr,0))

    has_demand = any(sum(demand[d])>0 for d in DIAS_SEMANA)
    daily_w = {}
    for dia in DIAS_SEMANA:
        w = float(sum(demand[dia])) if has_demand else 1.0
        if w==0: w=1.0
        if peso_mes==2: w*=1.2
        elif peso_mes==0: w*=0.8
        daily_w[dia]=w
    total_w = max(1.0,sum(daily_w.values()))

    # ── Estado de cada empleado por día ───────────────────────────────────────
    st_map={}
    for emp in empleados:
        eid=emp['id']; st_map[eid]={}
        for i,dia in enumerate(DIAS_SEMANA):
            fecha=completas[i] if i<len(completas) else ''
            st_map[eid][dia]=day_status(emp,dia,fecha,dias_cierre,dom_ap,vacs,solics)

    # ── Objetivo de días por empleado ─────────────────────────────────────────
    target_days={}
    for emp in empleados:
        eid=emp['id']; h=emp.get('horasSemanales',40)
        d_base=4 if h<=24 else 5
        sal=saldos.get(eid,{}); dr=max(0,emp.get('diasServicioMaximos',224)-sal.get('diasUsados',0))
        if dr<=0: target_days[eid]=0; continue
        tr=dr/max(1.0,float(sems_efe.get(eid,47.0)))
        d=d_base
        if peso_mes==0 and tr<d_base-0.5: d=d_base-1
        elif peso_mes==2 and tr>d_base+0.5: d=min(d_base+1,6)
        if h>24: d=max(4,d)
        nd=sum(1 for dia in DIAS_SEMANA if st_map[eid][dia]=='disponible')
        target_days[eid]=max(0,min(d,nd,dr))

    # ══════════════════════════════════════════════════════════════════════════
    # FASE 1 — CP-SAT: minimizar desviación respecto a distribución proporcional
    #
    # OBJETIVO CORRECTO:
    #   target_workers[dia] = total_person_days * daily_w[dia] / total_w
    #   minimizar Σ_dia |workers[dia] - target_workers[dia]| * peso(dia)
    #
    # Esto garantiza que si Sábado tiene demanda 350 y Martes 252,
    # Sábado tendrá ~39% más trabajadores que Martes — no el doble ni el triple.
    # ══════════════════════════════════════════════════════════════════════════
    model = cp_model.CpModel()

    work={}
    for emp in empleados:
        eid=emp['id']; work[eid]={}
        for dia in DIAS_SEMANA:
            if st_map[eid][dia]=='disponible':
                work[eid][dia]=model.NewBoolVar(f'w_{eid[:5]}_{dia[:2]}')
            else:
                work[eid][dia]=model.NewConstant(0)

    # Restricción: cada empleado trabaja exactamente target_days días
    for emp in empleados:
        eid=emp['id']; t=target_days[eid]
        avail=[work[eid][d] for d in DIAS_SEMANA if st_map[eid][d]=='disponible']
        if avail: model.Add(sum(avail)==min(t,len(avail)))

    # Objetivo: minimizar desviación de la distribución proporcional
    total_pd = sum(target_days.values())  # person-days totales esta semana

    slack_pos={d:model.NewIntVar(0,len(empleados),f'sp_{d[:2]}') for d in DIAS_SEMANA}
    slack_neg={d:model.NewIntVar(0,len(empleados),f'sn_{d[:2]}') for d in DIAS_SEMANA}

    for dia in DIAS_SEMANA:
        workers=[work[emp['id']][dia] for emp in empleados]
        # Target proporcional a la demanda del día
        tgt=int(round(total_pd * daily_w[dia] / total_w))
        # workers = tgt + slack_pos - slack_neg
        # (slack_pos = exceso, slack_neg = déficit)
        model.Add(sum(workers)+slack_neg[dia]-slack_pos[dia]==tgt)

    # Penalizar más el déficit que el exceso (mejor sobrar que faltar)
    obj_terms=[]
    for dia in DIAS_SEMANA:
        w_int=int(daily_w[dia]/total_w*1000)
        obj_terms.append(slack_neg[dia]*w_int*3)   # déficit: penalización alta
        obj_terms.append(slack_pos[dia]*w_int*1)   # exceso: penalización baja
    model.Minimize(sum(obj_terms))

    solver=cp_model.CpSolver()
    solver.parameters.max_time_in_seconds=20.0
    solver.parameters.num_search_workers=2
    solver.parameters.log_search_progress=False

    cp_st=solver.Solve(model)
    if cp_st not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        return jsonify({'error':'Sin solución factible','status':'INFEASIBLE'}),400

    day_assigned={
        emp['id']:{dia:bool(solver.Value(work[emp['id']][dia])) for dia in DIAS_SEMANA}
        for emp in empleados
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FASE 2 — Asignación de franja horaria
    # ══════════════════════════════════════════════════════════════════════════
    coverage={d:[0]*N_SLOTS for d in DIAS_SEMANA}
    result={}

    # Rotación fulltime mañana/tarde
    turno={}
    for j,emp in enumerate(empleados):
        eid=emp['id']; h=emp.get('horasSemanales',40)
        if h<39: turno[eid]=None; continue
        prev=sem_ant.get(eid,{}); ult=None
        for di in range(6,-1,-1):
            t=prev.get(DIAS_SEMANA[di],'')
            if t and '-' in t and t not in ('LIBRE','VACACIONES','CERRADO','NO ALTA','INACTIVO'):
                ult=t; break
        if ult: turno[eid]='tarde' if parse_time(ult.split('-')[0])<14 else 'manana'
        else: turno[eid]='manana' if j%2==0 else 'tarde'

    INAC={'LIBRE','VACACIONES','CERRADO','NO ALTA','INACTIVO',''}

    for emp in empleados:
        eid=emp['id']; h=emp.get('horasSemanales',40)
        es_irr=emp.get('tipoJornada')=='Irregular'
        result[eid]={}
        activos=[d for d in DIAS_SEMANA if day_assigned[eid].get(d,False)]
        dsr=max(1,len(activos))

        ef=emp.get('franjaContrato','06:00 - 23:00') or '06:00 - 23:00'
        if turno.get(eid)=='manana': ef='06:00 - 15:00'
        elif turno.get(eid)=='tarde': ef='14:00 - 23:30'

        dia_red=None
        if h==39.5 and dsr>=5 and activos:
            dia_red=min(activos,key=lambda d:daily_w.get(d,1))

        prev_end=None

        for i,dia in enumerate(DIAS_SEMANA):
            st=st_map[eid][dia]
            if not day_assigned[eid].get(dia,False):
                result[eid][dia]=st if st in ('CERRADO','NO ALTA','INACTIVO','VACACIONES') else 'LIBRE'
                if st!='CERRADO': prev_end=None
                continue

            # Horas del día
            if h==39.5: hd=7.5 if dia==dia_red else 8.0
            elif es_irr:
                dd=float(sum(demand[dia])); dt=max(1.0,sum(sum(demand[d]) for d in activos))
                hd=max(4.0,min(10.0,round(h*(dd/dt)*dsr*2)/2))
            else: hd=h/dsr
            hd=max(4.0,hd)
            lf=hd+0.5 if hd>6 else hd

            # Ventana horaria
            pts=ef.split('-')
            fi=parse_time(pts[0].strip()); ff=parse_time(pts[1].strip())
            fecha=completas[i] if i<len(completas) else ''
            ap=parse_time(necesidades.get(dia,{}).get('apertura','06:00'))
            ci=parse_time(necesidades.get(dia,{}).get('cierre','23:00'))
            li=max(fi,ap); lF=min(ff,ci)
            if fecha and fecha in dias_he:
                ce=parse_time(dias_he[fecha])
                if 0<ce<lF: lF=ce
            if prev_end: mi=prev_end+12; li=max(li,mi)
            if lF-li<4: result[eid][dia]='LIBRE'; prev_end=None; continue

            # Mejor inicio por demanda 24h
            ms=max(li,lF-lf); best=li; bs=float('-inf')
            t=li
            while t<=ms+0.001:
                sc=0.0; ft=t
                while ft<t+lf-0.001:
                    sl=int(ft*2)
                    if 0<=sl<N_SLOTS: hu=demand[dia][sl]-coverage[dia][sl]; sc+=hu*2 if hu>0 else -1
                    ft+=0.5
                if sc>bs: bs=sc; best=t
                t+=0.5

            et=best+lf
            if et>lF: et=lF
            hef=(et-best)-0.5 if (et-best)>6 else (et-best)
            if hef<4: result[eid][dia]='LIBRE'; prev_end=None; continue

            result[eid][dia]=f"{float_to_time(best)} - {float_to_time(et)}"
            prev_end=et
            ft=best
            while ft<et-0.001:
                sl=int(ft*2)
                if 0<=sl<N_SLOTS: coverage[dia][sl]+=1
                ft+=0.5

    # Stats
    pp={d:0 for d in DIAS_SEMANA}; hh={d:0.0 for d in DIAS_SEMANA}
    for dia in DIAS_SEMANA:
        for emp in empleados:
            t=result.get(emp['id'],{}).get(dia,'')
            if t and t not in INAC and ' - ' in t:
                pp[dia]+=1; p1,p2=t.split(' - ')
                lon=parse_time(p2)-parse_time(p1); hh[dia]+=lon-0.5 if lon>6 else lon
    hh={d:round(v,1) for d,v in hh.items()}

    return jsonify({'horarios':result,'status':'OPTIMAL' if cp_st==cp_model.OPTIMAL else 'FEASIBLE',
                    'stats':{'personas_por_dia':pp,'horas_por_dia':hh}})


if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port)
