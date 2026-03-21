# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# These are the libraries the bot needs to run.
# - os, logging: built-in Python tools for environment variables and logs
# - datetime: for working with dates
# - dotenv: loads your .env file so the bot can read your secret keys
# - telegram: the Telegram bot library that handles messages and buttons
# - supabase: connects to your Supabase database
# - gspread + google.oauth2: connects to your Google Sheet
# ─────────────────────────────────────────────────────────────────────────────
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from supabase import create_client, Client
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# Prints helpful messages to your terminal so you can see what's happening.
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE CONNECTION
# Connects to your Supabase database using credentials from .env
# ─────────────────────────────────────────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"]
)

# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE SHEETS CONNECTION
# Connects to your Google Sheet using the service account credentials file.
# ─────────────────────────────────────────────────────────────────────────────
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_file("google_credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])

# The Telegram group chat ID where attendance polls will be posted.
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])

# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION STATES
# Tracks which step the admin is on during /start_session flow:
#   1. CHOOSE_TYPE  → pick Training / Friendly / Match
#   2. CHOOSE_DATE  → type the date
#   3. CHOOSE_TIME  → type the time / duration
#   4. CHOOSE_VENUE → type the venue
# ─────────────────────────────────────────────────────────────────────────────
CHOOSE_TYPE, CHOOSE_DATE, CHOOSE_TIME, CHOOSE_VENUE = range(4)

SESSION_TYPES = {
    "training": "🏋️ Training",
    "friendly": "🤝 Friendly",
    "match":    "⚽ Match",
}


# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE SHEETS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_member(username: str) -> dict | None:
    """
    Look up a member by Telegram username in the 'members' tab.
    Returns {username, name, position} or None if not found.
    """
    username = username.lstrip("@").lower()
    ws = sheet.worksheet("members")
    records = ws.get_all_records()
    for row in records:
        if str(row.get("username", "")).lstrip("@").lower() == username:
            return row
    return None


def get_all_members() -> list[dict]:
    """Returns all members from the 'members' tab."""
    ws = sheet.worksheet("members")
    return ws.get_all_records()


def is_admin(username: str) -> bool:
    """Returns True if the username is listed in the 'admins' tab."""
    username = username.lstrip("@").lower()
    ws = sheet.worksheet("admins")
    records = ws.get_all_records()
    for row in records:
        if str(row.get("username", "")).lstrip("@").lower() == username:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_active_sessions() -> list[dict]:
    """
    Returns ALL currently active sessions (there can be more than one).
    Sorted by date so the soonest session appears first.
    """
    res = supabase.table("sessions").select("*").eq("active", True).order("date").execute()
    return res.data


def get_session_by_id(session_id: int) -> dict | None:
    """Fetches a single session by its ID."""
    res = supabase.table("sessions").select("*").eq("id", session_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_member_response(session_id: int, username: str) -> str | None:
    """
    Checks how a member responded for a specific session.
    Returns 'yes', 'no', or None if they haven't responded.
    """
    res = (
        supabase.table("attendance")
        .select("attending")
        .eq("session_id", session_id)
        .eq("username", username.lstrip("@").lower())
        .execute()
    )
    return res.data[0]["attending"] if res.data else None


def upsert_attendance(session_id: int, username: str, name: str, position: str, attending: str):
    """
    Saves or updates a member's attendance for a specific session.
    - If no record exists → inserts a new one
    - If a record exists → updates the 'attending' field
    'attending' is 'yes' or 'no'.
    """
    existing = (
        supabase.table("attendance")
        .select("id")
        .eq("session_id", session_id)
        .eq("username", username)
        .execute()
    )
    if existing.data:
        supabase.table("attendance").update({
            "attending": attending
        }).eq("session_id", session_id).eq("username", username).execute()
    else:
        supabase.table("attendance").insert({
            "session_id": session_id,
            "username": username,
            "name": name,
            "position": position,
            "date": datetime.today().strftime("%Y-%m-%d"),
            "attending": attending
        }).execute()


def get_attendance_lists(session_id: int) -> tuple[list, list, list]:
    """
    Returns three lists for a given session:
    - attending: members who said 'yes'
    - not_attending: members who said 'no'
    - no_response: members from Google Sheet who haven't responded
    """
    res = supabase.table("attendance").select("*").eq("session_id", session_id).execute()
    attending = [r for r in res.data if r["attending"] == "yes"]
    not_attending = [r for r in res.data if r["attending"] == "no"]

    all_members = get_all_members()
    responded_usernames = {r["username"].lower() for r in res.data}
    no_response = [
        m for m in all_members
        if str(m.get("username", "")).lstrip("@").lower() not in responded_usernames
    ]

    return attending, not_attending, no_response


def esc(text: str) -> str:
    """Escapes HTML special characters to prevent formatting errors in Telegram."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_date(iso_date: str) -> str:
    """Converts a YYYY-MM-DD date string to 'Tuesday, 25 March 2025'."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%A, %d %B %Y")
    except Exception:
        return iso_date


