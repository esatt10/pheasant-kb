"""Named technologies, usage evidence, and passive website fingerprints."""

import re

# The first spelling is the display name. Later spellings normalize common aliases.
_FAMILIES = {
    "Cloud & infrastructure": (
        "AWS|Amazon Web Services",
        "Azure|Microsoft Azure",
        "Google Cloud|Google Cloud Platform|GCP",
        "Oracle Cloud Infrastructure|OCI",
        "IBM Cloud",
        "Cloudflare",
        "VMware vSphere|vSphere",
        "OpenStack",
        "Terraform",
        "Pulumi",
        "Ansible",
        "AWS CloudFormation|CloudFormation",
        "Microsoft Bicep|Azure Bicep",
        "HashiCorp Packer|Packer",
        "NVIDIA CUDA|CUDA",
    ),
    "Containers & delivery": (
        "Kubernetes|K8s",
        "OpenShift",
        "Docker",
        "Helm",
        "Argo CD|ArgoCD",
        "Flux CD|FluxCD",
        "Istio",
        "Linkerd",
        "Amazon EKS|EKS",
        "Azure Kubernetes Service|AKS",
        "Google Kubernetes Engine|GKE",
        "GitHub Actions",
        "GitLab",
        "Jenkins",
        "CircleCI",
        "Azure DevOps",
        "Tekton",
        "Buildkite",
        "Bazel",
        "Nix",
    ),
    "Data & analytics": (
        "Snowflake",
        "Databricks",
        "BigQuery|Google BigQuery",
        "Amazon Redshift|Redshift",
        "Microsoft Fabric",
        "Azure Synapse Analytics|Azure Synapse",
        "Apache Spark|PySpark",
        "Kafka|Apache Kafka",
        "Apache Flink|Flink",
        "Airflow|Apache Airflow",
        "dbt",
        "Fivetran",
        "Airbyte",
        "Dagster",
        "Apache Iceberg|Iceberg",
        "Delta Lake",
        "Power BI",
        "Tableau",
        "Looker",
        "Qlik Sense",
    ),
    "Databases & search": (
        "PostgreSQL|Postgres",
        "MySQL",
        "MariaDB",
        "SQL Server|MSSQL",
        "Oracle Database",
        "MongoDB",
        "Redis",
        "Amazon DynamoDB|DynamoDB",
        "Azure Cosmos DB|Cosmos DB",
        "Elasticsearch",
        "OpenSearch",
        "Apache Cassandra|Cassandra",
        "ClickHouse",
        "Pinecone",
        "Weaviate",
        "pgvector",
        "Qdrant",
    ),
    "AI & machine learning": (
        "PyTorch",
        "TensorFlow",
        "JAX",
        "Hugging Face Transformers",
        "vLLM",
        "TensorRT",
        "NVIDIA Triton|Triton Inference Server",
        "Ray Serve",
        "KubeFlow|Kubeflow",
        "MLflow",
        "Weights & Biases|W&B",
        "OpenAI",
        "Azure OpenAI",
        "Microsoft Foundry|Azure AI Foundry",
        "Amazon Bedrock|AWS Bedrock",
        "Vertex AI",
        "Amazon SageMaker|SageMaker",
        "LangChain",
        "LlamaIndex",
        "Anthropic Claude|Claude API",
        "Model Context Protocol|MCP",
        "scikit-learn|scikit learn",
        "XGBoost",
        "LightGBM",
        "CatBoost",
        "ONNX Runtime",
        "KServe",
        "NVIDIA NeMo|NeMo Framework",
        "Hugging Face Diffusers",
        "Ollama",
    ),
    "Optimization & operations research": (
        "Google OR-Tools|Google OR Tools|OR-Tools",
        "Gurobi Optimizer|Gurobi",
        "IBM ILOG CPLEX|ILOG CPLEX|CPLEX",
        "FICO Xpress Optimization|FICO Xpress",
        "Timefold Solver",
        "OptaPlanner",
        "Pyomo",
        "SCIP Optimization Suite",
        "HiGHS Solver",
        "Julia JuMP|JuMP.jl",
        "COIN-OR PuLP",
    ),
    "Business process & workflow automation": (
        "UiPath",
        "Automation Anywhere",
        "SS&C Blue Prism|Blue Prism",
        "Microsoft Power Automate|Power Automate",
        "Camunda",
        "Temporal Workflow|Temporal.io",
        "n8n",
        "Workato",
        "Zapier",
        "Pega Platform",
        "Appian Platform",
        "IBM Business Automation Workflow",
    ),
    "CRM & business operations": (
        "Salesforce",
        "HubSpot",
        "Microsoft Dynamics 365|Dynamics 365",
        "SAP",
        "SAP Business Technology Platform|SAP BTP",
        "ServiceNow",
        "Zendesk",
        "Intercom",
        "Marketo",
        "Jira",
        "Confluence",
        "Microsoft Power Platform|Power Platform",
        "MuleSoft",
        "Boomi",
    ),
    "Enterprise resource planning (ERP)": (
        "SAP S/4HANA|S/4HANA|SAP S4HANA",
        "SAP ECC|SAP ERP Central Component",
        "SAP Business One",
        "SAP Business ByDesign",
        "NetSuite|Oracle NetSuite",
        "Oracle Fusion Cloud ERP|Oracle Fusion ERP",
        "Oracle E-Business Suite|Oracle EBS",
        "Oracle JD Edwards EnterpriseOne|JD Edwards EnterpriseOne|JD Edwards",
        "Oracle PeopleSoft|PeopleSoft",
        "Microsoft Dynamics 365 Finance|Dynamics 365 Finance",
        "Microsoft Dynamics 365 Business Central|Dynamics 365 Business Central",
        "Microsoft Dynamics AX|Dynamics AX",
        "Microsoft Dynamics NAV|Dynamics NAV",
        "Infor CloudSuite Industrial|Infor SyteLine",
        "Infor LN",
        "Infor M3",
        "Epicor Kinetic",
        "Epicor Prophet 21",
        "IFS Cloud",
        "Sage X3",
        "Sage Intacct",
        "Workday Financial Management",
        "QAD Adaptive ERP",
        "Unit4 ERP",
        "Deltek Costpoint",
        "Odoo|Odoo ERP",
        "Acumatica|Acumatica Cloud ERP",
    ),
    "HR & workforce systems": (
        "Workday",
        "SAP SuccessFactors|SuccessFactors",
        "Oracle Fusion Cloud HCM|Oracle Cloud HCM",
        "UKG Pro",
        "ADP Workforce Now",
    ),
    "Warehouse management (WMS)": (
        "SAP Extended Warehouse Management|SAP EWM|SAP S/4HANA for Extended Warehouse Management",
        "SAP Warehouse Management|SAP WM|SAP LE-WM",
        "Oracle Fusion Cloud Warehouse Management|Oracle Warehouse Management Cloud"
        "|Oracle WMS Cloud",
        "Manhattan Active Warehouse Management|Manhattan Active WM|Manhattan Active WMS",
        "Manhattan SCALE",
        "Blue Yonder Warehouse Management|Blue Yonder WMS|JDA Warehouse Management|JDA WMS",
        "Infor WMS|Infor Warehouse Management",
        "Körber Warehouse Management|Körber WMS|Koerber WMS|HighJump WMS",
        "Tecsys Elite WMS",
        "Softeon WMS",
        "Deposco Bright Warehouse",
        "Made4net WarehouseExpert|WarehouseExpert",
        "Extensiv 3PL Warehouse Manager",
        "Odoo Inventory|Odoo Warehouse Management",
    ),
    "Warehouse execution (WES)": ("Honeywell Momentum WES|Momentum Warehouse Execution System",),
    "Warehouse controls & automation": (
        "Swisslog SynQ|SynQ",
        "Dematic iQ",
        "AutoStore",
    ),
    "Transportation & freight (TMS)": (
        "SAP Transportation Management|SAP TM",
        "Oracle Transportation Management|Oracle OTM",
        "Manhattan Active Transportation Management|Manhattan Active TM|Manhattan Active TMS",
        "Blue Yonder Transportation Management|Blue Yonder TMS|JDA Transportation Management",
        "Descartes Transportation Management|Descartes TMS",
        "MercuryGate TMS|MercuryGate",
        "Trimble TMW.Suite|TMW.Suite",
        "Transporeon",
    ),
    "Freight visibility & forwarding": (
        "project44",
        "FourKites",
        "CargoWise",
    ),
    "Supply chain planning & procurement": (
        "Blue Yonder",
        "Manhattan Active",
        "Microsoft Dynamics 365 Supply Chain Management|Dynamics 365 Supply Chain Management",
        "SAP Integrated Business Planning|SAP IBP",
        "SAP Ariba|Ariba",
        "Kinaxis Maestro",
        "Kinaxis RapidResponse|RapidResponse",
        "o9 Digital Brain",
        "Blue Yonder Demand Planning",
    ),
    "Order management (OMS)": (
        "Manhattan Active Order Management|Manhattan Active OM",
        "IBM Sterling Order Management",
        "Salesforce Order Management",
        "Fluent Order Management",
    ),
    "Manufacturing execution (MES)": (
        "Siemens Opcenter|Opcenter Execution|Opcenter",
        "Rockwell FactoryTalk ProductionCentre|FactoryTalk ProductionCentre",
        "Plex MES|Rockwell Plex",
        "SAP Digital Manufacturing",
        "DELMIA Apriso",
        "AVEVA Manufacturing Execution System|AVEVA MES",
        "GE Vernova Proficy Plant Applications|Proficy Plant Applications",
        "MasterControl Manufacturing Excellence",
    ),
    "Quality management (QMS)": (
        "ETQ Reliance",
        "TrackWise Digital",
    ),
    "Industrial automation & SCADA": (
        "Siemens TIA Portal|TIA Portal",
        "Siemens SIMATIC|SIMATIC",
        "Rockwell FactoryTalk|FactoryTalk",
        "Allen-Bradley ControlLogix|ControlLogix",
        "Ignition SCADA",
        "AVEVA PI System|OSIsoft PI",
        "AVEVA System Platform|Wonderware System Platform",
        "Schneider Electric EcoStruxure|EcoStruxure",
        "Emerson DeltaV|DeltaV",
        "Honeywell Experion PKS|Experion PKS",
        "GE Vernova iFIX|Proficy iFIX",
        "OPC UA",
        "MQTT",
        "PROFINET",
        "EtherNet/IP",
    ),
    "Product lifecycle management (PLM)": (
        "Siemens Teamcenter|Teamcenter",
        "Siemens NX",
        "PTC Windchill|Windchill",
        "Dassault Systèmes ENOVIA|ENOVIA",
        "Dassault Systèmes 3DEXPERIENCE|3DEXPERIENCE",
        "Autodesk Vault",
    ),
    "Engineering design (CAD)": (
        "SolidWorks",
        "Autodesk AutoCAD|AutoCAD",
    ),
    "Healthcare & clinical systems": (
        "Epic EHR|Epic EMR",
        "Oracle Health Millennium|Cerner Millennium",
        "MEDITECH Expanse",
        "athenaOne",
        "eClinicalWorks",
        "HL7 FHIR|FHIR",
        "LabWare LIMS",
        "LabVantage LIMS",
    ),
    "Banking, payments & insurance systems": (
        "Guidewire InsuranceSuite",
        "Guidewire PolicyCenter",
        "Guidewire ClaimCenter",
        "Guidewire BillingCenter",
        "Duck Creek Policy",
        "Temenos Transact|Temenos T24",
        "Finastra Fusion Essence",
        "nCino Bank Operating System",
        "Mambu",
        "Fiserv DNA",
        "ISO 20022",
    ),
    "Retail, commerce & POS": (
        "Oracle Retail Xstore|Xstore POS",
        "SAP Commerce Cloud|SAP Hybris",
        "Adobe Commerce|Magento Commerce",
        "Shopify POS",
        "Lightspeed Retail",
        "Toast POS",
        "Cegid Retail",
        "LS Central",
    ),
    "Construction & project delivery": (
        "Autodesk Construction Cloud",
        "Procore",
        "Oracle Primavera P6|Primavera P6",
        "Bentley ProjectWise|ProjectWise",
    ),
    "Enterprise asset management (EAM)": (
        "IBM Maximo",
        "HxGN EAM|Infor EAM",
        "SAP Asset Performance Management",
    ),
    "Field service management": (
        "Salesforce Field Service",
        "Microsoft Dynamics 365 Field Service|Dynamics 365 Field Service",
        "ServiceTitan",
    ),
    "Security & observability": (
        "Okta",
        "CrowdStrike",
        "Microsoft Entra ID|Azure Active Directory|Azure AD",
        "Ping Identity",
        "Zscaler",
        "Splunk",
        "Microsoft Sentinel|Azure Sentinel",
        "HashiCorp Vault",
        "Snyk",
        "Wiz Cloud|Wiz Security",
        "Datadog",
        "New Relic",
        "Grafana",
        "Prometheus",
        "OpenTelemetry|OTel",
        "PagerDuty",
        "Sentry",
        "Dynatrace",
    ),
    "Application development": (
        "Python",
        "TypeScript",
        "JavaScript",
        "React",
        "Next.js|NextJS",
        "Angular",
        "Vue.js|VueJS",
        "Node.js|NodeJS",
        "Java",
        "Kotlin",
        ".NET|ASP.NET",
        "C#",
        "C++",
        "Golang|Go language",
        "Rust",
        "Ruby on Rails|Rails",
        "Django",
        "FastAPI",
        "Spring Boot",
        "GraphQL",
        "gRPC",
        "WebAssembly|Wasm",
    ),
}
TECHNOLOGY_CATEGORY = {
    item.split("|")[0]: family for family, items in _FAMILIES.items() for item in items
}
_CASE_SENSITIVE = {
    "SAP",
    "AWS",
    "GCP",
    "OCI",
    "AKS",
    "GKE",
    "EKS",
    "JAX",
    "MCP",
    "React",
    "Java",
    "dbt",
    "Sentry",
    "Ariba",
    "Rails",
    "Intercom",
    "Workday",
    "Ignition SCADA",
    "Iceberg",
}
_ALIASES = sorted(
    (
        (alias, names.split("|")[0])
        for items in _FAMILIES.values()
        for names in items
        for alias in names.split("|")
    ),
    key=lambda item: -len(item[0]),
)


