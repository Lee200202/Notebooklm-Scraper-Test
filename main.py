"""抓「張震 股市盤中家教班」直播逐字稿並寫入 Google 試算表。由 GitHub Actions 執行。

用法：
  python main.py --mode check        # 檢查 Secrets、YouTube、試算表、Gemini 設定（不轉錄、不花生成額度）
  python main.py                     # daily：抓今天（台灣時間）那一集；試算表還沒資料時自動改做 init
  python main.py --mode init         # 抓頻道最新 5 場已結束直播中尚未寫入的
  python main.py --video-id XXXX     # 只處理指定影片
  python main.py --mode daily --watch  # 排程用：等到 11:20，每 3 分鐘檢查一次，直到今天這集轉錄完成或 14:00
  python main.py --mode report       # 排程用（17:05）：今天這集若仍未完成，寄一封失敗報告

通知信（有設定 MAIL_USERNAME／MAIL_APP_PASSWORD 時）：
  - 轉錄成功：寫入試算表後立即寄出
  - 轉錄失敗：排程一天內會自動重試，不逐次寄信；由 17:05 的 report 統一寄一封

「同一集每天最多失敗幾次」的限制只套用在排程觸發的執行；手動執行不受限制。

結束代碼：0 = 正常（包含「今天還沒好，下次再查」），1 = 有錯誤（GitHub 會寄 email 通知）。
排程執行時「轉錄失敗／額度不足」不回傳 1（避免 GitHub 立即寄信），改由 17:05 的報告通知；
金鑰、試算表、YouTube API 等系統性錯誤仍回傳 1。
"""
import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import notifier
from config import ConfigError
from youtube_client import YouTubeClient

log = logging.getLogger("transcript")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def now_taipei() -> datetime:
    return datetime.now(config.TAIPEI)


def sleep(seconds: float) -> None:  # 包一層，測試時可以替換成假時鐘
    time.sleep(seconds)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["check", "daily", "init", "report"], default="daily")
    parser.add_argument("--video-id", default="", help="只處理這支影片")
    parser.add_argument("--force", action="store_true", help="影片已在試算表中也重新轉錄（會新增一列）")
    parser.add_argument("--watch", action="store_true",
                        help="排程用：等到 POLL_START，每 POLL_INTERVAL_MINUTES 分鐘檢查，直到今天這集完成或 POLL_END")
    args = parser.parse_args(argv)
    args.video_id = args.video_id.strip()
    if args.video_id and not VIDEO_ID_RE.match(args.video_id):
        parser.error(f"影片 ID 格式不正確：「{args.video_id}」（應為 11 碼，例如 r5YtdmBoMEA）")
    if args.watch and (args.mode != "daily" or args.video_id):
        parser.error("--watch 只能搭配 --mode daily 使用")
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
        gemini_count = len(cfg.gemini_api_keys)
        if gemini_count > 1:
            ok(f"4 個 GitHub Secrets 都已設定（包含 {gemini_count} 組 Gemini API 金鑰）")
        else:
            ok("4 個 GitHub Secrets 都已設定")

    now = datetime.now(config.TAIPEI)
    youtube_keys = cfg.youtube_api_keys or ((cfg.youtube_api_key,) if cfg.youtube_api_key else ())
    if youtube_keys:
        try:
            yt_client = YouTubeClient(youtube_keys)
            streams = yt_client.recent_streams(cfg.channel_id, cfg.title_keyword)
            yt_info = f"（共 {len(yt_client.api_keys)} 組金鑰）" if len(yt_client.api_keys) > 1 else ""
            ok(f"YouTube Data API 正常{yt_info}，找到 {len(streams)} 場直播（現在台灣時間 {now:%Y-%m-%d %H:%M}）：")
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

    gemini_keys = cfg.gemini_api_keys or ((cfg.gemini_api_key,) if cfg.gemini_api_key else ())
    if gemini_keys:
        try:
            transcriber = Transcriber(gemini_keys, cfg.gemini_model, cfg.segment_minutes,
                                      cfg.video_fps, cfg.vocabulary, cfg.gemini_fallback_model)
            names = transcriber.check_models()
            total_keys = len(transcriber.api_keys)
            key_info = f"（共 {total_keys} 組金鑰已啟用備援輪替）" if total_keys > 1 else ""
            ok(f"Gemini API 金鑰有效{key_info}，主要模型 {names[0]}" + (f"，備援模型 {names[1]}" if len(names) > 1 else "，未設定備援模型"))
            if total_keys > 1:
                for i, client in enumerate(transcriber.clients, 1):
                    try:
                        for m in transcriber.models:
                            client.models.get(model=m)
                        print(f"     金鑰 #{i} 驗證正常")
                    except Exception as k_exc:
                        fail(f"金鑰 #{i} 驗證失敗：{k_exc}")
        except Exception as exc:
            models = " / ".join(m for m in (cfg.gemini_model, cfg.gemini_fallback_model) if m)
            fail(f"Gemini API 失敗（模型 {models}）：{exc}")

    if cfg.mail_username and cfg.mail_app_password:
        try:
            notifier.Mailer.from_settings(cfg).send(
                "✅ 設定檢查：寄信測試",
                f"這是「張震逐字稿」的寄信測試（{now:%Y-%m-%d %H:%M}）。\n收到這封信代表寄信設定正確：\n"
                "  • 轉錄成功：寫入試算表後立即通知\n  • 轉錄失敗：每天 17:05 若今天這集仍未完成，寄一封失敗報告",
            )
            ok(f"Gmail 寄信正常，已寄出測試信給 {len(cfg.mail_to)} 位收件者，請到信箱確認")
        except Exception as exc:
            fail(f"寄信失敗：{exc}（MAIL_USERNAME 要填完整 Gmail 地址，MAIL_APP_PASSWORD 要填 16 碼應用程式密碼）")
    elif cfg.mail_username or cfg.mail_app_password:
        fail("MAIL_USERNAME 和 MAIL_APP_PASSWORD 要一起設定")
    else:
        print("ℹ️ 未設定寄信（選填）：不會寄送成功／失敗通知")

    print("\n全部檢查通過，可以開始轉錄。" if problems == 0 else f"\n有 {problems} 項需要修正，請依上面的訊息調整後重新執行。")
    return 0 if problems == 0 else 1


