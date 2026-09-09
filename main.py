import os
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from FlightRadarAPI import FlightRadar24API
from geopy.distance import geodesic

# --- DUMMY WEB SERVER (Keeps Render Free Tier Active) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Delhi Go-Around Bot Active")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# Start background web server in a separate thread
threading.Thread(target=run_dummy_server, daemon=True).start()

# --- BOT CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8451032835:AAFz9Mqvpx-xndjfDuSDX8GtC8bXdnwB2RI"
TELEGRAM_CHAT_ID = "-5410082469"

# Delhi Airport (DEL / VIDP) Parameters
DEL_LAT = 28.5562
DEL_LON = 77.1000
DEL_ELEVATION_FT = 777  # Runway Elevation MSL

fr_api = FlightRadar24API()

# Tracking memory and hourly reporting counters
flight_history = {}
hourly_go_around_count = 0
last_hourly_report_time = time.time()

def send_telegram_alert(msg):
    """Sends immediate Markdown alert to Telegram."""
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
def monitor_delhi_go_arounds():
    global hourly_go_around_count

    try:
        # Log every time an API call is triggered
        print(f"[{time.strftime('%H:%M:%S')}] 📡 Fetching FlightRadar24 data for Delhi Airport...")

        bounds = fr_api.get_bounds_by_point(DEL_LAT, DEL_LON, 35000)
        aircraft_list = fr_api.get_flights(bounds=bounds)
        print(f"[{time.strftime('%H:%M:%S')}] ✅ Received {len(aircraft_list)} tracked aircraft.")
    except requests.exceptions.Timeout:
        # ...

def monitor_delhi_go_arounds():
    global hourly_go_around_count

    try:
        # Fetch flights within 35km radius of Delhi Airport
        bounds = fr_api.get_bounds_by_point(DEL_LAT, DEL_LON, 35000)
        aircraft_list = fr_api.get_flights(bounds=bounds)
    except requests.exceptions.Timeout:
        print("⚠️ FlightRadar24 API query timed out. Retrying on next 30s cycle...")
        return
    except Exception as e:
        print(f"⚠️ API query failed: {e}")
        return

    current_seen = set()

    for ac in aircraft_list:
        icao24 = ac.icao_24bit.upper() if ac.icao_24bit else None
        callsign = ac.callsign.strip() if ac.callsign else "UNKNOWN"
        lat = ac.latitude
        lon = ac.longitude
        alt_ft = ac.altitude             # Altitude MSL (ft)
        vspeed_ftmin = ac.vertical_speed # Vertical Speed (ft/min)
        ground_speed = ac.ground_speed   # Knots

        if not icao24 or None in [lat, lon, alt_ft, vspeed_ftmin]:
            continue

        current_seen.add(icao24)
        dist_km = geodesic((DEL_LAT, DEL_LON), (lat, lon)).km
        alt_agl = max(0, alt_ft - DEL_ELEVATION_FT)

        # Evaluate flights within 30 km radius
        if dist_km <= 30:
            prev = flight_history.get(icao24)

            if prev:
                # Flag confirmed arrival: Entered high (>=2500 ft AGL) and descending
                if prev['entry_alt_agl'] >= 2500 and vspeed_ftmin < 0:
                    prev['is_arrival'] = True

                # Flag departure: First seen low (<=800 ft AGL) and climbing
                if prev['first_seen_alt_agl'] <= 800 and vspeed_ftmin > 300 and not prev['is_arrival']:
                    prev['is_departure'] = True

                # --- GO-AROUND CRITERIA ---
                is_valid_arrival = prev['is_arrival'] and not prev['is_departure']
                was_on_low_approach = prev['lowest_alt_agl'] <= 1500
                was_descending = prev['last_vspeed'] <= 200
                is_climbing_now = vspeed_ftmin >= 600

                if is_valid_arrival and was_on_low_approach and was_descending and is_climbing_now:
                    if not prev.get('alert_sent'):
                        hourly_go_around_count += 1
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

                # Update flight state history
                prev['lowest_alt_agl'] = min(prev['lowest_alt_agl'], alt_agl)
                prev['last_vspeed'] = vspeed_ftmin
                prev['last_alt_ft'] = alt_ft
                flight_history[icao24] = prev

            else:
                # Register new incoming flight state
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

    # Clean up stale flights out of airspace scope
    stale_keys = [k for k in flight_history if k not in current_seen]
    for k in stale_keys:
        del flight_history[k]

def check_and_send_hourly_report():
    """Checks if 1 hour (3600 seconds) has elapsed and sends status report."""
    global hourly_go_around_count, last_hourly_report_time

    current_time = time.time()
    if current_time - last_hourly_report_time >= 3600:
        if hourly_go_around_count == 0:
            report_msg = (
                f"ℹ️ *HOURLY STATUS REPORT — DELHI AIRPORT (DEL/VIDP)*\n\n"
                f"🟢 *Status*: Operational & Monitoring Arrivals\n"
                f"📊 *Go-Arounds in past hour*: `0`"
            )
        else:
            report_msg = (
                f"📊 *HOURLY STATUS REPORT — DELHI AIRPORT (DEL/VIDP)*\n\n"
                f"🟢 *Status*: Operational & Monitoring Arrivals\n"
                f"🚨 *Total Go-Arounds detected in past hour*: `{hourly_go_around_count}`"
            )
        
        print(f"[REPORT] Sending hourly update. Total Go-Arounds: {hourly_go_around_count}")
        send_telegram_alert(report_msg)

        # Reset hourly counter and time anchor
        hourly_go_around_count = 0
        last_hourly_report_time = current_time

if __name__ == "__main__":
    print("🚀 24/7 Render Delhi Go-Around Service Starting (30s interval + Hourly Reports)...")
    send_telegram_alert("✅ *Delhi Airport Bot Updated: 30s Scan Interval & Hourly Reports Enabled*")
    
    while True:
        monitor_delhi_go_arounds()
        check_and_send_hourly_report()
        time.sleep(30)  # Polling interval set to 30 seconds to prevent API timeouts
