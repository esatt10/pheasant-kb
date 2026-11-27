"""The harness contribution to Pheasant's versioned extension API."""

from pheasant.extensions import Command, Extension
from pheasant_harness.permissions import HTTP, MCP
from pheasant_harness.schema import MIGRATIONS


def register_http(app, services):
    from pheasant_harness.http import register_harness_routes

    register_harness_routes(app, services=services)


def register_mcp(server, tools, anticipated):
    from pheasant_harness.mcp import register_harness_tools

    register_harness_tools(server, tools, anticipated)


def configure_command(parser):
    from pheasant_harness.cli import register

    parser.add_argument("--config", "-c", default="pheasant.yaml")
    register(parser)


def run_command(args):
    from pheasant_harness.cli import run

    return run(args)


def extension() -> Extension:
    return Extension(
        id="harness",
        api_version=1,
        register_http=register_http,
        http_permissions=HTTP,
        register_mcp=register_mcp,
        mcp_permissions=MCP,
        commands=(
            Command(
                "harness",
                "Agentic runtime: durable, verified, evidence-linked runs",
                configure_command,
                run_command,
            ),
        ),
        migrations=MIGRATIONS,
        ui_routes=("/harness",),
    )
