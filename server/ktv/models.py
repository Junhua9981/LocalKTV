"""Domain models.

Only four external queue-item states exist (PREPARING / READY / PLAYING /
FAILED).  Everything else (downloading, encoding, matching...) is a progress
message, never a domain state.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class KaraokeMode(str, Enum):
    ORIGINAL = "ORIGINAL"
    INSTANT = "INSTANT"
    AI = "AI"


class ItemStatus(str, Enum):
    PREPARING = "PREPARING"
    READY = "READY"
    PLAYING = "PLAYING"
    FAILED = "FAILED"


class AIStatus(str, Enum):
    NONE = "NONE"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Track(BaseModel):
    id: str
    title: str
    artist: str = ""
    duration_ms: int = 0
    source: str = "youtube"
    source_id: str = ""
    lyrics_offset_ms: int = 0
    # YouTube-tab queueing: play the downloaded video as-is — never crawl
    # lyrics and never render them (set per request, not persisted).
    video_only: bool = False
    # Search merge: the same song (title + artist) on other platforms, as
    # source_ids — acquire tries them by id before searching anew.
    alternates: list[str] = Field(default_factory=list)
    # 專輯封面（平台圖片 CDN 的網址，搜尋時順便拿到）；遙控器經
    # /api/covers/src 讓伺服器代抓並快取，空字串 = 用首字方塊。
    cover: str = ""

    @classmethod
    def from_source(cls, source: str, source_id: str, **kwargs) -> "Track":
        return cls(id=f"{source}_{source_id}", source=source, source_id=source_id, **kwargs)


class QueueItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("qi"))
    track: Track
    requested_by: str = "guest"
    karaoke_mode: KaraokeMode = KaraokeMode.ORIGINAL
    guide_vocal: float = 0.0  # 0..1
    # 對唱保留原唱：這些聲部（歌詞 line.part，0 = 合唱）的句子放原唱人聲
    keep_parts: list[int] = Field(default_factory=list)
    status: ItemStatus = ItemStatus.PREPARING
    ai_status: AIStatus = AIStatus.NONE
    progress_message: str = ""
    progress_value: float | None = None  # 0..1 when known
    added_at: float = Field(default_factory=time.time)


class PlaybackState(BaseModel):
    queue_item_id: str | None = None
    track_id: str | None = None
    playing: bool = False
    position_ms: int = 0
    duration_ms: int = 0
    updated_at: float = 0.0


class SessionSnapshot(BaseModel):
    type: str = "session.snapshot"
    room: str = "main"  # room slug this socket belongs to (ktv/rooms.py)
    room_name: str = ""
    users: list[str] = Field(default_factory=list)
    queue: list[QueueItem] = Field(default_factory=list)
    current: QueueItem | None = None
    playback: PlaybackState = Field(default_factory=PlaybackState)
    ai_available: bool = False
    audio_source: str = "auto"  # host-pinned primary acquisition source
    # /host 主要音源 choices [{value, text}], `auto` first (providers/registry.py)
    audio_sources: list[dict[str, str]] = Field(default_factory=list)
    charts_available: bool = False  # the plug-in serves 熱門榜 (/api/charts)
    pitch_semitones: int = 0  # session-wide key shift, -3..3
    pitch_capable: bool = False  # display reported an AudioWorklet context
    bg_mode: str = "auto"  # display background: auto (MV if any, else 文字PV) | mv | pv
    pv_karaoke: bool = True  # keep the bottom karaoke lyrics while 文字PV shows
    ambience: str = "room"  # 包廂音場: clean | room | concert
    beat_lights: bool = True  # display glow follows the beat
    fx_mode: str = "auto"  # display effects: auto (省電 when the TV drops frames) | full | lite
    ja_assist: str = "both"  # 日文歌: both (假名＋空耳) | kana | sora | off
    score: bool = True  # 評分: the display listens to the PC's mic input
    score_bar: bool = True  # 音高條 on the display while scoring
    score_offset_ms: int = 150  # mic + speaker delay the score allows for (Bluetooth sets)
    mic_monitor: bool = False  # 麥克風外放: the display plays the PC's mic through the speakers
    mic_volume: int = 100  # 外放音量 %, MIC_VOLUME_RANGE
    mic_echo: int = 30  # 迴音 amount 0..100 (PC mic and phone mics)
    server_time: float = Field(default_factory=time.time)
