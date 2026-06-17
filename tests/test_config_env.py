from pathlib import Path

from ecse_localizer.config import load_config


def test_config_expands_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("AIALRA_TEST_SECRET", "expanded-value")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
privacy:
  allow_cloud_api: false
  allow_upload_media: false
webui:
  session_secret: "${AIALRA_TEST_SECRET}"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config["webui"]["session_secret"] == "expanded-value"
