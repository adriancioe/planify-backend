"""
PlaniFy — Motor OR-Tools CP-SAT v3
Fase 1: CP-SAT — distribución proporcional a demanda + rotación semanal
Fase 2: Asignación determinista de franja horaria
"""
import os, datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)

DIAS = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo']
FRANJAS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0,30)]
NS = 48

def pt(t):
    try: h,m=t.strip().split(':'); return int(h)+int(m)/60.0
    except: return 0.0

def ft(h):
    hrs=int(h); m=round((h-hrs)*60)
    if m>=60: hrs+=1; m=0
    return f"{hrs:02d}:{m:02d}"

def peso_mes(fecha, niv):
    try:
        m=int(fecha.split('-')[1])-1; n=niv[m] if 0<=m<len(niv) else 'Medio'
        return 2 if n=='Alto' else 0 if n=='Bajo' else 1
    except: return 1

def dom_cerrado(f, dom_ap):
    try: return datetime.date.fromisoformat(f).weekday()==6 and f not in dom_ap
    except: return False

def is_vac(eid, f, vacs):
    return any(v.get('inicio','')<=f<=v.get('fin','') for v in vacs.get(eid,[]))

def get_status(emp, dia, f, cierre, dom_ap, vacs, solics):
    if not f: return 'CERRADO'
    eid=emp['id']; tc=emp.get('tipoContrato','Indefinido')
    if tc=='Temporal':
        fc=emp.get('fechaFinContrato','')
        if fc and f>fc: return 'NO ALTA'
    if tc=='Fijo Discontinuo':
        ok=any(p.get('inicio','')<=f<=p.get('fin','') for p in emp.get('periodosActividad',[]))
        return 'disponible' if ok else 'INACTIVO'
    if f<emp.get('fechaInicio','2000-01-01'): return 'NO ALTA'
    if f in cierre or dom_cerrado(f,dom_ap): return 'CERRADO'
    if is_vac(eid,f,vacs) or any(s.get('empId')==eid and s.get('estado')=='APROBADO' and s.get('fecha')==f for s in solics):
        return 'VACACIONES'
    if dia not in emp.get('diasDisponibles',DIAS): return 'LIBRE_FIJO'
    return 'disponible'

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status':'ok','version':'3.0'})

