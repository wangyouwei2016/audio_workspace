let subtitles = [];
let selectedVoice = null;
let voicesData = {};

// --- 收藏管理 (后端 favorites.json 持久化) ---
// 内存缓存(初始化时从后端拉取,增删时同步更新)
// key: voiceId, value: { model, id, name, desc, scene, language, age, ssml, instruct, timestamp }
let favoritesCache = {};

async function loadFavoritesFromServer() {
    try {
        const resp = await fetch('/api/favorites');
        favoritesCache = await resp.json();
    } catch (err) {
        console.error('加载收藏失败:', err);
        favoritesCache = {};
    }
}

function isFavorited(voiceId) {
    return voiceId in favoritesCache;
}

// 切换收藏状态(异步调用后端 API,成功后更新缓存)
// 返回 Promise<boolean>: true 表示已收藏, false 表示已取消
async function toggleFavorite(voice) {
    const voiceId = voice.id;
    if (voiceId in favoritesCache) {
        // 取消收藏
        const resp = await fetch(`/api/favorites/${encodeURIComponent(voiceId)}`, { method: 'DELETE' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || '取消收藏失败');
        }
        delete favoritesCache[voiceId];
        return false;
    } else {
        // 添加收藏
        const resp = await fetch('/api/favorites', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voice_id: voiceId,
                voice: {
                    model: voice._model,
                    id: voice.id, name: voice.name, desc: voice.desc,
                    scene: voice.scene, language: voice.language, age: voice.age,
                    ssml: voice.ssml, instruct: voice.instruct, timestamp: voice.timestamp,
                },
            }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || '收藏失败');
        }
        favoritesCache[voiceId] = {
            model: voice._model,
            id: voice.id, name: voice.name, desc: voice.desc,
            scene: voice.scene, language: voice.language, age: voice.age,
            ssml: voice.ssml, instruct: voice.instruct, timestamp: voice.timestamp,
        };
        return true;
    }
}

// 当前正在试听的音频(全局唯一,跨卡片共享)
let previewAudio = null;

// --- Instruct 指令管理 ---
// 当前选中的音色对象(用于判断是否支持 instruct)
let selectedVoiceObj = null;
// 当前 instruct 指令文本
let currentInstruction = '';

// 情感值(文档固定 7 种)
const EMOTIONS = [
    { value: 'neutral', label: 'neutral（中性）' },
    { value: 'fearful', label: 'fearful（害怕）' },
    { value: 'angry', label: 'angry（愤怒）' },
    { value: 'sad', label: 'sad（悲伤）' },
    { value: 'surprised', label: 'surprised（惊讶）' },
    { value: 'happy', label: 'happy（开心）' },
    { value: 'disgusted', label: 'disgusted（厌恶）' },
];

// 场景值(文档固定 7 种)
const SCENES = ['闲聊互动', '新闻播报', '广告促销', '比赛解说', '一些儿童内容解说', '语音导航', '脱口秀表演'];

// 角色(文档仅一个)
const ROLES = ['一个旁白'];

// 身份(文档仅一个)
const IDENTITIES = ['故事机'];

// 根据模板类型 + 选项生成 instruct 文本(严格按文档格式,含结尾句号)
function buildInstruction(template, opts) {
    const emotion = opts.emotion || '';
    switch (template) {
        case 'emotion':
            // 格式："你说话的情感是<情感值>。"
            return `你说话的情感是${emotion}。`;
        case 'scene_emotion': {
            // 格式："你正在进行<场景>，你说话的情感是<情感值>。"
            const scene = opts.scene || '';
            return `你正在进行${scene}，你说话的情感是${emotion}。`;
        }
        case 'role_emotion': {
            // 格式："你现在说话的角色是<角色>，你说话的情感是<情感值>。"
            const role = opts.role || '';
            return `你现在说话的角色是${role}，你说话的情感是${emotion}。`;
        }
        case 'identity_emotion': {
            // 格式："你正在以一个<身份>的身份说话，你说话的情感是<情感值>。"
            const identity = opts.identity || '';
            return `你正在以一个${identity}的身份说话，你说话的情感是${emotion}。`;
        }
        default:
            return '';
    }
}

