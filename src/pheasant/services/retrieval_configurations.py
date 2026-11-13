"""Named, immutable base configurations reuse the existing bundle promotion path."""

from __future__ import annotations

from pheasant.search.ranking import RankingParameters
from pheasant.security.authorization import require_permission
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest


def save_base(context: ServiceContext, name: str) -> dict:
    require_permission("tuning.manage")
    from pheasant.tuning.contracts import TuningBundle
    from pheasant.tuning.store import save_bundle

    if not str(name).strip() or len(name) > 120:
        raise InvalidRequest("a baseline needs a name of 1–120 characters")
    values = RankingParameters.from_config(context.config).values()
    bundle = TuningBundle(
        bundle_id=TuningBundle.identity(values),
        kb_id=context.config.knowledge_base_id,
        experiment_id="",
        decision_id="",
        snapshot_id="configured-base",
        parameters=values,
        motivating_stage="baseline",
    )
    save_bundle(context.state, bundle)
    from pheasant.artifacts.publications import publish

    revision = publish(
        context.state,
        context.config.knowledge_base_id,
        f"baseline:{name}",
        bundle.as_dict(),
        receipt={"name": name, "kind": "retrieval_baseline"},
    )
    return {
        "name": name,
        "bundle_id": bundle.bundle_id,
        "revision_id": revision,
        "parameters": values,
    }
