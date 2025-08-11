import logging
from datetime import datetime, timedelta
import asyncio
import os
import requests
from queue import Queue
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==== PLACEHOLDER VALUES ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY")

# ==== CONFIGURATION ====
TRAFFIC_DELAY_THRESHOLD_MINS = 5  # Only alert if delay > 5 minutes
MINOR_DELAY_THRESHOLD_MINS = 2    # Show minor delay info but no urgent alert

# ==== GLOBALS ====
user_data = {}  # Stores user info: location, times, etc.
user_state = {}  # Tracks user's current state in setup process
message_queue = Queue()  # Queue for thread-safe message sending
user_next_checks = {}  # For async scheduler: {chat_id: {"office": datetime, "home": datetime, "office_end": datetime, "home_end": datetime}}
app = None  # Global app instance

# User states
STATE_WAITING_OFFICE_TIME = "waiting_office_time"
STATE_WAITING_HOME_TIME = "waiting_home_time"
STATE_WAITING_HOME_LOCATION = "waiting_home_location"
STATE_WAITING_OFFICE_LOCATION = "waiting_office_location"
STATE_SETUP_COMPLETE = "setup_complete"

# ==== LOGGING ====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO,
    handlers=[
        logging.FileHandler('traffic_bot.log'),
        logging.StreamHandler()
    ]
)

# ==== BOT COMMANDS AND HANDLERS ====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_state[chat_id] = STATE_WAITING_OFFICE_TIME
    user_data[chat_id] = {}
    
    await update.message.reply_text(
        "Welcome to Traffic Alert Bot! 🚗\n\n"
        "Let's set up your daily commute schedule.\n\n"
        "When will you start to office? (Please enter time in HH:MM format, 24-hour)"
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    current_state = user_state.get(chat_id)
    
    if current_state == STATE_WAITING_OFFICE_TIME:
        await handle_office_time(update, context)
    elif current_state == STATE_WAITING_HOME_TIME:
        await handle_home_time(update, context)
    else:
        await update.message.reply_text("Please use /start to begin setup.")

async def handle_office_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    time_str = update.message.text
    
    try:
        office_time = datetime.strptime(time_str, "%H:%M").time()
        user_data[chat_id]["office_start_time"] = office_time
        user_state[chat_id] = STATE_WAITING_HOME_TIME
        
        await update.message.reply_text(
            f"✅ Office start time saved: {time_str}\n\n"
            "When will you start to home from office? (Please enter time in HH:MM format, 24-hour)"
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time format. Please use HH:MM (24-hour format).\n"
            "Example: 09:30 or 17:45"
        )

async def handle_home_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    time_str = update.message.text
    
    try:
        home_time = datetime.strptime(time_str, "%H:%M").time()
        user_data[chat_id]["home_start_time"] = home_time
        user_state[chat_id] = STATE_WAITING_HOME_LOCATION
        
        await update.message.reply_text(
            f"✅ Home start time saved: {time_str}\n\n"
            "Now, please send me your Home location 🏠\n"
            "Tap the 📍 button below to share your location.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Share Home Location", request_location=True)]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time format. Please use HH:MM (24-hour format).\n"
            "Example: 09:30 or 17:45"
        )

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    current_state = user_state.get(chat_id)
    
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    
    if current_state == STATE_WAITING_HOME_LOCATION:
        user_data[chat_id]["home_lat"] = lat
        user_data[chat_id]["home_lon"] = lon
        user_state[chat_id] = STATE_WAITING_OFFICE_LOCATION
        
        await update.message.reply_text(
            f"✅ Home location saved!\n"
            f"📍 Coordinates: {lat:.4f}, {lon:.4f}\n\n"
            "Now, please send me your Office location 🏢\n"
            "Tap the 📍 button below to share your office location.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Share Office Location", request_location=True)]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
        
    elif current_state == STATE_WAITING_OFFICE_LOCATION:
        user_data[chat_id]["office_lat"] = lat
        user_data[chat_id]["office_lon"] = lon
        user_state[chat_id] = STATE_SETUP_COMPLETE
        
        await update.message.reply_text(
            f"✅ Office location saved!\n"
            f"📍 Coordinates: {lat:.4f}, {lon:.4f}\n\n"
            "🎉 Setup complete! Your traffic monitoring is now active.\n\n"
            "📋 Your Schedule:\n"
            f"🏠➡️🏢 Office departure: {user_data[chat_id]['office_start_time'].strftime('%H:%M')}\n"
            f"🏢➡️🏠 Home departure: {user_data[chat_id]['home_start_time'].strftime('%H:%M')}\n\n"
            "You'll receive traffic updates automatically before your commute times!"
        )
        
        # Start async tracking scheduler for this user
        await schedule_tracking(chat_id)
        
    else:
        await update.message.reply_text("Please use /start to begin setup.")