// 校验 instruction 文本中各槽位是否在文档允许的合法集合内
// 返回: { ok: boolean, errors: string[] } errors 为空表示通过
function validateInstruction(text) {
    const errors = [];
    if (!text || !text.trim()) return { ok: true, errors: [] }; // 空指令视为未使用 instruct
    const t = text.trim();

    // 检查结尾句号(文档明确要求)
    if (!t.endsWith('。')) {
        errors.push('指令必须以中文句号「。」结尾');
    }

    // 用正则提取各槽位(模板格式固定,可以稳定匹配)
    // 场景:你正在进行<场景>，你说话的情感是<情感值>。
    const sceneMatch = t.match(/你正在进行(.+?)，你说话的情感是/);
    if (sceneMatch) {
        const scene = sceneMatch[1].trim();
        if (!SCENES.includes(scene)) {
            errors.push(`场景「${scene}」不在支持列表内。支持的场景：${SCENES.join('、')}`);
        }
    }

    // 角色:你现在说话的角色是<角色>，你说话的情感是<情感值>。
    const roleMatch = t.match(/你现在说话的角色是(.+?)，你说话的情感是/);
    if (roleMatch) {
        const role = roleMatch[1].trim();
        if (!ROLES.includes(role)) {
            errors.push(`角色「${role}」不在支持列表内。支持的角色：${ROLES.join('、')}`);
        }
    }

    // 身份:你正在以一个<身份>的身份说话，你说话的情感是<情感值>。
    const identityMatch = t.match(/你正在以一个(.+?)的身份说话，你说话的情感是/);
    if (identityMatch) {
        const identity = identityMatch[1].trim();
        if (!IDENTITIES.includes(identity)) {
            errors.push(`身份「${identity}」不在支持列表内。支持的身份：${IDENTITIES.join('、')}`);
        }
    }

    // 情感:任意模板都包含 "你说话的情感是<情感值>。"
    const emotionMatch = t.match(/你说话的情感是(\w+)\。/);
    if (emotionMatch) {
        const emotion = emotionMatch[1].trim();
        const validEmotions = EMOTIONS.map(e => e.value);
        if (!validEmotions.includes(emotion)) {
            errors.push(`情感值「${emotion}」不在支持列表内。支持的情感：${validEmotions.join('、')}`);
        }
    }

    return { ok: errors.length === 0, errors };
}

// 渲染模板选项(根据选中的模板按钮显示对应选项)
function renderInstructOptions(template) {
    const container = document.getElementById('instructOptions');
    container.innerHTML = '';
    let html = '';

    if (template === 'emotion') {
        html += '<div class="instruct-option-group"><label>情感</label><div class="instruct-chips">';
        EMOTIONS.forEach(e => {
            html += `<button class="instruct-chip" data-group="emotion" data-value="${e.value}">${e.label}</button>`;
        });
        html += '</div></div>';
    } else if (template === 'scene_emotion') {
        html += '<div class="instruct-option-group"><label>场景</label><div class="instruct-chips">';
        SCENES.forEach(s => {
            html += `<button class="instruct-chip" data-group="scene" data-value="${s}">${s}</button>`;
        });
        html += '</div></div>';
        html += '<div class="instruct-option-group"><label>情感</label><div class="instruct-chips">';
        EMOTIONS.forEach(e => {
            html += `<button class="instruct-chip" data-group="emotion" data-value="${e.value}">${e.label}</button>`;
        });
        html += '</div></div>';
    } else if (template === 'role_emotion') {
        html += '<div class="instruct-option-group"><label>角色</label><div class="instruct-chips">';
        ROLES.forEach(r => {
            html += `<button class="instruct-chip" data-group="role" data-value="${r}">${r}</button>`;
        });
        html += '</div></div>';
        html += '<div class="instruct-option-group"><label>情感</label><div class="instruct-chips">';
        EMOTIONS.forEach(e => {
            html += `<button class="instruct-chip" data-group="emotion" data-value="${e.value}">${e.label}</button>`;
        });
        html += '</div></div>';
    } else if (template === 'identity_emotion') {
        html += '<div class="instruct-option-group"><label>身份</label><div class="instruct-chips">';
        IDENTITIES.forEach(i => {
            html += `<button class="instruct-chip" data-group="identity" data-value="${i}">${i}</button>`;
        });
        html += '</div></div>';
        html += '<div class="instruct-option-group"><label>情感</label><div class="instruct-chips">';
        EMOTIONS.forEach(e => {
            html += `<button class="instruct-chip" data-group="emotion" data-value="${e.value}">${e.label}</button>`;
        });
        html += '</div></div>';
    }

    container.innerHTML = html;
    container.classList.remove('hidden');

    // 当前模板下的选项状态
    const opts = { emotion: '', scene: '', role: '', identity: '' };
    let activeTemplate = template;

    container.querySelectorAll('.instruct-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const group = chip.dataset.group;
            const value = chip.dataset.value;
            // 单选同组
            container.querySelectorAll(`.instruct-chip[data-group="${group}"]`).forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            opts[group] = value;

            // 检查必填项是否齐全
            const ready =
                (activeTemplate === 'emotion' && opts.emotion) ||
                (activeTemplate === 'scene_emotion' && opts.scene && opts.emotion) ||
                (activeTemplate === 'role_emotion' && opts.role && opts.emotion) ||
                (activeTemplate === 'identity_emotion' && opts.identity && opts.emotion);

            if (ready) {
                const text = buildInstruction(activeTemplate, opts);
                document.getElementById('instructText').value = text;
                currentInstruction = text;
                refreshInstructError(); // 模板生成的指令一定是合法的,这会清掉之前的错误提示
            }
        });
    });
}