# ─────────────────────────────────────────────────────────────────────────────
# POLL MESSAGE BUILDER
# Builds the attendance poll message posted in the group.
# Each poll embeds the session ID in its button callback data (e.g. attend:yes:42)
# so tapping a button on one poll only affects that specific session.
# ─────────────────────────────────────────────────────────────────────────────
def build_poll_message(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    attending, not_attending, no_response = get_attendance_lists(session["id"])

    time_line = f"\n⏰ {esc(session['time'])}" if session.get("time") else ""
    venue_line = f"\n📍 {esc(session['venue'])}" if session.get("venue") else ""

    lines = [
        f"📋 <b>{esc(session['name'])}</b>",
        f"📅 {format_date(session['date'])}{time_line}{venue_line}",
        "",
        "Are you attending this session?",
        "",
        f"✅ <b>Attending ({len(attending)})</b>",
    ]

    if attending:
        for r in attending:
            lines.append(f"  • {esc(r['name'])} — {esc(r['position'])}")
    else:
        lines.append("  <i>None yet</i>")

    lines.append("")
    lines.append(f"❌ <b>Not Attending ({len(not_attending)})</b>")

    if not_attending:
        for r in not_attending:
            lines.append(f"  • {esc(r['name'])} — {esc(r['position'])}")
    else:
        lines.append("  <i>None yet</i>")

    lines.append("")
    lines.append(f"⬜ <b>No Response ({len(no_response)})</b>")

    if no_response:
        for m in no_response:
            lines.append(f"  • {esc(m['name'])} — {esc(m['position'])}")
    else:
        lines.append("  <i>Everyone has responded!</i>")

    # Embed the session ID in the callback data so each poll's buttons
    # are linked to the correct session e.g. "attend:yes:42"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Attending", callback_data=f"attend:yes:{session['id']}"),
            InlineKeyboardButton("❌ Not Attending", callback_data=f"attend:no:{session['id']}"),
        ]
    ])

    return "\n".join(lines), keyboard


# ─────────────────────────────────────────────────────────────────────────────
# BOT COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a welcome message with available commands."""
    await update.message.reply_text(
        "👋 <b>Attendance Bot</b>\n\n"
        "Admin commands (use in private chat with bot):\n"
        "  /start_session — Start a new session\n"
        "  /end_session — Close an active session\n"
        "  /view_attendance — View attendance for a session",
        parse_mode="HTML"
    )


async def start_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Begins the session creation flow (admin only, private chat only).
    Shows the session type buttons as the first step.
    """
    username = update.effective_user.username

    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Please use this command in a private chat with me.")
        return ConversationHandler.END

    if not username or not is_admin(username):
        await update.message.reply_text("❌ You don't have permission to do this.")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏋️ Training", callback_data="type:training"),
            InlineKeyboardButton("🤝 Friendly", callback_data="type:friendly"),
            InlineKeyboardButton("⚽ Match",    callback_data="type:match"),
        ]
    ])

    await update.message.reply_text("Select the session type:", reply_markup=keyboard)
    return CHOOSE_TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the selected session type and asks for the date."""
    query = update.callback_query
    await query.answer()

    session_type = query.data.split(":")[1]
    label = SESSION_TYPES.get(session_type, session_type.capitalize())
    context.user_data["session_type"] = label

    await query.edit_message_text(
        f"Selected: <b>{label}</b>\n\n"
        f"Please enter the session date (DD-MM-YYYY):\n"
        f"Example: 25-03-2025",
        parse_mode="HTML"
    )
    return CHOOSE_DATE


