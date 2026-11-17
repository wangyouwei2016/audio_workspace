import logging
import os
import json
import shutil
import tempfile
import uuid
import subprocess
import ssl
import threading
import time
import urllib.request
import urllib.error

# Load .env file if it exists
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from flask import Flask, request, jsonify, send_file, render_template

import dashscope
from dashscope.audio.asr import Transcription
from dashscope import Files, HttpSpeechSynthesizer
from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

from srt_to_tts import (
    get_audio_duration_ms,
    normalize_to_wav,
    trim_audio,
    assemble_timeline,
    parse_srt as parse_srt_file,
    AudioPiece,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

# ── Timeline synthesis job tracking ─────────────────────────────────────────

_timeline_jobs: dict[str, dict] = {}
_timeline_lock = threading.Lock()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── 收藏持久化 ───────────────────────────────────────────────────────────────
# favorites.json: { voice_id: { model, id, name, desc, scene, language, age, ssml, instruct, timestamp } }
FAVORITES_FILE = os.path.join(os.path.dirname(__file__), "favorites.json")
_favorites_lock = threading.Lock()


def _read_favorites() -> dict:
    """读取收藏 JSON 文件,文件不存在或损坏时返回空 dict。"""
    if not os.path.exists(FAVORITES_FILE):
        return {}
    try:
        with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_favorites(fav: dict) -> None:
    """写入收藏 JSON 文件(原子写:先写临时文件再 rename)。"""
    tmp = FAVORITES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fav, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FAVORITES_FILE)

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma"}

