import os
import json
import time
import queue
import base64
import threading
from flask import Flask, Response, request, jsonify, render_template_string
from playwright.sync_api import sync_playwright

app = Flask(__name__)

DOWNLOAD_DIR = r"C:\YT_Downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Bộ nhớ đệm dùng chung an toàn giữa luồng Trình duyệt và luồng Flask
state = {
    "screenshot": None,
    "latest_video": None,
    "status_text": "Đang khởi động trình duyệt trên VPS...",
    "action_queue": queue.Queue()
}

REMOTE_UI_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: #0f172a; color: white; margin: 0; overflow: hidden; font-family: sans-serif; }
    </style>
</head>
<body class="flex flex-col h-screen">
    <div class="bg-slate-900 border-b border-slate-700 p-2 flex flex-wrap items-center justify-between gap-2 text-xs">
        <div class="flex items-center gap-2">
            <button onclick="switchTab(0)" class="px-3 py-1.5 bg-purple-600 hover:bg-purple-500 rounded font-semibold">
                🌐 Tab 1: Web Tool Của Bạn
            </button>
            <button onclick="switchTab(1)" class="px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded font-semibold">
                ▶️ Tab 2: YouTube Studio
            </button>
            <button onclick="pushVideoToYT()" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 rounded font-semibold">
                ⚡ Đẩy Video Vừa Tải Sang YouTube
            </button>
        </div>

        <div class="flex items-center gap-1.5 flex-1 max-w-xl">
            <input type="text" id="vn_text" placeholder="Gõ Tiếng Việt (Tiêu đề, Mô tả, nội dung...) rồi bấm Gửi Chữ" 
                   class="flex-1 px-2.5 py-1.5 rounded bg-slate-800 border border-slate-600 text-white text-xs">
            <button onclick="sendText()" class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 rounded font-semibold">Gửi Chữ</button>
            <button onclick="sendKey('Enter')" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">Enter</button>
            <button onclick="sendKey('Backspace')" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">Xóa</button>
            <button onclick="scrollPage(-400)" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">▲ Lên</button>
            <button onclick="scrollPage(400)" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">▼ Xuống</button>
        </div>

        <div id="file-status" class="text-emerald-400 font-mono text-[11px]">
            Đang kết nối hình ảnh...
        </div>
    </div>

    <div class="flex-1 flex items-center justify-center bg-black overflow-auto">
        <img id="screen" src="/screenshot" class="cursor-crosshair select-none max-w-full max-h-full" 
             onclick="handleClick(event)" draggable="false" />
    </div>

    <script>
        const img = document.getElementById('screen');

        setInterval(() => {
            img.src = '/screenshot?t=' + Date.now();
        }, 400);

        setInterval(async () => {
            const res = await fetch('/status');
            const data = await res.json();
            const label = data.latest_video ? ("✅ Đã tải: " + data.latest_video) : data.status_text;
            document.getElementById('file-status').innerText = label;
        }, 2000);

        async function handleClick(e) {
            const rect = img.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            const x = Math.round((e.clientX - rect.left) * (1280 / rect.width));
            const y = Math.round((e.clientY - rect.top) * (720 / rect.height));
            await fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ type: 'click', x, y })
            });
        }

        async function sendText() {
            const text = document.getElementById('vn_text').value;
            if (!text) return;
            await fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ type: 'fill_text', text })
            });
            document.getElementById('vn_text').value = '';
        }

        async function sendKey(key) {
            await fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ type: 'press', key })
            });
        }

        async function scrollPage(deltaY) {
            await fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ type: 'scroll', deltaY })
            });
        }

        async function switchTab(index) {
            await fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ type: 'switch_tab', index })
            });
        }

        async function pushVideoToYT() {
            const res = await fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ type: 'upload_to_yt' })
            });
            const data = await res.json();
            alert(data.message);
        }
    </script>
