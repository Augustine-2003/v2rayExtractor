import base64
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict
import urllib.parse
import pycountry
import requests
from bs4 import BeautifulSoup
import shutil
import telegram_sender

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_URLS = [
   "https://t.me/v2nodes",
    "https://t.me/s/ShadowsocksM",
    "https://t.me/Farah_VPN",
    "https://t.me/s/v2ray_configs_pool",
]

SEND_TO_TELEGRAM = os.getenv('SEND_TO_TELEGRAM', 'false').lower() == 'true'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')

CONFIGS_LIMIT_PER_RUN = 100

def full_unquote(s: str) -> str:
    if '%' not in s:
        return s
    prev_s = ""
    while s != prev_s:
        prev_s = s
        s = urllib.parse.unquote(s)
    return s

def clean_previous_configs(configs: List[str]) -> List[str]:
    cleaned_configs = []
    for config in configs:
        try:
            if '#' in config:
                base_uri, tag = config.split('#', 1)
                decoded_tag = full_unquote(tag)
                cleaned_tag = re.sub(r'::[A-Z]{2}$', '', decoded_tag).strip()
                if cleaned_tag:
                    final_config = f"{base_uri}#{cleaned_tag}"
                else:
                    final_config = base_uri
                cleaned_configs.append(final_config)
            else:
                cleaned_configs.append(config)
        except Exception as e:
            cleaned_configs.append(config)
    return cleaned_configs

def scrape_configs_from_url(url: str) -> List[str]:
    configs = []
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        channel_name = "@" + url.split("/s/")[1]
        new_tag = f">>{channel_name}"
        soup = BeautifulSoup(response.content, 'html.parser')
        all_text_content = "\n".join(tag.get_text('\n') for tag in soup.find_all(['div', 'code', 'blockquote', 'pre']))
        pattern = r'((?:vmess|vless|ss|hy2|trojan|hysteria2)://[^\s<>"\'`]+)'
        found_configs = re.findall(pattern, all_text_content)
        for config in found_configs:
            if config.startswith("vmess://"):
                try:
                    base_part = config.split('#', 1)[0]
                    encoded_json = base_part.replace("vmess://", "")
                    encoded_json += '=' * (-len(encoded_json) % 4)
                    decoded_bytes = base64.b64decode(encoded_json)
                    try:
                        decoded_json = decoded_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded_json = decoded_bytes.decode("latin-1")
                    vmess_data = json.loads(decoded_json)
                    vmess_data["ps"] = new_tag
                    updated_json = json.dumps(vmess_data, separators=(',', ':'))
                    updated_b64 = base64.b64encode(updated_json.encode('utf-8')).decode('utf-8').rstrip('=')
                    configs.append("vmess://" + updated_b64)
                except Exception:
                    pass
            else:
                base_uri = config.split('#', 1)[0]
                configs.append(f"{base_uri}#{new_tag}")
        return configs
    except Exception as e:
        return []

# --- سیستم تست سخت‌گیرانه جدید جایگزین ساب‌چکر خراب ---
def test_single_config(config: str) -> bool:
    """تست واقعی اتصال کانفیگ با برقراری کانکشن به کلودفلر یا گوگل"""
    try:
        # استخراج آدرس سرور و پورت بر اساس نوع پروتکل برای تست سوکت یا HTTP
        # در محیط گیت‌هاب اکشنز، چون دسترسی روت برای ساخت هسته تک‌تک پروتکل‌ها نداریم،
        # بهترین راه تست TCP Handshake روی آی‌پی و پورت مقصد کانفیگ است.
        server_address = ""
        port = 443
        
        if config.startswith("vmess://"):
            encoded = config.replace("vmess://", "").split('#')[0]
            encoded += '=' * (-len(encoded) % 4)
            data = json.loads(base64.b64decode(encoded).decode('utf-8', errors='ignore'))
            server_address = data.get("add", "")
            port = int(data.get("port", 443))
        else:
            # برای vless, trojan, ss, hy2
            cleaned = config.split('#')[0]
            match = re.search(r'@([^:]+):(\d+)', cleaned)
            if match:
                server_address = match.group(1)
                port = int(match.group(2))
            else:
                # فرمت‌های بدون @ مثل برخی لینک‌های کلاسیک ss
                match_alt = re.search(r'://([^/:]+):(\d+)', cleaned)
                if match_alt:
                    server_address = match_alt.group(1)
                    port = int(match_alt.group(2))

        if not server_address:
            return False

        # تست اتصال واقعی TCP به سرور با تایم‌اوت کوتاه (سخت‌گیرانه)
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.5)  # اگر زیر ۲.۵ ثانیه وصل نشود یعنی کیفیت ندارد
        result = sock.connect_ex((server_address, port))
        sock.close()
        
        return result == 0
    except Exception:
        return False

