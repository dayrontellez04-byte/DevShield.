"""
DevShield - Motor de análisis de ofertas laborales del sector TI (solo lógica, sin Flask ni BD).

Pasos: 1) valida sector TI, 2) detecta nivel y cargo, 3) extrae salario y lo compara con la banda
del cargo, 4) detecta señales de riesgo, 5) calcula puntaje (0-100) y clasifica bajo/medio/alto.
"""
import os
import re
import unicodedata

SMMLV_2026 = 1_750_905                                        
TRM_USD_COP = float(os.getenv('DEVSHIELD_TRM_USD', '4000'))
BANDA_POR_DEFECTO = {'min': 2_000_000, 'max': 3_500_000}
MIN_CARACTERES, MAX_CARACTERES, ZONA_TITULO = 40, 20_000, 180
UMBRAL_ALTO, UMBRAL_MEDIO = 50, 20
# Reglas: señal CRÍTICA => alto; 2+ señales ALTAS => alto; 1 señal ALTA => mínimo medio.
PESOS = {
    'exp_3': 15, 'exp_2': 10, 'cruzadas_base': 12, 'cruzadas_extra': 8, 'cruzadas_tope': 24,
    'multi_3': 18, 'multi_4': 22, 'stack': 10, 'contrato_encubierto': 18, 'prestacion': 10,
    'contrato_no_esp': 8, 'comision': 20, 'mensajeria': 6, 'urgencia': 5, 'req_incong': 8,
    'jornada': 10, 'sobrecarga_tope': 20, 'presion_2': 8, 'presion_4': 12, 'convenir': 12,
    'sal_bajo_leve': 10, 'sal_bajo_medio': 20, 'sal_bajo_fuerte': 25, 'sal_inicia_bajo': 6,
    'sal_alto_sin_exp': 20, 'sal_alto': 10, 'critica': 40,
}
STACK_MAX_JUNIOR = 7
SALARIO_SOSPECHOSO_X = 1.4
NIVEL_FACTOR = {'semi': 1.5, 'senior': 2.2}          # bandas junior -> referencia para semi/senior
PATRONES_ADMIN_TOPE = 30
CATEGORIAS_VALIDAS = ('salario', 'contrato', 'lenguaje')
EXPERIENCIAS_VALIDAS = ('JUNIOR', 'MID', 'SENIOR')
MONEDAS_VALIDAS = ('COP', 'USD')
JORNADA_MAX_SEMANAL, JORNADA_ALERTA_HORAS = 42, 48   # Ley 2101 de 2021
HORAS_MES = 160


class TextoInvalidoError(ValueError):
    """El texto está vacío, es muy corto o demasiado largo."""


class OfertaNoTIError(ValueError):
    """El texto no corresponde a una oferta del sector TI."""