# ==== ASYNC SCHEDULER LOGIC ====

async def schedule_tracking(chat_id):
    """Initialize next check times for the user."""
    data = user_data.get(chat_id)
    if not data:
        return

    now = datetime.now()
    today = now.date()

    def next_check_time(base_time, before_mins, after_mins):
        start_check = datetime.combine(today, base_time) - timedelta(minutes=before_mins)
        end_check = datetime.combine(today, base_time) + timedelta(minutes=after_mins)
        if end_check < now:
            # schedule for next day
            start_check += timedelta(days=1)
            end_check += timedelta(days=1)
        return start_check, end_check

    office_start, office_end = next_check_time(data["office_start_time"], 30, 30)
    home_start, home_end = next_check_time(data["home_start_time"], 60, 30)

    user_next_checks[chat_id] = {
        "office": office_start,
        "home": home_start,
        "office_end": office_end,
        "home_end": home_end
    }

async def schedule_tracking_for_mode(chat_id, mode):
    """Schedule the next day's tracking window for a specific mode."""
    data = user_data.get(chat_id)
    if not data:
        return

    tomorrow = datetime.now().date() + timedelta(days=1)

    if mode == "office":
        office_time = data["office_start_time"]
        start_check = datetime.combine(tomorrow, office_time) - timedelta(minutes=30)
        end_check = datetime.combine(tomorrow, office_time) + timedelta(minutes=30)
        user_next_checks[chat_id]["office"] = start_check
        user_next_checks[chat_id]["office_end"] = end_check

    elif mode == "home":
        home_time = data["home_start_time"]
        start_check = datetime.combine(tomorrow, home_time) - timedelta(minutes=60)
        end_check = datetime.combine(tomorrow, home_time) + timedelta(minutes=30)
        user_next_checks[chat_id]["home"] = start_check
        user_next_checks[chat_id]["home_end"] = end_check

async def async_scheduler():
    """Main async scheduler loop checking all users and sending updates."""
    while True:
        now = datetime.now()
        for chat_id, checks in list(user_next_checks.items()):
            data = user_data.get(chat_id)
            if not data:
                continue

            # OFFICE check window
            if checks.get("office") and checks.get("office_end") and checks["office"] <= now <= checks["office_end"]:
                if now >= checks["office"]:
                    await send_tomtom_update_async(chat_id, "office")
                    user_next_checks[chat_id]["office"] = now + timedelta(minutes=2)
            elif checks.get("office_end") and now > checks["office_end"]:
                await schedule_tracking_for_mode(chat_id, "office")

            # HOME check window
            if checks.get("home") and checks.get("home_end") and checks["home"] <= now <= checks["home_end"]:
                if now >= checks["home"]:
                    await send_tomtom_update_async(chat_id, "home")
                    user_next_checks[chat_id]["home"] = now + timedelta(minutes=2)
            elif checks.get("home_end") and now > checks["home_end"]:
                await schedule_tracking_for_mode(chat_id, "home")

        await asyncio.sleep(60)  # Check every minute

