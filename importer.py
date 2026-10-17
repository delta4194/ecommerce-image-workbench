"""Local document/table import. No model calls, network access or macro execution."""
import base64
import csv
import io
import json
import re
import zipfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

ALIASES = {
    'name': ['产品名称','商品名称','产品名','商品名','名称','productname','name'],
    'erp': ['erp产品编号','erp编号','产品编号','货号','sku','erp'],
    'asin': ['asin','本品asin'],
    'reference_url': ['竞品亚马逊链接','竞品链接','参考链接','referenceurl'],
    'source_url': ['阿里巴巴链接','1688链接','采购链接','厂家链接','sourceurl'],
    'identity': ['拿货规格','采购规格','规格','套装数量','identity'],
    'own_url': ['uk链接','本品链接','自有链接','ownurl'],
    'facts': ['五点描述','卖点','产品资料','商品描述','描述','facts'],
    'stage': ['制图进度','进度','stage'],
    'notes': ['备注','作图要求','补充要求','notes'],
}
def key_of(value):
    compact = re.sub(r'[\s：:_-]', '', value).lower()
    return next((key for key, names in ALIASES.items() if compact in names), '')

def clean(value):
    value = re.sub(r'<br\s*/?>', '\n', str(value or ''), flags=re.I)
    value = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'\2', value)
    value = re.sub(r'\\([_*&|])', r'\1', value)
    return unescape(value).strip()

def urls(value):
    return list(dict.fromkeys(re.findall(r'https?://[^\s<>\]\)"\u3002]+', value)))


def is_link_only(value):
    """整格内容只是一条链接（或链接+一两个符号）时，它不可能是拿货规格。"""
    text = clean(value)
    if not urls(text):
        return False
    return len(re.sub(r'https?://\S+', '', text).strip(' \t\r\n·-—,，;；。') ) < 6


def supplier_links(value):
    """一行里可能有两个（或多个）1688货源链接，各对应一套采购SKU。"""
    return [u for u in urls(value) if '1688.com' in u][:4]


COMPETITOR_IDENTITY = re.compile(r'竞品[^\n]{0,8}(一样|相同|同款|同图|图片)|(一样|相同|同款|同图)[^\n]{0,8}竞品')


def identity_from_competitor(spec):
    """拿货规格写“和竞品一样”这类文字时，用竞品主图当产品基准图。"""
    return bool(COMPETITOR_IDENTITY.search(clean(spec)))

SKU_LABEL = r'[A-Za-z0-9][A-Za-z0-9._\-]{0,30}'
QUANTITY_WORD = r'各\s*(?:[0-9]+|[一二三四五六七八九十两几]+)\s*(?:个|件|张|只|套|对|双|支|条|把|片|盒|包|卷|款|色|pcs)?'
UNIT_PREFIX = re.compile(r'^(?:pcs|个|件|张|只|套|对|双|支|条|把|片|盒|包|卷|色|款|[+＋、,，;；:：])+', re.I)


def normalize_sku(value):
    """把“双+浅灰”“个*1”这类粘连的单位字去掉，只留款式名。"""
    text = str(value or '').strip()
    text = re.sub(r'[（(][^）)]{0,80}[）)]', '', text)   # 去掉括号备注：(10个装+10根25cm麻绳)
    text = re.sub(r'[（(].*$', '', text)                 # 没闭合的括号也从这里截断
    text = UNIT_PREFIX.sub('', text.strip())
    return text.strip(' \t·-—:：;；,，+*×✕')


def component_line(value):
    """判断一行是不是“同一个商品的采购款式续行”。

    支持 A*2、单独一个款式号（NF-W063）、以及“各3个”这类数量说明。
    """
    text = clean(value).strip()
    if not text:
        return False
    if re.fullmatch(SKU_LABEL, text):
        return True
    if re.fullmatch(QUANTITY_WORD, text, re.I):
        return True
    return bool(re.fullmatch(r'[^\n*×✕]{1,60}\s*[*×✕]\s*\d+\s*(?:pcs|个|件|张|只|套)?', text, re.I))