@app.route('/resolver', methods=['POST'])
def resolver():
    d=request.get_json(force=True,silent=True)
    if not d: return jsonify({'error':'Sin JSON'}),400

    emps   = d.get('empleados',[])
    comp   = d.get('completas',[])
    cierre = d.get('diasCierre',[])
    dom_ap = d.get('domingosApertura',[])
    he     = d.get('diasHorarioEspecial',{})
    niv    = d.get('nivelesMeses',['Medio']*12)
    nec    = d.get('necesidades',{})
    vacs   = d.get('vacaciones',{})
    solics = d.get('diasSolicitados',[])
    sem_ant= d.get('horariosSemAnt',{})
    # diasLibresSemAnt: {emp_id: ['Martes','Jueves']} — días libres la semana pasada
    libres_ant = d.get('diasLibresSemAnt',{})
    saldos = d.get('saldos',{})
    sems_e = d.get('semsEfectivas',{})

    if not emps or len(comp)<7:
        return jsonify({'error':'Se necesitan empleados y 7 fechas'}),400

    pm = peso_mes(comp[0], niv)

    # ── Demanda 24h ────────────────────────────────────────────────────────────
    dem = {dia:[0]*NS for dia in DIAS}
    for i,dia in enumerate(DIAS):
        for fi,fr in enumerate(FRANJAS):
            dem[dia][fi]=int(nec.get(dia,{}).get(fr,0))

    has_dem = any(sum(dem[d])>0 for d in DIAS)
    dw = {}
    for dia in DIAS:
        w=float(sum(dem[dia])) if has_dem else 1.0
        if w==0: w=1.0
        if pm==2: w*=1.2
        elif pm==0: w*=0.8
        dw[dia]=w
    tw=max(1.0,sum(dw.values()))

    # ── Estado inamovible ──────────────────────────────────────────────────────
    sm={}
    for emp in emps:
        eid=emp['id']; sm[eid]={}
        for i,dia in enumerate(DIAS):
            f=comp[i] if i<len(comp) else ''
            sm[eid][dia]=get_status(emp,dia,f,cierre,dom_ap,vacs,solics)

    # ── Objetivo de días por empleado ──────────────────────────────────────────
    td={}
    for emp in emps:
        eid=emp['id']; h=emp.get('horasSemanales',40)
        db=4 if h<=24 else 5
        sal=saldos.get(eid,{}); dr=max(0,emp.get('diasServicioMaximos',224)-sal.get('diasUsados',0))
        if dr<=0: td[eid]=0; continue
        tr=dr/max(1.0,float(sems_e.get(eid,47.0)))
        dobj=db
        if pm==0 and tr<db-0.5: dobj=db-1
        elif pm==2 and tr>db+0.5: dobj=min(db+1,6)
        if h>24: dobj=max(4,dobj)
        nd=sum(1 for dia in DIAS if sm[eid][dia]=='disponible')
        td[eid]=max(0,min(dobj,nd,dr))

    # ══════════════════════════════════════════════════════════════════════════
    # FASE 1 — CP-SAT
    # Objetivo doble:
    #   A) Minimizar desviación respecto a distribución proporcional a demanda
    #   B) Penalizar repetir los mismos días libres que la semana anterior (rotación)
    # ══════════════════════════════════════════════════════════════════════════
    model=cp_model.CpModel()

    # Variables
    work={}
    for emp in emps:
        eid=emp['id']; work[eid]={}
        for dia in DIAS:
            work[eid][dia]=(model.NewBoolVar(f'w_{eid[:5]}_{dia[:2]}')
                           if sm[eid][dia]=='disponible' else model.NewConstant(0))

    # Restricción: cada empleado trabaja exactamente td[eid] días
    for emp in emps:
        eid=emp['id']; t=td[eid]
        avail=[work[eid][d] for d in DIAS if sm[eid][d]=='disponible']
        if avail: model.Add(sum(avail)==min(t,len(avail)))

    # Objetivo A: minimizar desviación de distribución proporcional
    total_pd=sum(td.values())
    sp={d:model.NewIntVar(0,len(emps),f'sp_{d[:2]}') for d in DIAS}
    sn={d:model.NewIntVar(0,len(emps),f'sn_{d[:2]}') for d in DIAS}
    for dia in DIAS:
        workers=[work[emp['id']][dia] for emp in emps]
        tgt=max(0,int(round(total_pd*dw[dia]/tw)))
        model.Add(sum(workers)+sn[dia]-sp[dia]==tgt)

    obj_dem=[]
    for dia in DIAS:
        wi=int(dw[dia]/tw*1000)
        obj_dem.append(sn[dia]*wi*3)  # déficit penaliza más
        obj_dem.append(sp[dia]*wi*1)  # exceso penaliza menos

    # Objetivo B: penalizar repetir días libres de la semana anterior (forzar rotación)
    # Si un empleado tuvo libre el Martes la semana pasada, penalizar que lo tenga libre esta semana
    obj_rot=[]
    PESO_ROTACION=800  # Cuanto mayor, más rotación forzada
    for emp in emps:
        eid=emp['id']
        prev_libres=libres_ant.get(eid,[])  # días que tuvo LIBRE la semana pasada
        for dia in prev_libres:
            if sm[eid][dia]=='disponible' and not isinstance(work[eid][dia],int):
                # libre_hoy = 1 - work[eid][dia]  (libre si no trabaja)
                libre_hoy=model.NewBoolVar(f'lib_{eid[:5]}_{dia[:2]}')
                model.Add(libre_hoy==1-work[eid][dia])
                obj_rot.append(libre_hoy*PESO_ROTACION)

    model.Minimize(sum(obj_dem)+sum(obj_rot))

    solver=cp_model.CpSolver()
    solver.parameters.max_time_in_seconds=20.0
    solver.parameters.num_search_workers=2
    solver.parameters.log_search_progress=False
    cp_st=solver.Solve(model)

    if cp_st not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        return jsonify({'error':'Sin solución factible','status':'INFEASIBLE'}),400

    assigned={emp['id']:{dia:bool(solver.Value(work[emp['id']][dia])) for dia in DIAS} for emp in emps}

    # ══════════════════════════════════════════════════════════════════════════
    # FASE 2 — Franja horaria
    # ══════════════════════════════════════════════════════════════════════════
    cov={d:[0]*NS for d in DIAS}
    res={}
    INAC={'LIBRE','VACACIONES','CERRADO','NO ALTA','INACTIVO',''}

    # Rotación fulltime mañana/tarde
    turno={}
    for j,emp in enumerate(emps):
        eid=emp['id']; h=emp.get('horasSemanales',40)
        if h<39: turno[eid]=None; continue
        prev=sem_ant.get(eid,{}); ult=None
        for di in range(6,-1,-1):
            t=prev.get(DIAS[di],'')
            if t and '-' in t and t not in INAC: ult=t; break
        if ult: turno[eid]='tarde' if pt(ult.split('-')[0])<14 else 'manana'
        else: turno[eid]='manana' if j%2==0 else 'tarde'

    for emp in emps:
        eid=emp['id']; h=emp.get('horasSemanales',40); es_irr=emp.get('tipoJornada')=='Irregular'
        res[eid]={}
        act=[d for d in DIAS if assigned[eid].get(d,False)]
        dsr=max(1,len(act))
        ef=emp.get('franjaContrato','06:00 - 23:00') or '06:00 - 23:00'
        if turno.get(eid)=='manana': ef='06:00 - 15:00'
        elif turno.get(eid)=='tarde': ef='14:00 - 23:30'

        dr=None
        if h==39.5 and dsr>=5 and act:
            dr=min(act,key=lambda d:dw.get(d,1))

        pe=None
        for i,dia in enumerate(DIAS):
            st=sm[eid][dia]
            if not assigned[eid].get(dia,False):
                res[eid][dia]=st if st in ('CERRADO','NO ALTA','INACTIVO','VACACIONES') else 'LIBRE'
                if st!='CERRADO': pe=None
                continue
            # Horas efectivas
            if h==39.5: hd=7.5 if dia==dr else 8.0
            elif es_irr:
                dd=float(sum(dem[dia])); dt=max(1.0,sum(sum(dem[d]) for d in act))
                hd=max(4.0,min(10.0,round(h*(dd/dt)*dsr*2)/2))
            else: hd=h/dsr
            hd=max(4.0,hd)
            lf=hd+0.5 if hd>6 else hd
            # Ventana
            pts=ef.split('-'); fi=pt(pts[0].strip()); ff=pt(pts[1].strip())
            fecha=comp[i] if i<len(comp) else ''
            ap=pt(nec.get(dia,{}).get('apertura','06:00')); ci=pt(nec.get(dia,{}).get('cierre','23:00'))
            li=max(fi,ap); lF=min(ff,ci)
            if fecha and fecha in he:
                ce=pt(he[fecha])
                if 0<ce<lF: lF=ce
            if pe: li=max(li,pe+12)
            if lF-li<4: res[eid][dia]='LIBRE'; pe=None; continue
            # Mejor inicio
            ms=max(li,lF-lf); best=li; bs=float('-inf')
            t=li
            while t<=ms+0.001:
                sc=0.0; ft2=t
                while ft2<t+lf-0.001:
                    sl=int(ft2*2)
                    if 0<=sl<NS: hu=dem[dia][sl]-cov[dia][sl]; sc+=hu*2 if hu>0 else -1
                    ft2+=0.5
                if sc>bs: bs=sc; best=t
                t+=0.5
            et=min(best+lf,lF)
            hef=(et-best)-0.5 if (et-best)>6 else (et-best)
            if hef<4: res[eid][dia]='LIBRE'; pe=None; continue
            res[eid][dia]=f"{ft(best)} - {ft(et)}"
            pe=et
            ft2=best
            while ft2<et-0.001:
                sl=int(ft2*2)
                if 0<=sl<NS: cov[dia][sl]+=1
                ft2+=0.5

    # Stats
    pp={d:0 for d in DIAS}; hh={d:0.0 for d in DIAS}
    for dia in DIAS:
        for emp in emps:
            t=res.get(emp['id'],{}).get(dia,'')
            if t and t not in INAC and ' - ' in t:
                pp[dia]+=1; p1,p2=t.split(' - ')
                lon=pt(p2)-pt(p1); hh[dia]+=lon-0.5 if lon>6 else lon
    hh={d:round(v,1) for d,v in hh.items()}

    return jsonify({'horarios':res,
                    'status':'OPTIMAL' if cp_st==cp_model.OPTIMAL else 'FEASIBLE',
                    'stats':{'personas_por_dia':pp,'horas_por_dia':hh}})

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port)