VOICES = {
    "cosyvoice-v3-flash": {
        "label": "CosyVoice V3 Flash",
        "voices": [
            {"id": "longanyang", "name": "龙安洋", "desc": "阳光大男孩", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": True, "timestamp": True},
            {"id": "longanhuan_v3", "name": "龙安欢（V3）", "desc": "欢脱元气女", "scene": "社交陪伴", "language": "中文（普通话、广东话、东北话、河南话、湖南话、陕西话、山东话、四川话、安徽话）、英文", "age": "20~30岁", "ssml": True, "instruct": True, "timestamp": True},
            {"id": "longanhuan", "name": "龙安欢", "desc": "欢脱元气女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": True, "timestamp": True},
            {"id": "longhuhu_v3", "name": "龙呼呼", "desc": "天真烂漫女童", "scene": "童声", "language": "中文（普通话）、英文", "age": "6~10岁", "ssml": True, "instruct": True, "timestamp": True},
            {"id": "longpaopao_v3", "name": "龙泡泡", "desc": "飞天泡泡音", "scene": "智能玩具/儿童故事机", "language": "中文（普通话）、英文", "age": "6~15岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjielidou_v3", "name": "龙杰力豆", "desc": "阳光顽皮男", "scene": "智能玩具/儿童故事机", "language": "中文（普通话）、英文", "age": "10岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxian_v3", "name": "龙仙", "desc": "豪放可爱女", "scene": "智能玩具/儿童故事机", "language": "中文（普通话）、英文", "age": "12岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longling_v3", "name": "龙铃", "desc": "稚气呆板女", "scene": "智能玩具/儿童故事机", "language": "中文（普通话）、英文", "age": "10岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshanshan_v3", "name": "龙闪闪", "desc": "戏剧化童声", "scene": "消费电子-儿童有声书", "language": "中文（普通话）、英文", "age": "6~15岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longniuniu_v3", "name": "龙牛牛", "desc": "阳光男童声", "scene": "消费电子-儿童有声书", "language": "中文（普通话）、英文", "age": "6~15岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjiaxin_v3", "name": "龙嘉欣", "desc": "优雅粤语女", "scene": "方言", "language": "中文（粤语）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjiayi_v3", "name": "龙嘉怡", "desc": "知性粤语女", "scene": "方言", "language": "中文（粤语）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanyue_v3", "name": "龙安粤", "desc": "欢脱粤语男", "scene": "方言", "language": "中文（粤语）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longlaotie_v3", "name": "龙老铁", "desc": "东北直率男", "scene": "方言", "language": "中文（东北话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshange_v3", "name": "龙陕哥", "desc": "原味陕北男", "scene": "方言", "language": "中文（陕西话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanmin_v3", "name": "龙安闽", "desc": "清纯萝莉女", "scene": "方言", "language": "中文（闽南话）、英文", "age": "18~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "loongkyong_v3", "name": "loongkyong", "desc": "韩语女", "scene": "出海营销", "language": "韩语", "age": "25~30岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongriko_v3", "name": "Riko", "desc": "二次元霓虹女", "scene": "出海营销", "language": "日语", "age": "18~25岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongtomoka_v3", "name": "loongtomoka", "desc": "日语女", "scene": "出海营销", "language": "日语", "age": "30~35岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongabby_v3", "name": "loongabby", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "30~35岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongandy_v3", "name": "loongandy", "desc": "美式英文男", "scene": "出海营销", "language": "美式英语", "age": "30~35岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongannie_v3", "name": "loongannie", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "30~35岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongava_v3", "name": "loongava", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongbeth_v3", "name": "loongbeth", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongbetty_v3", "name": "loongbetty", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongcally_v3", "name": "loongcally", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "25~30岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongcindy_v3", "name": "loongcindy", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "30~35岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongdavid_v3", "name": "loongdavid", "desc": "美式英文男", "scene": "出海营销", "language": "美式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongdonna_v3", "name": "loongdonna", "desc": "美式英文女", "scene": "出海营销", "language": "美式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongemily_v3", "name": "loongemily", "desc": "英式英文女", "scene": "出海营销", "language": "英式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongeric_v3", "name": "loongeric", "desc": "英式英文男", "scene": "出海营销", "language": "英式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongluna_v3", "name": "loongluna", "desc": "英式英文女", "scene": "出海营销", "language": "英式英语", "age": "35~40岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongluca_v3", "name": "loongluca", "desc": "英式英文男", "scene": "出海营销", "language": "英式英语", "age": "25~30岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongtomoya_v3", "name": "loongtomoya", "desc": "日语男", "scene": "出海营销", "language": "日语", "age": "30~35岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongyuuna_v3", "name": "Yuuna", "desc": "日语女", "scene": "出海营销", "language": "日语", "age": "18~25岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongyuuma_v3", "name": "Yuuma", "desc": "日语男", "scene": "出海营销", "language": "日语", "age": "20~25岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongjihun_v3", "name": "Jihun", "desc": "韩语男", "scene": "出海营销", "language": "韩语", "age": "25~30岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongindah_v3", "name": "loongindah", "desc": "印尼女", "scene": "出海营销", "language": "印尼语", "age": "22~27岁", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "longfei_v3", "name": "龙飞", "desc": "热血磁性男", "scene": "诗词朗诵", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingxiao_v3", "name": "龙应笑", "desc": "清甜推销女", "scene": "电话销售", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingxun_v3", "name": "龙应询", "desc": "年轻青涩男", "scene": "客服", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingjing_v3", "name": "龙应静", "desc": "低调冷静女", "scene": "客服", "language": "中文（普通话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingling_v3", "name": "龙应聆", "desc": "温和共情女", "scene": "客服", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingtao_v3", "name": "龙应桃", "desc": "温柔淡定女", "scene": "客服", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiaochun_v3", "name": "龙小淳", "desc": "知性积极女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiaoxia_v3", "name": "龙小夏", "desc": "沉稳权威女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyumi_v3", "name": "YUMI", "desc": "正经青年女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanyun_v3", "name": "龙安昀", "desc": "居家暖男", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanwen_v3", "name": "龙安温", "desc": "优雅知性女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanli_v3", "name": "龙安莉", "desc": "利落从容女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanlang_v3", "name": "龙安朗", "desc": "清爽利落男", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingmu_v3", "name": "龙应沐", "desc": "优雅知性女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longantai_v3", "name": "龙安台", "desc": "嗲甜台湾女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhua_v3", "name": "龙华", "desc": "元气甜美女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longcheng_v3", "name": "龙橙", "desc": "智慧青年男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longze_v3", "name": "龙泽", "desc": "温暖元气男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longzhe_v3", "name": "龙哲", "desc": "呆板大暖男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyan_v3", "name": "龙颜", "desc": "温暖春风女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxing_v3", "name": "龙星", "desc": "温婉邻家女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longtian_v3", "name": "龙天", "desc": "磁性理智男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longwan_v3", "name": "龙婉", "desc": "细腻柔声女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longqiang_v3", "name": "龙嫱", "desc": "浪漫风情女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longfeifei_v3", "name": "龙菲菲", "desc": "甜美娇气女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhao_v3", "name": "龙浩", "desc": "多情忧郁男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanrou_v3", "name": "龙安柔", "desc": "温柔闺蜜女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhan_v3", "name": "龙寒", "desc": "温暖痴情男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanzhi_v3", "name": "龙安智", "desc": "睿智轻熟男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanling_v3", "name": "龙安灵", "desc": "思维灵动女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanya_v3", "name": "龙安雅", "desc": "高雅气质女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanqin_v3", "name": "龙安亲", "desc": "亲和活泼女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longmiao_v3", "name": "龙妙", "desc": "抑扬顿挫女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longsanshu_v3", "name": "龙三叔", "desc": "沉稳质感男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "25~45岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyuan_v3", "name": "龙媛", "desc": "温暖治愈女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "35~40岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyue_v3", "name": "龙悦", "desc": "温暖磁性女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "30~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiu_v3", "name": "龙修", "desc": "博才说书男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "25~35岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longnan_v3", "name": "龙楠", "desc": "睿智青年男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longwanjun_v3", "name": "龙婉君", "desc": "细腻柔声女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyichen_v3", "name": "龙逸尘", "desc": "洒脱活力男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longlaobo_v3", "name": "龙老伯", "desc": "沧桑岁月爷", "scene": "有声书", "language": "中文（普通话）、英文", "age": "60岁以上", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longlaoyi_v3", "name": "龙老姨", "desc": "烟火从容阿姨", "scene": "有声书", "language": "中文（普通话）、英文", "age": "60岁以上", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjiqi_v3", "name": "龙机器", "desc": "呆萌机器人", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhouge_v3", "name": "龙猴哥", "desc": "经典猴哥", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longdaiyu_v3", "name": "龙黛玉", "desc": "娇率才女音", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "15~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanran_v3", "name": "龙安燃", "desc": "活泼质感女", "scene": "直播带货", "language": "中文（普通话）、英文", "age": "30~40岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanxuan_v3", "name": "龙安宣", "desc": "经典直播女", "scene": "直播带货", "language": "中文（普通话）、英文", "age": "30~40岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshuo_v3", "name": "龙硕", "desc": "博才干练男", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshu_v3", "name": "龙书", "desc": "沉稳青年男", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "20~25岁", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "loongbella_v3", "name": "Bella3.0", "desc": "精准干练女", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "25~30岁", "ssml": True, "instruct": False, "timestamp": True},
        ],
    },
    "cosyvoice-v3-plus": {
        "label": "CosyVoice V3 Plus",
        "voices": [
            {"id": "longanyang", "name": "龙安洋", "desc": "阳光大男孩", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": True, "timestamp": True},
            {"id": "longanhuan", "name": "龙安欢", "desc": "欢脱元气女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "20~30岁", "ssml": True, "instruct": True, "timestamp": True},
        ],
    },
    "cosyvoice-v2": {
        "label": "CosyVoice V2",
        "voices": [
            {"id": "longyingxiao", "name": "龙应笑", "desc": "清甜推销女", "scene": "电话销售", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjiqi", "name": "龙机器", "desc": "呆萌机器人", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhouge", "name": "龙猴哥", "desc": "经典猴哥", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjixin", "name": "龙机心", "desc": "毒舌心机女", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanyue", "name": "龙安粤", "desc": "欢脱粤语男", "scene": "短视频配音", "language": "中文（粤语）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshange", "name": "龙陕哥", "desc": "原味陕北男", "scene": "短视频配音", "language": "中文（陕西话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanmin", "name": "龙安敏", "desc": "甜美闽南女", "scene": "短视频配音", "language": "中文（闽南话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longdaiyu", "name": "龙黛玉", "desc": "娇率才女音", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longgaoseng", "name": "龙高僧", "desc": "得道高僧音", "scene": "短视频配音", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanli", "name": "龙安莉", "desc": "利落从容女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanlang", "name": "龙安朗", "desc": "清爽利落男", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanwen", "name": "龙安温", "desc": "优雅知性女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanyun", "name": "龙安昀", "desc": "居家暖男", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyumi_v2", "name": "YUMI", "desc": "正经青年女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiaochun_v2", "name": "龙小淳", "desc": "知性积极女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiaoxia_v2", "name": "龙小夏", "desc": "沉稳权威女", "scene": "语音助手", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyichen", "name": "龙逸尘", "desc": "洒脱活力男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longwanjun", "name": "龙婉君", "desc": "细腻柔声女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longlaobo", "name": "龙老伯", "desc": "沧桑岁月爷", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longlaoyi", "name": "龙老姨", "desc": "烟火从容阿姨", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longbaizhi", "name": "龙白芷", "desc": "睿气旁白女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longsanshu", "name": "龙三叔", "desc": "沉稳质感男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiu_v2", "name": "龙修", "desc": "博才说书男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longmiao_v2", "name": "龙妙", "desc": "抑扬顿挫女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyue_v2", "name": "龙悦", "desc": "温暖磁性女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longnan_v2", "name": "龙楠", "desc": "睿智青年男", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyuan_v2", "name": "龙媛", "desc": "温暖治愈女", "scene": "有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanqin", "name": "龙安亲", "desc": "亲和活泼女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanya", "name": "龙安雅", "desc": "高雅气质女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanshuo", "name": "龙安朔", "desc": "干净清爽男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanling", "name": "龙安灵", "desc": "思维灵动女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanzhi", "name": "龙安智", "desc": "睿智轻熟男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanrou", "name": "龙安柔", "desc": "温柔闺蜜女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longqiang_v2", "name": "龙嫱", "desc": "浪漫风情女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhan_v2", "name": "龙寒", "desc": "温暖痴情男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxing_v2", "name": "龙星", "desc": "温婉邻家女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhua_v2", "name": "龙华", "desc": "元气甜美女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longwan_v2", "name": "龙婉", "desc": "积极知性女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longcheng_v2", "name": "龙橙", "desc": "智慧青年男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longfeifei_v2", "name": "龙菲菲", "desc": "甜美娇气女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiaocheng_v2", "name": "龙小诚", "desc": "磁性低音男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longzhe_v2", "name": "龙哲", "desc": "呆板大暖男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyan_v2", "name": "龙颜", "desc": "温暖春风女", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longtian_v2", "name": "龙天", "desc": "磁性理智男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longze_v2", "name": "龙泽", "desc": "温暖元气男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshao_v2", "name": "龙邵", "desc": "积极向上男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhao_v2", "name": "龙浩", "desc": "多情忧郁男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "kabuleshen_v2", "name": "龙深", "desc": "实力歌手男", "scene": "社交陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longhuhu", "name": "龙呼呼", "desc": "天真烂漫女童", "scene": "童声", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanpei", "name": "龙安培", "desc": "青少年教师女", "scene": "消费电子-教育培训", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longwangwang", "name": "龙汪汪", "desc": "台湾少年音", "scene": "消费电子-儿童陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longpaopao", "name": "龙泡泡", "desc": "飞天泡泡音", "scene": "消费电子-儿童陪伴", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshanshan", "name": "龙闪闪", "desc": "戏剧化童声", "scene": "消费电子-儿童有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longniuniu", "name": "龙牛牛", "desc": "阳光男童声", "scene": "消费电子-儿童有声书", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingmu", "name": "龙应沐", "desc": "优雅知性女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingxun", "name": "龙应询", "desc": "年轻青涩男", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingcui", "name": "龙应催", "desc": "严肃催收男", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingda", "name": "龙应答", "desc": "开朗高音女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingjing", "name": "龙应静", "desc": "低调冷静女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingyan", "name": "龙应严", "desc": "义正严辞女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingtian", "name": "龙应甜", "desc": "温柔甜美女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingbing", "name": "龙应冰", "desc": "尖锐强势女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingtao", "name": "龙应桃", "desc": "温柔淡定女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longyingling", "name": "龙应聆", "desc": "温和共情女", "scene": "客服", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanran", "name": "龙安燃", "desc": "活泼质感女", "scene": "直播带货", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanxuan", "name": "龙安宣", "desc": "经典直播女", "scene": "直播带货", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanchong", "name": "龙安冲", "desc": "激情推销男", "scene": "直播带货", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longanping", "name": "龙安萍", "desc": "高亢直播女", "scene": "直播带货", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjielidou_v2", "name": "龙杰力豆", "desc": "阳光顽皮男", "scene": "童声", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longling_v2", "name": "龙铃", "desc": "稚气呆板女", "scene": "童声", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longke_v2", "name": "龙可", "desc": "懵懂乖乖女", "scene": "童声", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxian_v2", "name": "龙仙", "desc": "豪放可爱女", "scene": "童声", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longlaotie_v2", "name": "龙老铁", "desc": "东北直率男", "scene": "方言", "language": "中文（东北话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjiayi_v2", "name": "龙嘉怡", "desc": "知性粤语女", "scene": "方言", "language": "中文（粤语）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longtao_v2", "name": "龙桃", "desc": "积极粤语女", "scene": "方言", "language": "中文（粤语）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longfei_v2", "name": "龙飞", "desc": "热血磁性男", "scene": "诗词朗诵", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "libai_v2", "name": "李白", "desc": "古代诗仙男", "scene": "诗词朗诵", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjin_v2", "name": "龙津", "desc": "优雅温润男", "scene": "诗词朗诵", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshu_v2", "name": "龙书", "desc": "沉稳青年男", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "loongbella_v2", "name": "Bella2.0", "desc": "精准干练女", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longshuo_v2", "name": "龙硕", "desc": "博才干练男", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longxiaobai_v2", "name": "龙小白", "desc": "沉稳播报女", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "longjing_v2", "name": "龙婧", "desc": "典型播音女", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "loongstella_v2", "name": "loongstella", "desc": "飒爽利落女", "scene": "新闻播报", "language": "中文（普通话）、英文", "age": "", "ssml": True, "instruct": False, "timestamp": True},
            {"id": "loongyuuna_v2", "name": "loongyuuna", "desc": "元气霓虹女", "scene": "出海营销", "language": "日语", "age": "", "ssml": True, "instruct": False, "timestamp": False},
            {"id": "loongyuuma_v2", "name": "loongyuuma", "desc": "干练霓虹男", "scene": "出海营销", "language": "日语", "age": "", "ssml": True, "instruct": False, "timestamp": False},
            {"id": "loongjihun_v2", "name": "loongjihun", "desc": "阳光韩国男", "scene": "出海营销", "language": "韩语", "age": "", "ssml": True, "instruct": False, "timestamp": False},
            {"id": "loongeva_v2", "name": "loongeva", "desc": "知性英文女", "scene": "出海营销", "language": "英式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongbrian_v2", "name": "loongbrian", "desc": "沉稳英文男", "scene": "出海营销", "language": "英式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongluna_v2", "name": "loongluna", "desc": "英式英文女", "scene": "出海营销", "language": "英式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongluca_v2", "name": "loongluca", "desc": "英式英文男", "scene": "出海营销", "language": "英式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongemily_v2", "name": "loongemily", "desc": "英式英文女", "scene": "出海营销", "language": "英式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongeric_v2", "name": "loongeric", "desc": "英式英文男", "scene": "出海营销", "language": "英式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongabby_v2", "name": "loongabby", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongannie_v2", "name": "loongannie", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongandy_v2", "name": "loongandy", "desc": "美式英文男", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongava_v2", "name": "loongava", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongbeth_v2", "name": "loongbeth", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongbetty_v2", "name": "loongbetty", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongcindy_v2", "name": "loongcindy", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongcally_v2", "name": "loongcally", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongdavid_v2", "name": "loongdavid", "desc": "美式英文男", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongdonna_v2", "name": "loongdonna", "desc": "美式英文女", "scene": "出海营销", "language": "美式英文", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongkyong_v2", "name": "loongkyong", "desc": "韩语女", "scene": "出海营销", "language": "韩语", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongtomoka_v2", "name": "loongtomoka", "desc": "日语女", "scene": "出海营销", "language": "日语", "age": "", "ssml": False, "instruct": False, "timestamp": False},
            {"id": "loongtomoya_v2", "name": "loongtomoya", "desc": "日语男", "scene": "出海营销", "language": "日语", "age": "", "ssml": False, "instruct": False, "timestamp": False},
        ],
    },
}

def extract_audio(input_path: str, output_path: str) -> str:
    ext = os.path.splitext(input_path)[1].lower()
    if ext in VIDEO_EXTS:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                output_path,
            ],
            check=True, capture_output=True,
        )
        return output_path
    elif ext in AUDIO_EXTS:
        if ext == ".wav":
            return input_path
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                output_path,
            ],
            check=True, capture_output=True,
        )
        return output_path
    return input_path


def transcribe(audio_path: str) -> list:
    result = Files.upload(file_path=audio_path, purpose="file-extract")
    if result.status_code != 200:
        raise Exception(f"File upload failed: {result}")

    file_id = result.output["uploaded_files"][0]["file_id"]
    file_info = Files.get(file_id=file_id)
    file_url = file_info.output["url"]

    trans_result = Transcription.call(
        model="paraformer-v2",
        file_urls=[file_url],
        language_hints=["zh"],
        timestamp_alignment_enabled=True,
    )

    if trans_result.status_code != 200:
        raise Exception(f"Transcription failed: {trans_result}")

    transcription_url = None
    for r in trans_result.output.get("results", []):
        output = r.get("output", r)
        transcription_url = output.get("transcription_url")
        if transcription_url:
            break

    if not transcription_url:
        raise Exception("No transcription URL in result")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(transcription_url)
    with urllib.request.urlopen(req, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    sentences = data.get("transcripts", [{}])[0].get("sentences", [])
    subtitles = []
    for i, s in enumerate(sentences):
        subtitles.append({
            "id": i + 1,
            "begin_time": s["begin_time"],
            "end_time": s["end_time"],
            "text": s["text"].strip(),
        })
    return subtitles


def _patch_ws_connect(synthesizer):
    from dashscope.audio.tts_v2.speech_synthesizer import websocket as ws_module

    def patched_connect(timeout_seconds=30):
        synthesizer.ws = ws_module.WebSocketApp(
            synthesizer.url,
            header=synthesizer.request.get_websocket_headers(
                headers=synthesizer.headers, workspace=synthesizer.workspace
            ),
            on_message=synthesizer.on_message,
            on_error=synthesizer.on_error,
            on_close=synthesizer.on_close,
        )
        synthesizer.thread = threading.Thread(
            target=synthesizer.ws.run_forever,
            kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}},
        )
        synthesizer.thread.daemon = True
        synthesizer.thread.start()
        start = time.time()
        while (
            not (synthesizer.ws.sock and synthesizer.ws.sock.connected)
            and (time.time() - start) < timeout_seconds
        ):
            time.sleep(0.1)
        if not (synthesizer.ws.sock and synthesizer.ws.sock.connected):
            raise TimeoutError("WebSocket connection timeout")

    synthesizer._SpeechSynthesizer__connect = patched_connect


