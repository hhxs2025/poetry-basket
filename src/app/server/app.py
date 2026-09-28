#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诗篓 —— 私人诗词工作台
版本: 1.2.0
- 所有用户可见全部诗稿，仅录入用户（或管理员）可编辑/删除
- 显示名称（display_name）支持中文
- 作品类型：收录 / 原创
- 正文支持 HTML 排版，图片以 <img> 语法插入正文
- 导出时自动剥离 HTML 标签，输出纯文本
"""

import sys
import os
import json
import time
import random
import string
import sqlite3
import re
from html.parser import HTMLParser
from datetime import datetime
from functools import wraps

VENDOR_DIR = os.path.join(os.path.dirname(__file__), 'vendor')
if os.path.exists(VENDOR_DIR):
    sys.path.insert(0, VENDOR_DIR)

from flask import Flask, g, request, jsonify, session, redirect, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='../ui', static_url_path='/static')
app.config['SECRET_KEY'] = 'poetry-basket-local-only-2026'
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

TRIM_PKGVAR = os.environ.get('TRIM_PKGVAR', '/var/data')
TRIM_SERVICE_PORT = int(os.environ.get('TRIM_SERVICE_PORT', 5678))

USER_DB = os.path.join(TRIM_PKGVAR, 'poetry.db')
USER_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'users.json')
os.makedirs(TRIM_PKGVAR, exist_ok=True)
os.makedirs(os.path.dirname(USER_FILE), exist_ok=True)

# ============================================================
# 图片存储目录（从 TRIM_APPDEST 软链接反推存储池，不写死路径）
# ============================================================
def find_upload_dir():
    # 1. 优先使用系统注入的共享目录变量
    share_path = os.environ.get('TRIM_DATA_SHARE_PATH', '')
    if share_path:
        first = share_path.split(':')[0]
        if os.path.isdir(first):
            return os.path.join(first, 'poetry-basket', 'uploads')

    # 2. 从 TRIM_APPDEST 软链接反推存储池根目录
    #    /var/apps/poetry-basket/target -> /vol1/@appcenter/poetry-basket
    appdest = os.environ.get('TRIM_APPDEST', '')
    if appdest:
        real = os.path.realpath(appdest)
        parts = real.split('/')
        # parts = ['', 'vol1', '@appcenter', 'poetry-basket']
        if len(parts) >= 3:
            vol_root = '/' + parts[1]
            return os.path.join(vol_root, '@appshare', 'poetry-basket', 'uploads')

    # 3. 最终回退到应用数据目录
    return os.path.join(TRIM_PKGVAR, 'uploads')

UPLOAD_DIR = find_upload_dir()
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_filename(ext):
    ts = int(time.time())
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}_{rand}.{ext}"

# ============================================================
# HTML → 纯文本
# ============================================================
class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {'div', 'p', 'blockquote', 'li', 'tr',
                  'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
    BREAK_TAGS = {'br', 'hr'}

    def __init__(self):
        super().__init__()
        self.result = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAK_TAGS:
            self.result.append('\n')
        elif tag in self.BLOCK_TAGS:
            if self.result and not self.result[-1].endswith('\n'):
                self.result.append('\n')

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            if self.result and not self.result[-1].endswith('\n'):
                self.result.append('\n')

    def handle_data(self, data):
        self.result.append(data)

def html_to_text(html):
    """将 HTML 内容转为纯文本，保留段落/换行结构"""
    if not html:
        return ''
    if '<' not in html:
        return html.strip()
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html.strip()
    text = ''.join(parser.result)
    # 合并 3 个以上连续换行为 2 个
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 去掉每行尾部空格
    lines = [line.rstrip() for line in text.split('\n')]
    return '\n'.join(lines).strip()

# ============================================================
# 用户管理
# ============================================================
def load_users():
    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, 'r', encoding='utf-8') as f:
                users = json.load(f)
                if users:
                    for name, info in users.items():
                        if 'display_name' not in info:
                            info['display_name'] = name
                    return users
        except:
            pass
    return {'admin': {'password': 'admin123', 'admin': True, 'display_name': '管理员'}}

def save_users(users):
    with open(USER_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

USERS = load_users()

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' in session:
            return f(*args, **kwargs)
        if request.path.startswith('/api/'):
            return jsonify({'code': 401, 'msg': '请先登录'}), 401
        return redirect('/login')
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'code': 401, 'msg': '请先登录'}), 401
        users = load_users()
        if not users.get(session['user'], {}).get('admin', False):
            return jsonify({'code': 403, 'msg': '权限不足'}), 403
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    return session.get('user', '')

def is_admin_user():
    users = load_users()
    return users.get(get_current_user(), {}).get('admin', False)

def can_edit_poem(poem_id):
    current_user = get_current_user()
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT user_id FROM poems WHERE id = ?', (poem_id,))
    row = cursor.fetchone()
    if not row:
        return False, '未找到'
    if row['user_id'] == current_user or is_admin_user():
        return True, ''
    return False, '只能操作自己录入的诗'

def attach_display_name(poems_or_poem):
    users = load_users()
    if isinstance(poems_or_poem, list):
        for p in poems_or_poem:
            p['display_name'] = users.get(p.get('user_id', ''), {}).get('display_name', p.get('user_id', '未知'))
        return poems_or_poem
    else:
        p = poems_or_poem
        p['display_name'] = users.get(p.get('user_id', ''), {}).get('display_name', p.get('user_id', '未知'))
        return p

# ============================================================
# 平水韵数据
# ============================================================
_RHYME_TONES = {
    '一东': ('平', '东同风中空红翁蓬童聪虫弓公功宫攻蒙笼聋通'),
    '二冬': ('平', '冬农钟龙松峰蓉封胸凶雍浓重从逢缝'),
    '三江': ('平', '江窗双邦杠庞撞缸'),
    '四支': ('平', '支枝移垂吹奇时诗旗知池师姿迟之芝尸施持规皮披为随宜思仪义'),
    '五微': ('平', '微归飞衣稀菲非机挥晖韦违威'),
    '六鱼': ('平', '鱼居书余疏初虚庐驴除如茹徐渔'),
    '七虞': ('平', '虞湖珠孤无呼徒途涂奴儒夫符须枯乌都'),
    '八齐': ('平', '齐西低啼鸡栖溪迷泥圭题'),
    '九佳': ('平', '佳街柴崖涯阶皆乖怀排埋'),
    '十灰': ('平', '灰来开台才哀该苔栽灾猜孩财'),
    '十一真': ('平', '真人春尘新亲臣邻贫民频津身陈'),
    '十二文': ('平', '文云君勤军闻分纷群欣'),
    '十三元': ('平', '元言门存昏魂尊村孙论盆恩'),
    '十四寒': ('平', '寒看安残丹兰欢盘难端栏干'),
    '十五删': ('平', '删山间颜闲班弯关还'),
    '一先': ('平', '先前年天烟边田连篇千泉仙然悬'),
    '二萧': ('平', '萧遥桥潮朝消宵招寥条苗'),
    '三肴': ('平', '肴巢郊交包茅梢嘲'),
    '四豪': ('平', '豪高劳涛曹刀操袍糟'),
    '五歌': ('平', '歌河波多罗和科柯娥'),
    '六麻': ('平', '麻花家斜华沙蛇霞涯夸'),
    '七阳': ('平', '阳长光香乡霜凉芳堂强当量王'),
    '八庚': ('平', '庚生声情明清平争鸣行衡'),
    '九青': ('平', '青星亭灵经形刑丁瓶'),
    '十蒸': ('平', '蒸登灯朋冰兴升腾'),
    '十一尤': ('平', '尤秋流楼愁舟游头休求留油浮'),
    '十二侵': ('平', '侵心深林音吟寻琴金'),
    '十三覃': ('平', '覃南潭谈甘含蓝蚕'),
    '十四盐': ('平', '盐帘檐纤尖严占廉'),
    '十五咸': ('平', '咸岩衫帆凡馋衔'),
    '一董': ('仄', '董孔桶笼总'),
    '二肿': ('仄', '肿种踵宠陇'),
    '三讲': ('仄', '讲港棒蚌'),
    '四纸': ('仄', '纸水指几美否鄙喜'),
    '五尾': ('仄', '尾鬼苇伟'),
    '六语': ('仄', '语雨旅汝许吕阻楚'),
    '七麌': ('仄', '麌土舞虎古补母鲁'),
    '八荠': ('仄', '荠礼体启洗'),
    '九蟹': ('仄', '蟹解买楷'),
    '十贿': ('仄', '贿改采海载'),
    '十一轸': ('仄', '轸忍尽引紧'),
    '十二吻': ('仄', '吻粉愤隐'),
    '十三阮': ('仄', '阮本远晚返'),
    '十四旱': ('仄', '旱满管短断'),
    '十五潸': ('仄', '潸眼版简'),
    '十六铣': ('仄', '铣浅展善选'),
    '十七筱': ('仄', '筱小老绕晓'),
    '十八巧': ('仄', '巧爪饱搅'),
    '十九皓': ('仄', '皓老早草宝'),
    '二十哿': ('仄', '哿我火朵'),
    '二十一马': ('仄', '马社写也雅'),
    '二十二养': ('仄', '养象想赏往'),
    '二十三梗': ('仄', '梗影永猛饼'),
    '二十四迥': ('仄', '迥顶艇醒'),
    '二十五有': ('仄', '有手酒首口柳'),
    '二十六寝': ('仄', '寝锦品枕'),
    '二十七感': ('仄', '感览胆惨'),
    '二十八俭': ('仄', '俭染闪险'),
    '二十九豏': ('仄', '豏减槛范'),
    '一送': ('仄', '送弄梦凤众'),
    '二宋': ('仄', '宋用统颂'),
    '三绛': ('仄', '绛巷降撞'),
    '四寘': ('仄', '寘志意吏利义器位'),
    '五未': ('仄', '未贵味畏蔚'),
    '六御': ('仄', '御虑据誉'),
    '七遇': ('仄', '遇步路露固故墓'),
    '八霁': ('仄', '霁世计济替细'),
    '九泰': ('仄', '泰外带大会'),
    '十卦': ('仄', '卦画挂卖派'),
    '十一队': ('仄', '队内对废爱'),
    '十二震': ('仄', '震信进镇'),
    '十三问': ('仄', '问运郡训'),
    '十四愿': ('仄', '愿寸论恨'),
    '十五翰': ('仄', '翰岸汉半乱'),
    '十六谏': ('仄', '谏雁患慢'),
    '十七霰': ('仄', '霰见面变电'),
    '十八啸': ('仄', '啸照笑妙召'),
    '十九效': ('仄', '效教貌孝'),
    '二十号': ('仄', '号到报噪'),
    '二十一个': ('仄', '个贺作佐'),
    '二十二禡': ('仄', '禡谢化骂驾'),
    '二十三漾': ('仄', '漾相向望浪'),
    '二十四敬': ('仄', '敬庆命令政'),
    '二十五径': ('仄', '径定听胜'),
    '二十六宥': ('仄', '宥旧秀昼救'),
    '二十七沁': ('仄', '沁禁任荫'),
    '二十八勘': ('仄', '勘暗憾暂'),
    '二十九艳': ('仄', '艳剑念赡'),
    '三十陷': ('仄', '陷泛鉴梵'),
    '一屋': ('仄', '屋竹哭独读福服鹿'),
    '二沃': ('仄', '沃足促俗续玉'),
    '三觉': ('仄', '觉角捉剥'),
    '四质': ('仄', '质日实一吉七'),
    '五物': ('仄', '物佛屈乞'),
    '六月': ('仄', '月发骨忽伐竭'),
    '七曷': ('仄', '曷达末夺脱'),
    '八黠': ('仄', '黠札拔八'),
    '九屑': ('仄', '屑节绝列热别'),
    '十药': ('仄', '药落阁鹤薄'),
    '十一陌': ('仄', '陌客白百伯'),
    '十二锡': ('仄', '锡敌笛激'),
    '十三职': ('仄', '职力德息直'),
    '十四缉': ('仄', '缉急湿习十'),
    '十五合': ('仄', '合答榻杂'),
    '十六叶': ('仄', '叶接蝶捷'),
    '十七洽': ('仄', '洽法甲鸭'),
}

PINGSHUI_DATA = {}
for rhyme, (tone, chars) in _RHYME_TONES.items():
    for ch in chars:
        if ch not in PINGSHUI_DATA:
            PINGSHUI_DATA[ch] = {'tone': tone, 'rhyme': rhyme}

def lookup_char(ch):
    info = PINGSHUI_DATA.get(ch)
    if info:
        return info['tone'], info['rhyme']
    return None, None

# ============================================================
# 数据库
# ============================================================
def init_db():
    conn = sqlite3.connect(USER_DB)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS poems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            dynasty TEXT,
            genre TEXT DEFAULT '诗',
            content TEXT NOT NULL,
            annotation TEXT,
            appreciation TEXT,
            tags TEXT,
            user_id TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute("PRAGMA table_info(poems)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'user_id' not in columns:
        cursor.execute("ALTER TABLE poems ADD COLUMN user_id TEXT DEFAULT 'admin'")
        cursor.execute("UPDATE poems SET user_id = 'admin' WHERE user_id IS NULL")

    if 'type' not in columns:
        cursor.execute("ALTER TABLE poems ADD COLUMN type TEXT DEFAULT '收录'")
        cursor.execute("UPDATE poems SET type = '收录' WHERE type IS NULL")

    conn.commit()
    conn.close()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(USER_DB)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

init_db()

# ============================================================
# 格律校验
# ============================================================
def analyze_rhythm_pingshui(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return {'error': '请至少输入一句诗'}

    output = []
    first_rhyme = None
    rhyme_errors = []

    for idx, line in enumerate(lines, 1):
        chars = list(line)
        char_info = []
        for ch in chars:
            tone, rhyme = lookup_char(ch)
            char_info.append({
                'char': ch,
                'tone': tone if tone else '？',
                'rhyme': rhyme if rhyme else '未知',
                'known': tone is not None
            })

        last = char_info[-1]
        last_rhyme = last['rhyme']

        if idx == 1:
            first_rhyme = last_rhyme

        if idx % 2 == 0:
            if last_rhyme != first_rhyme or last['tone'] != '平':
                rhyme_errors.append(idx)

        output.append({
            'line': line,
            'length': len(chars),
            'chars': char_info,
            'last_char': chars[-1],
            'last_tone': last['tone'],
            'last_rhyme': last_rhyme,
            'is_even': (idx % 2 == 0)
        })

    rhyme_ok = (len(rhyme_errors) == 0 and len(lines) >= 2 and first_rhyme is not None)
    if rhyme_ok:
        conclusion = f'押韵正确：偶句均押「{first_rhyme}」部'
    else:
        if len(lines) < 2:
            conclusion = '至少两句才能判断押韵'
        else:
            error_pos = '、'.join(map(str, rhyme_errors))
            conclusion = f'押韵有误：第 {error_pos} 句未押「{first_rhyme}」部'

    return {
        'lines': output,
        'conclusion': conclusion,
        'rhyme_ok': rhyme_ok,
        'first_rhyme': first_rhyme,
        'errors': rhyme_errors
    }

# ============================================================
# 认证路由
# ============================================================
@app.route('/login')
def login_page():
    return app.send_static_file('login.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    users = load_users()
    if username in users and users[username].get('password') == password:
        session['user'] = username
        return jsonify({'code': 0, 'msg': '登录成功'})
    return jsonify({'code': 401, 'msg': '用户名或密码错误'}), 401

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'code': 0, 'msg': '已退出'})

@app.route('/api/user')
def api_user():
    if 'user' in session:
        users = load_users()
        username = session['user']
        return jsonify({
            'code': 0,
            'data': {
                'username': username,
                'display_name': users.get(username, {}).get('display_name', username),
                'admin': users.get(username, {}).get('admin', False)
            }
        })
    return jsonify({'code': 401, 'msg': '未登录'}), 401

# ============================================================
# 图片上传 API
# ============================================================
@app.route('/api/upload', methods=['POST'])
@auth_required
def api_upload():
    if 'file' not in request.files:
        return jsonify({'code': 400, 'msg': '未选择文件'})

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({'code': 400, 'msg': '未选择文件'})

    if not allowed_file(file.filename):
        return jsonify({'code': 400, 'msg': '仅支持 jpg / png / webp / gif 格式'})

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_IMAGE_SIZE:
        return jsonify({'code': 400, 'msg': '图片大小不能超过 5MB'})

    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext == 'jpeg':
        ext = 'jpg'
    filename = generate_filename(ext)

    try:
        file.save(os.path.join(UPLOAD_DIR, filename))
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'保存失败：{str(e)}'})

    return jsonify({
        'code': 0,
        'msg': '上传成功',
        'filename': filename
    })

@app.route('/api/upload/<filename>', methods=['DELETE'])
@auth_required
def api_delete_upload(filename):
    filename = secure_filename(filename)
    if not filename:
        return jsonify({'code': 400, 'msg': '文件名无效'})

    filepath = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            return jsonify({'code': 500, 'msg': f'删除失败：{str(e)}'})
    return jsonify({'code': 0, 'msg': '删除成功'})

@app.route('/uploads/<filename>')
@auth_required
def serve_upload(filename):
    filename = secure_filename(filename)
    return send_from_directory(UPLOAD_DIR, filename)

# ============================================================
# 管理员修改密码
# ============================================================
@app.route('/api/admin/change_password', methods=['POST'])
@auth_required
def api_change_password():
    data = request.json
    old_password = data.get('old_password', '').strip()
    new_password = data.get('new_password', '').strip()
    current_user = session.get('user', '')

    if not current_user:
        return jsonify({'code': 401, 'msg': '请先登录'}), 401

    if not old_password or not new_password:
        return jsonify({'code': 400, 'msg': '请完整填写所有字段'})

    if len(new_password) < 4:
        return jsonify({'code': 400, 'msg': '新密码至少4位'})

    users = load_users()
    if current_user not in users:
        return jsonify({'code': 404, 'msg': '用户不存在'})

    if users[current_user]['password'] != old_password:
        return jsonify({'code': 400, 'msg': '当前密码错误'})

    users[current_user]['password'] = new_password
    save_users(users)
    return jsonify({'code': 0, 'msg': '密码修改成功'})

# ============================================================
# 用户管理 API（管理员）
# ============================================================
@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_get_users():
    users = load_users()
    data = []
    for name, info in users.items():
        data.append({
            'username': name,
            'display_name': info.get('display_name', name),
            'admin': info.get('admin', False),
            'has_password': bool(info.get('password'))
        })
    return jsonify({'code': 0, 'data': data})

@app.route('/api/admin/users', methods=['POST'])
@admin_required
def admin_add_user():
    data = request.json
    display_name = data.get('display_name', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    is_admin = data.get('admin', False)

    if not display_name or not username or not password:
        return jsonify({'code': 400, 'msg': '请完整填写所有字段'})
    if len(username) < 2 or len(password) < 4:
        return jsonify({'code': 400, 'msg': '账号至少2位，密码至少4位'})

    users = load_users()
    if username in users:
        return jsonify({'code': 400, 'msg': '账号已存在'})

    users[username] = {
        'password': password,
        'admin': bool(is_admin),
        'display_name': display_name
    }
    save_users(users)
    return jsonify({'code': 0, 'msg': '添加成功'})

@app.route('/api/admin/users/<username>', methods=['DELETE'])
@admin_required
def admin_delete_user(username):
    if username == 'admin':
        return jsonify({'code': 400, 'msg': '不能删除默认管理员'})
    users = load_users()
    if username not in users:
        return jsonify({'code': 404, 'msg': '用户不存在'})
    if username == session['user']:
        return jsonify({'code': 400, 'msg': '不能删除自己'})
    del users[username]
    save_users(users)
    return jsonify({'code': 0, 'msg': '删除成功'})

@app.route('/api/admin/users/<username>', methods=['PUT'])
@admin_required
def admin_update_user(username):
    data = request.json
    users = load_users()
    if username not in users:
        return jsonify({'code': 404, 'msg': '用户不存在'})

    password = data.get('password', '').strip()
    if password:
        if len(password) < 4:
            return jsonify({'code': 400, 'msg': '密码至少4位'})
        users[username]['password'] = password

    display_name = data.get('display_name', '').strip()
    if display_name:
        users[username]['display_name'] = display_name

    if 'admin' in data and username != 'admin':
        users[username]['admin'] = bool(data['admin'])

    save_users(users)
    return jsonify({'code': 0, 'msg': '更新成功'})

@app.route('/api/admin/users/<username>/reset_password', methods=['POST'])
@admin_required
def admin_reset_password(username):
    data = request.json
    new_password = data.get('password', '').strip()

    if username == 'admin':
        return jsonify({'code': 400, 'msg': '请使用"修改密码"功能修改管理员密码'})

    if not new_password or len(new_password) < 4:
        return jsonify({'code': 400, 'msg': '密码至少4位'})

    users = load_users()
    if username not in users:
        return jsonify({'code': 404, 'msg': '用户不存在'})

    users[username]['password'] = new_password
    save_users(users)
    return jsonify({'code': 0, 'msg': '重置成功'})

@app.route('/admin')
@auth_required
def admin_page():
    users = load_users()
    if not users.get(session.get('user', ''), {}).get('admin', False):
        return '权限不足', 403
    return app.send_static_file('admin.html')

# ============================================================
# 诗词 API
# ============================================================

@app.route('/api/poems')
@auth_required
def api_poems():
    keyword = request.args.get('keyword', '').strip()
    tag = request.args.get('tag', '').strip()
    author = request.args.get('author', '').strip()
    dynasty = request.args.get('dynasty', '').strip()
    poem_type = request.args.get('type', '').strip()

    db = get_db()
    cursor = db.cursor()

    params = []
    sql = 'SELECT id, title, author, dynasty, genre, tags, type, user_id, created_at FROM poems WHERE 1=1'

    if keyword:
        sql += ' AND (title LIKE ? OR author LIKE ? OR content LIKE ? OR tags LIKE ?)'
        params.extend([f'%{keyword}%'] * 4)
    if tag:
        sql += ' AND tags LIKE ?'
        params.append(f'%{tag}%')
    if author:
        sql += ' AND author LIKE ?'
        params.append(f'%{author}%')
    if dynasty:
        sql += ' AND dynasty LIKE ?'
        params.append(f'%{dynasty}%')
    if poem_type:
        sql += ' AND type = ?'
        params.append(poem_type)

    sql += ' ORDER BY created_at DESC'
    cursor.execute(sql, params)
    poems = [dict(row) for row in cursor.fetchall()]

    attach_display_name(poems)
    return jsonify({'code': 0, 'data': poems})

@app.route('/api/poem/<int:poem_id>')
@auth_required
def api_poem_detail(poem_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM poems WHERE id = ?', (poem_id,))
    poem = cursor.fetchone()
    if not poem:
        return jsonify({'code': 404, 'msg': '未找到'})
    poem_data = dict(poem)
    attach_display_name(poem_data)
    return jsonify({'code': 0, 'data': poem_data})

@app.route('/api/poem', methods=['POST'])
@auth_required
def api_create_poem():
    data = request.json
    title = data.get('title', '').strip()
    author = data.get('author', '').strip()
    dynasty = data.get('dynasty', '').strip()
    genre = data.get('genre', '诗').strip()
    content = data.get('content', '').strip()
    annotation = data.get('annotation', '').strip()
    appreciation = data.get('appreciation', '').strip()
    tags = data.get('tags', '').strip()
    poem_type = data.get('type', '收录').strip()
    current_user = get_current_user()

    if not title or not author or not content:
        return jsonify({'code': 400, 'msg': '标题、作者、正文为必填项'})

    if poem_type not in ('收录', '原创'):
        poem_type = '收录'

    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO poems (title, author, dynasty, genre, content, annotation, appreciation, tags, type, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (title, author, dynasty, genre, content, annotation, appreciation, tags, poem_type, current_user))
    db.commit()
    return jsonify({'code': 0, 'msg': '添加成功', 'id': cursor.lastrowid})

@app.route('/api/poem/<int:poem_id>', methods=['PUT'])
@auth_required
def api_update_poem(poem_id):
    data = request.json
    title = data.get('title', '').strip()
    author = data.get('author', '').strip()
    dynasty = data.get('dynasty', '').strip()
    genre = data.get('genre', '诗').strip()
    content = data.get('content', '').strip()
    annotation = data.get('annotation', '').strip()
    appreciation = data.get('appreciation', '').strip()
    tags = data.get('tags', '').strip()
    poem_type = data.get('type', '收录').strip()

    if not title or not author or not content:
        return jsonify({'code': 400, 'msg': '标题、作者、正文为必填项'})

    allowed, msg = can_edit_poem(poem_id)
    if not allowed:
        return jsonify({'code': 403, 'msg': msg}), 403

    if poem_type not in ('收录', '原创'):
        poem_type = '收录'

    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        UPDATE poems SET
            title = ?, author = ?, dynasty = ?, genre = ?,
            content = ?, annotation = ?, appreciation = ?, tags = ?,
            type = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (title, author, dynasty, genre, content, annotation, appreciation, tags, poem_type, poem_id))
    db.commit()
    return jsonify({'code': 0, 'msg': '更新成功'})

@app.route('/api/poem/<int:poem_id>', methods=['DELETE'])
@auth_required
def api_delete_poem(poem_id):
    allowed, msg = can_edit_poem(poem_id)
    if not allowed:
        return jsonify({'code': 403, 'msg': msg}), 403

    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM poems WHERE id = ?', (poem_id,))
    db.commit()
    return jsonify({'code': 0, 'msg': '删除成功'})

@app.route('/api/tags')
@auth_required
def api_tags():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT DISTINCT tags FROM poems WHERE tags != ""')
    rows = cursor.fetchall()
    tags_set = set()
    for row in rows:
        for t in row['tags'].split(','):
            t = t.strip()
            if t:
                tags_set.add(t)
    return jsonify({'code': 0, 'data': sorted(tags_set)})

@app.route('/api/stats')
@auth_required
def api_stats():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT COUNT(*) as total FROM poems')
    total = cursor.fetchone()['total']
    cursor.execute('SELECT COUNT(DISTINCT author) as authors FROM poems')
    authors = cursor.fetchone()['authors']
    cursor.execute('SELECT COUNT(DISTINCT tags) as tags FROM poems WHERE tags != ""')
    tags = cursor.fetchone()['tags']
    return jsonify({'code': 0, 'data': {'total': total, 'authors': authors, 'tags': tags}})

@app.route('/api/export/<int:poem_id>')
@auth_required
def api_export_poem(poem_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM poems WHERE id = ?', (poem_id,))
    poem = cursor.fetchone()
    if not poem:
        return jsonify({'code': 404, 'msg': '未找到'})

    users = load_users()
    display_name = users.get(poem['user_id'], {}).get('display_name', poem['user_id'])

    content_text = html_to_text(poem['content'])
    annotation_text = html_to_text(poem['annotation'] or '')
    appreciation_text = html_to_text(poem['appreciation'] or '')

    content = f"""
