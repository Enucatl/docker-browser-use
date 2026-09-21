"""Optional OpenTelemetry traces and metrics for agent operations."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    TraceIdRatioBased,
)
from opentelemetry.trace import Status, StatusCode

DEFAULT_SERVICE_NAME = "browser-use-agent"


@dataclass(frozen=True, slots=True)
class OTelSettings:
    """Runtime settings for optional OTLP/HTTP export."""

    enabled: bool = False
    endpoint: str | None = None
    service_name: str = DEFAULT_SERVICE_NAME
    sampler: str = "traceidratio"
    sampler_arg: float = 1.0


def _env_bool(name: str, default: bool) -> bool:
    """Parse a conventional boolean environment value."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_otel_settings() -> OTelSettings:
    """Load standard OTEL environment settings.

    Export is enabled when an OTLP endpoint is present unless ``OTEL_ENABLED``
    or ``OTEL_SDK_DISABLED`` explicitly disables it.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or None
    enabled = _env_bool("OTEL_ENABLED", endpoint is not None)
    if _env_bool("OTEL_SDK_DISABLED", False):
        enabled = False
    sampler = os.environ.get("OTEL_TRACES_SAMPLER", "traceidratio").strip().lower()
    try:
        sampler_arg = min(1.0, max(0.0, float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1"))))
    except ValueError:
        sampler_arg = 1.0
    return OTelSettings(
        enabled=enabled,
        endpoint=endpoint,
        service_name=os.environ.get("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME),
        sampler=sampler,
        sampler_arg=sampler_arg,
    )


def _endpoint(endpoint: str, suffix: str) -> str:
    """Return an OTLP/HTTP signal endpoint from a base or signal URL."""
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith(suffix) else f"{endpoint}{suffix}"


def _sampler(settings: OTelSettings) -> Any:
    """Build the configured trace sampler."""
    if settings.sampler in {"always_off", "off"}:
        return ALWAYS_OFF
    if settings.sampler in {"traceidratio", "ratio"}:
        return TraceIdRatioBased(settings.sampler_arg)
    return ALWAYS_ON


class Telemetry:
    """Own one trace and metric provider pair for the controller process."""

    def __init__(
        self,
        settings: OTelSettings | None = None,
        *,
        span_exporter: SpanExporter | None = None,
        span_processor: SpanProcessor | None = None,
        metric_reader: MetricReader | None = None,
    ) -> None:
        """Create telemetry, optionally attaching test or OTLP exporters.

        Args:
            settings: Export and sampling settings; loaded from the environment
                when omitted.
            span_exporter: Optional synchronous exporter, useful for tests.
            span_processor: Optional custom span processor.
            metric_reader: Optional metric reader, useful for tests.
        """
        settings = settings or load_otel_settings()
        resource = Resource.create({"service.name": settings.service_name})
        tracer_provider = TracerProvider(resource=resource, sampler=_sampler(settings))
        if span_processor is not None:
            tracer_provider.add_span_processor(span_processor)
        elif span_exporter is not None:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif settings.enabled and settings.endpoint:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=_endpoint(settings.endpoint, "/v1/traces"))
                )
            )

        if metric_reader is None and settings.enabled and settings.endpoint:
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=_endpoint(settings.endpoint, "/v1/metrics"))
            )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader] if metric_reader else []
        )
        self.tracer = tracer_provider.get_tracer("browser_use_agent")
        self.meter = meter_provider.get_meter("browser_use_agent")
        self.step_latency = self.meter.create_histogram(
            "browser_use.agent.step.duration", unit="ms", description="Agent step latency."
        )
        self.queue_depth = self.meter.create_up_down_counter(
            "browser_use.agent.queue.depth", unit="{runs}", description="Runs in the worker queue."
        )
        self.approval_wait = self.meter.create_histogram(
            "browser_use.agent.approval.wait.duration",
            unit="ms",
            description="Human approval wait latency.",
        )

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_id: UUID,
        step_id: UUID | None = None,
        attributes: dict[str, Any] | None = None,
    ):
        """Create a safe phase span carrying only operational identifiers."""
        span_attributes: dict[str, Any] = {"run_id": str(run_id)}
        if step_id is not None:
            span_attributes["step_id"] = str(step_id)
        if attributes:
            span_attributes.update(attributes)
        with self.tracer.start_as_current_span(
            name,
            attributes=span_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise

    def record_step(self, duration_ms: float, *, status: str) -> None:
        """Record one observe/decide/act step latency."""
        self.step_latency.record(duration_ms, {"status": status})

    def record_approval_wait(self, duration_ms: float, *, decision: str) -> None:
        """Record one approval decision wait."""
        self.approval_wait.record(duration_ms, {"decision": decision})

    def add_queue_depth(self, delta: int) -> None:
        """Adjust the number of runs tracked by the worker queue."""
        self.queue_depth.add(delta)


_telemetry: Telemetry | None = None


def get_telemetry() -> Telemetry:
    """Return the process-wide environment-configured telemetry instance."""
    global _telemetry
    if _telemetry is None:
        _telemetry = Telemetry()
    return _telemetry
