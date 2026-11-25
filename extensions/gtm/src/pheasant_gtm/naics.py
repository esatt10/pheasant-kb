"""Offline lookup for the U.S. Census Bureau's 2022 NAICS structure."""

from functools import cache
from importlib.resources import files

EDITION = 2022
_RANGES = {
    "32": "31-33",
    "33": "31-33",
    "31": "31-33",
    "44": "44-45",
    "45": "44-45",
    "48": "48-49",
    "49": "48-49",
}


@cache
def titles() -> dict[str, str]:
    lines = (
        files("pheasant_gtm").joinpath("naics_2022.tsv").read_text(encoding="utf-8").splitlines()
    )
    return dict(line.split("\t", 1) for line in lines if line and not line.startswith("#"))


def classification(code: str, source: str) -> dict:
    labels = titles()
    sector = _RANGES.get(code[:2], code[:2])
    return {
        "edition": EDITION,
        "code": code,
        "sector_code": sector,
        "industry": labels[sector],
        "sub_industry": labels[code] if code != sector else None,
        "source": source,
    }


def catalog() -> dict:
    return {"edition": EDITION, "codes": [classification(code, "reference") for code in titles()]}