def bundle_parts(spec):
    """Purchase variants are components of a listing, never individual tasks."""
    parts=[]
    # 一份规格里可能横跨两个1688链接、用空格或逗号排开：A*1 B*2 C*1 D*1 E*3 F*2
    for match in re.finditer(r'([^\s*×✕:：,;，、/]{1,60}?)\s*[*×✕]\s*(\d+)\s*(?:pcs|个|件|张|只|套)?',clean(spec),re.I):
        sku=normalize_sku(match.group(1))
        if sku: parts.append({'sku':sku,'quantity':int(match.group(2))})
    if not parts:
        for line in re.split(r'[\n;,；，]',clean(spec)):
            match=re.fullmatch(r'\s*([^\n*×✕:：]{1,60}?)\s*[*×✕]\s*(\d+)\s*(?:pcs|个|件|张|只|套)?\s*',line,re.I)
            if match: parts.append({'sku':match[1].strip(),'quantity':int(match[2])})
    if not parts:
        # 换行/顿号排开的款式号，配合“各N个”表示每款N个：
        # NF-W063 / NF-W066 / … / 各3个
        text=clean(spec)
        each=re.search(r'各\s*([0-9]+)',text)
        quantity=int(each.group(1)) if each else 0
        for token in re.split(r'[\n;,；，、/]+',text):
            label=normalize_sku(re.sub(r'^[\s\-•·]+','',token))
            if not label or re.fullmatch(QUANTITY_WORD,label,re.I):
                continue
            if re.fullmatch(SKU_LABEL,label) or (
                    len(label)<=20 and not re.search(r'\d',label) and not urls(label)
                    and re.fullmatch(r'[\u4e00-\u9fffA-Za-z（）()\-+/\s]{1,20}',label)):
                # 纯款式/颜色名（如“红色/黑色/灰色/绿色 各一个”）也是采购款式。
                parts.append({'sku':label,'quantity':quantity})
    return parts

def purchase_skus(spec):
    """A bare procurement label such as '10' is a SKU name, not a quantity."""
    parts=bundle_parts(spec)
    if parts:
        skus=[p['sku'] for p in parts if p['sku'] and not re.match(r'^\d+\s*(?:张|包|件|个|pcs)',p['sku'],re.I)]
        skus=[s for s in dict.fromkeys(skus) if s and not re.fullmatch(QUANTITY_WORD,s,re.I)]
        return skus[:24]
    label=clean(spec)
    label=re.sub(r'^(?:采购SKU|商家SKU|SKU|款式|颜色)\s*[:：]\s*','',label,flags=re.I).strip()
    if not label or len(label)>80 or '\n' in label or urls(label):return []
    if re.search(r'\d\s*(?:pcs|cm|mm|厘米|毫米|张|件|包|套|个|厚)',label,re.I):return []
    return [label]

def join_wrapped_rows(rows):
    """把“一条商品记录”里的多行内容并回去。

    飞书里的“拿货规格”经常是几十行的多行文本（含括号备注、各N个、甚至 1688 链接）。
    只有含 ERP 编号 / ASIN（或明显是表格行）才算新商品，其余换行都并回上一条记录的规格格。
    """
    result=[]
    targets=[]
    for row in rows:
        if not row: continue
        cells=[clean(cell) for cell in row]
        if not any(cells): continue
        if not result or looks_like_record(cells) or is_header_row(cells):
            result.append(cells)
            targets.append(spec_index(cells))
            continue
        target=targets[-1]
        extra=[cell for cell in cells if cell and cell not in ('是','否','广告未创建','广告已创建')]
        if extra and 0<=target<len(result[-1]):
            result[-1][target]=(str(result[-1][target])+'\n'+' '.join(extra)).strip()
    return result


ERP_CODE = re.compile(r'\bWLH\d{3,}\b', re.I)
ASIN_CODE = re.compile(r'\bB0[A-Z0-9]{8}\b')
ANY_URL = re.compile(r'https?://')


def looks_like_record(cells):
    """一行只有带 ERP 编号 / ASIN，或“多列 + 链接且首格不是链接”时，才算新的商品记录。"""
    text=' '.join(cells)
    if ERP_CODE.search(text) or ASIN_CODE.search(text):
        return True
    filled=[cell for cell in cells if cell]
    return len(filled)>=3 and bool(ANY_URL.search(text)) and not ANY_URL.match(cells[0] or '')


