import os
import asyncio
import httpx
import threading
import logging
import re
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
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
HERO_SMS_KEY = os.getenv("HERO_SMS_API_KEY", "")
HERO_SMS_URL = os.getenv("HERO_SMS_BASE_URL", "https://hero-sms.com/stubs/handler_api.php")

# --- GLOBAL CONFIGURATION METRICS ---
PH_COUNTRY_ID = 4 
USD_TO_PHP_RATE = 60.50  

# --- PRICING, STOCKS & OPERATOR PROFILES ---
SERVICES_PRICING = {
    "PHBINGO": {"code": "ot", "price": 5.00, "stock": 100, "operator": "any"},     
    "TARA 777": {"code": "ot", "price": 5.00, "stock": 100, "operator": "any"}  
}

# --- INITIALIZE CORE CLIENTS ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
http_client: httpx.AsyncClient = None

# --- STATE MACHINES ---
class TopUpStates(StatesGroup):
    waiting_for_receipt = State()

class AdminStates(StatesGroup):
    waiting_for_approved_amount = State()
    waiting_for_broadcast = State()
    waiting_for_new_price = State() # NEW: State for dynamically updating prices

# --- NON-BLOCKING DATABASE WORKER ---
async def db_execute(query_chain):
    return await asyncio.to_thread(query_chain.execute)

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
        [types.KeyboardButton(text="📞 Buy Numbers"), types.KeyboardButton(text="💵 Price List")],
        [types.KeyboardButton(text="💰 Balance"), types.KeyboardButton(text="📦 Stocks")],
        [types.KeyboardButton(text="💳 Top Up Balance")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard_grid, resize_keyboard=True, is_persistent=True)

def inline_back_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Close Menu", callback_data="menu_main"))
    return builder.as_markup()

def cancel_topup_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="❌ Cancel Top Up", callback_data="cancel_topup"))
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
    await bot.set_my_commands(admin_cmds, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))

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
    await message.answer("Welcome to OMAR OTP Bot!\n\nUse the menu buttons below to navigate.", reply_markup=main_menu_keyboard())

