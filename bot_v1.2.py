import os
import asyncio
import httpx
import threading
import logging
import re
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramAPIError

# --- INITIAL SETUP & LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) 
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[:-8]
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
HERO_SMS_KEY = os.getenv("HERO_SMS_API_KEY", "")
HERO_SMS_URL = os.getenv("HERO_SMS_BASE_URL", "https://hero-sms.com/stubs/handler_api.php")

# --- GLOBAL CONFIGURATION METRICS ---
PH_COUNTRY_ID = 4 
USD_TO_PHP_RATE = 60.50  

# --- PRICING, STOCKS & PERSISTENCE ---
SERVICES_FILE = "services.json"

DEFAULT_SERVICES = {
    "UNO": {"code": "ot", "price": 7.00, "stock": 100, "operator": "any"},     
    "CASINO +": {"code": "ot", "price": 5.00, "stock": 100, "operator": "any"}
}

def load_services() -> dict:
    if not os.path.exists(SERVICES_FILE):
        return DEFAULT_SERVICES.copy()
    try:
        with open(SERVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if data else DEFAULT_SERVICES.copy()
    except Exception as e:
        logging.error(f"Failed to load services: {e}")
        return DEFAULT_SERVICES.copy()

def save_services():
    try:
        with open(SERVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(SERVICES_PRICING, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save services: {e}")

SERVICES_PRICING = load_services()

# --- INITIALIZE CORE CLIENTS ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
http_client: httpx.AsyncClient = None

def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(timeout=15.0)
    return http_client

# --- STATE MACHINES ---
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_new_price = State()
    waiting_for_add_user_id = State()
    waiting_for_new_service_name = State()
    waiting_for_new_service_code = State()
    waiting_for_new_service_price = State()

# --- USER ACCESS MANAGEMENT (WHITELIST) ---
ALLOWED_USERS_FILE = "allowed_users.json"

def load_allowed_users() -> set:
    if not os.path.exists(ALLOWED_USERS_FILE):
        return set()
    try:
        with open(ALLOWED_USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("allowed_users", []))
    except Exception:
        return set()

def save_allowed_users(users_set: set):
    try:
        with open(ALLOWED_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"allowed_users": list(users_set)}, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save allowed users: {e}")

def is_user_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    return user_id in load_allowed_users()

def add_allowed_user(user_id: int):
    users = load_allowed_users()
    users.add(user_id)
    save_allowed_users(users)

def remove_allowed_user(user_id: int):
    users = load_allowed_users()
    users.discard(user_id)
    save_allowed_users(users)

# --- NON-BLOCKING DATABASE WORKER ---
async def db_execute(query_chain):
    try:
        return await asyncio.to_thread(query_chain.execute)
    except Exception as e:
        logging.error(f"Database query error: {e}")
        class DummyResult:
            data = None
        return DummyResult()

def format_ph_number(raw_num: str) -> str:
    cleaned = raw_num.strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("63"): cleaned = cleaned[2:]
    if cleaned.startswith("0"): cleaned = cleaned[1:]
    return cleaned

# ==========================================
# 📱 UI KEYBOARD FACTORIES
# ==========================================

def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard_grid = [
        [types.KeyboardButton(text="📞 Buy Numbers"), types.KeyboardButton(text="🔁 Extend Numbers")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard_grid, resize_keyboard=True, is_persistent=True)

def inline_back_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Close Menu", callback_data="menu_main"))
    return builder.as_markup()

def admin_back_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Admin Dashboard", callback_data="admin_dashboard_home"))
    return builder.as_markup()

# ==========================================
# 🔥 ANTI-SPAM SINGLE WINDOW ENGINE
# ==========================================

async def send_or_edit_transient(message: types.Message, state: FSMContext, text: str, reply_markup: types.InlineKeyboardMarkup):
    try: await message.delete()
    except Exception: pass

    session_data = await state.get_data()
    old_msg_id = session_data.get("last_transient_msg_id")

    if old_msg_id:
        try:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=old_msg_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
            return
        except Exception: pass

    out_msg = await message.answer(text, reply_markup=reply_markup, parse_mode="Markdown")
    await state.update_data(last_transient_msg_id=out_msg.message_id)

async def track_ui(message: types.Message, state: FSMContext):
    await state.update_data(last_transient_msg_id=message.message_id)

# ==========================================
# 🚀 STARTUP INITIALIZATION
# ==========================================

async def on_startup(bot: Bot):
    global http_client
    http_client = httpx.AsyncClient(timeout=15.0)

    user_cmds = [
        types.BotCommand(command="start", description="Open Main Dashboard"),
        types.BotCommand(command="buy_again", description="Re-order Last Service Selection")
    ]
    await bot.set_my_commands(user_cmds, scope=types.BotCommandScopeDefault())

    admin_cmds = [
        types.BotCommand(command="start", description="Open Main Dashboard"),
        types.BotCommand(command="admin", description="Open Admin Panel 👑"),
        types.BotCommand(command="buy_again", description="Re-order Last Service Selection")
    ]
    try:
        await bot.set_my_commands(admin_cmds, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        logging.warning(f"Note: Could not set admin-specific commands yet (Admin hasn't started the bot or chat not found: {e}).")

# ==========================================
# 👤 STANDARD USER CONTROLLER INTERFACES
# ==========================================

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    username = message.from_user.username or "Anonymous"

    try: await message.delete()
    except Exception: pass

    await db_execute(supabase.table("users").upsert({"telegram_id": tg_id, "username": username}, on_conflict="telegram_id"))

    if not is_user_allowed(tg_id):
        await message.answer(
            f"⛔ **Access Restricted**\n\n"
            f"This bot is private and requires administrator authorization to use.\n\n"
            f"• **Your Telegram ID:** `{tg_id}`\n"
            f"• **Username:** @{username}\n\n"
            f"Please share your Telegram ID with the Administrator to request access.",
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return

    if tg_id == ADMIN_ID:
        try:
            admin_cmds = [
                types.BotCommand(command="start", description="Open Main Dashboard"),
                types.BotCommand(command="admin", description="Open Admin Panel 👑"),
                types.BotCommand(command="buy_again", description="Re-order Last Service Selection")
            ]
            await bot.set_my_commands(admin_cmds, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception:
            pass

    await message.answer(
        "👋 **Welcome to Keppo SMS Bot!**\n\n"
        "✅ Your account is authorized for **Unlimited Access**.\n"
        "Use the menu buttons below to lease or extend numbers.",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

@router.message(Command("buy_again"))
async def cmd_buy_again(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id
    if not is_user_allowed(tg_id):
        return await message.answer("⛔ **Access Denied.** You are not authorized to use this bot.")

    res = await db_execute(supabase.table("activations").select("service").eq("telegram_id", tg_id).order("activation_id", desc=True).limit(1))

    if not res.data:
        await send_or_edit_transient(message, state, "❌ No previous transaction histories found to clone.", inline_back_keyboard())
        return

    await initiate_number_purchase(message, tg_id, res.data[0]["service"], is_callback=False, state=state)

@router.callback_query(F.data == "menu_main")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    try: await callback.message.delete()
    except Exception: pass
    await state.update_data(last_transient_msg_id=None)
    await callback.answer()

@router.message(F.text == "🔁 Extend Numbers")
async def menu_extend_numbers_text(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return await message.answer("⛔ **Access Denied.** You are not authorized to use this bot.")

    res = await db_execute(
        supabase.table("activations")
        .select("*")
        .eq("telegram_id", user_id)
        .order("activation_id", desc=True)
        .limit(6)
    )

    if not res.data:
        await send_or_edit_transient(
            message, state, 
            "❌ **No recent activations found.**\n\nYou must purchase a number first before you can extend it.", 
            inline_back_keyboard()
        )
        return

    builder = InlineKeyboardBuilder()
    for act in res.data:
        act_id = act.get("activation_id")
        ph = act.get("phone_number", "Unknown")
        srv = act.get("service", "Service")
        st = act.get("status", "unknown")
        builder.row(types.InlineKeyboardButton(
            text=f"🔁 {srv} ({ph}) [{st.upper()}]", 
            callback_data=f"act_extend:{act_id}"
        ))
    builder.row(types.InlineKeyboardButton(text="🔙 Close Menu", callback_data="menu_main"))

    await send_or_edit_transient(
        message, state, 
        "📲 **Select an active or recent line to extend (Request another SMS):**", 
        builder.as_markup()
    )

# ==========================================
# 📱 MOBILE INFRASTRUCTURE ALLOCATION LINE
# ==========================================

@router.message(F.text == "📞 Buy Numbers")
async def menu_buy_text(message: types.Message, state: FSMContext):
    if not is_user_allowed(message.from_user.id):
        return await message.answer("⛔ **Access Denied.** You are not authorized to use this bot.")

    builder = InlineKeyboardBuilder()
    for s_name, s_meta in SERVICES_PRICING.items():
        builder.row(types.InlineKeyboardButton(text=f"📱 Get {s_name} (₱{s_meta['price']:.2f})", callback_data=f"buy_srv:{s_name}"))
    builder.row(types.InlineKeyboardButton(text="🔙 Close Menu", callback_data="menu_main"))

    await send_or_edit_transient(message, state, "Select target application platform integration context:", builder.as_markup())

@router.callback_query(F.data.startswith("buy_srv:"))
async def cb_execute_purchase(callback: types.CallbackQuery, state: FSMContext):
    if not is_user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Access Denied. Contact Admin.", show_alert=True)
    await callback.answer()
    await initiate_number_purchase(callback.message, callback.from_user.id, callback.data.split(":")[1], is_callback=True, state=state)

async def initiate_number_purchase(msg: types.Message, user_id: int, service_name: str, is_callback: bool, state: FSMContext):
    service_meta = SERVICES_PRICING.get(service_name)
    if not service_meta:
        text = "❌ **This service is currently unavailable.**"
        if is_callback: await msg.edit_text(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
        else: await send_or_edit_transient(msg, state, text, inline_back_keyboard())
        return

    cost = service_meta.get("price", 0.0)
    service_code = service_meta["code"]
    target_operator = service_meta.get("operator", "any")

    # Whitelisted users have UNLIMITED access - no balance deduction or check needed!

    text_loading = f"⏳ **Allocating {service_name} ({target_operator.upper()}) line... Please wait.**"

    if is_callback:
        await msg.edit_text(text_loading, parse_mode="Markdown")
        status_msg = msg
    else:
        status_msg = await bot.send_message(msg.chat.id, text_loading, parse_mode="Markdown")
        await track_ui(status_msg, state)

    try:
        cheapest_operator = ""
        try:
            client = get_http_client()
            price_resp = await client.get(
                HERO_SMS_URL,
                params={"api_key": HERO_SMS_KEY, "action": "getPrices", "service": service_code, "country": PH_COUNTRY_ID},
                timeout=8.0
            )
            if price_resp.status_code == 200:
                price_data = price_resp.json()
                country_str = str(PH_COUNTRY_ID)
                if country_str in price_data and service_code in price_data[country_str]:
                    service_nodes = price_data[country_str][service_code]
                    lowest_market_cost_php = float('inf')

                    for op_name, op_info in service_nodes.items():
                        if isinstance(op_info, dict) and op_info.get("count", 0) > 0:
                            if target_operator != "any" and op_name.lower() != target_operator.lower(): continue
                            op_cost_usd = float(op_info.get("cost", 0))
                            op_cost_php = op_cost_usd * USD_TO_PHP_RATE
                            if op_cost_php < lowest_market_cost_php:
                                lowest_market_cost_php = op_cost_php
                                cheapest_operator = op_name
        except Exception as api_err:
            logging.warning(f"Could not check prices: {api_err}")
            cheapest_operator = ""

        api_params = {"api_key": HERO_SMS_KEY, "action": "getNumber", "service": service_code, "country": PH_COUNTRY_ID}
        request_operator = cheapest_operator if target_operator == "any" else target_operator
        if request_operator and request_operator != "any":
            api_params["operator"] = request_operator

        client = get_http_client()
        resp = await client.get(HERO_SMS_URL, params=api_params, timeout=15.0)

        if resp.text.startswith("ACCESS_NUMBER"):
            _, act_id, raw_phone = resp.text.split(":")
            ph_clean = format_ph_number(raw_phone)

            await db_execute(supabase.table("activations").insert({
                "activation_id": act_id,
                "telegram_id": user_id,
                "service": service_name,
                "amount": cost,
                "phone_number": ph_clean,
                "status": "pending"
            }))

            control_kb = InlineKeyboardBuilder()
            control_kb.row(
                types.InlineKeyboardButton(text="❌ Cancel", callback_data=f"act_cancel:{act_id}"), 
                types.InlineKeyboardButton(text="⚠️ Report Used", callback_data=f"act_ban:{act_id}")
            )

            await status_msg.edit_text(
                f"📱 **Line Lease Active**\n\n"
                f"• **Platform:** `{service_name}`\n"
                f"• **Provider:** `{request_operator.upper() if request_operator else 'ANY'}`\n"
                f"• **Phone Number:** `{ph_clean}`\n\n"
                f"⏳ Waiting for verification code to arrive... (**120s remaining**)", 
                reply_markup=control_kb.as_markup(), parse_mode="Markdown"
            )
            asyncio.create_task(poll_for_incoming_otp(user_id, act_id, status_msg.message_id, service_name, cost, ph_clean, state))

        elif "NO_NUMBERS" in resp.text:
            await status_msg.edit_text(
                f"⚠️ **Out of Stock!**\n\n"
                f"No numbers are available for **{service_name}** ({target_operator.upper()}) right now.\n"
                f"Please try again in a few moments or switch the network operator in `/admin`.",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )
        elif "NO_BALANCE" in resp.text:
            await status_msg.edit_text(
                f"⚠️ **Provider Funds Depleted!**\n\n"
                f"The HeroSMS API account has insufficient balance.\n"
                f"Please notify the Administrator to top up the HeroSMS provider account.",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )
        elif "BAD_KEY" in resp.text:
            await status_msg.edit_text(
                f"⚠️ **Configuration Error:** Invalid HeroSMS API Key.",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ **Allocation Notice:** Provider response: `{resp.text}`.\n"
                f"Please try again in a few moments.",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )

    except Exception as e:
        logging.error(f"Error during initiate_number_purchase: {e}")
        await status_msg.edit_text(
            f"❌ **System Notice:** Number allocation failed: `{e}`.\nPlease try again shortly.",
            reply_markup=inline_back_keyboard(),
            parse_mode="Markdown"
        )

# ==========================================
# ⏳ BACKGROUND SMS MONITORING & FIRE-AND-FORGET
# ==========================================

async def background_cancel_hero_api(act_id: str):
    """Silently attempts to cancel the API lease. Waits the required 2 mins if blocked by HeroSMS."""
    try:
        client = get_http_client()
        resp = await client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 8, "id": act_id})
        if "EARLY_CANCEL_DENIED" in resp.text or "CANT_CANCEL" in resp.text or "WAIT_CODE" in resp.text:
            await asyncio.sleep(125) 
            await client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 8, "id": act_id})
    except Exception: pass

async def poll_for_incoming_otp(user_id: int, act_id: str, msg_id: int, service_name: str, cost: float, phone_num: str, state: FSMContext):
    max_seconds = 120  # 2 minutes total wait time
    sleep_interval = 5
    iterations = max_seconds // sleep_interval

    for i in range(iterations):
        await asyncio.sleep(sleep_interval)

        # 1. Check if user already clicked the manual Cancel button
        act_chk = await db_execute(supabase.table("activations").select("status").eq("activation_id", act_id))
        if not act_chk.data or act_chk.data[0]["status"] != "pending": 
            return

        try:
            client = get_http_client()
            resp = await client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getStatus", "id": act_id})
            if resp.text.startswith("STATUS_OK"):
                raw_msg = resp.text.split(':', 1)[1]

                # SMART DETECT ENGINE: Block Shopee SMS on generic numbers
                if service_name == "Any Other" and "shopee" in raw_msg.lower():
                    asyncio.create_task(background_cancel_hero_api(act_id))
                    await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))

                    try: await bot.delete_message(chat_id=user_id, message_id=msg_id)
                    except Exception: pass

                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🤖 **Smart Detect Activated!**\n\n"
                             f"⚠️ The network routed a Shopee OTP to this generic number. We have auto-closed this line to protect your account.\n\n"
                             f"Please lease a new number.",
                        parse_mode="Markdown"
                    )
                    return

                # Enhanced code extractor
                code_matches = re.findall(r'(?:code|pin|otp|is|passcode)[:\s-]*([0-9]{4,8})\b', raw_msg, re.IGNORECASE)
                if not code_matches:
                    code_matches = re.findall(r'\b\d{4,8}\b', raw_msg)
                verification_code = code_matches[0] if code_matches else "Code not found"

                ph_time = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%B %d, %Y - %I:%M %p (PHT)")

                await db_execute(supabase.table("activations").update({"status": "otp_received"}).eq("activation_id", act_id))

                try: await bot.delete_message(chat_id=user_id, message_id=msg_id)
                except Exception: pass

                ext_kb = InlineKeyboardBuilder()
                ext_kb.row(types.InlineKeyboardButton(text="🔁 Receive Another SMS (Extend)", callback_data=f"act_extend:{act_id}"))
                ext_kb.row(types.InlineKeyboardButton(text="✅ Close & Finish", callback_data=f"act_finish:{act_id}"))

                await bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 **OTP Received Successfully!**\n\n"
                         f"📱 **Phone Number:** `{phone_num}`\n\n"
                         f"📋 **Code:** `{verification_code}`\n\n"
                         f"📝 **Raw Message:**\n"
                         f"`{raw_msg}`\n\n"
                         f"🕒 **Date and Time:**\n{ph_time}",
                    reply_markup=ext_kb.as_markup(),
                    parse_mode="Markdown"
                )
                return
            elif "STATUS_WAIT_CODE" in resp.text: 
                if i > 0 and i % 3 == 0:
                    time_left = max_seconds - (i * sleep_interval)
                    control_kb = InlineKeyboardBuilder()
                    control_kb.row(
                        types.InlineKeyboardButton(text="❌ Cancel", callback_data=f"act_cancel:{act_id}"), 
                        types.InlineKeyboardButton(text="⚠️ Report Used", callback_data=f"act_ban:{act_id}")
                    )
                    try:
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=msg_id,
                            text=f"📱 **Line Lease Active**\n\n• Platform: `{service_name}`\n• Phone Number: `{phone_num}`\n\n⏳ Waiting for code... (**{time_left}s remaining**)",
                            reply_markup=control_kb.as_markup(),
                            parse_mode="Markdown"
                        )
                    except Exception: 
                        pass
        except Exception: 
            pass

    # AUTO-CANCEL ON TIMEOUT
    final_chk = await db_execute(supabase.table("activations").select("status").eq("activation_id", act_id))
    if final_chk.data and final_chk.data[0]["status"] == "pending":
        asyncio.create_task(background_cancel_hero_api(act_id))
        await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))

        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=f"⏰ **Timeout!**\n\nNo OTP was received within 2 minutes for `{phone_num}`. The line lease has been closed.\n\nYou can request another number anytime.",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )
        except Exception: 
            pass

