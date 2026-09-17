from local_stt.audio.capture import parse_sources, resolve_routed_source

SOURCES = [
    {"index": 51, "name": "alsa_output.pci.analog-stereo.monitor", "description": "Monitor"},
    {"index": 52, "name": "alsa_input.pci.analog-stereo", "description": "Built-in"},
    {"index": 60, "name": "bluez_input.headset", "description": "Headset"},
]


def test_parse_sources_skips_monitors_and_marks_default() -> None:
    devices = parse_sources(SOURCES, default="bluez_input.headset")
    assert [(d.name, d.is_default) for d in devices] == [
        ("alsa_input.pci.analog-stereo", False),
        ("bluez_input.headset", True),
    ]


def test_resolve_routed_source_uses_source_index_not_target_object() -> None:
    outputs = [
        {"source": 60, "properties": {"node.name": "other-app"}},
        # PipeWire echoes the requested target even when it does not exist.
        {
            "source": 52,
            "properties": {"node.name": "local-stt.capture.1.3", "target.object": "nope"},
        },
    ]
    assert resolve_routed_source(outputs, SOURCES, "local-stt.capture.1.3") == (
        "alsa_input.pci.analog-stereo"
    )
    assert resolve_routed_source(outputs, SOURCES, "local-stt.capture.1.4") is None
