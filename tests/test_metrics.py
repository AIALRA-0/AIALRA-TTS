from ecse_localizer.metrics import collect_system_metrics


def test_metrics_shape(tmp_path):
    config = {"output_dir": str(tmp_path)}
    metrics = collect_system_metrics(config)

    assert "cpu" in metrics
    assert "memory" in metrics
    assert "gpu" in metrics
    assert "disk" in metrics
    assert metrics["disk"]["total_bytes"] >= metrics["disk"]["free_bytes"]