def _alias_pattern(index: int, alias: str) -> str:
    pattern = re.escape(alias)
    if alias not in _CASE_SENSITIVE:
        pattern = f"(?i:{pattern})"
    return rf"(?<![\w-])(?P<p{index}>{pattern})(?![\w-])"


_MATCH = re.compile("|".join(_alias_pattern(i, alias) for i, (alias, _) in enumerate(_ALIASES)))

_HIRING = re.compile(
    r"\b(?:experience|proficiency|familiarity|expertise|qualifications?|requirements?|"
    r"required|preferred|skills?|knowledge of|you will|hands.on|work with|build and maintain)\b",
    re.I,
)
_CHANGE = re.compile(
    r"\b(?:migrat\w+|roll(?:ing)? out|adopt\w+|implement\w+|evaluat\w+|pilot\w*|moderniz\w+|"
    r"transition(?:ing)? to)\b",
    re.I,
)
_INTEGRATION = re.compile(
    r"\b(?:integrat\w*|compatib\w*|connector|supports?|works with|for customers|for clients)\b",
    re.I,
)
_INTERNAL = re.compile(
    r"\b(?:we (?:use|run|operate|deploy|build|rely on)|our (?:internal|production|engineering|"
    r"infrastructure|stack)|running in production|our teams use)\b",
    re.I,
)
_PRODUCT = re.compile(
    r"(?:^|/)(?:docs?|documentation|developers?|integrations?|products?|platform|solutions?|app)(?:/|$)",
    re.I,
)
_TECHNICAL = re.compile(
    r"\b(?:engineering|developer|architecture|integrations?|technology|technical|"
    r"documentation|customer stories|case stud(?:y|ies)|enterprise resource planning|"
    r"warehouse management|warehouse execution|transportation management|supply chain planning|"
    r"order management|manufacturing execution|industrial automation|"
    r"product lifecycle management|clinical systems|core banking|field service|"
    r"operations research|mathematical optimi[sz]ation|workflow automation|"
    r"robotic process automation|machine learning|generative ai)\b",
    re.I,
)

