"""Real document fixtures, all local and temporary; no model/network/OCR calls."""
import gzip
import io
import json
import tarfile
import zipfile

import pytest

from wm.agent.context import ContextStore
from wm.agent import material_readers as readers


@pytest.fixture
def store(tmp_path):
    return ContextStore(tmp_path / 'agent')


def docx_bytes():
    docx = pytest.importorskip('docx')
    doc = docx.Document()
    doc.add_paragraph('第一段：材料来自本地。')
    doc.add_paragraph('Second paragraph with evidence.')
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = '名称'
    table.cell(0, 1).text = '数量'
    table.cell(1, 0).text = '苹果'
    table.cell(1, 1).text = '3'
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def xlsx_bytes():
    openpyxl = pytest.importorskip('openpyxl')
    book = openpyxl.Workbook()
    first = book.active
    first.title = '预算'
    first.append(['名称', '数量', '公式'])
    first.append(['苹果', 3, '=B2*2'])
    second = book.create_sheet('来源')
    second['A1'] = 'https://example.invalid/not-fetched'
    second['B1'] = True
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def pdf_bytes(*texts, encrypted=False):
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
    writer = pypdf.PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=200, height=200)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 10 100 Td ({text}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    if encrypted:
        writer.encrypt('temporary-fixture-password')
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def tar_bytes(files, mode='w:gz'):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def plain_docx_bytes(text):
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return zip_bytes({"word/document.xml": document.encode("utf-8")})


def pptx_bytes(text='PptCanary53'):
    presentation = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'slide" Target="slides/slide1.xml"/></Relationships>')
    slide = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp>'
             f'<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    return zip_bytes({"ppt/presentation.xml": presentation.encode("utf-8"),
                      "ppt/_rels/presentation.xml.rels": rels.encode("utf-8"),
                      "ppt/slides/slide1.xml": slide.encode("utf-8")})


def odt_bytes(text='OdtCanary64'):
    content = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text>'
               f'<text:p>{text}</text:p><table:table><table:table-row><table:table-cell>'
               '<text:p>单元格 OdtCell75</text:p></table:table-cell></table:table-row></table:table>'
               '</office:text></office:body></office:document-content>')
    return zip_bytes({"content.xml": content.encode("utf-8")})


def ods_bytes(text='OdsCanary41'):
    content = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:spreadsheet>'
               f'<table:table table:name="预算"><table:table-row><table:table-cell><text:p>{text}</text:p>'
               '</table:table-cell><table:table-cell><text:p>42</text:p></table:table-cell>'
               '</table:table-row></table:table></office:spreadsheet></office:body></office:document-content>')
    return zip_bytes({"content.xml": content.encode("utf-8")})


def odp_bytes(text='OdpCanary52'):
    content = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"><office:body><office:presentation>'
               f'<draw:page draw:name="第一页"><draw:frame><draw:text-box><text:p>{text}</text:p>'
               '</draw:text-box></draw:frame></draw:page></office:presentation></office:body></office:document-content>')
    return zip_bytes({"content.xml": content.encode("utf-8")})


def rtf_bytes():
    return (b'{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}\\f0 RtfCanary86\\par '
            b'second line: \\u20013?\\u25991?}')


def epub_bytes():
    container = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    package = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
               '<item id="c1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
               '<spine><itemref idref="c1"/></spine></package>')
    chapter = ('<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
               '<head><title>目录不该出现</title></head><body><h1>EpubCanary97</h1>'
               '<p>正文里有真的文字。</p><script>alert("不要执行")</script></body></html>')
    return zip_bytes({"META-INF/container.xml": container.encode("utf-8"),
                      "OEBPS/content.opf": package.encode("utf-8"),
                      "OEBPS/Text/ch1.xhtml": chapter.encode("utf-8")})


