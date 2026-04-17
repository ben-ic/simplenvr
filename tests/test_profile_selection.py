"""Decision-boundary tests for the ONVIF profile selector.

`select_main_and_sub` is the pure picker extracted from
`interrogate_camera`. It answers "given these ONVIF profiles, which one
do we record main from, and which one do we record sub from?"

The load-bearing rule these tests pin: **codec is the primary sort key
for sub selection, area is the tiebreaker.** Before the fix, two
profiles that tied on pixel area (a Tapo C120 exposing an H.264 sub and
an MJPEG "preview" both at 640×360) fell out of the selector based on
whatever order the camera's ONVIF stack returned them in — producing
silent-black recordings the first time the chronic-failure breaker
downgraded the recorder to sub. See plans/substream-codec-aware-plan.md.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.discovery.onvif_client import (
    _codec_rank,
    _profile_codec,
    select_main_and_sub,
)


# ---------------------------------------------------------------------------
# Synthetic profile factory — shapes a plain SimpleNamespace to match the
# attribute path the selector reads: profile.VideoEncoderConfiguration.
# Encoding + Resolution.Width/Height. We never exercise .token in these
# tests; that attribute is only read during the live GetStreamUri call.
# ---------------------------------------------------------------------------

def mk_profile(
    token: str,
    encoding: str | None,
    width: int | None = None,
    height: int | None = None,
):
    """Construct a fake ONVIF profile. `encoding=None` produces a
    profile with no VideoEncoderConfiguration at all — the edge case
    that crashes a naive selector that doesn't null-check the chain.
    `width=None` produces a profile with an encoder but no Resolution,
    another legitimate ONVIF shape (audio-only, analytics-only).
    """
    if encoding is None:
        return SimpleNamespace(token=token, VideoEncoderConfiguration=None)
    if width is None or height is None:
        return SimpleNamespace(
            token=token,
            VideoEncoderConfiguration=SimpleNamespace(
                Encoding=encoding, Resolution=None
            ),
        )
    return SimpleNamespace(
        token=token,
        VideoEncoderConfiguration=SimpleNamespace(
            Encoding=encoding,
            Resolution=SimpleNamespace(Width=width, Height=height),
        ),
    )


# ---------------------------------------------------------------------------
# Codec rank — the primary sort dimension.
# ---------------------------------------------------------------------------

def test_codec_rank_orders_h265_before_h264_before_mjpeg():
    h265 = mk_profile("p1", "H265", 3840, 2160).VideoEncoderConfiguration
    h264 = mk_profile("p2", "H264", 1920, 1080).VideoEncoderConfiguration
    mjpeg = mk_profile("p3", "MJPEG", 640, 360).VideoEncoderConfiguration
    none = mk_profile("p4", None).VideoEncoderConfiguration
    assert _codec_rank(h265) < _codec_rank(h264) < _codec_rank(mjpeg)
    # A missing encoder is indistinguishable from MJPEG rank-wise —
    # both are "anything we cannot confidently stream-copy."
    assert _codec_rank(none) == _codec_rank(mjpeg)


def test_profile_codec_normalizes_case_and_returns_none_for_missing():
    p = mk_profile("p1", "h264", 1920, 1080)
    assert _profile_codec(p) == "H264"
    p_none = mk_profile("p2", None)
    assert _profile_codec(p_none) is None
    # An encoder with an empty-string Encoding is treated as unknown —
    # zeep-async occasionally returns ``Encoding=""`` for profiles
    # stripped of their video config mid-transaction.
    p_empty = SimpleNamespace(
        VideoEncoderConfiguration=SimpleNamespace(Encoding="")
    )
    assert _profile_codec(p_empty) is None


# ---------------------------------------------------------------------------
# The load-bearing tests from the plan.
# ---------------------------------------------------------------------------

def test_prefers_h264_over_mjpeg_on_tie():
    """Tapo C120 reproduction: three profiles, /stream1 is H.264 2560×1440
    main, /stream2 is H.264 640×360 sub, /stream8 is MJPEG 640×360
    "preview". Area alone would tie /stream2 and /stream8 — the camera
    happens to return MJPEG first in real life, which is how we got the
    original bug. Codec-primary ranking picks /stream2."""
    main = mk_profile("stream1", "H264", 2560, 1440)
    sub_h264 = mk_profile("stream2", "H264", 640, 360)
    sub_mjpeg = mk_profile("stream8", "MJPEG", 640, 360)
    # Hand them to the selector in the exact order the Tapo returned
    # them — MJPEG preview first. A naive ONVIF-order tiebreaker
    # regresses to sub_mjpeg; codec-primary ranking picks sub_h264.
    profiles = [main, sub_mjpeg, sub_h264]

    got_main, got_sub = select_main_and_sub(profiles)

    assert got_main is main
    assert got_sub is sub_h264, (
        "MJPEG should never be selected as sub when an H.264 "
        "alternative at the same resolution exists"
    )


def test_prefers_h265_over_h264():
    """Axis-shaped case: an H.265 sub exists alongside an H.264 sub at
    the same resolution. H.265 costs less storage at matched quality,
    so it wins when available — it's also preferred for main."""
    main = mk_profile("p_main", "H264", 3840, 2160)
    sub_h264 = mk_profile("p_sub_h264", "H264", 640, 360)
    sub_h265 = mk_profile("p_sub_h265", "H265", 640, 360)

    got_main, got_sub = select_main_and_sub([main, sub_h264, sub_h265])

    assert got_main is main
    assert got_sub is sub_h265


