"""A trace collector that keeps the raw OTLP bodies it gets on POST /v1/traces."""

from . import Server


def server() -> Server:
    def app(handler, method, path, raw):
        if method == "POST" and path.startswith("/v1/traces"):
            return 200, b"", {"content-type": "application/x-protobuf"}
        return 404, b"", None

    return Server(app)


def trace_bodies(srv: Server) -> list[bytes]:
    return [r["body"] for r in srv.requests if r["path"].startswith("/v1/traces")]


def span_count(srv: Server) -> int:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    count = 0
    for body in trace_bodies(srv):
        req = ExportTraceServiceRequest()
        req.ParseFromString(body)
        count += sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans)
    return count
