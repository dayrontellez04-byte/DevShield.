import json
import os
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

from analizador import (AnalizadorDevShield, OfertaNoTIError, TextoInvalidoError,
                        CATEGORIAS_VALIDAS, EXPERIENCIAS_VALIDAS, MONEDAS_VALIDAS, normalizar)
from seguridad import (LimitadorIntentos, obtener_secret_key, obtener_password_admin)
from archivos import extraer_texto, ArchivoInvalidoError, MAX_BYTES
from reporte_pdf import generar_pdf
from migraciones import asegurar_columnas

app = Flask(__name__)
# Configuración SQLite
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# La clave de sesión sale del entorno (o se genera y guarda localmente); no vive en el código.
app.config['SECRET_KEY'] = obtener_secret_key(BASE_DIR)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['MAX_CONTENT_LENGTH'] = MAX_BYTES + 100_000      # tope de subida (archivo + cabeceras del formulario)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Límite de intentos de inicio de sesión (caso de uso 5)
limitador = LimitadorIntentos(max_intentos=5, bloqueo_segundos=300)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "devshield.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- MODELOS DE BASE DE DATOS ---

class Usuario(db.Model):
    __tablename__ = 'usuarios'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    apellido = db.Column(db.String(80), nullable=False)
    correo = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Administrador(db.Model):
    __tablename__ = 'administradores'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    usuario = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class RolMercado(db.Model):
    __tablename__ = 'roles_mercado'
    id = db.Column(db.Integer, primary_key=True)
    rol = db.Column(db.String(100), nullable=False)
    min_salario = db.Column(db.Integer, nullable=False)
    max_salario = db.Column(db.Integer, nullable=False)
    experiencia = db.Column(db.String(10), nullable=False, default='JUNIOR')   # JUNIOR | MID | SENIOR
    moneda = db.Column(db.String(3), nullable=False, default='COP')            # COP | USD

class Vacante(db.Model):
    """Oferta laboral analizada por un candidato (RF-01). Permite el historial."""
    __tablename__ = 'vacantes'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    texto_completo = db.Column(db.Text, nullable=False)
    empresa = db.Column(db.String(150), nullable=True)
    rol_detectado = db.Column(db.String(100), nullable=True)
    salario_ofrecido = db.Column(db.Integer, nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

class AnalisisRiesgo(db.Model):
    """Resultado del análisis de una vacante (RF-04)."""
    __tablename__ = 'analisis_riesgo'
    id = db.Column(db.Integer, primary_key=True)
    vacante_id = db.Column(db.Integer, db.ForeignKey('vacantes.id'), nullable=False, index=True)
    nivel_riesgo = db.Column(db.String(20), nullable=False)          # Riesgo bajo | medio | alto
    puntaje_total = db.Column(db.Integer, nullable=False)
    justificacion = db.Column(db.Text, nullable=True)
    recomendaciones = db.Column(db.Text, nullable=True)
    resultado_json = db.Column(db.Text, nullable=False)              # resultado completo, para reabrirlo y exportar PDF
    fecha_analisis = db.Column(db.DateTime, default=datetime.utcnow)

class PatronAlerta(db.Model):
    """Frases de alerta que el administrador gestiona (RF-02 / RF-05, caso de uso 7)."""
    __tablename__ = 'patrones_alerta'
    id = db.Column(db.Integer, primary_key=True)
    frase = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(20), nullable=False, default='lenguaje')   # salario | contrato | lenguaje
    peso = db.Column(db.Integer, nullable=False, default=5)                    # puntos que suma (1-25)
    activo = db.Column(db.Boolean, nullable=False, default=True)

PATRONES_POR_DEFECTO = [
    ('sin contrato', 'contrato', 15),
    ('sin vinculacion laboral', 'contrato', 15),
    ('periodo de prueba sin pago', 'contrato', 20),
    ('ganar dinero rapido', 'contrato', 15),
    ('trabajo de lunes a domingo', 'lenguaje', 12),
    ('otras funciones que se asignen', 'lenguaje', 6),
    ('funciones adicionales segun necesidad', 'lenguaje', 6),
    ('disponibilidad inmediata', 'lenguaje', 4),
]

ADMINS_AUTORIZADOS = [('Harold', 'harold'), ('Karen', 'karen'), ('Andrea', 'andrea')]

