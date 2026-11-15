"""Fleet settings: several knowledge bases an operator provisioned side by side.

One region is one knowledge base, and stays so: every stable id, lease and
table is built on it. A customer group that needs several gets several
regions, each a small scale-to-zero stack, sharing what is expensive to
duplicate (the container environment, the database server, the model
deployments, the registry, the sign-in app). What ties them together is a
**fleet directory**: a few tables in one database every stack can reach
(``persistence/fleet_store.py``).

The operator provisions *slots* -- stacks with nothing in them. Whoever
administers a knowledge base claims a free slot for a new one
(``services/fleet.py``), which is a row changing, not a deployment. When no
slot is free they file a capacity request, and the operator decides: adding a
slot is a cost, and it is theirs to incur.

Off by default. A region with ``fleet.enabled`` false opens no second
database and is exactly what it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pheasant.config.model import ModelMixin


@dataclass
class FleetSettings(ModelMixin):
    enabled: bool = False
    #: This stack's slot: unique in the fleet, and fixed for the life of its
    #: data (it is what a claim, an admin list and a usage row are keyed on).
    #: Enabled with no slot is not part of any fleet: no directory is opened.
    slot: str = ""
    #: The address people reach this stack at.
    url: str = ""
    #: The directory. On Postgres, the *name* of the variable holding its DSN;
    #: with ``path`` set instead, a SQLite file (one host, or a test).
    dsn_env: str = "PHEASANT_FLEET_DATABASE_URL"
    path: Path | None = None
    #: A stack that is in use before anybody claims it: the first knowledge
    #: base of a deployment. ``false`` registers as a free slot.
    claimed: bool = False