// 显示/隐藏 Instruct 面板(根据选中音色是否支持 instruct)
function updateInstructPanel(voice) {
    selectedVoiceObj = voice;
    const panel = document.getElementById('instructPanel');
    if (voice && voice.instruct) {
        panel.classList.remove('hidden');
        bounceIn(panel);
    } else {
        panel.classList.add('hidden');
        // 清空状态
        document.getElementById('instructText').value = '';
        document.getElementById('instructOptions').classList.add('hidden');
        document.getElementById('instructOptions').innerHTML = '';
        document.querySelectorAll('.instruct-template-btn').forEach(b => b.classList.remove('active'));
        currentInstruction = '';
    }
}

// 绑定 Instruct 面板事件(初始化时调用一次)
function initInstructPanel() {
    // 模板按钮
    document.querySelectorAll('.instruct-template-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const template = btn.dataset.template;
            // 切换激活态:再次点击同一个则取消
            if (btn.classList.contains('active')) {
                btn.classList.remove('active');
                document.getElementById('instructOptions').classList.add('hidden');
                document.getElementById('instructOptions').innerHTML = '';
                return;
            }
            document.querySelectorAll('.instruct-template-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderInstructOptions(template);
        });
    });

    // 文本框手动编辑(实时校验)
    document.getElementById('instructText').addEventListener('input', (e) => {
        currentInstruction = e.target.value.trim();
        refreshInstructError();
    });

    // 清空按钮
    document.getElementById('instructClearBtn').addEventListener('click', () => {
        document.getElementById('instructText').value = '';
        document.getElementById('instructOptions').classList.add('hidden');
        document.getElementById('instructOptions').innerHTML = '';
        document.querySelectorAll('.instruct-template-btn').forEach(b => b.classList.remove('active'));
        currentInstruction = '';
        refreshInstructError();
    });
}

// 实时校验并显示/隐藏错误提示
function refreshInstructError() {
    const errBox = document.getElementById('instructError');
    if (!errBox) return;
    if (!currentInstruction) {
        errBox.classList.add('hidden');
        errBox.innerHTML = '';
        return;
    }
    const { ok, errors } = validateInstruction(currentInstruction);
    if (ok) {
        errBox.classList.add('hidden');
        errBox.innerHTML = '';
    } else {
        errBox.classList.remove('hidden');
        errBox.innerHTML = errors.map(e => `• ${e}`).join('<br>');
    }
}

// --- Clay Bounce Animations ---
function bounceIn(el, delay = 0) {
    el.style.opacity = '0';
    el.style.transform = 'scale(0.85) translateY(18px)';
    setTimeout(() => {
        el.style.transition = 'all 0.45s cubic-bezier(0.34, 1.56, 0.64, 1)';
        el.style.opacity = '1';
        el.style.transform = 'scale(1) translateY(0)';
    }, delay);
}

function addBounceOnClick(el) {
    el.addEventListener('mousedown', () => {
        el.style.transition = 'all 0.12s ease';
        el.style.transform = 'translateY(1px) scale(0.96)';
    });
    el.addEventListener('mouseup', () => {
        el.style.transition = 'all 0.4s cubic-bezier(0.34, 1.56, 0.64, 1)';
        el.style.transform = '';
    });
    el.addEventListener('mouseleave', () => {
        el.style.transition = 'all 0.3s ease';
        el.style.transform = '';
    });
}

// Apply bounce to all current and future buttons
function applyBounceToButtons() {
    document.querySelectorAll('.btn').forEach(btn => {
        if (!btn.dataset.bound) {
            addBounceOnClick(btn);
            btn.dataset.bound = '1';
        }
    });
}

