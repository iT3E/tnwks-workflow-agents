"""A model server that speaks the OpenAI Chat Completions API and answers from a script.

The script is a list of steps. Each request gets the step that matches the
number of tool results in its messages: step 0 before any tool result, step 1
after one, and so on. A step is ("tool", name, arguments) or ("text", text).
Each request is kept, so a test can read the key, the tools and the messages.
"""

import json
import time

from . import Server

KEY = "test-model-key-not-a-secret"


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
    return sum(1 for m in body.get("messages", []) if m.get("role") == "tool")


def make_app(script: list):
    def app(handler, method, path, raw):
        if method != "POST" or not path.rstrip("/").endswith("chat/completions"):
            return 404, {"error": {"message": "mock: unknown path"}}, None
        body = json.loads(raw or b"{}")
        step = script[min(tool_results(body), len(script) - 1)]
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        base = {"id": "chatcmpl-mock", "created": int(time.time()), "model": body.get("model", "mock")}
        if step[0] == "tool":
            call = {
                "id": f"call_{tool_results(body)}",
                "type": "function",
                "function": {"name": step[1], "arguments": json.dumps(step[2])},
            }
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": step[1]}, "stop"
        if not body.get("stream"):
            return (
                200,
                dict(
                    base,
                    object="chat.completion",
                    usage=usage,
                    choices=[{"index": 0, "message": message, "finish_reason": finish}],
                ),
                None,
            )
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("connection", "close")
        handler.end_headers()
        chunk = dict(base, object="chat.completion.chunk")

        def send(data):
            handler.wfile.write(f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode())
            handler.wfile.flush()

        send(dict(chunk, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]))
        if step[0] == "tool":
            send(
                dict(
                    chunk, choices=[{"index": 0, "delta": {"tool_calls": [dict(call, index=0)]}, "finish_reason": None}]
                )
            )
        else:
            send(dict(chunk, choices=[{"index": 0, "delta": {"content": step[1]}, "finish_reason": None}]))
        send(dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": finish}]))
        send(dict(chunk, choices=[], usage=usage))
        send("[DONE]")
        handler.close_connection = True
        return 200, None, None

    return app


def server(script: list) -> Server:
    return Server(make_app(script))
