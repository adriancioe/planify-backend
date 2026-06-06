"""
PlaniFy — Motor de Planificación OR-Tools (CP-SAT)
===================================================
Dos fases:
  Fase 1 — CP-SAT decide qué días trabaja cada empleado (distribución óptima)
  Fase 2 — Asignación determinista de la franja horaria según demanda 24h
"""

import os
import math
import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)

DIAS_SEMANA   = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
FRANJAS_30MIN = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
N_SLOTS       = 48   # franjas de 30 min en 24h


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_time(t: str) -> float:
    """'HH:MM' → horas decimales  (e.g. '14:30' → 14.5)"""
    try:
        h, m = t.strip().split(':')
        return int(h) + int(m) / 60.0
    except Exception:
        return 0.0


def float_to_time(h: float) -> str:
    """horas decimales → 'HH:MM'  (e.g. 14.5 → '14:30')"""
    hrs  = int(h)
    mins = round((h - hrs) * 60)
    if mins >= 60:
        hrs += 1
        mins = 0
    return f"{hrs:02d}:{mins:02d}"


def get_peso_mes(fecha: str, niveles_meses: list) -> int:
    """Devuelve 0=Bajo, 1=Medio, 2=Alto según el mes de la fecha."""
    try:
        m     = int(fecha.split('-')[1]) - 1
        nivel = niveles_meses[m] if 0 <= m < len(niveles_meses) else 'Medio'
        return 2 if nivel == 'Alto' else 0 if nivel == 'Bajo' else 1
    except Exception:
        return 1


def is_domingo_cerrado(fecha: str, domingos_apertura: list) -> bool:
    try:
        return datetime.date.fromisoformat(fecha).weekday() == 6 and fecha not in domingos_apertura
    except Exception:
        return False


def is_vacaciones(emp_id: str, fecha: str, vacaciones_data: dict) -> bool:
    for v in vacaciones_data.get(emp_id, []):
        if v.get('inicio', '') <= fecha <= v.get('fin', ''):
            return True
    return False


def get_day_status(emp: dict, dia: str, fecha: str,
                   dias_cierre: list, domingos_apertura: list,
                   vacaciones_data: dict, dias_solicitados: list) -> str:
    """
    Devuelve el estado inamovible del día para este empleado:
    'disponible' | 'CERRADO' | 'NO ALTA' | 'VACACIONES' | 'INACTIVO' | 'LIBRE_FIJO'
    """
    if not fecha:
        return 'CERRADO'

    emp_id = emp['id']
    tipo_c = emp.get('tipoContrato', 'Indefinido')

    # Temporal caducado
    if tipo_c == 'Temporal':
        fin_c = emp.get('fechaFinContrato', '')
        if fin_c and fecha > fin_c:
            return 'NO ALTA'

    # Fijo Discontinuo fuera de llamamiento
    if tipo_c == 'Fijo Discontinuo':
        activo = any(
            p.get('inicio', '') <= fecha <= p.get('fin', '')
            for p in emp.get('periodosActividad', [])
        )
        return 'disponible' if activo else 'INACTIVO'

    # Antes del alta
    if fecha < emp.get('fechaInicio', '2000-01-01'):
        return 'NO ALTA'

    # Tienda cerrada
    if fecha in dias_cierre or is_domingo_cerrado(fecha, domingos_apertura):
        return 'CERRADO'

    # Vacaciones o solicitud aprobada
    solic_aprobada = any(
        s.get('empId') == emp_id and
        s.get('estado') == 'APROBADO' and
        s.get('fecha') == fecha
        for s in dias_solicitados
    )
    if is_vacaciones(emp_id, fecha, vacaciones_data) or solic_aprobada:
        return 'VACACIONES'

    # Fuera de días disponibles del empleado
    if dia not in emp.get('diasDisponibles', DIAS_SEMANA):
        return 'LIBRE_FIJO'

    return 'disponible'


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'PlaniFy Solver v1.0'})


