"""
DevShield - Motor de análisis de ofertas laborales del sector TI.

Este módulo contiene SOLO la lógica del análisis (sin Flask ni base de datos),
de modo que sea modular, mantenible y fácil de probar (RFN-04).

Qué hace, en orden:
  1. Valida que el texto sea una oferta del sector TI (si no, la rechaza).
  2. Detecta el nivel (junior / semi-senior / senior) y el cargo concreto.
  3. Extrae el salario (cifras, rangos, "millones", SMMLV, USD) y lo compara
     con la banda del cargo detectado, no con un promedio general.
  4. Detecta señales de riesgo: experiencia oculta, funciones cruzadas,
     contrato, sobrecarga horaria, trabajo no remunerado, posible fraude, etc.
  5. Calcula el puntaje (0-100) y clasifica: Riesgo bajo / medio / alto.
"""
import os
import re
import unicodedata

# ---------------------------------------------------------------------------
# Parámetros (ajustables)
# ---------------------------------------------------------------------------
SMMLV_2026 = 1_750_905                       # Decreto 1469 de 2025
TRM_USD_COP = float(os.getenv('DEVSHIELD_TRM_USD', '4000'))  # conversión aproximada
BANDA_POR_DEFECTO = {'min': 2_000_000, 'max': 3_500_000}

MIN_CARACTERES = 40
MAX_CARACTERES = 20_000
ZONA_TITULO = 180            # caracteres iniciales donde suele estar el cargo
UMBRAL_ALTO = 50             # puntaje >= 50  -> Riesgo alto
UMBRAL_MEDIO = 20            # puntaje >= 20  -> Riesgo medio
# Reglas de clasificación (además de la suma de puntos):
#  - Una señal CRÍTICA (fraude, trabajo no remunerado, salario bajo el mínimo legal) => Riesgo alto.
#  - Una señal de severidad ALTA, aunque sea sola, => como mínimo Riesgo medio.
#  - Dos o más señales de severidad ALTA => Riesgo alto.

# Peso de cada señal en el puntaje (todo el ajuste de la calibración está aquí)
PESOS = {
    'exp_3': 15, 'exp_2': 10,
    'cruzadas_base': 12, 'cruzadas_extra': 8, 'cruzadas_tope': 24,
    'multi_3': 18, 'multi_4': 22, 'stack': 10,
    'contrato_encubierto': 18, 'prestacion': 10, 'contrato_no_esp': 8, 'comision': 20,
    'mensajeria': 6, 'urgencia': 5, 'req_incong': 8,
    'jornada': 10, 'sobrecarga_tope': 20, 'presion_2': 8, 'presion_4': 12,
    'convenir': 12,
    'sal_bajo_leve': 10, 'sal_bajo_medio': 20, 'sal_bajo_fuerte': 25, 'sal_inicia_bajo': 6,
    'sal_alto_sin_exp': 20, 'sal_alto': 10,
    'critica': 40,
}
STACK_MAX_JUNIOR = 7        # tecnologías distintas (no genéricas) a partir de las cuales un 'junior' es sospechoso
SALARIO_SOSPECHOSO_X = 1.4   # > 1.4 veces el máximo de la banda junior
# Las bandas del administrador son junior. Para semi-senior y senior se usa un múltiplo como referencia mínima.
NIVEL_FACTOR = {'semi': 1.5, 'senior': 2.2}
PATRONES_ADMIN_TOPE = 30     # máximo de puntos que pueden sumar las frases editadas por el administrador
CATEGORIAS_VALIDAS = ('salario', 'contrato', 'lenguaje')
EXPERIENCIAS_VALIDAS = ('JUNIOR', 'MID', 'SENIOR')   # nivel de experiencia de cada banda salarial
MONEDAS_VALIDAS = ('COP', 'USD')                       # las bandas en USD se convierten con la TRM
JORNADA_MAX_SEMANAL = 42     # Ley 2101 de 2021 (desde el 15 de julio de 2026)
JORNADA_ALERTA_HORAS = 48    # se alerta sobre este valor (el horario publicado suele incluir el almuerzo)


class TextoInvalidoError(ValueError):
    """El texto está vacío, es muy corto o demasiado largo."""


