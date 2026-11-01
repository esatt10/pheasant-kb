"""The compose answer shape: a new artifact assembled from documented parts.

What it changes is exactly what assumed the finished thing exists in the
corpus — the plan (one query per component), the sufficiency question, the
single-call write, the draft effort and the acceptance rewrite — and nothing
for the two intents that were already there.
"""

from __future__ import annotations

from types import SimpleNamespace

from pheasant.assistant.chat import COMPOSE_SYSTEM, system_prompt_for
from pheasant.assistant.workflows import decision_gates
from pheasant.assistant.workflows.agentic import (
    COMPOSE_DEFAULTS,
    DEFAULTS,
    PLANNER_COMPOSE_RULE,
    PLANNER_SYSTEM,
    resolve_options,
    synthesis_window,
)
from tests.test_decisions_api import _LLM, _decider, _hit, _run, _Search, _Wire

PLAN = {
    "queries": ["azurerm_key_vault example", "azurerm_private_endpoint example"],
    "modes": ["hybrid"],
    "reasoning": "one per component",
}


def _search_everything() -> _Search:
    search = _Search({})
    search.search_context = lambda kb, query, *args, **kwargs: (
        search.calls.append(query) or {"results": [_hit("key_vault")], "counts": {}}
    )
    return search


def test_compose_plans_one_query_per_component_and_writes_once_at_full_effort(
    monkeypatch,
) -> None:
    llm = _LLM([PLAN, '```hcl\nresource "x" "y" {}\n```\n# [1]'])
    # Sufficient, then an artifact the grounded judge distrusts but which
    # answers the question: reported, never rewritten.
    wire = _Wire({"sufficient": 0.9}, {"grounded": 0.02, "responsive": 0.95})

    result = _run(_search_everything(), llm, _decider(), monkeypatch, wire, intent="compose")

    planner, writer = llm.log
    assert "1 to 24 queries" in planner["system"] and PLANNER_COMPOSE_RULE in planner["system"]
    assert writer["system"].startswith(COMPOSE_SYSTEM), "the compose prompt, no length rule"
    assert writer["max_output_tokens"] == COMPOSE_DEFAULTS["max_output_tokens"]
    assert writer["effort"] is None, "an artifact is never drafted at the low draft effort"
    assert llm.calls == 2, "no rewrite on a grounded-only rejection"
    assert result.route["intent"] == "compose"
    assert "plan" in [s.name for s in result.steps], "staged retrieval is skipped: plan first"
    assert "Evidence gap:" not in result.answer
    accept = next(s for s in result.steps if s.name == "accept")
    assert "reported for artifacts rather than acted on" in accept.detail
    sufficiency = wire.requests[0][1]["questions"][0]
    assert sufficiency["name"] == "sufficient"
    assert "NEW artifact" in sufficiency["instructions"]
    grounded = wire.requests[1][1]["questions"][0]
    assert decision_gates.GROUNDED_COMPOSE in grounded["instructions"]


def test_the_accept_gate_never_rewrites_an_artifact(monkeypatch) -> None:
    # A responsive miss on an artifact is reported, not acted on: the rewrite
    # it used to buy dropped two of the components the question asked for.
    llm = _LLM([PLAN, '```hcl\nresource "x" "y" {}\n```\n# [1]'])
    wire = _Wire({"sufficient": 0.9}, {"grounded": 0.9, "responsive": 0.1})

    result = _run(_search_everything(), llm, _decider(), monkeypatch, wire, intent="compose")

    assert llm.calls == 2, "plan and one write; no rewrite"
    assert result.decisions["repairs"] == 0 and result.decisions["accepted"] is False


def test_the_other_intents_keep_their_prompts_and_planner_breadth() -> None:
    assert "1 to 3 queries" in PLANNER_SYSTEM.replace(
        "{planned_queries}", str(DEFAULTS["planned_queries"])
    )
    assert "COMPOSE" not in system_prompt_for("procedural")
    assert decision_gates.sufficiency_question("procedural") == (
        decision_gates.SUFFICIENCY + decision_gates.CRITERIA["procedural"]
    )
    assert decision_gates.sufficiency_question("compose") == decision_gates.SUFFICIENCY_COMPOSE


