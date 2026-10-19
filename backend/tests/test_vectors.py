"""The firmware's test-vector header is generated from the shared examples and kept current."""

from iot_backend import vectors


def test_the_generated_header_is_up_to_date():
    assert vectors.OUTPUT.read_text(encoding="utf-8") == vectors.generate(), (
        "run `uv run python -m iot_backend.vectors` after changing an example"
    )


def test_every_example_is_in_the_header():
    text = vectors.generate()
    frames = list(vectors.PROTOCOL_EXAMPLES.glob("*.json"))
    lines = list(vectors.SERIAL_EXAMPLES.glob("*.json"))
    assert f"#define IOT_FRAME_VECTOR_COUNT {len(frames)}" in text
    assert f"#define IOT_SERIAL_VECTOR_COUNT {len(lines)}" in text


def test_c_strings_cannot_be_misread():
    assert vectors._c_string(b'a"b\\c') == '"a\\"b\\\\c"'
    assert vectors._c_string(b"\xe9" + b"1") == '"\\3511"'  # octal: the 1 is not swallowed
    assert vectors._c_string(b"??=") == '"\\?\\?="'  # no trigraph
