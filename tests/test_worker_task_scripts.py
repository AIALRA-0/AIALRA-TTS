from pathlib import Path


def test_install_worker_task_does_not_embed_remote_secret_in_task_action():
    script = Path(__file__).resolve().parents[1] / "install_worker_task.ps1"
    text = script.read_text(encoding="utf-8")
    encoded_block = text.split("$encodedArgs = @(", 1)[1].split("$action =", 1)[0]

    assert "-WorkerToken" not in encoded_block
    assert "WORKER_SHARED_TOKEN" not in encoded_block
    assert "-RemoteBaseUrl" not in encoded_block
    assert "REMOTE_PUBLIC_BASE_URL" not in encoded_block
    assert "Test-PersistentEnvValue" in text
    assert 'SetEnvironmentVariable("WORKER_SHARED_TOKEN"' in text
    assert "not embedded in the scheduled task command line" in text


def test_start_worker_script_reads_remote_secret_from_environment_by_default():
    script = Path(__file__).resolve().parents[1] / "13_start_worker.ps1"
    text = script.read_text(encoding="utf-8")

    assert '[string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL' in text
    assert '[string]$WorkerToken = $env:WORKER_SHARED_TOKEN' in text
    assert '"--worker-token", $WorkerToken' in text
    assert "WorkerToken is required" in text
