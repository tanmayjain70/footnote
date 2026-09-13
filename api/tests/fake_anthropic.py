"""A stand-in for api.anthropic.com that speaks the wire format, not a mock.

The provider under test is pointed at this server through ``base_url`` and
the real SDK does everything it would do in production -- authentication
headers, request serialisation, SSE parsing, retries. What comes out the
other end is therefore the request the API would actually receive, which is
what the tests assert on: every document has citations enabled, the system
prompt carries its cache marker, nothing deprecated is sent.

Streaming responses follow the event sequence the SDK's accumulator expects:
``message_start`` (a whole message, usage included), ``content_block_start``,
``content_block_delta`` (text and citations), ``content_block_stop``,
``message_delta`` (stop reason, cumulative usage), ``message_stop``. A
thinking block goes first by default, because the real model sends one and
the provider must not count it as an answer block.

Scripted failures: ``next_refusal()`` and ``next_error()`` queue what the
next request gets instead of an answer.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class RecordedRequest:
    path: str
    headers: dict[str, str]  # lower-cased names
    body: dict[str, Any]

    @property
    def streaming(self) -> bool:
        return bool(self.body.get("stream"))


@dataclass
class FakeBlock:
    """One text block of the streamed answer, with an optional citation."""

    text: str
    #: Fields of a ``content_block_location`` citation without its ``type``.
    citation: dict[str, Any] | None = None


@dataclass
class Scripted:
    kind: str  # "refusal" | "error"
    status: int = 500


def default_blocks() -> list[FakeBlock]:
    return [
        FakeBlock(
            text="The Tenant may determine this Lease on 31 March 2029. ",
            citation={
                "document_index": 1,
                "document_title": "Lease of Unit 2B, Whitworth Court — page 7",
                "start_block_index": 0,
                "end_block_index": 1,
                "cited_text": (
                    "The Tenant may determine this Lease on 31 March 2029 by giving the "
                    "Landlord not less than 6 months' prior written notice."
                ),
            },
        ),
        FakeBlock(
            text="Six months' written notice is required.",
            citation={
                "document_index": 1,
                "document_title": "Lease of Unit 2B, Whitworth Court — page 7",
                "start_block_index": 0,
                "end_block_index": 2,
                "cited_text": (
                    "The Tenant may determine this Lease on 31 March 2029 by giving the "
                    "Landlord not less than 6 months' prior written notice. "
                    "Time is of the essence."
                ),
            },
        ),
    ]


def default_extraction() -> dict[str, Any]:
    return {
        "fields": [
            {
                "key": "term_end",
                "value": "31 March 2034",
                "quote": (
                    "The Term shall commence on 1 April 2024 and shall expire on 31 March 2034."
                ),
                "chunk_id": None,
                "confidence": "high",
            }
        ]
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class FakeAnthropic:
    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.script: deque[Scripted] = deque()
        self.model = "claude-opus-5"
        self.thinking_first = True
        self.blocks: list[FakeBlock] = default_blocks()
        #: The JSON the non-streaming (parse) endpoint returns as its single
        #: text block. Tests set it to match the schema they request.
        self.extraction: dict[str, Any] = default_extraction()
        self.usage: dict[str, int] = {
            "input_tokens": 1200,
            "output_tokens": 42,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 900,
        }
        self._counter = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------- lifecycle --

    @property
    def base_url(self) -> str:
        assert self._server is not None, "the fake is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeAnthropic:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.fake = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeAnthropic:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def reset(self) -> None:
        """Forget recorded requests and scripted responses; restore defaults."""
        self.requests.clear()
        self.script.clear()
        self.thinking_first = True
        self.blocks = default_blocks()
        self.extraction = default_extraction()

    # -------------------------------------------------------- scripting --

    def next_refusal(self) -> None:
        self.script.append(Scripted("refusal"))

    def next_error(self, status: int = 500) -> None:
        self.script.append(Scripted("error", status))

    # -------------------------------------------------------- responses --

    def _message_id(self) -> str:
        self._counter += 1
        return f"msg_fake_{self._counter:04d}"

    def stream_body(self, refusal: bool) -> bytes:
        message = {
            "id": self._message_id(),
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            # The API reports input usage up front and output usage at the
            # end; the accumulator overwrites with the message_delta totals.
            "usage": {**self.usage, "output_tokens": 1},
        }
        out = [_sse("message_start", {"type": "message_start", "message": message})]
        index = 0

        if not refusal:
            if self.thinking_first:
                out.append(
                    _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                        },
                    )
                )
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "thinking_delta",
                                "thinking": "Checking the break clause.",
                            },
                        },
                    )
                )
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "signature_delta", "signature": "sig_fake"},
                        },
                    )
                )
                out.append(
                    _sse("content_block_stop", {"type": "content_block_stop", "index": index})
                )
                index += 1

            for block in self.blocks:
                out.append(
                    _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "text", "text": "", "citations": []},
                        },
                    )
                )
                for piece in _split(block.text, 12):
                    out.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "text_delta", "text": piece},
                            },
                        )
                    )
                if block.citation is not None:
                    citation = {"type": "content_block_location", **block.citation}
                    out.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "citations_delta", "citation": citation},
                            },
                        )
                    )
                out.append(
                    _sse("content_block_stop", {"type": "content_block_stop", "index": index})
                )
                index += 1

        delta: dict[str, Any] = {"stop_reason": "end_turn", "stop_sequence": None}
        if refusal:
            delta = {
                "stop_reason": "refusal",
                "stop_sequence": None,
                "stop_details": {
                    "type": "refusal",
                    "category": "general_harms",
                    "explanation": "Scripted refusal.",
                },
            }
        out.append(
            _sse("message_delta", {"type": "message_delta", "delta": delta, "usage": self.usage})
        )
        out.append(_sse("message_stop", {"type": "message_stop"}))
        return b"".join(out)

    def message_body(self, refusal: bool) -> bytes:
        message: dict[str, Any] = {
            "id": self._message_id(),
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [] if refusal else [{"type": "text", "text": json.dumps(self.extraction)}],
            "stop_reason": "refusal" if refusal else "end_turn",
            "stop_sequence": None,
            "usage": self.usage,
        }
        if refusal:
            message["stop_details"] = {
                "type": "refusal",
                "category": "general_harms",
                "explanation": "Scripted refusal.",
            }
        return json.dumps(message).encode()


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> FakeAnthropic:
        return self.server.fake  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # The stdlib handler prints every request to stderr; the tests read
        # the recorded requests instead.
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("request-id", "req_fake")
        # One connection per request keeps the handler threads from idling on
        # a keep-alive socket after the test has moved on.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        headers = {name.lower(): value for name, value in self.headers.items()}
        self.fake.requests.append(RecordedRequest(self.path, headers, body))

        if self.path != "/v1/messages":
            error = {"type": "not_found_error", "message": "no such route"}
            body = json.dumps({"type": "error", "error": error}).encode()
            self._send(404, "application/json", body)
            return

        scripted = self.fake.script.popleft() if self.fake.script else None
        if scripted is not None and scripted.kind == "error":
            self._send(
                scripted.status,
                "application/json",
                json.dumps(
                    {"type": "error", "error": {"type": "api_error", "message": "scripted outage"}}
                ).encode(),
            )
            return

        refusal = scripted is not None and scripted.kind == "refusal"
        if body.get("stream"):
            self._send(200, "text/event-stream", self.fake.stream_body(refusal))
        else:
            self._send(200, "application/json", self.fake.message_body(refusal))


__all__ = ["FakeAnthropic", "FakeBlock", "RecordedRequest", "default_blocks"]