========================================
{poem['title']}
{poem['author']}  {poem['dynasty']}  【{poem['genre']}】
========================================

{content_text}

{annotation_text if annotation_text else ''}

{appreciation_text if appreciation_text else ''}

类型：{poem['type'] if poem['type'] else '收录'}
主题：{poem['tags'] if poem['tags'] else '无'}
录入人：{display_name}
录入时间：{poem['created_at']}
========================================
"""
    return jsonify({'code': 0, 'data': {'title': poem['title'], 'content': content}})

@app.route('/api/export/batch', methods=['POST'])
@auth_required
def api_export_batch():
    data = request.json
    poem_ids = data.get('ids', [])
    if not poem_ids:
        return jsonify({'code': 400, 'msg': '请选择要导出的诗词'})

    db = get_db()
    cursor = db.cursor()
    placeholders = ','.join(['?'] * len(poem_ids))
    cursor.execute(f'SELECT * FROM poems WHERE id IN ({placeholders}) ORDER BY created_at DESC', poem_ids)
    poems = cursor.fetchall()

    if not poems:
        return jsonify({'code': 404, 'msg': '未找到任何诗词'})

    users = load_users()

    lines = []
    lines.append('诗篓·诗稿')
    lines.append('')
    lines.append(f'共 {len(poems)} 首  导出时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append('')
    lines.append('---')
    lines.append('')

    for p in poems:
        display_name = users.get(p['user_id'], {}).get('display_name', p['user_id'])
        content_text = html_to_text(p['content'])
        annotation_text = html_to_text(p['annotation'] or '')
        appreciation_text = html_to_text(p['appreciation'] or '')

        lines.append(f'《{p["title"]}》')
        lines.append('')
        lines.append(f'作者：{p["author"]}    朝代：{p["dynasty"] or "—"}    体裁：{p["genre"] or "诗"}    类型：{p["type"] or "收录"}')
        lines.append(f'录入人：{display_name}')
        lines.append('')
        lines.append(content_text)
        lines.append('')

        if annotation_text:
            lines.append('笺注：')
            lines.append(annotation_text)
            lines.append('')

        if appreciation_text:
            lines.append('赏析：')
            lines.append(appreciation_text)
            lines.append('')

        if p['tags'] and p['tags'].strip():
            lines.append(f'主题：{p["tags"]}')
            lines.append('')

        lines.append('---')
        lines.append('')

    markdown = '\n'.join(lines)
    return jsonify({'code': 0, 'data': {'markdown': markdown, 'count': len(poems)}})

@app.route('/api/check', methods=['POST'])
@auth_required
def api_check():
    text = request.json.get('text', '').strip()
    if not text:
        return jsonify({'code': 400, 'msg': '请输入诗句'})
    text = html_to_text(text)
    result = analyze_rhythm_pingshui(text)
    return jsonify({'code': 0, 'data': result})

# ============================================================
# 页面路由
# ============================================================
@app.route('/')
@auth_required
def index():
    return app.send_static_file('index.html')

@app.route('/detail/<int:poem_id>')
@auth_required
def detail_page(poem_id):
    return app.send_static_file('detail.html')

@app.route('/edit/<int:poem_id>')
@auth_required
def edit_page(poem_id):
    return app.send_static_file('edit.html')

@app.route('/create')
@auth_required
def create_page():
    return app.send_static_file('create.html')

@app.route('/check')
@auth_required
def check_page():
    return app.send_static_file('check.html')

if __name__ == '__main__':
    from waitress import serve
    serve(app, host='0.0.0.0', port=TRIM_SERVICE_PORT, threads=4)