def inicializar_db():
    with app.app_context():
        db.create_all()
        asegurar_columnas(os.path.join(BASE_DIR, 'devshield.db'))   # actualiza BD de versiones anteriores

        # Cargar administradores permitidos si no existen.
        # La contraseña viene de DEVSHIELD_ADMIN_PASSWORD; si no está definida se genera una aleatoria
        # y se muestra UNA sola vez en la consola (no hay contraseñas escritas en el código).
        password_admin, generada = obtener_password_admin()
        creados = []
        for nombre, usuario in ADMINS_AUTORIZADOS:
            if not Administrador.query.filter_by(usuario=usuario).first():
                nuevo_admin = Administrador(nombre=nombre, usuario=usuario)
                nuevo_admin.set_password(password_admin)
                db.session.add(nuevo_admin)
                creados.append(usuario)
        if creados and generada:
            print('\n[DevShield] Administradores creados: ' + ', '.join(creados))
            print(f'[DevShield] Contraseña generada (guárdala, no se vuelve a mostrar): {password_admin}\n')

        # Cargar roles por defecto
        if RolMercado.query.count() == 0:
            roles_default = [
                {'rol': 'Dev Java Junior', 'min': 2500000, 'max': 3800000},
                {'rol': 'Dev Frontend Junior', 'min': 2200000, 'max': 3500000},
                {'rol': 'QA Tester Junior', 'min': 2000000, 'max': 3000000},
                {'rol': 'Backend Junior', 'min': 3000000, 'max': 5500000},
                {'rol': 'Datos / Analista Junior', 'min': 2100000, 'max': 4000000},
                {'rol': 'Soporte técnico Junior', 'min': 1800000, 'max': 2800000}
            ]
            for r in roles_default:
                db.session.add(RolMercado(rol=r['rol'], min_salario=r['min'], max_salario=r['max'],
                                          experiencia='JUNIOR', moneda='COP'))

        # Cargar frases de alerta por defecto
        if PatronAlerta.query.count() == 0:
            for frase, categoria, peso in PATRONES_POR_DEFECTO:
                db.session.add(PatronAlerta(frase=frase, categoria=categoria, peso=peso, activo=True))

        db.session.commit()

# --- LÓGICA DEL ANALIZADOR ---
# La lógica vive en analizador.py (módulo independiente de Flask y de la BD).

def obtener_roles():
    """Bandas salariales vigentes (las que administra el administrador)."""
    return [{'rol': r.rol, 'min': r.min_salario, 'max': r.max_salario,
             'experiencia': r.experiencia or 'JUNIOR', 'moneda': r.moneda or 'COP'}
            for r in RolMercado.query.order_by(RolMercado.id).all()]

def obtener_patrones(solo_activos=False):
    """Frases de alerta vigentes (las que administra el administrador)."""
    q = PatronAlerta.query.order_by(PatronAlerta.id)
    if solo_activos:
        q = q.filter_by(activo=True)
    return [{'frase': p.frase, 'categoria': p.categoria, 'peso': p.peso, 'activo': p.activo}
            for p in q.all()]

# Las tablas se crean al importar el módulo: así funciona con `python app.py`, `flask run` o gunicorn.
inicializar_db()

# --- RUTAS DE LA APLICACIÓN ---

RX_EMPRESA = re.compile(r'(?:empresa|compañ[ií]a|company)\s*[:\-]\s*([^\n\r.,;]{2,80})', re.I)

def guardar_analisis(usuario_id, texto, resultado):
    """Guarda la vacante y su análisis. Devuelve el id del análisis (o None si falla; nunca rompe el análisis)."""
    try:
        m = RX_EMPRESA.search(texto)
        salario = resultado.get('salario')
        vacante = Vacante(usuario_id=usuario_id, texto_completo=texto,
                          empresa=m.group(1).strip() if m else None,
                          rol_detectado=resultado.get('rol_detectado'),
                          salario_ofrecido=int(salario) if salario else None)
        db.session.add(vacante)
        db.session.flush()
        justificacion = ' | '.join(f"{b['titulo']}: {b['detalle']}" for b in resultado.get('banderas', []))
        analisis = AnalisisRiesgo(vacante_id=vacante.id, nivel_riesgo=resultado['nivel'],
                                  puntaje_total=int(resultado['porcentaje']),
                                  justificacion=justificacion,
                                  recomendaciones=resultado.get('recomendacion'),
                                  resultado_json=json.dumps(resultado, ensure_ascii=False))
        db.session.add(analisis)
        db.session.commit()
        return analisis.id
    except Exception:
        db.session.rollback()
        app.logger.exception('No se pudo guardar el historial del análisis')
        return None