def synthesize_cosyvoice(model: str, voice: str, text: str, instruction: str = None) -> bytes:
    kwargs = dict(
        model=model,
        voice=voice,
        format=AudioFormat.MP3_22050HZ_MONO_256KBPS,
    )
    if instruction:
        kwargs["instruction"] = instruction
    synthesizer = SpeechSynthesizer(**kwargs)
    _patch_ws_connect(synthesizer)
    audio = synthesizer.call(text)
    if audio:
        return audio
    resp = synthesizer.get_response()
    msg = str(resp)
    if "418" in msg:
        raise Exception(f"音色 '{voice}' 与模型 '{model}' 不兼容，请换选其他音色或模型")
    raise Exception(f"合成失败: {msg}")


# ── Timeline Synthesis ───────────────────────────────────────────────────────

TTS_DELAY_SECONDS = 0.5


def _run_timeline_synthesis(job_id: str, model: str, voice: str, segments: list, instruction: str = None) -> None:
    """Background thread: synthesize each subtitle segment and assemble on timeline."""
    temp_dir = tempfile.mkdtemp(prefix=f"tts_job_{job_id}_")

    try:
        with _timeline_lock:
            _timeline_jobs[job_id]["total"] = len(segments)

        synthesized: list[tuple[int, int, str]] = []  # (start_ms, end_ms, wav_path)

        for i, seg in enumerate(segments):
            with _timeline_lock:
                _timeline_jobs[job_id].update({
                    "current": i + 1,
                    "status": "synthesizing",
                    "status_text": f"合成中 ({i + 1}/{len(segments)}): {seg['text'][:25]}...",
                })

            try:
                audio_bytes = synthesize_cosyvoice(model, voice, seg["text"], instruction=instruction)

                temp_mp3 = os.path.join(temp_dir, f"seg_{i:03d}.mp3")
                with open(temp_mp3, "wb") as f:
                    f.write(audio_bytes)

                duration_ms = get_audio_duration_ms(temp_mp3)
                window_ms = int(seg["end_time"]) - int(seg["begin_time"])

                # Calculate available time: extend into gap before next subtitle
                if i < len(segments) - 1:
                    available_ms = int(segments[i + 1]["begin_time"]) - int(seg["begin_time"])
                else:
                    last_end = max(int(s["end_time"]) for s in segments)
                    available_ms = (last_end + 1000) - int(seg["begin_time"])

                if duration_ms > available_ms + 200:
                    # Truly unavoidable overlap with next segment — trim to available
                    overshoot = duration_ms - available_ms
                    logger.warning(
                        "Job %s seg %d: audio %dms > available %dms (+%dms), trimming",
                        job_id, i, duration_ms, available_ms, overshoot,
                    )
                    trimmed = os.path.join(temp_dir, f"trimmed_{i:03d}.wav")
                    trim_audio(temp_mp3, available_ms, trimmed)
                    synthesized.append((int(seg["begin_time"]), int(seg["end_time"]), trimmed))
                elif duration_ms > window_ms + 200:
                    # Audio exceeds subtitle window but fits in gap — keep full audio
                    normalized = os.path.join(temp_dir, f"norm_{i:03d}.wav")
                    normalize_to_wav(temp_mp3, normalized)
                    synthesized.append((int(seg["begin_time"]), int(seg["end_time"]), normalized))
                else:
                    normalized = os.path.join(temp_dir, f"norm_{i:03d}.wav")
                    normalize_to_wav(temp_mp3, normalized)
                    synthesized.append((int(seg["begin_time"]), int(seg["end_time"]), normalized))

                with _timeline_lock:
                    _timeline_jobs[job_id]["completed"] += 1

            except Exception as e:
                logger.warning("Job %s segment %d failed: %s", job_id, i, e)
                with _timeline_lock:
                    _timeline_jobs[job_id]["failed"] += 1

            if i < len(segments) - 1:
                time.sleep(TTS_DELAY_SECONDS)

        # Assemble timeline
        if not synthesized:
            raise RuntimeError("所有片段合成均失败，无法生成配音")

        with _timeline_lock:
            _timeline_jobs[job_id].update({
                "status": "assembling",
                "status_text": "正在按时间轴拼接音频...",
            })

        pieces = [
            AudioPiece(path=wav_path, start_ms=start_ms, is_silence=False)
            for start_ms, end_ms, wav_path in synthesized
        ]

        last_end_ms = max(end_ms for _, end_ms, _ in synthesized)
        total_duration_ms = last_end_ms + 1000

        output_path = os.path.join(UPLOAD_DIR, f"timeline_{job_id}.mp3")
        assemble_timeline(pieces, total_duration_ms, output_path)

        with _timeline_lock:
            _timeline_jobs[job_id].update({
                "status": "done",
                "status_text": "合成完成！",
                "output_path": output_path,
            })

    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e)
        with _timeline_lock:
            _timeline_jobs[job_id].update({
                "status": "error",
                "status_text": f"合成失败: {e}",
            })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ── Routes ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/voices")
