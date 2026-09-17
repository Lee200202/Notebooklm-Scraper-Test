"""用 YouTube Data API v3 找出頻道的直播場次。

不直接爬 youtube.com 網頁：GitHub Actions 等雲端 IP 很容易被 YouTube 擋（要求登入驗證），
官方 API 穩定又免費（每天 10,000 單位，本程式每次檢查只用 3 單位）。
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

from config import TAIPEI

API_BASE = "https://www.googleapis.com/youtube/v3"

# 標題格式：2026/09/17(四)張震  股市盤中家教班
TITLE_DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


@dataclass
class Stream:
    video_id: str
    title: str
    episode_date: date
    duration_sec: int
    live_status: str  # live / upcoming / none
    privacy: str  # public / unlisted / private
    started_at: datetime | None
    ended_at: datetime | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def is_ready(self, now: datetime, delay_minutes: int) -> bool:
        """直播已結束、回放已處理完成且公開，Gemini 才讀得到。"""
        return (
            self.live_status == "none"
            and self.ended_at is not None
            and now - self.ended_at >= timedelta(minutes=delay_minutes)
            and self.privacy == "public"
            and self.duration_sec > 0
        )

    def status_text(self, now: datetime, delay_minutes: int) -> str:
        if self.live_status == "upcoming":
            return "尚未開播"
        if self.live_status == "live":
            return "直播中"
        if self.privacy != "public":
            return f"非公開影片（{self.privacy}）"
        if self.ended_at is None or self.duration_sec == 0:
            return "直播已結束，YouTube 回放處理中"
        if not self.is_ready(now, delay_minutes):
            return f"直播剛結束，等待 {delay_minutes} 分鐘緩衝"
        return "可轉錄"


def parse_duration(value: str) -> int:
    match = DURATION_RE.match(value or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TAIPEI)


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _get(self, endpoint: str, **params) -> dict:
        params["key"] = self.api_key
        resp = self.session.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"YouTube API {endpoint} 失敗（HTTP {resp.status_code}）：{resp.text[:500]}")
        return resp.json()

    def recent_streams(self, channel_id: str, title_keyword: str, limit: int = 15) -> list[Stream]:
        """頻道最近的直播場次，新到舊排序。"""
        # channels.list 取得「所有上傳影片」播放清單（1 單位）
        data = self._get("channels", part="contentDetails", id=channel_id)
        if not data.get("items"):
            raise RuntimeError(f"找不到頻道 {channel_id}")
        uploads = data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        # playlistItems.list 取最新影片 ID（1 單位）
        data = self._get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=limit)
        ids = [item["contentDetails"]["videoId"] for item in data.get("items", [])]

        streams = [s for s in self.get_streams(ids) if title_keyword in s.title]
        streams.sort(key=lambda s: (s.episode_date, s.started_at or datetime.min.replace(tzinfo=TAIPEI)), reverse=True)
        return streams

    def get_streams(self, video_ids: list[str]) -> list[Stream]:
        """videos.list 取影片詳細資料（1 單位），只保留直播影片。"""
        if not video_ids:
            return []
        data = self._get(
            "videos",
            part="snippet,contentDetails,liveStreamingDetails,status",
            id=",".join(video_ids[:50]),
            maxResults=50,
        )
        streams = []
        for video in data.get("items", []):
            live = video.get("liveStreamingDetails")
            if not live:  # 一般上傳影片，不是直播
                continue
            snippet = video["snippet"]
            started_at = _parse_time(live.get("actualStartTime"))
            streams.append(
                Stream(
                    video_id=video["id"],
                    title=snippet["title"],
                    episode_date=_episode_date(snippet, live, started_at),
                    duration_sec=parse_duration(video["contentDetails"].get("duration", "")),
                    live_status=snippet.get("liveBroadcastContent", "none"),
                    privacy=video["status"].get("privacyStatus", ""),
                    started_at=started_at,
                    ended_at=_parse_time(live.get("actualEndTime")),
                )
            )
        return streams


def _episode_date(snippet: dict, live: dict, started_at: datetime | None) -> date:
    """優先用標題上的日期，其次用開播時間（台灣時間）。"""
    match = TITLE_DATE_RE.search(snippet["title"])
    if match:
        try:
            return date(*(int(x) for x in match.groups()))
        except ValueError:
            pass
    when = started_at or _parse_time(live.get("scheduledStartTime")) or _parse_time(snippet["publishedAt"])
    return when.date()
