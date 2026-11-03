"""Metrik Prometheus (diekspos di /metrics). Dashboard Grafana lengkap menyusul di TASK-24."""

from prometheus_client import Counter, Gauge, Histogram

TOOL_CALLS = Counter("mcp_tool_calls_total", "Jumlah tool call", ["tool", "status"])
TOOL_LATENCY = Histogram(
    "mcp_tool_latency_seconds",
    "Latensi tool call",
    ["tool"],
    buckets=(0.005, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30),
)
TOOL_OUTPUT_TOKENS = Counter("mcp_tool_output_tokens_total", "Token output tool ke LLM", ["tool"])
WEBHOOK_SENT = Counter("mcp_webhook_sent_total", "Webhook ke Dashboard", ["status"])
WEBHOOK_LATENCY = Histogram("mcp_webhook_latency_seconds", "Latensi webhook Dashboard")
OUTBOX_PENDING = Gauge("mcp_outbox_pending", "Event outbox pending")
RUNS_TOTAL = Counter("mcp_runs_total", "Agent run", ["trigger", "status"])
LLM_TOKENS = Counter("mcp_llm_tokens_total", "Token LLM via Gateway", ["purpose", "kind"])
JOBS_TOTAL = Counter("mcp_jobs_total", "Job selesai", ["kind", "status"])
