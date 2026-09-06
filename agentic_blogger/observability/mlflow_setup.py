"""MLflow wiring — fail-open. An MLflow outage must degrade to no traces,
never a failed content job."""

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def init_mlflow() -> bool:
    global _initialized
    if _initialized:
        return True
    try:
        import mlflow
        import mlflow.langchain

        tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment("agentic-blogger/blog")
        mlflow.langchain.autolog()
        _initialized = True
        return True
    except Exception:
        logger.exception("MLflow init failed — continuing without tracing")
        return False


def start_job_run(job_id: str, resumed: bool = False):
    """Returns an active mlflow run context manager, or a no-op context
    manager if MLflow is unavailable. Never raises."""
    import contextlib

    if not init_mlflow():
        return contextlib.nullcontext()

    try:
        import mlflow

        run_ctx = mlflow.start_run(run_name=f"job-{job_id}")
        mlflow.set_tag("job_id", job_id)
        mlflow.set_tag("resumed", str(resumed))
        return run_ctx
    except Exception:
        logger.exception("Failed to start MLflow run — continuing without tracing")
        import contextlib
        return contextlib.nullcontext()


def log_job_metrics(cost_usd, tokens_in: int, tokens_out: int) -> None:
    try:
        import mlflow

        mlflow.log_metric("total_cost_usd", float(cost_usd))
        mlflow.log_metric("total_tokens_in", tokens_in)
        mlflow.log_metric("total_tokens_out", tokens_out)
    except Exception:
        logger.exception("Failed to log MLflow metrics — continuing")
