import base64
import json
import logging
import os
import re
import subprocess
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
    "https://t.me/s/prrofile_purple",
    "https://t.me/s/ShadowsocksM",
    "https://t.me/s/ip_cf",
    "https://t.me/s/v2ray_configs_pool",
]

SEND_TO_TELEGRAM = os.getenv('SEND_TO_TELEGRAM', 'false').lower() == 'true'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
SUB_CHECKER_DIR = Path("sub-checker")

# میزان محدودیت تست در هر بار اجرا
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
            logging.warning(f"Could not clean config, adding original: {config[:50]}... Error: {e}")
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
                except Exception as e:
                    logging.warning(f"Could not parse vmess config, skipping: {config[:50]}... Error: {e}")
            else:
                base_uri = config.split('#', 1)[0]
                configs.append(f"{base_uri}#{new_tag}")
        logging.info(f"Found and re-tagged {len(configs)} configs in {url}")
        return configs
    except Exception as e:
        logging.error(f"Could not fetch or parse {url}: {e}")
        return []

def run_sub_checker(input_configs: List[str]) -> List[str]:
    if not SUB_CHECKER_DIR.is_dir():
        logging.error(f"Sub-checker directory not found at '{SUB_CHECKER_DIR}'")
        return []
    normal_txt_path = SUB_CHECKER_DIR / "normal.txt"
    final_txt_path = SUB_CHECKER_DIR / "final.txt"
    cl_py_path = SUB_CHECKER_DIR / "cl.py"
    
    if final_txt_path.exists():
        final_txt_path.unlink()

    logging.info(f"Writing {len(input_configs)} configs to '{normal_txt_path}'")
    normal_txt_path.write_text("\n".join(input_configs), encoding="utf-8")
    logging.info("Running sub-checker script (cl.py)...")
    try:
        process = subprocess.run(
            ["python", cl_py_path.name],
            cwd=SUB_CHECKER_DIR,
            capture_output=True,
            text=True,
            timeout=1200
        )
        if final_txt_path.exists():
            logging.info("Reading checked configs from 'final.txt'")
            checked_configs = final_txt_path.read_text(encoding="utf-8").splitlines()
            return [line for line in checked_configs if line.strip()]
        else:
            logging.error("'final.txt' was not created by the sub-checker.")
            return []
    except subprocess.TimeoutExpired:
        logging.error("Sub-checker script timed out.")
        return []
    except Exception as e:
        logging.error(f"An error occurred while running sub-checker: {e}")
        return []

def process_and_save_results(checked_configs: List[str]) -> Dict[str, int]:
    if not checked_configs:
        logging.warning("No checked configs to process.")
        return {}
    loc_dir = Path("loc")
    if loc_dir.is_dir():
        try:
            shutil.rmtree(loc_dir)
        except OSError as e:
            logging.error(f"Error removing directory {loc_dir}: {e}")
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
            if match:
                location_code = match.group(1).upper()
        except Exception:
            pass
        if location_code not in configs_by_location:
            configs_by_location[location_code] = []
        configs_by_location[location_code].append(config)

    for proto, configs in configs_by_protocol.items():
        if configs:
            file_path = Path(f"{proto}.html")
            file_path.write_text("\n".join(configs), encoding="utf-8")

    for loc_code, configs in configs_by_location.items():
        country_flag = "❓"
        try:
            country = pycountry.countries.get(alpha_2=loc_code)
            if country and hasattr(country, 'flag'):
                country_flag = country.flag
        except Exception:
            pass
        file_path = loc_dir / f"{loc_code} {country_flag}.txt"
        file_path.write_text("\n".join(configs), encoding="utf-8")

    protocol_counts = {proto: len(configs) for proto, configs in configs_by_protocol.items()}
    return protocol_counts

def main():
    logging.info("--- Starting V2Ray Extractor (Batch Mode) ---")

    # 1. جمع‌آوری از تلگرام
    all_raw_configs = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_url = {executor.submit(scrape_configs_from_url, url): url for url in TELEGRAM_URLS}
        for future in future_to_url:
            all_raw_configs.extend(future.result())
    unique_new_configs = sorted(list(set(all_raw_configs)))
    logging.info(f"Collected {len(unique_new_configs)} unique new configs from Telegram.")

    # 2. خواندن صف یا کانفیگ‌های قبلی
    previous_configs = []
    previous_mix_file = Path("mix/sub.html")
    if previous_mix_file.is_file():
        try:
            previous_configs = previous_mix_file.read_text(encoding="utf-8").splitlines()
            previous_configs = [line.strip() for line in previous_configs if '://' in line]
            previous_configs = clean_previous_configs(previous_configs)
        except Exception as e:
            logging.error(f"Could not read historical file: {e}")

    # 3. ترکیب و ساخت مخزن کل کانفیگ‌ها
    combined_configs = sorted(list(set(unique_new_configs + previous_configs)))
    total_pool_size = len(combined_configs)
    logging.info(f"Total pool size available for processing: {total_pool_size}")

    if not combined_configs:
        logging.warning("No configs found anywhere. Exiting.")
        return

    # 4. جداسازی ۱۰۰ تای اول برای تست و نگه‌داشتن بقیه در صف
    configs_to_test = combined_configs[:CONFIGS_LIMIT_PER_RUN]
    remaining_queue = combined_configs[CONFIGS_LIMIT_PER_RUN:]
    logging.info(f"Processing batch of {len(configs_to_test)} configs now. {len(remaining_queue)} postponed for next runs.")

    # 5. اجرای چکر روی ۱۰۰ تا
    checked_configs = run_sub_checker(configs_to_test)
    logging.info(f"Sub-checker returned {len(checked_configs)} valid configs from this batch.")

    # 6. ترکیب مجدد کانفیگ‌های سالمِ تست‌شده با مابقی صف برای ذخیره در فایل پایگاه داده (mix/sub.html)
    # این باعث می‌شود کانفیگ‌های تست‌نشده برای ران‌های بعدی گیت‌هاب باقی بمانند
    final_mix_pool = sorted(list(set(checked_configs + remaining_queue)))
    Path("mix").mkdir(exist_ok=True)
    Path("mix/sub.html").write_text("\n".join(final_mix_pool), encoding="utf-8")
    logging.info(f"Saved update back to 'mix/sub.html'. Next run starts with remaining queue.")

    # 7. دسته‌بندی و ارسال به تلگرام
    protocol_counts = process_and_save_results(checked_configs)
    
    if SEND_TO_TELEGRAM and checked_configs:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not TELEGRAM_CHANNEL_ID:
            logging.warning("Telegram configuration missing in secrets. Skipping.")
        else:
            try:
                bot = telegram_sender.init_bot(TELEGRAM_BOT_TOKEN)
                if bot and protocol_counts:
                    logging.info("Sending batch summary and configs to Telegram.")
                    telegram_sender.send_summary_message(bot, TELEGRAM_CHANNEL_ID, protocol_counts)
                    grouped_configs = telegram_sender.regroup_configs_by_source(checked_configs)
                    telegram_sender.send_all_grouped_configs(bot, TELEGRAM_CHANNEL_ID, grouped_configs)
            except Exception as e:
                logging.error(f"Telegram notification error: {e}")

    logging.info("--- Batch Process Finished Successfully ---")

if __name__ == "__main__":
    main()
