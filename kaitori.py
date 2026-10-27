"""ディズニー・ロルカナ 買取価格比較スクリプト

ブラックフロッグ / カービット / ロルハッピー の買取価格を取得・照合して、
画像付きHTMLとCSVを出力する。前回実行分との価格差も出す。

使い方:
    python kaitori.py              # 取得して出力
    python kaitori.py --no-fetch   # 前回取得したデータで出力だけやり直す

出力 (このファイルと同じフォルダ):
    lorcana_kaitori_all.html / .csv     最新版
    history/YYYY-MM-DD.json             日付ごとのスナップショット (前回比の計算に使う)
    cache/                              取得した生データ
"""
import argparse
import collections
import csv
import datetime
import glob
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, 'cache')
HISTORY = os.path.join(BASE, 'history')
TEMPLATE = os.path.join(BASE, 'template.html')
WAIT = 1.0  # リクエスト間隔(秒)。お店のサーバーに負荷をかけないため

BF_URL = 'https://blackfrogstorage.com'
LH_URL = 'https://lorhappybuy.shop15.makeshop.jp'
CB_URL = 'https://carbbitlorcana-afk.github.io/github.io/'
CB_IMAGE = 'https://carbbit.ocnk.net/data/carbbit/product/'
LH_SKIP = ('サプライ', 'まとめて')  # カード以外のカテゴリ

SHOPS = {'bf': 'ブラックフロッグ', 'cb': 'カービット', 'lh': 'ロルハッピー'}
RMAP = {'C': 'コモン', 'U': 'アンコモン', 'R': 'レア', 'SR': 'スーパーレア', 'L': 'レジェンダリー',
        'EP': 'エピック', 'E': 'エンチャンテッド', 'I': 'アイコニック', 'P': 'プロモ'}
SETS = {1: '物語のはじまり', 2: 'フラッドボーンの渾沌', 3: 'インクランド探訪', 4: '逆襲のアースラ',
        5: '星々の輝き', 6: '大いなるアズライト', 7: 'アーケイジアと魔法の島', 8: 'ジャファーの王権',
        9: '物語のおもいで', 10: 'WHISPERS IN THE WELL', 11: 'WINTERSPELL', 12: '未知なる彼方へ！',
        13: 'ヴァインズ・アタック！', 14: 'ハイペリアシティ'}
SET_ORDER = [SETS[i] for i in sorted(SETS, reverse=True)] + ['プロモ', '英語版']
RAR_ORDER = ['アイコニック', 'エンチャンテッド', 'エピック', 'レジェンダリー', 'スーパーレア',
             'レア', 'アンコモン', 'コモン', 'プロモ']
INHERENT = {'E', 'I', 'EP'}  # もともと光るレアリティ。FOIL表記の有無を区別しない
CODE = re.compile(r'([A-Z]*\d+[A-Z]?/[A-Z]*\d+)\s*((?:JA|EN)-\d+)')
SWAPPED_CODE = re.compile(r'\[((?:JA|EN)-\d+)\s+([A-Z]*\d+[A-Z]?/[A-Z]*\d+)\]')


def log(*a):
    print(*a, flush=True)


def warn(msg):
    """ログに警告を出す。GitHub Actions 上では実行結果の画面にも表示される"""
    log(('::warning::' if os.environ.get('GITHUB_ACTIONS') else '警告: ') + msg)


def get(url, tries=3):
    """一時的なエラー (タイムアウト・混雑など) は間をあけてやり直す"""
    for n in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                body = r.read().decode('utf-8', 'replace')
            time.sleep(WAIT)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 404 or n == tries:
                raise
            err = f'HTTP {e.code}'
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if n == tries:
                raise
            err = str(e)
        wait = 10 * 3 ** (n - 1)  # 10秒, 30秒
        log(f'    アクセス失敗 ({err}) {wait}秒後にやり直し {n}/{tries - 1}: {url}')
        time.sleep(wait)


# ---------------------------------------------------------------- 取得

