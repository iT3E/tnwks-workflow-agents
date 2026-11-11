"""The container contract, for each agent of the image: GET /ping and POST /invocations on port 8080.

The in-process tests call the app of each agent over HTTP. The process tests
start the entrypoint of the image as the sandbox does, with no command, and
check the port, the agent from AGENT_NAME, and the traces: each span of a run
gets to the collector before the answer, and no text of the run is in a span.
"""

import os
import unittest
from unittest import mock

import support  # noqa: F401  (sets the import path)
from support import Asgi, Process, base_env, collector, wait_port
from support.cases import CASES

import httpx

import agents
from contract import server

PORT = 8080


class EveryAgentHasACase(unittest.TestCase):
    def test_each_agent_has_a_contract_case(self):
        self.assertEqual(sorted(CASES), agents.names())


class ContractInProcess(unittest.TestCase):
    """The two paths, for each agent."""

    def test_ping(self):
        for name in agents.names():
            with self.subTest(agent=name), Asgi(server.create_app(agents.load(name))) as app:
                r = httpx.get(f"{app.url}/ping")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "Healthy")
                self.assertIsInstance(r.json()["time_of_last_update"], int)

    def test_invocation_with_a_good_input(self):
        for name, case in CASES.items():
            with self.subTest(agent=name), case.running():
                with mock.patch.dict(os.environ, case.env()), Asgi(server.create_app(agents.load(name))) as app:
                    r = httpx.post(f"{app.url}/invocations", json={"input": case.good_input()}, timeout=60)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(sorted(r.json()), ["output"])
                    case.check_output(self, r.json()["output"])

    def test_bodies_that_are_not_valid(self):
        for name, case in CASES.items():
            with self.subTest(agent=name), case.running():
                with mock.patch.dict(os.environ, case.env()), Asgi(server.create_app(agents.load(name))) as app:
                    for body in [b"not json", b"[]", b'{"prompt": "x"}', b'{"input": "text"}', b'{"input": {}}']:
                        r = httpx.post(
                            f"{app.url}/invocations", content=body, headers={"content-type": "application/json"}
                        )
                        self.assertEqual(r.status_code, 400, body)
                        self.assertIn("error", r.json())

    def test_an_error_of_the_agent_is_a_500_with_the_reason(self):
        class Broken:
            NAME = "broken"

            @staticmethod
            async def invoke(payload):
                raise RuntimeError("the model gateway is down")

        with Asgi(server.create_app(Broken)) as app:
            r = httpx.post(f"{app.url}/invocations", json={"input": {}})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json(), {"error": "RuntimeError: the model gateway is down"})

    def test_spans_are_flushed_before_each_answer(self):
        calls = []

        class Agent:
            NAME = "probe"

            @staticmethod
            async def invoke(payload):
                calls.append("invoke")
                if payload.get("fail"):
                    raise RuntimeError("x")
                return {}

        with mock.patch.object(server.telemetry, "flush", side_effect=lambda *a: calls.append("flush")):
            with Asgi(server.create_app(Agent)) as app:
                httpx.post(f"{app.url}/invocations", json={"input": {}})
                httpx.post(f"{app.url}/invocations", json={"input": {"fail": True}})
        self.assertEqual(calls, ["invoke", "flush", "invoke", "flush"])


class ContractAsTheSandboxStartsIt(unittest.TestCase):
    """The entrypoint with no command serves the agent of AGENT_NAME on port 8080, with traces."""

    def test_no_command_serves_the_agent_on_8080_with_spans_before_the_answer(self):
        for name, case in CASES.items():
            with self.subTest(agent=name), case.running(), collector.server() as otel:
                env = base_env(
                    **case.env(),
                    OTEL_EXPORTER_OTLP_ENDPOINT=otel.url,
                    # A value that asks for text in the spans: the code must replace it.
                    OTEL_SEMCONV_STABILITY_OPT_IN="gen_ai_unredacted_attributes=gen_ai.input.messages",
                )
                proc = Process([], env)
                try:
                    wait_port(PORT)
                    ping = httpx.get(f"http://127.0.0.1:{PORT}/ping")
                    self.assertEqual(ping.json()["status"], "Healthy")
                    r = httpx.post(
                        f"http://127.0.0.1:{PORT}/invocations", json={"input": case.good_input()}, timeout=60
                    )
                    # Read the collector at the moment of the answer, before the exporter's own timer.
                    spans_at_answer = collector.span_count(otel)
                    bodies = b"".join(collector.trace_bodies(otel))
                finally:
                    out, err = proc.stop()
                self.assertEqual(r.status_code, 200, err[-2000:])
                case.check_output(self, r.json()["output"])
                self.assertGreater(spans_at_answer, 0, "no span got to the collector before the answer")
                for marker in case.markers():
                    self.assertNotIn(marker.encode(), bodies, f"{marker} is in a span")

    def test_an_unknown_agent_does_not_start(self):
        proc = Process(["serve"], base_env(AGENT_NAME="no-such-agent"))
        proc.proc.wait(timeout=60)
        out, err = proc.stop()
        self.assertEqual(proc.proc.returncode, 2)
        self.assertIn("no workflow agent", err)

    def test_an_agent_with_no_config_does_not_start(self):
        proc = Process(["serve"], base_env(AGENT_NAME=agents.names()[0]))
        proc.proc.wait(timeout=60)
        out, err = proc.stop()
        self.assertEqual(proc.proc.returncode, 2)
        self.assertIn("missing environment variables", err)


if __name__ == "__main__":
    unittest.main()