@router.callback_query(F.data.startswith("act_"))
async def cb_lifecycle_mutation(callback: types.CallbackQuery, state: FSMContext):
    if not is_user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Access Denied. Contact Admin.", show_alert=True)
    action, act_id = callback.data.split(":")[0], callback.data.split(":")[1]
    user_id = callback.from_user.id

    chk = await db_execute(supabase.table("activations").select("*").eq("activation_id", act_id))
    if not chk.data:
        return await callback.answer("❌ This lease context is no longer active.", show_alert=True)

    act_data = chk.data[0]
    db_status = act_data["status"]
    refund_val = float(act_data.get("amount", 0.0))
    service_name = act_data["service"]
    phone_num = act_data["phone_number"]

    # 1. HANDLING CANCELLATION (BEFORE OTP ARRIVES)
    if action in ["act_cancel", "act_ban"]:
        if db_status != "pending":
            return await callback.answer("❌ This lease cannot be cancelled now.", show_alert=True)

        asyncio.create_task(background_cancel_hero_api(act_id))
        await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))

        action_text = "cancelled" if action == "act_cancel" else "reported as used & cancelled"
        await callback.answer("Number cancelled successfully.", show_alert=False)
        await callback.message.edit_text(
            f"✅ Number `{phone_num}` for service **{service_name}** has been {action_text}.\n\nYou can lease a new number anytime.",
            reply_markup=inline_back_keyboard(), parse_mode="Markdown"
        )
        return

    # 2. EXTEND NUMBER (REQUEST ANOTHER SMS FROM SAME NUMBER)
    elif action == "act_extend":
        if db_status not in ["otp_received", "completed", "pending"]:
            return await callback.answer("❌ This lease cannot be extended now.", show_alert=True)

        resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 3, "id": act_id})
        
        if any(ok in resp.text for ok in ["ACCESS_RETRY_GET", "ACCESS_ACTIVATION", "STATUS_WAIT_CODE"]) or resp.text.startswith("ACCESS"):
            await db_execute(supabase.table("activations").update({"status": "pending"}).eq("activation_id", act_id))

            control_kb = InlineKeyboardBuilder()
            control_kb.row(
                types.InlineKeyboardButton(text="❌ Cancel Extend", callback_data=f"act_cancelextend:{act_id}"),
                types.InlineKeyboardButton(text="✅ Close & Finish", callback_data=f"act_finish:{act_id}")
            )

            await callback.message.edit_text(
                f"📱 **Line Lease Extended**\n\n• Platform: `{service_name}`\n• Phone Number: `{phone_num}`\n\n⏳ Waiting for the **next** verification code to arrive... (**120s remaining**)",
                reply_markup=control_kb.as_markup(),
                parse_mode="Markdown"
            )
            asyncio.create_task(poll_for_incoming_otp(user_id, act_id, callback.message.message_id, service_name, refund_val, phone_num, state))
            await callback.answer("⏳ Extension activated. Waiting for next SMS...")
        else:
            await callback.answer(f"⚠️ Provider: {resp.text}", show_alert=True)
            await callback.message.edit_text(
                f"⚠️ **Could not extend number `{phone_num}`.**\n\nProvider response: `{resp.text}`\n(The session on the network may have already expired).",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )
        return

    # 3. CLOSE/FINISH OR CANCEL EXTEND
    elif action in ["act_finish", "act_cancelextend"]:
        await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 6, "id": act_id})
        await db_execute(supabase.table("activations").update({"status": "completed"}).eq("activation_id", act_id))

        if action == "act_cancelextend":
            await callback.message.edit_text(f"✅ Extension cancelled. Line `{phone_num}` is now closed.", reply_markup=inline_back_keyboard())
        else:
            try: await callback.message.edit_reply_markup(reply_markup=None)
            except Exception: pass
        await callback.answer("✅ Line closed successfully.")
        return

