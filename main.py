"""抓「張震 股市盤中家教班」直播逐字稿並寫入 Google 試算表。由 GitHub Actions 執行。

用法：
  python main.py --mode check        # 檢查 Secrets、YouTube、試算表、Gemini 設定（不轉錄、不花生成額度）
  python main.py                     # daily：抓今天（台灣時間）那一集；試算表還沒資料時自動改做 init
  python main.py --mode init         # 抓頻道最新 5 場已結束直播中尚未寫入的
  python main.py --video-id XXXX     # 只處理指定影片（不受「每天失敗次數」限制）

結束代碼：0 = 正常（包含「今天還沒好，下次再查」），1 = 有錯誤（GitHub 會寄 email 通知）。
"""
import argparse
import logging
import os
import re
import sys
from datetime import datetime

import config
from config import ConfigError
from youtube_client import YouTubeClient

log = logging.getLogger("transcript")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["check", "daily", "init"], default="daily")
    parser.add_argument("--video-id", default="", help="只處理這支影片")
    parser.add_argument("--force", action="store_true", help="影片已在試算表中也重新轉錄（會新增一列）")
    args = parser.parse_args(argv)
    args.video_id = args.video_id.strip()
    if args.video_id and not VIDEO_ID_RE.match(args.video_id):
        parser.error(f"影片 ID 格式不正確：「{args.video_id}」（應為 11 碼，例如 r5YtdmBoMEA）")
    return args


def run_check() -> int:
    """逐項檢查設定並全部回報，而不是遇到第一個錯誤就停止。"""
    from sheet_store import RESULT_CHECK, SheetStore
    from transcriber import Transcriber

    problems = 0

    def ok(message: str) -> None:
        print(f"✅ {message}")

    def fail(message: str) -> None:
        nonlocal problems
        problems += 1
        print(f"❌ {message}")

    try:
        cfg = config.load_settings(strict=False)
    except ConfigError as exc:
        fail(f"Variables 設定錯誤：{exc}")
        return 1

    missing = config.missing_secrets()
    if missing:
        fail(f"缺少 GitHub Secrets：{', '.join(missing)}")
    else:
        ok("4 個 GitHub Secrets 都已設定")

    now = datetime.now(config.TAIPEI)
    if cfg.youtube_api_key:
        try:
            streams = YouTubeClient(cfg.youtube_api_key).recent_streams(cfg.channel_id, cfg.title_keyword)
            ok(f"YouTube Data API 正常，找到 {len(streams)} 場直播（現在台灣時間 {now:%Y-%m-%d %H:%M}）：")
            for s in streams:
                print(f"     {s.episode_date}  {s.video_id}  {s.duration_sec // 60:>3d} 分鐘  "
                      f"{s.status_text(now, cfg.ready_delay_minutes)}  {s.title}")
        except Exception as exc:
            fail(f"YouTube Data API 失敗：{exc}")

    if cfg.spreadsheet_id and cfg.service_account_json:
        try:
            store = SheetStore(cfg.spreadsheet_id, config.parse_service_account(cfg.service_account_json))
            count = len(store.existing_video_ids())
            store.log("check", None, RESULT_CHECK)  # 實際寫入一列，確認有編輯權限
            ok(f"Google 試算表可讀寫，目前已有 {count} 集逐字稿（「執行紀錄」已新增一列檢查紀錄）")
        except Exception as exc:
            fail(f"Google 試算表失敗：{exc}")

    if cfg.gemini_api_key:
        try:
            transcriber = Transcriber(cfg.gemini_api_key, cfg.gemini_model, cfg.segment_minutes,
                                      cfg.video_fps, cfg.vocabulary)
            ok(f"Gemini API 金鑰有效，可使用模型 {cfg.gemini_model}（{transcriber.check_model()}）")
        except Exception as exc:
            fail(f"Gemini API 失敗（模型 {cfg.gemini_model}）：{exc}")

    print("\n全部檢查通過，可以開始轉錄。" if problems == 0 else f"\n有 {problems} 項需要修正，請依上面的訊息調整後重新執行。")
    return 0 if problems == 0 else 1


