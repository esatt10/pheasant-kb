"""Field catalog and the versioned research request."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Kind = Literal["official", "jobs", "news", "filing", "external"]
ValueType = Literal["text", "integer", "number", "boolean", "url", "date", "enum"]
Scalar = str | int | float | bool
Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Seed = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
MAX_RESEARCH_LIMIT = 250
PageLimit = Annotated[int, Field(ge=4, le=MAX_RESEARCH_LIMIT)]
ExcerptLimit = Annotated[int, Field(ge=1, le=MAX_RESEARCH_LIMIT)]
ProductPageLimit = Annotated[int, Field(ge=0, le=MAX_RESEARCH_LIMIT)]


def words(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Observation(Model):
    field: Key
    passage_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]
    value: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | int | float | bool
    quote: str = Field(min_length=12, max_length=380)
    context: str | None = Field(default=None, max_length=300)


class ReviewRequest(Model):
    observations: list[Observation] = Field(max_length=120)


class Acquisition(Model):
    follow_links: bool = True
    embedded_data: bool = True
    site_adapters: bool = True
    job_board_urls: list[Seed] = Field(default_factory=list, max_length=5)
    identity_fallback_urls: list[Seed] = Field(default_factory=list, max_length=5)
    allow_partial: bool = False
    max_attempts: int = Field(default=2, ge=1, le=4)
    retry_delay_seconds: int = Field(default=0, ge=0, le=60)


class RunControl(Model):
    paused: bool | None = None
    acquisition: Acquisition | None = None
    page_limit: PageLimit | None = None
    excerpt_limit: ExcerptLimit | None = None
    add_urls: list[Seed] = Field(default_factory=list, max_length=20)
    retry_urls: list[Seed] = Field(default_factory=list, max_length=20)


class Condition(Model):
    field: Key
    equals: Scalar


class Band(Model):
    through: int = Field(ge=0)
    value: Term


class Enrichment(Model):
    enabled: bool = True
    model: Term = "auto"


class FieldSpec(Model):
    key: Key
    definition: str = Field(min_length=8, max_length=1200)
    type: ValueType = "text"
    hubspot: Key | None = None
    options: list[Term] = Field(default_factory=list, max_length=100)
    keywords: list[Term] = Field(default_factory=list, max_length=12)
    labels: list[Term] = Field(default_factory=list, max_length=8)
    sources: list[Kind] = Field(
        default_factory=lambda: ["official", "filing", "news", "external"],
        max_length=5,
    )
    max_age_days: int | None = Field(default=None, ge=1, le=36500)
    applies_when: list[Condition] = Field(default_factory=list, max_length=8)
    derive_from: Key | None = None
    bands: list[Band] = Field(default_factory=list, max_length=25)
    multiple: bool = False
    min_origins: int = Field(default=1, ge=1, le=5)
    inference: bool = False
    rich_text: bool | None = None

    @model_validator(mode="after")
    def validate_spec(self) -> FieldSpec:
        if self.rich_text and (self.type != "text" or self.derive_from):
            raise ValueError("rich_text requires a non-derived text field")
        if self.type == "enum" and not self.options:
            raise ValueError("enum fields need options")
        if len({words(option) for option in self.options}) != len(self.options):
            raise ValueError("enum options must be distinct after normalization")
        if self.bands:
            if not self.derive_from or self.type != "enum" or self.multiple:
                raise ValueError("bands require derive_from and a single-valued enum")
            limits = [band.through for band in self.bands]
            if limits != sorted(set(limits)) or any(
                b.value not in self.options for b in self.bands
            ):
                raise ValueError("bands require increasing, unique limits and defined options")
        if self.derive_from and not self.bands:
            raise ValueError("derive_from requires bands")
        return self


class Request(Model):
    company: str | None = Field(default=None, min_length=1, max_length=200)
    website: Seed | None = None
    urls: list[Seed] = Field(default_factory=list, max_length=20)
    naics_code: (
        Annotated[str, StringConstraints(pattern=r"^(?:[0-9]{2,6}|[0-9]{2}-[0-9]{2})$")] | None
    ) = None
    fields: list[FieldSpec] = Field(default_factory=list, max_length=40)
    depth: Literal["standard", "deep"] = "standard"
    page_limit: PageLimit = 36
    excerpt_limit: ExcerptLimit | None = None
    job_limit: int = Field(default=24, ge=0, le=60)
    product_page_limit: ProductPageLimit = 0
    product_urls: list[Seed] = Field(default_factory=list, max_length=20)
    signal_days: int = Field(default=365, ge=1, le=3650)
    search: bool = True
    review: bool = False
    decisions: bool = False
    model: Term | None = None
    decision_model: Term | None = None
    enrichment: Enrichment = Field(default_factory=Enrichment)
    acquisition: Acquisition = Field(default_factory=Acquisition)

    @model_validator(mode="after")
    def validate_request(self) -> Request:
        if self.depth == "deep":
            if "page_limit" not in self.model_fields_set:
                self.page_limit = 80
            if "job_limit" not in self.model_fields_set:
                self.job_limit = 36
        elif self.excerpt_limit is not None:
            raise ValueError("excerpt_limit requires deep research")
        if self.naics_code is not None:
            from pheasant_gtm.naics import titles

            if self.naics_code not in titles():
                raise ValueError("Unknown 2022 NAICS code")
        self.company = (self.company.strip() or None) if self.company else None
        if (
            self.company
            and not (self.website or self.urls)
            and re.match(r"^(?:https?://|[^\s/]+\.[a-zA-Z]{2,63}(?:[/?#]|$))", self.company)
        ):
            if " " not in self.company:
                self.website, self.company = self.company, None
        if not (self.company or self.website or self.urls):
            raise ValueError("provide a company name, a website, or seed URLs")
        if self.model and not self.review:
            raise ValueError("model requires review")
        if self.decision_model and not (self.review and self.decisions):
            raise ValueError("decision_model requires review and decisions")
        if self.product_urls and not self.product_page_limit:
            raise ValueError("product_urls requires a positive product_page_limit")
        keys = [field.key for field in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError("custom field keys must be unique")
        ordered_fields(self)
        return self


def _fact(key, definition, type="text", hubspot=None, **kwargs) -> FieldSpec:
    return FieldSpec(key=key, definition=definition, type=type, hubspot=hubspot, **kwargs)


_OFFICIAL = ["official", "filing"]
_SIGNALS = ["official", "jobs", "news", "filing", "external"]
_BANDS = [
    (10, "1–10"),
    (50, "11–50"),
    (200, "51–200"),
    (500, "201–500"),
    (1000, "501–1,000"),
    (5000, "1,001–5,000"),
    (10000, "5,001–10,000"),
    (10**12, "10,001+"),
]
DEFAULT_FIELDS = (
    _fact(
        "name", "Official operating name of the target business.", hubspot="name", sources=_OFFICIAL
    ),
    _fact("legal_name", "Registered legal name of the target entity.", sources=_OFFICIAL),
    _fact("domain", "Primary website hostname of the matched company.", hubspot="domain"),
    _fact("website", "Canonical homepage of the matched company.", "url", "website"),
    _fact(
        "about",
        "Public short overview of the business, its products, and customers.",
        hubspot="about_us",
        keywords=["about", "we provide"],
    ),
    _fact(
        "description",
        "Company's stated mission or what the business does.",
        hubspot="description",
        keywords=["mission", "our purpose"],
    ),
    _fact(
        "industry",
        "Primary industry of the company in plain language.",
        hubspot="industry",
        keywords=["industry", "sector"],
    ),
    _fact(
        "industry_group",
        "Broad industry classification with a named taxonomy if available.",
        keywords=["industry", "sector"],
    ),
    _fact(
        "naics_code",
        "U.S. 2022 NAICS code assigned or explicitly reported for this company.",
        sources=_OFFICIAL,
    ),
    _fact(
        "founded_year",
        "Four-digit founding year of the business.",
        "integer",
        "founded_year",
        keywords=["founded", "established"],
    ),
    _fact(
        "employees",
        "Exact company-wide employee count.",
        "integer",
        "numberofemployees",
        keywords=["employees", "headcount"],
        max_age_days=730,
        sources=_OFFICIAL,
    ),
    _fact(
        "employee_range",
        "Employee size band derived from a sourced exact headcount.",
        "enum",
        options=[label for _, label in _BANDS],
        derive_from="employees",
        bands=[Band(through=n, value=label) for n, label in _BANDS],
    ),
    _fact(
        "annual_revenue",
        "Reported total annual revenue in original currency for the latest complete fiscal year. "
        "Include currency and period in evidence.",
        "number",
        "annualrevenue",
        keywords=["annual revenue", "total revenue", "net sales"],
        max_age_days=920,
        sources=_OFFICIAL,
    ),
    _fact(
        "revenue_currency",
        "ISO 4217 currency code for the selected annual revenue. Use its source and fiscal year.",
        keywords=["annual revenue", "usd", "eur", "gbp"],
        max_age_days=920,
        sources=_OFFICIAL,
    ),
    _fact(
        "revenue_fiscal_year",
        "Fiscal year of reported total annual revenue.",
        "integer",
        keywords=["fiscal year", "full year"],
        max_age_days=920,
        sources=_OFFICIAL,
    ),
    _fact(
        "is_public",
        "Whether the target entity itself is currently publicly traded. "
        "Private ownership requires explicit evidence.",
        "boolean",
        "is_public",
        keywords=["publicly traded", "privately held", "stock exchange"],
        sources=_OFFICIAL,
    ),
    _fact(
        "ticker",
        "Exchange-assigned ticker for the target's publicly traded shares.",
        applies_when=[Condition(field="is_public", equals=True)],
        keywords=["ticker", "trades under", "nasdaq", "nyse"],
        sources=_OFFICIAL,
    ),
    _fact(
        "address",
        "Street address of the company's corporate headquarters.",
        hubspot="address",
        keywords=["headquarters", "principal executive offices"],
        sources=_OFFICIAL,
    ),
    _fact(
        "city",
        "City of the company's headquarters.",
        hubspot="city",
        keywords=["headquarters", "based in"],
        sources=_OFFICIAL,
    ),
    _fact(
        "region",
        "State or province of the company's headquarters.",
        hubspot="state",
        keywords=["headquarters", "based in"],
        sources=_OFFICIAL,
    ),
    _fact(
        "postal_code",
        "Postal code of the company's headquarters.",
        hubspot="zip",
        keywords=["headquarters", "postal code"],
        sources=_OFFICIAL,
    ),
    _fact(
        "country",
        "Country of the company's headquarters.",
        hubspot="country",
        keywords=["headquarters", "based in"],
        sources=_OFFICIAL,
    ),
    _fact(
        "country_code",
        "Two-letter ISO code for the headquarters country.",
        keywords=["headquarters", "country"],
        sources=_OFFICIAL,
    ),
    _fact(
        "phone",
        "Public corporate switchboard or headquarters phone number.",
        hubspot="phone",
        keywords=["contact", "phone"],
        sources=["official"],
    ),
    _fact(
        "linkedin_company",
        "Official company LinkedIn page URL.",
        "url",
        "linkedin_company_page",
        sources=["official"],
    ),
    _fact(
        "facebook_company",
        "Official company Facebook page URL.",
        "url",
        "facebook_company_page",
        sources=["official"],
    ),
    _fact(
        "twitter_handle",
        "Official company handle on X (formerly Twitter).",
        hubspot="twitterhandle",
        sources=["official"],
    ),
    _fact(
        "total_funding",
        "Publicly disclosed cumulative funding raised. Include currency and reporting date.",
        "number",
        "total_money_raised",
        keywords=["total funding", "funding to date", "raised"],
        max_age_days=1095,
    ),
    _fact(
        "timezone",
        "IANA time zone of the company's headquarters.",
        keywords=["timezone", "time zone"],
    ),
    _fact(
        "linkedin_handle",
        "Official LinkedIn company profile handle (company/slug).",
        sources=["official"],
    ),
    _fact(
        "parent_company",
        "Direct corporate parent if the relationship is explicitly documented.",
        keywords=["subsidiary of", "parent company", "owned by"],
    ),
    _fact(
        "products_services",
        "Marketed products and services offered by the company.",
        keywords=["solutions", "platform", "products", "services"],
        multiple=True,
        sources=["official"],
    ),
    _fact(
        "business_activities",
        "Publicly evidenced categories of products and services the company describes.",
        multiple=True,
        sources=_OFFICIAL,
    ),
    _fact(
        "customer_segments",
        "Customer segments or industries the company explicitly serves.",
        keywords=["customers", "industries", "solutions"],
        multiple=True,
        sources=["official"],
    ),
    _fact(
        "support_services",
        "Documented support offerings, channels, or service tiers available to customers.",
        keywords=["support", "managed services", "sla", "professional services"],
        multiple=True,
        sources=["official"],
    ),
    _fact(
        "technology_stack",
        "Named technologies tied to company use or hiring requirements. "
        "Distinguish observed deployment, a mention, and a hiring requirement.",
        keywords=["platform", "stack", "migration", "experience"],
        multiple=True,
        sources=_SIGNALS,
        max_age_days=730,
    ),
    _fact(
        "web_technologies",
        "Technology detected in public website markup, including analytics, frameworks "
        "and marketing tags.",
        multiple=True,
        sources=_OFFICIAL,
    ),
    _fact(
        "capabilities_in_development",
        "Capabilities the company describes building, expanding, or modernizing. "
        "Label hiring evidence as an indicator.",
        keywords=["build", "scale", "modernize", "launch", "expand"],
        multiple=True,
        inference=True,
        sources=_SIGNALS,
        max_age_days=365,
    ),
    _fact(
        "service_need_hypotheses",
        "Potential external services opportunities inferred from dated evidence. "
        "Describe the trigger and uncertainty. Budget and procurement intent require direct proof.",
        keywords=["migration", "implementation", "partner", "transformation", "rollout"],
        multiple=True,
        inference=True,
        sources=_SIGNALS,
        max_age_days=365,
    ),
)


def ordered_fields(request: Request) -> list[FieldSpec]:
    fields = {field.key: field for field in (*DEFAULT_FIELDS, *request.fields)}
    ordered: list[FieldSpec] = []
    pending: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in pending or key not in fields:
            raise ValueError(f"invalid or cyclic field dependency: {key}")
        pending.add(key)
        field = fields[key]
        for dependency in field.applies_when:
            visit(dependency.field)
            upstream = fields[dependency.field]
            if upstream.multiple or upstream.inference:
                raise ValueError(f"{key} requires a factual, single-valued condition")
        if field.derive_from:
            visit(field.derive_from)
            upstream = fields[field.derive_from]
            if upstream.type != "integer" or upstream.multiple or upstream.inference:
                raise ValueError(f"{key} requires a factual, single-valued integer upstream")
        pending.remove(key)
        visited.add(key)
        ordered.append(field)

    for key in fields:
        visit(key)
    return ordered


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# These properties depend on a particular HubSpot account, its relationships or analytics.
CRM_ONLY = [
    "owner",
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_assigneddate",
    "createdate",
    "closedate",
    "lastmodifieddate",
    "num_associated_contacts",
    "num_associated_deals",
    "hs_object_id",
    "hs_analytics_source",
    "notes_last_contacted",
    "notes_last_updated",
    "hs_ideal_customer_profile",
]