RELATIONS = {
    "internal_use",
    "change_in_progress",
    "hiring",
    "product_or_integration",
    "mentioned",
    "public_website",
}


def technologies(passage: str, *, kind: str, path: str, first_party: bool) -> dict[str, str]:
    matches = {}
    for match in _MATCH.finditer(passage):
        name = _ALIASES[int(match.lastgroup[1:])][1]
        window = passage[max(0, match.start() - 120) : match.end() + 65]
        if kind == "jobs":
            relation = "hiring" if _HIRING.search(window) else "mentioned"
        elif first_party and _CHANGE.search(window) and re.search(r"\b(?:we|our)\b", window, re.I):
            relation = "change_in_progress"
        elif first_party and _INTERNAL.search(window):
            relation = "internal_use"
        elif first_party and (_INTEGRATION.search(window) or _PRODUCT.search(path)):
            relation = "product_or_integration"
        else:
            relation = "mentioned"
        matches.setdefault(name, relation)
    return matches


def technical_link(path: str, label: str) -> bool:
    return bool(_PRODUCT.search(path) or _TECHNICAL.search(path.replace("-", " ") + " " + label))


_WEB_MARKERS = {
    "Next.js": ("/_next/static/", "__next_data__"),
    "Nuxt": ("/_nuxt/", "__nuxt_data__"),
    "Gatsby": ("gatsby-script", "gatsby-focus-wrapper"),
    "WordPress": ("/wp-content/", "/wp-includes/", "wordpress"),
    "Drupal": ("/core/misc/drupal", "drupalsettings", "drupal"),
    "Shopify": ("cdn.shopify.com/", "shopify"),
    "Webflow": ("webflow.js", "webflow"),
    "Google Tag Manager": ("googletagmanager.com/gtm.js", "googletagmanager.com/ns.html"),
    "Google Analytics": ("google-analytics.com/analytics.js", "googletagmanager.com/gtag/js?"),
    "Adobe Experience Platform": ("assets.adobedtm.com/",),
    "HubSpot Tracking Code": ("js.hs-scripts.com/", "js.hs-analytics.net/"),
    "Marketo Munchkin": ("munchkin.marketo.net/",),
    "Segment Analytics": ("cdn.segment.com/analytics.js/",),
    "Pendo": ("cdn.pendo.io/agent/",),
    "Hotjar": ("static.hotjar.com/",),
    "OneTrust": ("cdn.cookielaw.org/", "cdn.onetrust.com/"),
    "Cookiebot": ("consent.cookiebot.com/",),
    "Intercom Messenger": ("widget.intercom.io/",),
    "Zendesk Web Widget": ("static.zdassets.com/ekr/",),
}
WEB_TECHNOLOGY = frozenset(_WEB_MARKERS)
_CANONICAL = {alias.casefold(): label for alias, label in _ALIASES}
_CANONICAL.update((name.casefold(), name) for name in WEB_TECHNOLOGY)


def canonical_name(value: str) -> str:
    value = value.strip()
    return _CANONICAL.get(value.casefold(), value)


def website_markers(resources: list[str]) -> list[str]:
    values = [value.casefold() for value in resources]
    return [
        name
        for name, markers in _WEB_MARKERS.items()
        if any(
            marker in value
            if len(marker) > 12 or "/" in marker
            else marker == value.split(" ", 1)[0]
            for marker in markers
            for value in values
        )
    ]
