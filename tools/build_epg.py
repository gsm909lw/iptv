#!/usr/bin/env python3
"""每日 EPG 构建：51zmt(央视/卫视) + epg.pw(北京本地台等) 合并，频道名 id 体系"""
import re, gzip, json, ssl, urllib.request, urllib.parse, sys
from datetime import date, timedelta

CTX = ssl.create_default_context()
UA = {'User-Agent': 'Mozilla/5.0'}

def fetch(url, timeout=60, binary=False):
    req = urllib.request.Request(url, headers=UA)
    d = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
    return d if binary else d.decode('utf-8', errors='ignore')

# ---------- 1) 51zmt 新鲜数据（数字 id -> 频道名） ----------
import os
zmt_urls = [
    'https://s.102031.xyz/xml/a1999882e.xml',
    'https://epg.51zmt.top:8001/e.xml',
]
x = None
local = os.environ.get('ZMT_LOCAL_FILE')  # 调试用：直接读本地文件
if local:
    x = open(local, encoding='utf-8').read()
    print('51zmt 源(本地):', local)
else:
    for u in zmt_urls:
        try:
            x = fetch(u, timeout=120)
            if '<programme' in x:
                print('51zmt 源:', u); break
        except Exception as e:
            print('失败:', u, e)
if not x or '<programme' not in x:
    sys.exit('51zmt 全部源失败')

idmap = {}
for cid, body in re.findall(r'<channel id="([^"]+)">(.*?)</channel>', x, re.S):
    names = re.findall(r'<display-name[^>]*>([^<]+)</display-name>', body)
    if names: idmap[cid] = names[0].strip()
x = re.sub(r'<channel id="([^"]+)"', lambda m: f'<channel id="{idmap.get(m.group(1), m.group(1))}"', x)
x = re.sub(r'(<programme [^>]*?)channel="([^"]+)"',
           lambda m: m.group(1) + f'channel="{idmap.get(m.group(2), m.group(2))}"', x)
# 去掉昨天以前的过期节目（按 start 日期逐条判断）
cutoff = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
def keep_old(m):
    tag = m.group(0)
    sm = re.search(r'start="(\d{8})', tag)
    return tag if (sm and sm.group(1) >= cutoff) else ''
x = re.sub(r'<programme [^>]*>.*?</programme>\s*', keep_old, x, flags=re.S)

# ---------- 2) epg.pw 补北京本地台等 ----------
# (输出频道名[须与 m3u 的 tvg-name 一致], [epg.pw 上的候选名])
PW_TARGETS = [
    ('北京新闻', ['北京新闻']),
    ('北京文艺', ['北京文艺']),
    ('北京影视', ['北京影视']),
    ('北京生活', ['北京生活']),
    ('北京财经', ['北京财经']),
    ('北京纪实科教', ['北京纪实科教', '北京纪实-科教', '北京纪实']),
    ('北京体育休闲', ['北京体育休闲', '北京体育']),
    ('卡酷动画', ['卡酷动画', '卡酷少儿']),
]

# 动态解析频道 id（避免硬编码失效）
pw = {}
try:
    page = fetch('https://epg.pw/areas/cn.html?lang=zh-hans', timeout=45)
    for href, title in re.findall(r'href="([^"]+)"[^>]*title="查看频道 ([^"]+)"', page):
        name = re.sub(r'\s*未来一周的节目表.*$', '', title).strip()
        m = re.search(r'/last/(\d+)\.html', href)
        if m and name and name not in pw:
            pw[name] = m.group(1)
    print('epg.pw 解析到', len(pw), '个频道')
except Exception as e:
    print('epg.pw 页面解析失败:', e)

blocks = []
existing = set(re.findall(r'<channel id="([^"]+)"', x))
for out_name, candidates in PW_TARGETS:
    cid = next((pw[c] for c in candidates if pw.get(c)), None)
    if not cid or out_name in existing:
        print(f'跳过: {out_name} (id={cid}, 已存在={out_name in existing})')
        continue
    ch_tag = f'<channel id="{out_name}"><display-name lang="zh">{out_name}</display-name></channel>\n'
    progs = []
    for off in range(3):  # 今天起 3 天
        d = (date.today() + timedelta(days=off)).strftime('%Y%m%d')
        try:
            xml = fetch(f'https://epg.pw/api/epg.xml?channel_id={cid}&date={d}', timeout=45)
            progs += re.findall(r'<programme .*?</programme>', xml, re.S)
        except Exception as e:
            print('epg.pw 失败:', out_name, d, e)
    if progs:
        # 归一 channel 属性为输出频道名
        p = ''.join(progs).replace(f'channel="{cid}"', f'channel="{out_name}"')
        blocks.append(ch_tag + p + '\n')
        print(f'{out_name}(id={cid}): {len(progs)} 条')
x = x.replace('</tv>', ''.join(blocks) + '</tv>')

# ---------- 3) 输出（固定写到脚本所在目录，避免相对路径歧义） ----------
HERE = os.path.dirname(os.path.abspath(__file__))
out = gzip.compress(x.encode('utf-8'), 9)
open(os.path.join(HERE, 'epg.xml.gz'), 'wb').write(out)
print(f'完成: 频道 {x.count("<channel ")} 节目 {x.count("<programme")} 大小 {len(out)//1024}KB')
print('输出:', os.path.join(HERE, 'epg.xml.gz'))
