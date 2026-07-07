import os
import asyncio
import httpx
from dotenv import load_dotenv

# Load your existing .env keys
load_dotenv()
HERO_SMS_KEY = os.getenv("HERO_SMS_API_KEY")
HERO_SMS_URL = os.getenv("HERO_SMS_BASE_URL")

async def fetch_services():
    print("⏳ Connecting to HeroSMS to download the official service catalog...")
    
    async with httpx.AsyncClient() as client:
        params = {"api_key": HERO_SMS_KEY, "action": "getServicesList"}
        resp = await client.get(HERO_SMS_URL, params=params, timeout=30.0)
        
        try:
            data = resp.json()
            services = data.get("services", [])
            
            # Save all services to a text file
            with open("services_list.txt", "w", encoding="utf-8") as f:
                f.write("--- HEROSMS OFFICIAL SERVICE CODES ---\n\n")
                for srv in services:
                    f.write(f"App Name: {srv['name']} | Code: '{srv['code']}'\n")
                    
            print(f"✅ SUCCESS! Downloaded {len(services)} services.")
            print("👉 Open the new 'services_list.txt' file in your folder to find your codes!")
            
        except Exception as e:
            print("❌ Failed to parse response:", resp.text)

if __name__ == "__main__":
    asyncio.run(fetch_services())