# LINE bot 輕量版：一支 Python 程式，接上你自己電腦裡的小模型，回答問題。沒有顯示卡也跑得動。
#
# 跟課堂版 line_bot_local.py 差在哪：
#   只做一件事：文字問題進來 → 你電腦上的 Ollama 小模型回答 → 回覆。
#   拿掉照片、日曆、雲端硬碟、試算表（那些要 gws，是課堂版的事）。
#   預設模型 gemma4:e2b（小、CPU 跑得動）；想更小就 MODEL=gemma3:1b。
#   多一個「模擬模式」：不接 LINE、不開通道，在終端機假裝手機傳訊息進來，看 bot 回什麼。
#   作業先用模擬模式做到會回答；要接真的 LINE 再照指南第 4 段。
#
# 用法（在這個檔所在的資料夾；Windows 用 PowerShell、Mac 用「終端機」）：
#   python line_bot_lite.py --simulate      ← 模擬模式：終端機打字，bot 回答；不需要 LINE 任何東西
#   python line_bot_lite.py                 ← 真的接 LINE：要 cloudflared、LINE 長效通行碼、Channel secret
#   換模型：Windows PowerShell 先打  $env:MODEL="gemma3:1b"   ／ Mac 終端機先打  export MODEL=gemma3:1b
#
# 需要：Python 3（只用內建功能，不用 pip install）、Ollama 開著、模型拉好（ollama pull gemma4:e2b）。
# 選做：要它會上網查，開程式時加 --search（搜尋走 Ollama 的線上服務，要網路；額度規則可能變）。

import base64, getpass, hashlib, hmac, json, os, queue, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("MODEL", "gemma4:e2b")
PORT = int(os.environ.get("PORT", "8000"))
OLLAMA = "http://127.0.0.1:11434"
SEARCH = "--search" in sys.argv          # 要不要讓模型上網查（預設不要：純本機、不用網路）
SIMULATE = "--simulate" in sys.argv      # 模擬模式：不接 LINE
GREETING = re.compile(r"^(hi|hello|你好|嗨|哈囉|測試|test)$", re.I)
HELP = ("你好！我是跑在同學電腦上的小模型 🤖\n問我問題，我用自己的記憶回答" + ("，需要的話上網查、附來源" if SEARCH else "（沒有上網，答案請自己核對）") + "。")
SYSTEM = ("你是 LINE 上的助理，今天是 {today}。回答用台灣的繁體中文、純文字（不要用星號或井字號），120 字以內，先講結論。"
          + ("遇到最近的事、版本號、價格或任何你不確定的事實，先呼叫 web_search。不用自己寫網址，程式會附上來源。" if SEARCH
             else "你沒有上網；不確定的事就說不確定，不要編造。"))
TOOLS = [{"type": "function", "function": {"name": "web_search", "description": "搜尋網路，回傳前幾筆標題、網址與內文。",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]

LINE_TOKEN = LINE_SECRET = ""   # main() 開起來時問你，再填進這裡；不寫進檔案
_turn = threading.Lock()        # 一次只答一題：CPU 一次跑一題才不會更慢
_procs = []                     # 開出去的 cloudflared，結束時一定要關
TUNNEL_WAIT = 60


def log(s):
    print(f"[{datetime.now():%H:%M:%S}] {s}", flush=True)


# ===== 1. 叫 Ollama、叫 LINE 的小工具 =====
def http_json(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:200]}
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}


def ollama(path, body, timeout=600):
    code, res = http_json("POST", OLLAMA + path, body, timeout=timeout)
    if code == 0:
        raise RuntimeError("連不到 Ollama：先把 Ollama 打開（Windows 工作列有小羊駝；或另開視窗打 ollama serve）")
    if code != 200:
        raise RuntimeError(f"Ollama {path} 回應 {code}：{res}")
    return res


def chat(messages, **extra):
    # CPU 跑小模型，一題可能要等幾十秒到幾分鐘，所以 timeout 給 10 分鐘；num_ctx 開小一點省記憶體
    body = {"model": MODEL, "messages": messages, "stream": False, "think": False, "keep_alive": "30m", "options": {"num_ctx": 4096}}
    return ollama("/api/chat", {**body, **extra}).get("message", {})


def line(method, path, body=None):
    return http_json(method, "https://api.line.me" + path, body, {"Authorization": "Bearer " + LINE_TOKEN})


def reply(token, text):
    if SIMULATE:
        print("\n🤖 bot 回覆：\n" + text + "\n", flush=True)
        return
    code, res = line("POST", "/v2/bot/message/reply", {"replyToken": token, "messages": [{"type": "text", "text": text[:5000]}]})
    if code != 200:
        log(f"⚠️ LINE 回覆失敗 {code}：{res}")