#  Utilidades
def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y con variantes de escritura unificadas."""
    t = ''.join(c for c in unicodedata.normalize('NFKD', texto) if not unicodedata.combining(c)).lower()
    t = re.sub(r"(?<=\d)['’´](?=\d)", '.', t)
    t = t.replace('node.js', 'nodejs').replace('c sharp', 'c#')
    for a, b in ((r'front[\s-]?end', 'frontend'), (r'back[\s-]?end', 'backend'),
                 (r'full[\s-]?stack', 'fullstack'), (r'dev[\s-]ops', 'devops'), (r'[ \t\r\f\v]+', ' ')):
        t = re.sub(a, b, t)
    return t


def _rx(p):
    """Término como palabra completa (evita que 'java' calce en 'javascript')."""
    return re.compile(rf'(?<![a-z0-9])(?:{p})(?![a-z0-9#+])')


def _hay(p, texto):
    return _rx(p).search(texto) is not None


def _cop(n):
    return '$' + f'{round(n):,}'.replace(',', '.')


def _f(s):
    return float(s.strip().replace(',', '.'))


def promedio_bandas(roles):
    """Promedio general de las bandas (último recurso)."""
    if not roles:
        return dict(BANDA_POR_DEFECTO)
    return {k: round(sum(r[k] for r in roles) / len(roles)) for k in ('min', 'max')}


#  Catálogos
# Cargos TI (2 puntos c/u, distintos; si están en el título valen 2 más)
ROLES_TI = [
   
    'desarrolladora?(?:es)?(?! (?:de|para) (?:negocios?|mercados?|clientes?|talento|personas|proyectos? (?:inmobiliari\\w+|de construccion)|inmobiliari\\w+|comercial(?:es)?|organizacional|humano|de marca|de canal(?:es)?))',
    'developers?', 'programadora?(?:es)?', 'coders?', 'analista (?:programador|desarrollador)',
    'ingenier[oa] (?:de |en )?(?:software|sistemas|computacion|informatica|desarrollo|datos|pruebas|redes|telecomunicaciones|infraestructura|ciberseguridad|seguridad informatica|seguridad de la informacion|nube|cloud|devops|automatizacion de pruebas|soporte|ti|it|tecnologia|inteligencia artificial|ia|machine learning|ml)',
    '(?:software|backend|frontend|fullstack|full stack|data|devops|cloud|platform|machine learning|ml|ai|qa|test|security|network|systems?|site reliability|mobile|ios|android|embedded|firmware|web|game|blockchain|solutions?|support|infrastructure|automation|application) (?:engineer|developer|analyst|scientist|architect|administrator|specialist|programmer|consultant)s?',
    'desarrollo (?:de software|web|movil|de aplicaciones|de apps|de videojuegos|de sistemas|de soluciones|backend|frontend)', 'frontend',
    'backend', 'fullstack', 'devops', 'devsecops', 'sre', 'mlops', 'dataops', 'qa', 'tester', 'sdet',
    '(?:pruebas|testing) (?:de software|funcionales?|automatizadas?|de rendimiento|manuales)', 'automatizacion de pruebas',
    'analista (?:de |en )?(?:pruebas|datos|sistemas|calidad de software|calidad de sistemas|seguridad informatica|seguridad de la informacion|ciberseguridad|bi|inteligencia de negocios?|soporte|soporte tecnico|infraestructura|monitoreo|redes|ti|it|tecnologia|requerimientos|funcional|desarrollo|aplicaciones|soc|vulnerabilidades|bases de datos|integracion|procesos ti)',
    'analista (?:qa|bi|etl|soc|noc)', 'analista funcional(?: (?:sap|erp|ti|de sistemas))?',
    'data (?:analyst|engineer|scientist|steward|architect)', '(?:cientific[oa]|ingenier[oa]|analista|arquitect[oa]) de datos',
    '(?:desarrollador|ingeniero|analista)\\w* (?:de )?bi', 'business intelligence', 'inteligencia de negocios',
    'ingenier[oa] de ia|ingenier[oa] de inteligencia artificial', 'prompt engineer', 'dba',
    'administrador(?:a)? (?:de |en )?(?:bases de datos|sistemas|redes|infraestructura|servidores|plataformas?|nube|ti|it|seguridad informatica|aplicaciones|sistemas operativos|tecnologia)',
    'sysadmin', 'netadmin', 'noc', 'soc',
    'arquitect[oa] (?:de |en )?(?:software|soluciones|datos|nube|cloud|ti|it|infraestructura|sistemas|aplicaciones|tecnologia|integracion|seguridad)',
    'ciberseguridad', 'seguridad informatica', 'seguridad de la informacion', 'pentest\\w*', 'ethical hack\\w*', 'hacking etico', 'infosec',
    'cybersecurity',
    'tecnico (?:en|de) (?:redes|sistemas|computo|computadores|computadoras|informatica|soporte|telecomunicaciones|mantenimiento de (?:equipos de computo|computadores|computadoras|pcs?|equipos informaticos)|tecnologia|ti|it)',
    'tecnologo (?:en )?(?:sistemas|analisis y desarrollo|desarrollo de software|informatica|redes|gestion de redes|telecomunicaciones)',
    'auxiliar (?:de |en )?(?:sistemas|ti|it|informatica|soporte|computo)',
    'asistente (?:de |en )?(?:sistemas|ti|it|informatica|soporte tecnico)',
    'soporte (?:tecnico|informatico|it|ti|n[123]|nivel [123]|funcional|a usuarios?|en sitio|remoto|de aplicaciones|de infraestructura|de software|tic)',
    'help ?desk', 'service ?desk', 'mesa de (?:ayuda|servicios?)', 'centro de computo', 'ingenier[oa] de soporte',
    'especialista (?:en|de) (?:soporte|ti|it|tecnologia|sistemas|ciberseguridad)', 'scrum master', 'product owner', 'product manager',
    'agile coach',
    '(?:lider|jefe|gerente|coordinador|director|gestor) (?:tecnico|de (?:tecnologia|ti|it|sistemas|desarrollo|ingenieria|infraestructura|proyectos de (?:ti|software|tecnologia)|proyectos tecnologicos|seguridad informatica|soporte|tecnologia de la informacion|innovacion tecnologica))',
    'tech lead', 'team lead', 'cto', 'cio', 'ciso', 'project manager (?:de )?(?:ti|it|software)', 'ux ?/ ?ui', 'ui ?/ ?ux',
    'ux (?:designer|researcher|writer)', 'ui designer',
    'disenador[a]? (?:de )?(?:ux|ui|producto digital|web|interfaces|de experiencia de usuario)', 'ux', 'product designer',
    'technical writer', 'developer advocate',
    'consultor[a]? (?:de |en )?(?:sap|erp|oracle|salesforce|ti|it|tecnologia|servicenow|power bi|bi|datos|sistemas|ciberseguridad|cloud|funcional sap|tecnico|software|implementacion)',
    'abap', 'implementador (?:de )?(?:erp|software|sistemas)', 'integrador de sistemas',
    '(?:desarrollador|developer|consultor|analista|ingeniero|administrador)\\w* (?:sap|salesforce|servicenow|power apps|power platform|rpa|uipath|sharepoint|dynamics|odoo|netsuite)',
    'rpa', 'low[- ]?code', 'no[- ]?code', 'game ?dev\\w*',
    '(?:ingenier[oa]|especialista|analista|tecnico)\\w* (?:de |en )?(?:blockchain|iot|embebidos?|embedded|firmware|videojuegos?)',
    'tecnologias? de la informacion', '\\btic\\b', 'tecnologia informatica',
]

# Cargos claramente NO TI (basta uno en el título para sospechar; se unen en una sola regex)
ROLES_NO_TI = '|'.join([
    'vendedor\\w*',
    'asesor\\w* (?:comercial(?:es)?|de ventas|de servicio|de cartera|de credito|financier\\w+|de seguros|inmobiliari\\w+|telefonic\\w+|de call center|de atencion|de cobranza|de mercadeo|de campo)',
    'ejecutiv[oa]\\w* (?:comercial(?:es)?|de ventas|de cuentas?|de desarrollo de negocios|de atencion|de servicio)',
    'gerente (?:comercial|de ventas|de mercadeo|de marketing|de operaciones|financier\\w+|de produccion|de tienda|de sucursal|administrativ\\w+|de recursos humanos|de talento humano)',
    'jefe de (?:ventas|produccion|bodega|compras|cartera|tienda|almacen|calidad|logistica|mantenimiento)', 'desarrollo de negocios',
    'desarrollador de negocios', 'gestor comercial', 'promotor\\w*', 'impulsador\\w*', 'cociner[oa]', 'chef', 'mesero', 'mesera', 'barista',
    'bartender', 'panader[oa]', 'repostero',
    'auxiliar (?:de |en )?(?:cocina|bodega|enfermeria|contabilidad|contable|servicios generales|logistica|odontologia|farmacia|administrativ[oa]|de ventas|de almacen|de produccion|de despachos|de cartera|de nomina|de recursos humanos)',
    'asistente (?:administrativ[oa]|contable|de gerencia|de nomina|de recursos humanos|comercial|de ventas|de bodega|de compras)',
    'conductor\\w*', 'mensajer[oa]', 'domiciliari[oa]', 'operari[oa]', 'soldador\\w*', 'albanil', 'ayudante', 'vigilante', 'escolta',
    'guarda de seguridad', 'aseador\\w*', 'servicios generales', 'jardiner[oa]', 'enfermer[oa]', 'medic[oa] (?:general|especialista)',
    'odontolog\\w+', 'fisioterapeuta', 'nutricionista', 'psicolog[oa]', 'terapeuta', 'bacteriolog[oa]', 'quimico farmaceutico',
    'veterinari[oa]', 'docente', 'profesor\\w*', 'tutor de (?!software)\\w+', 'licenciad[oa]', 'abogad[oa]', 'contador\\w*',
    'revisor fiscal', 'secretari[oa]', 'recepcionista', 'cajer[oa]', 'call center', 'telemercadeo', 'telemercaderista', 'community manager',
    'publicista', 'periodista', 'redactor(?! tecnico)', 'fotograf[oa]', 'camar[oa]grafo', 'estilista', 'peluquer[oa]', 'barbero',
    'manicurista', 'disenador[a]? (?:grafic[oa]|industrial|de modas|de interiores|publicitari[oa]|de moda|de joyas|textil)', 'electricista',
    'mecanic[oa]', 'plomer[oa]', 'carpinter[oa]',
    'tecnico (?:electricista|electric[oa]|mecanic[oa]|electronic[oa]|de refrigeracion|de aire acondicionado|biomedic[oa]|industrial|de mantenimiento (?!de (?:equipos de computo|computadores|computadoras|pcs?)))',
    'ingenier[oa] (?:civil|industrial|ambiental|quimic[oa]|mecanic[oa]|electric[oa]|electronic[oa]|agronom[oa]|agroindustrial|de petroleos|de alimentos|de minas|biomedic[oa]|de produccion|de procesos|de calidad|de mantenimiento|forestal|de transporte|de materiales|de obra|geologo|geologica|pesquero|textil)',
    'arquitect[oa](?! (?:de|en) (?:software|soluciones|datos|nube|cloud|ti|it|infraestructura|sistemas|aplicaciones|tecnologia|integracion|seguridad))',
    'topograf[oa]', 'agronom[oa]', 'zootecnista', 'residente de obra', 'maestro de obra',
    'analista (?:de )?(?:credito|cartera|financier[oa]|nomina|compras|mercadeo|marketing|produccion|costos|tesoreria|contable|recursos humanos|seleccion|talento humano|logistic[oa]|de calidad(?! de software)|presupuesto|riesgo(?: crediticio)?|tributari[oa]|comercial|cobranza|facturacion|inventarios?|trade marketing)',
    'coordinador[a]? (?:de )?(?:logistica|ventas|produccion|calidad|talento humano|recursos humanos|mercadeo|compras|bodega)',
    'talento humano', 'recursos humanos',
])

TECNOLOGIAS = {
    'python': r'python', 'java': r'java', 'javascript': r'javascript|js|ecmascript', 'typescript': r'typescript|ts',
    'csharp': r'c#|csharp', 'cpp': r'c\+\+', 'golang': r'golang', 'rust': r'rust(?:lang)?', 'ruby': r'ruby|rails',
    'php': r'php', 'kotlin': r'kotlin', 'swift': r'swift(?:ui)?', 'scala': r'scala', 'dart': r'dart', 'perl': r'perl',
    'cobol': r'cobol', 'abap': r'abap', 'vbnet': r'vb\.net|visual basic', 'bash': r'bash|shell script\w*',
    'powershell': r'powershell', 'sql': r'sql(?: server)?|t-sql|mysql|postgres(?:ql)?|plsql|pl/sql|oracle|mariadb|sqlite',
    'rlang': r'lenguaje r', 'matlab': r'matlab',
    'react': r'react(?:js| native)?', 'angular': r'angular(?:js)?', 'vue': r'vue(?:js)?', 'svelte': r'svelte',
    'nextjs': r'next\.?js|nuxt(?:js)?', 'node': r'nodejs|node', 'express': r'expressjs|nestjs', 'django': r'django',
    'flask': r'flask', 'fastapi': r'fastapi', 'spring': r'spring(?: boot| cloud)?', 'hibernate': r'hibernate',
    'laravel': r'laravel|symfony|codeigniter', 'dotnet': r'\.net|dotnet|asp\.net|net core', 'flutter': r'flutter',
    'android': r'android', 'ios': r'ios', 'xamarin': r'xamarin|ionic|maui', 'jquery': r'jquery|bootstrap|tailwind',
    'html': r'html5?', 'css': r'css3?|sass|scss', 'wordpress': r'wordpress|woocommerce|shopify|magento|prestashop',
    'unity': r'unity(?: 3d)?|unreal(?: engine)?',
    'mongodb': r'mongodb|mongo', 'redis': r'redis|elasticsearch|cassandra|dynamodb|firebase|neo4j|couchbase',
    'dw': r'snowflake|bigquery|redshift|data ?warehouse|databricks|hive', 'spark': r'spark|hadoop|kafka|airflow|dbt|nifi',
    'powerbi': r'power ?bi|tableau|looker|qlik|dax|metabase', 'pandas': r'pandas|numpy|scikit[- ]?learn|jupyter',
    'ml': r'machine learning|aprendizaje automatico|deep learning|tensorflow|pytorch|nlp|llms?|mlops|inteligencia artificial',
    'etl': r'etl|elt|ssis|talend|informatica powercenter',
    'docker': r'docker', 'kubernetes': r'kubernetes|k8s|helm|openshift', 'aws': r'aws|amazon web services',
    'azure': r'azure', 'gcp': r'gcp|google cloud', 'terraform': r'terraform|ansible|puppet|cloudformation',
    'cicd': r'ci ?/ ?cd|jenkins|gitlab ci|github actions|argocd|circleci|azure devops',
    'linux': r'linux|unix|ubuntu|centos|red hat|debian', 'nginx': r'nginx|apache|tomcat|iis',
    'monitor': r'prometheus|grafana|datadog|new relic|splunk|zabbix|nagios|elk',
    'vmware': r'vmware|hyper-?v|virtualizacion|proxmox',
    'windows': r'windows(?: server)?|active directory|office 365|microsoft 365|exchange|sharepoint',
    'redes': r'tcp ?/ ?ip|cisco|ccna|ccnp|vpn|vlan|firewalls?|fortinet|fortigate|palo alto|mikrotik|dns|dhcp|routers?|switches',
    'seguridad': r'siem|owasp|iso ?27001|pentest\w*|nessus|burp|vulnerabilidades|hardening|kali linux',
    'itil': r'itil|cobit|servicenow|glpi|remedy|mesa de ayuda|help ?desk',
    'selenium': r'selenium',
    'cypress': r'cypress|playwright|appium|cucumber|testng|junit|pytest|jmeter|soapui|katalon|robot framework',
    'postman': r'postman|swagger', 'git': r'git(?:hub|lab)?|bitbucket', 'jira': r'jira|confluence|trello|azure boards',
    'scrum': r'scrum|kanban|agil(?:e|es)?', 'api': r'apis?|rest(?:ful)?|graphql|soap|grpc|json|xml|webhooks?',
    'microservicios': r'microservicios?|microservices?|arquitectura de software|soa',
    'figma': r'figma|sketch|adobe xd|invision|prototip\w+|wireframes?|design system\w*',
    'erp': r'sap|erp|salesforce|dynamics|oracle ebs|odoo|netsuite|servicenow|power apps|power automate|rpa|uipath|automation anywhere|blue prism',
    'basesdatos': r'bases? de datos|base de datos|dwh', 'software': r'software', 'programacion': r'programacion',
    'nube': r'nube|cloud', 'iot': r'iot|arduino|raspberry|embebid\w+|plc',
}
# Cuentan para validar el sector, pero no para el "stack desproporcionado"
TECNOLOGIAS_GENERICAS = {'api', 'git', 'html', 'css', 'sql', 'software', 'programacion', 'basesdatos', 'scrum', 'jira',
                         'itil', 'windows', 'redes', 'nube', 'microservicios', 'figma', 'postman', 'linux', 'seguridad',
                         'monitor', 'nginx', 'iot', 'jquery'}

# Familias de cargo: patrones que las delatan y pistas en el nombre del rol de la BD
FAMILIAS = {
    'java': {'patrones': [r'java', r'spring(?: boot)?', r'hibernate', r'jsp'], 'pistas': ['java']},
    'frontend': {'patrones': [r'frontend', r'react(?:js)?', r'angular(?:js)?', r'vue(?:js)?', r'html5?', r'css3?',
                              r'javascript', r'typescript', r'ui ?/ ?ux|ux ?/ ?ui'], 'pistas': ['frontend', 'front']},
    'backend': {'patrones': [r'backend', r'python', r'django', r'flask', r'fastapi', r'nodejs', r'php', r'laravel', r'c#',
                             r'\.net|dotnet', r'golang', r'ruby|rails', r'microservicios', r'apis?|rest(?:ful)?'],
                'pistas': ['backend', 'back']},
    'qa': {'patrones': [r'qa', r'tester', r'testing', r'pruebas(?: de software)?', r'calidad de software', r'selenium',
                        r'cypress', r'automatizacion de pruebas'], 'pistas': ['qa', 'tester', 'test', 'prueba']},
    'datos': {'patrones': [r'analista de datos', r'data (?:analyst|engineer|scientist)', r'cientifico de datos',
                           r'power ?bi', r'tableau', r'pandas', r'business intelligence', r'etl', r'machine learning'],
              'pistas': ['dato', 'data', 'bi']},
    'soporte': {'patrones': [r'soporte (?:tecnico|informatico|it|ti|n[123]|nivel [123]|a usuarios?|en sitio|remoto|funcional)',
                             r'help ?desk', r'service ?desk', r'mesa de (?:ayuda|servicios?)', r'active directory',
                             r'office 365|microsoft 365', r'tecnico (?:en|de) (?:sistemas|soporte|computo|informatica|redes)',
                             r'auxiliar de (?:sistemas|ti|it|soporte)',
                             r'mantenimiento (?:preventivo|correctivo)?(?: y correctivo)? de (?:equipos|computadores|computadoras|pcs?)',
                             r'usuarios? finales?', r'itil', r'glpi|servicenow|remedy'],
                'pistas': ['soporte', 'support', 'help']},
    'devops': {'patrones': [r'devops', r'docker', r'kubernetes|k8s', r'ci ?/ ?cd', r'jenkins', r'terraform', r'aws',
                            r'azure', r'sre', r'infraestructura'], 'pistas': ['devops', 'infra', 'cloud', 'sre']},
    'mobile': {'patrones': [r'android', r'kotlin', r'swift', r'flutter', r'react native', r'ios'],
               'pistas': ['mobile', 'movil', 'android', 'ios']},
}
PALABRAS_GENERICAS = {'junior', 'jr', 'dev', 'developer', 'desarrollador', 'desarrolladora', 'ingeniero', 'analista',
                      'tecnico', 'de', 'del', 'la', 'el', 'y', 'e', 'en', 'ti', 'it', 'ii', 'i', 'nivel', 'trainee',
                      'practicante', 'senior', 'sr', 'semi', 'ssr'}

# Áreas técnicas (3 o más = perfil "todoterreno")
AREAS_TI = {
    'desarrollo': (r'frontend|backend|fullstack|java|spring(?: boot)?|angular|react|vue|python|django|nodejs|php|laravel|'
                   r'\.net|desarrolladora?(?:es)?|programadora?(?:es)?|desarrollo (?:de )?(?:software|aplicaciones|apps|web|sistemas)',
                   'desarrollo de software'),
    'infra': (r'devops|docker|kubernetes|k8s|ci ?/ ?cd|terraform|jenkins|infraestructura|aws|azure|servidores', 'infraestructura / DevOps'),
    'soporte': (r'soporte tecnico|mesa de ayuda|help ?desk|soporte n[123]|soporte informatico', 'soporte técnico'),
    'diseno': (r'ux ?[-/] ?ui|ui ?[-/] ?ux|prototip\w+|figma|diseno de interfaces|diseno ux', 'diseño UX/UI'),
    'qa': (r'\bqa\b|tester|automatizacion de pruebas', 'QA / pruebas'),
    'datos': (r'analisis de datos|power ?bi|tableau|etl|data analyst|ciencia de datos', 'análisis de datos'),
    'mobile': (r'plataforma movil|apps? moviles?|android|ios|flutter|kotlin|swift|react native', 'desarrollo móvil'),
}
AREA_A_FAMILIAS = {'desarrollo': ['java', 'frontend', 'backend'], 'infra': ['devops'], 'soporte': ['soporte'],
                    'qa': ['qa'], 'datos': ['datos'], 'mobile': ['mobile']}

# Funciones ajenas al cargo
AREAS_CRUZADAS = {
    'soporte': (r'soporte tecnico|mesa de ayuda|help ?desk|soporte n[123]|soporte (?:a|al) usuarios?', 'soporte técnico'),
    'cliente': (r'atencion al cliente|servicio al cliente|call center|telemercadeo|servicio al usuario', 'atención al cliente'),
    'ventas': (r'(?:realizar|hacer|apoyar|gestionar|cerrar)\s+(?:las\s+)?ventas|apoyo (?:en|a|de) (?:las )?ventas|apoyo comercial|'
               r'labores comerciales|venta de (?:productos|servicios|seguros)|prospeccion de clientes|cobranza', 'ventas / cobranza'),
    'marketing': (r'community manager|manejo de redes sociales|diseno grafico|edicion de video|fotografia|marketing digital|'
                  r'disenar piezas', 'marketing / diseño gráfico'),
    'admin': (r'labores administrativas|tareas administrativas|digitacion|facturacion|contabilidad|auxiliar administrativ[oa]|'
              r'archivo fisico', 'tareas administrativas / contables'),
    'hardware': (r'mantenimiento de (?:equipos|computadores|computadoras|impresoras|pcs?)|reparacion de (?:equipos|computadores|pcs?)',
                 'mantenimiento de equipos'),
    'instalaciones': (r'cableado|instalacion de (?:camaras|puntos|antenas)|camaras de seguridad|instalacion electrica|alarmas',
                      'instalación de cámaras / cableado'),
}

#  Expresiones regulares
RX_SEMI = re.compile(r'semi[\s-]?senior|semi[\s-]?sr\b|\bssr\b|\bmid[\s-]?level\b|\bmid\b')
RX_SENIOR = re.compile(r'\bsenior\b|\bsr\.?(?=\s|$|[,;:)])|\blider tecnico\b|\btech lead\b|\blead\b|\barquitecto\b|\bstaff\b|\bprincipal\b')
RX_JUNIOR = re.compile(r'\bjunior\b|\bjr\.?(?=\s|$|[,;:)])|\btrainee\b|\bentry[\s-]?level\b|\bsin experiencia\b|\bprimer empleo\b|'
                       r'\brecien egresad|\bpracticante\b|\bpasante\b|\baprendiz\b|\bpasantia\b')
RX_PRACTICA = re.compile(r'practicante|pasante|pasantia|aprendiz|practica (?:universitaria|profesional|laboral)|etapa (?:lectiva|productiva)')

RX_PRESTACION = re.compile(r'prestacion de servicios|contrato de servicios|orden de servicios|\bcontratista\b|\bcontractor\b|'
                           r'\bops\b|\bhonorarios\b|\bfreelance\b|\bindependiente\b')
RX_SUBORDINACION = re.compile(r'horario (?:fijo|establecido|de oficina)|de lunes a viernes|lunes a viernes|\b8 horas\b|tiempo completo|'
                              r'jornada completa|presencial|exclusividad|dedicacion exclusiva|cumplir (?:con )?(?:el )?horario|'
                              r'control de horario|reportar(?:as)? a|reporte diario')
RX_CONTRATO_FORMAL = re.compile(r'termino indefinido|contrato indefinido|termino fijo|obra o labor|contrato laboral|contrato directo|'
                                r'prestaciones de ley|prestaciones sociales|seguridad social|afiliacion a (?:eps|arl|caja)|nomina')
RX_SIN_PRESTACIONES = re.compile(r'sin prestaciones(?: sociales| de ley)?|no incluye prestaciones|'
                                 r'no (?:tiene|ofrece|cuenta con) prestaciones|sin seguridad social')
RX_CONTRATO_MENCION = re.compile(r'\bcontrato\b|contratacion|vinculacion')
RX_CONTRATO_VAGO = re.compile(r'(?:tipo de )?contrato\s*:?\s*(?:a convenir|por definir|por confirmar|no especificado|segun (?:el )?perfil)')
RX_VAGO = re.compile(r'a convenir|segun experiencia|de acuerdo (?:a|con) (?:la |su |tu )?experiencia|segun (?:el )?perfil|'
                     r'acorde al mercado|salario competitivo|salario atractivo|negociable|a negociar')
RX_NO_PAGO = re.compile(r'ad[\s-]?honorem|no remunerad[oa]s?|sin remuneracion|sin salario|trabajo voluntario|voluntariado|'
                        r'sin retribucion economica|sin pago|no se paga|no tiene remuneracion')
RX_SOLO_COMISION = re.compile(r'(?:100 ?%|solo|unicamente|exclusivamente|solamente)\s+(?:por\s+)?comisiones?|sin salario fijo|'
                              r'sin sueldo fijo|pago por (?:resultados|produccion)')
RX_GANANCIAS_IRREALES = re.compile(r'ganancias? (?:ilimitadas?|garantizadas?)|ingresos? (?:ilimitados?|garantizados?)|gana(?:r)? dinero facil')

RX_FRAUDE_COBRO = [re.compile(p) for p in (
    r'inversion inicial',
    r'(?:pago|cobro|costo|cuota|valor)\s+(?:de\s+(?:la\s+|el\s+|tu\s+)?)?(?:inscripcion|capacitacion|curso|bootcamp|kit|membresia|certificacion|uniforme)\b',
    r'debes? pagar|tendras que pagar|(?:consign|transferi|deposit)\w*\s.{0,30}(?:dinero|plata|valor|cuota)',
    r'(?:debes|deberas|tienes que|tendras que|es necesario|se debe|se requiere que)\s+(?:consignar|pagar|transferir|depositar|cancelar|abonar)',
    r'poliza de (?:seguro|cumplimiento)|verificacion de datos|certificado de ingreso|expedicion del certificado',
    r'(?:realizar|hacer|efectuar)\s+(?:un\s+|el\s+)?(?:pago|abono|deposito|consignacion)',
    r'(?:pago|cobro|abono|consignacion|deposito)\s+de\s+\$\s?\d[\d.,]*\s+por\s+concepto',
    r'por concepto de (?:certificacion|inscripcion|capacitacion|curso|kit|poliza|verificacion|afiliacion|uniforme|material|induccion)',
    r'(?:cancelar|pagar|consignar|abonar)\s+(?:el\s+)?(?:valor|monto|suma)\s+de',
    r'descuent\w+ (?:del|de su|de tu) (?:salario|sueldo|pago).{0,30}(?:capacitacion|curso|formacion|equipo)')]
RX_FRAUDE_NEGATIVO = re.compile(r'(?:empresa|nosotros|compania)\s+(?:asume|cubre|paga|financia|patrocina)|sin costo|gratuit[oa]|cubiert[oa]|financiad[oa]')
RX_FRAUDE_DATOS = [re.compile(p) for p in (
    r'(?:envi|compart|manda)\w*\s.{0,30}(?:clave|contrasena|tarjeta|datos bancarios|cuenta bancaria|\bpin\b)',
    r'(?:clave|contrasena|tarjeta|datos bancarios|cuenta bancaria|\bpin\b).{0,30}(?:enviar|compartir|mandar)')]
RX_MENSAJERIA_ACTIVA = re.compile(r'(?:escribe|escribenos|escribir|contacta|contactanos|comunicate|manda|envia)\w*\s.{0,40}(?:whatsapp|telegram)')
RX_CANAL_FORMAL = re.compile(r'@|linkedin|www\.|https?://|portal|correo')
RX_URGENCIA = re.compile(r'escribe(?:nos)? inmediatamente|cupos? limitados?|ultimos cupos|solo por hoy|responde (?:ya|hoy)|aprovecha ahora')
RX_REQ_INCONGRUENTES = re.compile(r'manejo basico de (?:computador\w*|pc|ofimatica|office)|no se requiere experiencia en programacion|'
                                  r'sin (?:conocimientos?|experiencia) (?:previos? )?(?:de|en) programacion')
RX_SIN_EXPERIENCIA = re.compile(r'no se requiere experiencia|no requiere experiencia|sin experiencia|no necesitas experiencia|te capacitamos|nosotros te capacitamos')
RX_SOLO_MENSAJERIA = re.compile(r'(?:unicamente|solo|solamente|exclusivamente)\s+(?:por\s+)?(?:whatsapp|telegram)|escribe(?:nos)? al (?:whatsapp|telegram)')

HORARIOS_ABUSIVOS = [
    (re.compile(r'24 ?/ ?7|24x7|24 horas'), '24/7', 15),
    (re.compile(r'disponibilidad (?:total|permanente|completa|24)'), 'disponibilidad total', 10),
    (re.compile(r'(?:fines? de semana|sabados?|domingos?|festivos?).{0,30}(?:obligatori|disponib|laborar|trabajar)'), 'trabajo en fines de semana/festivos', 6),
    (re.compile(r'horas? extras? (?:sin|no) (?:remuner|pag)|sin pago de horas extras?'), 'horas extra sin pago', 15),
    (re.compile(r'sin horario|horario (?:abierto|ilimitado)'), 'sin horario definido', 15),
    (re.compile(r'\b(?:10|11|12)\s*horas diarias'), 'jornadas de 10+ horas', 12),
    (re.compile(r'\bon[\s-]?call\b|guardias? (?:sin|no)'), 'guardias sin compensación', 8),
]
LENGUAJE_PRESION = [r'bajo presion', r'(?:ponerse|ponte|poner) la camiseta', r'somos una familia', r'como una familia',
                    r'alto compromiso', r'compromiso total', r'multitarea', r'multitasking', r'\btodero\b', r'hacer de todo',
                    r'ritmo acelerado', r'cultura de alto rendimiento', r'tolerancia a la frustracion', r'sin limite de horario',
                    r'mentalidad de startup', r'no (?:es )?un 9 a 5']

# Salario
RX_NO_SALARIO_ANTES = re.compile(r'bono|bonificacion|auxilio|comision|incentivo|descuento|multa|inversion|deposito|garantia|cobro|'
                                 r'capacitacion|costo|cuota|facturacion|presupuesto|capital|ventas? (?:de|por|mensuales?|anuales?)')
RX_NO_SALARIO_DESPUES = re.compile(r'^\s*(?:de\s+)?(?:usuarios|clientes|empleados|colaboradores|personas|visitas|transacciones|'
                                   r'registros|descargas|ventas|facturacion|euros? en)')
RX_CLAVE_SALARIO = re.compile(r'salari|sueldo|remuner|honorari|\bpago\b|pagamos|ofrecemos|asignacion|devengar|compensacion|ingreso|mensual|\bcop\b|pesos|smmlv|\$')
RX_POR_PROYECTO = re.compile(r'por proyecto|pago (?:unico|por entrega|por tarea)|valor (?:total )?del proyecto|presupuesto (?:del|para el) proyecto|por entregables?|pago unico')
RX_USD = re.compile(r'\busd\b|us\$|u\$s|dolar|\bdls\b')
_N = r'\d+(?:[.,]\d+)?'
_SEP = r'(?:-|–|—|a|y|hasta|al)'
RX_MILL_RANGO = re.compile(rf'({_N})\s*(?:millones?|mm)?\s*{_SEP}\s*({_N})\s*millones?\b')
RX_MILL = re.compile(rf'({_N})\s*millones?\b')
RX_MIL_MONEDA = re.compile(rf'({_N})\s*mil\b(?=\s*(?:dolares|usd|pesos|cop|mensuales))')
RX_SMMLV_RANGO = re.compile(rf'({_N})\s*(?:-|–|—|a|y)\s*({_N})\s*(?:smmlv|smlmv|smlv|salarios? minimos?)')
RX_SMMLV = re.compile(rf'({_N})\s*(?:smmlv|smlmv|smlv|salarios? minimos?)')
RX_SM_SOLO = re.compile(r'(?<![\d.,])\s*salario minimo(?: legal(?: mensual)?(?: vigente)?)?')
RX_M_ABREV = re.compile(rf'(\$\s*)?({_N})\s*(?:m|mm|mill)\b(?![a-z0-9])')
RX_M_RANGO = re.compile(rf'({_N})\s*(?:m|mm)?\s*(?:-|–|—|a|y|hasta|al|to)\s*\$?\s*({_N})\s*(?:m|mm)\b(?![a-z0-9])')
RX_K_RANGO = re.compile(rf'(?:\$|usd|us\$)?\s*({_N})\s*k?\s*(?:-|–|—|a|to)\s*(?:\$|usd|us\$)?\s*({_N})\s*k\b(?![a-z0-9])')
RX_HORA = re.compile(rf'(?:(usd|us\$|\$|cop)\s*)?({_N})\s*(usd|dolares|cop|pesos)?\s*(?:/|por|per|x)\s*(?:hora|hour|hr|h)\b')
RX_K_ABREV = re.compile(rf'(\$\s*|usd\s*)?({_N})\s*k\b(?![a-z0-9])')
RX_MILL_PALABRA = re.compile(r'\b(un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+millones?'
                             r'(?:\s+(y medio|quinientos mil|y quinientos mil|doscientos mil|trescientos mil|cuatrocientos mil))?')
EXTRA_PALABRA = {'y medio': .5, 'quinientos mil': .5, 'y quinientos mil': .5, 'doscientos mil': .2,
                 'trescientos mil': .3, 'cuatrocientos mil': .4}
RX_MONTO = re.compile(r'(?<![\d.,])(\d{1,3}(?:[.,]\d{3}){1,3}|\d{4,9})(?![\d])')
RX_UNION = re.compile(r'^\s*(?:cop|pesos|usd|dolares)?\s*(?:-|–|—|a|y|hasta|al|to)\s*(?:\$|cop|usd|us\$)?\s*$')
NUM_PALABRA = {'un': 1, 'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6, 'siete': 7,
               'ocho': 8, 'nueve': 9, 'diez': 10}
RX_ANOS = re.compile(r'(?<![\d.,])(\d{1,2}|un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s*'
                     r'(?:\+|\s?o mas)?\s*(?:(?:-|a|y|hasta|o)\s*(\d{1,2}|dos|tres|cuatro|cinco|seis)\s*)?(anos?|years?|meses)\b')


#  Analizador
class AnalizadorDevShield:
    """
    texto    : texto completo de la vacante.
    roles    : [{'rol', 'min', 'max', 'experiencia': JUNIOR|MID|SENIOR, 'moneda': COP|USD}, ...] (bandas del admin).
    patrones : [{'frase', 'categoria': contrato|lenguaje|salario, 'peso'}, ...] frases de alerta activas (opcional).
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
        self.patrones = patrones or []
        self.t = normalizar(texto).strip()
        self.titulo = self._extraer_titulo()
        self.es_practica = bool(RX_PRACTICA.search(self.t))
        self.banderas, self.puntaje, self.hay_critica = [], 0, False
        self.experiencia_mercado, self.banda_de_nivel = 'JUNIOR', False
        self.nivel, self.nivel_inferido = 'no_indicado', False
        self.rol_detectado = self.mercado = self.salario = None
        self.anos_exigidos, self.areas_ti, self.contrato_formal = 0, [], False
        self.estado_salario = 'no_detectado'
        self.veredicto_salario = 'No se detectó un salario numérico en la oferta.'
        self.avisos = []

    #  utilidades
    def _extraer_titulo(self):
        """Zona donde suele estar el cargo: primeras líneas cortas + lo que siga a 'cargo:' / 'buscamos...'."""
        lineas = [l.strip() for l in self.t.split('\n') if l.strip()]
        cortas = [l for l in lineas[:3] if len(l) <= 110]
        partes = [' | '.join(cortas) if len(lineas) > 1 and cortas else self.t[:ZONA_TITULO]]
        for rx in (r'(?:cargo|vacante|puesto|posicion|rol|oferta|perfil)\s*[:\-]\s*([^\n]{0,120})',
                   r'(?:buscamos|busca|se busca|se requiere|se necesita|se necesitan|requerimos|necesitamos|estamos buscando|'
                   r'vacante para)\s+(?:a\s+)?(?:un|una|unos|unas|el|la)?\s*([^\n.:;]{0,100})'):
            m = re.search(rx, self.t)
            if m:
                partes.append(m.group(1))
        return ' | '.join(partes)

    @staticmethod
    def _normalizar_roles(roles):
        """Completa experiencia/moneda y lleva las bandas a COP."""
        salida = []
        for r in roles:
            exp = str(r.get('experiencia') or 'JUNIOR').upper()
            mon = str(r.get('moneda') or 'COP').upper()
            exp = exp if exp in EXPERIENCIAS_VALIDAS else 'JUNIOR'
            mon = mon if mon in MONEDAS_VALIDAS else 'COP'
            f = TRM_USD_COP if mon == 'USD' else 1
            salida.append({'rol': r['rol'], 'min': round(r['min'] * f), 'max': round(r['max'] * f),
                           'experiencia': exp, 'moneda': mon})
        return salida

    def _pool_bandas(self):
        """Bandas del nivel de la vacante si existen; si no, las junior."""
        clave = {'semi': 'MID', 'senior': 'SENIOR'}.get(self.nivel, 'JUNIOR')
        del_nivel = [r for r in self.roles if r['experiencia'] == clave]
        if del_nivel:
            self.experiencia_mercado, self.banda_de_nivel = clave, clave != 'JUNIOR'
            return del_nivel
        return [r for r in self.roles if r['experiencia'] == 'JUNIOR'] or self.roles

    def _agregar(self, titulo, detalle, puntos, severidad, categoria, critica=False):
        self.banderas.append({'titulo': titulo, 'detalle': detalle, 'severidad': severidad,
                              'categoria': categoria, 'puntos': puntos})
        self.puntaje += puntos
        self.hay_critica = self.hay_critica or critica

    #  1. Sector TI
    def validar_sector_ti(self):
        """Reglas: cargo TI en título y ninguno ajeno -> TI; cargo no-TI sin cargo TI -> rechaza salvo cuerpo muy técnico;
        sin cargo claro -> se exige evidencia suficiente (cargos y tecnologías TI)."""
        t, tit = self.t, self.titulo
        roles_tit = [p for p in ROLES_TI if _hay(p, tit)]
        roles_cuerpo = [p for p in ROLES_TI if _hay(p, t)]
        techs = [k for k, p in TECNOLOGIAS.items() if _hay(p, t)]
        no_ti = _hay(ROLES_NO_TI, tit)
        puntos = len(roles_cuerpo) * 2 + len(roles_tit) * 2 + len(techs)
        if roles_tit and not no_ti:
            ok = True
        elif no_ti and not roles_tit:
            ok = len(roles_cuerpo) >= 2 and puntos >= 8
        elif roles_tit and no_ti:
            ok = puntos >= 4
        else:
            tecnicas = [k for k in techs if k not in TECNOLOGIAS_GENERICAS]
            ok = (len(roles_cuerpo) >= 1 and puntos >= 3) or (len(techs) >= 3 and len(tecnicas) >= 2)
        if not ok:
            raise OfertaNoTIError('DevShield analiza únicamente ofertas del sector TI (desarrollo, QA, datos, soporte, '
                                  'infraestructura, ciberseguridad). No se encontraron cargos ni tecnologías de ese sector en el texto.')
        self.num_tecnologias = len([k for k in techs if k not in TECNOLOGIAS_GENERICAS])

    #  2. Nivel
    def detectar_nivel(self):
        t = self.t
        # "desarrolladores junior" / "equipo junior" nombran a otros, no al cargo ofertado
        tit = re.sub(r'(?:desarrolladores|programadores|ingenieros|analistas|testers|perfiles|equipo|equipos|a|para|con)\s+'
                     r'(?:junior|jr\.?|senior|sr\.?|semi[\s-]?senior)', ' ', self.titulo)
        pos = {}
        for nombre, rx, txt in (('junior', RX_JUNIOR, tit), ('semi', RX_SEMI, tit), ('senior', RX_SENIOR, RX_SEMI.sub(' ', tit))):
            m = rx.search(txt)
            if m:
                pos[nombre] = m.start()
        if pos:
            self.nivel = min(pos, key=pos.get)               # manda el primero en el título
        else:
            cuerpo = RX_SEMI.sub(' ', t)
            j, s, sm = len(RX_JUNIOR.findall(cuerpo)), len(RX_SENIOR.findall(cuerpo)), len(RX_SEMI.findall(t))
            if j and j >= s and j >= sm:
                self.nivel = 'junior'
            elif sm and sm >= s:
                self.nivel = 'semi'
            elif s:
                self.nivel = 'senior'
        if self.nivel == 'no_indicado' and self.anos_exigidos > 0:      # se deduce por años exigidos
            self.nivel_inferido = True
            a = self.anos_exigidos
            self.nivel = 'junior' if a <= 1.5 else 'semi' if a < 4 else 'senior'

    #  3. Cargo y banda de mercado
    def _puntaje_familias(self, ajustar_soporte=True):
        res = {f: 3 * sum(_hay(p, self.titulo) for p in c['patrones']) + sum(_hay(p, self.t) for p in c['patrones'])
               for f, c in FAMILIAS.items()}
        # "soporte técnico" solo en el cuerpo de otro cargo (p. ej. desarrollador) es función cruzada, no el cargo
        if ajustar_soporte and not any(_hay(p, self.titulo) for p in FAMILIAS['soporte']['patrones']):
            if any(v >= 3 for k, v in res.items() if k != 'soporte'):
                res['soporte'] = 0
        return res

    def detectar_rol_y_mercado(self):
        if not self.roles:
            self.mercado = dict(BANDA_POR_DEFECTO)
            self.avisos.append('No hay bandas salariales configuradas; se usó una referencia general.')
            return
        roles = self._pool_bandas()
        if len(self.areas_ti) >= 3:                           # perfil todoterreno: banda más alta entre las áreas
            cruda = self._puntaje_familias(ajustar_soporte=False)
            fams = [f for a in self.areas_ti for f in AREA_A_FAMILIAS.get(a, []) if cruda.get(f, 0) >= 1]
            invol = [r for r in roles if any(h in normalizar(r['rol']) for f in fams for h in FAMILIAS[f]['pistas'])]
            if invol:
                top = max(invol, key=lambda r: r['min'])
                self.rol_detectado, self.mercado = top['rol'], {'min': top['min'], 'max': top['max']}
                self.avisos.append('El cargo mezcla varias áreas; el salario se comparó con la banda más alta entre ellas.')
                return
        fam = self._puntaje_familias()
        cand = []
        for r in roles:
            nombre, pts = normalizar(r['rol']), 0
            for tok in (w for w in re.findall(r'[a-z0-9#+.]+', nombre) if w not in PALABRAS_GENERICAS):
                pts += 3 if _hay(re.escape(tok), self.titulo) else 1 if _hay(re.escape(tok), self.t) else 0
            pts += sum(fam[f] for f, c in FAMILIAS.items() if any(p in nombre for p in c['pistas']))
            cand.append((pts, r))
        cand.sort(key=lambda x: -x[0])
        mejor = cand[0][0]
        if mejor >= 3:
            el = [r for p, r in cand if p >= max(3, mejor * 0.75)][:2]
            self.rol_detectado = ' / '.join(r['rol'] for r in el)
            self.mercado = promedio_bandas(el)
        else:
            self.mercado = promedio_bandas(roles)
            self.avisos.append('No se identificó un cargo específico; el salario se comparó con el promedio de las bandas configuradas.')

    #  4. Experiencia oculta, áreas, funciones cruzadas, stack
    def extraer_anos(self):
        """Mayor requisito de años de experiencia (cota inferior si es rango)."""
        mayor = 0
        for m in RX_ANOS.finditer(self.t):
            if not re.search(r'experiencia|experience|trayectoria', self.t[max(0, m.start() - 60): m.end() + 60]):
                continue
            if re.search(r'somos|empresa|compania|nuestr[oa]|contamos|fundad|llevamos|lideres|mercado',
                         self.t[max(0, m.start() - 40): m.start()]):
                continue
            n = int(m.group(1)) if m.group(1).isdigit() else NUM_PALABRA.get(m.group(1), 0)
            mayor = max(mayor, n / 12 if m.group(3) == 'meses' else n)
        self.anos_exigidos = mayor

    def analizar_experiencia(self):
        a = self.anos_exigidos
        if self.nivel != 'junior' or self.nivel_inferido or a < 2:   # solo si la oferta DICE ser junior
            return
        self._agregar('Experiencia oculta', f'Pide {a:g} años de experiencia para un perfil "Junior".',
                      PESOS['exp_3'] if a >= 3 else PESOS['exp_2'], 'alta', 'lenguaje')

    def detectar_areas_ti(self):
        self.areas_ti = [a for a, (p, _) in AREAS_TI.items() if _hay(p, self.t)]

    def analizar_funciones_cruzadas(self):
        fam = self._puntaje_familias()
        mejor = max(fam, key=fam.get)
        principal = mejor if fam[mejor] >= 3 else None
        dev = 'desarrollo' in self.areas_ti
        enc = []
        for area, (patron, etiqueta) in AREAS_CRUZADAS.items():
            if principal == 'soporte' and not dev and area in ('soporte', 'hardware'):
                continue
            if _rx(patron).search(self.t):
                if area == 'soporte' and re.search(r'soporte (?:tecnico )?n[123]', self.t):
                    etiqueta = 'soporte técnico N1/N2'
                enc.append(etiqueta)
        if self.es_practica and len(self.areas_ti) < 4:
            return                                           # un practicante rota por áreas: es formación
        if len(self.areas_ti) >= 3:
            nombres = [AREAS_TI[a][1] for a in self.areas_ti]
            enc = [e for e in enc if not e.startswith('soporte técnico')]
            detalle = (f'Combina funciones de {len(nombres)} áreas distintas: {", ".join(nombres)}'
                       + (f'; además {", ".join(enc)}' if enc else '') + '.')
            self._agregar('Funciones cruzadas', detalle, PESOS['multi_4'] if len(nombres) >= 4 else PESOS['multi_3'], 'alta', 'lenguaje')
        elif enc:
            pts = min(PESOS['cruzadas_tope'], PESOS['cruzadas_base'] + PESOS['cruzadas_extra'] * (len(enc) - 1))
            self._agregar('Funciones cruzadas', f'Incluye funciones ajenas al cargo: {", ".join(enc)}.', pts, 'media', 'lenguaje')

    def analizar_stack_desproporcionado(self):
        if self.nivel != 'junior' or self.nivel_inferido or len(self.areas_ti) >= 3:
            return
        fuertes = sum(1 for v in self._puntaje_familias().values() if v >= 4)
        if self.num_tecnologias >= STACK_MAX_JUNIOR or fuertes >= 4:
            self._agregar('Stack desproporcionado',
                          f'Lista {self.num_tecnologias} tecnologías de áreas distintas para un perfil "Junior".',
                          PESOS['stack'], 'media', 'lenguaje')

    # ---- 5. Contrato
    def validar_contrato(self):
        t = self.t
        presta = bool(RX_PRESTACION.search(t))
        formal = bool(RX_CONTRATO_FORMAL.search(RX_SIN_PRESTACIONES.sub(' ', t)))   # "sin prestaciones" no es contrato formal
        if RX_SIN_PRESTACIONES.search(t):
            self._agregar('Sin prestaciones sociales', 'La oferta aclara que no incluye prestaciones sociales ni seguridad social.',
                          PESOS['prestacion'], 'media', 'contrato')
        if presta:
            if RX_SUBORDINACION.search(t):
                self._agregar('Contrato sin garantías', 'Prestación de servicios con horario fijo o exclusividad: posible '
                              'relación laboral encubierta, sin prestaciones sociales.', PESOS['contrato_encubierto'], 'alta', 'contrato')
            else:
                self._agregar('Contrato sin garantías', 'Prestación de servicios: sin prestaciones sociales ni estabilidad laboral.',
                              PESOS['prestacion'], 'media', 'contrato')
        elif not formal and not self.es_practica and (not RX_CONTRATO_MENCION.search(t) or RX_CONTRATO_VAGO.search(t)):
            self._agregar('Tipo de contrato no especificado', 'La oferta no indica qué tipo de contrato ofrece.',
                          PESOS['contrato_no_esp'], 'baja', 'contrato')
        sin_extras = re.sub(r'horas? extras? (?:sin|no) (?:remuner\w*|pag\w*)|sin pago de horas? extras?', ' ', t)
        if RX_NO_PAGO.search(sin_extras):
            self._agregar('Trabajo no remunerado', 'La oferta indica que la actividad no tiene remuneración.',
                          PESOS['critica'], 'alta', 'contrato', critica=True)
        if RX_GANANCIAS_IRREALES.search(t):
            self._agregar('Promesa de ganancias irreales', 'Promete ingresos ilimitados o garantizados: típico de ofertas fraudulentas.',
                          PESOS['critica'], 'alta', 'contrato', critica=True)
        if RX_SOLO_COMISION.search(t):
            self._agregar('Ingresos solo por comisión', 'No garantiza un salario fijo; el ingreso depende de resultados.',
                          PESOS['comision'], 'alta', 'contrato')
        self.contrato_formal = formal and not presta

    # 6. Fraude
    def analizar_fraude(self):
        t = self.t
        cobro = any(not RX_FRAUDE_NEGATIVO.search(t[max(0, m.start() - 60): m.end() + 60])
                    for rx in RX_FRAUDE_COBRO for m in rx.finditer(t))
        if cobro:
            self._agregar('Posible fraude: cobro al candidato', 'Menciona pagos, inversión o descuentos a cargo del candidato '
                          '(capacitación, kit, inscripción, etc.).', PESOS['critica'], 'alta', 'contrato', critica=True)
        if any(rx.search(t) for rx in RX_FRAUDE_DATOS):
            self._agregar('Posible fraude: datos sensibles', 'Solicita claves, tarjetas o datos bancarios antes de contratar.',
                          PESOS['critica'], 'alta', 'contrato', critica=True)
        if RX_SOLO_MENSAJERIA.search(t) or (RX_MENSAJERIA_ACTIVA.search(t) and not RX_CANAL_FORMAL.search(t)):
            self._agregar('Contacto solo por mensajería', 'El único canal es WhatsApp/Telegram; verifica que la empresa exista.',
                          PESOS['mensajeria'], 'baja', 'contrato')
        if RX_URGENCIA.search(t):
            self._agregar('Presión de urgencia', 'Te pide actuar de inmediato o habla de cupos limitados.',
                          PESOS['urgencia'], 'baja', 'lenguaje')
        if any(a in self.areas_ti for a in ('desarrollo', 'mobile', 'datos', 'infra')) and RX_REQ_INCONGRUENTES.search(t):
            self._agregar('Requisitos incongruentes con el cargo', 'Pide solo conocimientos básicos para un cargo técnico que '
                          'paga bien: poco habitual en ofertas legítimas.', PESOS['req_incong'], 'media', 'lenguaje')

    #  7. Sobrecarga y presión
    def _horas_semanales(self):
        """Estima horas semanales ('lunes a sábado de 7 am a 5 pm', '12 horas diarias'). Devuelve horas o None."""
        t = self.t
        dias = re.search(r'(?<![a-z])lunes\s*(?:a|al|-|–|/)\s*(viernes|sabado|domingo)\b|(?<![a-z])l\s*[-–/]\s*(v|s|d)\b', t)
        n_dias = None
        if dias:
            n_dias = {'viernes': 5, 'v': 5, 'sabado': 6, 's': 6, 'domingo': 7, 'd': 7}[dias.group(1) or dias.group(2)]
        elif re.search(r'(?:6|seis) dias (?:a la|por) semana|lunes a sabado', t):
            n_dias = 6
        if dias:
            zona = t[max(0, dias.start() - 50): dias.end() + 70]
            fr = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*(?:a|-|–|hasta)\s*(?:las\s*)?'
                           r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?!\s*(?:anos?|meses|millones|mil|smmlv|%|\d))', zona)
            if fr:
                h1, m1, mer1, h2, m2, mer2 = fr.groups()
                h1, h2 = int(h1), int(h2)
                if h1 <= 23 and h2 <= 23:
                    if mer2:
                        mer2 = mer2.replace('.', '')
                        mer1 = (mer1 or ('am' if mer2 == 'pm' and h1 < 12 else mer2)).replace('.', '')
                        ini = (h1 % 12) + (12 if mer1 == 'pm' else 0) + int(m1 or 0) / 60
                        fin = (h2 % 12) + (12 if mer2 == 'pm' else 0) + int(m2 or 0) / 60
                    else:                                    # "8 a 6" -> 8:00 a 18:00
                        ini, fin = h1 + int(m1 or 0) / 60, h2 + int(m2 or 0) / 60
                        if fin <= ini and h2 <= 12:
                            fin += 12
                    dur = fin - ini
                    if 3 <= dur <= 16:
                        return (dur - 1 if dur > 6 else dur) * n_dias       # 1 h de almuerzo
        m = re.search(r'\b(\d{1,2})\s*horas?\s*(?:diarias?|por dia|al dia)|jornadas? de (\d{1,2})\s*horas', t)
        if m and 9 <= int(m.group(1) or m.group(2)) <= 16:
            return int(m.group(1) or m.group(2)) * (n_dias or (6 if re.search(r'sabados?', t) else 5))
        return None

    def analizar_horarios_y_presion(self):
        t = self.t
        horas = self._horas_semanales()
        if horas and horas > JORNADA_ALERTA_HORAS:
            self._agregar('Jornada excesiva', f'El horario publicado suma ≈{horas:.0f} h semanales; el máximo legal es de '
                          f'{JORNADA_MAX_SEMANAL} h (Ley 2101 de 2021).', PESOS['jornada'], 'alta', 'lenguaje')
        etiquetas, puntos = [], 0
        m = re.search(r'(?:disponibilidad|turnos?|guardias?|laborar|trabajar|trabajo)\b.{0,60}(?:fines? de semana|sabados|domingos|festivos)', t)
        if m and not re.search(r'\bsin\b|\bno\b|libres?|descanso', m.group(0)):
            etiquetas.append('turnos o disponibilidad en fines de semana')
            puntos += 6
        for rx, etiqueta, p in HORARIOS_ABUSIVOS:
            m = rx.search(t)
            if not m:
                continue
            if etiqueta.startswith('guardias') and re.search(r'compensad|remunerad|pagad|recargo|descanso|tiempo libre|bono',
                                                             t[max(0, m.start() - 50): m.end() + 60]):
                continue                                     # guardia compensada: no es abuso
            etiquetas.append(etiqueta)
            puntos += p
        if etiquetas:
            puntos = min(PESOS['sobrecarga_tope'], puntos)
            self._agregar('Sobrecarga horaria', f'Menciona {", ".join(etiquetas)}.', puntos,
                          'alta' if puntos >= PESOS['sobrecarga_tope'] else 'media', 'lenguaje')
        frases = [p for p in LENGUAJE_PRESION if _hay(p, t)]
        if len(frases) >= 2:
            self._agregar('Lenguaje de presión', 'Usa expresiones que suelen ocultar sobrecarga de trabajo o falta de límites.',
                          PESOS['presion_4'] if len(frases) >= 4 else PESOS['presion_2'], 'media', 'lenguaje')

    #  8. Salario
    def _candidatos_salario(self):
        t, cands = self.t, []
        trabajo = [t]                                        # copia mutable donde se "blanquean" cifras ya usadas

        def contexto_ok(ini, fin, exigir_clave):
            antes = t[max(0, ini - 35): ini]
            no_sal = [m.start() for m in RX_NO_SALARIO_ANTES.finditer(antes)]
            if no_sal:
                sal = [m.start() for m in re.finditer(r'salari|sueldo|remuner', antes)]
                if not sal or max(sal) < max(no_sal):
                    return False                             # "bono", "auxilio"... más cerca que "salario"
            if RX_NO_SALARIO_DESPUES.search(t[fin: fin + 30]):
                return False
            return bool(RX_CLAVE_SALARIO.search(t[max(0, ini - 80): fin + 40])) if exigir_clave else True

        def periodo(ini, fin):
            despues = re.split(r'\+|;|\n|,\s|\.\s|\s(?:mas|y|con|sin|pero)\s', t[fin: fin + 40])[0]
            antes = t[max(0, ini - 60): ini]
            prev = re.search(r'(quincenal(?:es)?|semanal(?:es)?|diari[oa]s?|anual(?:es)?|mensual(?:es)?)'
                             r'[\s:$]*(?:de|es de|ascienden a)?[\s:$]*(?:cop|usd)?[\s$]*$', antes)
            if prev:
                return {'quincenal': 2, 'semanal': 4.345, 'diari': 22, 'anual': 1 / 12}.get(
                    next((k for k in ('quincenal', 'semanal', 'diari', 'anual') if prev.group(1).startswith(k)), ''), 1)
            if re.search(r'por hora|/ ?hora|/ ?h\b|per hour|hourly|la hora|x hora|por h\b', despues):
                return HORAS_MES
            if re.search(r'al ano|anual|por ano|/ ?ano|per year|annual|yearly', despues) or \
                    re.search(r'(?:salario|sueldo|remuneracion|pago|compensacion|ingreso)s?\s+anual(?:es)?[\s:$]*(?:de)?[\s:$]*(?:cop|usd)?[\s$]*$', antes):
                return 1 / 12
            if re.search(r'quincenal|por quincena|cada quincena', despues):
                return 2
            if re.search(r'semanal|por semana|a la semana', despues):
                return 4.345
            if re.search(r'diari[oa]|por dia', despues):
                return 22
            return 1

        def usd_cerca(ini, fin):
            return bool(RX_USD.search(t[max(0, ini - 20): fin + 25]))

        def agregar(vmin, vmax, ini, fin, usd=False, explicito=True):
            f = (TRM_USD_COP if usd else 1) * periodo(ini, fin)
            vmin, vmax = vmin * f, vmax * f
            if not (300_000 <= vmin <= 60_000_000 and vmax >= vmin):
                return
            v = t[max(0, ini - 80): fin + 40]
            pts = (2 if re.search(r'salari|sueldo|remuner|honorari|ofrecemos|pagamos', v) else 0) + (1 if explicito else 0) + (1 if 'mensual' in v else 0)
            proy = bool(RX_POR_PROYECTO.search(t[max(0, ini - 60): fin + 60])) and 'mensual' not in v
            cands.append({'min': vmin, 'max': vmax, 'inicio': ini, 'puntaje': pts, 'convertido': usd, 'por_proyecto': proy})

        # Cada paso: (regex, función(m) -> (vmin, vmax, usd, explicito) o None). Se aplican en orden y consumen la cifra.
        def rango(k, m): a, b = _f(m.group(1)) * k, _f(m.group(2)) * k; return min(a, b), max(a, b)

        def p_mill_rango(m): return contexto_ok(m.start(), m.end(), False) and (*rango(1e6, m), usd_cerca(m.start(), m.end()), True)
        def p_mill(m): return contexto_ok(m.start(), m.end(), False) and (_f(m.group(1)) * 1e6,) * 2 + (usd_cerca(m.start(), m.end()), True)
        def p_mil(m): return contexto_ok(m.start(), m.end(), False) and (_f(m.group(1)) * 1000,) * 2 + (usd_cerca(m.start(), m.end()), True)
        def p_smr(m): return (*rango(SMMLV_2026, m), False, True)
        def p_sm(m): return (_f(m.group(1)) * SMMLV_2026,) * 2 + (False, True)
        def p_smsolo(m): return (SMMLV_2026, SMMLV_2026, False, False)

        def p_hora(m):
            usd = bool(re.search(r'usd|us\$|dolar', (m.group(1) or '') + ' ' + (m.group(3) or ''))) or usd_cerca(m.start(), m.end())
            if usd or re.search(r'\$|cop|pesos', (m.group(1) or '') + (m.group(3) or '')):
                return (_f(m.group(2)) * HORAS_MES,) * 2 + (usd, True)

        def p_krango(m):
            usd = usd_cerca(m.start(), m.end()) or 'usd' in m.group(0) or 'us$' in m.group(0)
            if (usd or '$' in m.group(0)) and contexto_ok(m.start(), m.end(), True):
                return (*rango(1000, m), usd, True)

        def p_mrango(m): return contexto_ok(m.start(), m.end(), True) and (*rango(1e6, m), usd_cerca(m.start(), m.end()), True)

        def p_mpal(m):
            if contexto_ok(m.start(), m.end(), False):
                v = (NUM_PALABRA[m.group(1)] + EXTRA_PALABRA.get(m.group(2) or '', 0)) * 1e6
                return v, v, usd_cerca(m.start(), m.end()), True

        def p_mabrev(m):
            if (m.group(1) or contexto_ok(m.start(), m.end(), True)) and contexto_ok(m.start(), m.end(), False):
                v = _f(m.group(2)) * 1e6
                return v, v, usd_cerca(m.start(), m.end()), True

        def p_kabrev(m):
            usd = usd_cerca(m.start(), m.end()) or 'usd' in (m.group(1) or '')
            if (usd or m.group(1)) and contexto_ok(m.start(), m.end(), True):
                v = _f(m.group(2)) * 1000
                return v, v, usd, True

        for rx, fn in ((RX_MILL_RANGO, p_mill_rango), (RX_MILL, p_mill), (RX_MIL_MONEDA, p_mil), (RX_SMMLV_RANGO, p_smr),
                       (RX_SMMLV, p_sm), (RX_SM_SOLO, p_smsolo), (RX_HORA, p_hora), (RX_K_RANGO, p_krango),
                       (RX_M_RANGO, p_mrango), (RX_MILL_PALABRA, p_mpal), (RX_M_ABREV, p_mabrev), (RX_K_ABREV, p_kabrev)):
            for m in list(rx.finditer(trabajo[0])):
                r = fn(m)
                if r:
                    agregar(r[0], r[1], m.start(), m.end(), r[2], r[3])
                trabajo[0] = trabajo[0][:m.start()] + ' ' * (m.end() - m.start()) + trabajo[0][m.end():]

        # cifras completas (con separadores o de 4-9 dígitos), con posible rango "a - b"
        montos = []
        for m in RX_MONTO.finditer(trabajo[0]):
            valor = int(re.sub(r'[.,]', '', m.group(1)))
            usd = usd_cerca(m.start(), m.end())
            simbolo = usd or '$' in t[max(0, m.start() - 3): m.start()] or bool(re.search(r'cop|pesos|usd|dolar', t[m.end(): m.end() + 12]))
            por_hora = bool(re.match(r'\s*(?:cop|pesos|usd|dolares)?\s*(?:por hora|/ ?hora|/ ?h\b|x hora|la hora)', t[m.end(): m.end() + 25]))
            if (not usd and not por_hora and valor < 300_000) or valor > 100_000_000:
                continue                                     # años (2026), cantidades pequeñas, etc.
            if contexto_ok(m.start(), m.end(), not simbolo):
                montos.append({'valor': valor, 'ini': m.start(), 'fin': m.end(), 'usd': usd, 'sim': simbolo})
        i = 0
        while i < len(montos):
            a = montos[i]
            b = montos[i + 1] if i + 1 < len(montos) else None
            if b and RX_UNION.match(t[a['fin']: b['ini']]) and b['valor'] > a['valor']:
                agregar(a['valor'], b['valor'], a['ini'], b['fin'], a['usd'] or b['usd'], a['sim'] or b['sim'])
                i += 2
            else:
                agregar(a['valor'], a['valor'], a['ini'], a['fin'], a['usd'], a['sim'])
                i += 1
        return cands

    def extraer_salario(self):
        cands = self._candidatos_salario()
        if not cands:
            return
        c = max(cands, key=lambda c: (c['puntaje'], -c['inicio']))
        a, b = c['min'], c['max']
        texto = _cop(a) if a == b else f'{_cop(a)} – {_cop(b)}'
        if c['convertido']:
            texto += ' (aprox., TRM ' + f'{TRM_USD_COP:,.0f}'.replace(',', '.') + ')'
        self.salario = {'min': a, 'max': b, 'texto': texto, 'medio': round((a + b) / 2),
                        'convertido': c['convertido'], 'por_proyecto': c['por_proyecto']}

    def _veredicto(self, estado, texto):
        self.estado_salario, self.veredicto_salario = estado, texto

    def analizar_salario(self):
        self.extraer_salario()
        s, banda = self.salario, self.mercado
        rol = f' ({self.rol_detectado})' if self.rol_detectado else ''

        if s is None:
            if RX_VAGO.search(self.t):
                self._agregar('Salario a convenir', 'Opacidad en el rango salarial ofertado.', PESOS['convenir'], 'media', 'salario')
            return self._veredicto('no_detectado', 'No se detectó un salario numérico en la oferta.')
        if s['por_proyecto']:                                # no es mensual: no se compara
            return self._veredicto('no_aplica', 'Pago por proyecto: no es comparable con un salario mensual.')
        if self.es_practica:
            return self._veredicto('no_aplica', 'Práctica o pasantía: la banda salarial junior no aplica.')
        if s['max'] < SMMLV_2026:
            self._agregar('Salario por debajo del mínimo legal',
                          f'Ofrece {s["texto"]} COP; el salario mínimo legal 2026 es {_cop(SMMLV_2026)}.',
                          PESOS['critica'], 'alta', 'salario', critica=True)
            return self._veredicto('bajo', 'Por debajo del salario mínimo legal.')

        # Semi-senior / senior sin banda propia: la junior escalada por nivel es la referencia mínima
        if self.nivel in ('semi', 'senior') and not self.banda_de_nivel:
            factor = NIVEL_FACTOR[self.nivel]
            piso = banda['min'] * factor
            et = 'semi-senior' if self.nivel == 'semi' else 'senior'
            if s['max'] < piso:
                deficit = (piso - s['max']) / piso
                self._agregar('Salario bajo para el nivel',
                              f'Ofrece {s["texto"]} COP para un perfil {et}; lo habitual parte de {_cop(piso)} (banda junior x{factor:g}).',
                              PESOS['sal_bajo_medio'] if deficit <= 0.35 else PESOS['sal_bajo_fuerte'], 'media', 'salario')
                self._veredicto('bajo', f'Bajo para un perfil {et}{rol}: lo habitual parte de {_cop(piso)}.')
            else:
                self._veredicto('ok', f'Coherente con un perfil {et}{rol}.')
            self.avisos.append('La vacante no parece de nivel junior; la banda mostrada es la junior.')
            return

        bmin, bmax = banda['min'], banda['max']
        if s['max'] < bmin:
            d = (bmin - s['max']) / bmin
            pts = PESOS['sal_bajo_leve'] if d <= 0.10 else PESOS['sal_bajo_medio'] if d <= 0.25 else PESOS['sal_bajo_fuerte']
            sev = 'alta' if pts >= PESOS['sal_bajo_fuerte'] else 'media' if pts >= PESOS['sal_bajo_medio'] else 'baja'
            self._agregar('Salario bajo mercado', f'Ofrece {s["texto"]} COP, por debajo del rango de mercado{rol}.', pts, sev, 'salario')
            self._veredicto('bajo', f'Por debajo del mínimo de mercado{rol}.')
        elif s['min'] < bmin:
            self._agregar('Salario inicia bajo mercado',
                          f'El rango ofrecido ({s["texto"]} COP) empieza por debajo del mínimo de mercado{rol}.',
                          PESOS['sal_inicia_bajo'], 'baja', 'salario')
            self._veredicto('limite', f'Parte del rango está por debajo del mínimo de mercado{rol}.')
        elif self.nivel == 'junior' and s['min'] > bmax * SALARIO_SOSPECHOSO_X:
            if RX_SIN_EXPERIENCIA.search(self.t):
                self._agregar('Salario inusualmente alto', f'{s["texto"]} COP sin pedir experiencia es muy superior a la banda{rol}: '
                              'suena demasiado bueno para ser verdad.', PESOS['sal_alto_sin_exp'], 'alta', 'salario')
            else:
                self._agregar('Salario inusualmente alto', f'{s["texto"]} COP es muy superior a lo habitual para un perfil junior{rol}; '
                              'verifica que la oferta sea legítima.', PESOS['sal_alto'], 'media', 'salario')
            self._veredicto('alto', f'Muy por encima del rango de mercado{rol}.')
        elif s['min'] > bmax:
            self._veredicto('alto', f'Por encima del rango de mercado{rol}.')
        else:
            self._veredicto('ok', f'Dentro del rango de mercado{rol}.')

    # 9. Frases del administrador y combinaciones
    def analizar_patrones_admin(self):
        """Busca las frases de alerta activas que el administrador mantiene en la BD (RF-05)."""
        restante = PATRONES_ADMIN_TOPE
        for p in self.patrones:
            frase = normalizar(str(p.get('frase', ''))).strip()
            if not frase:
                continue
            rx = re.compile(r'(?<![a-z0-9])' + r'\s+'.join(re.escape(w) for w in frase.split()) + r'(?![a-z0-9])')
            if not rx.search(self.t):
                continue
            cat = p.get('categoria') if p.get('categoria') in CATEGORIAS_VALIDAS else 'lenguaje'
            try:
                peso = max(0, int(p.get('peso', 5)))
            except (TypeError, ValueError):
                peso = 5
            pts = min(peso, restante)
            restante -= pts
            self._agregar(f'Frase de alerta: «{frase}»',
                          'La oferta contiene una expresión que el sistema tiene registrada como señal de alerta.',
                          pts, 'alta' if peso >= 15 else 'media' if peso >= 8 else 'baja', cat)

    def analizar_combinaciones(self):
        """Señales leves que juntas describen una estafa típica."""
        alta = {b['titulo'] for b in self.banderas if b['severidad'] == 'alta'}
        todas = {b['titulo'] for b in self.banderas}
        if 'Salario inusualmente alto' in alta and todas & {'Contacto solo por mensajería',
                                                            'Requisitos incongruentes con el cargo', 'Presión de urgencia'}:
            self._agregar('Posible fraude: oferta demasiado buena', 'Salario muy superior al mercado, sin exigir experiencia y con '
                          'señales de contacto o requisitos poco serios: patrón típico de ofertas fraudulentas.',
                          PESOS['critica'], 'alta', 'salario', critica=True)

    # Ejecutar
    def ejecutar(self):
        self.validar_sector_ti()
        self.extraer_anos()
        self.detectar_nivel()
        self.detectar_areas_ti()
        self.detectar_rol_y_mercado()
        for paso in (self.analizar_experiencia, self.analizar_funciones_cruzadas, self.analizar_stack_desproporcionado,
                     self.validar_contrato, self.analizar_fraude, self.analizar_horarios_y_presion, self.analizar_salario,
                     self.analizar_patrones_admin, self.analizar_combinaciones):
            paso()

        puntaje = min(self.puntaje, 100)
        altas = sum(1 for b in self.banderas if b['severidad'] == 'alta')
        if self.hay_critica or altas >= 2:
            puntaje = max(puntaje, UMBRAL_ALTO)
        elif altas:
            puntaje = max(puntaje, UMBRAL_MEDIO)

        n = len(self.banderas)
        if puntaje >= UMBRAL_ALTO:
            nivel = 'Riesgo alto'
            desc = (f'Se detectaron {n} señales de alerta' + (f' ({altas} crítica{"s" if altas != 1 else ""})' if altas else '')
                    + ' en esta oferta laboral.')
            if any(b['titulo'].startswith('Posible fraude') for b in self.banderas):
                rec = ('No realices pagos ni compartas datos sensibles. Verifica que la empresa exista por canales oficiales '
                       'antes de continuar con el proceso.')
            else:
                rec = ('Se sugiere cautela antes de aceptar esta oferta. El salario, el contrato o las funciones presentan '
                       'inconsistencias. Considera negociar o preguntar más detalles al reclutador.')
        elif puntaje >= UMBRAL_MEDIO:
            nivel = 'Riesgo medio'
            desc = 'Se detectaron algunas señales de alerta que conviene revisar antes de postularte.'
            rec = ('La oferta tiene algunos puntos a aclarar. Antes de postularte, pregunta directamente sobre las '
                   'condiciones marcadas como banderas rojas.')
        else:
            nivel = 'Riesgo bajo'
            desc = ('Solo se detectaron observaciones menores en esta oferta laboral.' if n
                    else 'La oferta cumple con la mayoría de parámetros de transparencia.')
            rec = 'La vacante parece alineada a los estándares de un perfil junior en el mercado.'
            if self.contrato_formal:
                desc += ' Menciona un contrato laboral formal.'
        if self.avisos:
            desc += ' Nota: ' + ' '.join(self.avisos)

        desglose = {c: 0 for c in CATEGORIAS_VALIDAS}
        for b in self.banderas:
            desglose[b['categoria']] += b['puntos']
        orden = {'alta': 0, 'media': 1, 'baja': 2}
        banderas = sorted(self.banderas, key=lambda b: (orden[b['severidad']], -b['puntos']))
        s = self.salario
        return {
            'porcentaje': puntaje, 'nivel': nivel, 'descripcion': desc, 'recomendacion': rec,
            'banderas': [{k: b[k] for k in ('titulo', 'detalle', 'severidad', 'categoria')} for b in banderas],
            'salario': s['medio'] if s else None, 'salario_texto': s['texto'] if s else None,
            'salario_estado': self.estado_salario, 'salario_veredicto': self.veredicto_salario,
            'mercado': self.mercado, 'experiencia_mercado': self.experiencia_mercado,
            'rol_detectado': self.rol_detectado, 'nivel_detectado': self.nivel,
            'sector_ti': True, 'desglose': desglose,
        }
        
