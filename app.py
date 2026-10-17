import csv
import hmac
import io
import logging
import os
import secrets
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, g, session, Response
)
 
# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
# Donde se guarda la base de datos. En Render se pone VENDIX_DB=/var/data/vendix.db
# (la carpeta del disco persistente) para que los datos NO se borren.
# True cuando el programa corre como .exe (version para la computadora del cliente).
EMPAQUETADO = bool(getattr(sys, "frozen", False))
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

DATABASE = os.environ.get("VENDIX_DB", "vendix.db")
if EMPAQUETADO and "VENDIX_DB" not in os.environ:
    # En el .exe los datos se guardan en una carpeta fija del usuario,
    # para que NO se pierdan al cerrar o actualizar el programa.
    _base_datos = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATABASE = os.path.join(_base_datos, "Vendix", "vendix.db")
_carpeta_db = os.path.dirname(DATABASE)
if _carpeta_db:
    os.makedirs(_carpeta_db, exist_ok=True)

# Zona horaria (Honduras = UTC-6, sin cambio de hora). Evita que la caja
# "cambie de dia" a las 6 PM por usar la hora del servidor (UTC).
ZONA = timezone(timedelta(hours=float(os.environ.get("VENDIX_UTC_OFFSET", "-6"))))

# Acceso con usuario y clave (se configuran en Render > Environment).
USUARIO = os.environ.get("VENDIX_USUARIO", "admin")
CLAVE = os.environ.get("VENDIX_CLAVE", "")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = os.environ.get("VENDIX_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
 
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vendix")
 
 
# ---------------------------------------------------------------------------
# Conexión a la base de datos (una por request, vía flask.g)
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DATABASE, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 20000")
        g.db = conn
    return g.db
 
 
@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()
 
 
def crear_base():
    conn = sqlite3.connect(DATABASE, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS productos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                categoria TEXT NOT NULL,
                precio REAL NOT NULL DEFAULT 0,
                stock REAL NOT NULL DEFAULT 0,
                stock_minimo REAL NOT NULL DEFAULT 0,
                unidad TEXT NOT NULL DEFAULT 'Unidades'
            )
        """)
 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente TEXT NOT NULL,
                direccion TEXT DEFAULT '',
                notas TEXT DEFAULT '',
                metodo_pago TEXT NOT NULL,
                total REAL NOT NULL DEFAULT 0,
                estado TEXT NOT NULL DEFAULT 'Pendiente',
                fecha TEXT NOT NULL,
                fecha_archivado TEXT DEFAULT NULL
            )
        """)
 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detalle_pedido (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                producto_id INTEGER NOT NULL,
                cantidad REAL NOT NULL,
                precio REAL NOT NULL,
                subtotal REAL NOT NULL
            )
        """)
 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS retiros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concepto TEXT NOT NULL,
                monto REAL NOT NULL,
                fecha TEXT NOT NULL
            )
        """)
 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cierres_caja (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT NOT NULL UNIQUE,
                ventas REAL NOT NULL DEFAULT 0,
                efectivo REAL NOT NULL DEFAULT 0,
                transferencias REAL NOT NULL DEFAULT 0,
                retiros REAL NOT NULL DEFAULT 0,
                total_caja REAL NOT NULL DEFAULT 0,
                cantidad_pedidos INTEGER NOT NULL DEFAULT 0,
                fecha_cierre TEXT NOT NULL
            )
        """)
 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cajas_diarias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT NOT NULL UNIQUE,
                monto_inicial REAL NOT NULL DEFAULT 0,
                fecha_apertura TEXT NOT NULL
            )
        """)
 
        columnas = conn.execute("PRAGMA table_info(pedidos)").fetchall()
        nombres = [c["name"] for c in columnas]
        if "fecha_archivado" not in nombres:
            conn.execute("ALTER TABLE pedidos ADD COLUMN fecha_archivado TEXT DEFAULT NULL")
        if "notas" not in nombres:
            conn.execute("ALTER TABLE pedidos ADD COLUMN notas TEXT DEFAULT ''")
 
        conn.commit()
    finally:
        conn.close()
 
 