// --- Step Navigation ---
function goToStep(n) {
    document.querySelectorAll('.step-panel').forEach(el => {
        el.classList.add('hidden');
        el.classList.remove('active');
    });
    const panel = document.getElementById(`step${n}`);
    panel.classList.remove('hidden');
    panel.classList.add('active');
    bounceIn(panel);

    document.querySelectorAll('.step-dot').forEach((dot, i) => {
        dot.classList.remove('active', 'completed');
        if (i + 1 < n) dot.classList.add('completed');
        if (i + 1 === n) dot.classList.add('active');
    });
}

// --- Step 1: Upload & Transcribe ---
const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');
const uploadStatus = document.getElementById('uploadStatus');
const progressFill = document.getElementById('progressFill');
const statusText = document.getElementById('statusText');

uploadArea.addEventListener('click', () => fileInput.click());
uploadArea.addEventListener('dragover', e => { e.preventDefault(); uploadArea.classList.add('dragover'); });
uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
uploadArea.addEventListener('drop', e => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) handleFile(fileInput.files[0]); });

async function handleFile(file) {
    uploadStatus.classList.remove('hidden');
    progressFill.style.width = '10%';
    statusText.textContent = `正在上传 ${file.name}...`;

    const formData = new FormData();
    formData.append('file', file);

    try {
        const xhr = new XMLHttpRequest();
        const result = await new Promise((resolve, reject) => {
            xhr.upload.addEventListener('progress', e => {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 40);
                    progressFill.style.width = pct + '%';
                }
            });
            xhr.addEventListener('load', () => {
                if (xhr.status === 200) resolve(JSON.parse(xhr.responseText));
                else reject(new Error(JSON.parse(xhr.responseText).error || 'Upload failed'));
            });
            xhr.addEventListener('error', () => reject(new Error('Network error')));
            xhr.open('POST', '/api/upload');
            xhr.send(formData);
        });

        progressFill.style.width = '80%';
        statusText.textContent = '正在生成字幕...';

        if (result.error) throw new Error(result.error);

        subtitles = result.subtitles;
        progressFill.style.width = '100%';
        statusText.textContent = `字幕生成完成！共 ${subtitles.length} 条`;

        setTimeout(() => {
            renderSubtitles();
            goToStep(2);
        }, 800);
    } catch (err) {
        progressFill.style.width = '100%';
        progressFill.style.background = 'var(--danger)';
        statusText.textContent = `错误: ${err.message}`;
    }
}

// --- Step 2: SRT Upload ---
const srtUploadArea = document.getElementById('srtUploadArea');
const srtFileInput = document.getElementById('srtFileInput');
const srtUploadStatus = document.getElementById('srtUploadStatus');
const srtStatusText = document.getElementById('srtStatusText');

srtUploadArea.addEventListener('click', () => srtFileInput.click());
srtUploadArea.addEventListener('dragover', e => { e.preventDefault(); srtUploadArea.classList.add('dragover'); });
srtUploadArea.addEventListener('dragleave', () => srtUploadArea.classList.remove('dragover'));
srtUploadArea.addEventListener('drop', e => {
    e.preventDefault();
    srtUploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleSrtFile(e.dataTransfer.files[0]);
});
srtFileInput.addEventListener('change', () => { if (srtFileInput.files.length) handleSrtFile(srtFileInput.files[0]); });

async function handleSrtFile(file) {
    if (!file.name.toLowerCase().endsWith('.srt')) {
        srtUploadStatus.classList.remove('hidden');
        srtStatusText.textContent = '错误: 请上传 .srt 格式文件';
        srtStatusText.style.color = 'var(--danger)';
        return;
    }

    srtUploadStatus.classList.remove('hidden');
    srtStatusText.style.color = '';
    srtStatusText.textContent = `正在解析 ${file.name}...`;

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/upload-srt', { method: 'POST', body: formData });
        const result = await resp.json();

        if (result.error) throw new Error(result.error);

        subtitles = result.subtitles;
        srtStatusText.textContent = `解析完成！共 ${subtitles.length} 条字幕`;
        renderSubtitles();

        setTimeout(() => srtUploadStatus.classList.add('hidden'), 2000);
    } catch (err) {
        srtStatusText.textContent = `解析失败: ${err.message}`;
        srtStatusText.style.color = 'var(--danger)';
    }
}