def run_transcribe(args: argparse.Namespace) -> int:
    from sheet_store import RESULT_FAILED, RESULT_OK, RESULT_QUOTA, SheetStore
    from transcriber import QuotaExhausted, Transcriber

    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and len(config.missing_secrets()) == len(config.REQUIRED_SECRETS):
        # 剛推上 GitHub、還沒設定金鑰時，不要每 5 分鐘寄一次失敗通知
        log.warning("尚未設定任何 GitHub Secrets，排程先略過（設定方式見 README）")
        return 0

    cfg = config.load_settings()
    now = datetime.now(config.TAIPEI)
    youtube = YouTubeClient(cfg.youtube_api_key)
    store = SheetStore(cfg.spreadsheet_id, config.parse_service_account(cfg.service_account_json))
    done = store.existing_video_ids()
    mode = args.mode

    if args.video_id:
        mode = "manual"
        targets = youtube.get_streams([args.video_id])
        if not targets:
            log.error("找不到直播影片 %s（影片不存在、已被刪除，或不是直播影片）", args.video_id)
            return 1
    else:
        streams = youtube.recent_streams(cfg.channel_id, cfg.title_keyword)
        if mode == "daily" and not done:
            log.info("試算表還沒有任何逐字稿，先執行首次抓取（最新 %d 場）", cfg.init_video_count)
            mode = "init"
        if mode == "init":
            # 取最新 N 場「已結束」的直播（避開正在直播的那場），由舊到新處理，試算表才會依日期排列
            ended = [s for s in streams if s.live_status == "none"]
            targets = list(reversed(ended[: cfg.init_video_count]))
        else:
            targets = [s for s in streams if s.episode_date == now.date()]
            if not targets:
                log.info("YouTube 上還沒有今天（%s）的直播，下次排程再查", now.date())
                return 0

    if not args.force:
        for s in targets:
            if s.video_id in done:
                log.info("%s %s 已經在試算表中，略過", s.episode_date, s.video_id)
        targets = [s for s in targets if s.video_id not in done]

    transcriber = Transcriber(cfg.gemini_api_key, cfg.gemini_model, cfg.segment_minutes, cfg.video_fps, cfg.vocabulary)
    had_error = False
    for stream in targets:
        if not stream.is_ready(now, cfg.ready_delay_minutes):
            log.info("%s %s：%s，下次排程再查", stream.episode_date, stream.video_id,
                     stream.status_text(now, cfg.ready_delay_minutes))
            continue
        attempts = store.failed_attempts_today(stream.video_id)
        if mode != "manual" and attempts >= cfg.max_attempts_per_day:
            # 不回傳錯誤碼，避免之後每 5 分鐘都寄一次失敗通知信
            log.warning("%s %s 今天已失敗 %d 次，暫停重試（請查看「執行紀錄」工作表）",
                        stream.episode_date, stream.video_id, attempts)
            continue

        log.info("開始轉錄 %s（%d 分鐘）%s", stream.title, stream.duration_sec // 60, stream.url)
        try:
            text = transcriber.transcribe(stream.url, stream.duration_sec)
            store.append_transcript(stream, text, cfg.gemini_model)
        except QuotaExhausted as exc:
            log.error("%s", exc)
            store.log(mode, stream, RESULT_QUOTA, str(exc))
            had_error = True
            break  # 今天的配額用完，後面的影片也不用試了
        except Exception as exc:
            log.exception("轉錄 %s 失敗", stream.video_id)
            store.log(mode, stream, RESULT_FAILED, f"{type(exc).__name__}: {exc}")
            had_error = True
            continue
        chars = len("".join(text.split()))
        log.info("完成：%s，共 %d 字，已寫入試算表", stream.title, chars)
        store.log(mode, stream, RESULT_OK, f"{chars} 字")

    return 1 if had_error else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # SDK 每次輪詢都會印一行 HTTP 請求，只保留警告以上，log 比較好讀
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)
    if args.mode == "check" and not args.video_id:
        return run_check()
    try:
        return run_transcribe(args)
    except ConfigError as exc:
        log.error("設定錯誤：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
