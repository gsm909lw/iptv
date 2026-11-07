#!/usr/bin/env python3
"""每日 EPG 构建：112114 主源(央视/卫视/北京台, 当日新鲜数据) + epg.pw 补缺口，频道名 id 体系。

要点：
- 主源 112114 pp.xml（539 频道，当天数据），按台名归一化匹配我们的 m3u 台名
- 未匹配到的台，用 epg.pw 逐台补（仅限 epg.pw 上确实存在的台）
- 输出频道 id = m3u 的 tvg-id（中文台名，去掉 [高清] 之类的后缀）
- 只保留 昨天~+3天 的节目，避免文件过大
- 输出到仓库根目录 epg.xml.gz（GitHub Actions 提交的就是这个文件）
"""
import re, gzip, os, sys, ssl, urllib.request
from datetime import date, timedelta

CTX = ssl.create_default_context()
UA = {'User-Agent': 'Mozilla/5.0'}
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
M3U = os.path.join(ROOT, 'IPTV.m3u')
OUT = os.path.join(ROOT, 'epg.xml.gz')

SRC_112114 = 'https://epg.112114.xyz/pp.xml'
PW_AREA = 'https://epg.pw/areas/cn.html?lang=zh-hans'
PW_API = 'https://epg.pw/api/epg.xml?channel_id={cid}&date={d}'

# 我们的台名 -> 源里的台名（源里叫法不同时）
ALIAS = {
    '北京纪实科教': '北京纪实',
    '北京卡酷少儿': '卡酷少儿',
    'CCTV-5+ 体育赛事': 'CCTV5+',
    'CCTV-4K 超高清': 'CCTV4K',
    '求索记录4K': '求索纪录',
    'CETV1': '中国教育一套',
    'CETV2': '中国教育2台',
    'CETV4': '中国教育4台',
}
# 没有 EPG 的付费/轮播频道（按完整台名精确匹配，避免误伤 CCTV-7 国防军事 之类）
SKIP_NAMES = {'热播剧场', '星影', '动作影院', '家庭影院', '军旅剧场', '精选', '经典电影',
              '光影', '喜剧影院', '解密', '军事', '动画', '古装剧场', '城市剧场', '墨宝',
              '好学生', '早教', '爱生活', '美人', '美妆', '足球', '鉴赏', '音乐现场', '魅力时尚',
              '少儿动画'}


def fetch(url, timeout=90):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode('utf-8', errors='ignore')


def norm(s):
    s = s.replace(' ', '').replace('　', '')
    s = re.sub(r'\[[^\]]*\]$', '', s)
    s = re.sub(r'(高清|超清|超高清|蓝光|HDR|50帧)', '', s)
    s = re.sub(r'-(?=\d)', '', s)   # CCTV-2 -> CCTV2
    return s.lower()


def find_src_id(name, idset):
    k = norm(name)
    if k in idset:
        return idset[k]
    alt = norm(ALIAS.get(name, ''))
    if alt and alt in idset:
        return idset[alt]
    m = re.match(r'^(cctv\d*k?\+?)', k)
    if m and m.group(1) in idset:
        return idset[m.group(1)]
    return None


def keep_dates(progs, lo, hi):
    out = []
    for p in progs:
        m = re.search(r'start="(\d{8})', p)
        if m and lo <= m.group(1) <= hi:
            out.append(p)
    return out


