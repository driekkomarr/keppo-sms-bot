import os
import asyncio
import httpx
import threading
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

# --- LOAD ENVIRONMENT VARIABLES ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HERO_SMS_KEY = os.getenv("HERO_SMS_API_KEY")
HERO_SMS_URL = os.getenv("HERO_SMS_BASE_URL")

# --- OFFICIAL COUNTRY CODE ---
PH_COUNTRY_ID = 4 

# --- PRICING & LIVE DYNAMIC STOCK PROFILES ---
SERVICES_PRICING = {
    "Shopee": {"code": "ds", "price": 12.00, "stock": 50},     
    "GCash": {"code": "ot", "price": 15.00, "stock": 50},      
    "Any Other": {"code": "ot", "price": 18.00, "stock": 50}   
}

# --- INITIALIZE CONNECTIONS ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Shared Global HTTP Client to preserve socket pooling across operations
http_client: httpx.AsyncClient = None

# --- STATE MACHINES ---
class TopUpStates(StatesGroup):
    waiting_for_receipt = State()

class AdminStates(StatesGroup):
    waiting_for_approved_amount = State()
    waiting_for_broadcast = State()

# --- NON-BLOCKING ASYNC DATABASE WRAPPER ---
async def db_execute(query_chain):
    """Executes blocking Supabase network operations in an isolated worker thread."""
    return await asyncio.to_thread(query_chain.execute)

# --- UTILITY FUNCTIONS ---
def format_ph_number(raw_num: str) -> str:
    cleaned = raw_num.strip().replace("+", "")
    if cleaned.startswith("63"): cleaned = cleaned[2:]
    if cleaned.startswith("0"): cleaned = cleaned[1:]
    return cleaned

# --- UI KEYBOARDS (UPDATED TO MATCH IMAGE_5A841B.PNG) ---
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    # Row 1: 2 Columns
    builder.row(
        types.InlineKeyboardButton(text="📞 Buy Numbers", callback_data="menu_buy"),
        types.InlineKeyboardButton(text="💵 Price List", callback_data="menu_prices")
    )
    # Row 2: 2 Columns
    builder.row(
        types.InlineKeyboardButton(text="💰 Balance", callback_data="menu_balance"),
        types.InlineKeyboardButton(text="📦 Stocks", callback_data="menu_stocks")
    )
    # Row 3: Full Width
    builder.row(
        types.InlineKeyboardButton(text="💳 Top Up Balance", callback_data="menu_topup")
    )
    return builder.as_markup()

def back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_main"))
    return builder.as_markup()

def admin_back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Admin Dashboard", callback_data="admin_dashboard_home"))
    return builder.as_markup()