def analisis_del_usuario(analisis_id):
    """(analisis, vacante) si el análisis pertenece al candidato con sesión; si no, (None, None)."""
    uid = session.get('usuario_id')
    if not uid:
        return None, None
    analisis = AnalisisRiesgo.query.filter_by(id=analisis_id).first()
    if not analisis:
        return None, None
    vacante = Vacante.query.filter_by(id=analisis.vacante_id).first()
    if not vacante or vacante.usuario_id != uid:
        return None, None
    return analisis, vacante

def _clave_intentos(tipo, identificador):
    return f'{tipo}:{request.remote_addr}:{identificador}'

def _respuesta_bloqueo(segundos):
    minutos = max(1, -(-segundos // 60))
    return jsonify({'error': f'Demasiados intentos fallidos. Espera {minutos} minuto(s) antes de volver a intentarlo.'}), 429

@app.route('/')
def index():
    return render_template('index.html')

# Autenticación de Usuarios Regulares
@app.route('/api/auth/registro', methods=['POST'])
def registro():
    data = request.get_json() or {}
    nombre = data.get('nombre', '').strip()
    apellido = data.get('apellido', '').strip()
    correo = data.get('correo', '').strip().lower()
    password = data.get('password', '')

    if not all([nombre, apellido, correo, password]):
        return jsonify({'error': 'Todos los campos son obligatorios.'}), 400

    if Usuario.query.filter_by(correo=correo).first():
        return jsonify({'error': 'Este correo electrónico ya se encuentra registrado. Inicia sesión en su lugar.',
                        'codigo': 'correo_registrado'}), 409

    usuario = Usuario(nombre=nombre, apellido=apellido, correo=correo)
    usuario.set_password(password)
    db.session.add(usuario)
    db.session.commit()

    return jsonify({
        'status': 'success',
        'mensaje': 'Usuario creado exitosamente, por favor inicie sesión para verificar sus credenciales'
    })

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    correo = data.get('correo', '').strip().lower()
    password = data.get('password', '')

    clave = _clave_intentos('user', correo)
    bloqueo = limitador.segundos_bloqueado(clave)
    if bloqueo:
        return _respuesta_bloqueo(bloqueo)

    usuario = Usuario.query.filter_by(correo=correo).first()
    if not usuario or not usuario.check_password(password):
        quedan = limitador.registrar_fallo(clave)
        if quedan == 0:
            return _respuesta_bloqueo(limitador.segundos_bloqueado(clave))
        return jsonify({'error': f'Credenciales inválidas. Verifica tu correo y contraseña. Te quedan {quedan} intento(s).'}), 401

    limitador.limpiar(clave)
    session.clear()
    session['usuario_id'] = usuario.id
    session['admin_id'] = None
    return jsonify({'status': 'success'})

# Autenticación Exclusiva para Administradores
@app.route('/api/admin/login', methods=['POST'])
def login_admin():
    data = request.get_json() or {}
    usuario_input = data.get('usuario', '').strip().lower()
    password = data.get('password', '')

    clave = _clave_intentos('admin', usuario_input)
    bloqueo = limitador.segundos_bloqueado(clave)
    if bloqueo:
        return _respuesta_bloqueo(bloqueo)

    admin = Administrador.query.filter_by(usuario=usuario_input).first()
    if not admin or not admin.check_password(password):
        quedan = limitador.registrar_fallo(clave)
        if quedan == 0:
            return _respuesta_bloqueo(limitador.segundos_bloqueado(clave))
        return jsonify({'error': f'Acceso denegado. Credenciales de administrador inválidas. Te quedan {quedan} intento(s).'}), 401

    limitador.limpiar(clave)
    session.clear()
    session['usuario_id'] = None
    session['admin_id'] = admin.id
    return jsonify({
        'status': 'success',
        'admin': admin.nombre
    })

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Cierra de verdad la sesión (candidato o administrador) en el servidor."""
    session.clear()
    return jsonify({'status': 'success'})

@app.route('/api/analizar', methods=['POST'])
def analizar():
    if not (session.get('usuario_id') or session.get('admin_id')):
        return jsonify({'error': 'Debes iniciar sesión para analizar una oferta.'}), 401
    data = request.get_json(silent=True) or {}
    texto = data.get('texto', '')
    try:
        resultado = AnalizadorDevShield(texto, obtener_roles(), obtener_patrones(solo_activos=True)).ejecutar()
    except TextoInvalidoError as e:
        return jsonify({'error': str(e)}), 400
    except OfertaNoTIError as e:
        return jsonify({'error': str(e), 'sector_ti': False}), 422
    except Exception:
        # Un fallo inesperado nunca debe devolver HTML: la interfaz espera siempre JSON
        app.logger.exception('Error inesperado al analizar la vacante')
        return jsonify({'error': 'No fue posible analizar la oferta. Intenta de nuevo con el texto completo.'}), 500
    uid = session.get('usuario_id')
    if uid:                                   # el historial es de los candidatos
        resultado['analisis_id'] = guardar_analisis(uid, texto.strip(), resultado)
    return jsonify(resultado)

def validar_roles(nuevos_roles):
    """Valida la lista enviada por el administrador. Devuelve (lista_limpia, error)."""
    if not isinstance(nuevos_roles, list) or not nuevos_roles:
        return None, 'Debe existir al menos un rol con su rango salarial.'
    limpios = []
    for i, r in enumerate(nuevos_roles, start=1):
        if not isinstance(r, dict):
            return None, f'Fila {i}: formato inválido.'
        nombre = str(r.get('rol', '')).strip()
        if not nombre or len(nombre) > 100:
            return None, f'Fila {i}: el nombre del rol es obligatorio (máx. 100 caracteres).'
        try:
            minimo, maximo = int(r.get('min')), int(r.get('max'))
        except (TypeError, ValueError):
            return None, f'Fila {i} ("{nombre}"): los salarios deben ser numéricos.'
        if minimo <= 0 or maximo <= 0:
            return None, f'Fila {i} ("{nombre}"): los salarios deben ser mayores a cero.'
        if minimo > maximo:
            return None, f'Fila {i} ("{nombre}"): el salario mínimo no puede superar al máximo.'
        experiencia = str(r.get('experiencia') or 'JUNIOR').strip().upper()
        if experiencia not in EXPERIENCIAS_VALIDAS:
            return None, f'Fila {i} ("{nombre}"): la experiencia debe ser JUNIOR, MID o SENIOR.'
        moneda = str(r.get('moneda') or 'COP').strip().upper()
        if moneda not in MONEDAS_VALIDAS:
            return None, f'Fila {i} ("{nombre}"): la moneda debe ser COP o USD.'
        limpios.append({'rol': nombre, 'min': minimo, 'max': maximo, 'experiencia': experiencia, 'moneda': moneda})
    return limpios, None


@app.errorhandler(413)
def archivo_muy_grande(_e):
    return jsonify({'error': 'El archivo supera el tamaño máximo de 2 MB.'}), 413


@app.route('/api/extraer-texto', methods=['POST'])
def extraer_texto_archivo():
    """Carga de la oferta desde un archivo (.txt, .docx, .pdf): devuelve el texto para revisarlo y analizarlo."""
    if not (session.get('usuario_id') or session.get('admin_id')):
        return jsonify({'error': 'Debes iniciar sesión para cargar un archivo.'}), 401
    archivo = request.files.get('archivo')
    if not archivo or not archivo.filename:
        return jsonify({'error': 'Selecciona un archivo.'}), 400
    try:
        texto = extraer_texto(archivo.filename, archivo.read(MAX_BYTES + 1))
    except ArchivoInvalidoError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        app.logger.exception('Error inesperado leyendo el archivo')
        return jsonify({'error': 'No fue posible leer el archivo.'}), 500
    return jsonify({'texto': texto})


@app.route('/api/historial', methods=['GET'])
def historial():
    uid = session.get('usuario_id')
    if not uid:
        return jsonify({'error': 'Debes iniciar sesión como candidato para ver tu historial.'}), 401
    items = []
    for v in Vacante.query.filter_by(usuario_id=uid).order_by(Vacante.id.desc()).all():
        a = AnalisisRiesgo.query.filter_by(vacante_id=v.id).first()
        if not a:
            continue
        items.append({'id': a.id, 'fecha': a.fecha_analisis.strftime('%Y-%m-%d %H:%M'),
                      'cargo': v.rol_detectado, 'empresa': v.empresa,
                      'nivel': a.nivel_riesgo, 'puntaje': a.puntaje_total})
    return jsonify(items)


@app.route('/api/historial/<int:analisis_id>', methods=['GET', 'DELETE'])
def historial_detalle(analisis_id):
    analisis, vacante = analisis_del_usuario(analisis_id)
    if not analisis:
        return jsonify({'error': 'Análisis no encontrado.'}), 404
    if request.method == 'DELETE':
        db.session.delete(analisis)
        db.session.delete(vacante)
        db.session.commit()
        return jsonify({'status': 'success'})
    resultado = json.loads(analisis.resultado_json)
    resultado['analisis_id'] = analisis.id
    return jsonify(resultado)


@app.route('/api/historial/<int:analisis_id>/pdf', methods=['GET'])
def historial_pdf(analisis_id):
    """Exporta el análisis a PDF (AnalisisRiesgo.exportarPDF)."""
    analisis, vacante = analisis_del_usuario(analisis_id)
    if not analisis:
        return jsonify({'error': 'Análisis no encontrado.'}), 404
    pdf = generar_pdf(json.loads(analisis.resultado_json), fecha=analisis.fecha_analisis, cargo=vacante.rol_detectado)
    return Response(pdf, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename=DevShield_analisis_{analisis.id}.pdf'})


def validar_patrones(nuevos):
    """Valida la lista de frases enviada por el administrador. Devuelve (lista_limpia, error)."""
    if not isinstance(nuevos, list):
        return None, 'Formato inválido.'
    limpios, vistos = [], set()
    for i, p in enumerate(nuevos, start=1):
        if not isinstance(p, dict):
            return None, f'Fila {i}: formato inválido.'
        frase = ' '.join(str(p.get('frase', '')).split())
        if len(frase) < 3 or len(frase) > 150:
            return None, f'Fila {i}: la frase debe tener entre 3 y 150 caracteres.'
        categoria = str(p.get('categoria', '')).strip().lower()
        if categoria not in CATEGORIAS_VALIDAS:
            return None, f'Fila {i} ("{frase}"): la categoría debe ser salario, contrato o lenguaje.'
        try:
            peso = int(p.get('peso'))
        except (TypeError, ValueError):
            return None, f'Fila {i} ("{frase}"): el peso debe ser numérico.'
        if not 1 <= peso <= 25:
            return None, f'Fila {i} ("{frase}"): el peso debe estar entre 1 y 25.'
        clave = normalizar(frase)
        if clave in vistos:
            return None, f'Fila {i}: la frase "{frase}" está repetida.'
        vistos.add(clave)
        limpios.append({'frase': frase, 'categoria': categoria, 'peso': peso, 'activo': bool(p.get('activo', True))})
    if not any(x['activo'] for x in limpios):
        return None, 'Debe existir al menos una frase de alerta activa.'
    return limpios, None


@app.route('/api/patrones', methods=['GET', 'POST'])
def gestionar_patrones():
    if not session.get('admin_id'):
        return jsonify({'error': 'No autorizado. Se requiere sesión de administrador activa.'}), 403

    if request.method == 'POST':
        limpios, error = validar_patrones(request.get_json(silent=True))
        if error:
            return jsonify({'error': error}), 400
        PatronAlerta.query.delete()
        for x in limpios:
            db.session.add(PatronAlerta(frase=x['frase'], categoria=x['categoria'],
                                        peso=x['peso'], activo=x['activo']))
        db.session.commit()
        return jsonify({'status': 'success'})

    return jsonify(obtener_patrones())


@app.route('/api/roles', methods=['GET', 'POST'])
def gestionar_roles():
    if request.method == 'POST':
        # Validación de sesión de administrador
        if not session.get('admin_id'):
            return jsonify({'error': 'No autorizado. Se requiere sesión de administrador activa.'}), 403

        limpios, error = validar_roles(request.get_json(silent=True))
        if error:
            return jsonify({'error': error}), 400

        RolMercado.query.delete()
        for r in limpios:
            db.session.add(RolMercado(rol=r['rol'], min_salario=r['min'], max_salario=r['max'],
                                      experiencia=r['experiencia'], moneda=r['moneda']))
        db.session.commit()
        return jsonify({'status': 'success'})

    return jsonify(obtener_roles())

if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG', '0') == '1', port=5000)