def main():
    if not os.path.exists(M3U):
        sys.exit(f'找不到 m3u: {M3U}')
    m3u = open(M3U, encoding='utf-8').read()
    names, tvid = [], {}
    for l in m3u.splitlines():
        if l.startswith('#EXTINF'):
            n = l.split(',', 1)[1] if ',' in l else ''
            n = re.sub(r'\[[^\]]*\]$', '', n).strip()
            m = re.search(r'tvg-id="([^"]*)"', l)
            if n and n not in names:
                names.append(n)
                tvid[n] = (m.group(1).strip() if m else '')
    # 频道 id 统一用 m3u 的 tvg-id（播放器靠它匹配节目单）；没有 tvg-id 的退回台名
    cid = {n: (tvid.get(n) or n) for n in names}
    dup = len(cid) - len(set(cid.values()))
    print(f'm3u 台数: {len(names)} (id 重复 {dup})')

    lo = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
    hi = (date.today() + timedelta(days=3)).strftime('%Y%m%d')

    # ---------- 主源 112114 ----------
    try:
        src = fetch(SRC_112114)
        if '<programme' not in src:
            raise RuntimeError('无 programme 数据')
        print(f'112114: {len(src)//1024}KB')
    except Exception as e:
        sys.exit(f'112114 主源失败: {e}')

    blocks = {}
    for blk in re.findall(r'<programme .*?</programme>', src, re.S):
        m = re.search(r'channel="([^"]+)"', blk)
        if m:
            blocks.setdefault(m.group(1), []).append(blk)
    idset = {}
    for i in blocks:
        idset.setdefault(norm(i), i)
    print(f'112114 频道: {len(blocks)}')

    got = {}
    for n in names:
        if n in SKIP_NAMES:
            continue
        sid = find_src_id(n, idset)
        if sid:
            progs = keep_dates(blocks.get(sid, []), lo, hi)
            if progs:
                got[n] = [p.replace(f'channel="{sid}"', f'channel="{cid[n]}"') for p in progs]
    print(f'主源匹配: {len(got)} 台')

    # ---------- 补充源 epg.pw（只补主源完全没有的台；epg.pw 只提供当天数据） ----------
    need = [n for n in names if n not in got and n not in SKIP_NAMES]
    print(f'主源缺失、需要 epg.pw 补: {len(need)} 台')

    if need:
        try:
            page = fetch(PW_AREA, timeout=60)
            pw = {}
            for href, title in re.findall(r'href="([^"]+)"[^>]*title="查看频道 ([^"]+)"', page):
                nm = re.sub(r'\s*未来一周的节目表.*$', '', title).strip()
                mm = re.search(r'/last/(\d+)\.html', href)
                if mm and nm and nm not in pw:
                    pw[nm] = mm.group(1)
            pwset = {}
            for k, v in pw.items():
                pwset.setdefault(norm(k), v)
            print(f'epg.pw 频道: {len(pw)}')
            filled = 0
            for n in need:
                pwid = pw.get(n) or pwset.get(norm(n)) or pwset.get(norm(ALIAS.get(n, '')))
                if not pwid:
                    continue
                try:
                    xml = fetch(PW_API.format(cid=pwid, d=date.today().strftime('%Y%m%d')), timeout=30)
                except Exception as e:
                    print(f'  epg.pw 失败 {n}: {e}')
                    continue
                progs = keep_dates(re.findall(r'<programme .*?</programme>', xml, re.S), lo, hi)
                progs = [q.replace(f'channel="{pwid}"', f'channel="{cid[n]}"') for q in progs]
                if progs:
                    got[n] = progs
                    filled += 1
                    if filled % 10 == 0:
                        print(f'  ...已补 {filled} 台')
            print(f'epg.pw 补充: {filled} 台')
        except Exception as e:
            print(f'epg.pw 整体失败: {e}')

    if not got:
        sys.exit('没有任何节目数据，放弃写入')

    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="gsm909 iptv epg">']
    for n in names:
        if n in got:
            dn = ''.join(f'<display-name lang="zh">{x}</display-name>' for x in dict.fromkeys([cid[n], n]))
            xml.append(f'  <channel id="{cid[n]}">{dn}</channel>')
    for n in names:
        if n in got:
            for p in got[n]:
                xml.append('  ' + p.strip())
    xml.append('</tv>')
    data = '\n'.join(xml) + '\n'

    out = gzip.compress(data.encode('utf-8'), 9)
    open(OUT, 'wb').write(out)
    print(f'完成: 频道 {data.count("<channel ")} 节目 {data.count("<programme ")} 大小 {len(out)//1024}KB')
    print('输出:', OUT)


if __name__ == '__main__':
    main()
