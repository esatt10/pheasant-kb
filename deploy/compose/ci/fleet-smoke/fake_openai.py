"""A deterministic stand-in for the OpenAI API, for fleet smoke runs.

The shipped fleet (`deploy/compose/fleet.yaml`) talks to OpenAI for four
things: embeddings, image captions, the assistant's answers, and the agentic
planner/grader. A smoke run cannot hold a real key and must not depend on a
model's mood, so this answers the same wire formats — `POST
/v1/chat/completions` and `POST /v1/embeddings` — with replies chosen by which
of pheasant's prompts arrived. That keeps the fleet's config exactly as
shipped, provider settings included, with one `base_url` swapped.

Every reply is grounded in the prompt it answers (it only ever cites passage
numbers the prompt contains), so a run exercises the real validation paths:
citation verification, figure markers, long-form outlines and diagram specs.

`GET /stats` reports calls by kind, which is how a smoke run asserts that, say,
a long answer really did outline and write sections, or that a follow-up was
rewritten.

Standard library only: it runs in a slim container or next to a process fleet.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATS: dict[str, int] = {}
LOCK = threading.Lock()


def count(kind: str) -> None:
    with LOCK:
        STATS[kind] = STATS.get(kind, 0) + 1


def passages(prompt: str) -> list[tuple[int, str]]:
    """``[n] title (path)`` headers and outline lines ``[n] path: preview``."""
    found = []
    for match in re.finditer(r"^\[(\d{1,2})\] ([^\n]+)$", prompt, re.MULTILINE):
        found.append((int(match.group(1)), match.group(2).split(":")[0].strip()))
    seen, out = set(), []
    for number, title in found:
        if number not in seen:
            seen.add(number)
            out.append((number, title))
    return out


def figures(prompt: str) -> list[int]:
    return [int(n) for n in re.findall(r"^\[fig:(\d{1,2})\]", prompt, re.MULTILINE)]


def reply_for(system: str, prompt: str) -> tuple[str, str]:
    if "plan retrieval" in system:
        question = prompt.rsplit("Question:", 1)[-1].splitlines()[0].strip()
        plan = {"queries": [], "modes": ["hybrid"], "reasoning": "stub plan"}
        if "rollback" in question.lower():
            # No length rule fires on "rollback", so a medium answer here can
            # only be the planner overruling the rule's "short".
            plan["depth"] = "medium"
        return "planner", json.dumps(plan)
    if "judge whether" in system:
        return "grader", json.dumps({"sufficient": True})
    if "rewrite a follow-up" in system:
        follow_up = prompt.rsplit("Follow-up question:", 1)[-1].strip()
        previous = re.findall(r"^Q: (.+)$", prompt, re.MULTILINE)
        topic = previous[-1] if previous else ""
        return "rewrite", f"{follow_up} regarding {topic}".strip()
    if "plan a long, sectioned answer" in system:
        numbers = [n for n, _ in passages(prompt)] or [1]
        half = max(1, len(numbers) // 2)
        sections = [
            {"heading": "What the sources describe", "passages": numbers[:half]},
            {"heading": "How the parts connect", "passages": numbers[half:] or numbers[:1]},
        ]
        overview = f"The sources describe this in two parts [{numbers[0]}]."
        return "outline", json.dumps({"overview": overview, "sections": sections})
    if "ONE section" in system:
        numbers = [n for n, _ in passages(prompt)] or [1]
        heading = prompt.split("Section to write:", 1)[-1].splitlines()[0].strip()
        cites = "".join(f"[{n}]" for n in numbers[:2])
        return "section", f"{heading}: grounded in the passages shown {cites}."
    if "into a small diagram" in system:
        found = passages(prompt)[:5] or [(1, "source")]
        nodes = [
            {"id": f"n{i}", "label": title[:40], "cites": [n]} for i, (n, title) in enumerate(found)
        ]
        edges = [
            {"from": f"n{i}", "to": f"n{i + 1}", "label": "then", "cites": [found[i][0]]}
            for i in range(len(found) - 1)
        ]
        # One element nothing supports: must come back marked inferred.
        nodes.append({"id": "extra", "label": "Unsourced step", "cites": [99]})
        spec = {
            "kind": "flow",
            "title": "Stub diagram",
            "summary": "From the passages.",
            "nodes": nodes,
            "edges": edges,
        }
        return "diagram", json.dumps(spec)
    if "research assistant" in system:
        numbers = [n for n, _ in passages(prompt)] or [1]
        parts = [f"Answer drawn from the sources [{numbers[0]}]."]
        if "LENGTH: a medium-length answer" in system:
            parts.append(f"### Detail\n\nMore on it [{numbers[-1]}].")
        shown = figures(prompt)
        if shown:
            parts.append(f"[fig:{shown[0]}]")
        # A marker naming no passage and one naming no figure: both must be
        # dropped by verification before a reader sees them.
        parts.append("An invented citation [42] and figure [fig:9].")
        return "answer", "\n\n".join(parts)
    return "other", "ok"


def embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        vector[int.from_bytes(digest[:4], "big") % dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet
        return

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/stats":
            with LOCK:
                self._send(200, dict(STATS))
            return
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/embeddings"):
            inputs = body.get("input") or []
            inputs = [inputs] if isinstance(inputs, str) else inputs
            dimensions = int(body.get("dimensions") or 1536)
            count("embeddings")
            self._send(
                200,
                {
                    "data": [
                        {"index": i, "embedding": embedding(text, dimensions)}
                        for i, text in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
            return
        if self.path.endswith("/chat/completions"):
            messages = body.get("messages") or []
            system = next((m["content"] for m in messages if m.get("role") == "system"), "")
            user = next((m["content"] for m in messages if m.get("role") == "user"), "")
            if isinstance(user, list):  # the vision captioner
                count("caption")
                text = "A network architecture diagram: a router fans out to three regions."
            else:
                kind, text = reply_for(str(system), str(user))
                count(kind)
            self._send(
                200,
                {
                    "choices": [{"message": {"role": "assistant", "content": text}}],
                    "usage": {
                        "prompt_tokens": len(str(user)) // 4,
                        "completion_tokens": len(text) // 4,
                    },
                },
            )
            return
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