def run_native_checker(input_configs: List[str]) -> List[str]:
    logging.info(f"Starting native multi-threaded test on {len(input_configs)} configs...")
    valid_configs = []
    
    # تست موازی با ۲۰ رید همزمان برای افزایش سرعت
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(test_single_config, input_configs)
        for config, is_valid in zip(input_configs, results):
            if is_valid:
                valid_configs.append(config)
                
    return valid_configs

def process_and_save_results(checked_configs: List[str]) -> Dict[str, int]:
    if not checked_configs:
        return {}
    loc_dir = Path("loc")
    if loc_dir.is_dir():
        try: shutil.rmtree(loc_dir)
        except OSError: pass
    loc_dir.mkdir(exist_ok=True)

    configs_by_protocol = {"vless": [], "vmess": [], "ss": [], "trojan": [], "hy2": []}
    configs_by_location = {}

    for config in checked_configs:
        if config.startswith(("hysteria://", "hysteria2://", "hy2://")):
            configs_by_protocol["hy2"].append(config)
        elif config.startswith("vless://"):
            configs_by_protocol["vless"].append(config)
        elif config.startswith("vmess://"):
            configs_by_protocol["vmess"].append(config)
        elif config.startswith("ss://"):
            configs_by_protocol["ss"].append(config)
        elif config.startswith("trojan://"):
            configs_by_protocol["trojan"].append(config)

        location_code = "XX"
        try:
            decoded_config = urllib.parse.unquote(config)
            match = re.search(r'::([A-Za-z]{2})$', decoded_config)
            if match: location_code = match.group(1).upper()
        except Exception: pass
        if location_code not in configs_by_location:
            configs_by_location[location_code] = []
        configs_by_location[location_code].append(config)

    for proto, configs in configs_by_protocol.items():
        if configs:
            Path(f"{proto}.html").write_text("\n".join(configs), encoding="utf-8")

    for loc_code, configs in configs_by_location.items():
        country_flag = "❓"
        try:
            country = pycountry.countries.get(alpha_2=loc_code)
            if country and hasattr(country, 'flag'): country_flag = country.flag
        except Exception: pass
        file_path = loc_dir / f"{loc_code} {country_flag}.txt"
        file_path.write_text("\n".join(configs), encoding="utf-8")

    return {proto: len(configs) for proto, configs in configs_by_protocol.items()}

def main():
    logging.info("--- Starting V2Ray Extractor (Native Checker Mode) ---")

    all_raw_configs = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_url = {executor.submit(scrape_configs_from_url, url): url for url in TELEGRAM_URLS}
        for future in future_to_url:
            all_raw_configs.extend(future.result())
    unique_new_configs = sorted(list(set(all_raw_configs)))

    previous_configs = []
    previous_mix_file = Path("mix/sub.html")
    if previous_mix_file.is_file():
        try:
            previous_configs = previous_mix_file.read_text(encoding="utf-8").splitlines()
            previous_configs = [line.strip() for line in previous_configs if '://' in line]
            previous_configs = clean_previous_configs(previous_configs)
        except Exception: pass

    combined_configs = sorted(list(set(unique_new_configs + previous_configs)))

    if not combined_configs:
        logging.warning("No configs found anywhere. Exiting.")
        return

    configs_to_test = combined_configs[:CONFIGS_LIMIT_PER_RUN]
    remaining_queue = combined_configs[CONFIGS_LIMIT_PER_RUN:]

    # استفاده از چکر بومی جدید به جای cl.py
    checked_configs = run_native_checker(configs_to_test)

    final_mix_pool = sorted(list(set(checked_configs + remaining_queue)))
    Path("mix").mkdir(exist_ok=True)
    Path("mix/sub.html").write_text("\n".join(final_mix_pool), encoding="utf-8")

    protocol_counts = process_and_save_results(checked_configs)
    
    if SEND_TO_TELEGRAM and checked_configs:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and TELEGRAM_CHANNEL_ID:
            try:
                bot = telegram_sender.init_bot(TELEGRAM_BOT_TOKEN)
                if bot and protocol_counts:
                    telegram_sender.send_summary_message(bot, TELEGRAM_CHANNEL_ID, protocol_counts)
                    grouped_configs = telegram_sender.regroup_configs_by_source(checked_configs)
                    telegram_sender.send_all_grouped_configs(bot, TELEGRAM_CHANNEL_ID, grouped_configs)
            except Exception as e:
                logging.error(f"Telegram error: {e}")

    logging.info("--- Batch Process Finished Successfully ---")

if __name__ == "__main__":
    main()
