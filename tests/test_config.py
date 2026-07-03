"""Config schema: strict validation, fail-loud guards."""

import pytest

from qbert0g.config import Config, ConfigError


def _minimal(**overrides) -> dict:
    data = {"devices": [{"id": "mock-0", "type": "mock"}]}
    data.update(overrides)
    return data


class TestDefaults:
    def test_minimal_config_loads_with_safe_defaults(self):
        config = Config.from_dict(_minimal())
        assert config.server.listen == "127.0.0.1:50051"
        assert config.server.unix_socket == ""
        assert config.post_processing_mode == "raw"
        assert config.freshness.flush_device_buffer is True
        assert config.freshness.emit_generation_timestamp is True
        assert config.limits.max_bytes_per_request == 16384
        assert config.auth.header == "api-key"

    def test_qcc_mode_mapping(self):
        config = Config.from_dict(_minimal(post_processing={"mode": "raw"}))
        assert config.qcc_mode_for(config.devices[0]) == 1
        config = Config.from_dict(_minimal(post_processing={"mode": "sha256"}))
        assert config.qcc_mode_for(config.devices[0]) == 0
        config = Config.from_dict(_minimal(post_processing={"mode": "raw_samples"}))
        assert config.qcc_mode_for(config.devices[0]) == 2

    def test_device_post_processing_override_wins(self):
        config = Config.from_dict(
            {
                "post_processing": {"mode": "raw"},
                "devices": [{"id": "d0", "type": "mock", "post_processing": "sha256"}],
            }
        )
        assert config.qcc_mode_for(config.devices[0]) == 0


class TestRejections:
    def test_unknown_top_level_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict(_minimal(post_procesing={"mode": "raw"}))  # typo

    def test_unknown_section_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict(_minimal(server={"host": "0.0.0.0"}))  # old schema

    def test_pooling_refused(self):
        with pytest.raises(ConfigError, match="pooling"):
            Config.from_dict(_minimal(freshness={"allow_pooling": True}))

    def test_pregeneration_refused(self):
        with pytest.raises(ConfigError, match="pooling"):
            Config.from_dict(_minimal(freshness={"allow_pregeneration": True}))

    def test_bad_post_processing_mode_rejected(self):
        with pytest.raises(ConfigError, match="post_processing.mode"):
            Config.from_dict(_minimal(post_processing={"mode": "whitened"}))

    def test_bad_device_type_rejected(self):
        with pytest.raises(ConfigError, match="type"):
            Config.from_dict({"devices": [{"id": "d0", "type": "hopeium"}]})

    def test_hardware_device_requires_path(self):
        with pytest.raises(ConfigError, match="path"):
            Config.from_dict({"devices": [{"id": "d0", "type": "dragonfly"}]})

    def test_duplicate_device_id_rejected(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict(
                {"devices": [{"id": "d0", "type": "mock"}, {"id": "d0", "type": "mock"}]}
            )

    def test_no_bind_rejected(self):
        with pytest.raises(ConfigError, match="listen"):
            Config.from_dict(_minimal(server={"listen": "", "unix_socket": ""}))

    def test_missing_config_file_is_an_error(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "nope.yaml")


class TestExampleConfig:
    def test_shipped_example_parses(self):
        from pathlib import Path

        import yaml

        example = Path(__file__).parent.parent / "config.yaml.example"
        config = Config.from_dict(yaml.safe_load(example.read_text(encoding="utf-8")))
        assert config.post_processing_mode == "raw"
        assert config.devices[0].type == "firefly"
