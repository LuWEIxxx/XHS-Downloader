"""Create an Apple Live Photo pair from an image and a short movie.

The ISO-BMFF metadata layout follows Apple's public Live Photo metadata
conventions.  The implementation is adapted from
``yangzhen-23/video-to-live-photo`` (Apache-2.0); see the project URL and
license notice below.  It intentionally performs no video re-encoding.
"""

from __future__ import annotations

import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# Portions of this module are adapted from:
# https://github.com/yangzhen-23/video-to-live-photo
# Copyright 2026 Yang Zhen, Apache License 2.0.

CONTENT_KEY = b"com.apple.quicktime.content.identifier"
STILL_TIME_KEY = b"com.apple.quicktime.still-image-time"
TIMED_SAMPLE = b"\x00\x00\x00\x09\x00\x00\x00\x01\xff"
CONTAINER_TYPES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"dinf",
    b"stbl",
    b"edts",
    b"tref",
    b"gmhd",
    b"udta",
    b"ilst",
}


@dataclass(frozen=True, slots=True)
class Atom:
    start: int
    size: int
    type: bytes
    header_size: int = 8

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    def raw(self, data: bytes | bytearray) -> bytes:
        return bytes(data[self.start : self.end])


@dataclass(frozen=True, slots=True)
class LiveMovInfo:
    asset_id: str
    duration: float
    still_time: float
    has_video: bool
    has_audio: bool