@router.message(Command("buy_again"))
async def cmd_buy_again(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id
    res = await db_execute(supabase.table("activations").select("service").eq("telegram_id", tg_id).order("activation_id", descending=True).limit(1))

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

@router.message(F.text == "💰 Balance")
async def menu_balance_text(message: types.Message, state: FSMContext):
    res = await db_execute(supabase.table("users").select("balance").eq("telegram_id", message.from_user.id))
    balance = res.data[0]["balance"] if res.data else 0.00
    await send_or_edit_transient(message, state, f"💳 **Your Current Account Balance:** ₱{balance:.2f}", inline_back_keyboard())

@router.message(F.text == "💵 Price List")
async def menu_prices_text(message: types.Message, state: FSMContext):
    text = "📋 **Current Platform Pricing Profiles:**\n\n"
    for s_name, s_info in SERVICES_PRICING.items(): 
        op_tag = f" *(Provider: {s_info['operator'].upper()})*" if s_info['operator'] != "any" else ""
        text += f"• **{s_name}**: ₱{s_info['price']:.2f}{op_tag}\n"
    await send_or_edit_transient(message, state, text, inline_back_keyboard())

@router.message(F.text == "📦 Stocks")
async def menu_stocks_text(message: types.Message, state: FSMContext):
    text = "📊 **Live Available SIM Pools:**\n\n"
    for s_name, s_info in SERVICES_PRICING.items(): 
        op_tag = f" *(Provider: {s_info['operator'].upper()})*" if s_info['operator'] != "any" else ""
        text += f"• **{s_name}**: `{s_info['stock']}` pcs available{op_tag}\n"
    await send_or_edit_transient(message, state, text, inline_back_keyboard())

# ==========================================
# 💳 SECURE TOP UP PIPELINE MODULE
# ==========================================

@router.message(F.text == "💳 Top Up Balance")
async def menu_topup_text(message: types.Message, state: FSMContext):
    text = (
        "💸 **Manual Balance Realization Route**\n\n"
        "1. Remit your desired amount through GCash to: `09563495650` (Account: OMAR A)\n"
        "2. Save the screenshot of the transaction receipt.\n\n"
        "📸 **Please upload/send the image of your GCash receipt now:**\n*(Or click Cancel below to abort)*"
    )
    await send_or_edit_transient(message, state, text, cancel_topup_keyboard())
    await state.set_state(TopUpStates.waiting_for_receipt)

@router.callback_query(F.data == "cancel_topup")
async def cb_cancel_topup(callback: types.CallbackQuery, state: FSMContext):
    await state.clear() 
    try: await callback.message.delete()
    except Exception: pass
    await callback.answer("Top-up cancelled successfully.", show_alert=False)

@router.message(TopUpStates.waiting_for_receipt, F.text)
async def reject_text_receipt(message: types.Message, state: FSMContext):
    await send_or_edit_transient(message, state, "❌ **Invalid format.** Please upload the actual **image/screenshot** of your receipt.", cancel_topup_keyboard())

@router.message(TopUpStates.waiting_for_receipt, F.photo)
async def process_topup_receipt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    photo_id = message.photo[-1].file_id

    try: await message.delete()
    except Exception: pass
    await state.clear() 

    admin_kb = InlineKeyboardBuilder()
    admin_kb.row(types.InlineKeyboardButton(text="🟢 Input Amount & Approve", callback_data=f"adm_start_app:{user_id}"))
    admin_kb.row(types.InlineKeyboardButton(text="🔴 Deny Fraud/Invalid", callback_data=f"adm_den:{user_id}"))

    await bot.send_photo(
        chat_id=ADMIN_ID, photo=photo_id,
        caption=f"🚨 **Inbound Top-Up Receipt for Audit**\n\n• **User ID:** `{user_id}`\n• **Username:** @{username}\n\nPlease review the attached receipt.",
        reply_markup=admin_kb.as_markup(), parse_mode="Markdown"
    )

    confirm_msg = await message.answer("⏳ **Receipt uploaded successfully!**\nDispatched to the Admin for manual verification.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await track_ui(confirm_msg, state)

@router.callback_query(F.data.startswith("adm_start_app:"))
async def cb_admin_start_approve(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    target_user_id = int(callback.data.split(":")[1])
    await state.update_data(target_user=target_user_id)
    await state.set_state(AdminStates.waiting_for_approved_amount)
    await callback.message.answer(f"✅ **Approving Top-Up for User `{target_user_id}`**\n\nType the exact amount in PHP to credit to their account:", parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_approved_amount)
async def process_admin_amount_input(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        amount = float(message.text)
        if amount <= 0: raise ValueError
    except ValueError:
        return await message.answer("❌ Invalid numeric format. Type a valid number (e.g. 100).")

    data = await state.get_data()
    target_user_id = data.get("target_user")
    await state.clear()

    try: await message.delete()
    except Exception: pass

    db_call = await db_execute(supabase.rpc("refund_balance", {"user_id": target_user_id, "refund_amount": amount}))
    new_bal = db_call.data if db_call.data else amount
    await db_execute(supabase.table("topup_logs").insert({"telegram_id": target_user_id, "amount": amount}))

    await bot.send_message(chat_id=target_user_id, text=f"🎉 **Your top-up receipt was APPROVED!**\n\n💰 Added: ₱{amount:.2f}\n💳 Current Balance: ₱{new_bal:.2f}", parse_mode="Markdown")
    await message.answer(f"✅ Successfully injected ₱{amount:.2f} into User `{target_user_id}`'s account.", parse_mode="Markdown")

@router.callback_query(F.data.startswith("adm_den:"))
async def cb_admin_deny(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_user_id = int(callback.data.split(":")[1])
    await bot.send_message(chat_id=target_user_id, text="❌ **Your Top-Up receipt was declined.** Please ensure the image is clear and valid.", parse_mode="Markdown")
    await callback.message.edit_caption(caption=f"❌ **DENIED**\nReceipt verification request rejected for user `{target_user_id}`.", parse_mode="Markdown")
    await callback.answer()

# ==========================================
# 📱 MOBILE INFRASTRUCTURE ALLOCATION LINE
# ==========================================

@router.message(F.text == "📞 Buy Numbers")
async def menu_buy_text(message: types.Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    for s_name, s_meta in SERVICES_PRICING.items():
        builder.row(types.InlineKeyboardButton(text=f"📱 Get {s_name} (₱{s_meta['price']:.2f})", callback_data=f"buy_srv:{s_name}"))
    builder.row(types.InlineKeyboardButton(text="🔙 Close Menu", callback_data="menu_main"))

    await send_or_edit_transient(message, state, "Select target application platform integration context:", builder.as_markup())

@router.callback_query(F.data.startswith("buy_srv:"))
async def cb_execute_purchase(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await initiate_number_purchase(callback.message, callback.from_user.id, callback.data.split(":")[1], is_callback=True, state=state)

async def initiate_number_purchase(msg: types.Message, user_id: int, service_name: str, is_callback: bool, state: FSMContext):
    service_meta = SERVICES_PRICING[service_name]
    cost = service_meta["price"]
    service_code = service_meta["code"]
    target_operator = service_meta.get("operator", "any")

    if service_meta["stock"] <= 0:
        text = f"⚠️ **No stocks available!**\n\nThere are currently no lines left in stock for **{service_name}**. Please check back later."
        if is_callback: await msg.edit_text(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
        else: await send_or_edit_transient(msg, state, text, inline_back_keyboard())
        return

    is_allowed = await db_execute(supabase.rpc("deduct_balance", {"user_id": user_id, "cost": cost}))
    if not is_allowed.data:
        text = "❌ **Insufficient credit allocation context. Please perform a Top-Up.**"
        if is_callback: await msg.edit_text(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
        else: await send_or_edit_transient(msg, state, text, inline_back_keyboard())
        return

    service_meta["stock"] -= 1
    text_loading = f"⏳ **Allocating {target_operator.upper()} network line... Please wait.**"

    if is_callback:
        await msg.edit_text(text_loading, parse_mode="Markdown")
        status_msg = msg
    else:
        status_msg = await bot.send_message(msg.chat.id, text_loading, parse_mode="Markdown")
        await track_ui(status_msg, state)

    try:
        cheapest_operator = ""
        try:
            price_resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getPrices", "service": service_code, "country": PH_COUNTRY_ID})
            price_data = price_resp.json()
            country_str = str(PH_COUNTRY_ID)

            if country_str in price_data and service_code in price_data[country_str]:
                service_nodes = price_data[country_str][service_code]
                lowest_market_cost_php = float('inf')

                for op_name, op_info in service_nodes.items():
                    if op_info.get("count", 0) > 0:
                        if target_operator != "any" and op_name.lower() != target_operator.lower(): continue
                        op_cost_usd = float(op_info.get("cost", 0))
                        op_cost_php = op_cost_usd * USD_TO_PHP_RATE
                        if op_cost_php < lowest_market_cost_php:
                            lowest_market_cost_php = op_cost_php
                            cheapest_operator = op_name

                if lowest_market_cost_php != float('inf') and lowest_market_cost_php > cost:
                    raise Exception("Wholesale price inflation exceeded retail limits.")
        except Exception as api_err:
            if "inflation exceeded" in str(api_err): raise api_err
            cheapest_operator = "" 

        api_params = {"api_key": HERO_SMS_KEY, "action": "getNumber", "service": service_code, "country": PH_COUNTRY_ID}
        request_operator = cheapest_operator if target_operator == "any" else target_operator
        if request_operator and request_operator != "any":
            api_params["operator"] = request_operator

        resp = await http_client.get(HERO_SMS_URL, params=api_params)

        if resp.text.startswith("ACCESS_NUMBER"):
            _, act_id, raw_phone = resp.text.split(":")
            ph_clean = format_ph_number(raw_phone)

            await db_execute(supabase.table("activations").insert({"activation_id": act_id, "telegram_id": user_id, "service": service_name, "amount": cost, "phone_number": ph_clean, "status": "pending"}))

            control_kb = InlineKeyboardBuilder()
            control_kb.row(
                types.InlineKeyboardButton(text="❌ Cancel", callback_data=f"act_cancel:{act_id}"), 
                types.InlineKeyboardButton(text="⚠️ Report Used", callback_data=f"act_ban:{act_id}")
            )

            await status_msg.edit_text(
                f"📱 **Line Lease Active**\n\n• Platform: `{service_name}`\n• Provider: `{request_operator.upper()}`\n• Phone Number: `{ph_clean}`\n\n⏳ Waiting for verification code to arrive... (**120s remaining**)", 
                reply_markup=control_kb.as_markup(), parse_mode="Markdown"
            )
            asyncio.create_task(poll_for_incoming_otp(user_id, act_id, status_msg.message_id, service_name, cost, ph_clean, state))

        elif "NO_NUMBERS" in resp.text:
            service_meta["stock"] += 1 
            await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
            await status_msg.edit_text(f"⚠️ **Out of Stock!**\n\nNo available allocations for **{service_name}** in this region. Your ₱{cost:.2f} has been credited back.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
        else:
            raise Exception(f"Validation Failure/No Balance: {resp.text}")

    except Exception as e:
        service_meta["stock"] += 1 
        await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
        await status_msg.edit_text(f"❌ **System Notice:** Number allocation failed or node busy. Balance refunded.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")

# ==========================================
# ⏳ BACKGROUND SMS MONITORING & FIRE-AND-FORGET
# ==========================================

async def background_cancel_hero_api(act_id: str):
    """Silently attempts to cancel the API lease. Waits the required 2 mins if blocked by HeroSMS."""
    try:
        resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 8, "id": act_id})
        if "EARLY_CANCEL_DENIED" in resp.text or "CANT_CANCEL" in resp.text or "WAIT_CODE" in resp.text:
            await asyncio.sleep(125) 
            await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 8, "id": act_id})
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
            return # Stop the background loop immediately to prevent double refunds

        try:
            resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getStatus", "id": act_id})
            if resp.text.startswith("STATUS_OK"):
                raw_msg = resp.text.split(':', 1)[1]

                # 🤖 SMART DETECT ENGINE: Block Shopee SMS on generic numbers
                if service_name == "Any Other" and "shopee" in raw_msg.lower():
                    asyncio.create_task(background_cancel_hero_api(act_id))
                    await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))
                    await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
                    if service_name in SERVICES_PRICING: SERVICES_PRICING[service_name]["stock"] += 1

                    try: await bot.delete_message(chat_id=user_id, message_id=msg_id)
                    except Exception: pass

                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🤖 **Smart Detect Activated!**\n\n"
                             f"⚠️ The network routed a Shopee OTP to this number. Because Shopee actively bans generic numbers, we have auto-blocked this line to protect your account.\n\n"
                             f"💰 **We instantly refunded your ₱{cost:.2f}.** Please try leasing a new number.",
                        parse_mode="Markdown"
                    )
                    return

                # Normal Successful OTP Process with Regex Code Extraction
                # Finds an isolated sequence of 4 to 8 digits
                code_matches = re.findall(r'\b\d{4,8}\b', raw_msg)
                verification_code = code_matches[0] if code_matches else "Code not found"

                ph_time = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%B %d, %Y - %I:%M %p (PHT)")

                await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 6, "id": act_id})
                await db_execute(supabase.table("activations").update({"status": "completed"}).eq("activation_id", act_id))

                try: await bot.delete_message(chat_id=user_id, message_id=msg_id)
                except Exception: pass

                await bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 **OTP Received Successfully!**\n\n"
                         f"📱 **Phone Number:** `{phone_num}`\n\n"
                         f"📋 **Code:** `{verification_code}`\n\n"
                         f"📝 **Raw Message:**\n"
                         f"`{raw_msg}`\n\n"
                         f"🕒 **Date and Time:**\n{ph_time}",
                    parse_mode="Markdown"
                )
                return
            elif "STATUS_WAIT_CODE" in resp.text: 
                # 🕒 Update the countdown timer on the UI every 15 seconds to avoid Telegram Rate Limits
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
                        pass # Ignore errors if message text is identical or was deleted
        except Exception: 
            pass

    # 🛑 AUTO-CANCEL: If the loop finishes 120 seconds and no OTP was found
    final_chk = await db_execute(supabase.table("activations").select("status").eq("activation_id", act_id))
    if final_chk.data and final_chk.data[0]["status"] == "pending":

        # Restore stock and refund
        if service_name in SERVICES_PRICING: SERVICES_PRICING[service_name]["stock"] += 1

        asyncio.create_task(background_cancel_hero_api(act_id))
        await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))
        await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))

        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=f"⏰ **Timeout!**\n\nNo OTP was received within 2 minutes for `{phone_num}`. The lease has been automatically cancelled.\n\n💰 **₱{cost:.2f} has been refunded to your wallet.**",
                reply_markup=inline_back_keyboard(),
                parse_mode="Markdown"
            )
        except Exception: 
            pass

