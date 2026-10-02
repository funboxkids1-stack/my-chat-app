import os
import sys
import re
import json
import time
import socket
import mimetypes
import threading
import subprocess
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

PUBLIC_URL = None
MAX_PARTICIPANTS_PER_ROOM = 2
HEARTBEAT_TIMEOUT = 40.0  # seconds

def get_local_ip():
    """Finds the local LAN IP address suitable for sharing on Wi-Fi/Hotspot."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

LOCAL_IP = get_local_ip()

class ChatRoom:
    def __init__(self, code):
        self.code = code
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.participants = {} # user_id -> info
        self.messages = []
        self.counter = 0
        self.signals = []      # WebRTC signaling queue
        self.signal_counter = 0
        self.last_active = time.time()

rooms_lock = threading.Lock()
rooms = {}

def get_or_create_room(code):
    clean_code = (code or "LOBBY").strip().upper()
    with rooms_lock:
        if clean_code not in rooms:
            rooms[clean_code] = ChatRoom(clean_code)
        return rooms[clean_code]

class ChatRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        try:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        return json.loads(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/info":
            self.handle_info(query)
        elif path == "/api/poll":
            self.handle_poll(query)
        elif path == "/api/history":
            self.handle_history(query)
        elif path.startswith("/uploads/"):
            self.handle_upload_file(path)
        else:
            self.handle_static_file(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/join":
            self.handle_join()
        elif path == "/api/send":
            self.handle_send()
        elif path == "/api/signal":
            self.handle_signal()
        elif path == "/api/typing":
            self.handle_typing()
        elif path == "/api/leave":
            self.handle_leave()
        elif path == "/api/clear":
            self.handle_clear()
        else:
            self._send_json({"error": "Not found"}, 404)

    def handle_static_file(self, path):
        if path == "/" or path == "":
            rel_path = "index.html"
        else:
            rel_path = path.lstrip("/")

        file_path = os.path.join(BASE_DIR, rel_path)
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            file_path = os.path.join(BASE_DIR, "index.html")

        mime, _ = mimetypes.guess_type(file_path)
        if not mime:
            mime = "text/plain"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_response(500)
            self.end_headers()

    def handle_upload_file(self, path):
        filename = os.path.basename(path)
        file_path = os.path.join(UPLOADS_DIR, filename)
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        mime, _ = mimetypes.guess_type(file_path)
        if not mime:
            mime = "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_response(500)
            self.end_headers()

    def handle_info(self, query):
        room_code = query.get("room", [""])[0].strip().upper()
        now = time.time()
        active_count = 0
        if room_code:
            room = get_or_create_room(room_code)
            with room.lock:
                stale = [uid for uid, p in room.participants.items() if now - p["last_seen"] > HEARTBEAT_TIMEOUT]
                for uid in stale:
                    del room.participants[uid]
                active_count = len(room.participants)

        res = {
            "local_ip": LOCAL_IP,
            "port": PORT,
            "local_url": f"http://localhost:{PORT}",
            "lan_url": f"http://{LOCAL_IP}:{PORT}",
            "public_url": PUBLIC_URL,
            "room_code": room_code,
            "participant_count": active_count,
            "max_participants": MAX_PARTICIPANTS_PER_ROOM
        }
        self._send_json(res)

    def handle_join(self):
        data = self._read_json()
        user_id = data.get("user_id")
        user_name = data.get("name", "").strip()
        avatar = data.get("avatar", "")
        room_code = (data.get("room_code") or "").strip().upper()
        now = time.time()

        if not user_id:
            self._send_json({"error": "Missing user_id"}, 400)
            return

        if not room_code:
            self._send_json({"error": "Please enter a Room Code to join."}, 400)
            return

        room = get_or_create_room(room_code)

        with room.cond:
            room.last_active = now
            stale = [uid for uid, p in room.participants.items() if uid != user_id and now - p["last_seen"] > HEARTBEAT_TIMEOUT]
            for uid in stale:
                del room.participants[uid]

            if user_id in room.participants:
                if user_name:
                    room.participants[user_id]["name"] = user_name
                if avatar:
                    room.participants[user_id]["avatar"] = avatar
                room.participants[user_id]["last_seen"] = now
                user_info = room.participants[user_id]
            else:
                if len(room.participants) >= MAX_PARTICIPANTS_PER_ROOM:
                    self._send_json({
                        "error": f"Room '{room_code}' is full (strictly 2 people only). Try another room code.",
                        "code": "ROOM_FULL"
                    }, 403)
                    return

                existing_roles = [p["role"] for p in room.participants.values()]
                role = "Person 1" if "Person 1" not in existing_roles else "Person 2"
                default_name = user_name if user_name else role

                user_info = {
                    "id": user_id,
                    "name": default_name,
                    "role": role,
                    "avatar": avatar or ("👤" if role == "Person 1" else "👥"),
                    "last_seen": now,
                    "is_typing": False
                }
                room.participants[user_id] = user_info

                room.counter += 1
                sys_msg = {
                    "id": room.counter,
                    "type": "system",
                    "text": f"👋 {user_info['name']} entered room '{room_code}' ({user_info['role']})",
                    "timestamp": now,
                    "time_str": time.strftime("%I:%M %p")
                }
                room.messages.append(sys_msg)

            room.cond.notify_all()

            peer = None
            for uid, p in room.participants.items():
                if uid != user_id:
                    peer = {
                        "id": uid,
                        "name": p["name"],
                        "role": p["role"],
                        "avatar": p["avatar"],
                        "online": True
                    }
                    break

            best_share_url = PUBLIC_URL if PUBLIC_URL else f"http://{LOCAL_IP}:{PORT}"

            res = {
                "success": True,
                "room_code": room_code,
                "you": user_info,
                "peer": peer,
                "share_url": f"{best_share_url}?room={room_code}",
                "public_url": PUBLIC_URL,
                "lan_url": f"http://{LOCAL_IP}:{PORT}?room={room_code}",
                "messages": room.messages[-50:]
            }
        self._send_json(res)

    def handle_poll(self, query):
        user_id = query.get("user_id", [None])[0]
        room_code = query.get("room", [""])[0].strip().upper()
        last_id = int(query.get("last_id", [0])[0])
        last_sig = int(query.get("last_sig", [0])[0])
        now = time.time()

        if not user_id or not room_code:
            self._send_json({"error": "Missing user_id or room"}, 400)
            return

        room = get_or_create_room(room_code)
        wait_time = 22.0
        start_time = time.time()

        with room.cond:
            if user_id in room.participants:
                room.participants[user_id]["last_seen"] = now

            while True:
                new_msgs = [m for m in room.messages if m["id"] > last_id]
                new_sigs = [s for s in room.signals if s["id"] > last_sig and s["sender_id"] != user_id]

                now = time.time()
                peer = None
                for uid, p in room.participants.items():
                    if uid != user_id:
                        is_online = (now - p["last_seen"]) <= HEARTBEAT_TIMEOUT
                        peer = {
                            "id": uid,
                            "name": p["name"],
                            "role": p["role"],
                            "avatar": p["avatar"],
                            "online": is_online,
                            "is_typing": p["is_typing"] and is_online
                        }
                        break

                elapsed = time.time() - start_time
                if new_msgs or new_sigs or elapsed >= wait_time:
                    self._send_json({
                        "messages": new_msgs,
                        "signals": new_sigs,
                        "peer": peer,
                        "connected_count": len(room.participants),
                        "public_url": PUBLIC_URL
                    })
                    return

                remaining = wait_time - elapsed
                if remaining <= 0:
                    break
                room.cond.wait(timeout=min(remaining, 2.0))

        self._send_json({"messages": [], "signals": [], "peer": peer, "connected_count": len(room.participants)})

    def handle_signal(self):
        """WebRTC signaling for audio and video calls."""
        data = self._read_json()
        sender_id = data.get("sender_id")
        room_code = (data.get("room_code") or "").strip().upper()
        sig_type = data.get("type")
        payload = data.get("payload")
        call_type = data.get("call_type", "video") # 'audio' or 'video'

        if not sender_id or not room_code or not sig_type:
            self._send_json({"error": "Invalid signal data"}, 400)
            return

        room = get_or_create_room(room_code)
        with room.cond:
            room.signal_counter += 1
            sig_obj = {
                "id": room.signal_counter,
                "sender_id": sender_id,
                "sender_name": room.participants.get(sender_id, {}).get("name", "Peer"),
                "sender_avatar": room.participants.get(sender_id, {}).get("avatar", "👤"),
                "type": sig_type,
                "call_type": call_type,
                "payload": payload,
                "timestamp": time.time()
            }
            room.signals.append(sig_obj)
            # Retain only last 50 signals
            if len(room.signals) > 50:
                room.signals.pop(0)

            room.cond.notify_all()

        self._send_json({"success": True, "signal_id": sig_obj["id"]})

    def handle_send(self):
        data = self._read_json()
        sender_id = data.get("sender_id")
        room_code = (data.get("room_code") or "").strip().upper()
        text = data.get("text", "").strip()
        attachment = data.get("attachment")

        if not sender_id or not room_code or (not text and not attachment):
            self._send_json({"error": "Cannot send empty message"}, 400)
            return

        room = get_or_create_room(room_code)
        now = time.time()
        file_info = None

        if attachment and "data_url" in attachment:
            try:
                data_url = attachment["data_url"]
                header, encoded = data_url.split(",", 1)
                import base64
                file_bytes = base64.b64decode(encoded)
                clean_name = os.path.basename(attachment.get("name", "file"))
                safe_name = f"{int(now * 1000)}_{clean_name}"
                dest = os.path.join(UPLOADS_DIR, safe_name)
                with open(dest, "wb") as f:
                    f.write(file_bytes)
                file_info = {
                    "filename": attachment.get("name", "file"),
                    "url": f"/uploads/{safe_name}",
                    "size": len(file_bytes),
                    "type": attachment.get("type", "application/octet-stream")
                }
            except Exception as e:
                print("Error saving attachment:", e)

        with room.cond:
            sender = room.participants.get(sender_id, {
                "name": data.get("sender_name", "Anonymous"),
                "role": "Guest",
                "avatar": "💬"
            })
            if sender_id in room.participants:
                room.participants[sender_id]["last_seen"] = now
                room.participants[sender_id]["is_typing"] = False

            room.counter += 1
            msg = {
                "id": room.counter,
                "type": "chat",
                "sender_id": sender_id,
                "sender_name": sender.get("name", "Anonymous"),
                "role": sender.get("role", ""),
                "avatar": sender.get("avatar", "💬"),
                "text": text,
                "file": file_info,
                "timestamp": now,
                "time_str": time.strftime("%I:%M %p")
            }
            room.messages.append(msg)
            if len(room.messages) > 1000:
                room.messages.pop(0)

            room.cond.notify_all()

        self._send_json({"success": True, "message": msg})

    def handle_typing(self):
        data = self._read_json()
        user_id = data.get("user_id")
        room_code = (data.get("room_code") or "").strip().upper()
        is_typing = bool(data.get("is_typing", False))
        now = time.time()

        if user_id and room_code:
            room = get_or_create_room(room_code)
            with room.cond:
                if user_id in room.participants:
                    room.participants[user_id]["is_typing"] = is_typing
                    room.participants[user_id]["last_seen"] = now
                    room.cond.notify_all()

        self._send_json({"success": True})

    def handle_leave(self):
        data = self._read_json()
        user_id = data.get("user_id")
        room_code = (data.get("room_code") or "").strip().upper()
        now = time.time()

        if user_id and room_code:
            room = get_or_create_room(room_code)
            with room.cond:
                if user_id in room.participants:
                    user = room.participants.pop(user_id)
                    room.counter += 1
                    room.messages.append({
                        "id": room.counter,
                        "type": "system",
                        "text": f"🚪 {user['name']} left room '{room_code}'",
                        "timestamp": now,
                        "time_str": time.strftime("%I:%M %p")
                    })
                    # End any call
                    room.signal_counter += 1
                    room.signals.append({
                        "id": room.signal_counter,
                        "sender_id": user_id,
                        "type": "call-end",
                        "payload": {},
                        "timestamp": now
                    })
                    room.cond.notify_all()

        self._send_json({"success": True})

    def handle_clear(self):
        data = self._read_json()
        room_code = (data.get("room_code") or "").strip().upper()
        if room_code:
            room = get_or_create_room(room_code)
            with room.cond:
                room.messages.clear()
                room.counter += 1
                room.messages.append({
                    "id": room.counter,
                    "type": "system",
                    "text": f"🧹 Chat history for room '{room_code}' cleared",
                    "timestamp": time.time(),
                    "time_str": time.strftime("%I:%M %p")
                })
                room.cond.notify_all()
        self._send_json({"success": True})

    def handle_history(self, query):
        room_code = query.get("room", [""])[0].strip().upper()
        if room_code:
            room = get_or_create_room(room_code)
            with room.lock:
                self._send_json({"messages": room.messages, "room_code": room_code})
        else:
            self._send_json({"messages": []})

def start_cloudflared_tunnel():
    global PUBLIC_URL
    cf_path = os.path.join(BASE_DIR, "cloudflared.exe")
    if not os.path.exists(cf_path):
        return

    try:
        proc = subprocess.Popen(
            [cf_path, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        for line in proc.stdout:
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                PUBLIC_URL = match.group(0)
                print("\n" + "=" * 62)
                print("  🌍 INTERNET PUBLIC LINK READY (FOR 2ND PERSON FAR AWAY)")
                print("=" * 62)
                print(f"  Share this link: {PUBLIC_URL}")
                print("=" * 62 + "\n")
                break
    except Exception as e:
        print("Notice: Cloudflare tunnel could not start (will run in LAN/offline mode):", e)

def run():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    tunnel_thread = threading.Thread(target=start_cloudflared_tunnel, daemon=True)
    tunnel_thread.start()

    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, ChatRequestHandler)
    print("=" * 62)
    print("  [*] PRIVATE 2-PERSON CHAT WITH AUDIO & VIDEO CALL")
    print("=" * 62)
    print(f"  --> Local:                       http://localhost:{PORT}")
    print(f"  --> Same Wi-Fi / Hotspot:        http://{LOCAL_IP}:{PORT}")
    print("  --> Starting Internet Tunnel for distant 2nd person...")
    print("=" * 62)
    print("  * Features: Room Codes, Chat, WebRTC Audio Call & Video Call.")
    print("  * Press Ctrl+C to stop the server.")
    print("=" * 62 + "\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

if __name__ == "__main__":
    run()
