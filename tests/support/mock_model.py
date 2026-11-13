"""A model server that speaks the Anthropic Messages API and answers from a script.

The script is a list of steps. Each request gets the step that matches the
number of tool results in its messages: step 0 before any tool result, step 1
after one, and so on. A step is ("tool", name, arguments) or ("text", text).
Each request is kept, so a test can read the key, the tools and the messages.
It answers POST /v1/messages only, as a stream or as one message.
"""

import json

from . import Server

KEY = "test-model-key-not-a-secret"
PATH = "/v1/messages"


def reviewer_script(final_text: str, diff_path: str) -> list:
    return [
        ("tool", "pull_request_overview", {}),
        ("tool", "changed_files", {}),
        ("tool", "file_diff", {"path": diff_path}),
        ("tool", "trusted_comments", {}),
        ("tool", "check_runs", {}),
        ("text", final_text),
    ]


def tool_results(body: dict) -> int:
    count = 0
    for m in body.get("messages", []):
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            count += sum(1 for block in m["content"] if block.get("type") == "tool_result")
    return count


def make_app(script: list):
    def app(handler, method, path, raw):
        if method != "POST" or path.split("?", 1)[0].rstrip("/") != PATH:
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "mock: unknown path"}}, None
        body = json.loads(raw or b"{}")
        n = tool_results(body)
        step = script[min(n, len(script) - 1)]
        if step[0] == "tool":
            block = {"type": "tool_use", "id": f"toolu_mock_{n}", "name": step[1], "input": step[2]}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": step[1]}
            stop = "end_turn"
        message = {
            "id": f"msg_mock_{n}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "mock"),
            "content": [block],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        if not body.get("stream"):
            return 200, message, None

        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("connection", "close")
        handler.end_headers()

        def send(event: str, data: dict):
            handler.wfile.write(f"event: {event}\ndata: {json.dumps(dict(data, type=event))}\n\n".encode())
            handler.wfile.flush()

        start = dict(message, content=[], stop_reason=None, usage={"input_tokens": 100, "output_tokens": 1})
        send("message_start", {"message": start})
        if step[0] == "tool":
            send("content_block_start", {"index": 0, "content_block": dict(block, input={})})
            send(
                "content_block_delta",
                {"index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(step[2])}},
            )
        else:
            send("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
            send("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": step[1]}})
        send("content_block_stop", {"index": 0})
        send("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 10}})
        send("message_stop", {})
        handler.close_connection = True
        return 200, None, None

    return app


def server(script: list) -> Server:
    return Server(make_app(script))
