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
        self.wfile.write(b"Delhi Destination Bot Operational")

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

# --- 3. MONITORING & DESTINATION FILTERING LOGIC ---
def monitor_delhi_go_arounds():
    global hourly_go_around_count

    current_time_str = time.strftime("%H:%M:%S")
    print(f"[{current_time_str}] 📡 Polling flights destined for Delhi Airport...")

    try:
        bounds = fr_api.get_bounds_by_point(DEL_LAT, DEL_LON, 35000)
        aircraft_list = fr_api.get_flights(bounds=bounds)
    except requests.exceptions.Timeout:
        print("⚠️ FlightRadar24 API timeout (60s cycle skipped).")
        send_telegram_alert("⚠️ *FlightRadar24 API Timeout*: Request timed out. Retrying in 60s...")
        return
    except Exception as e:
        print(f"⚠️ API query failed: {e}")
        return

    current_seen_icaos = set()
    delhi_bound_flights = []

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
        # 1. Destination IATA is explicitly "DEL"
        # 2. Or fallback: High altitude entry (>=2500ft) and descending near DEL
        is_destined_for_delhi = (dest == "DEL") or (alt_agl >= 2500 and vspeed_ftmin < 0)

        if dist_km <= 30.0 and is_destined_for_delhi:
            current_seen_icaos.add(icao24)
            delhi_bound_flights.append({
                'icao24': icao24,
                'callsign': callsign,
                'origin': origin,
                'dest': dest,
                'alt_ft': alt_ft,
                'alt_agl': alt_agl,
                'vspeed_ftmin': vspeed_ftmin,
                'ground_speed': ground_speed,
                'dist_km': dist_km
            })

            prev = flight_history.get(icao24)

            if prev:
                prev['is_arrival'] = True # Confirmed inbound to Delhi

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
                    'is_arrival': True,
                    'alert_sent': False
                }

    # --- 4. BUILD MINUTELY TELEMETRY REPORT FOR DELHI ARRIVALS ONLY ---
    report_lines = [f"📡 *DELHI ARRIVALS REPORT — {current_time_str} IST*"]
    report_lines.append(f"🛬 *Inbound Flights to DEL (30km Scope)*: `{len(delhi_bound_flights)}`")
    report_lines.append("───────────────────────")

    if not delhi_bound_flights:
        report_lines.append("ℹ️ _No Delhi-bound arrivals active in 30km radius._")
    else:
        for item in delhi_bound_flights[:10]:
            icao = item['icao24']
            prev_data = flight_history.get(icao)
            
            if prev_data and 'last_alt_ft' in prev_data:
                alt_diff = item['alt_ft'] - prev_data['last_alt_ft']
                if alt_diff > 0:
                    delta_str = f" (▲+{alt_diff}ft)"
                elif alt_diff < 0:
                    delta_str = f" (▼{alt_diff}ft)"
                else:
                    delta_str = " (═ 0ft)"
            else:
                delta_str = " (🆕 New)"

            vspd = item['vspeed_ftmin']
            vspd_str = f"+{vspd}" if vspd > 0 else f"{vspd}"

            report_lines.append(
                f"• `{item['callsign']}` ({item['origin']} ➔ DEL)\n"
                f"  └ Alt: `{item['alt_ft']}ft`{delta_str} | VSpd: `{vspd_str}ft/min` | Dist: `{round(item['dist_km'], 1)}km`"
            )

    send_telegram_alert("\n".join(report_lines))

    # Clean up stale flights
    stale_keys = [k for k in flight_history if k not in current_seen_icaos]
    for k in stale_keys:
        del flight_history[k]

# --- 5. HOURLY SUMMARY REPORTING LOGIC ---
def check_and_send_hourly_report():
    global hourly_go_around_count, last_hourly_report_time

    current_time = time.time()
    if current_time - last_hourly_report_time >= 3600:
        report_msg = (
            f"📊 *HOURLY SUMMARY REPORT — DELHI (DEL/VIDP)*\n\n"
            f"🟢 *Status*: Monitoring Delhi Arrivals Only\n"
            f"🚨 *Total Go-Arounds in past hour*: `{hourly_go_around_count}`"
        )
        send_telegram_alert(report_msg)
        hourly_go_around_count = 0
        last_hourly_report_time = current_time

# --- 6. MAIN EXECUTION LOOP ---
if __name__ == "__main__":
    print("🚀 24/7 Render Delhi Go-Around Service Starting...")
    send_telegram_alert("✅ *Bot Updated: Filtering EXCLUSIVELY for Delhi Arrivals (DEL).*")

    while True:
        try:
            monitor_delhi_go_arounds()
            check_and_send_hourly_report()
        except Exception as e:
            print(f"⚠️ Unexpected error in main loop: {e}")
        
        time.sleep(60)