def test_compose_options_win_over_the_configured_general_keys() -> None:
    configured = {
        **DEFAULTS,
        "staged_retrieval": True,
        "combine_grade_and_answer": True,
        "max_context_passages": 16,
        "compose_options": {"max_context_passages": 30},
    }
    state = {"options": configured, "explicit_options": list(configured), "intent": "compose"}
    options = resolve_options(state)

    assert options["staged_retrieval"] is False
    assert options["combine_grade_and_answer"] is False
    assert options["max_context_passages"] == 30, "compose_options over COMPOSE_DEFAULTS"
    assert options["planned_queries"] == COMPOSE_DEFAULTS["planned_queries"]

    other = resolve_options({**state, "intent": "procedural"})
    assert other["staged_retrieval"] is True and other["max_context_passages"] == 16


def _passage(node: str, n: int) -> SimpleNamespace:
    return SimpleNamespace(node_id=node, chunk=n, key=lambda: f"{node}#{n}")


def test_the_synthesis_window_caps_passages_per_document_in_rank_order() -> None:
    ranked = [_passage("a", 1), _passage("a", 2), _passage("a", 3), _passage("b", 1)]
    ranked += [_passage("c", 1), _passage("a", 4)]

    capped = synthesis_window(ranked, {"max_context_passages": 4, "passages_per_document": 2})
    assert [(p.node_id, p.chunk) for p in capped] == [("a", 1), ("a", 2), ("b", 1), ("c", 1)]

    plain = synthesis_window(ranked, {"max_context_passages": 4, "passages_per_document": None})
    assert plain == ranked[:4], "unset keeps the plain top-N"


def _page(path: str, n: int = 0) -> SimpleNamespace:
    return SimpleNamespace(node_id=path, relative_path=path, title=path, key=lambda: f"{path}#{n}")


def test_each_named_components_own_page_enters_the_window_ahead_of_overviews() -> None:
    from pheasant.assistant.workflows.compose import own_pages_first

    # Two overviews top every per-component query; the components' own pages
    # rank third and below, and a window of four would hold none of them.
    ranked = [_page("learn/gateway-overview.txt", n) for n in range(3)]
    ranked += [_page("learn/backends.txt"), _page("r/api_management_backend.html.markdown")]
    ranked += [_page("r/api_management.html.markdown", n) for n in (1, 2)]
    queries = ["azurerm_api_management StandardV2", "azurerm_api_management_backend circuit"]

    window = synthesis_window(
        own_pages_first(ranked, ["Write the gateway", *queries]),
        {"max_context_passages": 4, "passages_per_document": 2},
    )
    assert [(p.node_id, p.key()) for p in window[:2]] == [
        ("r/api_management_backend.html.markdown", "r/api_management_backend.html.markdown#0"),
        ("r/api_management.html.markdown", "r/api_management.html.markdown#1"),
    ], "one passage per own page, in rank order; its second passage keeps its rank"
    assert [p.node_id for p in window[2:]] == ["learn/gateway-overview.txt"] * 2


def test_a_type_path_or_a_whole_document_name_names_its_page_and_other_words_do_not() -> None:
    from pheasant.assistant.workflows.compose import own_pages_first

    ranked = [_page("learn/overview.txt"), _page("templates/2024-05-01/service/backends.txt")]
    moved = own_pages_first(ranked, ["Microsoft.ApiManagement/service/backends pool"])
    assert moved[0].node_id == "templates/2024-05-01/service/backends.txt"
    assert own_pages_first(ranked, ["a backend pool with a circuit breaker"]) == ranked
    assert own_pages_first(ranked, ["backends pool"])[0].node_id == ranked[1].node_id


