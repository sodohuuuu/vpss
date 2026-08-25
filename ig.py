import asyncio
import os
import random
import sys
import time
import shutil
from playwright.async_api import async_playwright

# ================= DEFAULT CONFIGURATION =================
DEFAULT_SID = ["76248746678%3AirYuiwG9HsICGO%3A16%3AAYhv33lYNP9duoepc2MnnXv0JMeP9MwKrugV-FGM9g"]
DEFAULT_URL = "https://www.instagram.com/direct/t/1704768377345706/"
DEFAULT_OPPONENT = "CHUNKI/CHUDARA"
DEFAULT_ENGINE_COUNT = 4
DELAY = 0.3 
# =========================================================

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def professional_banner():
    clear()
    banner = f"""
    \033[95m    ██████╗ ██████╗ ██╗     ██╗██╗   ██╗██╗ ██████╗ ███╗   ██╗
    \033[94m    ██╔══██╗██╔══██╗██║     ██║██║   ██║██║██╔═══██╗████╗  ██║
    \033[96m    ██║  ██║██████╔╝██║     ██║██║   ██║██║██║   ██║██╔██╗ ██║
    \033[94m    ██║  ██║██╔══██╗██║     ██║╚██╗ ██╔╝██║██║   ██║██║╚██╗██║
    \033[95m    ██████╔╝██████╔╝███████╗██║ ╚████╔╝ ██║╚██████╔╝██║ ╚████║
    \033[0m
    \033[1;37m        🔱  MANSURIxGOD | THE ABSOLUTE KING 👑
    \033[1;31m        --------------------------------------------
    \033[1;33m  DEVELOPER : MANSURI 
    \033[1;36m        STATUS    : TARGET INJECTION ACTIVE
    \033[1;31m        --------------------------------------------
    \033[0m"""
    print(banner)

# 👑 Use {target} as a placeholder for the opponent names
STRIKE_MESSAGES = [
    "[{target}] 𝐒𝐘𝐒𝐓𝐄𝐌 𝐎𝐕𝐄𝐑𝐋𝐎𝐀𝐃 तेरी औकात नहीं हमसे लड़ने की 😂🦅",
    "[{target}] 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀 𝐊𝐎 𝐂𝐇𝐎𝐃 𝐃𝐀𝐀𝐋𝐄𝐍𝐆𝐄 //~ 🔥",
    "[{target}] T𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐌𝐘 𝐊𝐎 𝐊𝐈𝐍𝐍𝐄𝐑 𝐆𝐑𝐎𝐔𝐏 𝐖𝐀𝐋𝐄 𝐂𝐇𝐎𝐃𝐄𝐍𝐆𝐄 𝐘𝐀𝐀𝐃 𝐑𝐀𝐊𝐇𝐍𝐀 😝🤲🏻",
    "[{target}] 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀 𝐊𝐄 𝐁𝐇𝐎𝐒𝐃𝐄 𝐌𝐀𝐈 𝐈𝐓𝐍𝐄 𝐂𝐇𝐀𝐍𝐓𝐄 𝐌𝐀𝐑𝐔𝐍𝐆𝐀 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀 𝐊𝐀 𝐁𝐇𝐎𝐒𝐃𝐀 𝐅𝐀𝐀𝐓 𝐉𝐘𝐆𝐀 🐒🤣🔥",
    "[{target}] 𝐌𝐀𝐍𝐒𝐔𝐑𝐈 𝐒𝐈𝐃𝐄 𝐀𝐂𝐓𝐈𝐕𝐄 अब रोने का अलावा कोई रास्ता नहीं🤣🔥 "
   "꧁𓊈𒆜 𝙈𝘼𝙉𝙎𝙐𝙍𝙄 भगवान है👑 𒆜𓊉꧂ " ]

def countdown(seconds):
    for i in range(seconds, 0, -1):
        sys.stdout.write(f"\r\033[1;32m    [SYSTEM] DEPLOYING ENGINES IN {i} SECONDS... \033[0m")
        sys.stdout.flush()
        time.sleep(1)
    print("\n")

def get_payload(opponent):
    rand_id = random.randint(1000, 9999)
    gap_lines = "\n" * 160
    # 🔱 This line swaps {target} with your actual Opponent Names
    core = random.choice(STRIKE_MESSAGES).replace("{target}", opponent)
    return f"{core}{gap_lines}{core}{gap_lines}{core}\n🔱MANSURI GOD  [{rand_id}] 🔱"