@dataclass
class Context:
    cfg: config.Settings
    youtube: YouTubeClient
    store: object  # SheetStore
    transcriber: object  # Transcriber
    scheduled: bool
    mailer: "notifier.Mailer | None"


def run_transcribe(args: argparse.Namespace) -> int:
    from sheet_store import SheetStore
    from transcriber import Transcriber

    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and len(config.missing_secrets()) == len(config.REQUIRED_SECRETS):
        # 剛推上 GitHub、還沒設定金鑰時，不要每次排程都寄失敗通知
        log.warning("尚未設定任何 GitHub Secrets，排程先略過（設定方式見 README）")
        return 0

    cfg = config.load_settings()
    ctx = Context(
        cfg=cfg,
        youtube=YouTubeClient(cfg.youtube_api_keys or cfg.youtube_api_key),
        store=SheetStore(cfg.spreadsheet_id, config.parse_service_account(cfg.service_account_json)),
        transcriber=Transcriber(cfg.gemini_api_keys or cfg.gemini_api_key, cfg.gemini_model, cfg.segment_minutes, cfg.video_fps,
                                cfg.vocabulary, cfg.gemini_fallback_model),
        scheduled=os.getenv("GITHUB_EVENT_NAME") == "schedule",
        mailer=notifier.Mailer.from_settings(cfg),
    )
    if args.mode == "report":
        return run_report(ctx)
    if args.watch:
        return run_watch(ctx, args)
    return process_once(ctx, args)[0]


def run_watch(ctx: Context, args: argparse.Namespace) -> int:
    """在同一次執行中反覆檢查：等到 POLL_START，每 POLL_INTERVAL_MINUTES 分鐘檢查一次，
    直到今天這集轉錄完成（或失敗、或 YouTube 上根本沒有今天的直播），最晚到 POLL_END。

    GitHub Actions 排程最短間隔是 5 分鐘，而且可能延遲，所以排程提早幾分鐘啟動，由這裡精準控制時間。
    """
    cfg = ctx.cfg
    now = now_taipei()
    start = datetime.combine(now.date(), cfg.poll_start, tzinfo=config.TAIPEI)
    end = datetime.combine(now.date(), cfg.poll_end, tzinfo=config.TAIPEI)
    interval = timedelta(minutes=cfg.poll_interval_minutes)
    if now < start:
        log.info("等到 %s 開始檢查（之後每 %d 分鐘一次，最晚到 %s）",
                 f"{start:%H:%M}", cfg.poll_interval_minutes, f"{end:%H:%M}")
        sleep((start - now).total_seconds())

    checks = 0
    while True:
        checks += 1
        code, waiting = process_once(ctx, args)
        if not waiting:
            return code
        next_check = now_taipei() + interval
        if next_check > end:
            log.info("已接近 %s，停止等待（共檢查 %d 次），之後的排程會再檢查", f"{end:%H:%M}", checks)
            return code
        log.info("第 %d 次檢查：今天的直播還沒準備好，%s 再檢查", checks, f"{next_check:%H:%M}")
        sleep(interval.total_seconds())


