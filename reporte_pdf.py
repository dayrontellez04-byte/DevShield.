import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COLORES_NIVEL = {
    'Riesgo alto': (colors.HexColor('#F4E1DB'), colors.HexColor('#9B3A22')),
    'Riesgo medio': (colors.HexColor('#FBEFD5'), colors.HexColor('#9A6A12')),
    'Riesgo bajo': (colors.HexColor('#E3F1E6'), colors.HexColor('#2F6B3F')),
}
ETIQUETA_SEVERIDAD = {'alta': 'Alta', 'media': 'Media', 'baja': 'Baja'}


def _t(valor) -> str:
    """Texto seguro para reportlab: escapa XML y reemplaza caracteres fuera del alfabeto de la fuente base."""
    s = str(valor if valor is not None else '')
    for a, b in (('≈', '~'), ('→', '->'), ('✅', ''), ('–', '-'), ('—', '-')):
        s = s.replace(a, b)
    s = s.encode('cp1252', 'replace').decode('cp1252')
    return escape(s)


def _cop(n):
    return '$' + f'{round(n):,}'.replace(',', '.') + ' COP'


def generar_pdf(resultado: dict, fecha=None, cargo=None) -> bytes:
    """resultado: dict devuelto por AnalizadorDevShield.ejecutar(). Devuelve los bytes del PDF."""
    base = getSampleStyleSheet()
    normal = ParagraphStyle('n', parent=base['Normal'], fontName='Helvetica', fontSize=10, leading=14, alignment=TA_LEFT)
    pequeno = ParagraphStyle('p', parent=normal, fontSize=8.5, leading=11, textColor=colors.HexColor('#6B7280'))
    h1 = ParagraphStyle('h1', parent=normal, fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=colors.HexColor('#3E6A8E'))
    h2 = ParagraphStyle('h2', parent=normal, fontName='Helvetica-Bold', fontSize=12, leading=16, spaceBefore=12, spaceAfter=4)

    nivel = resultado.get('nivel', 'Riesgo bajo')
    fondo, texto_color = COLORES_NIVEL.get(nivel, COLORES_NIVEL['Riesgo bajo'])
    nivel_style = ParagraphStyle('nv', parent=normal, fontName='Helvetica-Bold', fontSize=16, leading=20, textColor=texto_color)

    historia = [Paragraph('DevShield - Informe de análisis de vacante', h1)]
    meta = []
    if fecha:
        meta.append(f'Fecha del análisis: {fecha.strftime("%d/%m/%Y %H:%M")}')
    if cargo or resultado.get('rol_detectado'):
        meta.append(f'Cargo de referencia: {cargo or resultado.get("rol_detectado")}')
    if meta:
        historia.append(Paragraph(_t(' | '.join(meta)), pequeno))
    historia.append(Spacer(1, 10))

    # Banner de riesgo
    banner = Table([[Paragraph(_t(f'{resultado.get("porcentaje", 0)}%'), nivel_style),
                     [Paragraph(_t(nivel), nivel_style), Paragraph(_t(resultado.get('descripcion', '')), normal)]]],
                   colWidths=[2.6 * cm, 14 * cm])
    banner.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), fondo), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 12), ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 10), ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    historia.append(banner)

    # Banderas rojas
    historia.append(Paragraph('Banderas rojas detectadas', h2))
    banderas = resultado.get('banderas') or []
    if banderas:
        filas = [[Paragraph('<b>Señal</b>', normal), Paragraph('<b>Detalle</b>', normal), Paragraph('<b>Severidad</b>', normal)]]
        for b in banderas:
            filas.append([Paragraph(_t(b.get('titulo')), normal), Paragraph(_t(b.get('detalle')), normal),
                          Paragraph(_t(ETIQUETA_SEVERIDAD.get(b.get('severidad'), '')), normal)])
        tabla = Table(filas, colWidths=[4.5 * cm, 9.5 * cm, 2.6 * cm], repeatRows=1)
        tabla.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F6EFE5')),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#E2D8CC')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        historia.append(tabla)
    else:
        historia.append(Paragraph('No se detectaron banderas rojas evidentes.', normal))

    # Salario vs. mercado
    historia.append(Paragraph('Salario vs. mercado', h2))
    mercado = resultado.get('mercado') or {}
    oferta = resultado.get('salario_texto') or (_cop(resultado['salario']) if resultado.get('salario') else 'No especificado')
    lineas = [f'Salario de la oferta: {oferta}']
    if mercado.get('min') and mercado.get('max'):
        lineas.append(f'Rango de mercado junior: {_cop(mercado["min"])} a {_cop(mercado["max"])}')
    lineas.append(resultado.get('salario_veredicto') or '')
    for l in lineas:
        historia.append(Paragraph(_t(l), normal))

    # Desglose
    desglose = resultado.get('desglose') or {}
    if desglose:
        historia.append(Paragraph('Desglose por criterio (puntos de riesgo)', h2))
        filas = [[Paragraph('<b>Criterio</b>', normal), Paragraph('<b>Puntos</b>', normal)]]
        for k in ('salario', 'contrato', 'lenguaje'):
            filas.append([Paragraph(k.capitalize(), normal), Paragraph(_t(desglose.get(k, 0)), normal)])
        t = Table(filas, colWidths=[6 * cm, 3 * cm])
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F6EFE5')),
                               ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#E2D8CC'))]))
        historia.append(t)

    # Recomendación
    historia.append(Paragraph('Recomendación', h2))
    historia.append(Paragraph(_t(resultado.get('recomendacion', '')), normal))
    historia.append(Spacer(1, 16))
    historia.append(Paragraph('Este informe es una orientación automática basada en reglas y no reemplaza la asesoría '
                              'legal o profesional. Verifica siempre las condiciones directamente con la empresa.', pequeno))

    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm,
                      bottomMargin=2 * cm, title='DevShield - Informe de análisis',
                      author='DevShield').build(historia)
    return buffer.getvalue()