# ------------------------------------------------------------- the review pass

DOCS = {
    1: SimpleNamespace(
        title="r/subnet.html.markdown",
        text='resource "azurerm_subnet" "x" {\n  address_prefixes = []\n'
        '  private_endpoint_network_policies = "Enabled"\n}',
    ),
    2: SimpleNamespace(
        title="r/ai_foundry.html.markdown",
        text='resource "azurerm_ai_foundry" "x" {\n  application_insights_id = ""\n}',
    ),
    3: SimpleNamespace(
        title="r/cognitive_account.html.markdown",
        text='resource "azurerm_cognitive_account" "x" {\n  custom_subdomain_name = ""\n}',
    ),
}

ARTIFACT = """Builds it.

```hcl
variable "subnet_cidr" {
  default = "<your-cidr>"
}

resource "azurerm_subnet" "pe" {
  address_prefixes = [var.subnet_cidr]
  enforce_private_link_endpoint_network_policies = true
}

resource "azurerm_cognitive_account" "ai" {
  custom_subdomain_name   = "<your-subdomain>"
  application_insights_id = azurerm_application_insights.ai.id
}
```
"""


def test_a_key_no_passage_contains_is_found_without_a_model() -> None:
    from pheasant.assistant.compose_review import review, undocumented_keys

    evidence = "\n".join(doc.text for doc in DOCS.values())
    # The removed argument is in no passage; the one carried across from
    # another resource is (on that resource) and the declared variable is the
    # artifact's own — neither is a presence miss.
    assert undocumented_keys(ARTIFACT, evidence) == [
        "enforce_private_link_endpoint_network_policies"
    ]
    checked = review(None, "q", ARTIFACT, DOCS, max_chars=10_000)
    assert checked.found() and not checked.reviewed


def test_a_key_documented_only_for_another_component_is_a_suspect() -> None:
    from pheasant.assistant.compose_review import suspects

    # Scoped to each block's own type: application_insights_id is documented —
    # for azurerm_ai_foundry — and not for the cognitive account it is written on.
    assert suspects(ARTIFACT, DOCS) == {
        "enforce_private_link_endpoint_network_policies": "azurerm_subnet",
        "application_insights_id": "azurerm_cognitive_account",
    }


def test_a_suspect_the_reviewer_neither_reports_nor_clears_is_still_corrected() -> None:
    from pheasant.assistant.compose_review import review

    silent = review(_LLM([{"issues": []}]), "q", ARTIFACT, DOCS, max_chars=10_000)
    assert silent.unknown == [
        "enforce_private_link_endpoint_network_policies",
        "application_insights_id",
    ]
    assert "on azurerm_cognitive_account is not documented" in silent.revision()

    cleared = review(
        _LLM([{"issues": [], "cleared": ["application_insights_id"]}]),
        "q",
        ARTIFACT,
        DOCS,
        max_chars=10_000,
    )
    assert cleared.unknown == ["enforce_private_link_endpoint_network_policies"]


def test_the_reviewer_finds_an_identifier_on_the_wrong_component() -> None:
    from pheasant.assistant.compose_review import review

    issues = {
        "issues": [
            {
                "component": "azurerm_cognitive_account",
                "identifier": "application_insights_id",
                "problem": "documented for azurerm_ai_foundry, not this resource",
                "fix": "remove",
            },
            # Not in the artifact: a reviewer citing it must not cost a rewrite.
            {"component": "x", "identifier": "made_up_thing", "problem": "?", "fix": "remove"},
        ]
    }
    llm = _LLM([issues])
    checked = review(llm, "q", ARTIFACT, DOCS, max_chars=10_000)

    assert checked.reviewed
    assert [i["identifier"] for i in checked.issues] == ["application_insights_id"]
    assert llm.log[0]["json_mode"] is True
    # The scoped check's suspects reach the reviewer to decide.
    assert "SUSPECTS" in llm.log[0]["prompt"]
    assert (
        "enforce_private_link_endpoint_network_policies (on azurerm_subnet)"
        in (llm.log[0]["prompt"])
    )
    # Reported is an issue; neither reported nor cleared stays a suspect.
    assert checked.unknown == ["enforce_private_link_endpoint_network_policies"]
    revision = checked.revision()
    assert "application_insights_id" in revision and "Change nothing else" in revision