// --- Step 2: Edit Subtitles ---
function renderSubtitles() {
    const list = document.getElementById('subtitleList');
    list.innerHTML = '';
    subtitles.forEach((s, i) => {
        const div = document.createElement('div');
        div.className = 'subtitle-item';
        div.innerHTML = `
            <div class="subtitle-num">${i + 1}</div>
            <div class="subtitle-fields">
                <div class="subtitle-time-row">
                    <input type="text" value="${formatTime(s.begin_time)}" data-idx="${i}" data-field="begin_time" class="time-input">
                    <span>→</span>
                    <input type="text" value="${formatTime(s.end_time)}" data-idx="${i}" data-field="end_time" class="time-input">
                </div>
                <textarea class="subtitle-text-input" data-idx="${i}" data-field="text" rows="2">${s.text}</textarea>
            </div>
            <button class="subtitle-delete" data-idx="${i}" title="删除">&times;</button>
        `;
        list.appendChild(div);
    });

    list.querySelectorAll('.time-input, .subtitle-text-input').forEach(input => {
        input.addEventListener('change', e => {
            const idx = parseInt(e.target.dataset.idx);
            const field = e.target.dataset.field;
            if (field === 'text') {
                subtitles[idx].text = e.target.value;
            } else {
                subtitles[idx][field] = parseTime(e.target.value);
            }
        });
    });

    list.querySelectorAll('.subtitle-delete').forEach(btn => {
        btn.addEventListener('click', e => {
            const idx = parseInt(e.target.dataset.idx);
            subtitles.splice(idx, 1);
            renderSubtitles();
        });
    });

    applyBounceToButtons();
}

function formatTime(ms) {
    const totalSec = ms / 1000;
    const h = String(Math.floor(totalSec / 3600)).padStart(2, '0');
    const m = String(Math.floor((totalSec % 3600) / 60)).padStart(2, '0');
    const s = String(Math.floor(totalSec % 60)).padStart(2, '0');
    const msPart = String(Math.round((totalSec - Math.floor(totalSec)) * 1000)).padStart(3, '0');
    return `${h}:${m}:${s},${msPart}`;
}

