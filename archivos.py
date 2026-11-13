"""
DevShield - Extracción de texto desde archivos cargados (sin Flask ni BD).

Formatos: .txt / .md, .docx (solo biblioteca estándar) y .pdf (requiere `pypdf`).
"""
import html
import io
import re
import zipfile

MAX_BYTES = 2 * 1024 * 1024        # 2 MB
MAX_PAGINAS_PDF = 30
EXTENSIONES = ('.txt', '.md', '.docx', '.pdf')


class ArchivoInvalidoError(ValueError):
    """El archivo no se pudo leer o no es de un formato admitido."""


def _decodificar(datos: bytes) -> str:
    for codificacion in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return datos.decode(codificacion)
        except UnicodeDecodeError:
            continue
    raise ArchivoInvalidoError('No se pudo leer el texto del archivo.')


def _leer_docx(datos: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            if 'word/document.xml' not in z.namelist():
                raise ArchivoInvalidoError('El archivo no parece un documento de Word válido.')
            info = z.getinfo('word/document.xml')
            if info.file_size > 20 * 1024 * 1024:           # protección contra zip bombs
                raise ArchivoInvalidoError('El documento es demasiado grande.')
            xml = z.read('word/document.xml').decode('utf-8', errors='replace')
    except zipfile.BadZipFile:
        raise ArchivoInvalidoError('El archivo no parece un documento de Word válido (.docx).')
    xml = re.sub(r'<w:tab\s*/>', '\t', xml)
    xml = re.sub(r'<w:(?:br|cr)\s*/>', '\n', xml)
    xml = xml.replace('</w:p>', '\n')
    texto = re.sub(r'<[^>]+>', '', xml)
    return html.unescape(texto)


def _leer_pdf(datos: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ArchivoInvalidoError('La lectura de PDF no está disponible en el servidor (falta instalar pypdf).')
    try:
        lector = PdfReader(io.BytesIO(datos))
        if lector.is_encrypted:
            raise ArchivoInvalidoError('El PDF está protegido con contraseña.')
        paginas = [(p.extract_text() or '') for p in lector.pages[:MAX_PAGINAS_PDF]]
    except ArchivoInvalidoError:
        raise
    except Exception:
        raise ArchivoInvalidoError('No se pudo leer el PDF. Intenta con otro archivo o pega el texto.')
    return '\n'.join(paginas)


def extraer_texto(nombre: str, datos: bytes) -> str:
    """Devuelve el texto del archivo o lanza ArchivoInvalidoError con un mensaje para el usuario."""
    nombre = (nombre or '').lower()
    if not nombre.endswith(EXTENSIONES):
        raise ArchivoInvalidoError('Formato no admitido. Usa un archivo .txt, .docx o .pdf.')
    if not datos:
        raise ArchivoInvalidoError('El archivo está vacío.')
    if len(datos) > MAX_BYTES:
        raise ArchivoInvalidoError('El archivo supera el tamaño máximo de 2 MB.')

    if nombre.endswith('.docx'):
        texto = _leer_docx(datos)
    elif nombre.endswith('.pdf'):
        texto = _leer_pdf(datos)
    else:
        texto = _decodificar(datos)

    texto = re.sub(r'[ \t]+', ' ', texto.replace('\r\n', '\n').replace('\r', '\n'))
    texto = re.sub(r'\n{3,}', '\n\n', texto).strip()
    if not texto:
        raise ArchivoInvalidoError('No se encontró texto en el archivo (si es un PDF escaneado, pega el texto manualmente).')
    return texto