@app.route('/resolver', methods=['POST'])
def resolver():
    """
    Recibe:
      empleados          — array de objetos empleado (ver estructura en docs)
      completas          — ['2026-03-23', ..., '2026-03-29']  (7 fechas L-D)
      diasCierre         — fechas de cierre de la tienda
      domingosApertura   — domingos comerciales abiertos
      diasHorarioEspecial— {'2026-12-24': '19:00'}
      nivelesMeses       — ['Medio', 'Bajo', 'Alto', ...] (12 valores Ene-Dic)
      necesidades        — {dia: {franja: personas, apertura: 'HH:MM', cierre: 'HH:MM'}}
      vacaciones         — {emp_id: [{inicio, fin, dias}]}
      diasSolicitados    — [{empId, fecha, estado:'APROBADO'}]
      horariosSemAnt     — {emp_id: {dia: turno}}  (semana anterior, para rotación)
      saldos             — {emp_id: {hUsadas: float, diasUsados: int}}
      semsEfectivas      — {emp_id: float}  (semanas laborables restantes del ciclo)

    Devuelve:
      horarios   — {emp_id: {dia: 'HH:MM - HH:MM' | 'LIBRE' | 'CERRADO' | ...}}
      status     — 'OPTIMAL' | 'FEASIBLE'
      stats      — {personas_por_dia, horas_por_dia}
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'No JSON recibido'}), 400

    empleados          = data.get('empleados', [])
    completas          = data.get('completas', [])
    dias_cierre        = data.get('diasCierre', [])
    domingos_apertura  = data.get('domingosApertura', [])
    dias_he            = data.get('diasHorarioEspecial', {})
    niveles_meses      = data.get('nivelesMeses', ['Medio'] * 12)
    necesidades        = data.get('necesidades', {})
    vacaciones_data    = data.get('vacaciones', {})
    dias_solicitados   = data.get('diasSolicitados', [])
    horarios_sem_ant   = data.get('horariosSemAnt', {})
    saldos             = data.get('saldos', {})
    sems_efe_map       = data.get('semsEfectivas', {})

    if not empleados or len(completas) < 7:
        return jsonify({'error': 'Se requieren empleados y 7 fechas en completas'}), 400

    fecha_ref = completas[0]
    peso_mes  = get_peso_mes(fecha_ref, niveles_meses)

    # ── 1. Matriz de demanda 24h ──────────────────────────────────────────────
    demand = {dia: [0] * N_SLOTS for dia in DIAS_SEMANA}
    for i, dia in enumerate(DIAS_SEMANA):
        for fi, franja in enumerate(FRANJAS_30MIN):
            demand[dia][fi] = int(necesidades.get(dia, {}).get(franja, 0))

    # Peso diario = suma de slots de demanda del día
    # Si la demanda está vacía → todos los días pesan igual (distribución equitativa)
    daily_weight = {}
    has_any_demand = any(sum(demand[d]) > 0 for d in DIAS_SEMANA)
    for dia in DIAS_SEMANA:
        w = float(sum(demand[dia])) if has_any_demand else 1.0
        if w == 0:
            w = 1.0  # Días sin demanda configurada → peso mínimo igual
        # Ajuste por mes de facturación
        if peso_mes == 2:
            w *= 1.2
        elif peso_mes == 0:
            w *= 0.8
        daily_weight[dia] = w

    total_weight = max(1.0, sum(daily_weight.values()))

    # ── 2. Estado inamovible de cada empleado por día ─────────────────────────
    status_map = {}
    for emp in empleados:
        emp_id = emp['id']
        status_map[emp_id] = {}
        for i, dia in enumerate(DIAS_SEMANA):
            fecha = completas[i] if i < len(completas) else ''
            status_map[emp_id][dia] = get_day_status(
                emp, dia, fecha, dias_cierre, domingos_apertura,
                vacaciones_data, dias_solicitados
            )

    # ── 3. Objetivo de días por empleado (proyección anual) ───────────────────
    target_days = {}
    for emp in empleados:
        emp_id = emp['id']
        h_sem  = emp.get('horasSemanales', 40)
        d_base = 4 if h_sem <= 24 else 5

        sal            = saldos.get(emp_id, {})
        dias_usados    = sal.get('diasUsados', 0)
        dias_max       = emp.get('diasServicioMaximos', 224)
        dias_restantes = max(0, dias_max - dias_usados)

        if dias_restantes <= 0:
            target_days[emp_id] = 0
            continue

        sems_efe   = float(sems_efe_map.get(emp_id, 47.0))
        target_raw = dias_restantes / max(1.0, sems_efe)

        # Modulación: solo ajustar si hay desviación real (±0.5 días)
        d_obj = d_base
        if peso_mes == 0 and target_raw < d_base - 0.5:
            d_obj = d_base - 1                         # Mes Bajo + adelantado
        elif peso_mes == 2 and target_raw > d_base + 0.5:
            d_obj = min(d_base + 1, 6)                 # Mes Alto + atrasado
        if h_sem > 24:
            d_obj = max(4, d_obj)                      # Full-time: mínimo 4 días

        n_disp = sum(1 for d in DIAS_SEMANA if status_map[emp_id][d] == 'disponible')
        target_days[emp_id] = max(0, min(d_obj, n_disp, dias_restantes))

    # ══════════════════════════════════════════════════════════════════════════
    # FASE 1 — CP-SAT: distribución óptima de días
    # ══════════════════════════════════════════════════════════════════════════
    model  = cp_model.CpModel()

    # Variables binarias: work[emp_id][dia] ∈ {0,1}
    work = {}
    for emp in empleados:
        emp_id = emp['id']
        work[emp_id] = {}
        for dia in DIAS_SEMANA:
            if status_map[emp_id][dia] == 'disponible':
                work[emp_id][dia] = model.NewBoolVar(f'w_{emp_id[:6]}_{dia[:2]}')
            else:
                work[emp_id][dia] = model.NewConstant(0)

    # Restricción: cada empleado trabaja exactamente target_days[emp_id] días
    for emp in empleados:
        emp_id = emp['id']
        t      = target_days[emp_id]
        avail  = [work[emp_id][d] for d in DIAS_SEMANA
                  if status_map[emp_id][d] == 'disponible']
        if avail:
            t_real = min(t, len(avail))
            model.Add(sum(avail) == t_real)

    # Objetivo: maximizar la cobertura ponderada por demanda
    # Cada trabajador en el día D aporta daily_weight[D] / total_weight
    # → CP-SAT distribuye proporcionalmente a la demanda configurada
    obj_terms = []
    for dia in DIAS_SEMANA:
        w_scaled = int(daily_weight[dia] / total_weight * 10_000)
        for emp in empleados:
            if status_map[emp['id']][dia] == 'disponible':
                obj_terms.append(work[emp['id']][dia] * w_scaled)

    if obj_terms:
        model.Maximize(sum(obj_terms))

    # Resolver
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds  = 15.0
    solver.parameters.num_search_workers   = 2
    solver.parameters.log_search_progress  = False

    cp_status = solver.Solve(model)

    if cp_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return jsonify({'error': 'Sin solución factible', 'status': 'INFEASIBLE'}), 400

    # Extraer asignación de días
    day_assigned = {
        emp['id']: {
            dia: bool(solver.Value(work[emp['id']][dia]))
            for dia in DIAS_SEMANA
        }
        for emp in empleados
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FASE 2 — Asignación determinista de franja horaria
    # ══════════════════════════════════════════════════════════════════════════
    coverage = {dia: [0] * N_SLOTS for dia in DIAS_SEMANA}
    result   = {}

    # Rotación fulltime: mañana / tarde (bloque semanal completo)
    turno_sem = {}
    for j, emp in enumerate(empleados):
        emp_id = emp['id']
        h_sem  = emp.get('horasSemanales', 40)
        if h_sem < 39:
            turno_sem[emp_id] = None
            continue
        prev_h = horarios_sem_ant.get(emp_id, {})
        ult_t  = None
        for di in range(6, -1, -1):
            t = prev_h.get(DIAS_SEMANA[di], '')
            if t and '-' in t and t not in ('LIBRE','VACACIONES','CERRADO','NO ALTA','INACTIVO'):
                ult_t = t
                break
        if ult_t:
            h_ini = parse_time(ult_t.split('-')[0])
            # Si la semana anterior empezó antes de las 14:00 → era mañana → esta semana tarde
            turno_sem[emp_id] = 'tarde' if h_ini < 14 else 'manana'
        else:
            turno_sem[emp_id] = 'manana' if j % 2 == 0 else 'tarde'

    INACTIVOS = {'LIBRE', 'VACACIONES', 'CERRADO', 'NO ALTA', 'INACTIVO', ''}

    for emp in empleados:
        emp_id      = emp['id']
        h_sem       = emp.get('horasSemanales', 40)
        es_irregular = emp.get('tipoJornada') == 'Irregular'
        result[emp_id] = {}

        dias_activos = [d for d in DIAS_SEMANA if day_assigned[emp_id].get(d, False)]
        d_serv_real  = max(1, len(dias_activos))

        # Franja de bloque (fulltime) o franja del contrato
        e_franja = emp.get('franjaContrato', '06:00 - 23:00') or '06:00 - 23:00'
        if turno_sem.get(emp_id) == 'manana':
            e_franja = '06:00 - 15:00'
        elif turno_sem.get(emp_id) == 'tarde':
            e_franja = '14:00 - 23:30'

        # Regla 39.5h: día de menor demanda → 7.5h, el resto → 8h
        dia_reducido = None
        if h_sem == 39.5 and d_serv_real >= 5 and dias_activos:
            dia_reducido = min(dias_activos, key=lambda d: daily_weight.get(d, 1))

        prev_end = None  # Para validar descanso mínimo 12h entre jornadas

        for i, dia in enumerate(DIAS_SEMANA):
            st = status_map[emp_id][dia]

            if not day_assigned[emp_id].get(dia, False):
                # No trabaja → asignar estado inamovible o LIBRE
                if st in ('CERRADO', 'NO ALTA', 'INACTIVO', 'VACACIONES'):
                    result[emp_id][dia] = st
                else:
                    result[emp_id][dia] = 'LIBRE'
                if st != 'CERRADO':
                    prev_end = None
                continue

            # ── Horas efectivas del día ──────────────────────────────────────
            if h_sem == 39.5:
                h_dia = 7.5 if dia == dia_reducido else 8.0
            elif es_irregular:
                # Jornada irregular: proporcional a la demanda del día
                dem_dia = float(sum(demand[dia]))
                dem_tot = max(1.0, sum(sum(demand[d]) for d in dias_activos))
                h_dia   = max(4.0, min(10.0, round(h_sem * (dem_dia / dem_tot) * d_serv_real * 2) / 2))
            else:
                h_dia = h_sem / d_serv_real

            h_dia = max(4.0, h_dia)  # Mínimo 4h por jornada

            # ── Turno físico: añadir 30 min de descanso si jornada efectiva > 6h ──
            long_fisico = h_dia + 0.5 if h_dia > 6 else h_dia

            # ── Ventana horaria ──────────────────────────────────────────────
            partes_franja = e_franja.split('-')
            f_ini = parse_time(partes_franja[0].strip())
            f_fin = parse_time(partes_franja[1].strip())

            fecha_dia = completas[i] if i < len(completas) else ''
            apertura  = parse_time(necesidades.get(dia, {}).get('apertura', '06:00'))
            cierre    = parse_time(necesidades.get(dia, {}).get('cierre',   '23:00'))
            lim_i = max(f_ini, apertura)
            lim_f = min(f_fin, cierre)

            # Cierre anticipado (horario especial)
            if fecha_dia and fecha_dia in dias_he:
                cierre_esp = parse_time(dias_he[fecha_dia])
                if 0 < cierre_esp < lim_f:
                    lim_f = cierre_esp

            # Descanso mínimo de 12h entre jornadas
            if prev_end is not None:
                min_inicio_12h = prev_end + 12
                if min_inicio_12h > lim_i:
                    lim_i = min_inicio_12h

            if lim_f - lim_i < 4:
                result[emp_id][dia] = 'LIBRE'
                prev_end = None
                continue

            # ── Buscar inicio óptimo según demanda 24h ───────────────────────
            max_s      = max(lim_i, lim_f - long_fisico)
            best_start = lim_i
            best_score = float('-inf')

            t = lim_i
            while t <= max_s + 0.001:
                score = 0.0
                ft    = t
                while ft < t + long_fisico - 0.001:
                    slot = int(ft * 2)
                    if 0 <= slot < N_SLOTS:
                        hueco  = demand[dia][slot] - coverage[dia][slot]
                        score += hueco * 2 if hueco > 0 else -1
                    ft += 0.5
                if score > best_score:
                    best_score = score
                    best_start = t
                t += 0.5

            end_t = best_start + long_fisico
            if end_t > lim_f:
                end_t = lim_f

            # Verificar que hay al menos 4h efectivas
            h_efectivas = (end_t - best_start) - 0.5 if (end_t - best_start) > 6 else (end_t - best_start)
            if h_efectivas < 4:
                result[emp_id][dia] = 'LIBRE'
                prev_end = None
                continue

            result[emp_id][dia] = f"{float_to_time(best_start)} - {float_to_time(end_t)}"
            prev_end = end_t

            # Actualizar cobertura real
            ft = best_start
            while ft < end_t - 0.001:
                slot = int(ft * 2)
                if 0 <= slot < N_SLOTS:
                    coverage[dia][slot] += 1
                ft += 0.5

    # ── Stats ─────────────────────────────────────────────────────────────────
    personas_por_dia = {}
    horas_por_dia    = {}
    for dia in DIAS_SEMANA:
        pp = 0
        hh = 0.0
        for emp in empleados:
            t = result.get(emp['id'], {}).get(dia, '')
            if t and t not in INACTIVOS and ' - ' in t:
                pp += 1
                p1, p2 = t.split(' - ')
                lon = parse_time(p2) - parse_time(p1)
                hh += (lon - 0.5 if lon > 6 else lon)
        personas_por_dia[dia] = pp
        horas_por_dia[dia]    = round(hh, 1)

    status_str = 'OPTIMAL' if cp_status == cp_model.OPTIMAL else 'FEASIBLE'

    return jsonify({
        'horarios': result,
        'status':   status_str,
        'stats': {
            'personas_por_dia': personas_por_dia,
            'horas_por_dia':    horas_por_dia,
        }
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