</body>
</html>
"""

def handle_download(download):
    save_path = os.path.join(DOWNLOAD_DIR, download.suggested_filename)
    print(f"[*] Đang tải file về VPS: {save_path}", flush=True)
    download.save_as(save_path)
    state["latest_video"] = save_path
    state["status_text"] = f"Đã tải xong: {download.suggested_filename}"
    print(f"[+] Hoàn tất tải về: {save_path}", flush=True)

def safe_add_cookies(context, cookies_raw):
    try:
        cookies = json.loads(cookies_raw)
        if not isinstance(cookies, list):
            return
        added = 0
        for c in cookies:
            try:
                item = {
                    "name": str(c["name"]),
                    "value": str(c["value"]),
                    "domain": c.get("domain", ".youtube.com"),
                    "path": c.get("path", "/"),
                    "secure": bool(c.get("secure", True)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                }
                ss = str(c.get("sameSite", "")).lower()
                item["sameSite"] = ss.capitalize() if ss in ["strict", "lax", "none"] else "Lax"
                context.add_cookies([item])
                added += 1
            except Exception:
                continue
        print(f"[+] Đã nạp thành công {added} cookies.", flush=True)
    except Exception as e:
        print(f"[!] Bỏ qua lỗi định dạng Cookie: {e}", flush=True)

def browser_worker():
    tool_url = os.environ.get("TOOL_URL", "https://www.google.com").strip() or "https://www.google.com"
    cookies_b64 = os.environ.get("COOKIES_B64", "")
    cookies_raw = ""
    if cookies_b64:
        try:
            cookies_raw = base64.b64decode(cookies_b64.encode("utf-8")).decode("utf-8")
        except Exception as e:
            print("Lỗi giải mã cookie:", e, flush=True)

    with sync_playwright() as p:
        # Chạy trình duyệt với cấu hình chống phát hiện bot và tương thích màn hình ảo Windows Server
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,720",
                "--no-sandbox"
            ]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        pages = []

        def on_new_page(new_p):
            new_p.set_viewport_size({"width": 1280, "height": 720})
            new_p.on("download", handle_download)
            if new_p not in pages:
                pages.append(new_p)

        context.on("page", on_new_page)

        if cookies_raw:
            safe_add_cookies(context, cookies_raw)

        # Tab 1: Mở trang Web Tool
        page1 = context.new_page()
        state["status_text"] = "Đang mở Tab 1 (Web Tool)..."
        try:
            page1.goto(tool_url, wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            print(f"[!] Tab 1 tải lâu nhưng vẫn tiếp tục: {e}", flush=True)

        # Tab 2: Mở YouTube Studio
        page2 = context.new_page()
        state["status_text"] = "Đang mở Tab 2 (YouTube Studio)..."
        try:
            page2.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            print(f"[!] Tab 2 tải lâu nhưng vẫn tiếp tục: {e}", flush=True)

        active_page = page1
        state["status_text"] = "🟢 Sẵn sàng thao tác (Chưa có file tải về)"

        # Vòng lặp chính: vừa xử lý thao tác chuột/phím, vừa cập nhật ảnh màn hình trên cùng 1 luồng
        while True:
            while not state["action_queue"].empty():
                act = state["action_queue"].get_nowait()
                act_type = act.get("type")
                try:
                    if act_type == "click":
                        active_page.mouse.click(act["x"], act["y"])
                    elif act_type == "fill_text":
                        active_page.keyboard.insert_text(act["text"])
                    elif act_type == "press":
                        active_page.keyboard.press(act["key"])
                    elif act_type == "scroll":
                        active_page.mouse.wheel(0, act["deltaY"])
                    elif act_type == "switch_tab":
                        idx = act.get("index", 0)
                        if 0 <= idx < len(pages):
                            active_page = pages[idx]
                    elif act_type == "upload_to_yt":
                        if state["latest_video"] and os.path.exists(state["latest_video"]):
                            active_page = pages[1]
                            active_page.goto("https://www.youtube.com/upload", wait_until="domcontentloaded", timeout=30000)
                            time.sleep(3)
                            active_page.locator('input[type="file"]').first.set_input_files(state["latest_video"])
                            state["status_text"] = "✅ Đã nạp video vào YouTube Studio!"
                except Exception as e:
                    print(f"[!] Lỗi thực thi thao tác {act_type}: {e}", flush=True)

            try:
                state["screenshot"] = active_page.screenshot(type="jpeg", quality=70)
            except Exception as e:
                print(f"[!] Lỗi chụp màn hình: {e}", flush=True)

            time.sleep(0.25)

@app.route("/")
def index():
    return render_template_string(REMOTE_UI_HTML)

@app.route("/status")
def get_status():
    name = os.path.basename(state["latest_video"]) if state["latest_video"] else None
    return jsonify({
        "latest_video": name,
        "status_text": state["status_text"]
    })

@app.route("/screenshot")
def screenshot():
    img = state.get("screenshot")
    if not img:
        return Response(status=204)
    return Response(img, mimetype="image/jpeg")

@app.route("/action", methods=["POST"])
def action():
    data = request.json
    if data.get("type") == "upload_to_yt":
        if not state["latest_video"] or not os.path.exists(state["latest_video"]):
            return jsonify({"message": "Chưa có video nào được tải về trên VPS!"})
        state["action_queue"].put(data)
        return jsonify({"message": "Đang chuyển sang Tab YouTube và nạp video..."})

    state["action_queue"].put(data)
    return jsonify({"ok": True})

if __name__ == "__main__":
    t = threading.Thread(target=browser_worker, daemon=True)
    t.start()
    app.run(host="127.0.0.1", port=8080, threaded=True)