function parseTime(str) {
    const parts = str.replace(',', ':').split(':').map(Number);
    if (parts.length === 4) return (parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000 + parts[3];
    if (parts.length === 3) return (parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000;
    return 0;
}

function generateSrt() {
    return subtitles.map((s, i) =>
        `${i + 1}\n${formatTime(s.begin_time)} --> ${formatTime(s.end_time)}\n${s.text}\n`
    ).join('\n');
}

document.getElementById('addSubtitleBtn').addEventListener('click', () => {
    const lastEnd = subtitles.length ? subtitles[subtitles.length - 1].end_time : 0;
    subtitles.push({ id: subtitles.length + 1, begin_time: lastEnd, end_time: lastEnd + 3000, text: '新字幕文本' });
    renderSubtitles();
});

document.getElementById('previewSrtBtn').addEventListener('click', () => {
    document.getElementById('srtPreview').textContent = generateSrt();
    document.getElementById('srtModal').classList.remove('hidden');
});

document.getElementById('closeModalBtn').addEventListener('click', () => document.getElementById('srtModal').classList.add('hidden'));
document.getElementById('closeModalBtn2').addEventListener('click', () => document.getElementById('srtModal').classList.add('hidden'));

document.getElementById('downloadSrtBtn').addEventListener('click', () => {
    const blob = new Blob([generateSrt()], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'subtitles.srt';
    a.click();
});

document.getElementById('backToStep1Btn').addEventListener('click', () => goToStep(1));
document.getElementById('toStep3Btn').addEventListener('click', () => {
    const synthInfo = document.getElementById('synthInfo');
    const countEl = document.getElementById('synthInfoCount');
    if (subtitles.length > 0) {
        const totalSec = Math.max(...subtitles.map(s => s.end_time)) / 1000;
        const mins = Math.floor(totalSec / 60);
        const secs = Math.floor(totalSec % 60);
        countEl.textContent = `共 ${subtitles.length} 条字幕，总时长 ${mins}分${secs}秒`;
        synthInfo.classList.remove('hidden');
    }
    goToStep(3);
});

// --- Step 3: Synthesize ---
async function loadVoices() {
    const resp = await fetch('/api/voices');
    voicesData = await resp.json();
    const modelSelect = document.getElementById('modelSelect');
    modelSelect.innerHTML = '<option value="">请选择模型</option>';
    // "我的收藏" 选项 — 放在最前面
    const favOpt = document.createElement('option');
    favOpt.value = '__favorites__';
    favOpt.textContent = '⭐ 我的收藏';
    modelSelect.appendChild(favOpt);
    Object.entries(voicesData).forEach(([key, val]) => {
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = val.label;
        modelSelect.appendChild(opt);
    });
}

// 渲染单张音色卡片(普通模型 / 收藏列表共用)
// voice: 音色对象 (普通模型需要带 _model 字段; 收藏列表已带 model 字段,会归一化为 _model)
// idx: 用于错开动画延迟
function renderVoiceCard(voice, idx) {
    const card = document.createElement('div');
    card.className = 'voice-card';
    card.dataset.voiceId = voice.id;
    card.dataset.model = voice._model;

    // 构建特性标签
    const features = [];
    if (voice.ssml) features.push('SSML');
    if (voice.instruct) features.push('Instruct');
    if (voice.timestamp) features.push('时间戳');
    const featureTags = features.map(f => `<span class="voice-feature-tag">${f}</span>`).join('');

    const sceneTag = voice.scene ? `<span class="voice-scene-tag">${voice.scene}</span>` : '';
    const ageInfo = voice.age ? `<span class="voice-meta-item">年龄：${voice.age}</span>` : '';
    // 在收藏列表中显示音色所属模型标签
    const modelTag = voice._showModel && voicesData[voice._model]
        ? `<span class="voice-model-tag">${voicesData[voice._model].label}</span>`
        : '';
    const favorited = isFavorited(voice.id);

    card.innerHTML = `
        <div class="voice-card-header">
            <div class="voice-name">${voice.name}</div>
            <div class="voice-card-actions">
                <button class="voice-fav-btn ${favorited ? 'favorited' : ''}" title="${favorited ? '取消收藏' : '收藏'}" aria-label="收藏">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="${favorited ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
                    </svg>
                </button>
                <button class="voice-preview-btn" title="点击试听" aria-label="试听">
                    <svg class="icon-play" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M8 5v14l11-7z"/>
                    </svg>
                    <svg class="icon-loading" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="display:none;">
                        <path d="M21 12a9 9 0 1 1-6.219-8.56" stroke-linecap="round"/>
                    </svg>
                    <svg class="icon-stop" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="display:none;">
                        <rect x="6" y="6" width="12" height="12" rx="2"/>
                    </svg>
                </button>
            </div>
        </div>
        <div class="voice-desc">${voice.desc}</div>
        <div class="voice-meta">
            ${ageInfo}
            <span class="voice-meta-item">语言：${voice.language}</span>
        </div>
        <div class="voice-tags">${modelTag}${sceneTag}${featureTags}</div>
    `;

    const favBtn = card.querySelector('.voice-fav-btn');
    const previewBtn = card.querySelector('.voice-preview-btn');
    const iconPlay = card.querySelector('.icon-play');
    const iconLoading = card.querySelector('.icon-loading');
    const iconStop = card.querySelector('.icon-stop');

    // 收藏按钮(异步调用后端 API)
    favBtn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        // 操作中禁用按钮,防止快速连点
        if (favBtn.disabled) return;
        favBtn.disabled = true;
        try {
            const nowFav = await toggleFavorite(voice);
            if (nowFav) {
                favBtn.classList.add('favorited');
                favBtn.title = '取消收藏';
                favBtn.querySelector('svg').setAttribute('fill', 'currentColor');
                // 弹跳动画
                favBtn.style.transform = 'scale(1.35)';
                setTimeout(() => { favBtn.style.transform = ''; }, 200);
            } else {
                favBtn.classList.remove('favorited');
                favBtn.title = '收藏';
                favBtn.querySelector('svg').setAttribute('fill', 'none');
                // 在"我的收藏"列表里取消收藏 → 移除卡片
                if (card.dataset.inFavorites === '1') {
                    card.style.transition = 'all 0.3s ease';
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.8)';
                    setTimeout(() => card.remove(), 300);
                    // 更新空状态
                    setTimeout(() => {
                        const list = document.getElementById('voiceList');
                        if (!list.children.length) renderEmptyFavorites();
                    }, 320);
                }
            }
        } catch (err) {
            alert('收藏操作失败: ' + err.message);
        } finally {
            favBtn.disabled = false;
        }
    });

    // 试听按钮
    previewBtn.addEventListener('click', async (ev) => {
        ev.stopPropagation();

        // 当前正在播放这张卡片 → 停止
        if (previewAudio && !previewAudio.paused && previewAudio._btn === previewBtn) {
            previewAudio.pause();
            showIcon(iconPlay, iconLoading, iconStop);
            previewAudio = null;
            return;
        }

        // 停止其他卡片
        if (previewAudio) {
            const prevBtn = previewAudio._btn;
            previewAudio.pause();
            previewAudio = null;
            if (prevBtn) {
                prevBtn.classList.remove('playing', 'loading');
                const p = prevBtn.querySelector('.icon-play');
                const l = prevBtn.querySelector('.icon-loading');
                const s = prevBtn.querySelector('.icon-stop');
                if (p && l && s) { p.style.display = ''; l.style.display = 'none'; l.style.animation = ''; s.style.display = 'none'; }
            }
        }

        // 如果该音色支持 instruct 且当前有指令,先校验指令是否合法
        if (voice.instruct && currentInstruction) {
            const { ok, errors } = validateInstruction(currentInstruction);
            if (!ok) {
                alert('Instruct 指令校验失败,请修正后再试听:\n\n• ' + errors.join('\n• '));
                return;
            }
        }

        // loading
        previewBtn.classList.add('loading');
        iconPlay.style.display = 'none';
        iconLoading.style.display = '';
        iconStop.style.display = 'none';
        iconLoading.style.animation = 'spin 0.8s linear infinite';

        try {
            const resp = await fetch('/api/synthesize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    model: voice._model,
                    voice: voice.id,
                    text: '你好，这是音色试听，希望你喜欢。',
                    instruction: voice.instruct ? (currentInstruction || '') : '',
                }),
            });
            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.error || '合成失败');
            }
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);

            previewBtn.classList.remove('loading');
            previewBtn.classList.add('playing');
            iconPlay.style.display = 'none';
            iconLoading.style.display = 'none';
            iconStop.style.display = '';

            const audio = new Audio(url);
            audio._btn = previewBtn;
            previewAudio = audio;
            audio.addEventListener('ended', () => {
                previewBtn.classList.remove('playing');
                showIcon(iconPlay, iconLoading, iconStop);
                previewAudio = null;
                URL.revokeObjectURL(url);
            });
            audio.addEventListener('error', () => {
                previewBtn.classList.remove('playing');
                showIcon(iconPlay, iconLoading, iconStop);
                previewAudio = null;
            });
            audio.play();
        } catch (err) {
            previewBtn.classList.remove('loading');
            showIcon(iconPlay, iconLoading, iconStop);
            alert('试听失败: ' + err.message);
        }
    });

    // 点击卡片其他区域 = 选中
    card.addEventListener('click', () => {
        document.querySelectorAll('.voice-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedVoice = voice.id;
        // 选中时同步所属模型(收藏列表里的音色可能来自不同模型)
        if (voice._model && voice._model !== '__favorites__') {
            selectedVoiceModel = voice._model;
        }
        // 显示/隐藏 Instruct 面板
        updateInstructPanel(voice);
    });

    return card;
}