def is_header_row(cells):
    keys=[key_of(cell) for cell in cells]
    return 'name' in keys and sum(bool(key) for key in keys)>=2


def spec_index(row):
    """拿货规格所在的列：最后一个链接列之后的那一格（表里规格紧跟在货源链接后面）。

    一旦记录建立就固定下来，后面所有续行都并到同一格——规格里自带链接也不会让判断漂移。
    """
    last_url=-1
    for index,cell in enumerate(row):
        if cell and ANY_URL.search(str(cell)):
            last_url=index
    if last_url>=0 and last_url+1<len(row):
        return last_url+1
    filled=[index for index,cell in enumerate(row) if cell]
    return filled[-1] if filled else 0

class Tables(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.text=[]
    def handle_starttag(self, tag, attrs):
        if tag=='tr': self.row=[]
        if tag in ('td','th'): self.cell=[]
        if tag=='br' and self.cell is not None: self.cell.append('\n')
        if tag=='a' and self.cell is not None:
            href=dict(attrs).get('href','')
            if href.startswith(('http://','https://')): self.cell.append(' '+href+' ')
    def handle_data(self, data):
        self.text.append(data)
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ('td','th') and self.cell is not None:
            if self.row is not None: self.row.append(''.join(self.cell).strip())
            self.cell=None
        if tag=='tr' and self.row is not None:
            self.rows.append(self.row); self.row=None

def text_rows(text):
    text=text.lstrip('\ufeff').strip()
    if not text: return []
    if '<table' in text.lower():
        parser=Tables(); parser.feed(text); return parser.rows
    lines=[line for line in text.splitlines() if line.strip()]
    if any(line.strip().startswith('|') for line in lines):
        return [[clean(c.replace('\x00','|')) for c in line.strip().strip('|').replace('\\|','\x00').split('|')]
                for line in lines if line.strip().startswith('|') and not re.fullmatch(r'[\s|:\-]+',line)]
    if '\t' in text: return join_wrapped_rows(list(csv.reader(io.StringIO(text),delimiter='\t')))
    try:
        dialect=csv.Sniffer().sniff(text[:12000],delimiters=',;')
        rows=list(csv.reader(io.StringIO(text),dialect))
        if rows and len(rows[0])>=2: return join_wrapped_rows(rows)
    except csv.Error: pass
    # Blank lines in specifications are NOT product boundaries. Only a new
    # explicit product-name field starts another free-text listing.
    blocks=re.split(r'\n(?=\s*(?:产品名称|商品名称|产品名|商品名)\s*[:：])',text)
    result=[]
    for block in blocks:
        mapped={}; current=''
        for line in block.splitlines():
            match=re.match(r'^\s*([^:：]{1,25})[:：]\s*(.*)',line)
            key=key_of(match[1]) if match else ''
            if key: mapped[key]=clean(match[2]); current=key
            elif current: mapped[current]+='\n'+line
        if mapped: result.append(mapped)
        else: result.append({'name':block.splitlines()[0][:120],'facts':block,'_uncertain':True})
    return result

def merge_listing_rows(products):
    """Only explicit same Amazon identity (or same ERP without ASIN) groups rows."""
    merged=[]; indexed={}
    for product in products:
        asin=product.get('asin','').strip().upper()
        erp=product.get('erp','').strip().upper()
        key=('asin',asin) if asin else ('erp',erp) if erp else None
        target=indexed.get(key) if key else None
        if target is None:
            merged.append(product)
            if key: indexed[key]=product
            continue
        for field in ('identity','facts','notes','raw_import'):
            value=product.get(field,'').strip()
            if value and value!=target.get(field,'').strip():
                target[field]=(target.get(field,'')+'\n'+value).strip()
        for field in ('source_url','reference_url','own_url','erp'):
            if not target.get(field): target[field]=product.get(field,'')
        target['warnings'].append('相同亚马逊商品标识的采购行已合并为一套商品；请核对组成与数量')
    for product in merged:
        product['bundle_components']=bundle_parts(product.get('identity',''))
        product['purchase_skus']=purchase_skus(product.get('identity',''))
    return merged

def office_rows(raw,suffix):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if len(z.infolist())>5000 or sum(i.file_size for i in z.infolist())>60_000_000:
            raise ValueError('文档解压后过大，请分批导出')
        def xml(path):
            data=z.read(path)
            if b'<!DOCTYPE' in data or b'<!ENTITY' in data: raise ValueError('不支持含自定义实体的文档')
            return ET.fromstring(data)
        if suffix=='.docx':
            ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            root=xml('word/document.xml'); rows=[]
            relpath='word/_rels/document.xml.rels'
            rels={n.attrib.get('Id'):n.attrib.get('Target','') for n in xml(relpath)} if relpath in z.namelist() else {}
            for link in root.findall('.//w:hyperlink',ns):
                target=rels.get(link.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'),'')
                if target.startswith(('https://','http://')):
                    label=ET.SubElement(link,'{'+ns['w']+'}t'); label.text=' '+target+' '
            for table in root.findall('.//w:tbl',ns):
                for row in table.findall('w:tr',ns):
                    rows.append(['\n'.join(''.join(p.itertext()) for p in c.findall('w:p',ns)) for c in row.findall('w:tc',ns)])
            if rows: return rows
            paragraphs=[''.join(t.text or '' for t in p.findall('.//w:t',ns)) for p in root.findall('.//w:p',ns)]
            return text_rows('\n'.join(paragraphs))
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            shared=[''.join(n.text or '' for n in si.findall('.//s:t',ns)) for si in xml('xl/sharedStrings.xml')]
        rows=[]
        for path in sorted(p for p in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',p)):
            root=xml(path); links={}
            relpath='xl/worksheets/_rels/'+Path(path).name+'.rels'
            if relpath in z.namelist():
                rels={n.attrib.get('Id'):n.attrib.get('Target','') for n in xml(relpath)}
                for link in root.findall('.//s:hyperlink',ns):
                    target=rels.get(link.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'),'')
                    if target.startswith(('https://','http://')): links[link.attrib.get('ref')]=target
            for row in root.findall('.//s:sheetData/s:row',ns):
                cells={}
                for c in row.findall('s:c',ns):
                    address=c.attrib.get('r','A1'); index=0
                    for char in re.sub('[^A-Z]','',address): index=index*26+ord(char)-64
                    if index>200: continue
                    value=c.findtext('s:v','',ns)
                    if c.attrib.get('t')=='s': value=shared[int(value)] if value else ''
                    elif c.attrib.get('t')=='inlineStr': value=''.join(n.text or '' for n in c.findall('.//s:t',ns))
                    # Never evaluate formulas. HYPERLINK literal targets can be recovered safely.
                    formula=c.findtext('s:f','',ns)
                    literal=re.match(r'(?i)HYPERLINK\(\s*"(https?://[^"]+)"',formula)
                    cells[max(0,index-1)]=links.get(address) or (literal[1] if literal else value)
                if cells: rows.append([cells.get(i,'') for i in range(max(cells)+1)])
        return rows

def parse_import(body):
    if body.get('data'):
        raw=base64.b64decode(body['data'].split(',',1)[-1],validate=True)
        if len(raw)>15_000_000: raise ValueError('单个文件最多15MB')
        suffix=Path(body.get('name','')).suffix.lower()
        if suffix in ('.xlsx','.docx'): rows=office_rows(raw,suffix)
        elif suffix in ('.csv','.tsv','.txt','.md','.html','.htm','.json'):
            text=None
            for encoding in ('utf-8-sig','utf-16','gb18030'):
                try: text=raw.decode(encoding); break
                except UnicodeError: pass
            if text is None: raise ValueError('无法读取文件编码，请导出为UTF-8 CSV')
            if suffix=='.json':
                parsed=json.loads(text); records=parsed if isinstance(parsed,list) else parsed.get('records',[])
                rows=[{key_of(k) or k:clean(v) for k,v in item.get('fields',item).items()} for item in records if isinstance(item,dict)]
            else: rows=text_rows(text)
        else: raise ValueError('支持 XLSX、CSV、TSV、DOCX、TXT、Markdown、HTML、JSON；PDF和截图请先复制文字')
    else: rows=text_rows(str(body.get('text',''))[:1_000_000])
    products=[]; header=None; warnings=[]
    for row in rows:
        if len(products)>=200: raise ValueError('一次最多导入200个商品，请分批导出')
        if isinstance(row,dict): item=dict(row); original=json.dumps(row,ensure_ascii=False)
        else:
            row=[clean(c) for c in row]
            if not any(row): continue
            keys=[key_of(c) for c in row]
            if 'name' in keys and sum(bool(k) for k in keys)>=2: header=keys; continue
            original=' | '.join(row)
            item={}
            if header:
                for key,value in zip(header,row):
                    if key and value:
                        if key.endswith('_url') and item.get(key): continue
                        item[key]=(item.get(key,'')+'\n'+value).strip()
            else:
                item['name']=row[0]
                item['erp']=next((c for c in row if re.fullmatch(r'(?i)WLH\d+',c)), '')
                item['asin']=next((c for c in row if re.fullmatch(r'(?i)[A-Z0-9]{10}',c) and re.search(r'\d',c)), '')
                # Find the procurement link, not a fixed column index. Bare numeric
                # text in the adjacent specification cell is a merchant SKU label.
                supplier_index=next((i for i,c in enumerate(row) if any(re.search(r'https?://(?:[\w-]+\.)?1688\.com/',u) for u in urls(c))),None)
                spec=''
                if supplier_index is not None and supplier_index+1<len(row):
                    adjacent=row[supplier_index+1]
                    if adjacent and not is_link_only(adjacent) and adjacent not in ('是','否','广告未创建'):
                        # 规格里自带 1688 链接是正常的，只要整格不只是链接就当作规格。
                        spec=adjacent
                if not spec:
                    # 两个1688链接时，规格常被挤到别的列：找带“款式*数量”写法的单元格。
                    spec=next((c for c in row[1:] if c and not urls(c) and re.search(r'[*×✕]\s*\d+',c)), '')
                if not spec:
                    spec=next((c for c in row[1:] if not urls(c) and re.search(r'\d+\s*(?:张|包|套|件|pcs|个|cm|mm|色|款)',c,re.I)), '')
                item['identity']=spec
                item['stage']=next((c for c in row if not urls(c) and re.search(r'制图|美工|阶段',c)), '')
        links=urls(original)
        suppliers=supplier_links(original)
        if suppliers:
            item['source_urls']=suppliers
        item.setdefault('source_url',suppliers[0] if suppliers else '')
        amazon=[u for u in links if re.search(r'://(?:[\w-]+\.)?amazon\.',u)]
        asin=item.get('asin','').upper()
        own=next((u for u in amazon if asin and asin in u.upper()),'')
        item.setdefault('own_url',own)
        item.setdefault('reference_url',next((u for u in amazon if u!=own),''))
        if not item.get('name','').strip():
            # A spreadsheet may use a merged listing-name/ASIN cell above
            # additional purchase-variant rows. Keep those rows in that listing.
            if products and item.get('identity') and not item.get('asin') and not item.get('erp'):
                products[-1]['identity']+='\n'+item['identity']
                products[-1]['raw_import']+='\n'+original
            continue
        item['raw_import']=original[:18000]
        item['source_urls']=list(dict.fromkeys(item.get('source_urls') or ([item['source_url']] if item.get('source_url') else [])))[:4]
        item['identity_from_competitor']=identity_from_competitor(item.get('identity',''))
        item['selected']=True
        item['warnings']=[]
        if item.pop('_uncertain',False): item['warnings'].append('自由文本：第一行暂作商品名称，请核对')
        if not item.get('identity'): item['warnings'].append('未找到拿货规格')
        products.append({**{k:'' for k in ALIASES},**item})
    if not products: raise ValueError('没有识别到商品。请复制包含产品名称的表格行或带字段名的资料')
    products=merge_listing_rows(products)
    warnings.append('一个亚马逊销售商品建立一个任务；拿货规格中的多个采购SKU及数量属于该商品的套装组成。')
    warnings.append('仅整理文档现有内容；链接不会自动变成产品卖点，图片仍需上传或从商品页采集。')
    return {'products':products,'warnings':warnings}
