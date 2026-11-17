import dashscope
import urllib.request
import ssl

import os
dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")

srt_path = "/Users/wangyw/Downloads/ttsmaker-file-2026-7-20-16-50-55.srt"
output_path = "/Users/wangyw/Downloads/ttsmaker-file-2026-7-20-16-50-55_tts_kai.mp3"

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
print(f"Model: qwen3-tts-flash, Voice: Kai (凯)")
print("Synthesizing...")

response = dashscope.MultiModalConversation.call(
    model="qwen3-tts-flash",
    text=full_text,
    voice="Kai",
    stream=False,
)

print(f"Status: {response.status_code}")

if response.status_code == 200:
    audio_url = response.output.audio.url
    print(f"Audio URL: {audio_url[:100]}...")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(audio_url)
    with urllib.request.urlopen(req, context=ctx) as resp:
        audio_data = resp.read()

    with open(output_path, "wb") as f:
        f.write(audio_data)
    print(f"Audio saved to: {output_path}")
    print(f"Size: {len(audio_data)} bytes ({len(audio_data)/1024:.1f} KB)")
    if hasattr(response, 'usage'):
        print(f"Usage: {response.usage}")
else:
    print(f"Error: {response}")
    print(f"Code: {response.code}")
    print(f"Message: {response.message}")
