import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
from dashscope.audio.tts_v2.speech_synthesizer import websocket as ws_module
import threading, time, ssl

import os
dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")

srt_path = "/Users/wangyw/Downloads/ttsmaker-file-2026-7-20-16-50-55.srt"
output_path = "/Users/wangyw/Downloads/ttsmaker-file-2026-7-20-16-50-55_tts_yingmu.mp3"

with open(srt_path, "r", encoding="utf-8") as f:
    content = f.read()

lines = content.strip().split("\n")
texts = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    if line.isdigit():
        continue
    if "-->" in line:
        continue
    texts.append(line)

full_text = "".join(texts)
print(f"Text length: {len(full_text)} chars")
print(f"Model: cosyvoice-v3-flash, Voice: longyingmu_v3 (龙应沐)")
print("Synthesizing...")

synthesizer = SpeechSynthesizer(
    model="cosyvoice-v3-flash",
    voice="longyingmu_v3",
    format=AudioFormat.MP3_22050HZ_MONO_256KBPS,
)

def patched_connect(timeout_seconds=30):
    synthesizer.ws = ws_module.WebSocketApp(
        synthesizer.url,
        header=synthesizer.request.get_websocket_headers(headers=synthesizer.headers, workspace=synthesizer.workspace),
        on_message=synthesizer.on_message, on_error=synthesizer.on_error, on_close=synthesizer.on_close,
    )
    synthesizer.thread = threading.Thread(target=synthesizer.ws.run_forever, kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}})
    synthesizer.thread.daemon = True
    synthesizer.thread.start()
    start = time.time()
    while not (synthesizer.ws.sock and synthesizer.ws.sock.connected) and (time.time() - start) < timeout_seconds:
        time.sleep(0.1)
    if not (synthesizer.ws.sock and synthesizer.ws.sock.connected):
        raise TimeoutError("timeout")

synthesizer._SpeechSynthesizer__connect = patched_connect

audio = synthesizer.call(full_text)

if audio:
    with open(output_path, "wb") as f:
        f.write(audio)
    print(f"Audio saved to: {output_path}")
    print(f"Size: {len(audio)} bytes ({len(audio)/1024:.1f} KB)")
else:
    print("Synthesis failed!")
    resp = synthesizer.get_response()
    print(f"Response: {resp}")
