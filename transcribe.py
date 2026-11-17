import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
import json

import os
dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")

def format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

class MyCallback(RecognitionCallback):
    def __init__(self):
        self.sentences = []
        self.full_text = ""

    def on_event(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        if sentence and isinstance(sentence, dict):
            if sentence.get("end_time", 0) > sentence.get("begin_time", 0):
                self.sentences.append(sentence)
                text = sentence.get("text", "")
                print(f"  [{format_time(sentence['begin_time']/1000)} --> {format_time(sentence['end_time']/1000)}] {text}")

    def on_complete(self) -> None:
        print("Transcription complete.")

    def on_error(self, result: RecognitionResult) -> None:
        print(f"Error: {result}")

audio_path = "/Users/wangyw/Downloads/ttsmaker-file-2026-7-20-16-50-55.mp3"

print("Starting transcription...")
callback = MyCallback()
recognition = Recognition(
    model="paraformer-realtime-v2",
    format="mp3",
    sample_rate=16000,
    language_hints=["zh"],
    callback=callback,
)

result = recognition.call(file=audio_path)
print(f"\nCall status: {result.status_code}")

if result.status_code != 200:
    print(f"Error: {result}")
    print(f"Message: {getattr(result, 'message', 'N/A')}")
else:
    sentences = callback.sentences
    if not sentences:
        all_sentences = result.get_sentence()
        if all_sentences:
            if isinstance(all_sentences, list):
                sentences = all_sentences
            elif isinstance(all_sentences, dict):
                sentences = [all_sentences]

    print(f"Total sentences: {len(sentences)}")

    if sentences:
        srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, sentence in enumerate(sentences, 1):
                begin_time = sentence.get("begin_time", 0) / 1000.0
                end_time = sentence.get("end_time", 0) / 1000.0
                text = sentence.get("text", "").strip()
                if not text:
                    continue
                f.write(f"{i}\n")
                f.write(f"{format_time(begin_time)} --> {format_time(end_time)}\n")
                f.write(f"{text}\n\n")
        print(f"SRT saved to: {srt_path}")
    else:
        print(f"Full result output: {result.output}")
        print(f"Result dict: {result.__dict__ if hasattr(result, '__dict__') else 'N/A'}")