def atom(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 4:
        raise ValueError("ISO-BMFF atom 类型必须是 4 字节")
    size = len(payload) + 8
    if size <= 0xFFFFFFFF:
        return struct.pack(">I4s", size, kind) + payload
    return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload


def parse_atoms(
    data: bytes | bytearray,
    start: int = 0,
    end: int | None = None,
) -> list[Atom]:
    boundary = len(data) if end is None else end
    if start < 0 or boundary > len(data) or start > boundary:
        raise ValueError("ISO-BMFF 解析范围无效")
    result: list[Atom] = []
    position = start
    while position < boundary:
        if position + 8 > boundary:
            raise ValueError("ISO-BMFF atom 头部不完整")
        size = int.from_bytes(data[position : position + 4], "big")
        kind = bytes(data[position + 4 : position + 8])
        header = 8
        if size == 1:
            if position + 16 > boundary:
                raise ValueError("ISO-BMFF 64 位 atom 头部不完整")
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header = 16
        elif size == 0:
            size = boundary - position
        if size < header or position + size > boundary:
            label = kind.decode("latin-1", errors="replace")
            raise ValueError(f"ISO-BMFF atom {label} 超出文件边界")
        result.append(Atom(position, size, kind, header))
        position += size
    return result


def child_atoms(data: bytes | bytearray, parent: Atom) -> list[Atom]:
    return parse_atoms(data, parent.payload_start, parent.end)


def first_child(
    data: bytes | bytearray,
    parent: Atom | None,
    kind: bytes,
) -> Atom | None:
    if parent is None:
        return None
    return next((item for item in child_atoms(data, parent) if item.type == kind), None)


def patch_chunk_offsets(moov: bytes, delta: int) -> bytes:
    payload = bytearray(moov)
    top = parse_atoms(payload)
    if len(top) != 1 or top[0].type != b"moov":
        raise ValueError("需要一个完整的 moov atom")

    def visit(parent: Atom) -> None:
        for item in child_atoms(payload, parent):
            if item.type in {b"stco", b"co64"}:
                count_at = item.payload_start + 4
                count = int.from_bytes(payload[count_at : count_at + 4], "big")
                width = 4 if item.type == b"stco" else 8
                entries = count_at + 4
                if entries + count * width > item.end:
                    raise ValueError("MOV chunk offset 条目超出 atom")
                for index in range(count):
                    position = entries + index * width
                    old = int.from_bytes(payload[position : position + width], "big")
                    new = old + delta
                    if new < 0 or new >= 1 << (width * 8):
                        raise ValueError("MOV chunk offset 调整后越界")
                    payload[position : position + width] = new.to_bytes(width, "big")
            elif item.type in CONTAINER_TYPES:
                visit(item)

    visit(top[0])
    return bytes(payload)


def _canonical_asset_id(asset_id: str) -> str:
    try:
        return str(uuid.UUID(asset_id)).upper()
    except (ValueError, AttributeError) as error:
        raise ValueError("Apple 资产标识必须是标准 UUID") from error


def apple_maker_note(asset_id: str) -> bytes:
    identifier = _canonical_asset_id(asset_id).encode("ascii")
    return (
        b"Apple iOS\x00\x00\x01MM"
        + b"\x00\x01"
        + b"\x00\x11\x00\x02"
        + struct.pack(">I", len(identifier) + 1)
        + struct.pack(">I", 32)
        + b"\x00\x00\x00\x00"
        + identifier
        + b"\x00"
    )


def write_live_jpeg(source: Path, output: Path, asset_id: str) -> None:
    note = apple_maker_note(asset_id)
    with Image.open(source) as original:
        image = original.convert("RGB")
        exif = original.getexif()
        exif[0x010F] = "Apple"
        exif[0x0110] = "XHS Downloader Live Photo"
        exif[0x927C] = note
        save_args: dict[str, object] = {
            "format": "JPEG",
            "quality": 95,
            "subsampling": 0,
            "exif": exif,
        }
        if original.info.get("icc_profile"):
            save_args["icc_profile"] = original.info["icc_profile"]
        image.save(output, **save_args)


def _fullbox(version: int = 0, flags: int = 0) -> bytes:
    return bytes((version,)) + flags.to_bytes(3, "big")


def _content_identifier_meta(asset_id: str) -> bytes:
    identifier = _canonical_asset_id(asset_id).encode("ascii")
    hdlr = atom(
        b"hdlr",
        _fullbox() + b"\x00" * 4 + b"mdta" + b"\x00" * 12 + b"\x00\x00",
    )
    key_entry = struct.pack(">I4s", 8 + len(CONTENT_KEY), b"mdta") + CONTENT_KEY
    keys = atom(b"keys", _fullbox() + struct.pack(">I", 1) + key_entry)
    data = atom(b"data", struct.pack(">II", 1, 0) + identifier)
    item = atom(b"\x00\x00\x00\x01", data)
    return atom(b"meta", hdlr + keys + atom(b"ilst", item))


def _pascal(text: bytes) -> bytes:
    if len(text) > 255:
        raise ValueError("Pascal 字符串过长")
    return bytes((len(text),)) + text


def _track_id(data: bytes, trak: Atom) -> int:
    tkhd = first_child(data, trak, b"tkhd")
    if tkhd is None:
        raise ValueError("MOV 轨道缺少 tkhd")
    version = data[tkhd.payload_start]
    position = tkhd.start + (28 if version == 1 else 20)
    return int.from_bytes(data[position : position + 4], "big")


def _track_handler(data: bytes, trak: Atom) -> bytes | None:
    mdia = first_child(data, trak, b"mdia")
    hdlr = first_child(data, mdia, b"hdlr")
    if hdlr is None or hdlr.start + 20 > hdlr.end:
        return None
    return data[hdlr.start + 16 : hdlr.start + 20]


def _movie_timescale(data: bytes, moov: Atom) -> int:
    mvhd = first_child(data, moov, b"mvhd")
    if mvhd is None:
        raise ValueError("MOV 缺少 mvhd")
    version = data[mvhd.payload_start]
    position = mvhd.start + (28 if version == 1 else 20)
    timescale = int.from_bytes(data[position : position + 4], "big")
    if timescale <= 0:
        raise ValueError("MOV 时间基无效")
    return timescale


def _movie_duration_from_data(data: bytes) -> float:
    moov = next((item for item in parse_atoms(data) if item.type == b"moov"), None)
    if moov is None:
        raise ValueError("MOV 缺少 moov atom")
    mvhd = first_child(data, moov, b"mvhd")
    if mvhd is None:
        raise ValueError("MOV 缺少 mvhd")
    version = data[mvhd.payload_start]
    position = mvhd.start + (28 if version == 1 else 20)
    timescale = int.from_bytes(data[position : position + 4], "big")
    width = 8 if version == 1 else 4
    duration = int.from_bytes(data[position + 4 : position + 4 + width], "big")
    if timescale <= 0 or duration <= 0:
        raise ValueError("MOV 时长无效")
    return duration / timescale


def movie_duration(source: Path) -> float:
    return _movie_duration_from_data(source.read_bytes())


def _timed_metadata_track(
    track_id: int,
    video_track_id: int,
    timescale: int,
    cover_ticks: int,
    sample_offset: int,
) -> bytes:
    track_duration = cover_ticks + 1
    matrix = struct.pack(
        ">9I",
        0x00010000,
        0,
        0,
        0,
        0x00010000,
        0,
        0,
        0,
        0x40000000,
    )
    tkhd = atom(
        b"tkhd",
        _fullbox(flags=0x0F)
        + struct.pack(">IIIII", 0, 0, track_id, 0, track_duration)
        + b"\x00" * 8
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + matrix
        + struct.pack(">II", 0, 0),
    )
    edits: list[bytes] = []
    if cover_ticks:
        edits.append(struct.pack(">IiHH", cover_ticks, -1, 1, 0))
    edits.append(struct.pack(">IiHH", 1, 0, 1, 0))
    elst = atom(b"elst", _fullbox() + struct.pack(">I", len(edits)) + b"".join(edits))
    edts = atom(b"edts", elst)
    tref = atom(b"tref", atom(b"cdsc", struct.pack(">I", video_track_id)))
    mdhd = atom(
        b"mdhd",
        _fullbox()
        + struct.pack(">IIII", 0, 0, timescale, 1)
        + struct.pack(">HH", 0x55C4, 0),
    )
    metadata_name = b"Core Media Metadata"
    hdlr = atom(
        b"hdlr",
        _fullbox()
        + b"mhlrmetaappl"
        + struct.pack(">II", 1, 0)
        + _pascal(metadata_name),
    )
    gmin = atom(
        b"gmin",
        _fullbox() + struct.pack(">H3HHH", 0x0040, 0x8000, 0x8000, 0x8000, 0, 0),
    )
    gmhd = atom(b"gmhd", gmin)
    data_name = b"Core Media Data Handler"
    data_hdlr = atom(
        b"hdlr",
        _fullbox()
        + b"dhlralisappl"
        + struct.pack(">II", 0, 0)
        + _pascal(data_name),
    )
    alias = atom(b"alis", _fullbox(flags=1))
    dref = atom(b"dref", _fullbox() + struct.pack(">I", 1) + alias)
    dinf = atom(b"dinf", dref)
    keyd = atom(b"keyd", b"mdta" + STILL_TIME_KEY)
    dtyp = atom(b"dtyp", _fullbox() + struct.pack(">I", 0x41))
    key_record = struct.pack(">II", 8 + len(keyd) + len(dtyp), 1) + keyd + dtyp
    keys = atom(b"keys", key_record)
    mebx = atom(b"mebx", b"\x00" * 6 + struct.pack(">H", 1) + keys)
    stsd = atom(b"stsd", _fullbox() + struct.pack(">I", 1) + mebx)
    stts = atom(b"stts", _fullbox() + struct.pack(">III", 1, 1, 1))
    stsc = atom(b"stsc", _fullbox() + struct.pack(">IIII", 1, 1, 1, 1))
    stsz = atom(b"stsz", _fullbox() + struct.pack(">II", 0, 1) + struct.pack(">I", len(TIMED_SAMPLE)))
    chunk_offsets = atom(b"stco", _fullbox() + struct.pack(">II", 1, sample_offset))
    stbl = atom(b"stbl", stsd + stts + stsc + stsz + chunk_offsets)
    minf = atom(b"minf", gmhd + data_hdlr + dinf + stbl)
    mdia = atom(b"mdia", mdhd + hdlr + minf)
    return atom(b"trak", tkhd + edts + tref + mdia)


def write_live_mov(
    source: Path,
    output: Path,
    asset_id: str,
) -> None:
    """Write a v2 Live Photo movie with ``moov`` before ``mdat``.

    Keeping the movie index ahead of media data lets mobile clients start
    playback after fetching only the beginning of the file.  All existing
    media samples are copied without re-encoding.
    """
    identifier = _canonical_asset_id(asset_id)
    data = source.read_bytes()
    top = parse_atoms(data)
    mdat = next((item for item in top if item.type == b"mdat"), None)
    moov = next((item for item in top if item.type == b"moov"), None)
    if mdat is None or moov is None:
        raise ValueError("MOV 必须包含 mdat 和 moov")
    timescale = _movie_timescale(data, moov)
    duration = _movie_duration_from_data(data)
    tracks = [item for item in child_atoms(data, moov) if item.type == b"trak"]
    ids = [_track_id(data, item) for item in tracks]
    video = next((item for item in tracks if _track_handler(data, item) == b"vide"), None)
    if video is None:
        raise ValueError("MOV 中没有视频轨")
    video_track_id = _track_id(data, video)
    total_ticks = max(1, round(duration * timescale))
    cover_ticks = min(max(0, round(duration * timescale / 2)), total_ticks - 1)
    prefix = b"".join(item.raw(data) for item in top if item.type not in {b"mdat", b"moov"})
    old_media = data[mdat.payload_start : mdat.end]
    new_mdat = atom(b"mdat", old_media + TIMED_SAMPLE)
    mdat_header_size = 16 if new_mdat[:4] == b"\x00\x00\x00\x01" else 8

    original_moov = moov.raw(data)
    parsed_original = parse_atoms(original_moov)[0]
    original_children = original_moov[
        parsed_original.payload_start : parsed_original.end
    ]
    placeholder_additions = _content_identifier_meta(identifier) + _timed_metadata_track(
        max(ids, default=0) + 1,
        video_track_id,
        timescale,
        cover_ticks,
        0,
    )
    placeholder_moov = atom(b"moov", original_children + placeholder_additions)
    new_media_start = len(prefix) + len(placeholder_moov) + mdat_header_size
    delta = new_media_start - mdat.payload_start
    sample_offset = new_media_start + len(old_media)

    patched_moov = patch_chunk_offsets(original_moov, delta)
    parsed_patched = parse_atoms(patched_moov)[0]
    patched_children = patched_moov[parsed_patched.payload_start : parsed_patched.end]
    additions = _content_identifier_meta(identifier) + _timed_metadata_track(
        max(ids, default=0) + 1,
        video_track_id,
        timescale,
        cover_ticks,
        sample_offset,
    )
    new_moov = atom(b"moov", patched_children + additions)
    if len(new_moov) != len(placeholder_moov):
        raise ValueError("MOV moov 大小在偏移修正后发生变化")
    output.write_bytes(prefix + new_moov + new_mdat)


def inspect_live_pair(image: Path, movie: Path) -> str:
    with Image.open(image) as source:
        note = source.getexif().get(0x927C)
    if not isinstance(note, bytes) or not note.startswith(b"Apple iOS\x00\x00\x01MM"):
        raise ValueError("JPG 缺少 Apple MakerNote")
    count = int.from_bytes(note[20:24], "big")
    offset = int.from_bytes(note[24:28], "big")
    image_id = _canonical_asset_id(note[offset : offset + count].rstrip(b"\x00").decode("ascii"))
    data = movie.read_bytes()
    _top = parse_atoms(data)
    moov = next(item for item in _top if item.type == b"moov")
    ids = re.findall(rb"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", moov.raw(data))
    if not ids:
        raise ValueError("MOV 缺少 Apple content identifier")
    movie_id = _canonical_asset_id(ids[-1].decode("ascii"))
    if image_id != movie_id:
        raise ValueError("JPG 与 MOV 的 Apple UUID 不一致")
    return image_id
