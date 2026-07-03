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


class TestChardev:
    """PCIe Dragonfly char-device entries (type `chardev`)."""

    def test_chardev_accepted_with_pci_address(self):
        config = Config.from_dict(
            {
                "devices": [
                    {
                        "id": "dragonfly-0",
                        "type": "chardev",
                        "path": "/dev/qrngDF0",
                        "pci_address": "0000:09:00.0",
                    }
                ]
            }
        )
        assert config.devices[0].type == "chardev"
        assert config.devices[0].pci_address == "0000:09:00.0"

    def test_pci_address_defaults_to_none(self):
        config = Config.from_dict(
            {"devices": [{"id": "df-0", "type": "chardev", "path": "/dev/qrngDF0"}]}
        )
        assert config.devices[0].pci_address is None

    def test_chardev_requires_path(self):
        with pytest.raises(ConfigError, match="path"):
            Config.from_dict({"devices": [{"id": "df-0", "type": "chardev"}]})

    def test_pci_address_rejected_on_non_chardev_types(self):
        for dev_type, path in [("mock", ""), ("dragonfly", "/dev/ttyQRNG0")]:
            with pytest.raises(ConfigError, match="pci_address"):
                Config.from_dict(
                    {
                        "devices": [
                            {
                                "id": "d0",
                                "type": dev_type,
                                "path": path,
                                "pci_address": "0000:09:00.0",
                            }
                        ]
                    }
                )

    def test_post_processing_rejected_on_chardev(self):
        # chardev has no qcc-cli -P chain: it serves whatever the DMA delivers.
        with pytest.raises(ConfigError, match="post_processing"):
            Config.from_dict(
                {
                    "devices": [
                        {
                            "id": "df-0",
                            "type": "chardev",
                            "path": "/dev/qrngDF0",
                            "post_processing": "raw",
                        }
                    ]
                }
            )

    def test_chardev_has_no_oneshot_limit(self):
        from qbert0g.config import ONE_SHOT_LIMITS

        assert "chardev" not in ONE_SHOT_LIMITS


def _controls(*entries) -> list:
    default = {
        "id": "prng-uniform-0",
        "type": "prng_uniform",
        "seed": "0x9e3779b97f4a7c15f39cc0605cedc834",
    }
    return [dict(default, **e) for e in entries] if entries else [default]


