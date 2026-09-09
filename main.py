import os
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from FlightRadarAPI import FlightRadar24API

# --- 1. DUMMY WEB SERVER (Keeps Render Free Tier Active) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Delhi Go-Around Monitor Active")

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# --- 2. BOT CONFIGURATION & CONSTANTS ---
TELEGRAM_BOT_TOKEN = "8451032835:AAFz9Mqvpx-xndjfDuSDX8GtC8bXdnwB2RI"
TELEGRAM_CHAT_ID = "5325948125"

DEL_LAT = 28.5562
DEL_LON = 77.1000
DEL_ELEVATION_FT = 777

fr_api = FlightRadar24API()

flight_history = {}
hourly_go_around_count = 0
last_hourly_report_time = time.time()

def send_telegram_alert(msg):
    """Sends Markdown formatted alert to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"⚠️ Telegram alert failed: {e}")

# --- 3. MONITORING & GO-AROUND DETECTION LOGIC ---
def monitor_delhi_go_arounds():
    global hourly_go_around_count

    current_time_str = time.strftime("%H:%M:%S")

    try:
        bounds = fr_api.get_bounds_by_point(DEL_LAT, DEL_LON, 35000)
        aircraft_list = fr_api.get_flights(bounds=bounds)
    except requests.exceptions.Timeout:
        print(f"[{current_time_str}] ⚠️ API timeout (60s cycle skipped).")
        return
    except Exception as e:
        print(f"[{current_time_str}] ⚠️ API query failed: {e}")
        return

    current_seen_icaos = set()

    for ac in aircraft_list:
        icao24 = getattr(ac, 'icao_24bit', None)
        if not icao24:
            continue
        
        icao24 = icao24.upper()
        callsign = getattr(ac, 'callsign', None) or "UNKNOWN"
        callsign = callsign.strip() if isinstance(callsign, str) else "UNKNOWN"
        
        alt_ft = getattr(ac, 'altitude', None)
        vspeed_ftmin = getattr(ac, 'vertical_speed', None)
        ground_speed = getattr(ac, 'ground_speed', 0)
        lat = getattr(ac, 'latitude', None)
        lon = getattr(ac, 'longitude', None)

        if None in [alt_ft, vspeed_ftmin, lat, lon]:
            continue

        dist_km = ac.get_distance_from(fr_api.get_airport("DEL")) if hasattr(ac, 'get_distance_from') else 0.0
        alt_agl = max(0, alt_ft - DEL_ELEVATION_FT)

        # Extract origin/destination codes
        dest = getattr(ac, 'destination_airport_iata', None) or "???"
        origin = getattr(ac, 'origin_airport_iata', None) or "???"

        # --- EXCLUSIVE DESTINATION FILTERING ---
        is_destined_for_delhi = (dest == "DEL") or (alt_agl >= 2500 and vspeed_ftmin < 0)

        if dist_km <= 30.0 and is_destined_for_delhi:
            current_seen_icaos.add(icao24)
            prev = flight_history.get(icao24)

            if prev:
                # --- GO-AROUND EVALUATION ---
                was_on_low_approach = prev['lowest_alt_agl'] <= 1500
                was_descending = prev['last_vspeed'] <= 200
                is_climbing_now = vspeed_ftmin >= 600

                if was_on_low_approach and was_descending and is_climbing_now:
                    if not prev.get('alert_sent'):
                        hourly_go_around_count += 1
                        alert_msg = (
                            f"🚨 *CONFIRMED GO-AROUND AT DELHI (DEL/VIDP)* 🚨\n\n"
                            f"✈️ *Flight/Callsign*: `{callsign}`\n"
                            f"🛫 *Route*: `{origin} ➔ DEL`\n"
                            f"🆔 *ICAO*: `{icao24}`\n"
                            f"📏 *Current Altitude*: `{int(alt_ft)} ft MSL` (`~{int(alt_agl)} ft AGL`)\n"
                            f"📉 *Lowest Approach Alt*: `{int(prev['lowest_alt_agl'])} ft AGL`\n"
                            f"📈 *Climb Rate*: `+{int(vspeed_ftmin)} ft/min`\n"
                            f"🚀 *Ground Speed*: `{int(ground_speed)} kts`\n"
                            f"📍 *Distance*: `{round(dist_km, 2)} km`"
                        )
                        print(f"🔥 [GO-AROUND DETECTED] {callsign}")
                        send_telegram_alert(alert_msg)
                        prev['alert_sent'] = True

                prev['lowest_alt_agl'] = min(prev['lowest_alt_agl'], alt_agl)
                prev['last_vspeed'] = vspeed_ftmin
                prev['last_alt_ft'] = alt_ft
                flight_history[icao24] = prev

            else:
                flight_history[icao24] = {
                    'callsign': callsign,
                    'entry_alt_agl': alt_agl,
                    'lowest_alt_agl': alt_agl,
                    'last_vspeed': vspeed_ftmin,
                    'last_alt_ft': alt_ft,
                    'alert_sent': False
                }

    # Clean up stale flights
    stale_keys = [k for k in flight_history if k not in current_seen_icaos]
    for k in stale_keys:
        del flight_history[k]

# --- 4. HOURLY SUMMARY REPORTING LOGIC ---
def check_and_send_hourly_report():
    global hourly_go_around_count, last_hourly_report_time

    current_time = time.time()
    if current_time - last_hourly_report_time >= 3600:
        report_msg = (
            f"📊 *HOURLY SUMMARY REPORT — DELHI (DEL/VIDP)*\n\n"
            f"🟢 *Status*: Actively Monitoring Delhi Arrivals\n"
            f"🚨 *Total Go-Arounds in Past Hour*: `{hourly_go_around_count}`"
        )
        send_telegram_alert(report_msg)
        hourly_go_around_count = 0
        last_hourly_report_time = current_time

# --- 5. MAIN EXECUTION LOOP ---
if __name__ == "__main__":
    print("🚀 Delhi Go-Around Monitor Started (Go-Around & Hourly Alerts Only)...")
    send_telegram_alert("✅ *Bot Started*: Minutely reports disabled. Telegram alerts will only trigger on Go-Around events.")

    while True:
        try:
            monitor_delhi_go_arounds()
            check_and_send_hourly_report()
        except Exception as e:
            print(f"⚠️ Unexpected error in main loop: {e}")
        
        time.sleep(60)