def test_the_artifacts_own_map_keys_are_not_suspects() -> None:
    from pheasant.assistant.compose_review import undocumented_keys

    artifact = (
        "```hcl\nlocals {\n  names = {\n    resource_group = var.prefix\n"
        "    key_vault      = var.prefix\n  }\n}\n\n"
        'resource "azurerm_key_vault" "kv" {\n  name = local.names.key_vault\n}\n'
        'resource "azurerm_resource_group" "rg" {\n  name = local.names.resource_group\n}\n```'
    )
    assert undocumented_keys(artifact, "resource azurerm_key_vault name") == []


def test_a_clean_artifact_and_an_unavailable_reviewer_cost_no_rewrite() -> None:
    from pheasant.assistant.compose_review import review

    clean = ARTIFACT.replace("  enforce_private_link_endpoint_network_policies = true\n", "")
    clean = clean.replace("  application_insights_id = azurerm_application_insights.ai.id\n", "")
    assert not review(_LLM([{"issues": []}]), "q", clean, DOCS, max_chars=10_000).found()

    unavailable = review(_LLM([]), "q", clean, DOCS, max_chars=10_000)
    assert not unavailable.found() and unavailable.error
    assert "review unavailable" in unavailable.summary()


def test_a_requested_component_the_artifact_omits_is_added_back() -> None:
    from pheasant.assistant.compose_review import review

    clean = ARTIFACT.replace("  enforce_private_link_endpoint_network_policies = true\n", "")
    clean = clean.replace("  application_insights_id = azurerm_application_insights.ai.id\n", "")
    checked = review(
        _LLM([{"issues": [], "omitted": ["a Key Vault"]}]), "q", clean, DOCS, max_chars=10_000
    )
    assert checked.found() and checked.omitted == ["a Key Vault"]
    assert "a Key Vault was asked for and is missing" in checked.revision()
    assert "1 requested component(s) missing" in checked.summary()


def test_an_untyped_suspect_needs_the_reviewer_to_confirm_it() -> None:
    from pheasant.assistant.compose_review import review

    # `required_version` sits outside any labelled block: the language's own
    # syntax far more often than an argument, so silence does not correct it.
    artifact = '```hcl\nterraform {\n  required_version = ">= 1.6"\n}\n```'
    assert review(None, "q", artifact, DOCS, max_chars=10_000).unknown == ["required_version"]
    assert review(_LLM([{"issues": []}]), "q", artifact, DOCS, max_chars=10_000).unknown == []


def test_the_passages_outrank_the_reviewer() -> None:
    from pheasant.assistant.compose_review import review

    artifact = (
        '```hcl\nresource "azurerm_subnet" "pe" {\n'
        '  private_endpoint_network_policies = "Enabled"\n}\n```'
    )
    wrong = {
        "issues": [
            {
                "component": "azurerm_subnet",
                "identifier": "private_endpoint_network_policies",
                "problem": "use the older name",
                "fix": "enforce_private_link_endpoint_network_policies",
            }
        ]
    }
    # The subnet's own page documents the argument, so the reviewer's claim
    # that it is wrong there is contradicted and dropped.
    assert not review(_LLM([wrong]), "q", artifact, DOCS, max_chars=10_000).found()


