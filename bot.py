import os
import asyncio
import httpx
import threading
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HERO_SMS_KEY = os.getenv("HERO_SMS_API_KEY")
HERO_SMS_URL = os.getenv("HERO_SMS_BASE_URL")

# --- GLOBAL CONFIGURATION METRICS ---
PH_COUNTRY_ID = 4 
USD_TO_PHP_RATE = 60.50  

# --- PRICING & LIVE DYNAMIC STOCK PROFILES ---
SERVICES_PRICING = {
    "Shopee": {"code": "ds", "price": 15.00, "stock": 50},     
    "GCash": {"code": "ot", "price": 10.00, "stock": 50},      
    "Any Other": {"code": "ot", "price": 5.00, "stock": 50}   
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

# --- NON-BLOCKING DATABASE WORKER ---
async def db_execute(query_chain):
    """Isolates blocking network I/O calls to Supabase into dedicated threads."""
    return await asyncio.to_thread(query_chain.execute)

# --- UTILITY STRING WORKERS ---
def format_ph_number(raw_num: str) -> str:
    """Cleans phone numbers into a standardized national formatting layout."""
    cleaned = raw_num.strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("63"): cleaned = cleaned[2:]
    if cleaned.startswith("0"): cleaned = cleaned[1:]
    return cleaned

# ==========================================
# 📱 UI KEYBOARD FACTORIES
# ==========================================

def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    """Builds a persistent input area keyboard layout matching image 1144.png."""
    keyboard_grid = [
        [types.KeyboardButton(text="📞 Buy Numbers"), types.KeyboardButton(text="💵 Price List")],
        [types.KeyboardButton(text="💰 Balance"), types.KeyboardButton(text="📦 Stocks")],
        [types.KeyboardButton(text="💳 Top Up Balance")]
    ]
    return types.ReplyKeyboardMarkup(
        keyboard=keyboard_grid,
        resize_keyboard=True,
        is_persistent=True
    )

def inline_back_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_main"))
    return builder.as_markup()

def admin_back_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Admin Dashboard", callback_data="admin_dashboard_home"))
    return builder.as_markup()

# ==========================================
# 🔥 SMART CONTEXT CLEANING ENGINE
# ==========================================

async def track_transient_ui(msg: types.Message, state: FSMContext):
    """Saves transient message IDs within the current session lifecycle."""
    await state.update_data(last_transient_msg_id=msg.message_id)

async def burn_transient_ui(chat_id: int, state: FSMContext):
    """Deletes temporary bot responses when navigating across menu items."""
    session_data = await state.get_data()
    msg_id = session_data.get("last_transient_msg_id")
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        await state.update_data(last_transient_msg_id=None)

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
    await message.answer("Welcome to OMAR OTP Bot!\n\nTap any of the options below to get started.", reply_markup=main_menu_keyboard())

@router.message(Command("buy_again"))
async def cmd_buy_again(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id
    try: await message.delete()
    except Exception: pass
    
    await burn_transient_ui(message.chat.id, state)
    res = await db_execute(supabase.table("activations").select("service").eq("telegram_id", tg_id).order("activation_id", descending=True).limit(1))
    
    if not res.data:
        err_msg = await message.answer("❌ No previous transaction histories found to clone.", reply_markup=inline_back_keyboard())
        await track_transient_ui(err_msg, state)
        return
        
    await initiate_number_purchase(message, tg_id, res.data[0]["service"], is_callback=False, state=state)

@router.callback_query(F.data == "menu_main")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await burn_transient_ui(callback.message.chat.id, state)
    menu_msg = await callback.message.answer("Welcome to OMAR OTP Bot!\n\nTap any of the options below to get started.", reply_markup=main_menu_keyboard())
    await track_transient_ui(menu_msg, state)

@router.message(F.text == "💰 Balance")
async def menu_balance_text(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass
    
    await burn_transient_ui(message.chat.id, state)
    res = await db_execute(supabase.table("users").select("balance").eq("telegram_id", message.from_user.id))
    balance = res.data[0]["balance"] if res.data else 0.00
    
    out_msg = await message.answer(f"💳 **Your Current Account Balance:** ₱{balance:.2f}", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await track_transient_ui(out_msg, state)

@router.message(F.text == "💵 Price List")
async def menu_prices_text(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass
    
    await burn_transient_ui(message.chat.id, state)
    text = "📋 **Current Platform Pricing Profiles:**\n\n"
    for s_name, s_info in SERVICES_PRICING.items(): 
        text += f"• **{s_name}**: ₱{s_info['price']:.2f}\n"
        
    out_msg = await message.answer(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await track_transient_ui(out_msg, state)

@router.message(F.text == "📦 Stocks")
async def menu_stocks_text(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass
    
    await burn_transient_ui(message.chat.id, state)
    text = "📊 **Live Available SIM Pools:**\n\n"
    for s_name, s_info in SERVICES_PRICING.items(): 
        text += f"• **{s_name}**: `{s_info['stock']}` pcs available\n"
        
    out_msg = await message.answer(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await track_transient_ui(out_msg, state)

# ==========================================
# 💳 SECURE TOP UP PIPELINE MODULE
# ==========================================

@router.message(F.text == "💳 Top Up Balance")
async def menu_topup_text(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass
    
    await burn_transient_ui(message.chat.id, state)
    out_msg = await message.answer(
        "💸 **Manual Balance Realization Route**\n\n"
        "1. Remit your desired amount through GCash to: `09563495650` (Account: OMAR A)\n"
        "2. Save the screenshot of the transaction receipt.\n\n"
        "📸 **Please upload/send the image of your GCash receipt now:**\n*(Or navigate elsewhere to cancel)*",
        reply_markup=inline_back_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(TopUpStates.waiting_for_receipt)
    await track_transient_ui(out_msg, state)

@router.message(TopUpStates.waiting_for_receipt, F.text)
async def reject_text_receipt(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass
    
    err = await message.answer("❌ **Invalid format.** Please upload the actual **image/screenshot** of your receipt.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await track_transient_ui(err, state)

@router.message(TopUpStates.waiting_for_receipt, F.photo)
async def process_topup_receipt(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    photo_id = message.photo[-1].file_id

    try: await message.delete()
    except Exception: pass

    admin_kb = InlineKeyboardBuilder()
    admin_kb.row(types.InlineKeyboardButton(text="🟢 Input Amount & Approve", callback_data=f"adm_start_app:{user_id}"))
    admin_kb.row(types.InlineKeyboardButton(text="🔴 Deny Fraud/Invalid", callback_data=f"adm_den:{user_id}"))

    await bot.send_photo(
        chat_id=ADMIN_ID, photo=photo_id,
        caption=f"🚨 **Inbound Top-Up Receipt for Audit**\n\n• **User ID:** `{user_id}`\n• **Username:** @{username}\n\nPlease review the attached receipt image.",
        reply_markup=admin_kb.as_markup(), parse_mode="Markdown"
    )
    confirm_msg = await message.answer("⏳ **Receipt uploaded successfully!**\nIt has been dispatched to the Admin for manual verification. You will be notified once approved.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await track_transient_ui(confirm_msg, state)

@router.callback_query(F.data.startswith("adm_start_app:"))
async def cb_admin_start_approve(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    target_user_id = int(callback.data.split(":")[1])

    await state.update_data(target_user=target_user_id)
    await state.set_state(AdminStates.waiting_for_approved_amount)
    await callback.message.answer(f"✅ **Approving Top-Up for User `{target_user_id}`**\n\nBased on the receipt, **type the exact amount in PHP** to credit to their account:", parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.waiting_for_approved_amount)
async def process_admin_amount_input(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return

    try:
        amount = float(message.text)
        if amount <= 0: raise ValueError
    except ValueError:
        return await message.answer("❌ Invalid numeric format. Type a valid number (e.g. 100). Try again:")

    data = await state.get_data()
    target_user_id = data.get("target_user")
    await state.clear()

    try: await message.delete()
    except Exception: pass

    db_call = await db_execute(supabase.rpc("refund_balance", {"user_id": target_user_id, "refund_amount": amount}))
    new_bal = db_call.data if db_call.data else amount

    await db_execute(supabase.table("topup_logs").insert({"telegram_id": target_user_id, "amount": amount}))

    await bot.send_message(chat_id=target_user_id, text=f"🎉 **Your top-up receipt was APPROVED!**\n\n💰 Added: ₱{amount:.2f}\n💳 Current Balance: ₱{new_bal:.2f}", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await message.answer(f"✅ Successfully injected ₱{amount:.2f} into User `{target_user_id}`'s account.", parse_mode="Markdown")

@router.callback_query(F.data.startswith("adm_den:"))
async def cb_admin_deny(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_user_id = int(callback.data.split(":")[1])

    await bot.send_message(chat_id=target_user_id, text="❌ **Your Top-Up receipt was declined.** Please ensure the image is clear and valid.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")
    await callback.message.edit_caption(caption=f"❌ **DENIED**\nReceipt verification request rejected for user `{target_user_id}`.", parse_mode="Markdown")
    await callback.answer()

# ==========================================
# 📱 MOBILE INFRASTRUCTURE ALLOCATION LINE
# ==========================================

@router.message(F.text == "📞 Buy Numbers")
async def menu_buy_text(message: types.Message, state: FSMContext):
    try: await message.delete()
    except Exception: pass
    
    await burn_transient_ui(message.chat.id, state)
    builder = InlineKeyboardBuilder()
    for s_name, s_meta in SERVICES_PRICING.items():
        builder.row(types.InlineKeyboardButton(text=f"📱 Get {s_name} (₱{s_meta['price']:.2f})", callback_data=f"buy_srv:{s_name}"))
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_main"))
    
    out_msg = await message.answer("Select target application platform integration context:", reply_markup=builder.as_markup())
    await track_transient_ui(out_msg, state)

@router.callback_query(F.data.startswith("buy_srv:"))
async def cb_execute_purchase(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await initiate_number_purchase(callback.message, callback.from_user.id, callback.data.split(":")[1], is_callback=True, state=state)

async def initiate_number_purchase(msg: types.Message, user_id: int, service_name: str, is_callback: bool, state: FSMContext):
    await burn_transient_ui(msg.chat.id, state)
    service_meta = SERVICES_PRICING[service_name]

    if service_meta["stock"] <= 0:
        text = f"⚠️ **No stocks available!**\n\nThere are currently no lines left in stock for **{service_name}**. Please check back later."
        err_msg = await msg.edit_text(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown") if is_callback else await msg.answer(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
        await track_transient_ui(err_msg, state)
        return

    cost = service_meta["price"]
    service_code = service_meta["code"]

    is_allowed = await db_execute(supabase.rpc("deduct_balance", {"user_id": user_id, "cost": cost}))
    if not is_allowed.data:
        text = "❌ **Insufficient credit allocation context. Please perform a Top-Up.**"
        err_msg = await msg.edit_text(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown") if is_callback else await msg.answer(text, reply_markup=inline_back_keyboard(), parse_mode="Markdown")
        await track_transient_ui(err_msg, state)
        return

    service_meta["stock"] -= 1
    text_loading = "⏳ **Allocating secure network line resource... Please wait.**"
    status_msg = await msg.edit_text(text_loading, parse_mode="Markdown") if is_callback else await msg.answer(text_loading, parse_mode="Markdown")
    await track_transient_ui(status_msg, state)

    try:
        cheapest_operator = ""
        try:
            price_resp = await http_client.get(
                HERO_SMS_URL, 
                params={"api_key": HERO_SMS_KEY, "action": "getPrices", "service": service_code, "country": PH_COUNTRY_ID}
            )
            price_data = price_resp.json()
            country_str = str(PH_COUNTRY_ID)

            if country_str in price_data and service_code in price_data[country_str]:
                service_nodes = price_data[country_str][service_code]
                lowest_market_cost_php = float('inf')

                for op_name, op_info in service_nodes.items():
                    if op_info.get("count", 0) > 0:
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
        if cheapest_operator and cheapest_operator != "any":
            api_params["operator"] = cheapest_operator

        resp = await http_client.get(HERO_SMS_URL, params=api_params)

        if resp.text.startswith("ACCESS_NUMBER"):
            _, act_id, raw_phone = resp.text.split(":")
            ph_clean = format_ph_number(raw_phone)

            await db_execute(supabase.table("activations").insert({
                "activation_id": act_id, "telegram_id": user_id, 
                "service": service_name, "amount": cost, "phone_number": ph_clean
            }))

            control_kb = InlineKeyboardBuilder()
            control_kb.row(types.InlineKeyboardButton(text="❌ Cancel", callback_data=f"act_cancel:{act_id}"), types.InlineKeyboardButton(text="⚠️ Report Used", callback_data=f"act_ban:{act_id}"))
            
            await status_msg.edit_text(
                f"📱 **Line Lease Active**\n\n"
                f"• Platform: `{service_name}`\n"
                f"• Phone Number: `{ph_clean}`\n\n"
                f"⏳ Waiting for verification transmission strings to arrive...", 
                reply_markup=control_kb.as_markup(), parse_mode="Markdown"
            )
            asyncio.create_task(poll_for_incoming_otp(user_id, act_id, status_msg.message_id, state))

        elif "NO_NUMBERS" in resp.text:
            service_meta["stock"] += 1 
            await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
            await status_msg.edit_text(f"⚠️ **Out of Stock!**\n\nNo available allocations for **{service_name}** in this region. Your ₱{cost:.2f} has been credited back.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")

        elif "NO_BALANCE" in resp.text:
            service_meta["stock"] += 1 
            await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
            await status_msg.edit_text(f"❌ **System Notice:** Infrastructure node maintenance required. Balance refunded.", reply_markup=inline_back_keyboard(), parse_mode="Markdown")

        else:
            raise Exception(f"Validation Failure: {resp.text}")

    except Exception as e:
        service_meta["stock"] += 1 
        await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
        await status_msg.edit_text(f"❌ **System error allocating number infrastructure:** {str(e)}", reply_markup=inline_back_keyboard(), parse_mode="Markdown")

# ==========================================
# ⏳ BACKGROUND SMS MONITORING TIMELINES
# ==========================================

async def poll_for_incoming_otp(user_id: int, act_id: str, msg_id: int, state: FSMContext):
    """Monitors verification transmission updates. OTP messages are never burned."""
    for _ in range(180):
        await asyncio.sleep(5)
        act_chk = await db_execute(supabase.table("activations").select("status").eq("activation_id", act_id))
        if not act_chk.data or act_chk.data[0]["status"] != "pending": return 

        try:
            resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getStatus", "id": act_id})
            if resp.text.startswith("STATUS_OK"):
                raw_token = resp.text.split(':')[1]
                
                # Commit updates across tracking databases
                await db_execute(supabase.table("activations").update({"status": "completed"}).eq("activation_id", act_id))
                await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 6, "id": act_id})
                
                # Burn active monitoring transient view frame
                try: await bot.delete_message(chat_id=user_id, message_id=msg_id)
                except Exception: pass
                await state.update_data(last_transient_msg_id=None)
                
                # Deliver completely isolated monospace verification code matching image 1156.png
                await bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 **OTP Received Successfully!**\n\n"
                         f"📋 **Verification Code:**\n"
                         f"`{raw_token}`\n\n"
                         f"Tap the number above to copy it instantly.",
                    parse_mode="Markdown"
                )
                return
            elif "STATUS_WAIT_CODE" not in resp.text: 
                break 
        except Exception: 
            pass

@router.callback_query(F.data.startswith("act_"))
async def cb_lifecycle_mutation(callback: types.CallbackQuery, state: FSMContext):
    action, act_id, user_id = callback.data.split(":")[0], callback.data.split(":")[1], callback.from_user.id

    chk = await db_execute(supabase.table("activations").select("*").eq("activation_id", act_id))
    if not chk.data or chk.data[0]["status"] != "pending":
        return await callback.answer("❌ This lease context is no longer active.", show_alert=True)

    refund_val = float(chk.data[0]["amount"])
    service_name = chk.data[0]["service"]
    phone_num = chk.data[0]["phone_number"]

    if service_name in SERVICES_PRICING:
        SERVICES_PRICING[service_name]["stock"] += 1

    await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 8, "id": act_id})
    await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))
    await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": refund_val}))

    await callback.answer("Wallet balance and stock allocation refunded successfully.")
    await burn_transient_ui(callback.message.chat.id, state)
    
    cancel_msg = await callback.message.answer(
        f"✅ Number `{phone_num}` for service **{service_name}** has been successfully cancelled.\n\n"
        f"• Value ₱{refund_val:.2f} was restored safely to your internal balance allocation profile.",
        reply_markup=inline_back_keyboard(), parse_mode="Markdown"
    )
    await track_transient_ui(cancel_msg, state)

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
                if row.get("created_at", "").startswith(today_string):
                    today_total += amt

        return (
            "👑 **TeleBOT System Administrator Console**\n\n"
            f"📊 **Live Revenue Dashboard:**\n"
            f"• **Today's Gross Top-Ups:** ₱`{today_total:.2f}`\n"
            f"• **All-Time Gross Top-Ups:** ₱`{all_time_total:.2f}`\n"
            f"• **Total Registered Users:** `{total_users}`\n\n"
            "Choose an administrative action below:"
        )
    except Exception as e:
        return f"👑 **Admin Console Error**\n⚠️ DB Error: `{e}`"

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
    await callback.message.edit_text(
        "📢 **Global Announcement Engine**\n\n"
        "Type and send the message text you wish to blast out to **all** users registered in the database.",
        reply_markup=admin_back_keyboard(), parse_mode="Markdown"
    )
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
    
    if not users_pool.data:
        return await progress_msg.edit_text("❌ Broadcast aborted: No registered users found inside database records.", reply_markup=admin_back_keyboard())

    success_count, failure_count = 0, 0
    for user in users_pool.data:
        target_chat_id = user["telegram_id"]
        if target_chat_id == ADMIN_ID: continue
        try:
            await bot.send_message(chat_id=target_chat_id, text=broadcast_text, parse_mode="Markdown")
            success_count += 1
            await asyncio.sleep(0.05) 
        except TelegramAPIError:
            failure_count += 1
            
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