# --- STARTUP HOOK (HIDES ADMIN COMMAND FROM USERS) ---
async def on_startup(bot: Bot):
    global http_client
    http_client = httpx.AsyncClient(timeout=15.0)
    
    user_commands = [
        types.BotCommand(command="start", description="Open Main Dashboard"),
        types.BotCommand(command="buy_again", description="Re-order Last Service Selection")
    ]
    await bot.set_my_commands(user_commands, scope=types.BotCommandScopeDefault())
    
    admin_commands = [
        types.BotCommand(command="start", description="Open Main Dashboard"),
        types.BotCommand(command="admin", description="Open Admin Panel 👑"),
        types.BotCommand(command="buy_again", description="Re-order Last Service Selection")
    ]
    await bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))

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
        all_time_total = 0.00
        today_total = 0.00
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
            if not res.data: 
                text += "No top-ups recorded yet."
            else:
                for row in reversed(res.data): 
                    date_str = str(row.get('created_at', 'Unknown'))[:10] if row.get('created_at') else 'N/A'
                    text += f"• **User:** `{row.get('telegram_id', 'Unknown')}` | **₱{row.get('amount', 0)}** | {date_str}\n"
                
        elif action == "sales":
            res = await db_execute(supabase.table("activations").select("*").eq("status", "completed").limit(10))
            text = "🛒 **Latest Completed OTP Dispatches:**\n\n"
            if not res.data: 
                text += "No completed sales yet."
            else:
                for row in reversed(res.data): 
                    text += f"• **{row.get('service', 'Service')}** | Cost: ₱{row.get('amount', 0)} | **User:** `{row.get('telegram_id', 'Unknown')}`\n"

        await callback.message.edit_text(text, reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    except Exception as e:
        error_text = f"⚠️ **Database Error Detected:**\n`{str(e)}`"
        await callback.message.edit_text(error_text, reply_markup=admin_back_keyboard(), parse_mode="Markdown")
    finally:
        await callback.answer()

@router.callback_query(F.data == "admin_trigger_broadcast")
async def cb_admin_trigger_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text(
        "📢 **Global Announcement Engine**\n\n"
        "Type and send the message text you wish to blast out to **all** users registered in the database.\n\n"
        "⚠️ _Note: Markdown formatting is fully supported._",
        reply_markup=admin_back_keyboard(),
        parse_mode="Markdown"
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
# 👤 STANDARD USER ENGINE LOGIC
# ==========================================

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    
    try: await message.delete()
    except Exception: pass
    
    await db_execute(supabase.table("users").upsert({"telegram_id": tg_id, "username": username}, on_conflict="telegram_id"))
    await message.answer("Welcome to Pinglong OTP Bot!\n\nTap any of the options below to get started.", reply_markup=main_menu_keyboard())

@router.message(Command("buy_again"))
async def cmd_buy_again(message: types.Message):
    tg_id = message.from_user.id
    
    try: await message.delete()
    except Exception: pass
    
    res = await db_execute(supabase.table("activations").select("service").eq("telegram_id", tg_id).order("activation_id", descending=True).limit(1))
    if not res.data:
        return await message.answer("❌ No previous transaction histories found to clone.", reply_markup=back_keyboard())
    await initiate_number_purchase(message, tg_id, res.data[0]["service"], is_callback=False)

@router.callback_query(F.data == "menu_main")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear() 
    await callback.message.edit_text("Welcome to Pinglong OTP Bot!\n\nTap any of the options below to get started.", reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "menu_balance")
async def cb_balance(callback: types.CallbackQuery):
    res = await db_execute(supabase.table("users").select("balance").eq("telegram_id", callback.from_user.id))
    balance = res.data[0]["balance"] if res.data else 0.00
    await callback.message.edit_text(f"💳 **Your Current Account Balance:** ₱{balance:.2f}", reply_markup=back_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "menu_prices")
async def cb_prices(callback: types.CallbackQuery):
    text = "📋 **Current Platform Pricing Profiles:**\n\n"
    for s_name, s_info in SERVICES_PRICING.items(): text += f"• **{s_name}**: ₱{s_info['price']:.2f}\n"
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "menu_stocks")
async def cb_stocks(callback: types.CallbackQuery):
    text = "📊 **Live Available SIM Pools:**\n\n"
    for s_name, s_info in SERVICES_PRICING.items(): 
        text += f"• **{s_name}**: `{s_info['stock']}` pcs available\n"
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

# --- SECURE PHOTO TOP UP MODULE ---
@router.callback_query(F.data == "menu_topup")
async def cb_topup_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💸 **Manual Balance Realization Route**\n\n"
        "1. Remit your desired amount through GCash to: `09123456789` (Account: Name Placeholder)\n"
        "2. Save the screenshot of the transaction receipt.\n\n"
        "📸 **Please upload/send the image of your GCash receipt now:**\n*(Or click Back to cancel)*",
        reply_markup=back_keyboard(), parse_mode="Markdown"
    )
    await state.set_state(TopUpStates.waiting_for_receipt)

@router.message(TopUpStates.waiting_for_receipt, F.text)
async def reject_text_receipt(message: types.Message):
    try: await message.delete()
    except Exception: pass
    await message.answer("❌ **Invalid format.** Please upload the actual **image/screenshot** of your receipt.", reply_markup=back_keyboard(), parse_mode="Markdown")

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
    await message.answer("⏳ **Receipt uploaded successfully!**\nIt has been dispatched to the Admin for manual verification. You will be notified once approved.", reply_markup=back_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("adm_start_app:"))