def test_a_variable_referenced_but_never_declared_is_corrected() -> None:
    from pheasant.assistant.compose_review import review, unresolved

    artifact = (
        '```hcl\nvariable "location" {\n  default = "<your-location>"\n}\n\n'
        'resource "azurerm_key_vault" "kv" {\n  location  = var.location\n'
        "  tenant_id = var.tenant_id\n}\n```"
    )
    assert unresolved(artifact) == ["tenant_id"]
    # A reviewer that sees nothing does not clear a reference that resolves to nothing.
    checked = review(_LLM([{"issues": []}]), "q", artifact, DOCS, max_chars=10_000)
    assert checked.found() and "`var.tenant_id` is referenced but never declared" in (
        checked.revision()
    )
    assert unresolved("```yaml\nname: ${var.x}\n```") == [], "no variable blocks, no rule"


PROJECT_PAGE = """# azurerm_cognitive_account_project

## Example Usage

```hcl
resource "azurerm_cognitive_account_project" "example" {
  name = "x"
}
```

## Arguments Reference

* `name` - (Required) The name.

* `identity` - (Required) An `identity` block as defined below.

* `description` - (Optional) A description.

An `identity` block supports the following:

* `type` - (Required) The identity type.

## Attributes Reference
"""


def test_a_required_argument_the_block_leaves_out_is_corrected() -> None:
    from pheasant.assistant.compose_review import missing_required, required_arguments

    # Top level only: `type` is required inside the optional-looking nested
    # block's own list, never on the resource itself.
    assert required_arguments(PROJECT_PAGE) == ["name", "identity"]
    docs = {
        1: SimpleNamespace(title="r/cognitive_account_project.html.markdown", text=PROJECT_PAGE)
    }
    without = '```hcl\nresource "azurerm_cognitive_account_project" "p" {\n  name = "p"\n}\n```'
    assert missing_required(without, docs) == [("azurerm_cognitive_account_project", "identity")]
    with_block = without.replace(
        '  name = "p"\n', '  name = "p"\n  identity {\n    type = "SystemAssigned"\n  }\n'
    )
    assert missing_required(with_block, docs) == []


def test_a_lookup_of_the_same_type_is_not_held_to_the_declarations_page() -> None:
    from pheasant.assistant.compose_review import missing_required

    docs = {
        1: SimpleNamespace(title="r/cognitive_account_project.html.markdown", text=PROJECT_PAGE)
    }
    artifact = (
        '```hcl\nresource "azurerm_cognitive_account_project" "p" {\n  name = "p"\n'
        '  identity {\n    type = "SystemAssigned"\n  }\n}\n'
        'resource "azurerm_cognitive_account_project" "q" {\n  name = "q"\n'
        '  identity {\n    type = "SystemAssigned"\n  }\n}\n'
        'data "azurerm_cognitive_account_project" "existing" {\n  name = "e"\n}\n```'
    )
    assert missing_required(artifact, docs) == []


def test_a_components_own_page_outranks_a_stale_example_elsewhere() -> None:
    from pheasant.assistant.compose_review import suspects

    docs = {
        1: SimpleNamespace(
            title="r/subnet.html.markdown",
            text="## Arguments Reference\n\n* `private_endpoint_network_policies` - (Optional)",
        ),
        2: SimpleNamespace(
            title="r/private_endpoint.html.markdown",
            text='resource "azurerm_subnet" "x" {\n'
            "  enforce_private_link_endpoint_network_policies = true\n}",
        ),
    }
    artifact = (
        '```hcl\nresource "azurerm_subnet" "pe" {\n'
        "  enforce_private_link_endpoint_network_policies = true\n}\n```"
    )
    assert suspects(artifact, docs) == {
        "enforce_private_link_endpoint_network_policies": "azurerm_subnet"
    }


# ------------------------------------------------ proportional document quotas

AUTO = {
    "max_context_passages": 40,
    "passages_per_document": "auto",
    "document_fraction": 0.25,
    "document_floor": 2,
    "document_window_share": 0.75,
}


