"""FR-4 纯逻辑：标识版式、过滤器转义、字体发现、元数据、参数校验。

对应验收：AC-10（显式标识几何/时长）、AC-11（隐式字段定义）、AC-12（无移除途径）

这些函数把"合规细节"从 ffmpeg 调用里拆出来单独测：
- 过滤器转义错 = 崩溃或标识静默不出现 → 必须单测钉死（本机 ffmpeg 的规则是冒号两层转义）
- 字体文件不存在 = ffmpeg 访问违例崩溃 → 必须调用前自己校验
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from submatrix.mark import (
    DEFAULT_COMMENT,
    DEFAULT_LABEL,
    DEFAULT_POSITION,
    DEFAULT_PROVIDER,
    DEFAULT_SECONDS,
    METADATA_FIELDS,
    MarkError,
    MarkOptions,
    build_drawtext,
    build_metadata,
    escape_filter_path,
    label_geometry,
    new_content_id,
    resolve_font,
)


def options(**kwargs) -> MarkOptions:
    base = {"src": Path("in.mp4"), "out": Path("out")}
    base.update(kwargs)
    return MarkOptions(**base)


# ---------------------------------------------------------------- 路径转义


def test_escape_filter_path_escapes_drive_colon_twice() -> None:
    """ffmpeg 过滤器要过两层解析，盘符冒号必须写成两个反斜杠 + 冒号。"""

    assert escape_filter_path(Path(r"C:\Windows\Fonts\msyh.ttc")) == r"C\\:/Windows/Fonts/msyh.ttc"


def test_escape_filter_path_keeps_spaces() -> None:
    assert escape_filter_path(Path(r"D:\我的 字体\x.ttf")) == r"D\\:/我的 字体/x.ttf"


def test_escape_filter_path_uses_forward_slashes() -> None:
    assert escape_filter_path(Path(r"C:\a\b.ttf")) == r"C\\:/a/b.ttf"


# ---------------------------------------------------------------- 版式


def test_label_geometry_scales_with_height() -> None:
    small = label_geometry(640, 360)
    large = label_geometry(1920, 1080)
    huge = label_geometry(3840, 2160)

    assert small.font_size < large.font_size < huge.font_size
    assert large.font_size == round(1080 * 0.05)
    assert huge.font_size == round(2160 * 0.05)


def test_label_geometry_has_minimums_for_tiny_video() -> None:
    geometry = label_geometry(320, 180)

    assert geometry.font_size >= 20
    assert geometry.margin >= 12
    assert geometry.padding >= 8


def test_label_geometry_top_right_expressions() -> None:
    geometry = label_geometry(1920, 1080, "top-right")

    assert geometry.x_expr == f"w-tw-{geometry.margin}"
    assert geometry.y_expr == str(geometry.margin)


def test_label_geometry_top_left_expressions() -> None:
    geometry = label_geometry(1920, 1080, "top-left")

    assert geometry.x_expr == str(geometry.margin)
    assert geometry.y_expr == str(geometry.margin)


def test_label_geometry_bottom_right_expressions() -> None:
    geometry = label_geometry(1920, 1080, "bottom-right")

    assert geometry.x_expr == f"w-tw-{geometry.margin}"
    assert geometry.y_expr == f"h-th-{geometry.margin}"


def test_label_geometry_bottom_left_expressions() -> None:
    geometry = label_geometry(1920, 1080, "bottom-left")

    assert geometry.x_expr == str(geometry.margin)
    assert geometry.y_expr == f"h-th-{geometry.margin}"


def test_label_geometry_rejects_unknown_position() -> None:
    with pytest.raises(ValueError, match="位置"):
        label_geometry(1920, 1080, "middle")


# ---------------------------------------------------------------- drawtext


def test_drawtext_contains_required_options() -> None:
    textfile = Path(r"C:\work\out\label.txt")
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    geometry = label_geometry(1920, 1080)

    filter_text = build_drawtext(
        textfile=textfile, font_file=font, geometry=geometry, seconds=3
    )

    assert filter_text.startswith("drawtext=")
    assert f"textfile={escape_filter_path(textfile)}" in filter_text
    assert f"fontfile={escape_filter_path(font)}" in filter_text
    assert ":expansion=none" in filter_text
    assert f":fontsize={geometry.font_size}" in filter_text
    assert ":fontcolor=white" in filter_text
    assert ":box=1" in filter_text
    assert f":boxborderw={geometry.padding}" in filter_text
    assert f":x={geometry.x_expr}" in filter_text
    assert f":y={geometry.y_expr}" in filter_text


def test_drawtext_formats_seconds_with_millisecond_precision() -> None:
    filter_text = build_drawtext(
        textfile=Path("label.txt"),
        font_file=Path("font.ttf"),
        geometry=label_geometry(1280, 720),
        seconds=3,
    )

    assert ":enable='between(t,0,3.000)'" in filter_text


def test_drawtext_supports_longer_label_duration() -> None:
    filter_text = build_drawtext(
        textfile=Path("label.txt"),
        font_file=Path("font.ttf"),
        geometry=label_geometry(1280, 720),
        seconds=7.5,
    )

    assert "between(t,0,7.500)" in filter_text


# ---------------------------------------------------------------- 字体发现


def test_resolve_font_uses_explicit_file(tmp_path) -> None:
    font = tmp_path / "custom.ttf"
    font.write_bytes(b"fake")

    assert resolve_font(font) == font


def test_resolve_font_rejects_missing_explicit_file(tmp_path) -> None:
    """字体不存在时 ffmpeg 会访问违例崩溃，所以必须在自己这层先拦住。"""

    with pytest.raises(MarkError, match="字体"):
        resolve_font(tmp_path / "nope.ttf")


def test_resolve_font_picks_first_existing_candidate(tmp_path) -> None:
    missing = tmp_path / "a.ttf"
    existing = tmp_path / "b.ttf"
    existing.write_bytes(b"fake")

    assert resolve_font(None, candidates=(missing, existing)) == existing


def test_resolve_font_errors_when_nothing_found(tmp_path) -> None:
    with pytest.raises(MarkError, match="字体"):
        resolve_font(None, candidates=(tmp_path / "nope1.ttf", tmp_path / "nope2.ttf"))


# ---------------------------------------------------------------- 内容编号 / 元数据


def test_new_content_id_is_uuid4() -> None:
    content_id = new_content_id()

    assert str(uuid.UUID(content_id)) == content_id
    assert uuid.UUID(content_id).version == 4


def test_new_content_id_is_unique() -> None:
    assert len({new_content_id() for _ in range(100)}) == 100


def test_build_metadata_defaults() -> None:
    metadata = build_metadata(options(), "cid")

    assert metadata == {
        "comment": DEFAULT_COMMENT,
        "artist": DEFAULT_PROVIDER,
        "copyright": DEFAULT_PROVIDER,
        "content_id": "cid",
    }
    assert set(metadata) == set(METADATA_FIELDS)


def test_build_metadata_respects_provider_and_overrides() -> None:
    metadata = build_metadata(
        options(provider="某某工作室", comment="AI 翻译", copyright_text="© 2026 某某"),
        "cid",
    )

    assert metadata["artist"] == "某某工作室"
    assert metadata["comment"] == "AI 翻译"
    assert metadata["copyright"] == "© 2026 某某"


def test_build_metadata_copyright_follows_provider_by_default() -> None:
    assert build_metadata(options(provider="X"), "cid")["copyright"] == "X"


# ---------------------------------------------------------------- 参数校验


def test_default_options_are_compliant() -> None:
    defaults = options()

    assert defaults.label_text == DEFAULT_LABEL
    assert defaults.label_seconds == DEFAULT_SECONDS
    assert defaults.position == DEFAULT_POSITION
    assert defaults.self_use is False
    defaults.validate()


def test_validate_rejects_empty_label() -> None:
    with pytest.raises(MarkError, match="标识文案"):
        options(label_text="   ").validate()


def test_validate_rejects_label_shorter_than_three_seconds() -> None:
    with pytest.raises(MarkError, match="3 秒"):
        options(label_seconds=2.5).validate()


def test_validate_accepts_longer_label_duration() -> None:
    options(label_seconds=10).validate()


def test_validate_rejects_unknown_position() -> None:
    with pytest.raises(MarkError, match="位置"):
        options(position="center").validate()


def test_validate_rejects_empty_provider() -> None:
    with pytest.raises(MarkError, match="服务提供者"):
        options(provider=" ").validate()