async def cb_admin_start_approve(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    target_user_id = int(callback.data.split(":")[1])
    
    await state.update_data(target_user=target_user_id)
    await state.set_state(AdminStates.waiting_for_approved_amount)
    await callback.message.answer(f"✅ **Approving Top-Up for User `{target_user_id}`**\n\nBased on the receipt you just viewed, **type the exact amount in PHP** to credit to their account:", parse_mode="Markdown")
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
    
    await bot.send_message(chat_id=target_user_id, text=f"🎉 **Your top-up receipt was APPROVED!**\n\n💰 Added: ₱{amount:.2f}\n💳 Current Balance: ₱{new_bal:.2f}", reply_markup=back_keyboard(), parse_mode="Markdown")
    await message.answer(f"✅ Successfully injected ₱{amount:.2f} into User `{target_user_id}`'s account.", parse_mode="Markdown")

@router.callback_query(F.data.startswith("adm_den:"))
async def cb_admin_deny(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_user_id = int(callback.data.split(":")[1])

    await bot.send_message(chat_id=target_user_id, text="❌ **Your Top-Up receipt was declined.** Please ensure the image is clear and valid.", reply_markup=back_keyboard(), parse_mode="Markdown")
    await callback.message.edit_caption(caption=f"❌ **DENIED**\nReceipt verification request rejected for user `{target_user_id}`.", parse_mode="Markdown")
    await callback.answer()

# --- DISPOSABLE PROVISIONING ENGINE ---
@router.callback_query(F.data == "menu_buy")
async def cb_buy_menu(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    
    for s_name, s_meta in SERVICES_PRICING.items():
        cost = s_meta["price"]
        builder.row(types.InlineKeyboardButton(text=f"📱 Get {s_name} (₱{cost:.2f})", callback_data=f"buy_srv:{s_name}"))
        
    builder.row(types.InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_main"))
    await callback.message.edit_text("Select target application platform integration context:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("buy_srv:"))
async def cb_execute_purchase(callback: types.CallbackQuery):
    await callback.answer()
    await initiate_number_purchase(callback.message, callback.from_user.id, callback.data.split(":")[1], is_callback=True)

async def initiate_number_purchase(msg: types.Message, user_id: int, service_name: str, is_callback: bool):
    service_meta = SERVICES_PRICING[service_name]
    
    # 🚨 DYNAMIC INVENTORY INTERCEPTOR GUARD
    if service_meta["stock"] <= 0:
        text = f"⚠️ **No stocks available!**\n\nThere are currently no lines left in stock for **{service_name}**. Please look for alternate apps or check back later."
        return await msg.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown") if is_callback else await msg.answer(text, reply_markup=back_keyboard(), parse_mode="Markdown")

    cost = service_meta["price"]
    service_code = service_meta["code"]

    is_allowed = await db_execute(supabase.rpc("deduct_balance", {"user_id": user_id, "cost": cost}))
    if not is_allowed.data:
        text = "❌ **Insufficient credit allocation context. Please perform a Top-Up.**"
        return await msg.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown") if is_callback else await msg.answer(text, reply_markup=back_keyboard(), parse_mode="Markdown")

    # Claim inventory allotment 
    service_meta["stock"] -= 1

    text_loading = "⏳ **Allocating secure network line resource... Please wait.**"
    status_msg = await msg.edit_text(text_loading, parse_mode="Markdown") if is_callback else await msg.answer(text_loading, parse_mode="Markdown")

    try:
        resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getNumber", "service": service_code, "country": PH_COUNTRY_ID})
        
        if resp.text.startswith("ACCESS_NUMBER"):
            _, act_id, raw_phone = resp.text.split(":")
            ph_clean = format_ph_number(raw_phone)
            
            await db_execute(supabase.table("activations").insert({"activation_id": act_id, "telegram_id": user_id, "service": service_name, "amount": cost, "phone_number": ph_clean}))

            control_kb = InlineKeyboardBuilder()
            control_kb.row(types.InlineKeyboardButton(text="❌ Cancel", callback_data=f"act_cancel:{act_id}"), types.InlineKeyboardButton(text="⚠️ Report Used", callback_data=f"act_ban:{act_id}"))
            control_kb.row(types.InlineKeyboardButton(text="🔙 Main Menu (Leaves Monitor Active)", callback_data="menu_main"))
            
            await status_msg.edit_text(f"📱 **Line Lease Active**\n\n• Platform: `{service_name}`\n• Phone Number: `+{ph_clean}`\n\n⏳ Waiting for verification transmission strings to arrive...", reply_markup=control_kb.as_markup(), parse_mode="Markdown")
            asyncio.create_task(poll_for_incoming_otp(user_id, act_id, status_msg.message_id))
        
        elif "NO_NUMBERS" in resp.text:
            service_meta["stock"] += 1 # Return allocation slot
            await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
            await status_msg.edit_text(f"⚠️ **Out of Stock!**\n\nNo available allocations for **{service_name}** in this region. Your ₱{cost:.2f} has been credited back.", reply_markup=back_keyboard(), parse_mode="Markdown")
            
        elif "NO_BALANCE" in resp.text:
            service_meta["stock"] += 1 # Return allocation slot
            await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
            await status_msg.edit_text(f"❌ **System Notice:** Infrastructure node maintenance required. Balance refunded.", reply_markup=back_keyboard(), parse_mode="Markdown")
            
        else:
            raise Exception(f"Validation Failure: {resp.text}")
            
    except Exception as e:
        service_meta["stock"] += 1 # Return allocation slot on failure
        await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": cost}))
        await status_msg.edit_text(f"❌ **System error allocating number infrastructure:** {str(e)}", reply_markup=back_keyboard(), parse_mode="Markdown")

async def poll_for_incoming_otp(user_id: int, act_id: str, msg_id: int):
    for _ in range(180):
        await asyncio.sleep(5)
        act_chk = await db_execute(supabase.table("activations").select("status").eq("activation_id", act_id))
        if not act_chk.data or act_chk.data[0]["status"] != "pending": return 

        try:
            resp = await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "getStatus", "id": act_id})
            if resp.text.startswith("STATUS_OK"):
                await db_execute(supabase.table("activations").update({"status": "completed"}).eq("activation_id", act_id))
                await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 6, "id": act_id})
                await bot.edit_message_text(chat_id=user_id, message_id=msg_id, text=f"🎉 **OTP Received Successfully!**\n\nVerification Code: `{resp.text.split(':')[1]}`", reply_markup=back_keyboard(), parse_mode="Markdown")
                return
            elif "STATUS_WAIT_CODE" not in resp.text: break 
        except Exception: pass

@router.callback_query(F.data.startswith("act_"))
async def cb_lifecycle_mutation(callback: types.CallbackQuery):
    action, act_id, user_id = callback.data.split(":")[0], callback.data.split(":")[1], callback.from_user.id

    chk = await db_execute(supabase.table("activations").select("*").eq("activation_id", act_id))
    if not chk.data or chk.data[0]["status"] != "pending":
        return await callback.answer("❌ This lease context is no longer active.", show_alert=True)

    refund_val = float(chk.data[0]["amount"])
    service_name = chk.data[0]["service"]
    phone_num = chk.data[0]["phone_number"]
    
    # 📦 DYNAMIC INVENTORY INJECTION RETURN 
    if service_name in SERVICES_PRICING:
        SERVICES_PRICING[service_name]["stock"] += 1

    await http_client.get(HERO_SMS_URL, params={"api_key": HERO_SMS_KEY, "action": "setStatus", "status": 8, "id": act_id})

    await db_execute(supabase.table("activations").update({"status": "cancelled"}).eq("activation_id", act_id))
    await db_execute(supabase.rpc("refund_balance", {"user_id": user_id, "refund_amount": refund_val}))

    # MATCHES EXACT TERMINATION LOG PATTERN OBSERVED IN IMAGE_5A841B.PNG
    await callback.message.edit_text(
        f"✅ Number `{phone_num}` for service **{service_name}** has been successfully cancelled.\n\n"
        f"• Value ₱{refund_val:.2f} was restored safely to your internal balance allocation profile.",
        reply_markup=back_keyboard(), parse_mode="Markdown"
    )
    await callback.answer("Wallet balance and stock allocation refunded successfully.")

# ==========================================
# 🌍 HEALTH CHECK SERVER (FOR FREE HOSTING)
# ==========================================

def run_health_check_server():
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive and polling!")
        def log_message(self, format, *args):
            return 

    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"🌍 Health check web terminal operational on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_health_check_server, daemon=True).start()

    dp.include_router(router)
    dp.startup.register(on_startup)
    asyncio.run(dp.start_polling(bot))