def fetch_blackfrog():
    top = get(BF_URL + '/')
    cats = sorted({int(c) for c in re.findall(r'list\.php\?category_id=(\d+)', top)})
    items = {}
    for c in cats:
        prev = None
        for page in range(1, 30):
            h = get(f'{BF_URL}/products/list.php?category_id={c}&disp_number=180&pageno={page}')
            got = {}
            for blk in h.split('<!--▼商品-->')[1:]:
                price = re.search(r'price02_default_(\d+)">([\d,]+)', blk)
                name = re.search(r'font-size: 0.9em;">\s*(.*?)\s*<', blk, re.S)
                img = re.search(r'background-image: url\(([^)]*)\)', blk)
                if price and name:
                    got[price.group(1)] = {'name': html.unescape(name.group(1).strip()),
                                           'price': int(price.group(2).replace(',', '')),
                                           'img': BF_URL + img.group(1) if img else ''}
            if not got or got.keys() == prev:
                break
            items.update(got)
            prev = got.keys()
            if len(got) < 180:
                break
        log(f'  ブラックフロッグ category {c}: 累計 {len(items)} 件')
    return list(items.values())


def fetch_lorhappy():
    top = get(LH_URL + '/')
    cats = {}
    for ct, label in re.findall(r'href="/view/category/(ct\d+)"[^>]*>([^<]*)', top):
        if label.strip() and not any(s in label for s in LH_SKIP):
            cats[ct] = label.strip()
    items = {}
    for ct, label in cats.items():
        prev = None
        for page in range(1, 50):
            h = get(f'{LH_URL}/view/category/{ct}?page={page}')
            got = {}
            for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
                try:
                    d = json.loads(m.group(1))
                except ValueError:
                    continue
                if d.get('@type') != 'ItemList':
                    continue
                for it in d['itemListElement']:
                    p = it['item']
                    got[p['url'].split('?')[0]] = {'name': p['name'], 'price': int(float(p['offers']['price'])),
                                                   'img': (p.get('image') or [''])[0]}
            if not got or got.keys() == prev:
                break
            items.update(got)
            prev = got.keys()
        log(f'  ロルハッピー {label}: 累計 {len(items)} 件')
    return list(items.values())


def fetch_carbbit():
    js = get(CB_URL + 'app.js')
    src = re.search(r"SOURCE\s*=\s*'([^']+)'", js).group(1)
    text = get(src)
    log(f'  カービット: {text.count(chr(10))} 行')
    return text


def fetch_all():
    """店ごとに取得する。失敗した店があっても残りの店は続ける。失敗した店のキーを返す"""
    os.makedirs(CACHE, exist_ok=True)
    jobs = [('bf', 'bf.json', fetch_blackfrog), ('lh', 'lh.json', fetch_lorhappy), ('cb', 'cb.csv', fetch_carbbit)]
    failed = set()
    for key, fname, fetch in jobs:
        log(f'{SHOPS[key]}を取得中…')
        try:
            data = fetch()
        except Exception as e:  # 1店の失敗で全体を止めない
            warn(f'{SHOPS[key]}の取得に失敗しました: {type(e).__name__}: {e}')
            failed.add(key)
            data = '' if fname.endswith('.csv') else []
        with open(os.path.join(CACHE, fname), 'w', encoding='utf-8') as f:
            f.write(data) if isinstance(data, str) else json.dump(data, f, ensure_ascii=False)
    return failed


# ---------------------------------------------------------------- 照合

def norm_name(s):
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'【[^】]*】|《[^》]*》|\[[^\]]*\]|\([^)]*\)|ノーマル版', '', s)
    return re.sub(r'[\s\-‐–—−]', '', s).lower()


def find_code(name):
    n = SWAPPED_CODE.sub(lambda g: f'[{g.group(2)} {g.group(1)}]', name.replace('[]', ''))
    return CODE.search(n)