@router.callback_query(F.data.startswith("act_"))
async def cb_lifecycle_mutation(callback: types.CallbackQuery, state: FSMContext):
    action, act_id = callback.data.split(":")[0], callback.data.split(":")[1]
    user_id = callback.from_user.id

    chk = await db_execute(supabase.table("activations").select("*").eq("activation_id", act_id))
    if not chk.data:
        return await callback.answer("❌ This lease context is no longer active.", show_alert=True)

    act_data = chk.data[0]
    db_status = act_data["status"]
    refund_val = float(act_data["amount"])
    service_name = act_data["service"]
    phone_num = act_data["phone_number"]

    # ONLY handling original cancel/ban logic (No extension logic)
    if action in ["act_cancel", "act_ban"]:
        if db_status != "pending":
            return await callback.answer("❌ This lease cannot be cancelled now.", show_alert=True)

        if service_name in SERVICES_PRICING: SERVICES_PRICING[service_name]["stock"] += 1

        asyncio.create_task(background_cancel_hero_api(act_id))
        await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))
        await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": refund_val}))

        action_text = "cancelled" if action == "act_cancel" else "reported as used & cancelled"
        await callback.answer("Wallet balance refunded successfully.", show_alert=False)
        await callback.message.edit_text(
            f"✅ Number `{phone_num}` for service **{service_name}** has been successfully {action_text}.\n\n"
            f"• Value ₱{refund_val:.2f} was restored safely to your internal balance allocation profile.",
            reply_markup=inline_back_keyboard(), parse_mode="Markdown"
        )
        return

