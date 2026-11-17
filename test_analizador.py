"""
DevShield - Pruebas unitarias del motor de análisis (analizador.py).

19 casos: validación (3), salario (7) y riesgos (9). No usan Flask ni la base de datos.

Ejecutar desde la carpeta del proyecto:
    python -m unittest test_analizador -v
"""
import unittest

from analizador import (AnalizadorDevShield, OfertaNoTIError, TextoInvalidoError,
                        SMMLV_2026, TRM_USD_COP)

# Mismas bandas que carga app.py por defecto
ROLES = [
    {'rol': 'Dev Java Junior', 'min': 2_500_000, 'max': 3_800_000},
    {'rol': 'Dev Frontend Junior', 'min': 2_200_000, 'max': 3_500_000},
    {'rol': 'QA Tester Junior', 'min': 2_000_000, 'max': 3_000_000},
    {'rol': 'Backend Junior', 'min': 3_000_000, 'max': 5_500_000},
    {'rol': 'Datos / Analista Junior', 'min': 2_100_000, 'max': 4_000_000},
    {'rol': 'Soporte técnico Junior', 'min': 1_800_000, 'max': 2_800_000},
]


def analizar(texto, patrones=None):
    return AnalizadorDevShield(texto, ROLES, patrones).ejecutar()


def titulos(resultado):
    return [b['titulo'] for b in resultado['banderas']]


# Oferta limpia de referencia (Backend Junior, salario dentro de la banda, contrato formal)
OFERTA_LIMPIA = (
    'Desarrollador Backend Junior\n'
    'Buscamos desarrollador backend junior con 1 año de experiencia en Python, Django y SQL. '
    'Salario: $3.200.000 mensuales. Contrato a término indefinido con prestaciones de ley. '
    'Horario de lunes a viernes de 8 am a 5 pm.'
)


class TestValidacion(unittest.TestCase):
    """RF-01 / HU-10 / HU-11: el texto debe ser suficiente y del sector TI."""

    def test_01_texto_vacio_o_solo_espacios(self):
        for texto in ('', '     ', None):
            with self.assertRaises(TextoInvalidoError):
                AnalizadorDevShield(texto, ROLES)

    def test_02_texto_demasiado_corto(self):
        with self.assertRaises(TextoInvalidoError):
            AnalizadorDevShield('Busco dev junior urgente', ROLES)

    def test_03_oferta_fuera_del_sector_ti_se_rechaza(self):
        cajero = ('Cajero de Supermercado - Tiempo Completo\n'
                  'Importante cadena de supermercados requiere cajero con experiencia en manejo de caja '
                  'registradora, datafono y atención al cliente. Contrato a término fijo con todas las '
                  'prestaciones de ley. Salario: $1.423.500 COP mensuales + auxilio de transporte.')
        with self.assertRaises(OfertaNoTIError):
            analizar(cajero)


class TestSalario(unittest.TestCase):
    """RF-03 / HU-04 / HU-16: extracción del salario y comparación con el mercado."""

    def test_04_cifra_con_puntos_y_dentro_de_la_banda(self):
        r = analizar(OFERTA_LIMPIA)
        self.assertEqual(r['salario'], 3_200_000)
        self.assertEqual(r['salario_estado'], 'ok')
        self.assertIn('Backend Junior', r['rol_detectado'])

    def test_05_rango_escrito_en_millones(self):
        r = analizar('Desarrollador Java Junior. Ofrecemos entre 3 y 4 millones mensuales. '
                     'Contrato a término indefinido. Stack: Java y Spring Boot.')
        self.assertEqual(r['salario_texto'], '$3.000.000 – $4.000.000')

    def test_06_salario_en_smmlv_y_en_dolares(self):
        r = analizar('Desarrollador Backend Junior. Salario: 2 SMMLV. Contrato a término indefinido. '
                     'Python y Django.')
        self.assertEqual(r['salario'], round(2 * SMMLV_2026))
        r = analizar('Backend Developer Junior. Salario: USD 2.000 mensuales. Contrato a término '
                     'indefinido. Python y Django.')
        self.assertEqual(r['salario'], round(2000 * TRM_USD_COP))

    def test_07_no_confunde_bonos_con_el_salario(self):
        r = analizar('Desarrollador Backend Junior. Salario: $3.500.000 mensuales. '
                     'Bono de desempeño de $500.000. Contrato a término indefinido. Python y Django.')
        self.assertEqual(r['salario'], 3_500_000)

    def test_08_salario_por_debajo_del_minimo_legal(self):
        r = analizar('Desarrollador Backend Junior. Salario: $1.200.000 mensuales. '
                     'Contrato a término indefinido. Python y Django.')
        self.assertIn('Salario por debajo del mínimo legal', titulos(r))
        self.assertEqual(r['nivel'], 'Riesgo alto')

    def test_09_oferta_sin_salario_no_interrumpe_el_analisis(self):
        r = analizar('Desarrollador Backend Junior. Contrato a término indefinido. '
                     'Python y Django. Horario de lunes a viernes.')
        self.assertIsNone(r['salario'])
        self.assertEqual(r['salario_estado'], 'no_detectado')
        self.assertIn('numérico', r['salario_veredicto'])

    def test_10_salario_a_convenir_es_bandera(self):
        r = analizar('Desarrollador Backend Junior. Salario a convenir. Contrato a término indefinido. '
                     'Python y Django.')
        self.assertIn('Salario a convenir', titulos(r))
        self.assertGreater(r['porcentaje'], 0)