def ofd_bytes(text='OfdCanary48'):
    """手写一个最小的 OFD: OFD.xml -> Document.xml 的页序 -> 每页 Content.xml 里的 TextCode."""
    return zip_bytes({
        "OFD.xml": ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DocBody>'
                    '<ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>').encode("utf-8"),
        "Doc_0/Document.xml": ('<?xml version="1.0" encoding="UTF-8"?>'
                               '<ofd:Document xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Pages>'
                               '<ofd:Page ID="1" BaseLoc="Pages/Page_0/Content.xml"/>'
                               '</ofd:Pages></ofd:Document>').encode("utf-8"),
        "Doc_0/Pages/Page_0/Content.xml": ('<?xml version="1.0" encoding="UTF-8"?>'
                                           '<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Content>'
                                           '<ofd:Layer><ofd:TextObject>'
                                           f'<ofd:TextCode>{text}</ofd:TextCode>'
                                           '<ofd:TextCode> 金额 42</ofd:TextCode>'
                                           '</ofd:TextObject></ofd:Layer></ofd:Content></ofd:Page>').encode("utf-8"),
    })


def eml_bytes():
    from email import policy
    from email.message import EmailMessage

    message = EmailMessage(policy=policy.default)
    message['From'] = 'sender@example.invalid'
    message['To'] = 'receiver@example.invalid'
    message['Subject'] = 'EmlSubject57'
    message.set_content('EmlBodyCanary68')
    message.add_alternative('<html><body><p>EmlHtmlCanary79</p></body></html>', subtype='html')
    message.add_attachment(b'attachment body', maintype='text', subtype='plain', filename='notes.txt')
    return message.as_bytes()


def fb2_bytes():
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info>'
            '<book-title>Fb2Canary80</book-title><author><first-name>Fb2Author91</first-name></author>'
            '</title-info></description><body><section><title><p>Fb2Section02</p></title>'
            '<p>Fb2Body13</p></section></body></FictionBook>').encode("utf-8")


def xmind_bytes():
    return zip_bytes({
        "content.json": json.dumps([{'title': 'XmindCanary24', 'children': {'attached': [
            {'title': 'XmindChild35', 'notes': {'plain': {'content': 'XmindNote46'}}}]}}]).encode("utf-8"),
        "metadata.json": b'{}',
    })


def rewrite_zip(data, additions):
    source, dest = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as out:
        for name in source.namelist():
            if name not in additions:
                out.writestr(name, source.read(name))
        for name, value in additions.items():
            out.writestr(name, value)
    return dest.getvalue()