async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Validates the date format and asks for the session time.
    Stays on this step if the format is wrong.
    """
    text = update.message.text.strip()

    try:
        parsed = datetime.strptime(text, "%d-%m-%Y")
        iso_date = parsed.strftime("%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid date format. Please enter as DD-MM-YYYY\nExample: 25-03-2025"
        )
        return CHOOSE_DATE

    session_type = context.user_data.get("session_type", "Session")
    context.user_data["session_date"] = iso_date
    context.user_data["session_name"] = f"{session_type} — {parsed.strftime('%d %B %Y')}"

    await update.message.reply_text(
        f"📅 Date: <b>{parsed.strftime('%A, %d %B %Y')}</b>\n\n"
        f"Please enter the time / duration:\n"
        f"Example: 8:00PM - 10:00PM",
        parse_mode="HTML"
    )
    return CHOOSE_TIME


async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the session time and asks for the venue."""
    context.user_data["session_time"] = update.message.text.strip()

    await update.message.reply_text(
        f"⏰ Time: <b>{esc(context.user_data['session_time'])}</b>\n\n"
        f"Please enter the venue:",
        parse_mode="HTML"
    )
    return CHOOSE_VENUE


async def choose_venue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Final step — saves the venue, creates the session in Supabase,
    builds the poll and posts it to the group chat.
    """
    context.user_data["session_venue"] = update.message.text.strip()

    session_name  = context.user_data["session_name"]
    iso_date      = context.user_data["session_date"]
    session_time  = context.user_data["session_time"]
    session_venue = context.user_data["session_venue"]

    # Insert the new session into Supabase
    res = supabase.table("sessions").insert({
        "name":   session_name,
        "date":   iso_date,
        "time":   session_time,
        "venue":  session_venue,
        "active": True
    }).execute()

    session = res.data[0]
    poll_text, poll_keyboard = build_poll_message(session)

    # Post the poll to the group chat
    poll_msg = await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=poll_text,
        parse_mode="HTML",
        reply_markup=poll_keyboard
    )

    # Save the poll message ID so we can edit it later when members respond
    supabase.table("sessions").update({
        "poll_message_id": poll_msg.message_id,
        "poll_chat_id":    poll_msg.chat_id
    }).eq("id", session["id"]).execute()

    await update.message.reply_text(
        f"✅ <b>Session started!</b>\n\n"
        f"📋 {esc(session_name)}\n"
        f"⏰ {esc(session_time)}\n"
        f"📍 {esc(session_venue)}\n\n"
        f"Poll has been posted to the group.",
        parse_mode="HTML"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the session creation flow at any step."""
    await update.message.reply_text("❌ Session creation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin only, private chat only.
    If multiple sessions are active, shows buttons to pick which one to close.
    If only one session is active, closes it immediately.
    """
    username = update.effective_user.username

    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Please use this command in a private chat with me.")
        return

    if not username or not is_admin(username):
        await update.message.reply_text("❌ You don't have permission to do this.")
        return

    sessions = get_active_sessions()
    if not sessions:
        await update.message.reply_text("❌ No active sessions found.")
        return

    # If only one session, close it directly without asking
    if len(sessions) == 1:
        await _close_session(sessions[0], context, update.message)
        return

    # Multiple sessions — show buttons so admin picks which one to close
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{esc(s['name'])} — {format_date(s['date'])}",
            callback_data=f"close:{s['id']}"
        )]
        for s in sessions
    ])

    await update.message.reply_text(
        "Which session would you like to close?",
        reply_markup=keyboard
    )


async def end_session_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the admin tapping a session button in /end_session.
    Closes the chosen session.
    """
    query = update.callback_query
    await query.answer()

    session_id = int(query.data.split(":")[1])
    session = get_session_by_id(session_id)

    if not session or not session["active"]:
        await query.edit_message_text("❌ Session not found or already closed.")
        return

    await query.edit_message_text(f"Closing <b>{esc(session['name'])}</b>...", parse_mode="HTML")
    await _close_session(session, context, query.message)


async def _close_session(session: dict, context, message):
    """
    Internal helper that closes a session:
    - Marks it inactive in Supabase
    - Edits the poll in the group to show CLOSED with final results
    - Sends a summary to the admin
    """
    attending, not_attending, no_response = get_attendance_lists(session["id"])
    supabase.table("sessions").update({"active": False}).eq("id", session["id"]).execute()

    # Build the final closed version of the poll message (no buttons)
    if session.get("poll_message_id") and session.get("poll_chat_id"):
        time_line  = f"\n⏰ {esc(session['time'])}"  if session.get("time")  else ""
        venue_line = f"\n📍 {esc(session['venue'])}" if session.get("venue") else ""
        closed_text = (
            f"📋 <b>{esc(session['name'])}</b>\n"
            f"📅 {format_date(session['date'])}{time_line}{venue_line}  |  🔒 CLOSED\n\n"
            f"✅ <b>Attending ({len(attending)})</b>\n"
        )
        for r in attending:
            closed_text += f"  • {esc(r['name'])} — {esc(r['position'])}\n"
        if not attending:
            closed_text += "  <i>None</i>\n"

        closed_text += f"\n❌ <b>Not Attending ({len(not_attending)})</b>\n"
        for r in not_attending:
            closed_text += f"  • {esc(r['name'])} — {esc(r['position'])}\n"
        if not not_attending:
            closed_text += "  <i>None</i>\n"

        closed_text += f"\n⬜ <b>No Response ({len(no_response)})</b>\n"
        for m in no_response:
            closed_text += f"  • {esc(m['name'])} — {esc(m['position'])}\n"
        if not no_response:
            closed_text += "  <i>Everyone responded</i>\n"

        try:
            await context.bot.edit_message_text(
                chat_id=session["poll_chat_id"],
                message_id=session["poll_message_id"],
                text=closed_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not edit poll message: {e}")

    time_line  = f"\n⏰ {esc(session['time'])}"  if session.get("time")  else ""
    venue_line = f"\n📍 {esc(session['venue'])}" if session.get("venue") else ""
    await message.reply_text(
        f"🔒 <b>Session closed!</b>\n\n"
        f"📋 {esc(session['name'])}\n"
        f"📅 {format_date(session['date'])}{time_line}{venue_line}\n\n"
        f"✅ {len(attending)} attending  |  "
        f"❌ {len(not_attending)} not attending  |  "
        f"⬜ {len(no_response)} no response\n\n"
        f"Use /view_attendance to see the full report.",
        parse_mode="HTML"
    )


async def view_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin only. Shows a list of all sessions (active and closed) to pick from.
    Active sessions are marked 🟢, closed ones are marked 🔒.
    Shows the most recent 10 sessions.
    """
    username = update.effective_user.username

    if not username or not is_admin(username):
        if update.effective_chat.type != "private":
            return
        await update.message.reply_text("❌ You don't have permission to do this.")
        return

    # Fetch the 10 most recent sessions regardless of active/closed status
    res = supabase.table("sessions").select("*").order("id", desc=True).limit(10).execute()
    if not res.data:
        await update.message.reply_text("❌ No sessions found.")
        return

    sessions = res.data

    # If only one session exists, show it directly without asking
    if len(sessions) == 1:
        await _send_attendance_report(sessions[0], update.message)
        return

    # Build a button for each session showing its status, name and date
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'🟢' if s['active'] else '🔒'} {esc(s['name'])} — {format_date(s['date'])}",
            callback_data=f"view:{s['id']}"
        )]
        for s in sessions
    ])

    await update.message.reply_text(
        "Which session would you like to view?",
        reply_markup=keyboard
    )


async def view_attendance_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the admin tapping a session button in /view_attendance.
    Shows the attendance report for the chosen session.
    """
    query = update.callback_query
    await query.answer()

    session_id = int(query.data.split(":")[1])
    session = get_session_by_id(session_id)

    if not session:
        await query.edit_message_text("❌ Session not found.")
        return

    await query.edit_message_text("Loading attendance report...")
    await _send_attendance_report(session, query.message)


async def _send_attendance_report(session: dict, message):
    """
    Internal helper that sends the full attendance report for a session.
    Shows ✅ attending, ❌ not attending, ⬜ no response for every member.
    Splits into multiple messages if over Telegram's 4096 character limit.
    """
    all_members = get_all_members()
    attending, not_attending, no_response = get_attendance_lists(session["id"])

    attending_usernames     = {r["username"].lower() for r in attending}
    not_attending_usernames = {r["username"].lower() for r in not_attending}

    status_label = "🟢 ACTIVE" if session["active"] else "🔒 CLOSED"
    time_line  = f"\n⏰ {esc(session['time'])}"  if session.get("time")  else ""
    venue_line = f"\n📍 {esc(session['venue'])}" if session.get("venue") else ""
    lines = [
        f"📋 <b>{esc(session['name'])}</b>",
        f"📅 {format_date(session['date'])}{time_line}{venue_line}  |  {status_label}",
        f"✅ {len(attending)} attending  |  ❌ {len(not_attending)} not attending  |  ⬜ {len(no_response)} no response\n",
    ]

    for i, member in enumerate(all_members, 1):
        uname    = str(member.get("username", "")).lstrip("@").lower()
        name     = member.get("name", "Unknown")
        position = member.get("position", "")
        if uname in attending_usernames:
            tick = "✅"
        elif uname in not_attending_usernames:
            tick = "❌"
        else:
            tick = "⬜"
        lines.append(f"{tick} {i}. {esc(name)} — {esc(position)}")

    text = "\n".join(lines)
    if len(text) > 4096:
        chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
        for chunk in chunks:
            await message.reply_text(chunk, parse_mode="HTML")
    else:
        await message.reply_text(text, parse_mode="HTML")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles taps on ✅ Attending and ❌ Not Attending buttons in the group poll.
    The session ID is embedded in the callback data (e.g. 'attend:yes:42')
    so each poll's buttons only affect their own session.
    Members can change their mind by tapping the other button.
    """
    query = update.callback_query
    await query.answer()

    username = query.from_user.username
    if not username:
        await query.answer("❌ You need a Telegram username.", show_alert=True)
        return

    # Check they are a registered member in the Google Sheet
    member = get_member(username)
    if not member:
        await query.answer("❌ You are not a registered member.", show_alert=True)
        return

    # Parse callback data: "attend:yes:42" → response=yes, session_id=42
    parts = query.data.split(":")
    response   = parts[1]           # 'yes' or 'no'
    session_id = int(parts[2])      # the specific session this poll belongs to

    # Fetch the session this button belongs to
    session = get_session_by_id(session_id)
    if not session or not session["active"]:
        await query.answer("❌ This session is no longer active.", show_alert=True)
        return

    attending      = response  # 'yes' or 'no'
    clean_username = username.lstrip("@").lower()

    # If they already have the same response, no need to update
    current = get_member_response(session_id, clean_username)
    if current == attending:
        label = "attending" if attending == "yes" else "not attending"
        await query.answer(f"You've already marked yourself as {label}.")
        return

    # Save or update their response
    upsert_attendance(session_id, clean_username, member["name"], member["position"], attending)

    # Rebuild and update only this poll's message
    text, keyboard = build_poll_message(session)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    label = "✅ Attending" if attending == "yes" else "❌ Not Attending"
    await query.answer(f"Marked as {label}!")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — BOT STARTUP
# Registers all command and button handlers, then starts the bot.
# ─────────────────────────────────────────────────────────────────────────────
def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    # Multi-step conversation handler for /start_session
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start_session", start_session)],
        states={
            CHOOSE_TYPE:  [CallbackQueryHandler(choose_type, pattern="^type:")],
            CHOOSE_DATE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_date)],
            CHOOSE_TIME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_time)],
            CHOOSE_VENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_venue)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    # end_session: close button picker (pattern "close:ID")
    app.add_handler(CommandHandler("end_session", end_session))
    app.add_handler(CallbackQueryHandler(end_session_pick, pattern="^close:"))

    # view_attendance: view button picker (pattern "view:ID")
    app.add_handler(CommandHandler("view_attendance", view_attendance))
    app.add_handler(CallbackQueryHandler(view_attendance_pick, pattern="^view:"))

    # Attend buttons in group polls (pattern "attend:yes/no:ID")
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^attend:"))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