def get_voices():
    return jsonify(VOICES)


# ── 收藏 API ─────────────────────────────────────────────────────────────────


@app.route("/api/favorites")
def get_favorites():
    """获取全部收藏音色。返回 { voice_id: { ...音色字段 } }。"""
    with _favorites_lock:
        return jsonify(_read_favorites())


@app.route("/api/favorites", methods=["POST"])
def add_favorite():
    """添加一个收藏。请求体: { "voice_id": "...", "voice": { ...音色字段 } }。"""
    data = request.get_json() or {}
    voice_id = data.get("voice_id", "").strip()
    voice = data.get("voice", {})
    if not voice_id or not voice:
        return jsonify({"error": "Missing voice_id or voice"}), 400

    with _favorites_lock:
        fav = _read_favorites()
        # 只保留必要的字段(忽略 _model/_showModel 等前端临时字段)
        fav[voice_id] = {
            "model": voice.get("model", ""),
            "id": voice.get("id", voice_id),
            "name": voice.get("name", ""),
            "desc": voice.get("desc", ""),
            "scene": voice.get("scene", ""),
            "language": voice.get("language", ""),
            "age": voice.get("age", ""),
            "ssml": bool(voice.get("ssml", False)),
            "instruct": bool(voice.get("instruct", False)),
            "timestamp": bool(voice.get("timestamp", False)),
        }
        _write_favorites(fav)
    return jsonify({"ok": True, "count": len(fav)})