class TestControls:
    """PRNG control sources (`controls:` section)."""

    def test_prng_uniform_accepted(self):
        config = Config.from_dict(_minimal(controls=_controls()))
        assert config.controls[0].id == "prng-uniform-0"
        assert config.controls[0].type == "prng_uniform"
        assert config.controls[0].seed_int == 0x9E3779B97F4A7C15F39CC0605CEDC834

    def test_prng_markov_accepted_with_model(self):
        config = Config.from_dict(
            _minimal(
                controls=_controls(
                    {"id": "prng-markov-0", "type": "prng_markov", "model": "/models/df0.npz"}
                )
            )
        )
        assert config.controls[0].model == "/models/df0.npz"

    def test_controls_default_to_empty(self):
        config = Config.from_dict(_minimal())
        assert config.controls == []
        assert config.profiles == []

    def test_unknown_control_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict(_minimal(controls=_controls({"sed": "typo"})))

    def test_unknown_control_type_rejected(self):
        with pytest.raises(ConfigError, match="type"):
            Config.from_dict(_minimal(controls=_controls({"type": "prng_mersenne"})))

    def test_seed_required(self):
        entry = _controls()[0]
        del entry["seed"]
        with pytest.raises(ConfigError, match="seed"):
            Config.from_dict(_minimal(controls=[entry]))

    def test_seed_must_be_128_bit_hex(self):
        for bad in ("42", "0x42", "0x" + "f" * 31, "0x" + "f" * 33, "0x" + "g" * 32, ""):
            with pytest.raises(ConfigError, match="seed"):
                Config.from_dict(_minimal(controls=_controls({"seed": bad})))

    def test_markov_requires_model(self):
        with pytest.raises(ConfigError, match="model"):
            Config.from_dict(_minimal(controls=_controls({"type": "prng_markov"})))

    def test_model_rejected_on_uniform(self):
        with pytest.raises(ConfigError, match="model"):
            Config.from_dict(_minimal(controls=_controls({"model": "/models/df0.npz"})))

    def test_control_id_colliding_with_device_rejected(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict(_minimal(controls=_controls({"id": "mock-0"})))


def _profile(**overrides) -> dict:
    entry = {"id": "raw-mock", "transform": "identity", "inputs": ["mock-0"]}
    entry.update(overrides)
    return entry


class TestProfiles:
    """Profile transforms (`profiles:` section) + the period-4 guard."""

    def test_identity_profile_accepted(self):
        config = Config.from_dict(_minimal(profiles=[_profile()]))
        assert config.profiles[0].transform == "identity"
        assert config.profiles[0].inputs == ["mock-0"]

    def test_xnor_over_device_and_control_accepted(self):
        config = Config.from_dict(
            _minimal(
                controls=_controls(),
                profiles=[
                    _profile(
                        id="qp-match", transform="xnor", inputs=["mock-0", "prng-uniform-0"]
                    )
                ],
            )
        )
        assert config.profiles[0].inputs == ["mock-0", "prng-uniform-0"]

    def test_parity_profile_accepted(self):
        config = Config.from_dict(
            _minimal(
                profiles=[
                    _profile(
                        id="parity4",
                        transform="parity",
                        params={"taps": [0, 9, 19, 30], "stride": 4, "allow_period4": True},
                    )
                ]
            )
        )
        assert config.profiles[0].taps == (0, 9, 19, 30)
        assert config.profiles[0].stride == 4
        assert config.profiles[0].allow_period4 is True

    def test_unknown_transform_rejected(self):
        with pytest.raises(ConfigError, match="transform"):
            Config.from_dict(_minimal(profiles=[_profile(transform="sha256")]))

    def test_arity_mismatch_rejected(self):
        with pytest.raises(ConfigError, match="exactly 2"):
            Config.from_dict(_minimal(profiles=[_profile(transform="xnor")]))
        with pytest.raises(ConfigError, match="exactly 1"):
            Config.from_dict(_minimal(profiles=[_profile(inputs=["mock-0", "mock-0"])]))

    def test_unknown_input_rejected(self):
        with pytest.raises(ConfigError, match="not a configured device or control"):
            Config.from_dict(_minimal(profiles=[_profile(inputs=["ghost-0"])]))

    def test_profile_referencing_profile_rejected(self):
        # No nesting (v1): a profile input must be a device or control.
        with pytest.raises(ConfigError, match="not a configured device or control"):
            Config.from_dict(
                _minimal(profiles=[_profile(), _profile(id="nested", inputs=["raw-mock"])])
            )

    def test_profile_id_colliding_with_device_rejected(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict(_minimal(profiles=[_profile(id="mock-0")]))

    def test_params_rejected_on_non_parity(self):
        with pytest.raises(ConfigError, match="params"):
            Config.from_dict(_minimal(profiles=[_profile(params={"taps": [0, 1], "stride": 1})]))

    def test_parity_requires_params(self):
        with pytest.raises(ConfigError, match="params"):
            Config.from_dict(_minimal(profiles=[_profile(transform="parity")]))

    def test_parity_taps_must_be_strictly_increasing_non_negative(self):
        for bad_taps in ([], [3, 1], [1, 1], [-1, 2], [0, "one"], "nope"):
            with pytest.raises(ConfigError, match="taps"):
                Config.from_dict(
                    _minimal(
                        profiles=[
                            _profile(
                                transform="parity", params={"taps": bad_taps, "stride": 1}
                            )
                        ]
                    )
                )

    def test_parity_stride_must_be_positive_int(self):
        for bad_stride in (0, -4, 1.5, "four"):
            with pytest.raises(ConfigError, match="stride"):
                Config.from_dict(
                    _minimal(
                        profiles=[
                            _profile(
                                transform="parity", params={"taps": [0, 1], "stride": bad_stride}
                            )
                        ]
                    )
                )

    def test_period4_tap_distance_rejected(self):
        with pytest.raises(ConfigError, match="multiple of 4"):
            Config.from_dict(
                _minimal(
                    profiles=[_profile(transform="parity", params={"taps": [0, 4], "stride": 1})]
                )
            )

    def test_period4_stride_rejected(self):
        with pytest.raises(ConfigError, match="multiple of 4"):
            Config.from_dict(
                _minimal(
                    profiles=[_profile(transform="parity", params={"taps": [0, 1], "stride": 8})]
                )
            )

    def test_period4_accepted_with_escape_hatch(self):
        config = Config.from_dict(
            _minimal(
                profiles=[
                    _profile(
                        transform="parity",
                        params={"taps": [0, 4], "stride": 8, "allow_period4": True},
                    )
                ]
            )
        )
        assert config.profiles[0].taps == (0, 4)


class TestProfilesDefaults:
    def test_defaults(self):
        config = Config.from_dict(_minimal())
        assert config.profiles_defaults.chunk_bytes == 4096
        assert config.profiles_defaults.max_skew_ns == 50_000_000

    def test_overrides(self):
        config = Config.from_dict(
            _minimal(profiles_defaults={"chunk_bytes": 8192, "max_skew_ns": 1_000_000})
        )
        assert config.profiles_defaults.chunk_bytes == 8192
        assert config.profiles_defaults.max_skew_ns == 1_000_000

    def test_unknown_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown key"):
            Config.from_dict(_minimal(profiles_defaults={"chunk_size": 8192}))

    def test_bad_values_rejected(self):
        with pytest.raises(ConfigError, match="chunk_bytes"):
            Config.from_dict(_minimal(profiles_defaults={"chunk_bytes": 0}))
        with pytest.raises(ConfigError, match="max_skew_ns"):
            Config.from_dict(_minimal(profiles_defaults={"max_skew_ns": -1}))


class TestExampleConfig:
    def test_shipped_example_parses(self):
        from pathlib import Path

        import yaml

        example = Path(__file__).parent.parent / "config.yaml.example"
        config = Config.from_dict(yaml.safe_load(example.read_text(encoding="utf-8")))
        assert config.post_processing_mode == "raw"
        assert config.devices[0].type == "firefly"
        # The §6/§7 blocks ship in the example and must validate.
        assert config.controls[0].type == "prng_uniform"
        assert config.profiles[0].transform == "identity"
        assert config.profiles_defaults.chunk_bytes == 4096