class OfertaNoTIError(ValueError):
    """El texto no corresponde a una oferta del sector TI."""


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------
def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y con variantes de escritura unificadas."""
    t = unicodedata.normalize('NFKD', texto)
    t = ''.join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"(?<=\d)['’´](?=\d)", '.', t)          # 3'500.000 -> 3.500.000
    t = t.replace('node.js', 'nodejs').replace('c sharp', 'c#')
    t = re.sub(r'front[\s-]?end', 'frontend', t)
    t = re.sub(r'back[\s-]?end', 'backend', t)
    t = re.sub(r'full[\s-]?stack', 'fullstack', t)
    t = re.sub(r'dev[\s-]ops', 'devops', t)
    t = re.sub(r'[ \t\r\f\v]+', ' ', t)
    return t


def _rx(patron: str) -> re.Pattern:
    """Compila un término como palabra completa (evita que 'java' calce en 'javascript')."""
    return re.compile(rf'(?<![a-z0-9])(?:{patron})(?![a-z0-9#+])')


def _hay(patron: str, texto: str) -> bool:
    return _rx(patron).search(texto) is not None


def _formato_cop(n) -> str:
    return '$' + f'{round(n):,}'.replace(',', '.')


def _a_float(s: str) -> float:
    return float(s.strip().replace(',', '.'))


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------
# Cargos TI (valen 2 puntos cada uno, distintos; si están en el título valen 2 más)
ROLES_TI = [
    # desarrollo
    r'desarrolladora?(?:es)?(?! (?:de|para) (?:negocios?|mercados?|clientes?|talento|personas|proyectos? '
    r'(?:inmobiliari\w+|de construccion)|inmobiliari\w+|comercial(?:es)?|organizacional|humano|de marca|de canal(?:es)?))',
    r'developers?', r'programadora?(?:es)?', r'coders?', r'analista programador', r'analista desarrollador',
    r'ingenier[oa] (?:de |en )?(?:software|sistemas|computacion|informatica|desarrollo|datos|pruebas|'
    r'redes|telecomunicaciones|infraestructura|ciberseguridad|seguridad informatica|seguridad de la informacion|'
    r'nube|cloud|devops|automatizacion de pruebas|soporte|ti|it|tecnologia|inteligencia artificial|ia|machine learning|ml)',
    r'(?:software|backend|frontend|fullstack|full stack|data|devops|cloud|platform|machine learning|ml|ai|qa|test|'
    r'security|network|systems?|site reliability|mobile|ios|android|embedded|firmware|web|game|blockchain|'
    r'solutions?|support|infrastructure|automation|application) (?:engineer|developer|analyst|scientist|architect|'
    r'administrator|specialist|programmer|consultant)s?',
    r'desarrollo (?:de software|web|movil|de aplicaciones|de apps|de videojuegos|de sistemas|de soluciones|backend|frontend)',
    r'frontend', r'backend', r'fullstack', r'devops', r'devsecops', r'sre', r'mlops', r'dataops',
    r'qa', r'tester', r'sdet', r'(?:pruebas|testing) (?:de software|funcionales?|automatizadas?|de rendimiento|manuales)',
    r'automatizacion de pruebas',
    # análisis / datos / negocio TI
    r'analista (?:de |en )?(?:pruebas|datos|sistemas|calidad de software|calidad de sistemas|seguridad informatica|'
    r'seguridad de la informacion|ciberseguridad|bi|inteligencia de negocios?|soporte|soporte tecnico|'
    r'infraestructura|monitoreo|redes|ti|it|tecnologia|requerimientos|funcional|desarrollo|aplicaciones|'
    r'soc|vulnerabilidades|bases de datos|integracion|procesos ti)',
    r'analista (?:qa|bi|etl|soc|noc)', r'analista funcional(?: (?:sap|erp|ti|de sistemas))?',
    r'data (?:analyst|engineer|scientist|steward|architect)', r'(?:cientific[oa]|ingenier[oa]|analista|arquitect[oa]) de datos',
    r'(?:desarrollador|ingeniero|analista)\w* (?:de )?bi', r'business intelligence', r'inteligencia de negocios',
    r'ingenier[oa] de ia|ingenier[oa] de inteligencia artificial', r'prompt engineer',
    # infraestructura / redes / seguridad
    r'dba', r'administrador(?:a)? (?:de |en )?(?:bases de datos|sistemas|redes|infraestructura|servidores|plataformas?|'
    r'nube|ti|it|seguridad informatica|aplicaciones|sistemas operativos|tecnologia)',
    r'sysadmin', r'netadmin', r'noc', r'soc',
    r'arquitect[oa] (?:de |en )?(?:software|soluciones|datos|nube|cloud|ti|it|infraestructura|sistemas|aplicaciones|'
    r'tecnologia|integracion|seguridad)',
    r'ciberseguridad', r'seguridad informatica', r'seguridad de la informacion', r'pentest\w*', r'ethical hack\w*',
    r'hacking etico', r'infosec', r'cybersecurity',
    r'tecnico (?:en|de) (?:redes|sistemas|computo|computadores|computadoras|informatica|soporte|telecomunicaciones|'
    r'mantenimiento de (?:equipos de computo|computadores|computadoras|pcs?|equipos informaticos)|tecnologia|ti|it)',
    r'tecnologo (?:en )?(?:sistemas|analisis y desarrollo|desarrollo de software|informatica|redes|'
    r'gestion de redes|telecomunicaciones)',
    r'auxiliar (?:de |en )?(?:sistemas|ti|it|informatica|soporte|computo)',
    r'asistente (?:de |en )?(?:sistemas|ti|it|informatica|soporte tecnico)',
    r'soporte (?:tecnico|informatico|it|ti|n[123]|nivel [123]|funcional|a usuarios?|en sitio|remoto|de aplicaciones|'
    r'de infraestructura|de software|tic)',
    r'help ?desk', r'service ?desk', r'mesa de (?:ayuda|servicios?)', r'centro de computo',
    r'ingenier[oa] de soporte', r'especialista (?:en|de) (?:soporte|ti|it|tecnologia|sistemas|ciberseguridad)',
    # producto / gestión TI / diseño digital
    r'scrum master', r'product owner', r'product manager', r'agile coach', r'(?:lider|jefe|gerente|coordinador|director|'
    r'gestor) (?:tecnico|de (?:tecnologia|ti|it|sistemas|desarrollo|ingenieria|infraestructura|proyectos de (?:ti|software|tecnologia)|'
    r'proyectos tecnologicos|seguridad informatica|soporte|tecnologia de la informacion|innovacion tecnologica))',
    r'tech lead', r'team lead', r'cto', r'cio', r'ciso', r'project manager (?:de )?(?:ti|it|software)',
    r'ux ?/ ?ui', r'ui ?/ ?ux', r'ux (?:designer|researcher|writer)', r'ui designer', r'disenador[a]? (?:de )?(?:ux|ui|producto digital|web|interfaces|de experiencia de usuario)',
    r'ux', r'product designer', r'technical writer', r'developer advocate',
    # plataformas / consultoría TI
    r'consultor[a]? (?:de |en )?(?:sap|erp|oracle|salesforce|ti|it|tecnologia|servicenow|power bi|bi|datos|sistemas|'
    r'ciberseguridad|cloud|funcional sap|tecnico|software|implementacion)',
    r'abap', r'implementador (?:de )?(?:erp|software|sistemas)', r'integrador de sistemas',
    r'(?:desarrollador|developer|consultor|analista|ingeniero|administrador)\w* (?:sap|salesforce|servicenow|power apps|power platform|rpa|uipath|sharepoint|dynamics|odoo|netsuite)',
    r'rpa', r'low[- ]?code', r'no[- ]?code', r'game ?dev\w*',
    r'(?:ingenier[oa]|especialista|analista|tecnico)\w* (?:de |en )?(?:blockchain|iot|embebidos?|embedded|firmware|videojuegos?)',
    r'tecnologias? de la informacion', r'\btic\b', r'tecnologia informatica',
]

# Cargos claramente NO TI. Si el título trae uno de estos y ninguno de TI, se rechaza la oferta.
ROLES_NO_TI = [
    r'vendedor\w*', r'asesor\w* (?:comercial(?:es)?|de ventas|de servicio|de cartera|de credito|financier\w+|de seguros|'
    r'inmobiliari\w+|telefonic\w+|de call center|de atencion|de cobranza|de mercadeo|de campo)',
    r'ejecutiv[oa]\w* (?:comercial(?:es)?|de ventas|de cuentas?|de desarrollo de negocios|de atencion|de servicio)',
    r'gerente (?:comercial|de ventas|de mercadeo|de marketing|de operaciones|financier\w+|de produccion|de tienda|'
    r'de sucursal|administrativ\w+|de recursos humanos|de talento humano)',
    r'jefe de (?:ventas|produccion|bodega|compras|cartera|tienda|almacen|calidad|logistica|mantenimiento)',
    r'desarrollo de negocios', r'desarrollador de negocios', r'gestor comercial', r'promotor\w*', r'impulsador\w*',
    r'cociner[oa]', r'chef', r'mesero', r'mesera', r'barista', r'bartender', r'panader[oa]', r'repostero',
    r'auxiliar (?:de |en )?(?:cocina|bodega|enfermeria|contabilidad|contable|servicios generales|logistica|odontologia|'
    r'farmacia|administrativ[oa]|de ventas|de almacen|de produccion|de despachos|de cartera|de nomina|de recursos humanos)',
    r'asistente (?:administrativ[oa]|contable|de gerencia|de nomina|de recursos humanos|comercial|de ventas|de bodega|de compras)',
    r'conductor\w*', r'mensajer[oa]', r'domiciliari[oa]', r'operari[oa]', r'soldador\w*', r'albanil', r'ayudante',
    r'vigilante', r'escolta', r'guarda de seguridad', r'aseador\w*', r'servicios generales', r'jardiner[oa]',
    r'enfermer[oa]', r'medic[oa] (?:general|especialista)', r'odontolog\w+', r'fisioterapeuta', r'nutricionista',
    r'psicolog[oa]', r'terapeuta', r'bacteriolog[oa]', r'quimico farmaceutico', r'veterinari[oa]',
    r'docente', r'profesor\w*', r'tutor de (?!software)\w+', r'licenciad[oa]',
    r'abogad[oa]', r'contador\w*', r'revisor fiscal', r'secretari[oa]', r'recepcionista', r'cajer[oa]',
    r'call center', r'telemercadeo', r'telemercaderista', r'community manager', r'publicista', r'periodista', r'redactor(?! tecnico)',
    r'fotograf[oa]', r'camar[oa]grafo', r'estilista', r'peluquer[oa]', r'barbero', r'manicurista',
    r'disenador[a]? (?:grafic[oa]|industrial|de modas|de interiores|publicitari[oa]|de moda|de joyas|textil)',
    r'electricista', r'mecanic[oa]', r'plomer[oa]', r'carpinter[oa]', r'tecnico (?:electricista|electric[oa]|mecanic[oa]|'
    r'electronic[oa]|de refrigeracion|de aire acondicionado|biomedic[oa]|industrial|de mantenimiento (?!de (?:equipos de computo|computadores|computadoras|pcs?)))',
    r'ingenier[oa] (?:civil|industrial|ambiental|quimic[oa]|mecanic[oa]|electric[oa]|electronic[oa]|agronom[oa]|agroindustrial|'
    r'de petroleos|de alimentos|de minas|biomedic[oa]|de produccion|de procesos|de calidad|de mantenimiento|forestal|'
    r'de transporte|de materiales|de obra|geologo|geologica|pesquero|textil)',
    r'arquitect[oa](?! (?:de|en) (?:software|soluciones|datos|nube|cloud|ti|it|infraestructura|sistemas|aplicaciones|tecnologia|integracion|seguridad))',
    r'topograf[oa]', r'agronom[oa]', r'zootecnista', r'residente de obra', r'maestro de obra',
    r'analista (?:de )?(?:credito|cartera|financier[oa]|nomina|compras|mercadeo|marketing|produccion|costos|tesoreria|'
    r'contable|recursos humanos|seleccion|talento humano|logistic[oa]|de calidad(?! de software)|presupuesto|riesgo(?: crediticio)?|'
    r'tributari[oa]|comercial|cobranza|facturacion|inventarios?|trade marketing)',
    r'coordinador[a]? (?:de )?(?:logistica|ventas|produccion|calidad|talento humano|recursos humanos|mercadeo|compras|bodega)',
    r'talento humano', r'recursos humanos',
]

# Tecnologías (valen 1 punto cada una, distintas)
TECNOLOGIAS = {
    # lenguajes
    'python': r'python', 'java': r'java', 'javascript': r'javascript|js|ecmascript', 'typescript': r'typescript|ts',
    'csharp': r'c#|csharp', 'cpp': r'c\+\+', 'golang': r'golang', 'rust': r'rust(?:lang)?', 'ruby': r'ruby|rails',
    'php': r'php', 'kotlin': r'kotlin', 'swift': r'swift(?:ui)?', 'scala': r'scala', 'dart': r'dart',
    'perl': r'perl', 'cobol': r'cobol', 'abap': r'abap', 'vbnet': r'vb\.net|visual basic', 'bash': r'bash|shell script\w*',
    'powershell': r'powershell', 'sql': r'sql(?: server)?|t-sql|mysql|postgres(?:ql)?|plsql|pl/sql|oracle|mariadb|sqlite',
    'rlang': r'lenguaje r', 'matlab': r'matlab',
    # web / móvil
    'react': r'react(?:js| native)?', 'angular': r'angular(?:js)?', 'vue': r'vue(?:js)?', 'svelte': r'svelte',
    'nextjs': r'next\.?js|nuxt(?:js)?', 'node': r'nodejs|node', 'express': r'expressjs|nestjs',
    'django': r'django', 'flask': r'flask', 'fastapi': r'fastapi', 'spring': r'spring(?: boot| cloud)?',
    'hibernate': r'hibernate', 'laravel': r'laravel|symfony|codeigniter', 'dotnet': r'\.net|dotnet|asp\.net|net core',
    'flutter': r'flutter', 'android': r'android', 'ios': r'ios', 'xamarin': r'xamarin|ionic|maui',
    'jquery': r'jquery|bootstrap|tailwind', 'html': r'html5?', 'css': r'css3?|sass|scss',
    'wordpress': r'wordpress|woocommerce|shopify|magento|prestashop', 'unity': r'unity(?: 3d)?|unreal(?: engine)?',
    # datos
    'mongodb': r'mongodb|mongo', 'redis': r'redis|elasticsearch|cassandra|dynamodb|firebase|neo4j|couchbase',
    'dw': r'snowflake|bigquery|redshift|data ?warehouse|databricks|hive', 'spark': r'spark|hadoop|kafka|airflow|dbt|nifi',
    'powerbi': r'power ?bi|tableau|looker|qlik|dax|metabase', 'pandas': r'pandas|numpy|scikit[- ]?learn|jupyter',
    'ml': r'machine learning|aprendizaje automatico|deep learning|tensorflow|pytorch|nlp|llms?|mlops|inteligencia artificial',
    'etl': r'etl|elt|ssis|talend|informatica powercenter',
    # nube / devops / infraestructura
    'docker': r'docker', 'kubernetes': r'kubernetes|k8s|helm|openshift', 'aws': r'aws|amazon web services',
    'azure': r'azure', 'gcp': r'gcp|google cloud', 'terraform': r'terraform|ansible|puppet|cloudformation',
    'cicd': r'ci ?/ ?cd|jenkins|gitlab ci|github actions|argocd|circleci|azure devops', 'linux': r'linux|unix|ubuntu|centos|red hat|debian',
    'nginx': r'nginx|apache|tomcat|iis', 'monitor': r'prometheus|grafana|datadog|new relic|splunk|zabbix|nagios|elk',
    'vmware': r'vmware|hyper-?v|virtualizacion|proxmox', 'windows': r'windows(?: server)?|active directory|office 365|microsoft 365|exchange|sharepoint',
    'redes': r'tcp ?/ ?ip|cisco|ccna|ccnp|vpn|vlan|firewalls?|fortinet|fortigate|palo alto|mikrotik|dns|dhcp|routers?|switches',
    'seguridad': r'siem|owasp|iso ?27001|pentest\w*|nessus|burp|vulnerabilidades|hardening|kali linux',
    'itil': r'itil|cobit|servicenow|glpi|remedy|mesa de ayuda|help ?desk',
    # QA
    'selenium': r'selenium', 'cypress': r'cypress|playwright|appium|cucumber|testng|junit|pytest|jmeter|soapui|katalon|robot framework',
    'postman': r'postman|swagger',
    # herramientas y metodologías
    'git': r'git(?:hub|lab)?|bitbucket', 'jira': r'jira|confluence|trello|azure boards', 'scrum': r'scrum|kanban|agil(?:e|es)?',
    'api': r'apis?|rest(?:ful)?|graphql|soap|grpc|json|xml|webhooks?', 'microservicios': r'microservicios?|microservices?|arquitectura de software|soa',
    'figma': r'figma|sketch|adobe xd|invision|prototip\w+|wireframes?|design system\w*',
    'erp': r'sap|erp|salesforce|dynamics|oracle ebs|odoo|netsuite|servicenow|power apps|power automate|rpa|uipath|automation anywhere|blue prism',
    'basesdatos': r'bases? de datos|base de datos|dwh', 'software': r'software', 'programacion': r'programacion',
    'nube': r'nube|cloud', 'iot': r'iot|arduino|raspberry|embebid\w+|plc',
}
# Tecnologías demasiado genéricas: cuentan para validar el sector, pero no para el "stack desproporcionado"
TECNOLOGIAS_GENERICAS = {'api', 'git', 'html', 'css', 'sql', 'software', 'programacion', 'basesdatos', 'scrum',
                         'jira', 'itil', 'windows', 'redes', 'nube', 'microservicios', 'figma', 'postman', 'linux',
                         'seguridad', 'monitor', 'nginx', 'iot', 'jquery'}

# Familias de cargo -> patrones que las delatan y pistas en el nombre del rol en BD
FAMILIAS = {
    'java': {
        'patrones': [r'java', r'spring(?: boot)?', r'hibernate', r'jsp'],
        'pistas': ['java'],
    },
    'frontend': {
        'patrones': [r'frontend', r'react(?:js)?', r'angular(?:js)?', r'vue(?:js)?',
                     r'html5?', r'css3?', r'javascript', r'typescript', r'ui ?/ ?ux|ux ?/ ?ui'],
        'pistas': ['frontend', 'front'],
    },
    'backend': {
        'patrones': [r'backend', r'python', r'django', r'flask', r'fastapi', r'nodejs',
                     r'php', r'laravel', r'c#', r'\.net|dotnet', r'golang', r'ruby|rails',
                     r'microservicios', r'apis?|rest(?:ful)?'],
        'pistas': ['backend', 'back'],
    },
    'qa': {
        'patrones': [r'qa', r'tester', r'testing', r'pruebas(?: de software)?',
                     r'calidad de software', r'selenium', r'cypress', r'automatizacion de pruebas'],
        'pistas': ['qa', 'tester', 'test', 'prueba'],
    },
    'datos': {
        'patrones': [r'analista de datos', r'data (?:analyst|engineer|scientist)',
                     r'cientifico de datos', r'power ?bi', r'tableau', r'pandas',
                     r'business intelligence', r'etl', r'machine learning'],
        'pistas': ['dato', 'data', 'bi'],
    },
    'soporte': {
        'patrones': [r'soporte (?:tecnico|informatico|it|ti|n[123]|nivel [123]|a usuarios?|en sitio|remoto|funcional)',
                     r'help ?desk', r'service ?desk', r'mesa de (?:ayuda|servicios?)', r'active directory',
                     r'office 365|microsoft 365', r'tecnico (?:en|de) (?:sistemas|soporte|computo|informatica|redes)',
                     r'auxiliar de (?:sistemas|ti|it|soporte)', r'mantenimiento (?:preventivo|correctivo)?'
                     r'(?: y correctivo)? de (?:equipos|computadores|computadoras|pcs?)', r'usuarios? finales?',
                     r'itil', r'glpi|servicenow|remedy'],
        'pistas': ['soporte', 'support', 'help'],
    },
    'devops': {
        'patrones': [r'devops', r'docker', r'kubernetes|k8s', r'ci ?/ ?cd', r'jenkins',
                     r'terraform', r'aws', r'azure', r'sre', r'infraestructura'],
        'pistas': ['devops', 'infra', 'cloud', 'sre'],
    },
    'mobile': {
        'patrones': [r'android', r'kotlin', r'swift', r'flutter', r'react native', r'ios'],
        'pistas': ['mobile', 'movil', 'android', 'ios'],
    },
}

# Palabras del nombre de un rol de la BD que NO identifican el cargo
PALABRAS_GENERICAS = {
    'junior', 'jr', 'dev', 'developer', 'desarrollador', 'desarrolladora', 'ingeniero',
    'analista', 'tecnico', 'de', 'del', 'la', 'el', 'y', 'e', 'en', 'ti', 'it', 'ii', 'i',
    'nivel', 'trainee', 'practicante', 'senior', 'sr', 'semi', 'ssr',
}

# Nivel
RX_SEMI = re.compile(r'semi[\s-]?senior|semi[\s-]?sr\b|\bssr\b|\bmid[\s-]?level\b|\bmid\b')
RX_SENIOR = re.compile(r'\bsenior\b|\bsr\.?(?=\s|$|[,;:)])|\blider tecnico\b|\btech lead\b|\blead\b|'
                       r'\barquitecto\b|\bstaff\b|\bprincipal\b')
RX_JUNIOR = re.compile(r'\bjunior\b|\bjr\.?(?=\s|$|[,;:)])|\btrainee\b|\bentry[\s-]?level\b|'
                       r'\bsin experiencia\b|\bprimer empleo\b|\brecien egresad|\bpracticante\b|'
                       r'\bpasante\b|\baprendiz\b|\bpasantia\b')
RX_PRACTICA = re.compile(r'practicante|pasante|pasantia|aprendiz|practica (?:universitaria|profesional|laboral)|'
                         r'etapa (?:lectiva|productiva)')

# Contrato
RX_PRESTACION = re.compile(r'prestacion de servicios|contrato de servicios|orden de servicios|'
                           r'\bcontratista\b|\bcontractor\b|\bops\b|\bhonorarios\b|\bfreelance\b|\bindependiente\b')
RX_SUBORDINACION = re.compile(
    r'horario (?:fijo|establecido|de oficina)|de lunes a viernes|lunes a viernes|\b8 horas\b|'
    r'tiempo completo|jornada completa|presencial|exclusividad|dedicacion exclusiva|'
    r'cumplir (?:con )?(?:el )?horario|control de horario|reportar(?:as)? a|reporte diario')
RX_CONTRATO_FORMAL = re.compile(
    r'termino indefinido|contrato indefinido|termino fijo|obra o labor|contrato laboral|'
    r'contrato directo|prestaciones de ley|prestaciones sociales|seguridad social|'
    r'afiliacion a (?:eps|arl|caja)|nomina')
RX_SIN_PRESTACIONES = re.compile(r'sin prestaciones(?: sociales| de ley)?|no incluye prestaciones|'
                                 r'no (?:tiene|ofrece|cuenta con) prestaciones|sin seguridad social')
RX_CONTRATO_MENCION = re.compile(r'\bcontrato\b|contratacion|vinculacion')
RX_CONTRATO_VAGO = re.compile(r'(?:tipo de )?contrato\s*:?\s*(?:a convenir|por definir|por confirmar|no especificado|'
                              r'segun (?:el )?perfil)')
RX_VAGO = re.compile(r'a convenir|segun experiencia|de acuerdo (?:a|con) (?:la |su |tu )?experiencia|'
                     r'segun (?:el )?perfil|acorde al mercado|salario competitivo|'
                     r'salario atractivo|negociable|a negociar')

# Trabajo no remunerado
RX_NO_PAGO = re.compile(r'ad[\s-]?honorem|no remunerad[oa]s?|sin remuneracion|sin salario|'
                        r'trabajo voluntario|voluntariado|sin retribucion economica|'
                        r'sin pago|no se paga|no tiene remuneracion')
# Remuneración solo variable
RX_SOLO_COMISION = re.compile(r'(?:100 ?%|solo|unicamente|exclusivamente|solamente)\s+(?:por\s+)?comisiones?|'
                              r'sin salario fijo|sin sueldo fijo|pago por (?:resultados|produccion)')
RX_GANANCIAS_IRREALES = re.compile(r'ganancias? (?:ilimitadas?|garantizadas?)|ingresos? (?:ilimitados?|garantizados?)|'
                                   r'gana(?:r)? dinero facil')

# Fraude
RX_FRAUDE_COBRO = [
    re.compile(r'inversion inicial'),
    re.compile(r'(?:pago|cobro|costo|cuota|valor)\s+(?:de\s+(?:la\s+|el\s+|tu\s+)?)?'
               r'(?:inscripcion|capacitacion|curso|bootcamp|kit|membresia|certificacion|uniforme)\b'),
    re.compile(r'debes? pagar|tendras que pagar|(?:consign|transferi|deposit)\w*\s.{0,30}(?:dinero|plata|valor|cuota)'),
    re.compile(r'(?:debes|deberas|tienes que|tendras que|es necesario|se debe|se requiere que)\s+'
               r'(?:consignar|pagar|transferir|depositar|cancelar|abonar)'),
    re.compile(r'poliza de (?:seguro|cumplimiento)|verificacion de datos|certificado de ingreso|'
               r'expedicion del certificado'),
    re.compile(r'(?:realizar|hacer|efectuar)\s+(?:un\s+|el\s+)?(?:pago|abono|deposito|consignacion)'),
    re.compile(r'(?:pago|cobro|abono|consignacion|deposito)\s+de\s+\$\s?\d[\d.,]*\s+por\s+concepto'),
    re.compile(r'por concepto de (?:certificacion|inscripcion|capacitacion|curso|kit|poliza|verificacion|'
               r'afiliacion|uniforme|material|induccion)'),
    re.compile(r'(?:cancelar|pagar|consignar|abonar)\s+(?:el\s+)?(?:valor|monto|suma)\s+de'),
    re.compile(r'descuent\w+ (?:del|de su|de tu) (?:salario|sueldo|pago).{0,30}(?:capacitacion|curso|formacion|equipo)'),
]
RX_FRAUDE_NEGATIVO = re.compile(r'(?:empresa|nosotros|compania)\s+(?:asume|cubre|paga|financia|patrocina)|'
                                r'sin costo|gratuit[oa]|cubiert[oa]|financiad[oa]')
RX_FRAUDE_DATOS = [
    re.compile(r'(?:envi|compart|manda)\w*\s.{0,30}(?:clave|contrasena|tarjeta|datos bancarios|cuenta bancaria|\bpin\b)'),
    re.compile(r'(?:clave|contrasena|tarjeta|datos bancarios|cuenta bancaria|\bpin\b).{0,30}(?:enviar|compartir|mandar)'),
]
RX_MENSAJERIA_ACTIVA = re.compile(r'(?:escribe|escribenos|escribir|contacta|contactanos|comunicate|manda|envia)\w*\s.{0,40}'
                                  r'(?:whatsapp|telegram)')
RX_CANAL_FORMAL = re.compile(r'@|linkedin|www\.|https?://|portal|correo')
RX_URGENCIA = re.compile(r'escribe(?:nos)? inmediatamente|cupos? limitados?|ultimos cupos|solo por hoy|'
                         r'responde (?:ya|hoy)|aprovecha ahora')
RX_REQ_INCONGRUENTES = re.compile(r'manejo basico de (?:computador\w*|pc|ofimatica|office)|'
                                  r'no se requiere experiencia en programacion|'
                                  r'sin (?:conocimientos?|experiencia) (?:previos? )?(?:de|en) programacion')
RX_SIN_EXPERIENCIA = re.compile(r'no se requiere experiencia|no requiere experiencia|sin experiencia|'
                                r'no necesitas experiencia|te capacitamos|nosotros te capacitamos')
RX_SOLO_MENSAJERIA = re.compile(r'(?:unicamente|solo|solamente|exclusivamente)\s+(?:por\s+)?(?:whatsapp|telegram)|'
                                r'escribe(?:nos)? al (?:whatsapp|telegram)')

# Sobrecarga horaria
HORARIOS_ABUSIVOS = [
    (re.compile(r'24 ?/ ?7|24x7|24 horas'), '24/7', 15),
    (re.compile(r'disponibilidad (?:total|permanente|completa|24)'), 'disponibilidad total', 10),
    (re.compile(r'(?:fines? de semana|sabados?|domingos?|festivos?).{0,30}(?:obligatori|disponib|laborar|trabajar)'),
     'trabajo en fines de semana/festivos', 6),
    (re.compile(r'horas? extras? (?:sin|no) (?:remuner|pag)|sin pago de horas extras?'),
     'horas extra sin pago', 15),
    (re.compile(r'sin horario|horario (?:abierto|ilimitado)'), 'sin horario definido', 15),
    (re.compile(r'\b(?:10|11|12)\s*horas diarias'), 'jornadas de 10+ horas', 12),
    (re.compile(r'\bon[\s-]?call\b|guardias? (?:sin|no)'), 'guardias sin compensación', 8),
]

# Lenguaje de presión / cultura tóxica (se requieren >= 2 para marcarlo)
LENGUAJE_PRESION = [
    r'bajo presion', r'(?:ponerse|ponte|poner) la camiseta', r'somos una familia',
    r'como una familia', r'alto compromiso', r'compromiso total', r'multitarea',
    r'multitasking', r'\btodero\b', r'hacer de todo', r'ritmo acelerado',
    r'cultura de alto rendimiento', r'tolerancia a la frustracion',
    r'sin limite de horario', r'mentalidad de startup', r'no (?:es )?un 9 a 5',
]

# Áreas técnicas: una oferta que mezcla 3 o más es un perfil "todoterreno"
AREAS_TI = {
    'desarrollo': (r'frontend|backend|fullstack|java|spring(?: boot)?|angular|react|vue|python|django|nodejs|'
                   r'php|laravel|\.net|desarrolladora?(?:es)?|programadora?(?:es)?|desarrollo (?:de )?(?:software|aplicaciones|apps|web|sistemas)', 'desarrollo de software'),
    'infra': (r'devops|docker|kubernetes|k8s|ci ?/ ?cd|terraform|jenkins|infraestructura|aws|azure|servidores',
              'infraestructura / DevOps'),
    'soporte': (r'soporte tecnico|mesa de ayuda|help ?desk|soporte n[123]|soporte informatico',
                'soporte técnico'),
    'diseno': (r'ux ?[-/] ?ui|ui ?[-/] ?ux|prototip\w+|figma|diseno de interfaces|diseno ux',
               'diseño UX/UI'),
    'qa': (r'\bqa\b|tester|automatizacion de pruebas', 'QA / pruebas'),
    'datos': (r'analisis de datos|power ?bi|tableau|etl|data analyst|ciencia de datos', 'análisis de datos'),
    'mobile': (r'plataforma movil|apps? moviles?|android|ios|flutter|kotlin|swift|react native',
               'desarrollo móvil'),
}
# Familias de BD que corresponde consultar para cada área técnica
AREA_A_FAMILIAS = {
    'desarrollo': ['java', 'frontend', 'backend'], 'infra': ['devops'], 'soporte': ['soporte'],
    'qa': ['qa'], 'datos': ['datos'], 'mobile': ['mobile'],
}

# Funciones ajenas al cargo
AREAS_CRUZADAS = {
    'soporte': (r'soporte tecnico|mesa de ayuda|help ?desk|soporte n[123]|soporte (?:a|al) usuarios?',
                'soporte técnico'),
    'cliente': (r'atencion al cliente|servicio al cliente|call center|telemercadeo|servicio al usuario',
                'atención al cliente'),
    'ventas': (r'(?:realizar|hacer|apoyar|gestionar|cerrar)\s+(?:las\s+)?ventas|apoyo (?:en|a|de) (?:las )?ventas|apoyo comercial|'
               r'labores comerciales|venta de (?:productos|servicios|seguros)|prospeccion de clientes|cobranza',
               'ventas / cobranza'),
    'marketing': (r'community manager|manejo de redes sociales|diseno grafico|edicion de video|'
                  r'fotografia|marketing digital|disenar piezas',
                  'marketing / diseño gráfico'),
    'admin': (r'labores administrativas|tareas administrativas|digitacion|facturacion|contabilidad|'
              r'auxiliar administrativ[oa]|archivo fisico',
              'tareas administrativas / contables'),
    'hardware': (r'mantenimiento de (?:equipos|computadores|computadoras|impresoras|pcs?)|'
                 r'reparacion de (?:equipos|computadores|pcs?)',
                 'mantenimiento de equipos'),
    'instalaciones': (r'cableado|instalacion de (?:camaras|puntos|antenas)|camaras de seguridad|'
                      r'instalacion electrica|alarmas',
                      'instalación de cámaras / cableado'),
}

# Aviso de contexto "no salarial" para cifras
RX_NO_SALARIO_ANTES = re.compile(r'bono|bonificacion|auxilio|comision|incentivo|descuento|multa|inversion|'
                                 r'deposito|garantia|cobro|capacitacion|costo|cuota|facturacion|presupuesto|capital|'
                                 r'ventas? (?:de|por|mensuales?|anuales?)')
RX_NO_SALARIO_DESPUES = re.compile(r'^\s*(?:de\s+)?(?:usuarios|clientes|empleados|colaboradores|personas|'
                                   r'visitas|transacciones|registros|descargas|ventas|facturacion|euros? en)')
RX_CLAVE_SALARIO = re.compile(r'salari|sueldo|remuner|honorari|\bpago\b|pagamos|ofrecemos|asignacion|devengar|'
                              r'compensacion|ingreso|mensual|\bcop\b|pesos|smmlv|\$')
RX_POR_PROYECTO = re.compile(r'por proyecto|pago (?:unico|por entrega|por tarea)|valor (?:total )?del proyecto|'
                             r'presupuesto (?:del|para el) proyecto|por entregables?|pago unico')
RX_USD = re.compile(r'\busd\b|us\$|u\$s|dolar|\bdls\b')

_N = r'\d+(?:[.,]\d+)?'
RX_MILL_RANGO = re.compile(rf'({_N})\s*(?:millones?|mm)?\s*(?:-|–|—|a|y|hasta|al)\s*({_N})\s*millones?\b')
RX_MILL = re.compile(rf'({_N})\s*millones?\b')
RX_MIL_MONEDA = re.compile(rf'({_N})\s*mil\b(?=\s*(?:dolares|usd|pesos|cop|mensuales))')
RX_SMMLV_RANGO = re.compile(rf'({_N})\s*(?:-|–|—|a|y)\s*({_N})\s*(?:smmlv|smlmv|smlv|salarios? minimos?)')
RX_SMMLV = re.compile(rf'({_N})\s*(?:smmlv|smlmv|smlv|salarios? minimos?)')
RX_M_ABREV = re.compile(rf'(\$\s*)?({_N})\s*(?:m|mm|mill)\b(?![a-z0-9])')
RX_M_RANGO = re.compile(rf'({_N})\s*(?:m|mm)?\s*(?:-|–|—|a|y|hasta|al|to)\s*\$?\s*({_N})\s*(?:m|mm)\b(?![a-z0-9])')
RX_K_RANGO = re.compile(rf'(?:\$|usd|us\$)?\s*({_N})\s*k?\s*(?:-|–|—|a|to)\s*(?:\$|usd|us\$)?\s*({_N})\s*k\b(?![a-z0-9])')
RX_HORA = re.compile(rf'(?:(usd|us\$|\$|cop)\s*)?({_N})\s*(usd|dolares|cop|pesos)?\s*(?:/|por|per|x)\s*(?:hora|hour|hr|h)\b')
RX_K_ABREV = re.compile(rf'(\$\s*|usd\s*)?({_N})\s*k\b(?![a-z0-9])')
RX_MILL_PALABRA = re.compile(r'\b(un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+millones?'
                             r'(?:\s+(y medio|quinientos mil|y quinientos mil|doscientos mil|trescientos mil|cuatrocientos mil))?')
EXTRA_PALABRA = {'y medio': .5, 'quinientos mil': .5, 'y quinientos mil': .5, 'doscientos mil': .2,
                 'trescientos mil': .3, 'cuatrocientos mil': .4}
HORAS_MES = 160                                 # para convertir tarifas por hora a mensual
RX_SM_SOLO = re.compile(r'(?<![\d.,])\s*salario minimo(?: legal(?: mensual)?(?: vigente)?)?')
RX_MONTO = re.compile(r'(?<![\d.,])(\d{1,3}(?:[.,]\d{3}){1,3}|\d{4,9})(?![\d])')
RX_UNION = re.compile(r'^\s*(?:cop|pesos|usd|dolares)?\s*(?:-|–|—|a|y|hasta|al|to)\s*(?:\$|cop|usd|us\$)?\s*$')

NUM_PALABRA = {'un': 1, 'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5,
               'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10}
RX_ANOS = re.compile(
    r'(?<![\d.,])(\d{1,2}|un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s*'
    r'(?:\+|\s?o mas)?\s*(?:(?:-|a|y|hasta|o)\s*(\d{1,2}|dos|tres|cuatro|cinco|seis)\s*)?'
    r'(anos?|years?|meses)\b')


# ---------------------------------------------------------------------------
# Bandas salariales
# ---------------------------------------------------------------------------
def promedio_bandas(roles):
    """Promedio general de las bandas (solo como último recurso)."""
    if not roles:
        return dict(BANDA_POR_DEFECTO)
    return {
        'min': round(sum(r['min'] for r in roles) / len(roles)),
        'max': round(sum(r['max'] for r in roles) / len(roles)),
    }


# ---------------------------------------------------------------------------
# Analizador
# ---------------------------------------------------------------------------
class AnalizadorDevShield:
    """
    texto : texto completo de la vacante.
    roles : lista de dicts [{'rol': str, 'min': int, 'max': int,
            'experiencia': 'JUNIOR'|'MID'|'SENIOR', 'moneda': 'COP'|'USD'}, ...]
            (bandas salariales que administra el administrador; 'experiencia' y 'moneda'
            son opcionales: por defecto JUNIOR y COP).
    patrones : lista de dicts [{'frase': str, 'categoria': 'contrato'|'lenguaje'|'salario',
               'peso': int}, ...] con las frases de alerta ACTIVAS que administra el
               administrador (opcional).
    """

    def __init__(self, texto: str, roles: list, patrones: list = None):
        if not isinstance(texto, str) or not texto.strip():
            raise TextoInvalidoError('Texto vacío. Pega el texto completo de la oferta laboral.')
        if len(texto.strip()) < MIN_CARACTERES:
            raise TextoInvalidoError('El texto es demasiado corto para analizarlo. '
                                     'Pega la oferta completa (cargo, requisitos y condiciones).')
        if len(texto) > MAX_CARACTERES:
            raise TextoInvalidoError('El texto es demasiado largo (máximo 20.000 caracteres).')

        self.roles = self._normalizar_roles(roles or [])
        self.experiencia_mercado = 'JUNIOR'   # nivel de la banda con la que se compara
        self.banda_de_nivel = False           # True si la banda es específica del nivel (MID/SENIOR)
        self.patrones = patrones or []
        self.t = normalizar(texto).strip()
        self.titulo = self._extraer_titulo()
        self.banderas = []
        self.puntaje = 0
        self.hay_critica = False

        # Resultados intermedios
        self.nivel = 'no_indicado'
        self.nivel_inferido = False
        self.rol_detectado = None
        self.mercado = None
        self.salario = None            # dict con min / max / texto
        self.anos_exigidos = 0
        self.estado_salario = 'no_detectado'
        self.veredicto_salario = 'No se detectó un salario numérico en la oferta.'
        self.avisos = []
        self.es_practica = bool(RX_PRACTICA.search(self.t))

    # ------------------------------------------------------------------ util
    def _extraer_titulo(self) -> str:
        """Zona donde suele estar el cargo: las primeras líneas cortas (o los primeros caracteres)
        más lo que venga después de 'cargo:' / 'buscamos un ...'."""
        lineas = [l.strip() for l in self.t.split('\n') if l.strip()]
        cortas = [l for l in lineas[:3] if len(l) <= 110]
        if len(lineas) > 1 and cortas:
            base = ' | '.join(cortas)
        else:
            base = self.t[:ZONA_TITULO]
        partes = [base]
        for rx in (r'(?:cargo|vacante|puesto|posicion|rol|oferta|perfil)\s*[:\-]\s*([^\n]{0,120})',
                   r'(?:buscamos|busca|se busca|se requiere|se necesita|se necesitan|requerimos|necesitamos|'
                   r'estamos buscando|vacante para)\s+(?:a\s+)?(?:un|una|unos|unas|el|la)?\s*([^\n.:;]{0,100})'):
            m = re.search(rx, self.t)
            if m:
                partes.append(m.group(1))
        return ' | '.join(partes)

    @staticmethod
    def _normalizar_roles(roles):
        """Completa experiencia/moneda y lleva todas las bandas a COP."""
        salida = []
        for r in roles:
            exp = str(r.get('experiencia') or 'JUNIOR').upper()
            moneda = str(r.get('moneda') or 'COP').upper()
            if exp not in EXPERIENCIAS_VALIDAS:
                exp = 'JUNIOR'
            if moneda not in MONEDAS_VALIDAS:
                moneda = 'COP'
            factor = TRM_USD_COP if moneda == 'USD' else 1
            salida.append({'rol': r['rol'], 'min': round(r['min'] * factor), 'max': round(r['max'] * factor),
                           'experiencia': exp, 'moneda': moneda})
        return salida

    def _pool_bandas(self):
        """Bandas contra las que se compara: las del nivel de la vacante si existen; si no, las junior."""
        por_exp = {e: [r for r in self.roles if r['experiencia'] == e] for e in EXPERIENCIAS_VALIDAS}
        clave = {'semi': 'MID', 'senior': 'SENIOR'}.get(self.nivel, 'JUNIOR')
        if por_exp[clave]:
            self.experiencia_mercado = clave
            self.banda_de_nivel = clave != 'JUNIOR'
            return por_exp[clave]
        return por_exp['JUNIOR'] or self.roles

    def _agregar(self, titulo, detalle, puntos, severidad, categoria, critica=False):
        self.banderas.append({
            'titulo': titulo, 'detalle': detalle, 'severidad': severidad,
            'categoria': categoria, 'puntos': puntos,
        })
        self.puntaje += puntos
        if critica:
            self.hay_critica = True

    # ------------------------------------------------- 1. Validación sector TI
    def validar_sector_ti(self):
        """Acepta la oferta solo si es del sector TI.

        Reglas (en orden):
          1. Cargo TI en el título y ninguno ajeno a TI  -> TI.
          2. Cargo ajeno a TI en el título (vendedor, contador, ingeniero civil...) y sin cargo TI
             -> se rechaza, salvo que el cuerpo sea claramente técnico (varios cargos y tecnologías TI).
          3. Sin cargo claro en el título -> se exige evidencia suficiente en el texto
             (cargos y tecnologías TI).
        """
        t, tit = self.t, self.titulo
        roles_tit = [p for p in ROLES_TI if _hay(p, tit)]
        roles_cuerpo = [p for p in ROLES_TI if _hay(p, t)]
        techs = [k for k, p in TECNOLOGIAS.items() if _hay(p, t)]
        no_ti_tit = [p for p in ROLES_NO_TI if _hay(p, tit)]
        puntos = len(roles_cuerpo) * 2 + len(roles_tit) * 2 + len(techs)

        if roles_tit and not no_ti_tit:
            ok = True
        elif no_ti_tit and not roles_tit:
            ok = len(roles_cuerpo) >= 2 and puntos >= 8
        elif roles_tit and no_ti_tit:
            ok = puntos >= 4
        else:
            tecnicas = [k for k in techs if k not in TECNOLOGIAS_GENERICAS]
            ok = (len(roles_cuerpo) >= 1 and puntos >= 3) or (len(techs) >= 3 and len(tecnicas) >= 2)

        if not ok:
            raise OfertaNoTIError(
                'DevShield analiza únicamente ofertas del sector TI (desarrollo, QA, datos, '
                'soporte, infraestructura, ciberseguridad). No se encontraron cargos ni tecnologías de ese '
                'sector en el texto.')
        self.num_tecnologias = len([k for k in techs if k not in TECNOLOGIAS_GENERICAS])
        self.techs = techs

    # ------------------------------------------------------ 2. Nivel de cargo
    def detectar_nivel(self):
        t, tit = self.t, self.titulo
        # "desarrolladores junior" / "equipo junior" nombran a otros, no al cargo ofertado
        tit_cargo = re.sub(r'(?:desarrolladores|programadores|ingenieros|analistas|testers|perfiles|equipo|equipos|a|para|con)\s+'
                           r'(?:junior|jr\.?|senior|sr\.?|semi[\s-]?senior)', ' ', tit)
        tit_cargo_sin_semi = RX_SEMI.sub(' ', tit_cargo)
        pos = {}
        for nombre, rx, txt in (('junior', RX_JUNIOR, tit_cargo), ('semi', RX_SEMI, tit_cargo),
                                ('senior', RX_SENIOR, tit_cargo_sin_semi)):
            m = rx.search(txt)
            if m:
                pos[nombre] = m.start()
        if pos:
            self.nivel = min(pos, key=pos.get)      # manda el que aparece primero en el título
        else:
            cuerpo = RX_SEMI.sub(' ', t)
            j = len(RX_JUNIOR.findall(cuerpo))
            s = len(RX_SENIOR.findall(cuerpo))
            sm = len(RX_SEMI.findall(t))
            if j and j >= s and j >= sm:
                self.nivel = 'junior'
            elif sm and sm >= s:
                self.nivel = 'semi'
            elif s:
                self.nivel = 'senior'
        # Sin etiqueta explícita: se deduce por los años de experiencia que pide
        if self.nivel == 'no_indicado' and self.anos_exigidos > 0:
            self.nivel_inferido = True
            if self.anos_exigidos <= 1.5:
                self.nivel = 'junior'
            elif self.anos_exigidos < 4:
                self.nivel = 'semi'
            else:
                self.nivel = 'senior'

    # --------------------------------------------- 3. Cargo y banda de mercado
    def _puntaje_familias(self, ajustar_soporte=True):
        resultado = {}
        for fam, cfg in FAMILIAS.items():
            en_titulo = sum(1 for p in cfg['patrones'] if _hay(p, self.titulo))
            en_cuerpo = sum(1 for p in cfg['patrones'] if _hay(p, self.t))
            resultado[fam] = 3 * en_titulo + en_cuerpo
        # Una mención de "soporte técnico" solo en el cuerpo de una oferta cuyo cargo es otro
        # (p. ej. desarrollador) es una función cruzada, no el cargo de la vacante.
        if ajustar_soporte and not any(_hay(p, self.titulo) for p in FAMILIAS['soporte']['patrones']):
            if any(v >= 3 for k, v in resultado.items() if k != 'soporte'):
                resultado['soporte'] = 0
        return resultado

    @staticmethod
    def _tokens_rol(nombre_norm):
        return [w for w in re.findall(r'[a-z0-9#+.]+', nombre_norm) if w not in PALABRAS_GENERICAS]

    def detectar_rol_y_mercado(self):
        if not self.roles:
            self.mercado = dict(BANDA_POR_DEFECTO)
            self.avisos.append('No hay bandas salariales configuradas; se usó una referencia general.')
            return
        roles = self._pool_bandas()

        if len(self.areas_ti) >= 3:
            cruda = self._puntaje_familias(ajustar_soporte=False)
            fams = [f for a in self.areas_ti for f in AREA_A_FAMILIAS.get(a, []) if cruda.get(f, 0) >= 1]
            involucrados = [r for r in roles
                            if any(h in normalizar(r['rol']) for f in fams for h in FAMILIAS[f]['pistas'])]
            if involucrados:
                top = max(involucrados, key=lambda r: r['min'])
                self.rol_detectado = top['rol']
                self.mercado = {'min': top['min'], 'max': top['max']}
                self.avisos.append('El cargo mezcla varias áreas; el salario se comparó con la banda '
                                   'más alta entre ellas.')
                return

        fam = self._puntaje_familias()
        candidatos = []
        for r in roles:
            nombre = normalizar(r['rol'])
            puntos = 0
            for tok in self._tokens_rol(nombre):
                if _hay(re.escape(tok), self.titulo):
                    puntos += 3
                elif _hay(re.escape(tok), self.t):
                    puntos += 1
            for f, cfg in FAMILIAS.items():
                if any(p in nombre for p in cfg['pistas']):
                    puntos += fam[f]
            candidatos.append((puntos, r))

        candidatos.sort(key=lambda x: -x[0])
        mejor = candidatos[0][0]
        if mejor >= 3:
            elegidos = [r for p, r in candidatos if p >= max(3, mejor * 0.75)][:2]
            self.rol_detectado = ' / '.join(r['rol'] for r in elegidos)
            self.mercado = {
                'min': round(sum(r['min'] for r in elegidos) / len(elegidos)),
                'max': round(sum(r['max'] for r in elegidos) / len(elegidos)),
            }
        else:
            self.mercado = promedio_bandas(roles)
            self.avisos.append('No se identificó un cargo específico; el salario se comparó '
                               'con el promedio de las bandas configuradas.')

    # -------------------------------------------------- 4. Experiencia oculta
    def extraer_anos(self):
        """Mayor requisito de años de experiencia (cota inferior si es un rango)."""
        mayor = 0
        for m in RX_ANOS.finditer(self.t):
            ventana = self.t[max(0, m.start() - 60): m.end() + 60]
            if not re.search(r'experiencia|experience|trayectoria', ventana):
                continue
            if re.search(r'somos|empresa|compania|nuestr[oa]|contamos|fundad|llevamos|lideres|mercado',
                         self.t[max(0, m.start() - 40): m.start()]):
                continue
            n = NUM_PALABRA.get(m.group(1)) if not m.group(1).isdigit() else int(m.group(1))
            anos = n / 12 if m.group(3) == 'meses' else n
            mayor = max(mayor, anos)
        self.anos_exigidos = mayor

    def analizar_experiencia(self):
        mayor = self.anos_exigidos
        # solo se acusa "experiencia oculta" cuando la oferta DICE ser junior
        if self.nivel != 'junior' or self.nivel_inferido:
            return
        if mayor >= 3:
            self._agregar('Experiencia oculta',
                          f'Pide {mayor:g} años de experiencia para un perfil "Junior".',
                          PESOS['exp_3'], 'alta', 'lenguaje')
        elif mayor >= 2:
            self._agregar('Experiencia oculta',
                          f'Pide {mayor:g} años de experiencia para un perfil "Junior".',
                          PESOS['exp_2'], 'alta', 'lenguaje')

    # ------------------------------------------------- 5. Funciones cruzadas
    def _familia_principal(self):
        fam = self._puntaje_familias()
        mejor = max(fam, key=fam.get)
        return mejor if fam[mejor] >= 3 else None

    def detectar_areas_ti(self):
        self.areas_ti = [a for a, (patron, _) in AREAS_TI.items() if _hay(patron, self.t)]

    def analizar_funciones_cruzadas(self):
        principal = self._familia_principal()
        dev_presente = 'desarrollo' in self.areas_ti
        encontradas = []
        for area, (patron, etiqueta) in AREAS_CRUZADAS.items():
            if principal == 'soporte' and not dev_presente and area in ('soporte', 'hardware'):
                continue
            m = _rx(patron).search(self.t)
            if m:
                if area == 'soporte' and re.search(r'soporte (?:tecnico )?n[123]', self.t):
                    etiqueta = 'soporte técnico N1/N2'
                encontradas.append(etiqueta)

        multi = len(self.areas_ti) >= 3
        if self.es_practica and len(self.areas_ti) < 4:
            return                      # un practicante rota por varias áreas: es parte de su formación
        if multi:
            nombres = [AREAS_TI[a][1] for a in self.areas_ti]
            # el soporte ya queda dicho dentro de la lista de áreas
            encontradas = [e for e in encontradas if not e.startswith('soporte técnico')]
            detalle = (f'Combina funciones de {len(nombres)} áreas distintas: {", ".join(nombres)}'
                       + (f'; además {", ".join(encontradas)}' if encontradas else '') + '.')
            puntos = PESOS['multi_4'] if len(nombres) >= 4 else PESOS['multi_3']
            self._agregar('Funciones cruzadas', detalle, puntos, 'alta', 'lenguaje')
        elif encontradas:
            puntos = min(PESOS['cruzadas_tope'], PESOS['cruzadas_base'] + PESOS['cruzadas_extra'] * (len(encontradas) - 1))
            self._agregar('Funciones cruzadas',
                          f'Incluye funciones ajenas al cargo: {", ".join(encontradas)}.',
                          puntos, 'media', 'lenguaje')

    def analizar_stack_desproporcionado(self):
        if self.nivel != 'junior' or self.nivel_inferido or len(self.areas_ti) >= 3:
            return
        fam = self._puntaje_familias()
        familias_fuertes = sum(1 for f, v in fam.items() if v >= 4)
        if self.num_tecnologias >= STACK_MAX_JUNIOR or familias_fuertes >= 4:
            self._agregar('Stack desproporcionado',
                          f'Lista {self.num_tecnologias} tecnologías de áreas distintas para un perfil "Junior".',
                          PESOS['stack'], 'media', 'lenguaje')

    # --------------------------------------------------------- 6. Contrato
    def validar_contrato(self):
        t = self.t
        presta = bool(RX_PRESTACION.search(t))
        sin_prest = bool(RX_SIN_PRESTACIONES.search(t))
        t_formal = RX_SIN_PRESTACIONES.sub(' ', t)      # "sin prestaciones" no es un contrato formal
        formal = bool(RX_CONTRATO_FORMAL.search(t_formal))
        if sin_prest:
            self._agregar('Sin prestaciones sociales',
                          'La oferta aclara que no incluye prestaciones sociales ni seguridad social.',
                          PESOS['prestacion'], 'media', 'contrato')

        if presta:
            if RX_SUBORDINACION.search(t):
                self._agregar('Contrato sin garantías',
                              'Prestación de servicios con horario fijo o exclusividad: posible '
                              'relación laboral encubierta, sin prestaciones sociales.',
                              PESOS['contrato_encubierto'], 'alta', 'contrato')
            else:
                self._agregar('Contrato sin garantías',
                              'Prestación de servicios: sin prestaciones sociales ni estabilidad laboral.',
                              PESOS['prestacion'], 'media', 'contrato')
        elif not formal and not self.es_practica and (not RX_CONTRATO_MENCION.search(t) or RX_CONTRATO_VAGO.search(t)):
            self._agregar('Tipo de contrato no especificado',
                          'La oferta no indica qué tipo de contrato ofrece.',
                          PESOS['contrato_no_esp'], 'baja', 'contrato')

        t_sin_extras = re.sub(r'horas? extras? (?:sin|no) (?:remuner\w*|pag\w*)|sin pago de horas? extras?', ' ', t)
        if RX_NO_PAGO.search(t_sin_extras):
            self._agregar('Trabajo no remunerado',
                          'La oferta indica que la actividad no tiene remuneración.',
                          PESOS['critica'], 'alta', 'contrato', critica=True)

        if RX_GANANCIAS_IRREALES.search(t):
            self._agregar('Promesa de ganancias irreales',
                          'Promete ingresos ilimitados o garantizados: típico de ofertas fraudulentas.',
                          PESOS['critica'], 'alta', 'contrato', critica=True)

        if RX_SOLO_COMISION.search(t):
            self._agregar('Ingresos solo por comisión',
                          'No garantiza un salario fijo; el ingreso depende de resultados.',
                          PESOS['comision'], 'alta', 'contrato')

        self.contrato_formal = formal and not presta

    def analizar_fraude(self):
        t = self.t
        cobro = False
        for rx in RX_FRAUDE_COBRO:
            for m in rx.finditer(t):
                ventana = t[max(0, m.start() - 60): m.end() + 60]
                if not RX_FRAUDE_NEGATIVO.search(ventana):
                    cobro = True
                    break
            if cobro:
                break
        if cobro:
            self._agregar('Posible fraude: cobro al candidato',
                          'Menciona pagos, inversión o descuentos a cargo del candidato '
                          '(capacitación, kit, inscripción, etc.).',
                          PESOS['critica'], 'alta', 'contrato', critica=True)

        if any(rx.search(t) for rx in RX_FRAUDE_DATOS):
            self._agregar('Posible fraude: datos sensibles',
                          'Solicita claves, tarjetas o datos bancarios antes de contratar.',
                          PESOS['critica'], 'alta', 'contrato', critica=True)

        if RX_SOLO_MENSAJERIA.search(t) or \
                (RX_MENSAJERIA_ACTIVA.search(t) and not RX_CANAL_FORMAL.search(t)):
            self._agregar('Contacto solo por mensajería',
                          'El único canal es WhatsApp/Telegram; verifica que la empresa exista.',
                          PESOS['mensajeria'], 'baja', 'contrato')

        if RX_URGENCIA.search(t):
            self._agregar('Presión de urgencia',
                          'Te pide actuar de inmediato o habla de cupos limitados.',
                          PESOS['urgencia'], 'baja', 'lenguaje')

        es_desarrollo = any(a in self.areas_ti for a in ('desarrollo', 'mobile', 'datos', 'infra'))
        if es_desarrollo and RX_REQ_INCONGRUENTES.search(t):
            self._agregar('Requisitos incongruentes con el cargo',
                          'Pide solo conocimientos básicos para un cargo técnico que paga bien: '
                          'poco habitual en ofertas legítimas.',
                          PESOS['req_incong'], 'media', 'lenguaje')

    # ----------------------------------------------- 7. Sobrecarga y presión
    def _horas_semanales(self):
        """Estima las horas semanales: 'lunes a sábado de 7:00 AM a 5:00 PM', 'L-V de 8 a 6',
        '12 horas diarias'. Devuelve (horas, días, etiqueta) o None si no se puede estimar."""
        t = self.t
        dias = re.search(r'(?<![a-z])lunes\s*(?:a|al|-|–|/)\s*(viernes|sabado|domingo)\b|'
                         r'(?<![a-z])l\s*[-–/]\s*(v|s|d)\b', t)
        n_dias = None
        if dias:
            n_dias = {'viernes': 5, 'v': 5, 'sabado': 6, 's': 6, 'domingo': 7, 'd': 7}[dias.group(1) or dias.group(2)]
        elif re.search(r'(?:6|seis) dias (?:a la|por) semana|lunes a sabado', t):
            n_dias = 6

        # a) franja horaria ("de 8 a 6", "7:00 am a 5:00 pm") cerca de la mención de los días
        if dias:
            zona = t[max(0, dias.start() - 50): dias.end() + 70]
            franja = re.search(
                r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*(?:a|-|–|hasta)\s*(?:las\s*)?'
                r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?!\s*(?:anos?|meses|millones|mil|smmlv|%|\d))', zona)
            if franja:
                h1, m1, mer1, h2, m2, mer2 = franja.groups()
                h1, h2 = int(h1), int(h2)
                if h1 <= 23 and h2 <= 23:
                    if mer2:
                        mer2 = mer2.replace('.', '')
                        mer1 = (mer1 or ('am' if mer2 == 'pm' and h1 < 12 else mer2)).replace('.', '')
                        ini = (h1 % 12) + (12 if mer1 == 'pm' else 0) + int(m1 or 0) / 60
                        fin = (h2 % 12) + (12 if mer2 == 'pm' else 0) + int(m2 or 0) / 60
                    else:                       # sin a.m./p.m.: "8 a 6" -> 8:00 a 18:00
                        ini = h1 + int(m1 or 0) / 60
                        fin = h2 + int(m2 or 0) / 60
                        if fin <= ini and h2 <= 12:
                            fin += 12
                    dur = fin - ini
                    if 3 <= dur <= 16:
                        if dur > 6:
                            dur -= 1                       # una hora de almuerzo
                        return dur * n_dias, n_dias, (dias.group(1) or dias.group(2))

        # b) "12 horas diarias", "jornada de 10 horas"
        m = re.search(r'\b(\d{1,2})\s*horas?\s*(?:diarias?|por dia|al dia)|jornadas? de (\d{1,2})\s*horas', t)
        if m:
            h = int(m.group(1) or m.group(2))
            if 9 <= h <= 16:
                d = n_dias or (6 if re.search(r'sabados?', t) else 5)
                return h * d, d, 'dias'
        return None

    def analizar_horarios_y_presion(self):
        t = self.t
        est = self._horas_semanales()
        if est and est[0] > JORNADA_ALERTA_HORAS:
            self._agregar('Jornada excesiva',
                          f'El horario publicado suma ≈{est[0]:.0f} h semanales; el máximo legal es de '
                          f'{JORNADA_MAX_SEMANAL} h (Ley 2101 de 2021).',
                          PESOS['jornada'], 'alta', 'lenguaje')
        etiquetas, puntos = [], 0
        m = re.search(r'(?:disponibilidad|turnos?|guardias?|laborar|trabajar|trabajo)\b.{0,60}'
                      r'(?:fines? de semana|sabados|domingos|festivos)', t)
        if m and not re.search(r'\bsin\b|\bno\b|libres?|descanso', m.group(0)):
            etiquetas.append('turnos o disponibilidad en fines de semana')
            puntos += 6
        for rx, etiqueta, p in HORARIOS_ABUSIVOS:
            m = rx.search(t)
            if m:
                if etiqueta.startswith('guardias') and re.search(
                        r'compensad|remunerad|pagad|recargo|descanso|tiempo libre|bono',
                        t[max(0, m.start() - 50): m.end() + 60]):
                    continue                     # guardia compensada: no es abuso
                etiquetas.append(etiqueta)
                puntos += p
        if etiquetas:
            puntos = min(PESOS['sobrecarga_tope'], puntos)
            self._agregar('Sobrecarga horaria',
                          f'Menciona {", ".join(etiquetas)}.',
                          puntos, 'alta' if puntos >= PESOS['sobrecarga_tope'] else 'media', 'lenguaje')

        frases = [p for p in LENGUAJE_PRESION if _hay(p, t)]
        if len(frases) >= 2:
            self._agregar('Lenguaje de presión',
                          'Usa expresiones que suelen ocultar sobrecarga de trabajo o falta de límites.',
                          PESOS['presion_4'] if len(frases) >= 4 else PESOS['presion_2'], 'media', 'lenguaje')

    # ------------------------------------------------------------ 8. Salario
    def _candidatos_salario(self):
        t = self.t
        trabajo = t
        candidatos = []

        def blanquear(m):
            nonlocal trabajo
            trabajo = trabajo[:m.start()] + ' ' * (m.end() - m.start()) + trabajo[m.end():]

        def contexto_ok(inicio, fin, exigir_clave):
            antes = t[max(0, inicio - 35): inicio]
            no_sal = [m.start() for m in RX_NO_SALARIO_ANTES.finditer(antes)]
            if no_sal:
                sal = [m.start() for m in re.finditer(r'salari|sueldo|remuner', antes)]
                if not sal or max(sal) < max(no_sal):
                    return False          # la palabra "no salarial" (bono, auxilio...) está más cerca
            if RX_NO_SALARIO_DESPUES.search(t[fin: fin + 30]):
                return False
            if exigir_clave:
                ventana = t[max(0, inicio - 80): fin + 40]
                return bool(RX_CLAVE_SALARIO.search(ventana))
            return True

        def periodo(inicio, fin):
            despues = t[fin: fin + 40]
            # solo lo que acompaña a esta cifra: se corta en '+', ';', salto de línea, ', ' o '. '
            despues = re.split(r'\+|;|\n|,\s|\.\s|\s(?:mas|y|con|sin|pero)\s', despues)[0]
            antes = t[max(0, inicio - 60): inicio]
            previo = re.search(r'(quincenal(?:es)?|semanal(?:es)?|diari[oa]s?|anual(?:es)?|mensual(?:es)?)'
                               r'[\s:$]*(?:de|es de|ascienden a)?[\s:$]*(?:cop|usd)?[\s$]*$', antes)
            if previo:
                base = previo.group(1)
                if base.startswith('quincenal'): return 2
                if base.startswith('semanal'): return 4.345
                if base.startswith('diari'): return 22
                if base.startswith('anual'): return 1 / 12
                return 1
            if re.search(r'por hora|/ ?hora|/ ?h\b|per hour|hourly|la hora|x hora|por h\b', despues):
                return HORAS_MES
            if re.search(r'al ano|anual|por ano|/ ?ano|per year|annual|yearly|al ano', despues) or \
                    re.search(r'(?:salario|sueldo|remuneracion|pago|compensacion|ingreso)s?\s+anual(?:es)?[\s:$]*(?:de)?[\s:$]*(?:cop|usd)?[\s$]*$', antes):
                return 1 / 12
            if re.search(r'quincenal|por quincena|cada quincena', despues):
                return 2
            if re.search(r'semanal|por semana|a la semana', despues):
                return 4.345
            if re.search(r'diari[oa]|por dia', despues):
                return 22
            return 1

        def mon_usd(inicio, fin):
            return bool(RX_USD.search(t[max(0, inicio - 20): fin + 25]))

        def agregar(vmin, vmax, inicio, fin, usd=False, explicito=True, clave=True):
            per = periodo(inicio, fin)
            factor = TRM_USD_COP if usd else 1
            vmin, vmax = vmin * factor * per, vmax * factor * per
            if not (300_000 <= vmin <= 60_000_000 and vmax >= vmin):
                return
            ventana = t[max(0, inicio - 80): fin + 40]
            puntaje = (2 if re.search(r'salari|sueldo|remuner|honorari|ofrecemos|pagamos', ventana) else 0) \
                + (1 if explicito else 0) + (1 if 'mensual' in ventana else 0)
            por_proyecto = bool(RX_POR_PROYECTO.search(t[max(0, inicio - 60): fin + 60])) and 'mensual' not in ventana
            candidatos.append({'min': vmin, 'max': vmax, 'inicio': inicio, 'usd': usd,
                               'puntaje': puntaje, 'convertido': usd, 'por_proyecto': por_proyecto})

        # a) rangos en millones
        for m in list(RX_MILL_RANGO.finditer(trabajo)):
            if contexto_ok(m.start(), m.end(), False):
                a, b = _a_float(m.group(1)) * 1e6, _a_float(m.group(2)) * 1e6
                agregar(min(a, b), max(a, b), m.start(), m.end(), mon_usd(m.start(), m.end()))
            blanquear(m)
        # b) millones sueltos
        for m in list(RX_MILL.finditer(trabajo)):
            if contexto_ok(m.start(), m.end(), False):
                v = _a_float(m.group(1)) * 1e6
                agregar(v, v, m.start(), m.end(), mon_usd(m.start(), m.end()))
            blanquear(m)
        # c) "2 mil dólares"
        for m in list(RX_MIL_MONEDA.finditer(trabajo)):
            if contexto_ok(m.start(), m.end(), False):
                v = _a_float(m.group(1)) * 1000
                agregar(v, v, m.start(), m.end(), mon_usd(m.start(), m.end()))
            blanquear(m)
        # d) SMMLV
        for m in list(RX_SMMLV_RANGO.finditer(trabajo)):
            a, b = _a_float(m.group(1)) * SMMLV_2026, _a_float(m.group(2)) * SMMLV_2026
            agregar(min(a, b), max(a, b), m.start(), m.end())
            blanquear(m)
        for m in list(RX_SMMLV.finditer(trabajo)):
            v = _a_float(m.group(1)) * SMMLV_2026
            agregar(v, v, m.start(), m.end())
            blanquear(m)
        for m in list(RX_SM_SOLO.finditer(trabajo)):
            agregar(SMMLV_2026, SMMLV_2026, m.start(), m.end(), explicito=False)
            blanquear(m)
        # d2) tarifas por hora ("USD 30 per hour")
        for m in list(RX_HORA.finditer(trabajo)):
            usd = bool(re.search(r'usd|us\$|dolar', (m.group(1) or '') + ' ' + (m.group(3) or ''))) or \
                mon_usd(m.start(), m.end())
            moneda = (m.group(1) or '') + (m.group(3) or '')
            if usd or re.search(r'\$|cop|pesos', moneda):
                v = _a_float(m.group(2)) * HORAS_MES
                agregar(v, v, m.start(), m.end(), usd)
                blanquear(m)
        # d3) rangos "90k-110k", "3-4M"
        for m in list(RX_K_RANGO.finditer(trabajo)):
            usd = mon_usd(m.start(), m.end()) or 'usd' in m.group(0) or 'us$' in m.group(0)
            if (usd or '$' in m.group(0)) and contexto_ok(m.start(), m.end(), True):
                a, b = _a_float(m.group(1)) * 1000, _a_float(m.group(2)) * 1000
                agregar(min(a, b), max(a, b), m.start(), m.end(), usd)
                blanquear(m)
        for m in list(RX_M_RANGO.finditer(trabajo)):
            if contexto_ok(m.start(), m.end(), True):
                a, b = _a_float(m.group(1)) * 1e6, _a_float(m.group(2)) * 1e6
                agregar(min(a, b), max(a, b), m.start(), m.end(), mon_usd(m.start(), m.end()))
                blanquear(m)
        # e) "tres millones", "3.5M", "$3.5M"
        for m in list(RX_MILL_PALABRA.finditer(trabajo)):
            if contexto_ok(m.start(), m.end(), False):
                v = (NUM_PALABRA[m.group(1)] + EXTRA_PALABRA.get(m.group(2) or '', 0)) * 1e6
                agregar(v, v, m.start(), m.end(), mon_usd(m.start(), m.end()))
            blanquear(m)
        for m in list(RX_M_ABREV.finditer(trabajo)):
            if m.group(1) or contexto_ok(m.start(), m.end(), True):
                if contexto_ok(m.start(), m.end(), False):
                    v = _a_float(m.group(2)) * 1e6
                    agregar(v, v, m.start(), m.end(), mon_usd(m.start(), m.end()))
            blanquear(m)
        for m in list(RX_K_ABREV.finditer(trabajo)):
            usd = mon_usd(m.start(), m.end()) or 'usd' in (m.group(1) or '')
            if (usd or m.group(1)) and contexto_ok(m.start(), m.end(), True):
                v = _a_float(m.group(2)) * 1000
                agregar(v, v, m.start(), m.end(), usd)
            blanquear(m)
        # f) cifras completas (con separadores o de 4-9 dígitos), con posible rango
        montos = []
        for m in RX_MONTO.finditer(trabajo):
            valor = int(re.sub(r'[.,]', '', m.group(1)))
            usd = mon_usd(m.start(), m.end())
            con_simbolo = '$' in t[max(0, m.start() - 3): m.start()] or \
                bool(re.search(r'cop|pesos|usd|dolar', t[m.end(): m.end() + 12]))
            con_simbolo = con_simbolo or usd
            por_hora = bool(re.match(r'\s*(?:cop|pesos|usd|dolares)?\s*(?:por hora|/ ?hora|/ ?h\b|x hora|la hora)', t[m.end(): m.end() + 25]))
            if not usd and not por_hora and valor < 300_000:
                continue            # años (2026), cantidades pequeñas, etc.
            if valor > 100_000_000:
                continue
            if not contexto_ok(m.start(), m.end(), not con_simbolo):
                continue
            montos.append({'valor': valor, 'inicio': m.start(), 'fin': m.end(),
                           'usd': usd, 'simbolo': con_simbolo})
        i = 0
        while i < len(montos):
            a = montos[i]
            if i + 1 < len(montos):
                b = montos[i + 1]
                if RX_UNION.match(t[a['fin']: b['inicio']]) and b['valor'] > a['valor']:
                    agregar(a['valor'], b['valor'], a['inicio'], b['fin'], a['usd'] or b['usd'],
                            explicito=a['simbolo'] or b['simbolo'])
                    i += 2
                    continue
            agregar(a['valor'], a['valor'], a['inicio'], a['fin'], a['usd'], explicito=a['simbolo'])
            i += 1

        return candidatos

    def extraer_salario(self):
        cands = self._candidatos_salario()
        if not cands:
            return
        mejor = max(cands, key=lambda c: (c['puntaje'], -c['inicio']))
        vmin, vmax = mejor['min'], mejor['max']
        if vmin == vmax:
            texto = f'{_formato_cop(vmin)}'
        else:
            texto = f'{_formato_cop(vmin)} – {_formato_cop(vmax)}'
        if mejor['convertido']:
            texto += ' (aprox., TRM ' + f'{TRM_USD_COP:,.0f}'.replace(',', '.') + ')'
        self.salario = {'min': vmin, 'max': vmax, 'texto': texto,
                        'medio': round((vmin + vmax) / 2), 'convertido': mejor['convertido'],
                        'por_proyecto': mejor['por_proyecto']}

    def analizar_salario(self):
        self.extraer_salario()
        banda = self.mercado
        rol = f' ({self.rol_detectado})' if self.rol_detectado else ''
        s = self.salario

        # Sin cifra
        if s is None:
            if RX_VAGO.search(self.t):
                self._agregar('Salario a convenir',
                              'Opacidad en el rango salarial ofertado.', PESOS['convenir'], 'media', 'salario')
            self.estado_salario = 'no_detectado'
            self.veredicto_salario = 'No se detectó un salario numérico en la oferta.'
            return

        # Pago por proyecto: no es un salario mensual, no se compara con el mercado ni con el SMMLV
        if s.get('por_proyecto'):
            self.estado_salario = 'no_aplica'
            self.veredicto_salario = 'Pago por proyecto: no es comparable con un salario mensual.'
            return

        # Práctica / pasantía: no aplican ni SMMLV ni banda junior
        if self.es_practica:
            self.estado_salario = 'no_aplica'
            self.veredicto_salario = 'Práctica o pasantía: la banda salarial junior no aplica.'
            return

        # Por debajo del mínimo legal
        if s['max'] < SMMLV_2026:
            self._agregar('Salario por debajo del mínimo legal',
                          f'Ofrece {s["texto"]} COP; el salario mínimo legal 2026 es {_formato_cop(SMMLV_2026)}.',
                          PESOS['critica'], 'alta', 'salario', critica=True)
            self.estado_salario = 'bajo'
            self.veredicto_salario = 'Por debajo del salario mínimo legal.'
            return

        # Vacante semi-senior / senior: la banda junior no aplica como techo, pero sí como referencia
        # mínima escalada por nivel (un senior pagado como junior es una señal de alerta).
        if self.nivel in ('semi', 'senior') and not self.banda_de_nivel:
            factor = NIVEL_FACTOR[self.nivel]
            piso = banda['min'] * factor
            etiqueta = 'semi-senior' if self.nivel == 'semi' else 'senior'
            if s['max'] < piso:
                deficit = (piso - s['max']) / piso
                puntos = PESOS['sal_bajo_medio'] if deficit <= 0.35 else PESOS['sal_bajo_fuerte']
                self._agregar('Salario bajo para el nivel',
                              f'Ofrece {s["texto"]} COP para un perfil {etiqueta}; lo habitual parte de '
                              f'{_formato_cop(piso)} (banda junior x{factor:g}).',
                              puntos, 'media', 'salario')
                self.estado_salario = 'bajo'
                self.veredicto_salario = f'Bajo para un perfil {etiqueta}{rol}: lo habitual parte de {_formato_cop(piso)}.'
            else:
                self.estado_salario = 'ok'
                self.veredicto_salario = f'Coherente con un perfil {etiqueta}{rol}.'
            self.avisos.append('La vacante no parece de nivel junior; la banda mostrada es la junior.')
            return

        # Comparación con la banda del cargo
        bmin, bmax = banda['min'], banda['max']
        if s['max'] < bmin:
            deficit = (bmin - s['max']) / bmin
            puntos = PESOS['sal_bajo_leve'] if deficit <= 0.10 else PESOS['sal_bajo_medio'] if deficit <= 0.25 else PESOS['sal_bajo_fuerte']
            self._agregar('Salario bajo mercado',
                          f'Ofrece {s["texto"]} COP, por debajo del rango de mercado{rol}.',
                          puntos, 'alta' if puntos >= PESOS['sal_bajo_fuerte'] else 'media' if puntos >= PESOS['sal_bajo_medio'] else 'baja', 'salario')
            self.estado_salario = 'bajo'
            self.veredicto_salario = f'Por debajo del mínimo de mercado{rol}.'
        elif s['min'] < bmin:
            self._agregar('Salario inicia bajo mercado',
                          f'El rango ofrecido ({s["texto"]} COP) empieza por debajo del mínimo de mercado{rol}.',
                          PESOS['sal_inicia_bajo'], 'baja', 'salario')
            self.estado_salario = 'limite'
            self.veredicto_salario = f'Parte del rango está por debajo del mínimo de mercado{rol}.'
        elif self.nivel == 'junior' and s['min'] > bmax * SALARIO_SOSPECHOSO_X:
            sin_exp = bool(RX_SIN_EXPERIENCIA.search(self.t))
            if sin_exp:
                self._agregar('Salario inusualmente alto',
                              f'{s["texto"]} COP sin pedir experiencia es muy superior a la banda{rol}: '
                              'suena demasiado bueno para ser verdad.',
                              PESOS['sal_alto_sin_exp'], 'alta', 'salario')
            else:
                self._agregar('Salario inusualmente alto',
                              f'{s["texto"]} COP es muy superior a lo habitual para un perfil junior{rol}; '
                              'verifica que la oferta sea legítima.',
                              PESOS['sal_alto'], 'media', 'salario')
            self.estado_salario = 'alto'
            self.veredicto_salario = f'Muy por encima del rango de mercado{rol}.'
        elif s['min'] > bmax:
            self.estado_salario = 'alto'
            self.veredicto_salario = f'Por encima del rango de mercado{rol}.'
        else:
            self.estado_salario = 'ok'
            self.veredicto_salario = f'Dentro del rango de mercado{rol}.'

    # ------------------------------------------------- 9. Combinaciones graves
    # ------------------------------------- 9. Frases de alerta del administrador
    def analizar_patrones_admin(self):
        """Busca en el texto las frases de alerta que el administrador mantiene en la BD (RF-05)."""
        restante = PATRONES_ADMIN_TOPE
        for p in self.patrones:
            frase = normalizar(str(p.get('frase', ''))).strip()
            if not frase:
                continue
            palabras = frase.split()
            rx = re.compile(r'(?<![a-z0-9])' + r'\s+'.join(re.escape(w) for w in palabras) + r'(?![a-z0-9])')
            if not rx.search(self.t):
                continue
            categoria = p.get('categoria') if p.get('categoria') in CATEGORIAS_VALIDAS else 'lenguaje'
            try:
                peso = max(0, int(p.get('peso', 5)))
            except (TypeError, ValueError):
                peso = 5
            puntos = min(peso, restante)
            restante -= puntos
            severidad = 'alta' if peso >= 15 else 'media' if peso >= 8 else 'baja'
            self._agregar(f'Frase de alerta: «{frase}»',
                          'La oferta contiene una expresión que el sistema tiene registrada como señal de alerta.',
                          puntos, severidad, categoria)

    def analizar_combinaciones(self):
        """Señales que por separado son leves pero juntas describen una estafa típica."""
        alta = {b['titulo'] for b in self.banderas if b['severidad'] == 'alta'}
        otras = {b['titulo'] for b in self.banderas}
        if 'Salario inusualmente alto' in alta and \
                otras & {'Contacto solo por mensajería', 'Requisitos incongruentes con el cargo',
                         'Presión de urgencia'}:
            self._agregar('Posible fraude: oferta demasiado buena',
                          'Salario muy superior al mercado, sin exigir experiencia y con señales de '
                          'contacto o requisitos poco serios: patrón típico de ofertas fraudulentas.',
                          PESOS['critica'], 'alta', 'salario', critica=True)

    # -------------------------------------------------------------- Ejecutar
    def ejecutar(self):
        self.validar_sector_ti()
        self.extraer_anos()
        self.detectar_nivel()
        self.detectar_areas_ti()
        self.detectar_rol_y_mercado()

        self.analizar_experiencia()
        self.analizar_funciones_cruzadas()
        self.analizar_stack_desproporcionado()
        self.validar_contrato()
        self.analizar_fraude()
        self.analizar_horarios_y_presion()
        self.analizar_salario()
        self.analizar_patrones_admin()
        self.analizar_combinaciones()

        puntaje = min(self.puntaje, 100)
        if self.hay_critica:
            puntaje = max(puntaje, UMBRAL_ALTO)
        elif sum(1 for b in self.banderas if b['severidad'] == 'alta') >= 2:
            puntaje = max(puntaje, UMBRAL_ALTO)
        elif any(b['severidad'] == 'alta' for b in self.banderas):
            puntaje = max(puntaje, UMBRAL_MEDIO)

        n = len(self.banderas)
        criticas = sum(1 for b in self.banderas if b['severidad'] == 'alta')
        if puntaje >= UMBRAL_ALTO:
            nivel = 'Riesgo alto'
            desc = f'Se detectaron {n} señales de alerta' + \
                   (f' ({criticas} crítica{"s" if criticas != 1 else ""})' if criticas else '') + \
                   ' en esta oferta laboral.'
            if any(b['titulo'].startswith('Posible fraude') for b in self.banderas):
                rec = ('No realices pagos ni compartas datos sensibles. Verifica que la empresa exista '
                       'por canales oficiales antes de continuar con el proceso.')
            else:
                rec = ('Se sugiere cautela antes de aceptar esta oferta. El salario, el contrato o las '
                       'funciones presentan inconsistencias. Considera negociar o preguntar más detalles '
                       'al reclutador.')
        elif puntaje >= UMBRAL_MEDIO:
            nivel = 'Riesgo medio'
            desc = 'Se detectaron algunas señales de alerta que conviene revisar antes de postularte.'
            rec = ('La oferta tiene algunos puntos a aclarar. Antes de postularte, pregunta directamente '
                   'sobre las condiciones marcadas como banderas rojas.')
        else:
            nivel = 'Riesgo bajo'
            if n:
                desc = 'Solo se detectaron observaciones menores en esta oferta.'
            else:
                desc = 'La oferta cumple con la mayoría de parámetros de transparencia.'
            rec = 'La vacante parece alineada a los estándares de un perfil junior en el mercado.'
            if getattr(self, 'contrato_formal', False):
                desc += ' Menciona un contrato laboral formal.'

        if self.avisos:
            desc += ' Nota: ' + ' '.join(self.avisos)

        desglose = {'salario': 0, 'contrato': 0, 'lenguaje': 0}
        for b in self.banderas:
            desglose[b['categoria']] += b['puntos']

        orden = {'alta': 0, 'media': 1, 'baja': 2}
        banderas = sorted(self.banderas, key=lambda b: (orden[b['severidad']], -b['puntos']))

        return {
            'porcentaje': puntaje,
            'nivel': nivel,
            'descripcion': desc,
            'recomendacion': rec,
            'banderas': [{'titulo': b['titulo'], 'detalle': b['detalle'],
                          'severidad': b['severidad'], 'categoria': b['categoria']}
                         for b in banderas],
            'salario': self.salario['medio'] if self.salario else None,
            'salario_texto': self.salario['texto'] if self.salario else None,
            'salario_estado': self.estado_salario,
            'salario_veredicto': self.veredicto_salario,
            'mercado': self.mercado,
            'experiencia_mercado': self.experiencia_mercado,
            'rol_detectado': self.rol_detectado,
            'nivel_detectado': self.nivel,
            'sector_ti': True,
            'desglose': desglose,
        }