@app.route("/api/favorites/<voice_id>", methods=["DELETE"])
def remove_favorite(voice_id: str):
    """删除一个收藏。"""
    with _favorites_lock:
        fav = _read_favorites()
        if voice_id not in fav:
            return jsonify({"error": "Not in favorites"}), 404
        del fav[voice_id]
        _write_favorites(fav)
    return jsonify({"ok": True, "count": len(fav)})


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in VIDEO_EXTS | AUDIO_EXTS:
        return jsonify({"error": f"Unsupported format: {ext}"}), 400

    task_id = uuid.uuid4().hex[:8]
    input_path = os.path.join(UPLOAD_DIR, f"{task_id}_input{ext}")
    file.save(input_path)

    try:
        wav_path = os.path.join(UPLOAD_DIR, f"{task_id}.wav")
        audio_path = extract_audio(input_path, wav_path)

        subtitles = transcribe(audio_path)
        return jsonify({"subtitles": subtitles, "task_id": task_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in [input_path, os.path.join(UPLOAD_DIR, f"{task_id}.wav")]:
            if os.path.exists(p):
                os.remove(p)


@app.route("/api/upload-srt", methods=["POST"])
def upload_srt():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext != ".srt":
        return jsonify({"error": f"Only .srt files supported, got: {ext}"}), 400

    task_id = uuid.uuid4().hex[:8]
    srt_path = os.path.join(UPLOAD_DIR, f"{task_id}.srt")
    file.save(srt_path)

    try:
        entries = parse_srt_file(srt_path)
        subtitles = [
            {
                "id": e.index,
                "begin_time": e.start_ms,
                "end_time": e.end_ms,
                "text": e.text,
            }
            for e in entries
        ]
        return jsonify({"subtitles": subtitles, "task_id": task_id})
    except Exception as e:
        return jsonify({"error": f"Failed to parse SRT: {e}"}), 500
    finally:
        if os.path.exists(srt_path):
            os.remove(srt_path)


@app.route("/api/synthesize", methods=["POST"])
def synthesize():
    data = request.get_json()
    model = data.get("model", "")
    voice = data.get("voice", "")
    text = data.get("text", "")
    instruction = data.get("instruction", "").strip() or None

    if not all([model, voice, text]):
        return jsonify({"error": "Missing model, voice, or text"}), 400

    try:
        audio = synthesize_cosyvoice(model, voice, text, instruction=instruction)

        output_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}.mp3")
        with open(output_path, "wb") as f:
            f.write(audio)

        return send_file(output_path, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/synthesize-timeline", methods=["POST"])
def synthesize_timeline():
    data = request.get_json()
    model = data.get("model", "")
    voice = data.get("voice", "")
    segments = data.get("segments", [])
    instruction = data.get("instruction", "").strip() or None

    if not all([model, voice]) or not segments:
        return jsonify({"error": "Missing model, voice, or segments"}), 400

    for seg in segments:
        if not all(k in seg for k in ("begin_time", "end_time", "text")):
            return jsonify({"error": "Each segment must have begin_time, end_time, text"}), 400

    job_id = uuid.uuid4().hex[:12]
    with _timeline_lock:
        _timeline_jobs[job_id] = {
            "total": len(segments),
            "current": 0,
            "completed": 0,
            "failed": 0,
            "status": "starting",
            "status_text": "正在启动...",
            "output_path": None,
        }

    thread = threading.Thread(
        target=_run_timeline_synthesis,
        args=(job_id, model, voice, segments, instruction),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/synthesize-status/<job_id>")
def synthesize_status(job_id: str):
    with _timeline_lock:
        job = _timeline_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "total": job["total"],
        "current": job["current"],
        "completed": job["completed"],
        "failed": job["failed"],
        "status": job["status"],
        "status_text": job["status_text"],
    })


@app.route("/api/synthesize-download/<job_id>")
def synthesize_download(job_id: str):
    with _timeline_lock:
        job = _timeline_jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Audio not ready"}), 404
    return send_file(job["output_path"], mimetype="audio/mpeg", as_attachment=False)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
