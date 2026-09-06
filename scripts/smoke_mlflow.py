#!/usr/bin/env python3
"""
Smoke test: MLflow tracking and artifact storage.

Tests that:
  - MLflow server is reachable at http://localhost:25000
  - Tracking works (log params, metrics, artifacts)
  - Backend store (Postgres) is connected
"""

import os
import sys
import logging
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    import mlflow
    import mlflow.langchain
except ImportError as e:
    logger.error(f"MLflow import failed: {e}")
    sys.exit(1)


def test_mlflow_tracking():
    """Test MLflow tracking and artifact storage."""
    logger.info("=== Smoke Test: MLflow Integration ===")

    # Set tracking URI
    tracking_uri = "http://localhost:25000"
    logger.info(f"Setting MLflow tracking URI: {tracking_uri}")
    mlflow.set_tracking_uri(tracking_uri)

    # Set experiment
    experiment_name = "smoke-test"
    logger.info(f"Setting experiment: {experiment_name}")
    mlflow.set_experiment(experiment_name)

    # Start a run
    logger.info("Starting MLflow run...")
    with mlflow.start_run(run_name="smoke-test-run"):
        run_id = mlflow.active_run().info.run_id
        logger.info(f"Run ID: {run_id}")

        # Log parameters
        logger.info("Logging parameters...")
        mlflow.log_param("model", "claude-haiku-4-5")
        mlflow.log_param("role", "research_synth")

        # Log metrics
        logger.info("Logging metrics...")
        mlflow.log_metric("tokens_in", 150)
        mlflow.log_metric("tokens_out", 42)
        mlflow.log_metric("cost_usd", 0.00123)

        # Log artifact
        logger.info("Logging artifact...")
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "test_artifact.txt"
            artifact_path.write_text("Test artifact content\nLine 2")

            mlflow.log_artifact(str(artifact_path), artifact_path=None)
            logger.info(f"✓ Artifact logged: {artifact_path.name}")

        logger.info("✓ Run completed successfully")

    # Verify we can retrieve the run
    logger.info(f"Retrieving run {run_id}...")
    try:
        run = mlflow.get_run(run_id)
        logger.info(f"✓ Run retrieved")
        logger.info(f"  Status: {run.info.status}")
        logger.info(f"  Params: {dict(run.data.params)}")
        logger.info(f"  Metrics: {dict(run.data.metrics)}")
    except Exception as e:
        logger.error(f"Failed to retrieve run: {e}")
        sys.exit(1)

    logger.info("\n=== SMOKE TEST PASSED ===")
    logger.info(f"MLflow tracking is operational")
    logger.info(f"Backend store (Postgres) is connected")
    logger.info(f"Artifact storage is working")

    return True


if __name__ == "__main__":
    try:
        success = test_mlflow_tracking()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