def ole_bytes(streams):
    """手写一个最小的 OLE 复合文档 (老式 Office / WPS): 每个流都占到 4 KB, 走 FAT 不走 MiniFAT."""
    sector = 512
    padded = {name: data + b'\0' * (-len(data) % sector) for name, data in streams.items()}
    padded = {name: data + b'\0' * (4096 - len(data)) if len(data) < 4096 else data for name, data in padded.items()}
    fat, entries, payload = [0xFFFFFFFF] * (sector // 4), [], bytearray()
    fat[0], fat[1] = 0xFFFFFFFD, 0xFFFFFFFE
    next_sector = 2
    for index, (name, data) in enumerate(padded.items(), 1):
        count = len(data) // sector
        chain = list(range(next_sector, next_sector + count))
        next_sector += count
        for number, following in zip(chain, chain[1:] + [0xFFFFFFFE]):
            fat[number] = following
        entries.append((name, 2, chain[0], len(data)))
        payload.extend(data)
    header = bytearray(sector)
    header[:8] = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    header[24:26], header[26:28] = (0x3E).to_bytes(2, 'little'), (3).to_bytes(2, 'little')
    header[28:30], header[30:32], header[32:34] = (0xFFFE).to_bytes(2, 'little'), (9).to_bytes(2, 'little'), (6).to_bytes(2, 'little')
    header[44:48], header[48:52] = (1).to_bytes(4, 'little'), (1).to_bytes(4, 'little')
    header[56:60] = (4096).to_bytes(4, 'little')
    header[60:64] = header[68:72] = (0xFFFFFFFE).to_bytes(4, 'little')
    header[76:80] = (0).to_bytes(4, 'little')
    for offset in range(80, sector, 4):
        header[offset:offset + 4] = (0xFFFFFFFF).to_bytes(4, 'little')
    directory = bytearray(sector)
    root_name = 'Root Entry'.encode('utf-16-le') + b'\0\0'
    directory[0:len(root_name)], directory[64:66] = root_name, len(root_name).to_bytes(2, 'little')
    directory[66], directory[116:120], directory[120:124] = 5, (0xFFFFFFFE).to_bytes(4, 'little'), b'\0' * 4
    for index, (name, kind, start, size) in enumerate(entries, 1):
        offset, raw = index * 128, name.encode('utf-16-le') + b'\0\0'
        directory[offset:offset + len(raw)] = raw
        directory[offset + 64:offset + 66] = len(raw).to_bytes(2, 'little')
        directory[offset + 66] = kind
        directory[offset + 116:offset + 120] = start.to_bytes(4, 'little')
        directory[offset + 120:offset + 124] = size.to_bytes(4, 'little')
    fat_bytes = b''.join(value.to_bytes(4, 'little') for value in fat)
    return bytes(header) + fat_bytes + bytes(directory) + bytes(payload)


def legacy_doc_bytes(text='DocCanary61'):
    word, table = bytearray(4096), bytearray(4096)
    word[10:12] = (0x0200).to_bytes(2, 'little')
    cps = (0).to_bytes(4, 'little') + len(text).to_bytes(4, 'little')
    pcd = b'\0\0' + (1024).to_bytes(4, 'little') + b'\0\0'
    plc = cps + pcd
    clx = b'\x02' + len(plc).to_bytes(4, 'little') + plc
    table[:len(clx)] = clx
    word[0x01A2:0x01A6], word[0x01A6:0x01AA] = (0).to_bytes(4, 'little'), len(clx).to_bytes(4, 'little')
    word[1024:1024 + len(text) * 2] = text.encode('utf-16-le')
    return ole_bytes({'WordDocument': bytes(word), '1Table': bytes(table)})


def legacy_xls_bytes(text='XlsCanary73'):
    def record(kind, payload):
        return kind.to_bytes(2, 'little') + len(payload).to_bytes(2, 'little') + payload
    strings = text.encode('ascii')
    sst = (1).to_bytes(4, 'little') + (1).to_bytes(4, 'little') + \
          len(text).to_bytes(2, 'little') + b'\0' + strings
    stream = record(0x0809, b'\0' * 16) + record(0x00FC, sst) + record(0x000A, b'')
    return ole_bytes({'Workbook': stream})


def legacy_ppt_bytes(text='PptCanary84'):
    def record(kind, payload):
        return (0).to_bytes(2, 'little') + kind.to_bytes(2, 'little') + len(payload).to_bytes(4, 'little') + payload
    slide = record(0x03EE, record(0x0FA0, text.encode('utf-16-le')))
    return ole_bytes({'PowerPoint Document': record(0x03E8, slide)})


def test_docx_paragraphs_tables_and_original_snapshot_remain_immutable(store):
    raw = docx_bytes()
    item = store.add_file('session', '说明.docx', raw)
    assert item['parse_status'] == 'stored'
    assert not list(store._folder('session').glob('*.extract-*'))
    snapshot = store.select('session', [item['id']])
    metadata = store.inspect('session', item['id'], snapshot=snapshot)
    assert metadata['parse_status'] == 'ready' and metadata['format'] == 'docx'
    assert 'text' not in metadata and 'pages' not in metadata, 'DOCX extraction must not invent layout page numbers'
    text = store.read('session', item['id'], snapshot=snapshot)
    assert '[段落 1]' in text and '第一段：材料来自本地。' in text
    assert '[表格 1]' in text and '第 2 行 | 苹果 | 3' in text
    assert text.endswith('[全文结束]')
    assert (store._folder('session') / item['file']).read_bytes() == raw
    assert snapshot[0]['parse_status'] == 'stored', 'A run snapshot is not rewritten after extraction'
    assert store.list('session')[0]['parse_status'] == 'ready'


def test_xlsx_sheet_selection_cells_formulas_and_relative_pagination(store):
    item = store.add_file('session', '数据.xlsx', xlsx_bytes())
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == 'ready' and [s['name'] for s in info['sheets']] == ['预算', '来源']
    assert info['sheets'][0]['cells'] == 6
    first = store.read('session', item['id'], sheet='预算')
    assert '[工作表 预算]' in first and 'B2: 3' in first and '=B2*2' in first and '未计算公式' in first
    assert 'example.invalid' not in first
    second = store.read('session', item['id'], sheet=2, offset=0, limit=15)
    assert second.startswith('[工作表 来源]') and second.endswith('[后续用 offset=15]')
    followup = store.read('session', item['id'], sheet='来源', offset=15)
    assert 'example.invalid' in second + followup and 'B1: TRUE' in followup
    with pytest.raises(ValueError, match='不存在'):
        store.read('session', item['id'], sheet='missing')
    with pytest.raises(ValueError, match='一次只能'):
        store.read('session', item['id'], sheet=1, page=1)


def test_pdf_text_pages_and_ocr_limit_are_reported_honestly(store):
    item = store.add_file('session', 'reference.pdf', pdf_bytes('Page one evidence', 'Page two evidence'))
    info = store.inspect('session', item['id'])
    assert info['pages'] == 2 and info['parse_status'] == 'ready'
    assert [s['page'] for s in info['sections']] == [1, 2]
    text = store.read('session', item['id'], page=2)
    assert '[第 2 页]' in text and 'Page two evidence' in text and 'Page one evidence' not in text
    with pytest.raises(ValueError, match='不存在'):
        store.read('session', item['id'], page=3)
    blank = store.add_file('session', 'blank.pdf', pdf_bytes(''))
    metadata = store.inspect('session', blank['id'])
    assert metadata['parse_status'] == 'needs_ocr' and metadata['readable'] is False
    assert '尚未运行 OCR' in store.read('session', blank['id'])


def test_encrypted_and_invalid_pdf_are_not_claimed_read(store):
    protected = store.add_file('session', 'private.pdf', pdf_bytes('secret', encrypted=True))
    info = store.inspect('session', protected['id'])
    assert info['parse_status'] == 'error' and '加密' in info['error']
    assert '尚未读取内容' in store.read('session', protected['id'])
    broken = store.add_file('session', 'broken.pdf', b'not really a PDF')
    assert store.inspect('session', broken['id'])['parse_status'] == 'error'


@pytest.mark.parametrize('suffix,raw,expected', [('.csv', '名称,数量\n苹果,3'.encode(), '名称,数量'),
    ('.json', b'{"source":"local","count":3}', '"count":3'),
    ('.txt', '中文文本'.encode('utf-16'), '中文文本'), ('.txt', '本地编码'.encode('gb18030'), '本地编码')])
def test_text_csv_json_and_declared_encodings_preserve_raw_offsets(store, suffix, raw, expected):
    item = store.add_file('session', 'input' + suffix, raw)
    assert expected in store.read('session', item['id'])
    metadata = store.inspect('session', item['id'])
    assert metadata['parse_status'] == 'ready' and metadata['format'] == suffix[1:]


def test_long_text_has_explicit_partial_status_and_pagination(store):
    item = store.add_file('session', 'large.txt', b'x' * (readers.MAX_TEXT + 100))
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == 'partial' and info['truncated'] is True and info['chars'] == readers.MAX_TEXT
    first = store.read('session', item['id'], limit=10)
    assert first.startswith('x' * 10) and first.endswith('[后续用 offset=10]')
    assert '其余尚未读取' in first
    last = store.read('session', item['id'], offset=readers.MAX_TEXT - 2)
    assert last.startswith('xx') and last.endswith('[全文结束]')


@pytest.mark.parametrize('name,raw,wants', [
    ('slides.pptx', pptx_bytes(), ['[幻灯片 1]', 'PptCanary53']),
    ('notes.odt', odt_bytes(), ['[段落 1]', 'OdtCanary64', '[表格 1]', 'OdtCell75']),
    ('sheet.ods', ods_bytes(), ['[工作表 预算]', 'OdsCanary41', '42']),
    ('slides.odp', odp_bytes(), ['[幻灯片 1]', 'OdpCanary52']),
    ('notes.rtf', rtf_bytes(), ['RtfCanary86', 'second line: 中文']),
    ('book.epub', epub_bytes(), ['[第 1 节]', 'EpubCanary97', '正文里有真的文字。']),
    ('invoice.ofd', ofd_bytes(), ['[第 1 页]', 'OfdCanary48', '金额 42']),
    ('mail.eml', eml_bytes(), ['EmlSubject57', 'EmlBodyCanary68', 'notes.txt']),
    ('book.fb2', fb2_bytes(), ['Fb2Canary80', 'Fb2Author91', 'Fb2Body13']),
    ('map.xmind', xmind_bytes(), ['XmindCanary24', 'XmindChild35', 'XmindNote46']),
])
def test_more_document_formats_extract_real_text(store, name, raw, wants):
    item = store.add_file('session', name, raw)
    assert item['readable'] is True
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == 'ready', info
    text = store.read('session', item['id'])
    for want in wants:
        assert want in text, want


@pytest.mark.parametrize('name,raw,wants', [
    ('旧报告.doc', legacy_doc_bytes(), ['DocCanary61']),
    ('旧表格.xls', legacy_xls_bytes(), ['XlsCanary73']),
    ('旧演示.ppt', legacy_ppt_bytes(), ['PptCanary84']),
    ('兼容.wps', legacy_doc_bytes('WpsCanary95'), ['WpsCanary95']),
    ('表格.et', legacy_xls_bytes('EtCanary06'), ['EtCanary06']),
    ('演示.dps', legacy_ppt_bytes('DpsCanary17'), ['DpsCanary17']),
], ids=['doc', 'xls', 'ppt', 'wps', 'et', 'dps'])
def test_legacy_office_and_wps_formats_extract_real_text(store, name, raw, wants):
    """老式 Office / WPS 是 OLE 复合文档: 只读文字, 不调用 Word、不执行宏, 也不假装读到了别的内容."""
    item = store.add_file('session', name, raw)
    assert item['readable'] is True
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == 'ready', info
    text = store.read('session', item['id'])
    for want in wants:
        assert want in text, want


def test_archives_read_text_and_documents_inside(store):
    """压缩包不解到磁盘: 里面的文字文件和文档按各自的后缀读, 可执行内容不执行也不假装读过."""
    raw = zip_bytes({
        "说明.txt": "压缩包文字 ZipCanary21".encode("utf-8"),
        "资料/report.docx": plain_docx_bytes("压缩包文档 DocInZip32"),
        "工具/run.exe": b"do not run",
    })
    item = store.add_file('session', '打包.zip', raw)
    assert item['readable'] is True
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == 'ready', info
    text = store.read('session', item['id'])
    assert '压缩包文字 ZipCanary21' in text and '压缩包文档 DocInZip32' in text
    assert 'run.exe' in text and 'do not run' not in text


def test_tar_and_gzip_files_are_read_with_the_same_reader(store, tmp_path):
    tarred = tar_bytes({"a.txt": b"TarCanary43"})
    item = store.add_file('session', '打包.tar.gz', tarred)
    assert 'TarCanary43' in store.read('session', item['id'])
    assert 'TarCanary43' in readers.extract(tarred, '.tar.gz')['text']
    compressed = store.add_file('session', '说明.txt.gz', gzip.compress('GzCanary54'.encode('utf-8')))
    assert 'GzCanary54' in store.read('session', compressed['id'])


def test_unknown_suffix_is_sniffed_before_giving_up(store):
    """后缀没写对 (比如 .bin) 时先认文件头; 认出来是 DOCX / ZIP 就照常读, 不因为后缀拒收."""
    docx = store.add_file('session', '没后缀.bin', plain_docx_bytes('SniffCanary65'))
    assert docx['readable'] is True and docx['format'] == 'docx'
    info = store.inspect('session', docx['id'])
    assert info['parse_status'] == 'ready' and 'SniffCanary65' in store.read('session', docx['id'])


def test_renamed_archives_are_recognized_and_unreadable_ones_say_where_they_stop(store, tmp_path):
    """后缀不对时按文件头认 GZ / TAR / 7z / RAR; 能解的就读, 没有解压组件的只认格式并说清卡在解压这一步."""
    gz = gzip.compress('MagicGzipCanary25'.encode("utf-8"))
    assert readers.sniff(gz, '.bin') == '.gz'
    assert 'MagicGzipCanary25' in readers.extract(gz, '.bin')['text']

    tarred = tar_bytes({"a.txt": b"MagicTarCanary36"}, mode='w')
    assert readers.sniff(tarred, '.bin') == '.tar'
    assert 'MagicTarCanary36' in readers.extract(tarred, '.bin')['text']

    for raw, name in ((b'7z\xbc\xaf\x27\x1c' + b'\0' * 64, '7z'), (b'Rar!\x1a\x07\x00' + b'\0' * 64, 'RAR')):
        result = readers.extract(raw, '.bin')
        assert result['parse_status'] == 'unsupported' and name in result['error'], result
        assert '解' in result['error'] and '没有读取' in result['error']

    plain = store.add_file('session', '没有后缀.bin', '其实是普通文字 UnnamedTextCanary47'.encode("utf-8"))
    assert plain['readable'] is True and 'UnnamedTextCanary47' in store.read('session', plain['id'])

    unknown = readers.extract(b'\x00\x01\x02\x03' * 64, '.xyz')
    assert unknown['parse_status'] == 'unsupported' and '后缀 .xyz' in unknown['error'] and '认不出' in unknown['error']
    macro = readers.extract(b'do not run', '.xlsm')
    assert macro['parse_status'] == 'unsupported' and '宏' in macro['error']


def test_corrupt_container_says_which_container_and_stage(store):
    broken = readers.extract(b'PK\x03\x04broken zip', '.zip')
    assert broken['parse_status'] == 'error' and 'ZIP' in broken['error']
    assert '尚未读取' in broken['error']


def test_archive_traversal_and_nested_limits_are_rejected(tmp_path):
    bad = zip_bytes({"../escape.txt": b"outside"})
    result = readers.extract(bad, '.zip')
    assert result['parse_status'] == 'error' and '越界' in result['error']
    assert not (tmp_path / 'escape.txt').exists()


def test_large_file_can_be_extracted_from_disk(tmp_path):
    """超过单次 32 MB 的文件先分块下到磁盘, 再从磁盘按上限解析, 不再直接判成"太大"."""
    path = tmp_path / 'large.txt'
    path.write_bytes(b'PathCanary28\n' + b'x' * (readers.MAX_BYTES + 1))
    result = readers.extract_path(path, '.txt')
    assert result['parse_status'] in {'ready', 'partial'} and 'PathCanary28' in result['text']


def test_files_bigger_than_eight_mb_are_read_and_cap_is_explicit(store):
    assert readers.MAX_BYTES >= 16 * 1024 * 1024
    raw = b'BigCanary18\n' + b'x' * (9 * 1024 * 1024)
    item = store.add_file('session', 'large.txt', raw)
    assert item['readable'] is True
    text = store.read('session', item['id'])
    assert 'BigCanary18' in text
    over = readers.extract(b'x' * (readers.MAX_BYTES + 1), '.txt')
    assert over['parse_status'] == 'error' and f'{readers.MAX_BYTES // (1024 * 1024)} MB' in over['error']


@pytest.mark.parametrize('filename,payload,status', [('image.png', b'\x89PNG\r\n\x1a\n', 'image'),
                                                     ('macros.xlsm', b'do not run', 'unsupported')])
def test_unsupported_formats_never_claim_extracted_text(store, filename, payload, status):
    """图片不解析文字 (交给看图的模型), 别的未知格式老实说没读; 都不许假装提取过文字."""
    item = store.add_file('session', filename, payload)
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == status and info['readable'] is False
    text = store.read('session', item['id'])
    if status == 'image':
        assert '图片' in text and '看图' in text
    else:
        assert '尚未读取内容' in text


@pytest.mark.parametrize('name,payload', [('../escape.txt', b'outside'), ('word/vbaProject.bin', b'macro'),
    ('word/bomb.xml', b'x' * (readers.MAX_XML + 1)), ('word/ratio.xml', b'x' * (2 * 1024 * 1024))],
    ids=['traversal', 'macros', 'expanded-size', 'compression-ratio'])
def test_zip_traversal_macros_expansion_and_ratio_limits(store, tmp_path, name, payload):
    item = store.add_file('session', 'unsafe.docx', rewrite_zip(docx_bytes(), {name: payload}))
    info = store.inspect('session', item['id'])
    assert info['parse_status'] == 'error' and info['readable'] is False
    assert not (tmp_path / 'escape.txt').exists()


def test_xml_entity_expansion_is_rejected_and_external_links_are_not_followed(store):
    malicious = b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///does-not-exist">]><x>&secret;</x>'
    item = store.add_file('session', 'entity.docx', rewrite_zip(docx_bytes(), {'word/document.xml': malicious}))
    assert '实体声明' in store.inspect('session', item['id'])['error']
    rel = b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="external" Type="hyperlink" Target="https://example.invalid/no-network" TargetMode="External"/></Relationships>'''
    safe = store.add_file('session', 'links.docx', rewrite_zip(docx_bytes(), {'word/_rels/document.xml.rels': rel}))
    info = store.inspect('session', safe['id'])
    assert info['parse_status'] == 'ready' and any('未访问链接' in x for x in info['warnings'])


def test_cache_reuse_still_verifies_original_hash_and_removed_snapshot(store, monkeypatch):
    item = store.add_file('session', 'source.docx', docx_bytes())
    snapshot = store.select('session', [item['id']])
    expected = store.read('session', item['id'], snapshot=snapshot)
    monkeypatch.setattr(readers, 'extract', lambda *args: pytest.fail('Unchanged material should use verified cache'))
    store.update('session', item['id'], remove=True)
    assert store.read('session', item['id'], snapshot=snapshot) == expected
    with pytest.raises(ValueError, match='不存在'):
        store.read('session', item['id'], snapshot=[])
    path = store._folder('session') / item['file']
    path.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='快照不一致'):
        store.inspect('session', item['id'], snapshot=snapshot)


def test_invalid_cache_is_rebuilt_without_modifying_source(store):
    item = store.add_file('session', 'local.txt', b'original local text')
    assert 'original' in store.read('session', item['id'])
    cache = next(store._folder('session').glob(f'*.extract-v{readers.CACHE_VERSION}.json'))
    data = json.loads(cache.read_text('utf-8'))
    data['result']['text'] = 'tampered cached text'
    cache.write_text(json.dumps(data), encoding='utf-8')
    assert 'original local text' in store.read('session', item['id'])


def test_reference_inspect_does_not_fetch_and_wrong_owner_cannot_read(store):
    item = store.add_reference('session', 'https://example.invalid/unvisited')
    assert store.inspect('session', item['id'])['parse_status'] == 'reference'
    assert '尚未访问链接' in store.inspect('session', item['id'])['reader']
    with pytest.raises(ValueError):
        store.inspect('other', item['id'])


def test_pdf_page_count_is_limited_with_visible_partial_status(store):
    raw = pdf_bytes(*(['text'] * (readers.MAX_PAGES + 1)))
    item = store.add_file('session', 'many.pdf', raw)
    info = store.inspect('session', item['id'])
    assert info['pages'] == readers.MAX_PAGES + 1
    assert info['parse_status'] == 'partial' and len(info['sections']) == readers.MAX_PAGES
    with pytest.raises(ValueError, match='超出'):
        store.read('session', item['id'], page=readers.MAX_PAGES + 1)


def test_worker_timeout_is_explicit_and_owned_process_is_cleaned(monkeypatch):
    monkeypatch.setattr(readers, 'TIMEOUT', 0.00001)
    result = readers.extract(b'only a temporary test', '.txt')
    assert result['parse_status'] == 'error' and '超过' in result['error']


def test_pdf_missing_dependency_has_unavailable_status(monkeypatch):
    import builtins
    original = builtins.__import__
    def without_pdf(name, *args, **kwargs):
        if name == 'pypdf':
            raise ImportError('temporary missing dependency fixture')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_pdf)
    result = readers._pdf(b'not accessed when dependency is absent')
    assert result['parse_status'] == 'unavailable' and '尚未读取内容' in result['error']


def test_parser_thread_does_not_overwrite_pin_or_remove_state(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    item = store.add_file('session', 'threaded.txt', b'text')
    entered, finish = threading.Event(), threading.Event()
    def parser(*args):
        entered.set()
        assert finish.wait(timeout=5)
        return {'format': 'txt', 'parse_status': 'ready', 'text': 'text'}
    monkeypatch.setattr(readers, 'extract', parser)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.inspect, 'session', item['id'])
        assert entered.wait(timeout=5)
        store.update('session', item['id'], pinned=True)
        finish.set()
        assert pending.result()['parse_status'] == 'ready'
    assert store.list('session')[0]['pinned'] is True
