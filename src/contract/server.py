"""The HTTP server of the container contract, for the agent that AGENT_NAME names.

    GET  /ping         200 {"status": "Healthy" | "HealthyBusy", "time_of_last_update": <unix seconds>}
    POST /invocations  body {"input": {...}}
                       200 {"output": {...}}
                       400 {"error": "..."}  the body or the input is not valid
                       500 {"error": "..."}  the run failed

The spans of a run are flushed before its answer goes out, on success and on
error, so a sandbox that ends after the answer loses none.
"""

import logging
import os
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import agents
from contract import ConfigError, InputError, telemetry

log = logging.getLogger(__name__)

PORT = 8080
ERROR_TEXT_LIMIT = 500


def create_app(agent) -> FastAPI:
    """The contract for one agent module (see agents/__init__.py for what a module gives)."""
    app = FastAPI(title=f"workflow agent {agent.NAME}", docs_url=None, redoc_url=None, openapi_url=None)
    state = {"busy": 0, "updated": int(time.time())}

    @app.get("/ping")
    async def ping():
        return {
            "status": "HealthyBusy" if state["busy"] else "Healthy",
            "time_of_last_update": state["updated"],
        }

    @app.post("/invocations")
    async def invocations(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("input"), dict):
            return JSONResponse({"error": "the body must be an object with an object `input`"}, status_code=400)

        state["busy"] += 1
        state["updated"] = int(time.time())
        try:
            output = await agent.invoke(body["input"])
            status, answer = 200, {"output": output}
        except InputError as e:
            status, answer = 400, {"error": str(e)[:ERROR_TEXT_LIMIT]}
        except Exception as e:
            log.exception("the run failed")
            status, answer = 500, {"error": f"{type(e).__name__}: {e}"[:ERROR_TEXT_LIMIT]}
        finally:
            state["busy"] -= 1
            state["updated"] = int(time.time())
            telemetry.flush()
        return JSONResponse(answer, status_code=status)

    return app


def main() -> int:
    """Serve the contract for AGENT_NAME on port 8080 (BIND sets the address, default all)."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    name = os.environ.get("AGENT_NAME", "")
    try:
        agent = agents.load(name)
        agent.check_config()
    except (agents.UnknownAgent, ConfigError) as e:
        print(f"serve: {e}", file=sys.stderr)
        return 2
    telemetry.setup(f"workflow-agent-{agent.NAME}")

    import uvicorn

    uvicorn.run(
        create_app(agent),
        host=os.environ.get("BIND", "0.0.0.0"),
        port=PORT,
        log_level="warning",
        access_log=False,
    )
    return 0
