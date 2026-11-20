import unittest
import os
import json
import importlib.util

# Set valid format token for test environment if missing or invalid
if ":" not in os.getenv("BOT_TOKEN", ""):
    os.environ["BOT_TOKEN"] = "1234567890:AAF2oaXbr66RGZEbksJId2wJmn9vENlTxsg"

spec = importlib.util.spec_from_file_location("bot_module", "bot_v1.2.py")
bot_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot_module)

class TestBotLogic(unittest.TestCase):
    def setUp(self):
        # Backup allowed_users.json
        if os.path.exists("allowed_users.json"):
            with open("allowed_users.json", "r", encoding="utf-8") as f:
                self.backup_data = f.read()
        else:
            self.backup_data = None

    def tearDown(self):
        # Restore allowed_users.json
        if self.backup_data is not None:
            with open("allowed_users.json", "w", encoding="utf-8") as f:
                f.write(self.backup_data)

    def test_main_menu_keyboard(self):
        kb = bot_module.main_menu_keyboard()
        buttons = []
        for row in kb.keyboard:
            for b in row:
                buttons.append(b.text)
        
        self.assertIn("📞 Buy Numbers", buttons)
        self.assertIn("🔁 Extend Numbers", buttons)
        self.assertNotIn("💳 Top Up Balance", buttons)
        self.assertNotIn("📦 Stocks", buttons)
        self.assertNotIn("💰 Balance", buttons)
        self.assertNotIn("💵 Price List", buttons)
        self.assertEqual(len(buttons), 2)
        print("✅ Test 1 Passed: Public menu keyboard only has Buy Numbers and Extend Numbers")

    def test_admin_access_allowed(self):
        admin_id = bot_module.ADMIN_ID
        self.assertTrue(bot_module.is_user_allowed(admin_id))
        print("✅ Test 2 Passed: Admin is always authorized")

    def test_unauthorized_user_blocked(self):
        test_uid = 99999999999
        # Ensure not in allowed_users
        bot_module.remove_allowed_user(test_uid)
        self.assertFalse(bot_module.is_user_allowed(test_uid))
        print("✅ Test 3 Passed: Unauthorized user is blocked")

    def test_add_and_remove_allowed_user(self):
        test_uid = 88888888888
        bot_module.add_allowed_user(test_uid)
        self.assertTrue(bot_module.is_user_allowed(test_uid))
        
        # Verify persistence in json
        with open("allowed_users.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertIn(test_uid, data["allowed_users"])

        # Revoke/Delete user
        bot_module.remove_allowed_user(test_uid)
        self.assertFalse(bot_module.is_user_allowed(test_uid))
        print("✅ Test 4 Passed: Admin can add and delete users with file persistence")

    def test_admin_dashboard_buttons(self):
        kb = bot_module.get_admin_dashboard_markup()
        callback_datas = []
        urls = []
        for row in kb.inline_keyboard:
            for b in row:
                if b.callback_data:
                    callback_datas.append(b.callback_data)
                if b.url:
                    urls.append(b.url)
        
        self.assertIn("admin_manage_users", callback_datas)
        self.assertIn("adm_add_service", callback_datas)
        self.assertIn("adm_topup_menu", callback_datas)
        self.assertIn("admin_log_topups", callback_datas)
        self.assertTrue(any("hero-sms.com" in u for u in urls))
        print("✅ Test 5 Passed: Admin dashboard contains Top Up, Logs, User Access, and HeroSMS portal buttons")

    def test_admin_states_topup(self):
        self.assertTrue(hasattr(bot_module.AdminStates, "waiting_for_topup_target_id"))
        self.assertTrue(hasattr(bot_module.AdminStates, "waiting_for_topup_amount"))
        print("✅ Test 6 Passed: AdminStates has topup states defined")

    def test_service_management_and_persistence(self):
        # 1. Add new test service
        test_srv = "WhatsApp_Test"
        bot_module.SERVICES_PRICING[test_srv] = {
            "code": "wa",
            "price": 8.50,
            "stock": 100,
            "operator": "any"
        }
        bot_module.save_services()
        
        # Verify persistence in services.json
        loaded = bot_module.load_services()
        self.assertIn(test_srv, loaded)
        self.assertEqual(loaded[test_srv]["price"], 8.50)
        self.assertEqual(loaded[test_srv]["code"], "wa")
        print("✅ Test 6 Passed: Dynamic service addition and persistence")

        # 2. Edit price
        bot_module.SERVICES_PRICING[test_srv]["price"] = 9.75
        bot_module.save_services()
        loaded = bot_module.load_services()
        self.assertEqual(loaded[test_srv]["price"], 9.75)
        print("✅ Test 7 Passed: Price modification and persistence")

        # 3. Delete service
        del bot_module.SERVICES_PRICING[test_srv]
        bot_module.save_services()
        loaded = bot_module.load_services()
        self.assertNotIn(test_srv, loaded)
        print("✅ Test 8 Passed: Service deletion and persistence")

if __name__ == "__main__":
    unittest.main()
