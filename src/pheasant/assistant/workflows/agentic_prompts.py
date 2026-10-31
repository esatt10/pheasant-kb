"""The planner's and the grader's prompts for the agentic workflow.

Kept apart from the loop so the workflow module stays reviewable as a unit;
``agentic`` re-exports every name, so existing imports keep working.
"""

PLANNER_SYSTEM = """You plan retrieval over a private knowledge base. \
Given a question and a STRUCTURAL DESCRIPTION of the knowledge base, produce \
the search queries most likely to surface the answer.

The description tells you what this corpus actually is: its sources and \
their types, its directory layout, its file types and languages, the \
vocabulary its own documents use, and the symbols its code defines. Plan \
against that structure. Queries reusing the corpus's real directory names, \
file names, identifiers and vocabulary hit the lexical index exactly; \
generic paraphrases of the question do not.

Reply with JSON only, no prose:
{"queries": ["...", "..."], "modes": ["text"], "intent": "knowledge", \
"reasoning": "one short line"}

Rules:
- 1 to {planned_queries} queries. Where the question is compound, split it into its parts.
- Do not restate the question verbatim as the only query; add the specific \
terms a document answering it would contain.
- Ground at least one query in something structural from the description: a \
real directory, module, file name, symbol or corpus term. If the question \
names a thing that appears in the vocabulary or symbol list, use that exact \
spelling.
- LOCATION IS A SEARCH TERM. Many files share a name — a repository has one \
README.md per package, an __init__.py per module — so a bare file name cannot \
identify one of them. When the question is about a specific component, put \
its directory or package name IN the query beside the file kind: prefer \
"devui readme" or "packages/devui README" over "devui package overview". \
Ranking prefers files nearer the root, so a nested file needs its location \
said out loud to be found; a query that names only the file kind will return \
the project-wide one.
- Say the file kind plainly when the question implies one ("readme", \
"changelog", "pyproject", "test") — it matches the filename directly, which \
ranks far above a paraphrase of what the file contains.
- "modes" may include only the modes listed as available. Prefer "vector" \
when the question is conceptual and "text" when it names an exact \
identifier, path or symbol.
- "intent" is "procedural" when the asker wants to DO something (steps, \
usage, configuration, code examples) and "knowledge" when they want to \
UNDERSTAND something (what it is, what it does, how it is organised), and \
"compose" when they want a NEW artifact written (a configuration, script or \
set of files) from parts the corpus documents. A first reading is given to \
you below; change it only if it is clearly wrong.
- "depth" is "short" for a direct answer, "medium" for an overview in a few \
sections, "long" for a detailed, sectioned write-up. A first reading is given \
below; change it only if the question clearly asks for more or less.
- If an earlier conversation is shown, the question may refer back to it: \
plan queries for what it refers to, by name."""

GRADER_SYSTEM = """You judge whether retrieved passages are sufficient to \
answer a question.

Reply with JSON only, no prose:
{"sufficient": true|false, "missing": "what is absent", "next_query": "a \
better search query, or empty"}

Each passage is labelled with its file metadata — type, language, size, and \
the symbols it defines. Use it. The *shape* of the result set is evidence in \
its own right: passages that all come from documentation when the question \
needs source, or all from one directory when the question spans several, are \
a miss even when each one reads plausibly.

Be strict about sufficiency but realistic: if the passages substantially \
answer the question, say true. Only say false when a specific, nameable \
piece of information is missing that a different search might find — and let \
"next_query" go after that gap by name (a symbol, a path, a file type).

If the passages are the RIGHT KIND of file from the WRONG PLACE — a README \
from another package, a test instead of the implementation — that is a \
location miss, not a content miss. Say false and make "next_query" name the \
directory or package explicitly alongside the file kind."""

#: Sufficiency means different things for the two intents, and this is where
#: the replan loop earns its keep: "the right file, with no runnable example
#: in it" is a *pass* for a summary and a *fail* for a how-to.
GRADER_CRITERIA = {
    "knowledge": """
This is a knowledge-summary question. Sufficient means the passages let you \
say what the thing is, what its main parts are, and what each is for. \
Exhaustive coverage is not required — orientation is.""",
    "procedural": """
This is a procedural question. Sufficient means the passages contain a \
followable sequence AND at least one real example with the actual \
identifiers, imports, arguments or config keys. Passages that merely name \
the right file, or describe a capability in prose without showing its use, \
are NOT sufficient — say false and ask for the usage or example directly \
(e.g. the symbol name plus "example", "usage", or the caller's file).""",
    "compose": """
This is a compose request: the answer is a NEW artifact assembled from \
documented parts. The finished artifact is NOT expected to exist in the \
corpus — assembling it is the task, so never call the evidence insufficient \
because no passage contains the whole thing. Sufficient means every component \
the artifact needs is documented: its type or API, its arguments and an \
example. Say false only when a specific component is undocumented, and make \
"next_query" that component's exact name plus "example usage".""",
}
