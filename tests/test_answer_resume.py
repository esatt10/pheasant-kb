"""A streamed answer outlives its connection: `services.answer_resume`.

A phone that slept through a two-minute compose dropped the answer's stream,
the server finished the answer anyway, and the reader -- told it failed --
asked again. With an ``answer_id`` the answer is kept as it forms, and
``GET /assistant/chat/answers/{id}`` hands it to whoever asked, including
after the stream that was carrying it is gone.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.request
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.services import ServiceContext, answer_resume
from pheasant.services import assistant as assistant_service
from pheasant.services.errors import AnswerNotFound


def _events(response) -> list[dict[str, Any]]:
    return [
        json.loads(line[5:].strip()) for line in response.iter_lines() if line.startswith("data:")
    ]


def _answer(question: str, **extra: Any) -> dict[str, Any]:
    return {
        "question": question,
        "answer": "Accounts are counted by date created. [1]",
        "steps": [{"name": "retrieve"}],
        "citations": [{"index": 1, "node_id": "file:kb:doc", "used": True}],
        "visual": {"status": "none"},
        **extra,
    }


def _collect(client: TestClient, answer_id: str, until: set[str], seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        kept = client.get(f"/assistant/chat/answers/{answer_id}")
        assert kept.status_code == 200, kept.text
        body = kept.json()
        if body["status"] in until or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def test_an_answer_whose_reader_left_is_finished_and_kept(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that was lost: the socket closes mid-answer, the answer does not.

    Over a real server, because the test client's stream does not disconnect:
    leaving it waits for the app to finish, which is the opposite of a phone
    that went to sleep.
    """

    release = threading.Event()

    def slow_answer(_context, request, *, on_step=None, **_kwargs):
        on_step(_Step("plan", "Planned 3 searches"))
        # The reader is gone by now; the answer goes on regardless.
        assert release.wait(20)
        on_step(_Step("compose", "Wrote the script"))
        return _answer(request.question)

    monkeypatch.setattr(assistant_service, "answer", slow_answer)
    with _Served(create_app(config=loaded_config)) as (host, port):
        body = json.dumps({"question": "@compose a SuiteScript", "answer_id": "phone-slept-0001"})
        connection = http.client.HTTPConnection(host, port, timeout=10)
        connection.request(
            "POST", "/assistant/chat/stream", body, {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        while not (line := response.readline().decode()).startswith("data:"):
            pass
        assert json.loads(line[5:])["name"] == "plan"
        connection.close()  # the screen went to sleep

        client = _Http(host, port)
        midway = client.collect("phone-slept-0001", {"running"})
        assert midway["status"] == "running" and midway["answer"] is None
        assert [step["name"] for step in midway["steps"]] == ["plan"]

        release.set()
        kept = client.collect("phone-slept-0001", {"done", "failed"})
    assert kept["status"] == "done", kept
    assert kept["answer"]["answer"] == "Accounts are counted by date created. [1]"
    assert [step["name"] for step in kept["steps"]] == ["plan", "compose"]


class _Served:
    """The app on a real socket, for a client that really goes away."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> tuple[str, int]:
        self.thread.start()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.server.started and self.server.servers and self.server.servers[0].sockets:
                return "127.0.0.1", self.server.servers[0].sockets[0].getsockname()[1]
            time.sleep(0.02)
        raise RuntimeError("the server did not start")

    def __exit__(self, *_exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


class _Http:
    def __init__(self, host: str, port: int) -> None:
        self.base = f"http://{host}:{port}"

    def collect(self, answer_id: str, until: set[str], seconds: float = 15.0) -> dict:
        deadline = time.monotonic() + seconds
        while True:
            with urllib.request.urlopen(f"{self.base}/assistant/chat/answers/{answer_id}") as kept:
                body = json.loads(kept.read())
            if body["status"] in until or time.monotonic() > deadline:
                return body
            time.sleep(0.05)


def test_a_visual_that_follows_the_text_is_kept_with_it(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(_context, request, **_kwargs):
        return _answer(request.question, visual={"status": "pending"})

    def render_visual(_context, answer, **_kwargs):
        return {"status": "ready", "kind": "flow"}

    monkeypatch.setattr(assistant_service, "answer", answer)
    monkeypatch.setattr(assistant_service, "render_visual", render_visual)
    client = TestClient(create_app(config=loaded_config))
    body = {"question": "draw the flow", "answer_id": "with-a-visual-1"}
    with client.stream("POST", "/assistant/chat/stream", json=body) as response:
        events = _events(response)

    assert [event["type"] for event in events] == ["answer", "visual"]
    assert "answer" not in events[1], "the stream's visual frame carries the visual alone"
    kept = _collect(client, "with-a-visual-1", {"done"})
    assert kept["answer"]["visual"] == {"status": "ready", "kind": "flow"}
    assert kept["answer"]["answer"].startswith("Accounts are counted")


def test_a_refusal_is_kept_as_one(loaded_config, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_context, _request, **_kwargs):
        raise RuntimeError("the provider refused")

    monkeypatch.setattr(assistant_service, "answer", broken)
    client = TestClient(create_app(config=loaded_config))
    body = {"question": "anything", "answer_id": "refused-0001"}
    with client.stream("POST", "/assistant/chat/stream", json=body) as response:
        assert [event["type"] for event in _events(response)] == ["error"]

    kept = _collect(client, "refused-0001", {"failed"})
    assert kept["status"] == "failed" and kept["error"] == "the provider refused"


def test_an_answer_nobody_named_is_not_kept_and_a_bad_name_is_refused(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        assistant_service, "answer", lambda _c, request, **_k: _answer(request.question)
    )
    app = create_app(config=loaded_config)
    client = TestClient(app)
    with client.stream("POST", "/assistant/chat/stream", json={"question": "q"}) as response:
        assert [event["type"] for event in _events(response)] == ["answer"]
    assert app.state.state.rows("SELECT COUNT(*) AS n FROM answer_streams")[0]["n"] == 0

    refused = client.post(
        "/assistant/chat/stream", json={"question": "q", "answer_id": "../../etc"}
    )
    assert refused.status_code == 422 and refused.json()["code"] == "INVALID_REQUEST"
    missing = client.get("/assistant/chat/answers/never-asked-001")
    assert missing.status_code == 404 and missing.json()["code"] == "UNKNOWN_ANSWER"


def test_an_answer_is_read_back_only_by_who_asked_and_only_for_an_hour(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(config=loaded_config)
    context = ServiceContext(config=loaded_config, state=app.state.state, searcher=None)
    recorder = answer_resume.start(context, "owned-answer-1", "ada@example.com")
    assert recorder is not None
    recorder.answered(_answer("q"))

    assert answer_resume.read(context, "owned-answer-1", "ada@example.com")["status"] == "done"
    # Someone else holding the id learns nothing: the same refusal as no answer.
    for other in ("grace@example.com", None):
        with pytest.raises(AnswerNotFound):
            answer_resume.read(context, "owned-answer-1", other)

    # An hour on, it is gone -- and the next recorder sweeps it out.
    monkeypatch.setattr(answer_resume, "RETENTION_SECONDS", -1)
    with pytest.raises(AnswerNotFound):
        answer_resume.read(context, "owned-answer-1", "ada@example.com")
    answer_resume.start(context, "next-answer-01", "ada@example.com")
    held = context.state.rows("SELECT answer_id FROM answer_streams")
    assert [row["answer_id"] for row in held] == ["next-answer-01"]


def test_a_store_that_cannot_keep_the_answer_costs_the_resume_not_the_answer(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only state store (an api replica on SQLite) still answers."""

    monkeypatch.setattr(
        assistant_service, "answer", lambda _c, request, **_k: _answer(request.question)
    )
    app = create_app(config=loaded_config)
    writes: list[str] = []

    def refuse(sql: str, params: Any = ()) -> None:
        writes.append(sql)
        raise PermissionError("attempt to write a readonly database")

    monkeypatch.setattr(app.state.state, "execute", refuse)
    client = TestClient(app)
    body = {"question": "q", "answer_id": "read-only-0001"}
    with client.stream("POST", "/assistant/chat/stream", json=body) as response:
        events = _events(response)
    assert [event["type"] for event in events] == ["answer"]
    assert len(writes) == 1, "a store that refused once is not asked again for every step"


class _Step:
    """The fields the stream route reads off a workflow step."""

    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail
        self.passages = None
        self.duration_seconds = 0.1
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0
        self.reasoning_tokens = 0
        self.provider_calls = 0
        self.provider_retries = 0
        self.model = None
        self.fanout_timings = None