def parse_numbered(name):
    """カービット/ロルハッピー形式: カード名【R】[12/204 JA-14]"""
    if re.search(r'未開封|キズ有|傷有', name):
        return None
    n = unicodedata.normalize('NFKC', name)
    m = CODE.search(n)
    if not m:
        return None
    r = re.search(r'【(C|U|R|SR|L|EP|E|I|P)】', n)
    rar = r.group(1) if r else '?'
    foil = ('FOIL' in n.split('【' + rar + '】')[0].upper()) and rar not in INHERENT and rar != 'P'
    code, st = m.group(1), m.group(2)
    variant = 'ノーマル版' if 'ノーマル版' in n else ('EN' if st.startswith('EN') else '')
    base = re.sub(r'^(【[^】]*】\s*)+', '', name)
    base = re.sub(r'\s*【(C|U|R|SR|L|EP|E|I|P)】.*$', '', base).replace('ノーマル版', '').strip()
    promo = rar == 'P' or not code.endswith(('/204', '/207'))
    setname = '英語版' if st.startswith('EN') else ('プロモ' if promo else SETS.get(int(st[3:]), st))
    meta = dict(code=code, set=st, setname=setname, rar=RMAP.get(rar, rar), foil=foil,
                name=base + (' (ノーマル版)' if variant == 'ノーマル版' else ''))
    return (code, st, foil, variant), meta, norm_name(name), rar


def merge():
    rows, idx = {}, {}

    def put(key, shop, price, img, meta):
        d = rows.setdefault(key, dict(meta))
        d[shop] = max(price, d.get(shop, 0))
        if img and not d.get('img_' + shop):
            d['img_' + shop] = img

    # カービット
    for r in csv.reader(io.open(os.path.join(CACHE, 'cb.csv'), encoding='utf-8-sig')):
        if len(r) < 4 or not r[1].strip() or 'サプライ' in r[3]:
            continue
        raw = re.sub(r'[,，¥￥円\s]', '', unicodedata.normalize('NFKC', r[2] or ''))
        if not raw.isdigit() or int(raw) <= 0:
            continue
        p = parse_numbered(r[1].strip())
        if not p:
            continue
        key, meta, n, rar = p
        img = CB_IMAGE + r[5].strip() if len(r) > 5 and r[5].strip() and not r[5].startswith('#') else ''
        put(key, 'cb', int(raw), img, meta)
        idx[(meta['set'], n, rar, key[2])] = key

    # ロルハッピー
    for it in json.load(open(os.path.join(CACHE, 'lh.json'), encoding='utf-8')):
        p = parse_numbered(it['name']) if it['price'] > 0 else None
        if not p:
            continue
        key, meta, n, rar = p
        put(key, 'lh', it['price'], it['img'], meta)
        idx.setdefault((meta['set'], n, rar, key[2]), key)

    # ブラックフロッグ (シングルは番号が無いので、シリーズ＋名前＋レアリティ＋FOILで照合)
    bf = json.load(open(os.path.join(CACHE, 'bf.json'), encoding='utf-8'))
    foil_codes = set()
    for it in bf:
        nm = unicodedata.normalize('NFKC', it['name'])
        m = find_code(nm)
        if m and 'FOIL' in nm:
            foil_codes.add(m.group(1) + m.group(2))
    unmatched = 0
    for it in bf:
        if it['price'] <= 0:
            continue
        nm = unicodedata.normalize('NFKC', it['name'])
        foil = '《FOIL》' in nm or 'エンチャンテッドFOIL' in nm
        r = re.search(r'\[(C|U|R|SR|S|L|EP|EN|E|I|P)\]', nm)
        rar = {'EN': 'E', 'S': 'SR'}.get(r.group(1), r.group(1)) if r else '?'
        if 'エンチャンテッドFOIL' in nm and rar not in ('P', 'I'):
            rar = 'E'
        base = re.sub(r'^(《[^》]*》|《エンチャンテッドFOIL)+', '', it['name'])
        base = re.sub(r'\[.*$', '', base).strip()
        m = find_code(nm)
        if m:  # プロモなど番号付き
            code, st = m.group(1), m.group(2)
            var = 'EN' if st.startswith('EN') else ''
            if rar == 'P' and not foil and code + st in foil_codes:
                var = 'ノーマル版'
            f = False if rar == 'P' else foil
            setname = '英語版' if st.startswith('EN') else ('プロモ' if rar == 'P' else SETS.get(int(st[3:]), st))
            meta = dict(code=code, set=st, setname=setname, rar=RMAP.get(rar, rar), foil=f,
                        name=base + (' (ノーマル版)' if var == 'ノーマル版' else ''))
            put((code, st, f, var), 'bf', it['price'], it['img'], meta)
            continue
        s = re.search(r'\((JA-\d+|\d+)(?:st|nd|rd|th)?\)', nm, re.I)
        if not s:
            continue
        st = 'JA-' + re.sub(r'\D', '', s.group(1))
        if rar in INHERENT:
            foil = False
        n = norm_name(it['name'])
        key = idx.get((st, n, rar, foil))
        if not key:  # レアリティ表記ゆれ対策: 名前とFOILが一致する候補が1つだけなら採用
            cand = {v for k, v in idx.items() if k[0] == st and k[1] == n and k[3] == foil
                    and (k[2] in INHERENT) == (rar in INHERENT) and k[2] != 'P'}
            key = cand.pop() if len(cand) == 1 else None
        if not key:
            unmatched += 1
            key = ('?', st, foil, n + rar)
        meta = dict(code='', set=st, setname=SETS.get(int(st[3:]), st), rar=RMAP.get(rar, rar), foil=foil, name=base)
        put(key, 'bf', it['price'], it['img'], meta)

    out = []
    for key, d in rows.items():
        d['img'] = d.get('img_lh') or d.get('img_cb') or d.get('img_bf') or ''
        d['id'] = '|'.join(str(k) for k in key)
        out.append({k: d[k] for k in ('id', 'setname', 'set', 'code', 'rar', 'foil', 'name', 'bf', 'cb', 'lh', 'img') if k in d})
    log(f'照合結果: {len(out)} 件 (ブラックフロッグ単独 {unmatched} 件)')
    return out


