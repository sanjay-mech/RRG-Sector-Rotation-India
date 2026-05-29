"""
Telegram bot for RRG sector rotation and stock alerts.
Run directly:  python telegram_bot.py
One-shot:      python telegram_bot.py --check-only
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Fix console encoding for Unicode on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

API_CONFIG = {
    "API_KEY": os.getenv("API_KEY", ""),
    "CLIENT_ID": os.getenv("CLIENT_ID", ""),
    "PASSWORD": os.getenv("PASSWORD", ""),
    "TOTP_TOKEN": os.getenv("TOTP_TOKEN", ""),
    "EXCHANGE": os.getenv("EXCHANGE", "NSE"),
}

from alert_engine import (
    RRGConfig, compute_sector_rrgs, compute_stock_rrg,
    detect_alerts, format_status_table, CATEGORY_MAP,
)
from bot_state import (
    subscribe_sectors, unsubscribe_sectors,
    subscribe_nfo, unsubscribe_nfo,
    subscribe_stocks, unsubscribe_stocks, unsubscribe_all,
    get_all_sector_subscribers, get_all_nfo_subscribers,
    get_all_stock_subscribers, get_user,
    get_nfo_list, set_nfo_list,
)
from loaders.AngelOneLoader import AngelOneLoader
from sectors import BENCHMARKS
from token_fetcher import get_token_from_symbol
from scrip_master_search import get_nfo_stocks

# Prevents first-cycle spam after every bot restart
_first_cycle_done = False


def _init_loader() -> Optional[AngelOneLoader]:
    if not all([API_CONFIG["API_KEY"], API_CONFIG["CLIENT_ID"], API_CONFIG["PASSWORD"], API_CONFIG["TOTP_TOKEN"]]):
        logger.error("Missing AngelOne API credentials in .env")
        return None
    try:
        return AngelOneLoader(
            config=API_CONFIG,
            tf=RRGConfig["timeframe"],
            period=RRGConfig["period"],
        )
    except Exception as e:
        logger.error(f"Failed to initialize AngelOne loader: {e}")
        return None


def run_rrg_check() -> Dict[str, dict]:
    loader = _init_loader()
    if not loader:
        return {}

    benchmark_symbol = BENCHMARKS.get("NIFTY 50", "Nifty 50")
    benchmark_token = get_token_from_symbol(benchmark_symbol)
    if not benchmark_token:
        logger.error("Benchmark token not found")
        loader.close()
        return {}

    try:
        benchmark_df = loader.get(benchmark_symbol, benchmark_token)
        if benchmark_df is None or benchmark_df.empty:
            logger.error("Failed to fetch benchmark data")
            loader.close()
            return {}
    except Exception as e:
        logger.error(f"Benchmark fetch failed: {e}")
        loader.close()
        return {}

    benchmark_closes = benchmark_df["Close"]
    if benchmark_closes.index.has_duplicates:
        benchmark_closes = benchmark_closes.loc[~benchmark_closes.index.duplicated()]
    if not benchmark_closes.index.is_monotonic_increasing:
        benchmark_closes = benchmark_closes.sort_index()

    sectors = compute_sector_rrgs(loader, benchmark_closes)
    loader.close()
    return sectors


STOCK_TITLES = {
    "Lagging":   "STOCK BREAKDOWN",
    "Weakening": "STOCK COOLING",
    "Improving": "STOCK RECOVERY",
    "Leading":   "STOCK LEADING",
}

def build_alert_message(grouped_alerts: Dict[str, List[dict]], timeframe: str = "weekly", scope: str = "sector") -> Optional[str]:
    if not grouped_alerts:
        return None

    now = datetime.now()
    time_str = now.strftime("%I:%M %p").lstrip("0")
    date_str = now.strftime("%b %d, %I:%M %p").lstrip("0")
    tf_display = timeframe.capitalize()

    total = sum(len(v) for v in grouped_alerts.values())

    lines = []
    lines.append(f"\U0001f4c5 *TF: {tf_display}* \u2014 {date_str}")
    lines.append(f"\U0001f916 *{tf_display} RRG Matrix* \u2014 {total} shift(s) detected")
    lines.append("")

    category_order = ["Lagging", "Weakening", "Improving", "Leading"]
    category_icons = {
        "Lagging":   "\U0001f4c9",
        "Weakening": "\u26a0\ufe0f",
        "Improving": "\U0001f4c8",
        "Leading":   "\U0001f3c6",
    }

    for cat in category_order:
        alerts = grouped_alerts.get(cat)
        if not alerts:
            continue
        if scope == "stock":
            title = STOCK_TITLES.get(cat, cat)
        else:
            title = CATEGORY_MAP[cat]["title"]
        icon = category_icons[cat]
        lines.append(f"{icon} *{title}*")
        for a in alerts:
            lines.append(f"{a['emoji']} *{a['name']}* \u2192 {a['quadrant']}")
            lines.append(f"  \U0001f504 `{a['path_str']}`")
        lines.append("")

    return "\n".join(lines)


async def send_alert_to_users(bot, chat_ids: List[int], message: str):
    for cid in chat_ids:
        try:
            await bot.send_message(chat_id=cid, text=message, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to send to {cid}: {e}")


async def run_alert_cycle(application=None):
    global _first_cycle_done
    logger.info("Starting RRG alert cycle")
    sectors = run_rrg_check()
    if not sectors:
        logger.warning("No sector data returned, skipping alert cycle")
        return

    grouped = detect_alerts(sectors)

    # Build stock alerts
    stock_subscribers = get_all_stock_subscribers()
    stock_alert_text = None
    if stock_subscribers:
        all_watched = list(set(s for stocks in stock_subscribers.values() for stocks in stocks))
        if all_watched:
            loader = _init_loader()
            if loader:
                benchmark_symbol = BENCHMARKS.get("NIFTY 50", "Nifty 50")
                benchmark_token = get_token_from_symbol(benchmark_symbol)
                if benchmark_token:
                    try:
                        benchmark_df = loader.get(benchmark_symbol, benchmark_token)
                        if benchmark_df is not None and not benchmark_df.empty:
                            benchmark_closes = benchmark_df["Close"]
                            if benchmark_closes.index.has_duplicates:
                                benchmark_closes = benchmark_closes.loc[~benchmark_closes.index.duplicated()]
                            if not benchmark_closes.index.is_monotonic_increasing:
                                benchmark_closes = benchmark_closes.sort_index()

                            stock_data = {}
                            for sym in all_watched:
                                result = compute_stock_rrg(loader, sym, benchmark_closes)
                                if result:
                                    stock_data[sym] = result
                            if stock_data:
                                stock_grouped = detect_alerts(stock_data)
                                stock_alert_text = build_alert_message(stock_grouped, timeframe=RRGConfig["timeframe"], scope="stock")
                    except Exception as e:
                        logger.error(f"Stock RRG check failed: {e}")
                loader.close()

    # Process all NFO stocks in one go
    nfo_subscribers = get_all_nfo_subscribers()
    nfo_alert_text = None
    if nfo_subscribers:
        nfo_symbols = get_nfo_list()
        if not nfo_symbols:
            nfo_symbols = [s["symbol"] for s in get_nfo_stocks()]
            if nfo_symbols:
                set_nfo_list(nfo_symbols)
                logger.info(f"Loaded {len(nfo_symbols)} NFO stocks")

        if nfo_symbols:
            logger.info(f"Processing all {len(nfo_symbols)} NFO stocks")
            loader = _init_loader()
            if loader:
                benchmark_symbol = BENCHMARKS.get("NIFTY 50", "Nifty 50")
                benchmark_token = get_token_from_symbol(benchmark_symbol)
                if benchmark_token:
                    try:
                        benchmark_df = loader.get(benchmark_symbol, benchmark_token)
                        if benchmark_df is not None and not benchmark_df.empty:
                            benchmark_closes = benchmark_df["Close"]
                            if benchmark_closes.index.has_duplicates:
                                benchmark_closes = benchmark_closes.loc[~benchmark_closes.index.duplicated()]
                            if not benchmark_closes.index.is_monotonic_increasing:
                                benchmark_closes = benchmark_closes.sort_index()

                            nfo_data = {}
                            for sym in nfo_symbols:
                                result = compute_stock_rrg(loader, sym, benchmark_closes)
                                if result:
                                    nfo_data[sym] = result
                            logger.info(f"Computed RRG for {len(nfo_data)}/{len(nfo_symbols)} NFO stocks")
                            if nfo_data:
                                nfo_grouped = detect_alerts(nfo_data)
                                nfo_alert_text = build_alert_message(nfo_grouped, timeframe=RRGConfig["timeframe"], scope="stock")
                    except Exception as e:
                        logger.error(f"NFO RRG check failed: {e}")
                loader.close()

    # First cycle after startup: establish baseline silently, don't send alerts
    if not _first_cycle_done:
        _first_cycle_done = True
        logger.info("First cycle done — establishing baseline, skipping alerts")
        return

    if not application:
        return

    bot = application.bot
    sector_subscribers = get_all_sector_subscribers()

    # Send sector alerts
    msg = build_alert_message(grouped, timeframe=RRGConfig["timeframe"])
    if msg:
        logger.info(f"Sending sector RRG alert to {len(sector_subscribers)} subscribers")
        await send_alert_to_users(bot, sector_subscribers, msg)
    else:
        logger.info("No sector alerts triggered this cycle")

    # Send stock alerts to relevant subscribers
    if stock_alert_text:
        for cid, stocks in stock_subscribers.items():
            # Check if any of the stock alerts match this user's subscriptions
            user_alert_lines = []
            for line in stock_alert_text.split("\n"):
                for s in stocks:
                    if s in line:
                        user_alert_lines.append(line)
                        break
            if user_alert_lines:
                user_msg = f"\U0001f4ca *Your Stock RRG Alerts*\n\n" + "\n".join(user_alert_lines)
                try:
                    await bot.send_message(chat_id=cid, text=user_msg, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"Failed to send stock alerts to {cid}: {e}")

    # Send NFO alerts to NFO subscribers
    if nfo_alert_text:
        for cid in nfo_subscribers:
            try:
                await bot.send_message(chat_id=cid, text=nfo_alert_text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Failed to send NFO alerts to {cid}: {e}")


def run_check_only():
    logger.info("Running one-shot RRG check")
    sectors = run_rrg_check()
    if not sectors:
        print("No sector data retrieved.")
        return

    print("\n=== SECTOR RRG STATUS ===")
    print(format_status_table(sectors))
    print()

    grouped = detect_alerts(sectors)
    msg = build_alert_message(grouped, timeframe=RRGConfig["timeframe"])
    if msg:
        print(msg)
    else:
        print("No alerts triggered.")
    print()


def get_public_ip() -> str:
    try:
        import requests
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception as e:
        return f"unknown ({e})"


def main():
    parser = argparse.ArgumentParser(description="RRG Telegram Bot")
    parser.add_argument("--check-only", action="store_true", help="One-shot RRG check to console, no bot")
    args = parser.parse_args()

    if args.check_only:
        run_check_only()
        return

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env. Get one from @BotFather.")
        sys.exit(1)

    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await update.message.reply_text(
            f"\U0001f44b *Hey {user.first_name}! Welcome aboard!* \U0001f680\n\n"
            "\U0001f4ca I track the *RRG (Relative Rotation Graph)* for Nifty sectors "
            "and 211 NFO stocks \u2014 so you always know where the smart money is rotating.\n\n"
            "\U0001f535 *SUBSCRIBE & GET ALERTS*\n"
            "\U0001f7e2 `/sub_sectors` \u2014 Sector rotation alerts\n"
            "\U0001f7e2 `/sub_nfo` \u2014 All 211 NFO stock alerts\n"
            "\U0001f7e2 `/sub_stocks RELIANCE-EQ,TCS-EQ` \u2014 Custom stock alerts\n\n"
            "\u26d4 *UNSUBSCRIBE*\n"
            "\U0001f534 `/unsub_sectors` \u2014 Stop sector alerts\n"
            "\U0001f534 `/unsub_nfo` \u2014 Stop NFO alerts\n"
            "\U0001f534 `/unsub_stocks` \u2014 Stop stock alerts\n"
            "\U0001f534 `/unsub_all` \u2014 Unsubscribe from everything\n\n"
            "\U0001f50d *CHECK ON DEMAND*\n"
            "\U0001f535 `/status` \u2014 Current sector RRG snapshot\n"
            "\U0001f535 `/check_stock RELIANCE-EQ` \u2014 Single stock RRG\n"
            "\U0001f535 `/check_nfo` \u2014 Trigger NFO batch scan\n"
            "\U0001f535 `/alert_now` \u2014 Instant sector shift report\n\n"
            "\u23f0 *Note:* All 211 NFO stocks process in one cycle (~3\u20134 min). "
            "You'll only be notified when something *changes quadrant*. Sit back and let the bot watch the markets! \U0001f60e"
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await start(update, context)

    async def sub_sectors(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        subscribe_sectors(cid)
        await update.message.reply_text(
            "Subscribed to sector rotation alerts.\n"
            "You'll be notified on quadrant changes and significant moves."
        )

    async def unsub_sectors(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        unsubscribe_sectors(cid)
        await update.message.reply_text("Unsubscribed from sector alerts.")

    async def sub_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        if not context.args:
            await update.message.reply_text(
                "Usage: /sub_stocks RELIANCE-EQ,TCS-EQ,INFY-EQ\n"
                "Use comma-separated symbols with -EQ suffix."
            )
            return
        symbols = [s.strip().upper() for s in " ".join(context.args).split(",") if s.strip()]
        if not symbols:
            await update.message.reply_text("No valid symbols provided.")
            return
        added = subscribe_stocks(cid, symbols)
        if added:
            await update.message.reply_text(f"Subscribed to stock alerts for: {', '.join(added)}")
        else:
            await update.message.reply_text("Those symbols were already subscribed.")

    async def unsub_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        if not context.args:
            removed = unsubscribe_stocks(cid)
            if removed:
                await update.message.reply_text(f"Unsubscribed from all stocks: {', '.join(removed)}")
            else:
                await update.message.reply_text("You have no stock subscriptions.")
            return
        symbols = [s.strip().upper() for s in " ".join(context.args).split(",") if s.strip()]
        removed = unsubscribe_stocks(cid, symbols)
        if removed:
            await update.message.reply_text(f"Unsubscribed: {', '.join(removed)}")
        else:
            await update.message.reply_text("None of those symbols were in your subscriptions.")

    async def sub_nfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        subscribe_nfo(cid)
        count = len(get_nfo_list())
        if not count:
            symbols = [s["symbol"] for s in get_nfo_stocks()]
            if symbols:
                set_nfo_list(symbols)
                count = len(symbols)
        await update.message.reply_text(
            f"Subscribed to NFO stock alerts ({count} stocks).\n"
            f"All stocks are processed together every cycle (may take a few minutes)."
        )

    async def unsub_nfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        unsubscribe_nfo(cid)
        await update.message.reply_text("Unsubscribed from NFO stock alerts.")

    async def check_nfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        msg = await update.message.reply_text("Fetching NFO stock list...")
        symbols = get_nfo_list()
        if not symbols:
            symbols = [s["symbol"] for s in get_nfo_stocks()]
            if symbols:
                set_nfo_list(symbols)
        if not symbols:
            await msg.edit_text("No NFO stocks found.")
            return
        await msg.edit_text(f"Processing all {len(symbols)} NFO stocks (may take a few minutes)...")

        loader = _init_loader()
        if not loader:
            await msg.edit_text("Failed to connect to AngelOne API.")
            return
        benchmark_symbol = BENCHMARKS.get("NIFTY 50", "Nifty 50")
        benchmark_token = get_token_from_symbol(benchmark_symbol)
        if not benchmark_token:
            await msg.edit_text("Benchmark token not found.")
            loader.close()
            return
        try:
            benchmark_df = loader.get(benchmark_symbol, benchmark_token)
            if benchmark_df is None or benchmark_df.empty:
                await msg.edit_text("Failed to fetch benchmark data.")
                loader.close()
                return
            benchmark_closes = benchmark_df["Close"]
            if benchmark_closes.index.has_duplicates:
                benchmark_closes = benchmark_closes.loc[~benchmark_closes.index.duplicated()]
            if not benchmark_closes.index.is_monotonic_increasing:
                benchmark_closes = benchmark_closes.sort_index()

            nfo_data = {}
            for sym in symbols:
                result = compute_stock_rrg(loader, sym, benchmark_closes)
                if result:
                    nfo_data[sym] = result
            loader.close()

            if nfo_data:
                nfo_grouped = detect_alerts(nfo_data)
                alert_text = build_alert_message(nfo_grouped, timeframe=RRGConfig["timeframe"], scope="stock")
                if alert_text:
                    await msg.edit_text(alert_text, parse_mode="Markdown")
                else:
                    await msg.edit_text(f"Processed all {len(symbols)} NFO stocks. No alerts triggered.")
            else:
                await msg.edit_text("No RRG data could be computed.")
        except Exception as e:
            await msg.edit_text(f"Error: {e}")
            loader.close()

    async def unsub_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_user.id
        unsubscribe_all(cid)
        await update.message.reply_text("Unsubscribed from all alerts.")

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("Computing sector RRG values...")
        sectors = run_rrg_check()
        if not sectors:
            await msg.edit_text("Failed to fetch sector RRG data. Check logs.")
            return
        table = format_status_table(sectors)
        await msg.edit_text(f"Sector RRG Status\n\n{table}", parse_mode="Markdown")

    async def alert_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("Scanning all sectors for shifts...")
        sectors = run_rrg_check()
        if not sectors:
            await msg.edit_text("Failed to fetch sector data.")
            return
        grouped = detect_alerts(sectors)
        alert_text = build_alert_message(grouped, timeframe=RRGConfig["timeframe"])
        if alert_text:
            await msg.edit_text(alert_text, parse_mode="Markdown")
        else:
            await msg.edit_text("No sector shifts detected right now. All quadrants stable.")

    async def check_sectors(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await status(update, context)

    async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Usage: /check_stock RELIANCE-EQ")
            return
        symbol = context.args[0].strip().upper()
        if not symbol.endswith("-EQ"):
            symbol += "-EQ"
        msg = await update.message.reply_text(f"Checking RRG for {symbol}...")

        loader = _init_loader()
        if not loader:
            await msg.edit_text("Failed to connect to AngelOne API.")
            return

        benchmark_symbol = BENCHMARKS.get("NIFTY 50", "Nifty 50")
        benchmark_token = get_token_from_symbol(benchmark_symbol)
        if not benchmark_token:
            await msg.edit_text("Benchmark token not found.")
            loader.close()
            return

        try:
            benchmark_df = loader.get(benchmark_symbol, benchmark_token)
            if benchmark_df is None or benchmark_df.empty:
                await msg.edit_text("Failed to fetch benchmark data.")
                loader.close()
                return
            benchmark_closes = benchmark_df["Close"]
            if benchmark_closes.index.has_duplicates:
                benchmark_closes = benchmark_closes.loc[~benchmark_closes.index.duplicated()]
            if not benchmark_closes.index.is_monotonic_increasing:
                benchmark_closes = benchmark_closes.sort_index()

            result = compute_stock_rrg(loader, symbol, benchmark_closes)
        except Exception as e:
            await msg.edit_text(f"Error: {e}")
            loader.close()
            return
        finally:
            loader.close()

        if not result:
            await msg.edit_text(f"No RRG data for {symbol}. Check symbol validity.")
            return

        quad_emoji = {"Leading": "\U0001f7e2", "Weakening": "\U0001f7e1", "Improving": "\U0001f535", "Lagging": "\U0001f534"}
        emoji = quad_emoji.get(result["quadrant"], "")
        await msg.edit_text(
            f"{symbol}\n\n"
            f"RS-Ratio:   {result['rs_ratio']:.1f}\n"
            f"Momentum: {result['momentum']:.1f}\n"
            f"Quadrant:  {emoji} {result['quadrant']}"
        )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("sub_sectors", sub_sectors))
    app.add_handler(CommandHandler("unsub_sectors", unsub_sectors))
    app.add_handler(CommandHandler("sub_stocks", sub_stocks))
    app.add_handler(CommandHandler("unsub_stocks", unsub_stocks))
    app.add_handler(CommandHandler("sub_nfo", sub_nfo))
    app.add_handler(CommandHandler("unsub_nfo", unsub_nfo))
    app.add_handler(CommandHandler("check_nfo", check_nfo))
    app.add_handler(CommandHandler("unsub_all", unsub_all))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("alert_now", alert_now))
    app.add_handler(CommandHandler("check_sectors", check_sectors))
    app.add_handler(CommandHandler("check_stock", check_stock))

    async def alert_callback(context: ContextTypes.DEFAULT_TYPE):
        await run_alert_cycle(application=context.application)

    job_queue = app.job_queue
    job_queue.run_repeating(alert_callback, interval=3600, first=10)

    public_ip = get_public_ip()
    logger.info(f"RRG Telegram Bot started. Public IP: {public_ip}")
    print(f"RRG Telegram Bot is running (IP: {public_ip}). Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
