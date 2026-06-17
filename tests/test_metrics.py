import json

from ecse_localizer.metrics import collect_system_metrics, sanitize_metrics


def test_metrics_shape(tmp_path):
    config = {"output_dir": str(tmp_path)}
    metrics = collect_system_metrics(config)

    assert "cpu" in metrics
    assert "memory" in metrics
    assert "gpu" in metrics
    assert "disk" in metrics
    assert metrics["disk"]["total_bytes"] >= metrics["disk"]["free_bytes"]
    assert "path" not in metrics["disk"]
    assert metrics["disk"]["scope"] == "output_volume"
    assert metrics["local_storage"]["managed_bytes"] >= 0


def test_sanitize_metrics_removes_paths_recursively():
    metrics = {
        "disk": {"path": r"C:\worker-private\out", "used_percent": 10},
        "nested": [{"output_dir": r"C:\worker-private\out", "value": 1}],
    }

    cleaned = sanitize_metrics(metrics)
    serialized = json.dumps(cleaned)

    assert "path" not in cleaned["disk"]
    assert "output_dir" not in cleaned["nested"][0]
    assert "worker-private" not in serialized