class TestRiesgos(unittest.TestCase):
    """RF-02 / RF-04 / HU-02 / HU-03 / HU-11: señales de riesgo y clasificación."""

    def test_11_oferta_limpia_es_riesgo_bajo_sin_banderas(self):
        r = analizar(OFERTA_LIMPIA)
        self.assertEqual(r['porcentaje'], 0)
        self.assertEqual(r['nivel'], 'Riesgo bajo')
        self.assertEqual(r['banderas'], [])

    def test_12_experiencia_oculta_en_cargo_junior(self):
        r = analizar('Desarrollador Backend Junior. Se requieren 3 años de experiencia en Python y Django. '
                     'Salario: $3.200.000. Contrato a término indefinido.')
        self.assertIn('Experiencia oculta', titulos(r))

    def test_13_prestacion_de_servicios_con_horario_fijo(self):
        r = analizar('Desarrollador Backend Junior. Contrato de prestación de servicios con horario fijo de '
                     'lunes a viernes. Salario: $3.200.000. Python y Django.')
        self.assertIn('Contrato sin garantías', titulos(r))
        banderas = {b['titulo']: b for b in r['banderas']}
        self.assertEqual(banderas['Contrato sin garantías']['severidad'], 'alta')

    def test_14_funciones_cruzadas(self):
        r = analizar('Desarrollador Backend Junior. Python y Django. Además brindará soporte técnico N1/N2 '
                     'y atención al cliente. Salario: $3.200.000. Contrato a término indefinido.')
        self.assertIn('Funciones cruzadas', titulos(r))

    def test_15_cobro_al_candidato_es_posible_fraude(self):
        r = analizar('Desarrollador Backend Junior. Python y Django. Salario: $3.200.000. Para iniciar debes '
                     'pagar una inversión inicial por concepto de capacitación. Contrato a término indefinido.')
        self.assertIn('Posible fraude: cobro al candidato', titulos(r))
        self.assertEqual(r['nivel'], 'Riesgo alto')
        self.assertGreaterEqual(r['porcentaje'], 50)

    def test_16_trabajo_no_remunerado_es_riesgo_alto(self):
        r = analizar('Desarrollador Backend Junior. Python y Django. Práctica ad honorem, no remunerada, '
                     'con posibilidad de vinculación futura.')
        self.assertIn('Trabajo no remunerado', titulos(r))
        self.assertEqual(r['nivel'], 'Riesgo alto')

    def test_17_jornada_excesiva(self):
        r = analizar('Desarrollador Backend Junior. Python y Django. Salario: $3.200.000. Contrato a término '
                     'indefinido. Horario: lunes a sábado de 7:00 AM a 6:00 PM.')
        self.assertIn('Jornada excesiva', titulos(r))

    def test_18_frase_de_alerta_del_administrador(self):
        patrones = [{'frase': 'disponibilidad inmediata', 'categoria': 'lenguaje', 'peso': 4}]
        r = analizar(OFERTA_LIMPIA + ' Se requiere disponibilidad inmediata.', patrones)
        self.assertIn('Frase de alerta: «disponibilidad inmediata»', titulos(r))
        self.assertEqual(r['desglose']['lenguaje'], 4)
        # sin la frase registrada, la misma oferta no genera esa bandera
        r2 = analizar(OFERTA_LIMPIA + ' Se requiere disponibilidad inmediata.')
        self.assertNotIn('Frase de alerta: «disponibilidad inmediata»', titulos(r2))

    def test_19_caso_an01_oferta_abusiva_completa(self):
        r = analizar('Desarrollador Junior\nSe requieren 3 años de experiencia previa en Java. '
                     'Contrato por prestación de servicios con horario fijo. Funciones de soporte técnico '
                     'N1/N2. Trabajo bajo presión y como una familia. Salario: $1.800.000 mensuales.')
        self.assertEqual(r['nivel'], 'Riesgo alto')
        self.assertGreaterEqual(r['porcentaje'], 50)
        for esperado in ('Experiencia oculta', 'Contrato sin garantías', 'Funciones cruzadas'):
            self.assertIn(esperado, titulos(r))
        self.assertIn(r['salario_estado'], ('bajo', 'limite'))


if __name__ == '__main__':
    unittest.main(verbosity=2)