def test_a_documents_quota_scales_with_its_size() -> None:
    from pheasant.assistant.workflows.compose import document_quota

    assert document_quota(1, AUTO) == 2, "a one-page note: the floor"
    assert document_quota(40, AUTO) == 10, "a 40-chunk guide: a quarter of it"
    assert document_quota(120, AUTO) == 30, "a 120-chunk PDF: thirty, not two"
    assert document_quota(400, AUTO) == 30, "never more than 75% of the window"
    assert document_quota(None, AUTO) == 2, "an unknown size gets the floor"
    assert document_quota(120, {**AUTO, "passages_per_document": 3}) == 3, "an int is a fixed cap"
    assert document_quota(120, {**AUTO, "passages_per_document": None}) is None


def test_a_long_pdf_keeps_its_passages_and_short_notes_keep_their_floor() -> None:
    pdf = [_passage("pdf", n) for n in range(60)]
    notes = [_passage(f"note{i}", n) for i in range(12) for n in range(3)]
    ranked = pdf[:35] + notes + pdf[35:]
    window = synthesis_window(ranked, AUTO, {"pdf": 120, **{f"note{i}": 3 for i in range(12)}})

    taken = {}
    for passage in window:
        taken[passage.node_id] = taken.get(passage.node_id, 0) + 1
    assert len(window) == 40
    assert taken["pdf"] == 30, "the PDF's quota, in rank order"
    assert all(taken.get(f"note{i}", 0) <= 2 for i in range(12)), "each note at its floor"


def test_hydration_reads_further_into_a_document_cited_many_times() -> None:
    from pheasant.assistant.chat import hydrate_citations

    seen = {}

    class Retriever:
        def documents(self, node_ids, **kwargs):
            seen.update(kwargs)
            return {}

    citations = [{"index": i + 1, "node_id": "pdf", "chunk_id": f"pdf#{i}"} for i in range(30)] + [
        {"index": 31, "node_id": "note", "chunk_id": "note#0"}
    ]
    options = {"passage_chars": 12_000, "scale_document_chars": True, "chars_per_passage": 4000}
    hydrate_citations(Retriever(), citations, options)
    assert seen["allowances"] == {"pdf": 120_000, "note": 12_000}

    seen.clear()
    hydrate_citations(Retriever(), citations, {"passage_chars": 12_000})
    assert "allowances" not in seen, "off by default: every other answer reads as it did"


def test_compose_keyword_pins_the_answer_shape() -> None:
    from pheasant.assistant.keywords import read

    directives = read("@compose a Terraform module for the storage account")
    assert directives.intent == "compose"
    assert directives.text == "a Terraform module for the storage account"
    assert read("@build the manifests").intent == "compose"
    assert read("@detailed @compose the pipeline").intent == "compose"
    assert read("what is compose").intent is None
    assert "intent" not in read("@table the ports").as_dict(), "payload unchanged without it"


# --------------------------------------------- the planner names what exists


class _Artifacts:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        if "FROM artifacts ORDER BY relative_path" not in sql:
            return []
        return [
            {"id": f"{source}:{path}", "relative_path": path, "source_id": source, "type": "file"}
            for path, source in sorted(self.paths.items())
        ]


def test_the_planner_is_told_the_documents_in_scope_by_name() -> None:
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.assistant.workflows.compose import documents_in_scope

    state = _Artifacts(
        {
            "r/api_management_api_policy.html.markdown": "gateway",
            "learn/llm-token-limit-policy.txt": "gateway",
            "r/key_vault.html.markdown": "security",
        }
    )
    scoped = PheasantRetriever(
        search=None, knowledge_base="kb", state=state, source_names=["gateway"]
    )
    assert scoped.document_names() == ["llm-token-limit-policy", "api_management_api_policy"]
    assert documents_in_scope(scoped).endswith("llm-token-limit-policy, api_management_api_policy")

    everything = PheasantRetriever(search=None, knowledge_base="kb", state=state)
    assert everything.document_names(limit=2) == [], "past the limit a scope is a corpus"
    assert documents_in_scope(SimpleNamespace()) == "", "a retriever without names adds nothing"
