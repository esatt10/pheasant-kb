"""The GTM contribution to Pheasant's versioned extension API."""

from pheasant.extensions import Command, Extension, Provenance, SourceContribution, source_settings
from pheasant_gtm.permissions import HTTP, MCP
from pheasant_gtm.schema import MIGRATIONS

SOURCE_TYPE = "gtm_research"
PROVENANCE = Provenance("gtm_evidence", "gtm-evidence")


def make_connector(source, state):
    from pheasant_gtm.connector import GTMConnector

    return GTMConnector(source, state)


def register_http(app, services):
    from pheasant_gtm.http import register_gtm_routes

    register_gtm_routes(app, services=services)


def register_mcp(server, tools, anticipated):
    from pheasant_gtm.mcp import register_gtm_tools

    register_gtm_tools(server, tools, anticipated)


def configure_command(parser):
    from pheasant_gtm.cli import register

    parser.add_argument("--config", "-c", default="pheasant.yaml")
    register(parser)


def run_command(args):
    from pheasant_gtm.cli import run

    return run(args)


GTM_SOURCE = SourceContribution(
    type=SOURCE_TYPE,
    label="GTM research",
    description="Curated company context from public evidence.",
    connector=make_connector,
    registration_help=(
        "Register GTM targets through POST /gtm/collections or register_gtm_collection."
    ),
    legacy_settings_key="gtm",
    provenance=PROVENANCE,
    public_sync_details=("gtm_catalog",),
)


def gtm_settings(data: dict) -> dict:
    return source_settings(data, GTM_SOURCE) or {}


def extension() -> Extension:
    return Extension(
        id="gtm",
        api_version=1,
        register_http=register_http,
        http_permissions=HTTP,
        register_mcp=register_mcp,
        mcp_permissions=MCP,
        sources=(GTM_SOURCE,),
        commands=(
            Command(
                "gtm",
                "Research companies using public web evidence",
                configure_command,
                run_command,
            ),
        ),
        migrations=MIGRATIONS,
        ui_routes=("/gtm",),
    )