async def block_media(route):
    if route.request.resource_type in ["image", "media", "font"]:
        await route.abort()
    else:
        await route.continue_()

async def force_name_lock(page, gc_name):
    try:
        gear = page.locator('svg[aria-label="Conversation information"]')
        await gear.click()
        change_btn = page.locator('div[aria-label="Change group name"][role="button"]')
        group_input = page.locator('input[aria-label="Group name"][name="change-group-name"]')
        save_btn = page.locator('div[role="button"]:has-text("Save")')
        await change_btn.click()
        await group_input.fill(gc_name)
        if await save_btn.is_enabled():
            await save_btn.click()
            print(f"\033[1;32m    🔒 [LOCK] NAME RESET TO: {gc_name}\033[0m")
        await gear.click()
    except Exception as e:
        print(f"    ⚠️ [SYSTEM] LOCK ERROR: {e}")
        await page.reload()

async def run_engine(engine_id, sid, url, opponent, gc_name, is_locker):
    user_data_dir = f"./session_data_{engine_id}"
    while True:
        async with async_playwright() as p:
            browser = await p.chromium.launch_persistent_context(
                user_data_dir, headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            )
            await browser.add_cookies([{"name": "sessionid", "value": sid, "domain": ".instagram.com", "path": "/", "secure": True, "httpOnly": True}])
            page = await browser.new_page()
            await page.route("**/*", block_media)
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                msg_box = page.locator('div[role="textbox"], div[aria-label="Message"]').first
                
                msg_count = 0
                for _ in range(150): 
                    if msg_count > 0 and msg_count % 30 == 0:
                        print(f"    \033[1;33m🧹 [E-{engine_id}] SOFT PURGE: RELOADING DOM...\033[0m")
                        await page.reload(wait_until='domcontentloaded')
                        msg_box = page.locator('div[role="textbox"], div[aria-label="Message"]').first
                        await msg_box.focus()

                    if is_locker and msg_count >= 19:
                        await force_name_lock(page, gc_name)
                        msg_count = 0
                        await msg_box.focus()
                    
                    await msg_box.focus()
                    # 🔱 Payload now includes the opponent names
                    await msg_box.fill(get_payload(opponent)) 
                    await page.keyboard.press("Enter")
                    
                    msg_count += 1
                    status = "\033[1;35mLOCKER\033[0m" if is_locker else "\033[1;36mSLAMMER\033[0m"
                    print(f"    \033[1;37m[E-{engine_id}] [{status}] STRIKE: {msg_count} | BY 𝙈𝘼𝙉𝙎𝙐𝙍𝙄xGOD\033[0m")
                    await asyncio.sleep(random.uniform(DELAY, DELAY + 0.1))
                    
            except Exception as e:
                print(f"    ⚠️ [E-{engine_id}] RELOADING ENGINE: {e}")
            
            await browser.close()
            if os.path.exists(user_data_dir):
                shutil.rmtree(user_data_dir, ignore_errors=True)
            await asyncio.sleep(1)

async def main():
    professional_banner()
    choice = input("\033[1;37m    🔱 USE DEFAULT SETTINGS BY MANSURIxGOD ? (y/n): \033[0m").strip().lower()
    
    if choice in ['y', 'yes']:
        sids, url, opponent, engine_count = DEFAULT_SID, DEFAULT_URL, DEFAULT_OPPONENT, DEFAULT_ENGINE_COUNT
    else:
        multi = input("    🔱 MULTI-ID MODE? (y/n): ").strip().lower()
        if multi in ['y', 'yes']:
            sids = [s.strip() for s in input("    🔱 SESSIONS (comma separated): ").split(',')]
        else:
            sids = [input("    🔱 SESSION ID: ").strip()]
        url = input("    🔱 GROUP URL: ").strip()
        opponent = input("    🔱 OPPONENT NAME: ").strip()
        engine_count = int(input("    🔱 ENGINE COUNT (3-4): ").strip() or 4)

    gc_name = f"[{opponent}] की मां चुदके पागल"
    professional_banner()
    countdown(5)
    
    tasks = [run_engine(i+1, sids[i % len(sids)], url, opponent, gc_name, i == 0) for i in range(engine_count)]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\033[1;31m   👑MANSURI GOD STOPPED BY USER. CLEANING CACHE... \033[0m")
        sys.exit()
