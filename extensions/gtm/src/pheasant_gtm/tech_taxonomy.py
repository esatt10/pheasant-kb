"""Versioned technology concepts, with a W3C SKOS representation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from pheasant_gtm.technology import TECHNOLOGY_CATEGORY, WEB_TECHNOLOGY

SCHEME = "urn:pheasant:gtm:technology"
VERSION = 1


@dataclass(frozen=True)
class Concept:
    id: str
    label: str
    broader: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


CONCEPTS = (
    Concept("ai", "AI & machine learning", aliases=("AI", "AI/ML", "artificial intelligence")),
    Concept("machine-learning", "Machine learning", ("ai",), ("ML", "ML frameworks")),
    Concept("generative-ai", "Generative AI", ("ai",), ("GenAI", "LLM", "large language models")),
    Concept("mlops", "Machine learning operations", ("ai",), ("MLOps", "model operations")),
    Concept(
        "optimization",
        "Optimization & operations research",
        aliases=("optimization", "optimisation", "operations research", "constraint programming"),
    ),
    Concept("automation", "Automation", aliases=("process automation",)),
    Concept(
        "workflow-automation",
        "Business process & workflow automation",
        ("automation",),
        ("RPA", "robotic process automation", "workflow automation", "business process automation"),
    ),
    Concept("supply-chain", "Supply chain & logistics", aliases=("supply chain", "logistics")),
    Concept("enterprise-applications", "Enterprise applications", aliases=("enterprise software",)),
    Concept("manufacturing-engineering", "Manufacturing & engineering systems"),
    Concept("cloud-infrastructure", "Cloud & infrastructure"),
    Concept("containers-delivery", "Containers & delivery", aliases=("CI/CD", "DevOps")),
    Concept("data-analytics", "Data & analytics"),
    Concept("databases-search", "Databases & search"),
    Concept("crm", "CRM & business operations", ("enterprise-applications",)),
    Concept("erp", "Enterprise resource planning (ERP)", ("enterprise-applications",)),
    Concept("hcm", "HR & workforce systems", ("enterprise-applications",)),
    Concept("wms", "Warehouse management (WMS)", ("supply-chain",)),
    Concept("wes", "Warehouse execution (WES)", ("supply-chain", "automation")),
    Concept("wcs", "Warehouse controls & automation", ("supply-chain", "automation")),
    Concept("tms", "Transportation & freight (TMS)", ("supply-chain",)),
    Concept("freight-visibility", "Freight visibility & forwarding", ("supply-chain",)),
    Concept("supply-chain-planning", "Supply chain planning & procurement", ("supply-chain",)),
    Concept("oms", "Order management (OMS)", ("supply-chain", "enterprise-applications")),
    Concept("mes", "Manufacturing execution (MES)", ("manufacturing-engineering",)),
    Concept("qms", "Quality management (QMS)", ("manufacturing-engineering",)),
    Concept("scada", "Industrial automation & SCADA", ("manufacturing-engineering", "automation")),
    Concept("plm", "Product lifecycle management (PLM)", ("manufacturing-engineering",)),
    Concept("cad", "Engineering design (CAD)", ("manufacturing-engineering",)),
    Concept("ehr", "Healthcare & clinical systems"),
    Concept("financial-systems", "Banking, payments & insurance systems"),
    Concept("pos", "Retail, commerce & POS"),
    Concept("construction", "Construction & project delivery"),
    Concept("eam", "Enterprise asset management (EAM)", ("enterprise-applications",)),
    Concept("field-service", "Field service management", ("enterprise-applications",)),
    Concept("security-observability", "Security & observability"),
    Concept("application-development", "Application development"),
    Concept("public-website", "Public website", aliases=("web technologies",)),
)

_BY_LABEL = {concept.label: concept.id for concept in CONCEPTS}
_TERMS = {
    term.casefold(): concept.id
    for concept in CONCEPTS
    for term in (concept.id, concept.label, *concept.aliases)
}
_AI_SUBSETS = {
    "machine-learning": (
        "PyTorch",
        "TensorFlow",
        "JAX",
        "scikit-learn",
        "XGBoost",
        "LightGBM",
        "CatBoost",
        "ONNX Runtime",
        "Hugging Face Transformers",
    ),
    "generative-ai": (
        "OpenAI",
        "Azure OpenAI",
        "Microsoft Foundry",
        "Amazon Bedrock",
        "Vertex AI",
        "Anthropic Claude",
        "Hugging Face Transformers",
        "vLLM",
        "LangChain",
        "LlamaIndex",
        "Model Context Protocol",
        "NVIDIA NeMo",
        "Hugging Face Diffusers",
        "Ollama",
    ),
    "mlops": (
        "KubeFlow",
        "MLflow",
        "Weights & Biases",
        "NVIDIA Triton",
        "Ray Serve",
        "TensorRT",
        "Amazon SageMaker",
        "ONNX Runtime",
        "KServe",
        "Vertex AI",
    ),
}


def concept_id(category: str) -> str:
    return _BY_LABEL[category]


def canonical_category(value: str) -> str | None:
    return _TERMS.get(value.strip().casefold())


@cache
def _members(category: str) -> frozenset[str]:
    names = {name for name, label in TECHNOLOGY_CATEGORY.items() if _BY_LABEL[label] == category}
    names.update(_AI_SUBSETS.get(category, ()))
    if category == "public-website":
        names.update(WEB_TECHNOLOGY)
    for concept in CONCEPTS:
        if category in concept.broader:
            names.update(_members(concept.id))
    return frozenset(names)


def category_products(category: str) -> list[str]:
    return sorted(_members(category))


def technology_labels(names: set[str]) -> list[str]:
    return [concept.label for concept in CONCEPTS if names.intersection(_members(concept.id))]


def taxonomy(*, skos: bool = False) -> dict:
    if not skos:
        return {
            "id": SCHEME,
            "version": VERSION,
            "concepts": [
                {
                    "id": concept.id,
                    "label": concept.label,
                    "broader": list(concept.broader),
                    "aliases": list(concept.aliases),
                    "product_count": len(_members(concept.id)),
                }
                for concept in CONCEPTS
            ],
        }
    return {
        "@context": {
            "skos": "http://www.w3.org/2004/02/skos/core#",
            "owl": "http://www.w3.org/2002/07/owl#",
        },
        "@graph": [
            {
                "@id": SCHEME,
                "@type": "skos:ConceptScheme",
                "skos:prefLabel": {"@value": "Pheasant GTM technology", "@language": "en"},
                "owl:versionInfo": str(VERSION),
                "skos:hasTopConcept": [
                    {"@id": f"{SCHEME}:{concept.id}"} for concept in CONCEPTS if not concept.broader
                ],
            },
            *[
                {
                    "@id": f"{SCHEME}:{concept.id}",
                    "@type": "skos:Concept",
                    "skos:notation": concept.id,
                    "skos:prefLabel": {"@value": concept.label, "@language": "en"},
                    "skos:altLabel": [
                        {"@value": alias, "@language": "en"} for alias in concept.aliases
                    ],
                    "skos:inScheme": {"@id": SCHEME},
                    "skos:broader": [{"@id": f"{SCHEME}:{parent}"} for parent in concept.broader],
                }
                for concept in CONCEPTS
            ],
        ],
    }