# ---------------------------------------------------------------- 出力

def best_shop(x):
    v = {k: x[k] for k in SHOPS if k in x}
    m = max(v.values())
    if len(v) == 1:
        return SHOPS[next(iter(v))] + 'のみ'
    if min(v.values()) == m:
        return '同額'
    return '/'.join(SHOPS[k] for k in v if v[k] == m)


def load_prev(today):
    """今日より前で一番新しいスナップショットを (日付, {id: 記録}) で返す"""
    snaps = sorted(p for p in glob.glob(os.path.join(HISTORY, '*.json'))
                   if os.path.basename(p) < today + '.json')
    if not snaps:
        return None, {}
    return os.path.basename(snaps[-1])[:-5], {c['id']: c for c in json.load(open(snaps[-1], encoding='utf-8'))}


def check_and_fill(cards, prev, failed):
    """件数が前回の半分未満の店も失敗扱いにし、失敗した店の価格は前回の値で埋める"""
    for k in SHOPS:
        if k in failed or not prev:
            continue
        n_now = sum(1 for c in cards if k in c)
        n_prev = sum(1 for p in prev.values() if k in p)
        if n_prev and n_now < n_prev * 0.5:
            warn(f'{SHOPS[k]}の件数が前回の半分未満です ({n_prev} → {n_now} 件)。サイトの変更などの可能性があるため前回の値を使います')
            failed.add(k)
    if not failed:
        return cards
    for c in cards:
        p = prev.get(c['id'], {})
        for k in failed:
            c.pop(k, None)
            if k in p:
                c[k] = p[k]
    return [c for c in cards if any(k in c for k in SHOPS)]


def add_changes(cards, prev):
    """前回スナップショットとの差分を付ける"""
    for c in cards:
        p = prev.get(c['id'])
        if not p:
            continue
        diff = {k: c[k] - p[k] for k in SHOPS if k in c and k in p and c[k] != p[k]}
        if diff:
            c['diff'] = diff