# ===== 2. 模型回答問題 =====
def ask_text(question):
    """回傳（答案, 來源網址清單）。沒開 --search 就純本機回答。"""
    msgs = [{"role": "system", "content": SYSTEM.format(today=datetime.now().strftime("%Y-%m-%d（%A）"))},
            {"role": "user", "content": question}]
    sources = []
    for _ in range(3 if SEARCH else 1):
        m = chat(msgs, **({"tools": TOOLS} if SEARCH else {}))
        msgs.append(m)
        if not m.get("tool_calls"):
            return m.get("content", "").replace("**", "").strip(), sources
        for c in m["tool_calls"]:
            q = (c["function"].get("arguments") or {}).get("query", "")
            log(f"  🔍 模型決定搜：{q}")
            try:
                rows = ollama("/api/experimental/web_search", {"query": q, "max_results": 3}, timeout=60).get("results", [])
                sources += [r["url"] for r in rows]
                out = json.dumps([{"title": r["title"], "url": r["url"], "content": r["content"][:1200]} for r in rows], ensure_ascii=False)
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e):
                    log("  ⚠️ 搜尋要先登入 Ollama（免費帳號）：另開視窗打 ollama signin，照它給的網址登入，再跑一次")
                    out = "ERROR: 搜尋功能沒登入，無法使用。請直接用你的記憶回答，並說明你沒有上網。"
                else:
                    out = f"ERROR: {e}"
            msgs.append({"role": "tool", "tool_name": "web_search", "content": out})
    return "查了幾次還整理不出答案，換個問法再試一次。", sources


# ===== 3. 處理一則訊息 =====
def handle(event):
    with _turn:
        _handle(event)


def _handle(event):
    if event.get("type") != "message" or not event.get("replyToken"):
        return
    msg, token = event["message"], event["replyToken"]
    if msg.get("type") != "text":
        return reply(token, "目前我只看得懂文字喔。")
    text = msg["text"].strip()
    if GREETING.match(text):
        return reply(token, HELP)
    user = event.get("source", {})
    if not SIMULATE and user.get("type") == "user":  # 「對方正在輸入…」動畫，最多 60 秒（CPU 慢，先讓對方知道有在跑）
        line("POST", "/v2/bot/chat/loading/start", {"chatId": user["userId"], "loadingSeconds": 60})
    log(f"📩 收到文字（{len(text)} 字），交給 {MODEL}…")  # 不印內容：螢幕可能正在投影
    t0 = time.time()
    try:
        answer, sources = ask_text(text)
        tail = ("\n\n來源：\n" + "\n".join("• " + u for u in list(dict.fromkeys(sources))[:3])) if sources else \
               ("\n\nℹ️ 這題沒有上網查，只憑模型記憶，請自己核對。")
        reply(token, f"{answer}{tail}\n（本地模型 {MODEL}，{time.time() - t0:.0f} 秒）")
        log(f"✅ 回覆了（{time.time() - t0:.0f} 秒）")
    except Exception as e:
        log(f"❌ {e}")
        reply(token, "⚠️ 出錯了：" + str(e)[:300])


class Webhook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if LINE_SECRET:  # 對簽章：確定真的是 LINE 送來的，不是知道網址的路人
            sig = base64.b64encode(hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()).decode()
            if not hmac.compare_digest(sig, self.headers.get("x-line-signature", "")):
                log("⛔ 簽章不對，不是 LINE 送來的，丟掉")
                self.send_response(401)
                self.end_headers()
                return
        self.send_response(200)  # 先跟 LINE 說收到了（LINE 要 2 秒內回應），再慢慢處理
        self.end_headers()
        for ev in json.loads(body or b"{}").get("events", []):
            threading.Thread(target=handle, args=(ev,), daemon=True).start()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write("LINE bot 輕量版運作中".encode())

    def log_message(self, *args):
        pass


# ===== 4. 模擬模式：終端機假裝是手機 =====
def simulate():
    log(f"模擬模式：不接 LINE。模型 {MODEL}" + ("，開了上網查" if SEARCH else "，純本機（沒上網）"))
    log("先載入模型（第一次要等一下）…")
    t0 = time.time()
    chat([{"role": "user", "content": "hi"}])
    log(f"✅ 模型可以用了（{time.time() - t0:.0f} 秒）。打一句話按 Enter，bot 會回；打 quit 結束。")
    n = 0
    while True:
        try:
            text = input("📱 你：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit", "q"):
            break
        if not text:
            continue
        n += 1
        # 做一個長得跟 LINE 送來一模一樣的事件，走同一條處理路：之後接真 LINE 時程式不用改
        handle({"type": "message", "replyToken": f"sim-{n}", "source": {"type": "user", "userId": "SIMULATED"},
                "message": {"type": "text", "id": str(n), "text": text}})
    log("模擬結束。")