function showIcon(play, loading, stop) {
    if (play) play.style.display = '';
    if (loading) { loading.style.display = 'none'; loading.style.animation = ''; }
    if (stop) stop.style.display = 'none';
}

// 渲染空收藏状态
function renderEmptyFavorites() {
    const voiceList = document.getElementById('voiceList');
    voiceList.innerHTML = `
        <div class="empty-favorites">
            <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
            </svg>
            <p class="empty-title">还没有收藏的音色</p>
            <p class="empty-hint">在音色卡片上点击星标按钮即可收藏</p>
        </div>
    `;
}

// 记录用户当前选中的模型(用于在收藏列表中选中卡片时同步)
let selectedVoiceModel = null;

document.getElementById('modelSelect').addEventListener('change', e => {
    const model = e.target.value;
    const voiceList = document.getElementById('voiceList');
    voiceList.innerHTML = '';
    selectedVoice = null;
    selectedVoiceModel = null;
    // 切换模型时隐藏 Instruct 面板
    updateInstructPanel(null);

    if (!model) return;

    if (model === '__favorites__') {
        // 渲染收藏列表(从内存缓存读)
        const favList = Object.values(favoritesCache);
        if (!favList.length) {
            renderEmptyFavorites();
            return;
        }
        favList.forEach((v, idx) => {
            // 归一化字段:_model、_showModel
            const voice = { ...v, _model: v.model, _showModel: true };
            const card = renderVoiceCard(voice, idx);
            card.dataset.inFavorites = '1';
            voiceList.appendChild(card);
            bounceIn(card, idx * 30);
        });
        return;
    }

    if (!voicesData[model]) return;

    voicesData[model].voices.forEach((v, idx) => {
        const voice = { ...v, _model: model, _showModel: false };
        const card = renderVoiceCard(voice, idx);
        voiceList.appendChild(card);
        bounceIn(card, idx * 30);
    });
});

document.getElementById('backToStep2Btn').addEventListener('click', () => goToStep(2));