def build_history(cards):
    """history/*.json を読み、カードごと・店ごとに「価格が変わった日」だけを残す"""
    paths = sorted(glob.glob(os.path.join(HISTORY, '*.json')))
    dates = [os.path.basename(p)[:-5] for p in paths]
    ids = {c['id'] for c in cards}
    series = collections.defaultdict(dict)
    last = collections.defaultdict(dict)
    for i, p in enumerate(paths):
        snap = {c['id']: c for c in json.load(open(p, encoding='utf-8'))}
        for cid in ids:
            s = snap.get(cid, {})
            for k in SHOPS:
                v = s.get(k)
                if k not in last[cid]:
                    if v is None:
                        continue
                elif last[cid][k] == v:
                    continue
                series[cid].setdefault(k, []).append([i, v])
                last[cid][k] = v
    return {'dates': dates, 'c': series}


def write_outputs(cards, today, prev_date, failed):
    cards.sort(key=lambda x: (SET_ORDER.index(x['setname']) if x['setname'] in SET_ORDER else 99,
                              RAR_ORDER.index(x['rar']) if x['rar'] in RAR_ORDER else 99,
                              x['foil'], -max(x.get(k, 0) for k in SHOPS)))
    with open(os.path.join(BASE, 'lorcana_kaitori_all.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['シリーズ', 'レアリティ', '加工', '番号', 'カード名', *SHOPS.values(), '最高値の店',
                    *[n + ' 前回比' for n in SHOPS.values()], '画像URL'])
        for x in cards:
            diff = x.get('diff', {})
            w.writerow([x['setname'], x['rar'], 'FOIL' if x['foil'] else '通常', x.get('code', ''), x['name'],
                        *[x.get(k, '-') for k in SHOPS], best_shop(x),
                        *[f'{diff[k]:+d}' if k in diff else '' for k in SHOPS], x.get('img', '')])
    data = json.dumps(cards, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    meta = json.dumps({'date': today, 'prev': prev_date, 'failed': [SHOPS[k] for k in SHOPS if k in failed]},
                      ensure_ascii=False)
    hist = json.dumps(build_history(cards), ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    page = (open(TEMPLATE, encoding='utf-8').read()
            .replace('__DATA__', data).replace('__HISTORY__', hist).replace('__META__', meta))
    open(os.path.join(BASE, 'lorcana_kaitori_all.html'), 'w', encoding='utf-8').write(page)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='ロルカナ買取価格比較')
    ap.add_argument('--no-fetch', action='store_true', help='取得せず、前回のデータで出力だけやり直す')
    args = ap.parse_args()
    # GitHub Actions は UTC で動くので、日付は日本時間で数える
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date().isoformat()
    failed = set() if args.no_fetch else fetch_all()
    cards = merge()
    os.makedirs(HISTORY, exist_ok=True)
    prev_date, prev = load_prev(today)
    cards = check_and_fill(cards, prev, failed)
    if len(failed) == len(SHOPS) or not cards:
        log('全店の取得に失敗したため、記録とページは更新しません')
        sys.exit(1)
    add_changes(cards, prev)
    snap = {c['id']: {k: v for k, v in c.items() if k in ('id', 'name', *SHOPS)} for c in cards}
    # 失敗した店の記録は前回分を引き継ぐ (推移グラフに「消えた→戻った」のノイズを出さないため)
    for cid, p in prev.items():
        for k in failed:
            if k in p:
                snap.setdefault(cid, {'id': cid, 'name': p.get('name', '')})[k] = p[k]
    json.dump(list(snap.values()), open(os.path.join(HISTORY, today + '.json'), 'w', encoding='utf-8'),
              ensure_ascii=False)
    write_outputs(cards, today, prev_date, failed)
    wins = collections.Counter(best_shop(c) for c in cards)
    changed = sum(1 for c in cards if 'diff' in c)
    log('最高値の店: ' + ', '.join(f'{k} {v}' for k, v in wins.most_common()))
    if prev_date:
        log(f'前回 ({prev_date}) から価格が変わったカード: {changed} 件')
    if failed:
        log('取得に失敗した店 (前回の値を使用): ' + '・'.join(SHOPS[k] for k in SHOPS if k in failed))
    log('出力: ' + os.path.join(BASE, 'lorcana_kaitori_all.html'))


if __name__ == '__main__':
    main()
