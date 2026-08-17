from datetime import datetime
from flask import Flask, redirect, render_template, request, url_for
import sqlite3

app = Flask(__name__)

# Fondo de caja inicial global editable
FONDO_CAJA_GLOBAL = 500.00

def init_db():
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    # Tablas del sistema (incluyendo la nueva tabla de retiros)
    cursor.execute("CREATE TABLE IF NOT EXISTS menu (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT, precio REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, cliente TEXT, platillo TEXT, notas TEXT, direccion TEXT, total REAL, metodo_pago TEXT, estado TEXT, fecha TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS inventario (id INTEGER PRIMARY KEY AUTOINCREMENT, insumo TEXT, cantidad REAL, unidad TEXT, stock_minimo REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS retiros (id INTEGER PRIMARY KEY AUTOINCREMENT, concepto TEXT, monto REAL, fecha TEXT)")
    
    # Datos por defecto si las tablas están vacías
    cursor.execute("SELECT COUNT(*) FROM menu")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT INTO menu (nombre, precio) VALUES (?, ?)", 
                           [("Baleadas Especiales", 45.0), ("Baleadas Sencillas", 25.0), ("Tamal de Pollo", 35.0), ("Arroz Chino", 220.0)])
    
    cursor.execute("SELECT COUNT(*) FROM inventario")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT INTO inventario (insumo, cantidad, unidad, stock_minimo) VALUES (?, ?, ?, ?)",
                           [("Harina", 10.0, "Libras", 3.0), ("Frijoles", 8.0, "Libras", 2.0), ("Queso", 5.0, "Libras", 1.5)])
    conn.commit()
    conn.close()

@app.route("/")
def index():
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, precio FROM menu")
    menu = cursor.fetchall()
    
    # Inventario con cálculo automático de stock bajo
    cursor.execute("SELECT id, insumo, cantidad, unidad, stock_minimo FROM inventario")
    inv_raw = cursor.fetchall()
    inventario = [(i[0], i[1], i[2], i[3], i[2] <= i[4]) for i in inv_raw]
    
    # Pedidos activos
    cursor.execute("SELECT id, cliente, platillo, direccion, total, metodo_pago, estado, fecha, notas FROM pedidos WHERE estado != 'Archivado'")
    pedidos = cursor.fetchall()
    
    # Historial / Reportes (todos los archivados o finalizados)
    cursor.execute("SELECT id, cliente, platillo, direccion, total, metodo_pago, estado, fecha, notas FROM pedidos WHERE estado = 'Archivado' ORDER BY id DESC")
    historial = cursor.fetchall()
    
    # Retiros y gastos de caja
    cursor.execute("SELECT id, concepto, monto, fecha FROM retiros ORDER BY id DESC")
    lista_retiros = cursor.fetchall()

    # Cálculos matemáticos del día en curso
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT SUM(total) FROM pedidos WHERE fecha LIKE ? AND estado != 'Archivado'", (f"{fecha_hoy}%",))
    total_hoy = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT SUM(total) FROM pedidos WHERE fecha LIKE ? AND metodo_pago = 'Efectivo' AND estado != 'Archivado'", (f"{fecha_hoy}%",))
    efectivo_hoy = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT SUM(total) FROM pedidos WHERE fecha LIKE ? AND metodo_pago != 'Efectivo' AND estado != 'Archivado'", (f"{fecha_hoy}%",))
    digital_hoy = cursor.fetchone()[0] or 0.0

    # Calcular retiros del día para descontarlos del efectivo en caja
    cursor.execute("SELECT SUM(monto) FROM retiros WHERE fecha LIKE ?", (f"{fecha_hoy}%",))
    total_retiros = cursor.fetchone()[0] or 0.0
    
    conn.close()
    return render_template("index.html", menu=menu, pedidos=pedidos, inventario=inventario, historial=historial,
                           total_hoy=total_hoy, efectivo_hoy=efectivo_hoy, digital_hoy=digital_hoy, 
                           fondo_inicial=FONDO_CAJA_GLOBAL, total_retiros=total_retiros, lista_retiros=lista_retiros)

@app.route("/agregar", methods=["POST"])
def agregar():
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nombre, precio FROM menu WHERE id = ?", (request.form["platillo_id"],))
    platillo = cursor.fetchone()
    if platillo:
        cantidad = int(request.form["cantidad"])
        total = platillo[1] * cantidad
        cursor.execute("INSERT INTO pedidos (cliente, platillo, notas, direccion, total, metodo_pago, estado, fecha) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (request.form["cliente"], f"{platillo[0]} (x{cantidad})", request.form.get("notas", ""), request.form["direccion"], total, request.form["metodo_pago"], "Pendiente", datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/retirar_caja", methods=["POST"])
def retirar_caja():
    concepto = request.form["concepto"]
    monto = float(request.form["monto"])
    
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO retiros (concepto, monto, fecha) VALUES (?, ?, ?)",
                   (concepto, monto, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/editar_inventario", methods=["POST"])
def editar_inventario():
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE inventario SET cantidad = ? WHERE id = ?", (request.form["nueva_cantidad"], request.form["id_insumo"]))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/actualizar_caja", methods=["POST"])
def actualizar_caja():
    global FONDO_CAJA_GLOBAL
    FONDO_CAJA_GLOBAL = float(request.form["fondo_inicial"])
    return redirect(url_for("index"))

@app.route("/estado/<int:id>")
def cambiar_estado(id):
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE pedidos SET estado = 'Entregado' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/archivar/<int:id>")
def archivar(id):
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE pedidos SET estado = 'Archivado' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/corte_dia")
def corte_dia():
    conn = sqlite3.connect("microempresa.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE pedidos SET estado = 'Archivado' WHERE estado != 'Archivado'")
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)