# ===== 5. 真的接 LINE：開通道、換 Webhook，結束時換回去（跟課堂版一樣） =====
def ask_settings():
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or getpass.getpass("貼上 LINE 長效通行碼（Channel access token，畫面不顯示）：").strip()
    secret = os.environ.get("LINE_CHANNEL_SECRET") or getpass.getpass("貼上 Channel secret（LINE Developers → Basic settings；必填；畫面不顯示）：").strip()
    return token, secret


def open_tunnel():
    if not shutil.which("cloudflared"):
        log("⚠️ 找不到 cloudflared，外面連不到這裡。Windows：winget install --id Cloudflare.cloudflared -e；Mac：brew install cloudflared")
        log("   裝完關掉這個視窗、新開一個再跑一次。")
        return None
    cf = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    _procs.append(cf)
    found = queue.Queue()

    def watch():
        for row in cf.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", row)
            if m:
                found.put(m.group(0))
        found.put(None)

    threading.Thread(target=watch, daemon=True).start()
    try:
        url = found.get(timeout=TUNNEL_WAIT)
    except queue.Empty:
        url = None
    if not url:
        log(f"⚠️ {TUNNEL_WAIT} 秒內通道沒拿到網址：看網路（學校網路可能擋），或再跑一次")
    return url


def restore(old):
    if not old:
        log("ℹ️ 原本沒有 Webhook 網址，現在指著一個已經關掉的通道；要用 Apps Script 版就回 LINE Developers 貼回 /exec 網址")
        return
    line("PUT", "/v2/bot/channel/webhook/endpoint", {"endpoint": old})
    code, now = line("GET", "/v2/bot/channel/webhook/endpoint")
    if code == 200 and now.get("endpoint") == old:
        log("↩️ LINE 的 Webhook 換回原本的了（重新讀過一次，確認是原本那個）")
    else:
        log(f"⚠️ 沒能確認換回去：到 LINE Developers →「Messaging API」→ Webhook URL 手動貼回：{old}")


def main():
    global LINE_TOKEN, LINE_SECRET
    if SIMULATE:
        return simulate()
    LINE_TOKEN, LINE_SECRET = ask_settings()
    code, info = line("GET", "/v2/bot/info")
    if code != 200:
        sys.exit(f"LINE 通行碼不對（{code}）：回 LINE Developers 重新複製長效通行碼")
    log(f"✅ LINE bot「{info.get('displayName')}」")
    if not LINE_SECRET:
        sys.exit("沒有 Channel secret 就不開通道：沒有它，知道通道網址的人都能假冒 LINE 送訊息進來。回 LINE Developers →「Basic settings」複製，再跑一次。")
    code, cur = line("GET", "/v2/bot/channel/webhook/endpoint")
    if code == 200:
        old = cur.get("endpoint")
    elif code == 404:
        old = None
    else:
        sys.exit(f"讀不到 LINE 現在的 Webhook 網址（{code}）：先不換。等一下再跑一次。{cur}")
    log(f"   原本的 Webhook：{old or '（沒有設定）'}——先抄下來，萬一沒換回去要手動貼回")
    log(f"載入模型 {MODEL}（第一次要等一下）…")
    chat([{"role": "user", "content": "hi"}])
    log("✅ 模型可以用了")

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Webhook)  # 只聽本機：外面只能從通道進來
    threading.Thread(target=server.serve_forever, daemon=True).start()
    switched = False
    try:
        url = open_tunnel()
        if url:
            code, res = line("PUT", "/v2/bot/channel/webhook/endpoint", {"endpoint": url + "/callback"})
            if code != 200:
                log(f"⚠️ 換 Webhook 失敗 {code}：{res}。可以手動貼到 LINE Developers：{url}/callback")
            else:
                switched = True
                log(f"✅ LINE 的 Webhook 換到這台電腦了：{url}/callback（按 Ctrl+C 會換回去）")
                log("   LINE 說換網址最多要 1 分鐘才生效；等一下再用手機傳訊息")
                time.sleep(5)
                code, test = line("POST", "/v2/bot/channel/webhook/test", {})
                log(f"   LINE 測試連線：{'✅ 通了' if test.get('success') else '還沒通（' + str(test.get('reason')) + '），等一分鐘再傳'}")
        log("開好了。手機傳訊息給 bot 試試看；這個視窗不要關，按 Ctrl+C 結束。" if switched
            else "⚠️ 這次沒接上 LINE（看上面的訊息）。按 Ctrl+C 結束。")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if switched:
            restore(old)
        for cf in _procs:
            cf.terminate()
        server.shutdown()


if __name__ == "__main__":
    main()