def process_once(ctx: Context, args: argparse.Namespace) -> tuple[int, bool]:
    """檢查並轉錄一次。回傳 (結束代碼, 是否在等待今天的直播準備好)。"""
    from sheet_store import RESULT_FAILED, RESULT_OK, RESULT_QUOTA
    from transcriber import QuotaExhausted

    cfg, youtube, store, transcriber = ctx.cfg, ctx.youtube, ctx.store, ctx.transcriber
    now = now_taipei()
    done = store.existing_video_ids()
    mode = args.mode

    if args.video_id:
        mode = "manual"
        targets = youtube.get_streams([args.video_id])
        if not targets:
            log.error("找不到直播影片 %s（影片不存在、已被刪除，或不是直播影片）", args.video_id)
            return 1, False
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
                log.info("YouTube 上沒有今天（%s）的直播（可能休市），之後的排程會再查", now.date())
                return 0, False

    if not args.force:
        for s in targets:
            if s.video_id in done:
                log.info("%s %s 已經在試算表中，略過", s.episode_date, s.video_id)
        targets = [s for s in targets if s.video_id not in done]

    had_error = waiting = False
    written: list[dict] = []
    failures: list[tuple[object, str]] = []
    for stream in targets:
        if not stream.is_ready(now, cfg.ready_delay_minutes):
            log.info("%s %s：%s", stream.episode_date, stream.video_id, stream.status_text(now, cfg.ready_delay_minutes))
            waiting = waiting or mode == "daily"
            continue
        attempts = store.failed_attempts_today(stream.video_id)
        # 次數限制只用在「排程」：避免自動重試浪費額度、寄一堆失敗通知信。
        # 在 Actions 頁面手動按「Run workflow」是刻意要重試，不受限制。
        if ctx.scheduled and attempts >= cfg.max_attempts_per_day:
            # 不回傳錯誤碼，避免之後每次排程都寄一次失敗通知信
            log.warning("%s %s 今天已失敗 %d 次，暫停重試（請查看「執行紀錄」工作表）",
                        stream.episode_date, stream.video_id, attempts)
            continue

        log.info("開始轉錄 %s（%d 分鐘）%s", stream.title, stream.duration_sec // 60, stream.url)
        try:
            text = transcriber.transcribe(stream.url, stream.duration_sec)
            models = " + ".join(transcriber.models_used)
            store.append_transcript(stream, text, models)
        except QuotaExhausted as exc:
            if len(transcriber.api_keys) > 1:
                message = f"所有 API 金鑰（共 {len(transcriber.api_keys)} 組）與模型（{' / '.join(transcriber.models)}）額度都用完：{exc}"
            else:
                message = f"所有模型（{' / '.join(transcriber.models)}）額度都用完：{exc}"
            log.error("%s", message)
            store.log(mode, stream, RESULT_QUOTA, message)
            failures.append((stream, message))
            had_error = True
            break  # 今天的配額用完，後面的影片也不用試了
        except Exception as exc:
            log.exception("轉錄 %s 失敗", stream.video_id)
            message = f"{type(exc).__name__}: {exc}"
            store.log(mode, stream, RESULT_FAILED, message)
            failures.append((stream, message))
            had_error = True
            continue
        chars = len("".join(text.split()))
        key_tag = f"，金鑰 #{'/'.join(map(str, transcriber.keys_used))}" if getattr(transcriber, "keys_used", None) else ""
        log.info("完成：%s，共 %d 字（模型 %s%s），已寫入試算表", stream.title, chars, models, key_tag)
        store.log(mode, stream, RESULT_OK, f"{chars} 字，模型 {models}")
        written.append({"stream": stream, "title": stream.title, "url": stream.url, "chars": chars,
                        "models": models, "preview": text[:400]})

    if written:
        notify_success(ctx, written, failures)

    code = 1 if had_error else 0
    if had_error and ctx.scheduled:
        # 排程會在今天稍後自動重試，失敗時不讓 GitHub 立即寄信；若到 17:05 仍未完成，由 report 寄失敗報告。
        # 仍在 Actions 頁面加上錯誤標註，方便查看
        for stream, message in failures:
            print(f"::error title=轉錄未完成 {stream.episode_date}::{' '.join(message.split())[:500]}", flush=True)
        log.warning("本次排程有集數未完成（已記錄到「執行紀錄」）；之後的排程會再試，17:05 仍未完成會寄失敗報告")
        code = 0
    return code, waiting and not had_error


def notify_success(ctx: Context, written: list[dict], failures: list[tuple[object, str]]) -> None:
    from sheet_store import RESULT_MAIL_ERROR, RESULT_MAIL_SUCCESS

    if not ctx.mailer:
        return
    subject, body = notifier.success_email(
        written, [(stream.title, message) for stream, message in failures],
        notifier.sheet_url(ctx.cfg.spreadsheet_id), notifier.actions_url(),
    )
    try:
        ctx.mailer.send(subject, body)
    except Exception as exc:
        # 寄信失敗不影響逐字稿（已寫入）；17:05 的 report 會替今天這集補寄
        log.warning("寄送成功通知失敗：%s（17:05 的報告會替今天這集補寄）", exc)
        for item in written:
            ctx.store.log("mail", item["stream"], RESULT_MAIL_ERROR, f"{type(exc).__name__}: {exc}")
        return
    for item in written:
        ctx.store.log("mail", item["stream"], RESULT_MAIL_SUCCESS)


def run_report(ctx: Context) -> int:
    """每天 17:05（排程）：今天的直播若仍未寫入試算表，寄一封失敗報告；
    若已完成但成功通知當時沒寄出（例如寄信失敗），補寄成功通知。每集每天最多各寄一次。"""
    from sheet_store import RESULT_MAIL_FAILURE, RESULT_MAIL_SUCCESS

    cfg, store = ctx.cfg, ctx.store
    if not ctx.mailer:
        log.warning("尚未設定寄信（MAIL_USERNAME／MAIL_APP_PASSWORD），無法寄送每日報告")
        return 0
    now = now_taipei()
    today = now.date()
    streams = [s for s in ctx.youtube.recent_streams(cfg.channel_id, cfg.title_keyword) if s.episode_date == today]
    if not streams:
        log.info("今天（%s）YouTube 上沒有直播（可能休市），不寄報告", today)
        return 0

    done = store.existing_video_ids()
    rows = store.log_rows()
    sheet, actions = notifier.sheet_url(cfg.spreadsheet_id), notifier.actions_url()
    for stream in streams:
        mine = [row for row in rows if row[3] == stream.video_id]
        if stream.video_id in done:
            if any(row[4] == RESULT_MAIL_SUCCESS for row in mine):
                log.info("%s 已完成，成功通知已寄過", stream.title)
                continue
            row = store.transcript_row(stream.video_id) or []
            item = {"title": stream.title, "url": stream.url,
                    "chars": int(row[8]) if len(row) > 8 and row[8].isdigit() else 0,
                    "models": row[9] if len(row) > 9 else "", "preview": row[11][:400] if len(row) > 11 else ""}
            subject, body = notifier.success_email([item], [], sheet, actions)
            result = RESULT_MAIL_SUCCESS
        else:
            if any(row[4] == RESULT_MAIL_FAILURE and row[0].startswith(today.isoformat()) for row in mine):
                log.info("%s 未完成，今天已寄過失敗報告", stream.title)
                continue
            today_rows = [row for row in mine if row[0].startswith(today.isoformat()) and row[1] != "mail"]
            subject, body = notifier.failure_email(
                stream.title, stream.url, stream.video_id, stream.status_text(now, cfg.ready_delay_minutes),
                f"{now:%Y-%m-%d %H:%M}", today_rows, sheet, actions,
            )
            result = RESULT_MAIL_FAILURE
        ctx.mailer.send(subject, body)  # 報告寄不出去就讓這次執行失敗，GitHub 會寄信提醒
        store.log("mail", stream, result)
        log.info("%s：%s", result, stream.title)
    return 0


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # GitHub 主機的時區是 UTC，log 時間改用台灣時間顯示才不會混淆
    for handler in logging.getLogger().handlers:
        if handler.formatter:
            handler.formatter.converter = lambda ts: datetime.fromtimestamp(ts, config.TAIPEI).timetuple()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
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