crear_base()
 
 
# ---------------------------------------------------------------------------
# Helpers de fecha / caja
# ---------------------------------------------------------------------------
def hoy():
    return datetime.now(ZONA).strftime("%Y-%m-%d")
 
 
def ahora():
    return datetime.now(ZONA).strftime("%Y-%m-%d %H:%M:%S")
 
 
def formatear_hora(fecha_texto):
    """Convierte 'YYYY-MM-DD HH:MM:SS' a algo legible tipo '10:32 AM'."""
    try:
        dt = datetime.strptime(fecha_texto, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return fecha_texto or ""
 
 
def caja_cerrada_hoy(conn):
    return conn.execute(
        "SELECT id FROM cierres_caja WHERE fecha = ?", (hoy(),)
    ).fetchone() is not None
 
 
def caja_abierta_hoy(conn):
    return conn.execute(
        "SELECT * FROM cajas_diarias WHERE fecha = ?", (hoy(),)
    ).fetchone()
 
 
def monto_inicial_hoy(conn):
    caja = caja_abierta_hoy(conn)
    return float(caja["monto_inicial"]) if caja else 0.0
 
 
def datos_caja_hoy(conn):
    fecha = hoy()
 
    ventas = conn.execute("""
        SELECT COALESCE(SUM(total),0) FROM pedidos
        WHERE substr(fecha,1,10)=? AND estado IN ('Entregado','Archivado')
    """, (fecha,)).fetchone()[0]
 
    efectivo = conn.execute("""
        SELECT COALESCE(SUM(total),0) FROM pedidos
        WHERE substr(fecha,1,10)=? AND metodo_pago='Efectivo'
          AND estado IN ('Entregado','Archivado')
    """, (fecha,)).fetchone()[0]
 
    transferencias = conn.execute("""
        SELECT COALESCE(SUM(total),0) FROM pedidos
        WHERE substr(fecha,1,10)=? AND metodo_pago='Transferencia'
          AND estado IN ('Entregado','Archivado')
    """, (fecha,)).fetchone()[0]
 
    retiros = conn.execute("""
        SELECT COALESCE(SUM(monto),0) FROM retiros
        WHERE substr(fecha,1,10)=?
    """, (fecha,)).fetchone()[0]
 
    cantidad = conn.execute("""
        SELECT COUNT(*) FROM pedidos
        WHERE substr(fecha,1,10)=? AND estado IN ('Entregado','Archivado')
    """, (fecha,)).fetchone()[0]
 
    inicial = monto_inicial_hoy(conn)
    total_caja = inicial + efectivo - retiros
 
    return {
        "ventas": ventas,
        "efectivo": efectivo,
        "transferencias": transferencias,
        "retiros": retiros,
        "cantidad": cantidad,
        "monto_inicial": inicial,
        "total_caja": total_caja,
    }
 
 
def retiros_hoy_lista(conn):
    """Devuelve cada retiro (gasto) de hoy con su concepto/nota y hora,
    más reciente primero, para mostrarlo en la tarjeta de Caja."""
    filas = conn.execute("""
        SELECT concepto, monto, fecha
        FROM retiros
        WHERE substr(fecha,1,10)=?
        ORDER BY id DESC
    """, (hoy(),)).fetchall()
 
    return [
        {
            "concepto": f["concepto"],
            "monto": f["monto"],
            "hora": formatear_hora(f["fecha"]),
        }
        for f in filas
    ]
 
 
# ---------------------------------------------------------------------------
# Decoradores de validación (evitan repetir los mismos checks en cada ruta)
# ---------------------------------------------------------------------------
def requiere_caja_abierta(vista):
    """La caja debe estar abierta y no cerrada todavía hoy."""
    @wraps(vista)
    def envoltura(*args, **kwargs):
        conn = get_db()
        if caja_cerrada_hoy(conn):
            flash("La caja de hoy ya está cerrada.", "danger")
            return redirect(url_for("inicio"))
        if not caja_abierta_hoy(conn):
            flash("Primero debes abrir la caja e ingresar el monto inicial del día.", "warning")
            return redirect(url_for("inicio"))
        return vista(*args, **kwargs)
    return envoltura
 
 
def requiere_caja_no_cerrada(vista):
    """Solo bloquea si la caja ya fue cerrada (no exige que esté abierta)."""
    @wraps(vista)
    def envoltura(*args, **kwargs):
        conn = get_db()
        if caja_cerrada_hoy(conn):
            flash("La caja de hoy ya está cerrada.", "danger")
            return redirect(url_for("inicio"))
        return vista(*args, **kwargs)
    return envoltura
 
 
# ---------------------------------------------------------------------------
# Acceso (login)
# ---------------------------------------------------------------------------
_intentos = {}  # ip -> lista de momentos de intentos fallidos
MAX_INTENTOS = 5
VENTANA_SEG = 600


def _ip_cliente():
    # Detras del proxy de Render, el ultimo valor es el que agrega Render.
    reenviado = request.headers.get("X-Forwarded-For", "")
    if reenviado:
        return reenviado.split(",")[-1].strip()
    return request.remote_addr or "desconocida"


def _bloqueado(ip):
    ahora_t = time.time()
    recientes = [t for t in _intentos.get(ip, []) if ahora_t - t < VENTANA_SEG]
    _intentos[ip] = recientes
    return len(recientes) >= MAX_INTENTOS


@app.before_request
def exigir_login():
    if EMPAQUETADO:
        # Version .exe: solo se abre en esa computadora (127.0.0.1), sin login.
        return None
    if request.endpoint in ("login", "static", "salud"):
        return None
    if not session.get("autenticado"):
        return redirect(url_for("login"))
    return None


@app.route("/salud")
def salud():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = _ip_cliente()
        if not CLAVE:
            flash("Falta configurar la clave (VENDIX_CLAVE) en el servidor.", "danger")
        elif _bloqueado(ip):
            flash("Demasiados intentos. Espera 10 minutos e intenta de nuevo.", "warning")
        else:
            usuario = request.form.get("usuario", "")
            clave = request.form.get("clave", "")
            ok_usuario = hmac.compare_digest(usuario.encode(), USUARIO.encode())
            ok_clave = hmac.compare_digest(clave.encode(), CLAVE.encode())
            if ok_usuario and ok_clave:
                session.clear()
                session["autenticado"] = True
                session.permanent = True
                _intentos.pop(ip, None)
                return redirect(url_for("inicio"))
            _intentos.setdefault(ip, []).append(time.time())
            flash("Usuario o clave incorrectos.", "danger")
    return render_template("login.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Vista principal
# ---------------------------------------------------------------------------
def render_inicio(conn, reporte=False):
    productos = conn.execute(
        "SELECT * FROM productos ORDER BY categoria, nombre"
    ).fetchall()
 
    pedidos = conn.execute("""
        SELECT * FROM pedidos
        WHERE estado IN ('Pendiente','Entregado')
        ORDER BY id DESC
    """).fetchall()
 
    historial = conn.execute("""
        SELECT * FROM pedidos
        WHERE estado IN ('Archivado','Cancelado')
        ORDER BY id DESC
    """).fetchall()
 
    caja = datos_caja_hoy(conn)
    lista_retiros = retiros_hoy_lista(conn)
 
    productos_vendidos = conn.execute("""
        SELECT p.nombre, SUM(d.cantidad) AS cantidad, SUM(d.subtotal) AS total
        FROM detalle_pedido d
        JOIN productos p ON p.id=d.producto_id
        JOIN pedidos pe ON pe.id=d.pedido_id
        WHERE substr(pe.fecha,1,10)=? AND pe.estado IN ('Entregado','Archivado')
        GROUP BY p.id, p.nombre
        ORDER BY cantidad DESC
    """, (hoy(),)).fetchall()
 
    cierres = conn.execute(
        "SELECT * FROM cierres_caja ORDER BY id DESC LIMIT 30"
    ).fetchall()
 
    return render_template(
        "index.html",
        productos=productos,
        pedidos=pedidos,
        historial=historial,
        ventas_hoy=caja["ventas"],
        efectivo=caja["efectivo"],
        digital=caja["transferencias"],
        retiros=caja["retiros"],
        lista_retiros=lista_retiros,
        pedidos_hoy=caja["cantidad"],
        total_caja=caja["total_caja"],
        monto_inicial=caja["monto_inicial"],
        caja_abierta=caja_abierta_hoy(conn),
        caja_cerrada=caja_cerrada_hoy(conn),
        reporte=reporte,
        productos_vendidos=productos_vendidos,
        cierres=cierres,
    )
 
 
@app.route("/")
def inicio():
    return render_inicio(get_db())
 
 
@app.route("/reportes")
def reportes():
    return render_inicio(get_db(), reporte=True)
 
 
# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------
@app.route("/agregar_producto", methods=["POST"])
def agregar_producto():
    conn = get_db()
    try:
        nombre = request.form["nombre"].strip()
        categoria = request.form["categoria"]
        precio = float(request.form["precio"])
        stock = float(request.form["stock"])
        stock_minimo = float(request.form["stock_minimo"])
        unidad = request.form["unidad"].strip() or "Unidades"
 
        if not nombre or precio < 0 or stock < 0 or stock_minimo < 0:
            flash("Datos del producto inválidos.", "danger")
            return redirect(url_for("inicio"))
 
        conn.execute("""
            INSERT INTO productos (nombre,categoria,precio,stock,stock_minimo,unidad)
            VALUES (?,?,?,?,?,?)
        """, (nombre, categoria, precio, stock, stock_minimo, unidad))
        conn.commit()
        flash(f"Producto '{nombre}' agregado.", "success")
 
    except (ValueError, KeyError):
        flash("Datos del producto inválidos.", "danger")
 
    return redirect(url_for("inicio"))
 
 
@app.route("/editar_stock", methods=["POST"])
def editar_stock():
    conn = get_db()
    try:
        producto_id = int(request.form["producto_id"])
        nuevo_stock = float(request.form["nuevo_stock"])
 
        if nuevo_stock < 0:
            flash("El stock no puede ser negativo.", "danger")
            return redirect(url_for("inicio"))
 
        conn.execute("UPDATE productos SET stock=? WHERE id=?", (nuevo_stock, producto_id))
        conn.commit()
        flash("Stock actualizado.", "success")
 
    except (ValueError, KeyError):
        flash("Stock inválido.", "danger")
 
    return redirect(url_for("inicio"))
 
 
# ---------------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------------
@app.route("/crear_pedido", methods=["POST"])
@requiere_caja_abierta
def crear_pedido():
    conn = get_db()
    try:
        cliente = request.form.get("cliente", "").strip()
        direccion = request.form.get("direccion", "").strip()
        notas = request.form.get("notas", "").strip()[:300]
        metodo_pago = request.form.get("metodo_pago", "Efectivo")
        ids = request.form.getlist("producto_id[]")
        cantidades = request.form.getlist("cantidad[]")
 
        if not cliente:
            flash("Debes ingresar el cliente.", "danger")
            return redirect(url_for("inicio"))
        if not ids or len(ids) != len(cantidades):
            flash("Debes agregar productos correctamente.", "danger")
            return redirect(url_for("inicio"))
 
        carrito = {}
        for pid, cantidad in zip(ids, cantidades):
            if not pid:
                continue
            cantidad = float(cantidad)
            if cantidad <= 0:
                flash("Las cantidades deben ser mayores que cero.", "danger")
                return redirect(url_for("inicio"))
            pid = int(pid)
            carrito[pid] = carrito.get(pid, 0) + cantidad
 
        if not carrito:
            flash("Debes seleccionar al menos un producto.", "danger")
            return redirect(url_for("inicio"))
 
        conn.execute("BEGIN IMMEDIATE")
        detalles = []
        total = 0
 
        for producto_id, cantidad in carrito.items():
            producto = conn.execute(
                "SELECT * FROM productos WHERE id=?", (producto_id,)
            ).fetchone()
 
            if producto is None:
                raise ValueError("Uno de los productos no existe.")
 
            # El stock se verifica aquí, pero se descuenta al entregar el pedido.
            if producto["stock"] < cantidad:
                raise ValueError(
                    f"Stock insuficiente para {producto['nombre']}. "
                    f"Disponible: {producto['stock']}"
                )
 
            subtotal = producto["precio"] * cantidad
            total += subtotal
            detalles.append((producto_id, cantidad, producto["precio"], subtotal))
 
        cur = conn.execute("""
            INSERT INTO pedidos (cliente,direccion,notas,metodo_pago,total,estado,fecha)
            VALUES (?,?,?,?,?,?,?)
        """, (cliente, direccion, notas, metodo_pago, round(total, 2), "Pendiente", ahora()))
 
        pedido_id = cur.lastrowid
 
        conn.executemany("""
            INSERT INTO detalle_pedido (pedido_id,producto_id,cantidad,precio,subtotal)
            VALUES (?,?,?,?,?)
        """, [(pedido_id, pid, cant, precio, subtotal) for pid, cant, precio, subtotal in detalles])
 
        conn.commit()
        flash(f"Pedido PED-{pedido_id:04d} creado.", "success")
 
    except (ValueError, KeyError) as e:
        conn.rollback()
        flash(f"No se pudo guardar el pedido: {e}", "danger")
    except Exception:
        conn.rollback()
        logger.exception("Error inesperado creando pedido")
        flash("Ocurrió un error inesperado al guardar el pedido.", "danger")
 
    return redirect(url_for("inicio"))
 
 
@app.route("/entregar/<int:id>", methods=["POST"])
@requiere_caja_abierta
def entregar(id):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
 
        pedido = conn.execute("SELECT * FROM pedidos WHERE id=?", (id,)).fetchone()
        if pedido is None:
            raise ValueError("Pedido no encontrado.")
        if pedido["estado"] != "Pendiente":
            raise ValueError("Este pedido ya no está pendiente.")
 
        detalles = conn.execute("""
            SELECT d.*, p.nombre, p.stock
            FROM detalle_pedido d
            JOIN productos p ON p.id=d.producto_id
            WHERE d.pedido_id=?
        """, (id,)).fetchall()
 
        for d in detalles:
            if d["stock"] < d["cantidad"]:
                raise ValueError(f"No hay suficiente stock de {d['nombre']} para entregar este pedido.")
 
        for d in detalles:
            conn.execute(
                "UPDATE productos SET stock=stock-? WHERE id=?",
                (d["cantidad"], d["producto_id"])
            )
 
        conn.execute("UPDATE pedidos SET estado='Entregado' WHERE id=?", (id,))
        conn.commit()
        flash(f"Pedido PED-{id:04d} entregado.", "success")
 
    except ValueError as e:
        conn.rollback()
        flash(f"No se pudo entregar: {e}", "danger")
    except Exception:
        conn.rollback()
        logger.exception("Error inesperado entregando pedido %s", id)
        flash("Ocurrió un error inesperado al entregar el pedido.", "danger")
 
    return redirect(url_for("inicio"))
 
 
@app.route("/archivar/<int:id>", methods=["POST"])
def archivar(id):
    conn = get_db()
    pedido = conn.execute("SELECT * FROM pedidos WHERE id=?", (id,)).fetchone()
 
    if pedido is None:
        flash("Pedido no encontrado.", "danger")
    elif pedido["estado"] != "Entregado":
        flash("Solo se pueden archivar pedidos entregados.", "warning")
    else:
        conn.execute(
            "UPDATE pedidos SET estado='Archivado', fecha_archivado=? WHERE id=?",
            (ahora(), id)
        )
        conn.commit()
        flash(f"Pedido PED-{id:04d} archivado.", "success")
 
    return redirect(url_for("inicio"))
 
 
@app.route("/cancelar/<int:id>", methods=["POST"])
@requiere_caja_no_cerrada
def cancelar(id):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
 
        pedido = conn.execute("SELECT * FROM pedidos WHERE id=?", (id,)).fetchone()
        if not pedido:
            raise ValueError("Pedido no encontrado.")
        if pedido["estado"] in ("Cancelado", "Archivado"):
            raise ValueError("Este pedido ya no se puede cancelar.")
 
        # Si ya fue entregado, el stock debe volver al inventario.
        if pedido["estado"] == "Entregado":
            detalles = conn.execute(
                "SELECT * FROM detalle_pedido WHERE pedido_id=?", (id,)
            ).fetchall()
            for d in detalles:
                conn.execute(
                    "UPDATE productos SET stock=stock+? WHERE id=?",
                    (d["cantidad"], d["producto_id"])
                )
 
        conn.execute("UPDATE pedidos SET estado='Cancelado' WHERE id=?", (id,))
        conn.commit()
        flash(f"Pedido PED-{id:04d} cancelado.", "success")
 
    except ValueError as e:
        conn.rollback()
        flash(f"No se pudo cancelar: {e}", "danger")
    except Exception:
        conn.rollback()
        logger.exception("Error inesperado cancelando pedido %s", id)
        flash("Ocurrió un error inesperado al cancelar el pedido.", "danger")
 
    return redirect(url_for("inicio"))
 
 
# ---------------------------------------------------------------------------
# Retiros y caja
# ---------------------------------------------------------------------------
@app.route("/registrar_retiro", methods=["POST"])
@requiere_caja_no_cerrada
def registrar_retiro():
    conn = get_db()
    try:
        concepto = request.form["concepto"].strip()
        monto = float(request.form["monto"])
 
        if not concepto or monto <= 0:
            flash("Datos del retiro inválidos.", "danger")
            return redirect(url_for("inicio"))
 
        conn.execute(
            "INSERT INTO retiros(concepto,monto,fecha) VALUES (?,?,?)",
            (concepto, monto, ahora())
        )
        conn.commit()
        flash("Retiro registrado.", "success")
 
    except (ValueError, KeyError):
        flash("Datos del retiro inválidos.", "danger")
 
    return redirect(url_for("inicio"))
 
 
@app.route("/abrir_caja", methods=["POST"])
def abrir_caja():
    conn = get_db()
    try:
        if caja_cerrada_hoy(conn):
            flash("La caja de hoy ya fue cerrada.", "danger")
            return redirect(url_for("inicio"))
 
        if caja_abierta_hoy(conn):
            return redirect(url_for("inicio"))
 
        monto = float(request.form["monto_inicial"])
        if monto < 0:
            flash("El monto inicial no puede ser negativo.", "danger")
            return redirect(url_for("inicio"))
 
        conn.execute(
            "INSERT INTO cajas_diarias(fecha, monto_inicial, fecha_apertura) VALUES (?,?,?)",
            (hoy(), monto, ahora())
        )
        conn.commit()
        flash("Caja abierta correctamente.", "success")
 
    except (ValueError, KeyError):
        flash("Monto inicial inválido.", "danger")
 
    return redirect(url_for("inicio"))
 
 
@app.route("/cerrar_caja", methods=["POST"])
def cerrar_caja():
    conn = get_db()
 
    if caja_cerrada_hoy(conn):
        return redirect(url_for("inicio"))
 
    if not caja_abierta_hoy(conn):
        flash("Primero debes abrir la caja del día.", "warning")
        return redirect(url_for("inicio"))
 
    datos = datos_caja_hoy(conn)
    conn.execute("""
        INSERT INTO cierres_caja
        (fecha,ventas,efectivo,transferencias,retiros,total_caja,cantidad_pedidos,fecha_cierre)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        hoy(), datos["ventas"], datos["efectivo"], datos["transferencias"],
        datos["retiros"], datos["total_caja"], datos["cantidad"], ahora()
    ))
    conn.commit()
    flash("Caja cerrada correctamente.", "success")
 
    return redirect(url_for("inicio"))
 
 
# ---------------------------------------------------------------------------
# Borrar datos de prueba
# ---------------------------------------------------------------------------
@app.route("/eliminar_producto/<int:id>", methods=["POST"])
def eliminar_producto(id):
    conn = get_db()
    usado = conn.execute(
        "SELECT COUNT(*) FROM detalle_pedido WHERE producto_id=?", (id,)
    ).fetchone()[0]
    if usado:
        flash("Este producto ya tiene pedidos en el historial y no se puede eliminar. "
              "Ponle el stock en 0.", "warning")
        return redirect(url_for("inicio"))
    conn.execute("DELETE FROM productos WHERE id=?", (id,))
    conn.commit()
    flash("Producto eliminado.", "success")
    return redirect(url_for("inicio"))


@app.route("/reiniciar_datos", methods=["POST"])
def reiniciar_datos():
    if request.form.get("confirmacion", "").strip().upper() != "BORRAR":
        flash("No se borro nada. Para borrar todo debes escribir BORRAR.", "warning")
        return redirect(url_for("inicio"))
    conn = get_db()
    for tabla in ("detalle_pedido", "pedidos", "retiros", "cierres_caja",
                  "cajas_diarias", "productos"):
        conn.execute(f"DELETE FROM {tabla}")
    conn.execute("DELETE FROM sqlite_sequence")  # los numeros PED-0001 vuelven a empezar
    conn.commit()
    flash("Listo: se borraron todos los datos. Puedes empezar de cero.", "success")
    return redirect(url_for("inicio"))


# ---------------------------------------------------------------------------
# Respaldo: exportar a CSV (se abre en Excel)
# ---------------------------------------------------------------------------
def _seguro(valor):
    """Evita que Excel interprete texto como formula (=, +, -, @)."""
    if isinstance(valor, str) and valor[:1] in ("=", "+", "-", "@"):
        return "'" + valor
    return valor


def _csv_respuesta(nombre, encabezados, filas):
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(encabezados)
    for fila in filas:
        escritor.writerow([_seguro(v) for v in fila])
    contenido = "\ufeff" + salida.getvalue()  # BOM para que Excel lea las tildes
    return Response(
        contenido,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


@app.route("/exportar/inventario")
def exportar_inventario():
    filas = get_db().execute(
        "SELECT nombre, categoria, precio, stock, unidad, stock_minimo "
        "FROM productos ORDER BY categoria, nombre"
    ).fetchall()
    return _csv_respuesta(
        f"inventario_{hoy()}.csv",
        ["Producto", "Categoria", "Precio", "Stock", "Unidad", "Stock minimo"],
        [tuple(f) for f in filas],
    )


@app.route("/exportar/pedidos")
def exportar_pedidos():
    filas = get_db().execute(
        "SELECT id, fecha, cliente, direccion, notas, metodo_pago, total, estado "
        "FROM pedidos ORDER BY id DESC"
    ).fetchall()
    return _csv_respuesta(
        f"pedidos_{hoy()}.csv",
        ["Pedido", "Fecha", "Cliente", "Direccion", "Notas", "Pago", "Total", "Estado"],
        [(f"PED-{f['id']:04d}",) + tuple(f)[1:] for f in filas],
    )


if __name__ == "__main__":
    puerto = os.environ.get("PORT")
    if puerto:
        # En un servidor (Render): escuchar en todas las interfaces.
        app.run(host="0.0.0.0", port=int(puerto), debug=False)
    else:
        # En una computadora: abrir el navegador automaticamente.
        import webbrowser
        import threading

        # Sin consola, stdout/stderr pueden ser None y Flask se cae.
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")

        url = "http://127.0.0.1:5000"

        # Si Vendix ya esta abierto, solo abrir el navegador.
        with socket.socket() as prueba:
            if prueba.connect_ex(("127.0.0.1", 5000)) == 0:
                webbrowser.open(url)
                sys.exit(0)

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        if EMPAQUETADO:
            print("Vendix esta funcionando. No cierres esta ventana mientras lo uses.")
        app.run(host="127.0.0.1", port=5000, debug=False)
