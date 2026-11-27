"""AUDIO B/C code converter: browser regression with entirely synthetic IDs.

No user spreadsheet contents, real identifiers, or mapping files are included.
"""
import io
import shutil
import zipfile
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

HTML = Path(__file__).resolve().parents[1] / "docs" / "site" / "id-codec.html"
CHROME = next((x for n in ("google-chrome", "chromium", "chromium-browser")
               if (x := shutil.which(n))), None)


def fake_excel() -> bytes:
    """Small ZIP-based .xlsx worksheet, with synthetic B/C and ignored D columns."""
    rows = (
        '<row r="1"><c r="B1" t="inlineStr"><is><t>序號</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>轉碼後ID</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>其他資料</t></is></c></row>'
        '<row r="2"><c r="B2" t="inlineStr"><is><t>00000011</t></is></c>'
        '<c r="C2"><v>7000011</v></c>'
        '<c r="D2" t="inlineStr"><is><t>不使用</t></is></c></row>'
        '<row r="3"><c r="B3"><v>12</v></c><c r="C3"><v>9000012</v></c></row>'
    )
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + rows + '</sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/worksheets/sheet1.xml', xml)
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
    return buf.getvalue()


@pytest.mark.skipif(not CHROME, reason="Chromium browser unavailable")
def test_codec_csv_both_directions_and_privacy():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(HTML.as_uri())
        assert page.get_by_text("資料僅在本機處理").count() == 1
        page.locator("#file").set_input_files({
            "name": "fake.csv", "mimeType": "text/csv",
            "buffer": b',\xe5\xba\x8f\xe8\x99\x9f,\xe8\xbd\x89\xe7\xa2\xbc\xe5\xbe\x8cID,\xe5\x85\xb6\xe4\xbb\x96\xe6\xac\x84\xe4\xbd\x8d\n0,00000001,7000001,ignored\n1,00000002,9000002,ignored\n',
        })
        page.wait_for_function("document.getElementById('count').textContent === '2'")
        page.locator("#singleIn").fill("1")
        page.locator("#convertOne").click()
        assert page.locator("#singleOut").input_value() == "7000001"
        page.locator("#toB").click()
        page.locator("#singleIn").fill("9000002")
        page.locator("#convertOne").click()
        assert page.locator("#singleOut").input_value() == "00000002"
        page.locator("#batchIn").fill("7000001\n9999999")
        page.locator("#convertBatch").click()
        assert page.locator("#resultRows tr").count() == 2
        assert "未收錄" in page.locator("#resultRows tr").nth(1).inner_text()
        page.locator("#clearData").click()
        assert page.locator("#count").inner_text() == "0"
        assert page.locator("#convertOne").is_disabled()
        assert not errors, errors
        browser.close()


@pytest.mark.skipif(not CHROME, reason="Chromium browser unavailable")
def test_codec_xlsx_without_d_column_and_reverse():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(HTML.as_uri())
        page.locator("#file").set_input_files({
            "name": "fake.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": fake_excel(),
        })
        page.wait_for_function("document.getElementById('count').textContent === '2'")
        page.locator("#singleIn").fill("00000011")
        page.locator("#convertOne").click()
        assert page.locator("#singleOut").input_value() == "7000011"
        page.locator("#toB").click()
        page.locator("#singleIn").fill("9000012")
        page.locator("#convertOne").click()
        assert page.locator("#singleOut").input_value() == "00000012"
        assert not errors, errors
        browser.close()
