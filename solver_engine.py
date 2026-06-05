import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)

@app.route('/resolver', methods=['POST'])
def resolver_cuadrante():
    datos = request.json
    empleados = datos.get('empleados', [])
    dias_cierre = datos.get('diasCierre', [])
    
    dias_semana = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
    turnos = ['MAÑANA', 'TARDE']  # Añadimos gestión de turnos para control de descansos
    
    model = cp_model.CpModel()
    
    # Variables de decisión: X[(empleado, dia, turno)] = 1 si trabaja ese turno
    X = {}
    # Variable auxiliar para saber si trabaja el día (para contar jornadas)
    TrabajaDia = {}
    
    for e in empleados:
        for d in dias_semana:
            TrabajaDia[(e['id'], d)] = model.NewBoolVar(f"trabaja_{e['id']}_{d}")
            for t in turnos:
                X[(e['id'], d, t)] = model.NewBoolVar(f"x_{e['id']}_{d}_{t}")
            
            # Un empleado solo puede hacer un turno al día (Mañana o Tarde, no ambos)
            model.Add(sum(X[(e['id'], d, t)] for t in turnos) <= 1)
            # Vincular la variable de día con los turnos
            model.Add(sum(X[(e['id'], d, t)] for t in turnos) == TrabajaDia[(e['id'], d)])

    # --- REGLA 3 y 2: DÍAS DE JORNADA, MÉTRICAS Y SALDOS ---
    for e in empleados:
        horas_sem = e.get('horasSemanales', 40)
        saldo_anterior = e.get('saldoHorasAnterior', 0) # Histórico anualizado
        
        # Objetivo de días según contrato
        dias_objetivo = 5 if horas_sem > 24 else 4
        
        # Ajuste inteligente de saldos: si viene con saldo positivo alto, intentamos reducir un día si se puede
        if saldo_anterior > 8:
            dias_objetivo = max(3, dias_objetivo - 1)
        elif saldo_anterior < -8:
            dias_objetivo = min(6, dias_objetivo + 1)
            
        model.Add(sum(TrabajaDia[(e['id'], d)] for d in dias_semana) == dias_objetivo)

    # --- REGLA 7: DÍAS DE CIERRE GENERAL ---
    for d in dias_semana:
        if d in dias_cierre:
            for e in empleados:
                for t in turnos:
                    model.Add(X[(e['id'], d, t)] == 0)

    # --- REGLA 6: ERGONOMÍA LEGAL (No doblar Tarde -> Mañana al día siguiente) ---
    for e in empleados:
        for i in range(len(dias_semana) - 1):
            dia_actual = dias_semana[i]
            dia_siguiente = dias_semana[i+1]
            # Si trabaja de tarde hoy, no puede abrir mañana (mínimo 12h de descanso continuo)
            model.Add(X[(e['id'], dia_actual, 'TARDE')] + X[(e['id'], dia_siguiente, 'MAÑANA')] <= 1)

    # --- REGLA 5: SOLAPAMIENTO DE ROLES CRÍTICOS (Responsable y Segundo Responsable) ---
    jefes = [e for e in empleados if e.get('rol') in ['Responsable', 'Segundo Responsable']]
    if len(jefes) >= 2:
        for d in dias_semana:
            if d not in dias_cierre:
                # Al menos uno de los perfiles de liderazgo debe estar en la tienda cada día de apertura
                model.Add(sum(TrabajaDia[(jff['id'], d)] for jff in jefes) >= 1)

    # --- REGLA 4: EQUIDAD EN FINES DE SEMANA (Sábado/Domingo de calidad) ---
    # Maximizamos que la gente pueda librar los sábados si la tienda abre
    objetivos_equidad = []
    for e in empleados:
        if e.get('rol') not in ['Responsable']: # El responsable suele tener otra rotación, protegemos al equipo
            # Creamos una variable que premia que libre el Sábado
            libre_sabado = model.NewBoolVar(f"libre_sab_{e['id']}")
            model.Add(TrabajaDia[(e['id'], 'Sábado')] == 0).OnlyEnforceIf(libre_sabado)
            model.Add(TrabajaDia[(e['id'], 'Sábado')] == 1).OnlyEnforceIf(libre_sabado.Not())
            objetivos_equidad.append(libre_sabado)
            
    if objetivos_equidad:
        model.Maximize(sum(objetivos_equidad))

    # --- SOLUCIONAR EL PUZZLE MATEMÁTICO ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)
    
    # --- CONSTRUIR LA RESPUESTA ---
    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        resultado = {}
        for e in empleados:
            resultado[e['id']] = {}
            horas_planificadas = 0
            for d in dias_semana:
                turno_asignado = "LIBRE"
                for t in turnos:
                    if solver.Value(X[(e['id'], d, t)]) == 1:
                        turno_asignado = f"{t} (09:00-17:00)" if t == 'MAÑANA' else f"{t} (14:00-22:00)"
                        horas_planificadas += 8 # Asumimos jornadas estándar de 8h para el cálculo de métricas
                resultado[e['id']][d] = turno_asignado
            
            # Calculamos el nuevo saldo de esta semana para devolverlo a las métricas de React
            saldo_semanal = horas_planificadas - e.get('horasSemanales', 40)
            resultado[e['id']]['_metricas'] = {
                "horasPlanificadas": horas_planificadas,
                "saldoSemanal": saldo_semanal,
                "nuevoSaldoAcumulado": e.get('saldoHorasAnterior', 0) + saldo_semanal
            }
            
        return jsonify({"status": "success", "horarios": resultado})
    else:
        return jsonify({"status": "error", "message": "No se encontró combinación válida con las restricciones comerciales"}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
