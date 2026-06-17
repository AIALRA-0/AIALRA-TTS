import json
from pathlib import Path

import yaml

from ecse_localizer.cli import main
from ecse_localizer.translation_sample import build_translation_quality_sample, write_translation_quality_sample


def sample_config(tmp_path: Path) -> dict:
    return {
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "translation": {"quality_mode": "best_quality", "style": "natural_chinese_lecture"},
    }


def test_translation_quality_sample_compares_all_required_stages(tmp_path):
    sample = build_translation_quality_sample(sample_config(tmp_path))

    assert sample["pass"] is True
    assert sample["checks"]["has_literal_lecture_coherence_repair"] is True
    assert sample["checks"]["all_repairs_selected"] is True
    assert sample["checks"]["all_repairs_preserve_numbers"] is True
    assert sample["checks"]["all_repairs_preserve_terms"] is True
    assert sample["paragraphs"]
    first = sample["rows"][0]
    assert first["zh_literal"] != first["zh_lecture"]
    assert first["zh_lecture"] != first["zh_coherence"]
    assert first["bad_translation_for_repair"] != first["zh_repair"]
    assert first["repair_selected"] is True
    assert first["missing_terms_after_repair"] == []
    assert first["missing_numbers_after_repair"] == []
    assert "SUMMARY_STYLE_TRANSLATION" in first["stage_flags"]["repair_input"]


def test_translation_quality_sample_writes_json_and_markdown(tmp_path):
    result = write_translation_quality_sample(tmp_path / "sample", sample_config(tmp_path))

    assert result["pass"] is True
    data = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    markdown = Path(result["markdown"]).read_text(encoding="utf-8")
    assert data["mode"] == "best_quality_translation_sample"
    assert "Stage Comparison" in markdown
    assert "Vout = Vin * R2 / (R1 + R2)" in markdown


def test_translation_sample_cli_generates_artifacts(tmp_path, capsys):
    config = sample_config(tmp_path)
    Path(config["input_dir"]).mkdir()
    Path(config["output_dir"]).mkdir()
    Path(config["work_dir"]).mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    out_dir = tmp_path / "quality-sample"

    rc = main(["--config", str(config_path), "translation-sample", "--output", str(out_dir)])

    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["pass"] is True
    assert Path(output["json"]).exists()
    assert Path(output["markdown"]).exists()