document.getElementById('synthesizeBtn').addEventListener('click', async () => {
    // 优先用音色所属模型(收藏列表里的音色可能来自不同模型);否则用下拉选中模型
    const model = selectedVoiceModel || document.getElementById('modelSelect').value;

    if (!model || model === '__favorites__') return alert('请选择模型');
    if (!selectedVoice) return alert('请选择音色');
    if (!subtitles.length) return alert('没有字幕数据');

    // 如果选中音色支持 instruct 且当前有指令,先校验
    if (selectedVoiceObj && selectedVoiceObj.instruct && currentInstruction) {
        const { ok, errors } = validateInstruction(currentInstruction);
        if (!ok) {
            alert('Instruct 指令校验失败,请修正后再合成:\n\n• ' + errors.join('\n• '));
            return;
        }
    }

    const synthStatus = document.getElementById('synthStatus');
    const audioResult = document.getElementById('audioResult');
    const synthBtn = document.getElementById('synthesizeBtn');
    const timelineProgress = document.getElementById('timelineProgress');
    const statusTextEl = document.getElementById('synthStatusText');
    const progressFill = document.getElementById('timelineProgressFill');
    const currentEl = document.getElementById('timelineCurrent');
    const totalEl = document.getElementById('timelineTotal');
    const timelineStatusEl = document.getElementById('timelineStatusText');

    synthStatus.classList.remove('hidden');
    timelineProgress.classList.remove('hidden');
    audioResult.classList.add('hidden');
    statusTextEl.textContent = '正在启动时间轴合成...';
    synthBtn.disabled = true;

    try {
        const segments = subtitles.map(s => ({
            begin_time: s.begin_time,
            end_time: s.end_time,
            text: s.text,
        }));

        const resp = await fetch('/api/synthesize-timeline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model,
                voice: selectedVoice,
                segments,
                instruction: (selectedVoiceObj && selectedVoiceObj.instruct) ? currentInstruction : '',
            }),
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.error || 'Failed to start synthesis');
        }

        const { job_id } = await resp.json();

        // Poll for progress
        const audioUrl = await new Promise((resolve, reject) => {
            const pollInterval = setInterval(async () => {
                try {
                    const statusResp = await fetch(`/api/synthesize-status/${job_id}`);
                    const status = await statusResp.json();

                    if (status.error) {
                        clearInterval(pollInterval);
                        reject(new Error(status.error));
                        return;
                    }

                    currentEl.textContent = status.current;
                    totalEl.textContent = status.total;
                    timelineStatusEl.textContent = status.status_text || '';

                    if (status.total > 0) {
                        const pct = Math.round((status.completed / status.total) * 100);
                        progressFill.style.width = pct + '%';
                    }

                    if (status.status === 'done') {
                        clearInterval(pollInterval);
                        resolve(`/api/synthesize-download/${job_id}`);
                    } else if (status.status === 'error') {
                        clearInterval(pollInterval);
                        reject(new Error(status.status_text || 'Synthesis failed'));
                    }
                } catch (err) {
                    clearInterval(pollInterval);
                    reject(err);
                }
            }, 1000);
        });

        statusTextEl.textContent = '合成完成！';
        progressFill.style.width = '100%';

        document.getElementById('audioPlayer').src = audioUrl;
        document.getElementById('downloadAudioBtn').href = audioUrl;
        audioResult.classList.remove('hidden');
    } catch (err) {
        alert('合成失败: ' + err.message);
    } finally {
        synthStatus.classList.add('hidden');
        timelineProgress.classList.add('hidden');
        synthBtn.disabled = false;
    }
});

// --- Clickable Step Navigation ---
document.querySelectorAll('.step-dot').forEach(dot => {
    dot.style.cursor = 'pointer';
    dot.addEventListener('click', () => {
        const step = parseInt(dot.dataset.step);
        if (step === 3 && !subtitles.length) {
            alert('请先上传或生成字幕');
            return;
        }
        if (step === 3) {
            const synthInfo = document.getElementById('synthInfo');
            const countEl = document.getElementById('synthInfoCount');
            const totalSec = Math.max(...subtitles.map(s => s.end_time)) / 1000;
            const mins = Math.floor(totalSec / 60);
            const secs = Math.floor(totalSec % 60);
            countEl.textContent = `共 ${subtitles.length} 条字幕，总时长 ${mins}分${secs}秒`;
            synthInfo.classList.remove('hidden');
        }
        goToStep(step);
    });
});

// Init — start from Step 2 so user can upload SRT directly
goToStep(2);
loadVoices();
applyBounceToButtons();
initInstructPanel();
// 预加载收藏数据(后端持久化),不阻塞页面渲染
loadFavoritesFromServer();
