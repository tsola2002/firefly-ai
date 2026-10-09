
import websocket
import ssl

URL = (
    "wss://chat-po.site/cabinet-client/socket.io/"
    "?EIO=4&transport=websocket"
)

def on_open(ws):
    print("[+] WebSocket connected")
    print("[+] Handshake response headers:")
    print(ws.handshake_response.headers)

    # Socket.IO Engine.IO v4 opening packet
    ws.send("40")

def on_message(ws, message):
    print("[MESSAGE]", repr(message))

def on_error(ws, error):
    print("[ERROR]", error)

def on_close(ws, code, reason):
    print("[CLOSED]", code, reason)

ws = websocket.WebSocketApp(
    URL,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close,
)

ws.run_forever(
    sslopt={"cert_reqs": ssl.CERT_REQUIRED},
    ping_interval=20,
    ping_timeout=10,
)