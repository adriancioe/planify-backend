import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app) # Permite que tu React desde StackBlitz hable con el servidor

@app.route('/resolver', methods=['POST'])
def resolver_cuadrante():
    datos = request.json
    empleados = datos.get('empleados', [])
    dias_cierre = datos.get('diasCierre', [])
    
    dias_semana = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
    
    # 1. CREAR EL MODELO MATEMÁTICO DE GOOGLE OR-TOOLS
    model = cp_model.CpModel()
    
    # Variables de decisión
    X = {}
    for e in empleados:
        for d in dias_semana:
            X[(e['id'], d)] = model.NewBoolVar(f"x_{e['id']}_{d}")
            
    # 2. CONTROL DE DÍAS POR CONTRATO
    for e in empleados:
        horas_sem = e.get('horasSemanales', 40)
        dias_objetivo = 5 if horas_sem > 24 else 4
        model.Add(sum(X[(e['id'], d)] for d in dias_semana) == dias_objetivo)
        
    # 3. DÍAS DE CIERRE GENERAL
    for d in dias_semana:
        if d in dias_cierre:
            for e in empleados:
                model.Add(X[(e['id'], d)] == 0)

    # 4. SOLUCIONAR EL PUZZLE MATEMÁTICO
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)
    
    # 5. CONSTRUIR LA RESPUESTA PARA LA APP
    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        resultado = {}
        for e in empleados:
            resultado[e['id']] = {}
            for d in dias_semana:
                if solver.Value(X[(e['id'], d)]) == 1:
                    resultado[e['id']][d] = "09:00 - 17:00"
                else:
                    resultado[e['id']][d] = "LIBRE"
        return jsonify({"status": "success", "horarios": resultado})
    else:
        return jsonify({"status": "error", "message": "No se encontró combinación válida"}), 400

if __name__ == '__main__':
    # Lee el puerto que le asigne el servidor de internet
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