# ==========================================
# 👑 EXCLUSIVE ADMINISTRATION SUITE
# ==========================================

def get_admin_dashboard_markup():
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="📢 Send Announcement Blast", callback_data="admin_trigger_broadcast"))
    kb.row(
        types.InlineKeyboardButton(text="📥 Recent Top-Ups", callback_data="admin_log_topups"),
        types.InlineKeyboardButton(text="🛒 Recent Sales", callback_data="admin_log_sales")
    )
    for s_name, s_meta in SERVICES_PRICING.items():
        current_op = s_meta.get("operator", "any").upper()
        current_price = s_meta.get("price", 0.00)
        
        # Row 1: Network Operator Cycle
        kb.row(types.InlineKeyboardButton(text=f"⚙️ {s_name} Network: {current_op} 🔄", callback_data=f"adm_cycle_op:{s_name}"))
        # Row 2: Price Adjustment Tool
        kb.row(types.InlineKeyboardButton(text=f"💰 Adjust {s_name} Price (Current: ₱{current_price:.2f})", callback_data=f"adm_price:{s_name}"))
        
    return kb.as_markup()

async def generate_admin_stats_text():
    try:
        users_query = await db_execute(supabase.table("users").select("telegram_id"))
        topups_query = await db_execute(supabase.table("topup_logs").select("amount", "created_at"))

        total_users = len(users_query.data) if users_query.data else 0
        all_time_total, today_total = 0.00, 0.00
        today_string = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if topups_query.data:
            for row in topups_query.data:
                amt = float(row["amount"])
                all_time_total += amt
                if row.get("created_at", "").startswith(today_string): today_total += amt

        return (
            "👑 **TeleBOT System Administrator Console**\n\n"
            f"📊 **Live Revenue Dashboard:**\n"
            f"• **Today's Gross Top-Ups:** ₱`{today_total:.2f}`\n"
            f"• **All-Time Gross Top-Ups:** ₱`{all_time_total:.2f}`\n"
            f"• **Total Registered Users:** `{total_users}`\n\n"
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

    ops = ["any", "smart", "globe", "tm"]
    current = SERVICES_PRICING[s_name].get("operator", "any")
    next_op = ops[(ops.index(current) + 1) % len(ops)]
    SERVICES_PRICING[s_name]["operator"] = next_op

    await callback.message.edit_reply_markup(reply_markup=get_admin_dashboard_markup())
    await callback.answer(f"{s_name} provider switched to {next_op.upper()}", show_alert=False)

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
        success_text = f"✅ **Success!** Retail price for **{s_name}** has been updated to **₱{new_price:.2f}**."
    else:
        success_text = "❌ **Error:** Service profile could not be located in memory."

    stats_text = await generate_admin_stats_text()
    await message.answer(f"{success_text}\n\n{stats_text}", reply_markup=get_admin_dashboard_markup(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("admin_log_"))
async def cb_admin_logs(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    action = callback.data.split("_")[2]

    try:
        if action == "topups":
            res = await db_execute(supabase.table("topup_logs").select("*").limit(10))
            text = "📥 **Latest Approved Wallet Injections:**\n\n"
            if not res.data: text += "No top-ups recorded yet."
            else:
                for row in reversed(res.data): 
                    date_str = str(row.get('created_at', 'Unknown'))[:10] if row.get('created_at') else 'N/A'
                    text += f"• **User:** `{row.get('telegram_id', 'Unknown')}` | **₱{row.get('amount', 0)}** | {date_str}\n"
        elif action == "sales":
            res = await db_execute(supabase.table("activations").select("*").eq("status", "completed").limit(10))
            text = "🛒 **Latest Completed OTP Dispatches:**\n\n"
            if not res.data: text += "No completed sales yet."
            else:
                for row in reversed(res.data): 
                    text += f"• **{row.get('service', 'Service')}** | Cost: ₱{row.get('amount', 0)} | **User:** `{row.get('telegram_id', 'Unknown')}`\n"
        await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    except Exception as e:
        await callback.message.edit_text(f"⚠️ **Database Error Detected:**\n`{str(e)}`", reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    finally:
        await callback.answer()

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