# ==========================================
# 👑 EXCLUSIVE ADMINISTRATION SUITE
# ==========================================

async def get_herosms_balance() -> str:
    if not HERO_SMS_KEY:
        return "Not Configured"
    try:
        client = get_http_client()
        resp = await client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getBalance"}, timeout=5.0)
        if resp.text.startswith("ACCESS_BALANCE:"):
            bal_usd = float(resp.text.split(":")[1])
            bal_php = bal_usd * USD_TO_PHP_RATE
            return f"${bal_usd:.2f} (~₱{bal_php:.2f})"
        return f"{resp.text}"
    except Exception:
        return "Offline/Unreachable"

def get_admin_dashboard_markup():
    kb = InlineKeyboardBuilder()
    # Row 1: Whitelist Users & Dispatches
    kb.row(
        types.InlineKeyboardButton(text="👥 Manage Whitelist Users", callback_data="admin_manage_users"),
        types.InlineKeyboardButton(text="🛒 Activation History", callback_data="admin_log_sales")
    )
    # Row 2: Provider Gateway & Broadcast Blast
    kb.row(
        types.InlineKeyboardButton(text="🌐 HeroSMS Provider Portal", url="https://hero-sms.com/user/balance"),
        types.InlineKeyboardButton(text="📢 Send Broadcast", callback_data="admin_trigger_broadcast")
    )
    # Row 3: Service Creation Tool
    kb.row(types.InlineKeyboardButton(text="➕ Add New Service", callback_data="adm_add_service"))

    for s_name, s_meta in SERVICES_PRICING.items():
        current_op = s_meta.get("operator", "any").upper()
        current_price = s_meta.get("price", 0.00)
        
        # Row 1: Network Operator Cycle
        kb.row(types.InlineKeyboardButton(text=f"⚙️ {s_name} Network: {current_op} 🔄", callback_data=f"adm_cycle_op:{s_name}"))
        # Row 2: Price Adjustment Tool & Deletion
        kb.row(
            types.InlineKeyboardButton(text=f"💰 Edit {s_name} (₱{current_price:.2f})", callback_data=f"adm_price:{s_name}"),
            types.InlineKeyboardButton(text="🗑️ Delete", callback_data=f"adm_del_srv:{s_name}")
        )
        
    return kb.as_markup()

