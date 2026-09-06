import os
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from FlightRadarAPI import FlightRadar24API
from geopy.distance import geodesic

# --- DUMMY WEB SERVER (Keeps Render Free Tier Happy) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Delhi Go-Around Bot Active")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# Start background server in a separate thread
threading.Thread(target=run_dummy_server, daemon=True).start()

# --- BOT CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8451032835:AAFz9Mqvpx-xndjfDuSDX8GtC8bXdnwB2RI"
TELEGRAM_CHAT_ID = "5325948125"

DEL_LAT = 28.5562
DEL_LON = 77.1000
DEL_ELEVATION_FT = 777

fr_api = FlightRadar24API()
flight_history = {}

def send_telegram_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        res.raise_for_status()
    except Exception as e:
        print(f"⚠️ Telegram alert failed: {e}")

def monitor_delhi_go_arounds():
    try:
        bounds = fr_api.get_bounds_by_point(DEL_LAT, DEL_LON, 35000)
        aircraft_list = fr_api.get_flights(bounds=bounds)
    except Exception as e:
        print(f"⚠️ API query failed: {e}")
        return

    current_seen = set()

    for ac in aircraft_list:
        icao24 = ac.icao_24bit.upper() if ac.icao_24bit else None
        callsign = ac.callsign.strip() if ac.callsign else "UNKNOWN"
        lat = ac.latitude
        lon = ac.longitude
        alt_ft = ac.altitude             
        vspeed_ftmin = ac.vertical_speed 
        ground_speed = ac.ground_speed   

        if not icao24 or None in [lat, lon, alt_ft, vspeed_ftmin]:
            continue

        current_seen.add(icao24)
        dist_km = geodesic((DEL_LAT, DEL_LON), (lat, lon)).km
        alt_agl = max(0, alt_ft - DEL_ELEVATION_FT)

        if dist_km <= 30:
            prev = flight_history.get(icao24)

            if prev:
                if prev['entry_alt_agl'] >= 2500 and vspeed_ftmin < 0:
                    prev['is_arrival'] = True

                if prev['first_seen_alt_agl'] <= 800 and vspeed_ftmin > 300 and not prev['is_arrival']:
                    prev['is_departure'] = True

                is_valid_arrival = prev['is_arrival'] and not prev['is_departure']
                was_on_low_approach = prev['lowest_alt_agl'] <= 1500
                was_descending = prev['last_vspeed'] <= 200
                is_climbing_now = vspeed_ftmin >= 600

                if is_valid_arrival and was_on_low_approach and was_descending and is_climbing_now:
                    if not prev.get('alert_sent'):
                        alert_msg = (
                            f"🚨 *CONFIRMED GO-AROUND AT DELHI (DEL/VIDP)* 🚨\n\n"
                            f"✈️ *Flight/Callsign*: `{callsign}`\n"
                            f"🆔 *ICAO*: `{icao24}`\n"
                            f"📏 *Current Altitude*: `{int(alt_ft)} ft MSL` (`~{int(alt_agl)} ft AGL`)\n"
                            f"📉 *Lowest Approach Alt*: `{int(prev['lowest_alt_agl'])} ft AGL`\n"
                            f"📈 *Climb Rate*: `+{int(vspeed_ftmin)} ft/min`\n"
                            f"🚀 *Ground Speed*: `{int(ground_speed)} kts`\n"
                            f"📍 *Distance*: `{round(dist_km, 2)} km`"
                        )
                        print(f"[ALERT] True Go-Around Detected: {callsign}")
                        send_telegram_alert(alert_msg)
                        prev['alert_sent'] = True

                prev['lowest_alt_agl'] = min(prev['lowest_alt_agl'], alt_agl)
                prev['last_vspeed'] = vspeed_ftmin
                prev['last_alt_ft'] = alt_ft
                flight_history[icao24] = prev

            else:
                is_arr = alt_agl >= 2500 and vspeed_ftmin < 0
                is_dep = alt_agl <= 800 and vspeed_ftmin > 300

                flight_history[icao24] = {
                    'callsign': callsign,
                    'entry_alt_agl': alt_agl,
                    'first_seen_alt_agl': alt_agl,
                    'lowest_alt_agl': alt_agl,
                    'last_vspeed': vspeed_ftmin,
                    'last_alt_ft': alt_ft,
                    'is_arrival': is_arr,
                    'is_departure': is_dep,
                    'alert_sent': False
                }

    stale_keys = [k for k in flight_history if k not in current_seen]
    for k in stale_keys:
        del flight_history[k]

if __name__ == "__main__":
    print("🚀 24/7 Render Delhi Go-Around Service Starting...")
    send_telegram_alert("✅ *Delhi Airport Go-Around Bot Deployed 24/7 on Render (Free)*")
    
    while True:
        monitor_delhi_go_arounds()
        time.sleep(5)
