"""
DevShield - Utilidades de seguridad (sin dependencias de Flask ni de la BD).

- LimitadorIntentos: bloquea temporalmente un acceso tras varios intentos de login fallidos
  (flujo alternativo del caso de uso 5).
- obtener_secret_key / obtener_password_admin: evitan dejar claves escritas en el código (RFN-03).
"""
import os
import secrets
import threading
import time


class LimitadorIntentos:
    """Cuenta fallos por clave (p. ej. 'admin:IP:usuario') y bloquea al superar el máximo."""

    def __init__(self, max_intentos: int = 5, bloqueo_segundos: int = 300, reloj=time.monotonic):
        self.max_intentos = max_intentos
        self.bloqueo = bloqueo_segundos
        self._reloj = reloj
        self._datos = {}
        self._lock = threading.Lock()

    def segundos_bloqueado(self, clave: str) -> int:
        """Segundos restantes de bloqueo (0 si puede intentar)."""
        with self._lock:
            d = self._datos.get(clave)
            if not d or not d['hasta']:
                return 0
            restante = d['hasta'] - self._reloj()
            if restante <= 0:
                del self._datos[clave]          # el bloqueo venció: se reinicia el conteo
                return 0
            return int(restante) + 1

    def registrar_fallo(self, clave: str) -> int:
        """Registra un fallo y devuelve los intentos que quedan (0 = ya bloqueado)."""
        with self._lock:
            d = self._datos.setdefault(clave, {'n': 0, 'hasta': 0})
            d['n'] += 1
            if d['n'] >= self.max_intentos:
                d['hasta'] = self._reloj() + self.bloqueo
                return 0
            return self.max_intentos - d['n']

    def limpiar(self, clave: str):
        with self._lock:
            self._datos.pop(clave, None)


def obtener_secret_key(directorio: str) -> str:
    """SECRET_KEY desde el entorno; si no existe, se genera una vez y se guarda en un archivo local."""
    env = os.getenv('DEVSHIELD_SECRET_KEY')
    if env:
        return env
    ruta = os.path.join(directorio, '.secret_key')
    try:
        with open(ruta, 'r', encoding='utf-8') as f:
            valor = f.read().strip()
            if valor:
                return valor
    except OSError:
        pass
    valor = secrets.token_hex(32)
    try:
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write(valor)
        try:
            os.chmod(ruta, 0o600)
        except OSError:
            pass
    except OSError:
        pass                                     # sin permisos de escritura: clave solo para esta ejecución
    return valor


def obtener_password_admin():
    """(password, fue_generada). Usa DEVSHIELD_ADMIN_PASSWORD o genera una aleatoria."""
    env = os.getenv('DEVSHIELD_ADMIN_PASSWORD')
    if env:
        return env, False
    return secrets.token_urlsafe(9), True