def test_mjpeg_only_camera_picks_mjpeg_as_main():
    """Genuinely MJPEG-only camera (very old IP cams, some budget
    brands). Main falls through to the MJPEG profile — the recorder's
    codec-aware branch handles the transcode at record time. Sub is
    None because no second profile exists."""
    only = mk_profile("p1", "MJPEG", 640, 480)

    got_main, got_sub = select_main_and_sub([only])

    assert got_main is only
    assert got_sub is None
    assert _profile_codec(got_main) == "MJPEG"


def test_mjpeg_plus_h264_sub_rank_inversion():
    """Regression guard: a SMALL MJPEG profile and a LARGER H.264 profile
    at the sub slot. H.264 wins on codec rank despite having more
    pixels. Codec is the primary sort axis, not a tiebreaker — a dumb
    "smallest-area" selector would pick MJPEG here and produce black
    recordings."""
    main = mk_profile("m", "H264", 2560, 1440)
    tiny_mjpeg = mk_profile("sub_mjpeg", "MJPEG", 320, 240)
    larger_h264 = mk_profile("sub_h264", "H264", 640, 480)

    got_main, got_sub = select_main_and_sub([main, tiny_mjpeg, larger_h264])

    assert got_main is main
    assert got_sub is larger_h264, (
        "H.264 must win on codec rank even when a smaller MJPEG "
        "candidate exists; area is strictly secondary"
    )


# ---------------------------------------------------------------------------
# Edge cases flagged by the first architect's review.
# ---------------------------------------------------------------------------

def test_single_profile_camera_returns_no_sub_candidate():
    """A camera that exposes exactly one profile — the selector must
    return that profile as main and None for sub, not crash when the
    sub_candidates list is empty. Realistic for some budget IP cameras
    and the Eufy hub's per-camera pass-through stream."""
    only = mk_profile("p1", "H264", 1920, 1080)

    got_main, got_sub = select_main_and_sub([only])

    assert got_main is only
    assert got_sub is None


def test_sub_candidate_with_missing_video_encoder_config():
    """Some ONVIF profiles have no VideoEncoderConfiguration at all —
    audio-only, analytics-only, metadata-only Profile T extensions.
    The selector must not crash when such a profile ends up in the
    sub candidate list; it should rank it as codec-dispreferred and
    fall back to it only when no other option exists."""
    main = mk_profile("p_main", "H264", 1920, 1080)
    sub_no_encoder = mk_profile("p_meta", None)  # VideoEncoderConfiguration is None

    got_main, got_sub = select_main_and_sub([main, sub_no_encoder])

    assert got_main is main
    # Only one sub candidate exists and it has no encoder; the selector
    # still returns it rather than None, so downstream GetStreamUri
    # can attempt the fetch. If GetStreamUri fails the scanner logs it
    # and moves on — no sub_stream is recorded, but the selector did
    # not crash on a null encoder chain.
    assert got_sub is sub_no_encoder
    assert _profile_codec(got_sub) is None


def test_all_profiles_missing_encoder_configs_does_not_crash():
    """Pathological camera that returns profiles with every
    VideoEncoderConfiguration set to None. The selector can't rank on
    codec or area — it falls back to first-profile-returned for main
    and picks the next one as sub. Decision: don't crash, let the
    caller's GetStreamUri attempt surface the real failure. See
    plans/substream-codec-aware-plan.md §Known limitations."""
    p1 = mk_profile("p1", None)
    p2 = mk_profile("p2", None)

    got_main, got_sub = select_main_and_sub([p1, p2])

    assert got_main is p1
    assert got_sub is p2


def test_empty_profile_list_raises():
    """Defensive: selector refuses to guess when given nothing. The
    caller should have already branched on `if profiles:` — raising
    here makes a regression in that guard loud instead of silently
    picking something nonexistent."""
    import pytest

    with pytest.raises(ValueError):
        select_main_and_sub([])
