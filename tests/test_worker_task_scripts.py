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


def test_worker_powershell_scripts_do_not_pass_remote_secret_as_process_args():
    root = Path(__file__).resolve().parents[1]
    for name in ["06_worker_heartbeat.ps1", "07_worker_poll.ps1", "09_worker_healthcheck.ps1", "13_start_worker.ps1"]:
        text = (root / name).read_text(encoding="utf-8")
        assert "--worker-token" not in text
        assert "--remote-base-url" not in text
        assert "$env:REMOTE_PUBLIC_BASE_URL" in text
        assert "$env:WORKER_SHARED_TOKEN" in text


def test_start_worker_script_reads_remote_secret_from_environment_by_default():
    script = Path(__file__).resolve().parents[1] / "13_start_worker.ps1"
    text = script.read_text(encoding="utf-8")
    assert '[string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL' in text
    assert '[string]$WorkerToken = $env:WORKER_SHARED_TOKEN' in text
    assert "$env:WORKER_SHARED_TOKEN = $WorkerToken" in text
    assert "WorkerToken is required" in text


def test_manage_worker_task_script_does_not_read_or_print_worker_secret():
    script = Path(__file__).resolve().parents[1] / "14_manage_worker_task.ps1"
    text = script.read_text(encoding="utf-8")

    assert "WORKER_SHARED_TOKEN" not in text
    assert "REMOTE_PUBLIC_BASE_URL" not in text
    assert "actions_redacted" in text
    assert "Task command arguments are intentionally omitted" in text
    for action in ["Status", "Start", "Stop", "Restart", "Uninstall"]:
        assert f'"{action}"' in text


def test_batch_chunk_status_reports_cosyvoice_file_progress():
    root = Path(__file__).resolve().parents[1]
    script = (root / "15_manage_batch_chunk.ps1").read_text(encoding="utf-8")

    assert "function Get-LatestCosyVoiceInputJson" in script
    assert "function Add-TtsFileProgress" in script
    assert "function Add-TtsPostprocessProgress" in script
    assert "--input-json" in script
    assert "seg_*_pcm.wav" in script
    assert "tts_mix" in script
    assert "Rendering hard subtitles" in script
    assert "latest=$($latest.Name)" in script
    assert 'if ($Progress.Contains("eta_seconds"))' in script


def test_cosyvoice_batch_writes_incremental_progress():
    root = Path(__file__).resolve().parents[1]
    script = (root / "tools" / "cosyvoice_batch.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--progress-json")' in script
    assert "def write_progress" in script
    assert "CosyVoice progress:" in script
    assert '"status": "running"' in script
    assert '"latest_segment_id": segment_id' in script
    assert '"eta_seconds": eta' in script
