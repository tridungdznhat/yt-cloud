import os
import io
import json
import time
import base64
import threading
import requests
from flask import Flask, Response, request, jsonify, render_template_string
from playwright.sync_api import sync_playwright

app = Flask(__name__)

DOWNLOAD_DIR = r"C:\YT_Downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Biến toàn cục quản lý trạng thái trình duyệt trên VPS
state = {
    "context": None,
    "active_page": None,
    "pages": [],
    "latest_video": None,
    "lock": threading.Lock()
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
    <!-- Thanh công cụ điều khiển trình duyệt trên VPS -->
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

        <!-- Thanh gõ Tiếng Việt & Phím tắt gửi thẳng vào trình duyệt VPS -->
        <div class="flex items-center gap-1.5 flex-1 max-w-xl">
            <input type="text" id="vn_text" placeholder="Gõ Tiếng Việt (Tiêu đề, Mô tả, nội dung...) rồi bấm Gửi Chữ" 
                   class="flex-1 px-2.5 py-1.5 rounded bg-slate-800 border border-slate-600 text-white text-xs">
            <button onclick="sendText()" class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 rounded font-semibold">
                Gửi Chữ
            </button>
            <button onclick="sendKey('Enter')" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">Enter</button>
            <button onclick="sendKey('Backspace')" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">Xóa</button>
            <button onclick="scrollPage(-400)" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">▲ Lên</button>
            <button onclick="scrollPage(400)" class="px-2.5 py-1.5 bg-slate-700 hover:bg-slate-600 rounded">▼ Xuống</button>
        </div>

        <div id="file-status" class="text-emerald-400 font-mono text-[11px]">
            Chưa có file tải về
        </div>
    </div>

    <!-- Màn hình trực tiếp của Trình duyệt VPS -->
    <div class="flex-1 flex items-center justify-center bg-black overflow-auto">
        <img id="screen" src="/screenshot" class="cursor-crosshair select-none max-w-full max-h-full" 
             onclick="handleClick(event)" draggable="false" />
    </div>

    <script>
        const img = document.getElementById('screen');

        // Cập nhật hình ảnh trình duyệt liên tục
        setInterval(() => {
            img.src = '/screenshot?t=' + Date.now();
        }, 450);

        // Kiểm tra file đã tải xong trên VPS chưa
        setInterval(async () => {
            const res = await fetch('/status');
            const data = await res.json();
            if (data.latest_video) {
                document.getElementById('file-status').innerText = "✅ Đã tải: " + data.latest_video;
            }
        }, 2500);

        async function handleClick(e) {
            const rect = img.getBoundingClientRect();
            const scaleX = 1280 / rect.width;
            const scaleY = 720 / rect.height;
            const x = Math.round((e.clientX - rect.left) * scaleX);
            const y = Math.round((e.clientY - rect.top) * scaleY);
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

def normalize_cookies(raw_cookies):
    cookies = json.loads(raw_cookies)
    valid = []
    for c in cookies:
        item = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".youtube.com"),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        ss = str(c.get("sameSite", "")).lower()
        item["sameSite"] = ss.capitalize() if ss in ["strict", "lax", "none"] else "Lax"
        valid.append(item)
    return valid

def handle_download(download):
    """Tự động bắt mọi file người dùng tải về từ Web Tool và lưu vào ổ cứng VPS."""
    save_path = os.path.join(DOWNLOAD_DIR, download.suggested_filename)
    print(f"[*] Đang tải file từ Web Tool về VPS: {save_path}")
    download.save_as(save_path)
    state["latest_video"] = save_path
    print(f"[+] Đã tải xong về VPS: {save_path}")

def attach_page_listeners(page):
    page.set_viewport_size({"width": 1280, "height": 720})
    page.on("download", handle_download)
    state["pages"].append(page)
    state["active_page"] = page

@app.route("/")
def index():
    return render_template_string(REMOTE_UI_HTML)

@app.route("/status")
def get_status():
    name = os.path.basename(state["latest_video"]) if state["latest_video"] else None
    return jsonify({"latest_video": name})

@app.route("/screenshot")
def screenshot():
    with state["lock"]:
        page = state.get("active_page")
        if not page:
            return Response(status=204)
        try:
            img_bytes = page.screenshot(type="jpeg", quality=70)
            return Response(img_bytes, mimetype="image/jpeg")
        except Exception:
            return Response(status=204)

@app.route("/action", methods=["POST"])
def action():
    data = request.json
    act_type = data.get("type")

    with state["lock"]:
        page = state.get("active_page")
        if not page:
            return jsonify({"ok": False})

        try:
            if act_type == "click":
                page.mouse.click(data["x"], data["y"])
            elif act_type == "fill_text":
                page.keyboard.insert_text(data["text"])
            elif act_type == "press":
                page.keyboard.press(data["key"])
            elif act_type == "scroll":
                page.mouse.wheel(0, data["deltaY"])
            elif act_type == "switch_tab":
                idx = data.get("index", 0)
                if 0 <= idx < len(state["pages"]):
                    state["active_page"] = state["pages"][idx]
                    state["active_page"].bring_to_front()
            elif act_type == "upload_to_yt":
                if not state["latest_video"] or not os.path.exists(state["latest_video"]):
                    return jsonify({"message": "Chưa có video nào được tải về từ Web Tool!"})
                # Chuyển sang Tab 2 (YouTube) và mở khung Upload
                yt_page = state["pages"][1]
                state["active_page"] = yt_page
                yt_page.bring_to_front()
                yt_page.goto("https://www.youtube.com/upload", wait_until="domcontentloaded")
                time.sleep(4)
                yt_page.locator('input[type="file"]').set_input_files(state["latest_video"])
                return jsonify({"message": "Đã đẩy video vào YouTube Studio! Bạn có thể chỉnh sửa Tiêu đề, Mô tả, Thumbnail ngay trên màn hình."})
        except Exception as e:
            print("Lỗi thao tác:", e)

    return jsonify({"ok": True})

def browser_worker():
    tool_url = os.environ.get("TOOL_URL", "https://www.google.com")
    cookies_b64 = os.environ.get("COOKIES_B64", "")
    cookies_raw = base64.b64decode(cookies_b64.encode("utf-8")).decode("utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            accept_downloads=True
        )
        state["context"] = context
        context.on("page", attach_page_listeners)

        if cookies_raw:
            context.add_cookies(normalize_cookies(cookies_raw))

        # Tab 1: Mở trang Web Tool của bạn
        page1 = context.new_page()
        attach_page_listeners(page1)
        page1.goto(tool_url, wait_until="domcontentloaded", timeout=60000)

        # Tab 2: Mở sẵn YouTube Studio với Cookie đã nạp
        page2 = context.new_page()
        attach_page_listeners(page2)
        page2.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=60000)

        # Quay lại Tab 1 để bạn bắt đầu làm video
        state["active_page"] = page1
        page1.bring_to_front()

        # Giữ trình duyệt chạy suốt phiên làm việc (tối đa 5 tiếng)
        while True:
            time.sleep(1)

if __name__ == "__main__":
    t = threading.Thread(target=browser_worker, daemon=True)
    t.start()
    app.run(host="127.0.0.1", port=8080, threaded=False)