async def generate_admin_stats_text():
    try:
        users_query = await db_execute(supabase.table("users").select("telegram_id"))
        activations_query = await db_execute(supabase.table("activations").select("activation_id, created_at"))
        hero_bal = await get_herosms_balance()

        total_users = len(users_query.data) if users_query.data else 0
        allowed_users_count = len(load_allowed_users())
        total_acts = len(activations_query.data) if activations_query.data else 0

        today_string = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_acts = 0
        if activations_query.data:
            for row in activations_query.data:
                if str(row.get("created_at", "")).startswith(today_string):
                    today_acts += 1

        return (
            "👑 **Keppo SMS Bot Administrator Console**\n\n"
            f"📊 **Live System & Provider Dashboard:**\n"
            f"• **HeroSMS Provider Balance:** `{hero_bal}`\n"
            f"• **Whitelisted Users (Unlimited):** `{allowed_users_count}`\n"
            f"• **Total Registered Accounts:** `{total_users}`\n"
            f"• **Dispatches Today:** `{today_acts}`\n"
            f"• **All-Time Dispatches:** `{total_acts}`\n\n"
            "Choose an administrative action below:"
        )
    except Exception as e: return f"👑 **Admin Console Error**\n⚠️ DB Error: `{e}`"

@router.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return 
    await state.clear()
    try: await message.delete()
    except Exception: pass

    stats_text = await generate_admin_stats_text()
    await message.answer(stats_text, reply_markup=get_admin_dashboard_markup(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_dashboard_home")
async def cb_admin_dashboard_home(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.clear()
    stats_text = await generate_admin_stats_text()
    await callback.message.edit_text(stats_text, reply_markup=get_admin_dashboard_markup(), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("adm_cycle_op:"))
async def cb_admin_cycle_op(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    s_name = callback.data.split(":")[1]

    if s_name in SERVICES_PRICING:
        ops = ["any", "smart", "globe", "tm"]
        current = SERVICES_PRICING[s_name].get("operator", "any")
        next_op = ops[(ops.index(current) + 1) % len(ops)]
        SERVICES_PRICING[s_name]["operator"] = next_op
        save_services()
        await callback.message.edit_reply_markup(reply_markup=get_admin_dashboard_markup())
        await callback.answer(f"{s_name} provider switched to {next_op.upper()}", show_alert=False)
    else:
        await callback.answer("Service not found.", show_alert=True)

# --- DYNAMIC PRICE ADJUSTMENT ENGINE ---
@router.callback_query(F.data.startswith("adm_price:"))
async def cb_admin_edit_price(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    s_name = callback.data.split(":")[1]
    
    await state.update_data(target_service=s_name)
    await state.set_state(AdminStates.waiting_for_new_price)
    
    current_price = SERVICES_PRICING[s_name]['price']
    await callback.message.edit_text(
        f"📈 **Adjust Pricing for {s_name}**\n\n"
        f"The current retail price is **₱{current_price:.2f}**.\n\n"
        "To update the price and allow the system to purchase more expensive HeroSMS nodes, type the new price (e.g., `6.50` or `7.00`) and send it to the bot:",
        reply_markup=admin_back_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_new_price)
async def process_admin_new_price(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    try:
        new_price = float(message.text)
        if new_price <= 0: raise ValueError
    except ValueError:
        return await message.answer("❌ Invalid format. Please type a valid number (e.g., `7.50`).")

    data = await state.get_data()
    s_name = data.get("target_service")
    await state.clear()

    try: await message.delete()
    except Exception: pass

    if s_name in SERVICES_PRICING:
        SERVICES_PRICING[s_name]["price"] = new_price
        save_services()
        success_text = f"✅ **Success!** Retail price for **{s_name}** has been updated to **₱{new_price:.2f}**."
    else:
        success_text = "❌ **Error:** Service profile could not be located in memory."

    stats_text = await generate_admin_stats_text()
    await message.answer(f"{success_text}\n\n{stats_text}", reply_markup=get_admin_dashboard_markup(), parse_mode="Markdown")

# --- ADD NEW SERVICE ENGINE ---
@router.callback_query(F.data == "adm_add_service")
async def cb_admin_add_service(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.waiting_for_new_service_name)
    await callback.message.edit_text(
        "➕ **Add New Service** (Step 1 of 3)\n\n"
        "Type the **Service Name** (e.g., `Telegram`, `WhatsApp`, `Shopee`, `Lazada`):",
        reply_markup=admin_back_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_new_service_name)
async def process_admin_new_service_name(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    srv_name = message.text.strip()
    if not srv_name or len(srv_name) > 30:
        return await message.answer("❌ Invalid length. Please enter a service name under 30 characters:")

    await state.update_data(new_srv_name=srv_name)
    await state.set_state(AdminStates.waiting_for_new_service_code)
    try: await message.delete()
    except Exception: pass

    await message.answer(
        f"➕ **Add New Service** (Step 2 of 3)\n\n"
        f"Service Name: **{srv_name}**\n\n"
        "Now enter the **HeroSMS Service Shortcode** (e.g. `tg` for Telegram, `wa` for WhatsApp, `ka` for Shopee, `ot` for Any Other):\n\n"
        "*(Check `services_list.txt` in your project folder to find official codes)*",
        reply_markup=admin_back_keyboard(),
        parse_mode="Markdown"
    )

@router.message(AdminStates.waiting_for_new_service_code)
async def process_admin_new_service_code(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    srv_code = message.text.strip().lower()
    if not srv_code:
        return await message.answer("❌ Shortcode cannot be empty. Please enter a valid code (e.g. `ot` or `tg`):")

    await state.update_data(new_srv_code=srv_code)
    await state.set_state(AdminStates.waiting_for_new_service_price)
    try: await message.delete()
    except Exception: pass

    data = await state.get_data()
    srv_name = data.get("new_srv_name")

    await message.answer(
        f"➕ **Add New Service** (Step 3 of 3)\n\n"
        f"Service: **{srv_name}** (Code: `{srv_code}`)\n\n"
        "Finally, enter the **Retail Price in PHP** (e.g., `6.00` or `7.50`):",
        reply_markup=admin_back_keyboard(),
        parse_mode="Markdown"
    )

@router.message(AdminStates.waiting_for_new_service_price)
async def process_admin_new_service_price(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        price = float(message.text.strip())
        if price <= 0: raise ValueError
    except ValueError:
        return await message.answer("❌ Invalid price format. Please enter a valid number (e.g. `6.50`):")

    data = await state.get_data()
    srv_name = data.get("new_srv_name")
    srv_code = data.get("new_srv_code")
    await state.clear()
    try: await message.delete()
    except Exception: pass

    SERVICES_PRICING[srv_name] = {
        "code": srv_code,
        "price": price,
        "stock": 100,
        "operator": "any"
    }
    save_services()

    stats_text = await generate_admin_stats_text()
    await message.answer(
        f"🎉 **Service Added Successfully!**\n\n"
        f"• **Name:** `{srv_name}`\n"
        f"• **Code:** `{srv_code}`\n"
        f"• **Price:** `₱{price:.2f}`\n"
        f"• **Network:** `ANY`\n\n"
        f"This service is now active and immediately available in **📞 Buy Numbers**!\n\n{stats_text}",
        reply_markup=get_admin_dashboard_markup(),
        parse_mode="Markdown"
    )

# --- DELETE SERVICE ENGINE ---
@router.callback_query(F.data.startswith("adm_del_srv:"))
async def cb_admin_delete_service(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    s_name = callback.data.split(":")[1]

    if s_name in SERVICES_PRICING:
        del SERVICES_PRICING[s_name]
        save_services()
        await callback.answer(f"Service '{s_name}' deleted.", show_alert=False)
    else:
        await callback.answer("Service not found.", show_alert=True)

    stats_text = await generate_admin_stats_text()
    await callback.message.edit_text(stats_text, reply_markup=get_admin_dashboard_markup(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("admin_log_"))
async def cb_admin_logs(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    action = callback.data.split("_")[2]

    try:
        if action == "sales":
            res = await db_execute(supabase.table("activations").select("*").order("activation_id", desc=True).limit(15))
            text = "🛒 **Recent OTP Line Activations:**\n\n"
            if not res.data:
                text += "No activations recorded yet."
            else:
                for row in res.data: 
                    st = row.get('status', 'unknown')
                    ph = row.get('phone_number', 'N/A')
                    srv = row.get('service', 'Service')
                    uid = row.get('telegram_id', 'Unknown')
                    text += f"• **{srv}** (`{ph}`) | [{st.upper()}] | User: `{uid}`\n"
            await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="Markdown")
        else:
            await callback.message.edit_text("ℹ️ Log section not found.", reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    except Exception as e:
        await callback.message.edit_text(f"⚠️ **Database Error Detected:**\n`{str(e)}`", reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    finally:
        await callback.answer()

# ==========================================
# 👥 USER ACCESS MANAGEMENT (WHITELIST - UNLIMITED)
# ==========================================

async def render_user_management_screen(target_message: types.Message, is_edit: bool = True):
    allowed_ids = load_allowed_users()
    
    text = (
        "👥 **Whitelist User Management (Unlimited Access)**\n\n"
        "Whitelisted users have **100% UNLIMITED access** to all services (*Buy Numbers*, *Extend Numbers*).\n"
        "They do NOT need wallet balance or approvals.\n\n"
    )
    
    if not allowed_ids:
        text += "🔒 *No public users currently whitelisted. (Only Admin has access)*\n"
    else:
        text += f"✅ **Whitelisted Users ({len(allowed_ids)}):**\n"
        for uid in sorted(allowed_ids):
            text += f"• User ID: `{uid}` (Unlimited)\n"

    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="➕ Whitelist User by Telegram ID", callback_data="adm_user_add_prompt"))
    
    for uid in sorted(allowed_ids):
        kb.row(types.InlineKeyboardButton(text=f"🗑️ Revoke Access: {uid}", callback_data=f"adm_user_revoke:{uid}"))
            
    kb.row(types.InlineKeyboardButton(text="🔙 Back to Admin Dashboard", callback_data="admin_dashboard_home"))

    if is_edit:
        await target_message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
    else:
        await target_message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_manage_users")
async def cb_admin_manage_users(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.clear()
    await render_user_management_screen(callback.message, is_edit=True)
    await callback.answer()

@router.callback_query(F.data == "adm_user_add_prompt")
async def cb_admin_user_add_prompt(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.waiting_for_add_user_id)
    await callback.message.edit_text(
        "➕ **Whitelist New User (Unlimited Access)**\n\n"
        "Please type and send the **Telegram ID** (numeric) of the user you want to authorize:\n\n"
        "*(The user can find their Telegram ID by starting the bot or forwarding a message to @userinfobot)*",
        reply_markup=admin_back_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.waiting_for_add_user_id)
async def process_admin_add_user_input(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        new_uid = int(message.text.strip())
        if new_uid <= 0: raise ValueError
    except ValueError:
        return await message.answer("❌ Invalid format. Please send a valid numeric Telegram ID (e.g. `1234567890`).")

    await state.clear()
    try: await message.delete()
    except Exception: pass

    add_allowed_user(new_uid)
    
    # Notify authorized user
    try:
        await bot.send_message(
            chat_id=new_uid,
            text=(
                "🎉 **Access Granted!**\n\n"
                "The Administrator has authorized your account for **Unlimited Access**!\n"
                "You can now use **📞 Buy Numbers** and **🔁 Extend Numbers** freely."
            ),
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await message.answer(f"✅ User `{new_uid}` is now **WHITELISTED** with **Unlimited Access**.", parse_mode="Markdown")
    await render_user_management_screen(message, is_edit=False)

@router.callback_query(F.data.startswith("adm_user_revoke:"))
async def cb_admin_user_revoke(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_uid = int(callback.data.split(":")[1])
    remove_allowed_user(target_uid)
    
    await callback.answer(f"User {target_uid} removed from whitelist.", show_alert=False)
    
    try:
        await bot.send_message(
            chat_id=target_uid,
            text="⚠️ **Access Update:** Your access to this bot has been revoked by the Administrator.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except Exception:
        pass

    await render_user_management_screen(callback.message, is_edit=True)

@router.callback_query(F.data == "admin_trigger_broadcast")
async def cb_admin_trigger_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("📢 **Global Announcement Engine**\n\nType and send the message text you wish to blast out to **all** users.", reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast)
async def process_global_broadcast_blast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    broadcast_text = message.text
    await state.clear()
    try: await message.delete()
    except Exception: pass

    progress_msg = await message.answer("⚡ **Retrieving target data rows... Commencing mass dispatch sequence...**")
    users_pool = await db_execute(supabase.table("users").select("telegram_id"))

    if not users_pool.data: return await progress_msg.edit_text("❌ Broadcast aborted: No registered users found inside database records.", reply_markup=admin_back_keyboard())

    success_count, failure_count = 0, 0
    for user in users_pool.data:
        target_chat_id = user["telegram_id"]
        if target_chat_id == ADMIN_ID: continue
        try:
            await bot.send_message(chat_id=target_chat_id, text=broadcast_text, parse_mode="Markdown")
            success_count += 1
            await asyncio.sleep(0.05) 
        except TelegramAPIError: failure_count += 1

    await progress_msg.edit_text(
        "✅ **Global Dispatch Routine Complete**\n\n"
        f"📊 **Summary Ledger Metrics:**\n"
        f"• **Successfully Delivered:** `{success_count}` accounts\n"
        f"• **Delivery Drops/Blocked:** `{failure_count}` accounts",
        reply_markup=admin_back_keyboard(), parse_mode="Markdown"
    )

# ==========================================
# 🌍 HEALTH CHECK WORKER TERMINAL
# ==========================================

def run_health_check_server():
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot operational.")
        def log_message(self, format, *args): return 
    port = int(os.getenv("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_health_check_server, daemon=True).start()
    dp.include_router(router)
    dp.startup.register(on_startup)
    asyncio.run(dp.start_polling(bot))
