# AGENTS.md — pheasant

Entry point for coding agents that read `AGENTS.md` by convention (Codex
CLI, Gemini CLI, and others). This file is intentionally short — it exists
so those tools have *something* to auto-load; it is not a duplicate of the
real hand-off doc.

**Read `CLAUDE.md` first.** It is the dense, canonical context file for
this repository (what pheasant is, repo layout, canonical commands, rules,
current work queue). Everything below is a pointer, not a substitute.

## "How do I configure pheasant?"

Do not answer from memory, and do not hand-write a `pheasant.yaml`. The
product configures itself:

```bash
pheasant setup          # interactive, sectioned, explains every option,
                        # writes pheasant.yaml + a 0600 .env + the commands
pheasant setup --accept-defaults   # non-interactive, all defaults
```

It reads the live schema in `src/pheasant/config/schema.py`, so it is always
current — which is exactly why it replaced the prose wizard that used to live
here. See `docs/how-to/setup.md`.

## Everything else

Ordinary code changes (bug fixes, features, tests) follow the rules in
`CLAUDE.md` §4 and the canonical commands in §3. A change that touches
`src/pheasant/config/schema.py` owes `docs/configuration.md` an update —
`tests/test_config_surface_freshness.py` enforces the mechanical part.

For local deployment, configuration, scaling, MCP attachment, or deployment
troubleshooting, use `.agents/skills/pheasant-deploy/SKILL.md`.

For *querying* a region — raw hybrid search and graph traversal judged by
your own harness rather than the region's answering workflow — use
`.agents/skills/pheasant-retrieval/SKILL.md`.