async def send_tomtom_update_async(chat_id, mode):
    """Async wrapper for sending TomTom update (calls sync code inside)."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_tomtom_update, chat_id, mode)

# ==== EXISTING SYNC FUNCTION FOR TOMTOM API ====

def send_tomtom_update(chat_id, mode):
    """Send traffic update using TomTom API."""
    try:
        data = user_data.get(chat_id)
        if not data:
            return
        
        if mode == "office":
            # From home to office
            start_lat, start_lon = data["home_lat"], data["home_lon"]
            end_lat, end_lon = data["office_lat"], data["office_lon"]
            route_desc = "🏠➡️🏢 Home to Office"
        else:
            # From office to home
            start_lat, start_lon = data["office_lat"], data["office_lon"]
            end_lat, end_lon = data["home_lat"], data["home_lon"]
            route_desc = "🏢➡️🏠 Office to Home"
        
        url = f"https://api.tomtom.com/routing/1/calculateRoute/{start_lat},{start_lon}:{end_lat},{end_lon}/json"
        params = {
            'key': TOMTOM_API_KEY,
            'traffic': 'true',
            'departAt': datetime.now().isoformat()
        }
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            route_data = response.json()
            
            if route_data.get("routes"):
                summary = route_data["routes"][0]["summary"]
                travel_time_mins = summary["travelTimeInSeconds"] // 60
                delay_seconds = summary.get("trafficDelayInSeconds", 0)
                delay_mins = delay_seconds // 60
                
                current_time = datetime.now().strftime("%H:%M")
                
                # Smart delay alerting with thresholds
                if delay_mins >= TRAFFIC_DELAY_THRESHOLD_MINS:
                    # Significant delay - urgent alert
                    message = (
                        f"🚨 {route_desc} Traffic Alert!\n"
                        f"⏰ Time: {current_time}\n"
                        f"🕐 Total travel time: {travel_time_mins} mins\n"
                        f"🚦 Traffic delay: {delay_mins} mins\n"
                        f"💡 Consider leaving early!"
                    )
                elif delay_mins >= MINOR_DELAY_THRESHOLD_MINS:
                    # Minor delay - informational
                    message = (
                        f"⚠️ {route_desc} Traffic Update\n"
                        f"⏰ Time: {current_time}\n"
                        f"🕐 Total travel time: {travel_time_mins} mins\n"
                        f"🚦 Minor delay: {delay_mins} mins\n"
                        f"ℹ️ Normal traffic conditions"
                    )
                else:
                    # No significant delay
                    message = (
                        f"✅ {route_desc} Traffic Update\n"
                        f"⏰ Time: {current_time}\n"
                        f"🕐 Travel time: {travel_time_mins} mins\n"
                        f"🚦 No delays - all clear!"
                    )
            else:
                message = f"❌ No route found for {route_desc}"
        else:
            message = f"❌ Failed to fetch traffic data for {route_desc} (Status: {response.status_code})"
        
        # Add message to queue for thread-safe sending
        message_queue.put((chat_id, message))
        
    except Exception as e:
        logging.error(f"Error sending TomTom update: {e}")
        message_queue.put((chat_id, f"❌ Error getting traffic update: {str(e)}"))

# ==== MESSAGE QUEUE PROCESSOR ====

async def message_queue_processor():
    """Process messages from the queue and send them."""
    while True:
        try:
            if not message_queue.empty():
                chat_id, message = message_queue.get_nowait()
                await app.bot.send_message(chat_id=chat_id, text=message)
                message_queue.task_done()
            else:
                await asyncio.sleep(1)  # Wait 1 second before checking again
        except Exception as e:
            logging.error(f"Error processing message queue: {e}")
            await asyncio.sleep(5)  # Wait 5 seconds on error

# ==== OTHER COMMANDS (status, settings) ====

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    data = user_data.get(chat_id)
    state = user_state.get(chat_id)
    
    if not data or state != STATE_SETUP_COMPLETE:
        await update.message.reply_text(
            "❌ Setup not complete. Please use /start to configure your commute."
        )
        return
    
    message = (
        f"⚙️ Traffic Alert Settings\n\n"
        f"🚨 Urgent Alert Threshold: {TRAFFIC_DELAY_THRESHOLD_MINS} minutes\n"
        f"⚠️ Minor Alert Threshold: {MINOR_DELAY_THRESHOLD_MINS} minutes\n\n"
        f"📊 Current thresholds:\n"
        f"• ≥{TRAFFIC_DELAY_THRESHOLD_MINS} mins: 🚨 Urgent alert with 'leave early' advice\n"
        f"• {MINOR_DELAY_THRESHOLD_MINS}-{TRAFFIC_DELAY_THRESHOLD_MINS-1} mins: ⚠️ Minor delay notification\n"
        f"• <{MINOR_DELAY_THRESHOLD_MINS} mins: ✅ All clear message\n\n"
        f"💡 Contact admin to adjust thresholds if needed."
    )
    
    await update.message.reply_text(message)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    data = user_data.get(chat_id)
    state = user_state.get(chat_id)
    
    if not data or state != STATE_SETUP_COMPLETE:
        await update.message.reply_text(
            "❌ Setup not complete. Please use /start to configure your commute."
        )
        return
    
    office_time = data["office_start_time"].strftime("%H:%M")
    home_time = data["home_start_time"].strftime("%H:%M")
    
    message = (
        f"📊 Traffic Bot Status\n\n"
        f"📋 Your Schedule:\n"
        f"🏠➡️🏢 Office departure: {office_time}\n"
        f"🏢➡️🏠 Home departure: {home_time}\n\n"
        f"📍 Locations configured: ✅\n"
        f"🤖 Monitoring active: ✅\n\n"
        f"ℹ️ You'll receive updates:\n"
        f"• 30 min before office time (until 30 min after)\n"
        f"• 60 min before home time (until 30 min after)\n"
        f"• Every 2 minutes during active periods"
    )
    
    await update.message.reply_text(message)

# ==== MAIN FUNCTION ====

def main():
    global app
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("settings", settings_command))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logging.info("Bot starting...")
    print("🤖 Traffic Alert Bot is running...")
    
    # Get event loop
    loop = asyncio.get_event_loop()

    # Start message queue processor
    loop.create_task(message_queue_processor())
    # Start async scheduler
    loop.create_task(async_scheduler())
    
    app.run_polling()

if __name__ == "__main__":
    main()
