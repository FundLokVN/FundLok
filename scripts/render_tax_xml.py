#!/usr/bin/env python3
"""
Vietnamese Tax Financial Statements (BCTC TT133/2016/TT-BTC) XML to HTML Renderer.
Renders files like B02-DN-2025.xml into a clean, printable, interactive HTML report.
"""
from __future__ import annotations

import argparse
import html
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

ACCOUNT_NAMES = {
    "111": "Tiền mặt",
    "1111": "Tiền Việt Nam",
    "1112": "Ngoại tệ",
    "112": "Tiền gửi ngân hàng",
    "1121": "Tiền Việt Nam",
    "1122": "Ngoại tệ",
    "121": "Chứng khoán kinh doanh",
    "128": "Đầu tư nắm giữ đến ngày đáo hạn",
    "1281": "Tiền gửi có kỳ hạn",
    "1288": "Các khoản đầu tư nắm giữ đến ngày đáo hạn khác",
    "131": "Phải thu của khách hàng",
    "133": "Thuế GTGT được khấu trừ",
    "1331": "Thuế GTGT được khấu trừ của HHDV",
    "1332": "Thuế GTGT được khấu trừ của TSCĐ",
    "136": "Phải thu nội bộ",
    "138": "Phải thu khác",
    "1381": "Tài sản thiếu chờ xử lý",
    "1388": "Phải thu khác",
    "141": "Tạm ứng",
    "151": "Hàng mua đang đi đường",
    "152": "Nguyên liệu, vật liệu",
    "153": "Công cụ, dụng cụ",
    "154": "Chi phí SXKD dở dang",
    "155": "Thành phẩm",
    "156": "Hàng hóa",
    "1561": "Giá mua hàng hóa",
    "1562": "Chi phí thu mua hàng hóa",
    "211": "Tài sản cố định hữu hình",
    "2111": "Nhà cửa, vật kiến trúc",
    "2112": "Máy móc, thiết bị",
    "2113": "Phương tiện vận tải, truyền dẫn",
    "2114": "Thiết bị, dụng cụ quản lý",
    "2118": "TSCĐ khác",
    "214": "Hao mòn TSCĐ",
    "2141": "Hao mòn TSCĐ hữu hình",
    "217": "Bất động sản đầu tư",
    "228": "Đầu tư góp vốn vào đơn vị khác",
    "229": "Dự phòng tổn thất tài sản",
    "241": "Xây dựng cơ bản dở dang",
    "242": "Chi phí trả trước",
    "331": "Phải trả cho người bán",
    "333": "Thuế và các khoản phải nộp Nhà nước",
    "3331": "Thuế GTGT phải nộp",
    "33311": "Thuế GTGT đầu ra",
    "33312": "Thuế GTGT hàng nhập khẩu",
    "3332": "Thuế tiêu thụ đặc biệt",
    "3333": "Thuế xuất, nhập khẩu",
    "3334": "Thuế thu nhập doanh nghiệp",
    "3335": "Thuế thu nhập cá nhân",
    "3338": "Thuế tài nguyên, thuế khác",
    "334": "Phải trả người lao động",
    "335": "Chi phí phải trả",
    "336": "Phải trả nội bộ",
    "338": "Phải trả, phải nộp khác",
    "3382": "Kinh phí công đoàn",
    "3383": "Bảo hiểm xã hội",
    "3384": "Bảo hiểm y tế",
    "3386": "Bảo hiểm thất nghiệp",
    "3388": "Phải trả, phải nộp khác",
    "341": "Vay và nợ thuê tài chính",
    "3411": "Các khoản đi vay",
    "3412": "Nợ thuê tài chính",
    "353": "Quỹ khen thưởng, phúc lợi",
    "411": "Vốn đầu tư của chủ sở hữu",
    "4111": "Vốn góp của chủ sở hữu",
    "4112": "Thặng dư vốn cổ phần",
    "4118": "Vốn khác",
    "413": "Chênh lệch tỷ giá hối đoái",
    "418": "Các quỹ thuộc vốn chủ sở hữu",
    "421": "Lợi nhuận sau thuế chưa phân phối",
    "4211": "LNST chưa phân phối năm trước",
    "4212": "LNST chưa phân phối năm nay",
    "511": "Doanh thu bán hàng và CCDV",
    "5111": "Doanh thu bán hàng hóa",
    "5112": "Doanh thu bán thành phẩm",
    "5113": "Doanh thu cung cấp dịch vụ",
    "515": "Doanh thu hoạt động tài chính",
    "632": "Giá vốn hàng bán",
    "635": "Chi phí tài chính",
    "642": "Chi phí quản lý kinh doanh",
    "6421": "Chi phí bán hàng",
    "6422": "Chi phí quản lý doanh nghiệp",
    "711": "Thu nhập khác",
    "811": "Chi phí khác",
    "821": "Chi phí thuế thu nhập doanh nghiệp",
    "911": "Xác định kết quả kinh doanh",
}

B01A_ITEMS = [
    # (code, name, is_header, level)
    ("TÀI SẢN", "TÀI SẢN", True, 0),
    ("110", "I. Tiền và các khoản tương đương tiền", True, 1),
    ("120", "II. Đầu tư tài chính", True, 1),
    ("130", "III. Các khoản phải thu", True, 1),
    ("131", "1. Phải thu thương mại", False, 2),
    ("132", "2. Trả trước cho người bán", False, 2),
    ("134", "3. Phải thu khác", False, 2),
    ("135", "4. Dự phòng phải thu khó đòi (*)", False, 2),
    ("140", "IV. Hàng tồn kho", True, 1),
    ("141", "Hàng tồn kho", False, 2),
    ("142", "Dự phòng giảm giá hàng tồn kho (*)", False, 2),
    ("150", "V. Tài sản cố định", True, 1),
    ("151", "- Nguyên giá", False, 2),
    ("152", "- Giá trị hao mòn luỹ kế (*)", False, 2),
    ("160", "VI. Bất động sản đầu tư", True, 1),
    ("170", "VII. Xây dựng cơ bản dở dang", True, 1),
    ("180", "VIII. Tài sản khác", True, 1),
    ("181", "1. Thuế GTGT được khấu trừ", False, 2),
    ("182", "2. Tài sản khác", False, 2),
    ("200", "TỔNG CỘNG TÀI SẢN (200 = 110 + 120 + 130 + 140 + 150 + 160 + 170 + 180)", True, 0),
    ("NGUỒN VỐN", "NGUỒN VỐN", True, 0),
    ("300", "A. NỢ PHẢI TRẢ", True, 1),
    ("311", "1. Phải trả người bán", False, 2),
    ("312", "2. Người mua trả tiền trước", False, 2),
    ("313", "3. Thuế và các khoản phải nộp Nhà nước", False, 2),
    ("314", "4. Phải trả người lao động", False, 2),
    ("315", "5. Chi phí phải trả", False, 2),
    ("316", "6. Vay và nợ thuê tài chính", False, 2),
    ("317", "7. Phải trả khác", False, 2),
    ("318", "8. Quỹ khen thưởng, phúc lợi", False, 2),
    ("319", "9. Dự phòng phải trả", False, 2),
    ("400", "B. VỐN CHỦ SỞ HỮU", True, 1),
    ("411", "1. Vốn đầu tư của chủ sở hữu", False, 2),
    ("412", "2. Thặng dư vốn cổ phần", False, 2),
    ("413", "3. Vốn khác", False, 2),
    ("414", "4. Cổ phiếu quỹ (*)", False, 2),
    ("415", "5. Chênh lệch tỷ giá hối đoái", False, 2),
    ("416", "6. Các quỹ thuộc vốn chủ sở hữu", False, 2),
    ("417", "7. Lợi nhuận sau thuế chưa phân phối", False, 2),
    ("500", "TỔNG CỘNG NGUỒN VỐN (500 = 300 + 400)", True, 0),
]

B02_ITEMS = [
    ("01", "1. Doanh thu bán hàng và cung cấp dịch vụ", True, 0),
    ("02", "2. Các khoản giảm trừ doanh thu", False, 0),
    ("10", "3. Doanh thu thuần về bán hàng và cung cấp dịch vụ (10 = 01 - 02)", True, 0),
    ("11", "4. Giá vốn hàng bán", False, 0),
    ("20", "5. Lợi nhuận gộp về bán hàng và cung cấp dịch vụ (20 = 10 - 11)", True, 0),
    ("21", "6. Doanh thu hoạt động tài chính", False, 0),
    ("22", "7. Chi phí tài chính", False, 0),
    ("23", "- Trong đó: Chi phí lãi vay", False, 1),
    ("24", "8. Chi phí quản lý kinh doanh", False, 0),
    ("30", "9. Lợi nhuận thuần từ hoạt động kinh doanh (30 = 20 + 21 - 22 - 24)", True, 0),
    ("31", "10. Thu nhập khác", False, 0),
    ("32", "11. Chi phí khác", False, 0),
    ("40", "12. Lợi nhuận khác (40 = 31 - 32)", True, 0),
    ("50", "13. Tổng lợi nhuận kế toán trước thuế (50 = 30 + 40)", True, 0),
    ("51", "14. Chi phí thuế TNDN hiện hành", False, 0),
    ("60", "15. Lợi nhuận sau thuế thu nhập doanh nghiệp (60 = 50 - 51)", True, 0),
]

B03A_ITEMS = [
    ("I", "I. Lưu chuyển tiền từ hoạt động kinh doanh", True, 0),
    ("01", "1. Tiền thu từ bán hàng, cung cấp dịch vụ và doanh thu khác", False, 1),
    ("02", "2. Tiền chi trả cho người cung cấp hàng hóa và dịch vụ", False, 1),
    ("03", "3. Tiền chi trả cho người lao động", False, 1),
    ("04", "4. Tiền chi trả lãi vay", False, 1),
    ("05", "5. Tiền chi nộp thuế thu nhập doanh nghiệp", False, 1),
    ("06", "6. Tiền thu khác từ hoạt động kinh doanh", False, 1),
    ("07", "7. Tiền chi khác cho hoạt động kinh doanh", False, 1),
    ("20", "Lưu chuyển tiền thuần từ hoạt động kinh doanh", True, 0),
    ("II", "II. Lưu chuyển tiền từ hoạt động đầu tư", True, 0),
    ("21", "1. Tiền chi để mua sắm, xây dựng TSCĐ và các TSDH khác", False, 1),
    ("22", "2. Tiền thu từ thanh lý, nhượng bán TSCĐ và các TSDH khác", False, 1),
    ("23", "3. Tiền chi cho vay, mua các công cụ nợ của đơn vị khác", False, 1),
    ("24", "4. Tiền thu hồi cho vay, bán lại các công cụ nợ của đơn vị khác", False, 1),
    ("25", "5. Tiền chi đầu tư góp vốn vào đơn vị khác", False, 1),
    ("26", "6. Tiền thu hồi đầu tư góp vốn vào đơn vị khác", False, 1),
    ("27", "7. Tiền thu lãi cho vay, cổ tức và lợi nhuận được chia", False, 1),
    ("30", "Lưu chuyển tiền thuần từ hoạt động đầu tư", True, 0),
    ("III", "III. Lưu chuyển tiền từ hoạt động tài chính", True, 0),
    ("31", "1. Tiền thu từ phát hành cổ phiếu, nhận vốn góp của CSH", False, 1),
    ("32", "2. Tiền trả lại vốn góp cho CSH, mua lại cổ phiếu đã phát hành", False, 1),
    ("33", "3. Tiền vay ngắn hạn, dài hạn nhận được", False, 1),
    ("34", "4. Tiền chi trả nợ gốc vay", False, 1),
    ("35", "5. Tiền chi trả nợ thuê tài chính (hoặc cổ tức, LN đã trả CSH)", False, 1),
    ("40", "Lưu chuyển tiền thuần từ hoạt động tài chính", True, 0),
    ("50", "Lưu chuyển tiền thuần trong kỳ (50 = 20 + 30 + 40)", True, 0),
    ("60", "Tiền và tương đương tiền đầu kỳ", True, 0),
    ("61", "Ảnh hưởng của thay đổi tỷ giá hối đoái quy đổi ngoại tệ", False, 0),
    ("70", "Tiền và tương đương tiền cuối kỳ (70 = 50 + 60 + 61)", True, 0),
]


def fmt_vnd(val: int | float | None) -> str:
    if val is None or val == "":
        return "-"
    try:
        n = int(round(float(val)))
        if n == 0:
            return "0"
        return f"{n:,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(val)


def render_xml_to_html(xml_path: str, output_path: str | None = None) -> str:
    with open(xml_path, "r", encoding="utf-8", errors="replace") as f:
        xml_content = f.read()

    root = ET.fromstring(xml_content)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def find_node(elem: ET.Element | None, *path: str) -> ET.Element | None:
        curr = elem
        for p in path:
            if curr is None:
                return None
            curr = next((c for c in curr.iter() if local(c.tag) == p and c is not curr), None)
        return curr

    def text_of(*path: str) -> str:
        n = find_node(root, *path)
        return (n.text or "").strip() if n is not None and n.text else ""

    # Meta
    company_name = text_of("tenNNT") or "DOANH NGHIỆP"
    tax_code = text_of("mst") or ""
    address = text_of("dchiNNT") or ""
    phone = text_of("dthoaiNNT") or ""
    email = text_of("emailNNT") or ""
    tax_dept = text_of("tenCQTNoiNop") or ""
    fiscal_year = text_of("kyKKhai") or ""
    from_date = text_of("kyKKhaiTuNgay") or ""
    to_date = text_of("kyKKhaiDenNgay") or ""
    report_name = text_of("tenTKhai") or "Bộ Báo Cáo Tài Chính"
    report_desc = text_of("moTaBMau") or ""
    filing_date = text_of("ngayLapTKhai") or ""
    signer = text_of("nguoiKy") or text_of("nguoiDaiDienTheoPhapLuat") or ""
    legal_rep = text_of("nguoiDaiDienTheoPhapLuat") or signer

    # Digital signature
    sig_time = text_of("SigningTime") or ""
    sig_subj = text_of("X509SubjectName") or ""
    is_signed = "Signature" in xml_content

    # 1. B01a Data
    b01_cn_node = find_node(root, "CTieuTKhaiChinh", "SoCuoiNam")
    b01_dn_node = find_node(root, "CTieuTKhaiChinh", "SoDauNam")
    b01_cn = {local(c.tag): c.text for c in b01_cn_node} if b01_cn_node is not None else {}
    b01_dn = {local(c.tag): c.text for c in b01_dn_node} if b01_dn_node is not None else {}

    # 2. B02 Data
    b02_nn_node = find_node(root, "PL_KQHDSXKD", "NamNay")
    b02_nt_node = find_node(root, "PL_KQHDSXKD", "NamTruoc")
    b02_nn = {local(c.tag): c.text for c in b02_nn_node} if b02_nn_node is not None else {}
    b02_nt = {local(c.tag): c.text for c in b02_nt_node} if b02_nt_node is not None else {}

    # 3. B03a Data
    b03_nn_node = find_node(root, "PL_LCTTTT", "NamNay")
    b03_nt_node = find_node(root, "PL_LCTTTT", "NamTruoc")
    b03_nn = {local(c.tag): c.text for c in b03_nn_node} if b03_nn_node is not None else {}
    b03_nt = {local(c.tag): c.text for c in b03_nt_node} if b03_nt_node is not None else {}

    # 4. CDTK Data
    cdtk_elem = find_node(root, "PL_CDTK")
    trial_rows = []
    if cdtk_elem is not None:
        def get_acc_dict(p_tag: str, c_tag: str) -> dict[str, int]:
            p = next((c for c in cdtk_elem if local(c.tag) == p_tag), None)
            if p is None: return {}
            c = next((k for k in p if local(k.tag) == c_tag), None)
            if c is None: return {}
            return {local(x.tag)[2:]: int(x.text or 0) for x in c if x.text}

        sd_dk_no = get_acc_dict("SoDuDauKy", "No")
        sd_dk_co = get_acc_dict("SoDuDauKy", "Co")
        ps_no = get_acc_dict("SoPhatSinhTrongKy", "No")
        ps_co = get_acc_dict("SoPhatSinhTrongKy", "Co")
        sd_ck_no = get_acc_dict("SoDuCuoiKy", "No")
        sd_ck_co = get_acc_dict("SoDuCuoiKy", "Co")

        all_accs = sorted(
            list(set(sd_dk_no.keys()) | set(sd_dk_co.keys()) | set(ps_no.keys()) | set(ps_co.keys()) | set(sd_ck_no.keys()) | set(sd_ck_co.keys())),
            key=lambda x: (x[:3], len(x), x)
        )
        for acc in all_accs:
            v_dk_no = sd_dk_no.get(acc, 0)
            v_dk_co = sd_dk_co.get(acc, 0)
            v_ps_no = ps_no.get(acc, 0)
            v_ps_co = ps_co.get(acc, 0)
            v_ck_no = sd_ck_no.get(acc, 0)
            v_ck_co = sd_ck_co.get(acc, 0)
            if any([v_dk_no, v_dk_co, v_ps_no, v_ps_co, v_ck_no, v_ck_co]):
                trial_rows.append({
                    "acc": acc,
                    "name": ACCOUNT_NAMES.get(acc, f"Tài khoản {acc}"),
                    "dk_no": v_dk_no,
                    "dk_co": v_dk_co,
                    "ps_no": v_ps_no,
                    "ps_co": v_ps_co,
                    "ck_no": v_ck_no,
                    "ck_co": v_ck_co,
                    "is_sub": len(acc) > 3
                })

    # Key Financial Highlights
    rev = b02_nn.get("ct10") or b02_nn.get("ct01") or "0"
    cogs = b02_nn.get("ct11") or "0"
    net_profit = b02_nn.get("ct60") or "0"
    total_assets = b01_cn.get("ct200") or "0"
    equity = b01_cn.get("ct400") or "0"
    liabilities = b01_cn.get("ct300") or "0"

    html_page = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(company_name)} - {html.escape(report_name)} ({fiscal_year})</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --primary: #0284c7;
    --primary-dark: #0369a1;
    --bg-page: #f8fafc;
    --surface: #ffffff;
    --border: #e2e8f0;
    --text-main: #0f172a;
    --text-muted: #64748b;
    --success: #16a34a;
    --badge-bg: #e0f2fe;
    --badge-text: #0369a1;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background-color: var(--bg-page);
    color: var(--text-main);
    line-height: 1.5;
    padding: 24px;
  }}
  .container {{
    max-width: 1200px;
    margin: 0 auto;
  }}
  .top-bar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
    flex-wrap: wrap;
    gap: 16px;
  }}
  .badge-signed {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #dcfce7;
    color: #15803d;
    padding: 6px 14px;
    border-radius: 9999px;
    font-size: 13px;
    font-weight: 600;
  }}
  .btn-print {{
    background: var(--primary);
    color: #fff;
    border: none;
    padding: 8px 18px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 4px rgba(2, 132, 199, 0.2);
    transition: all 0.2s;
  }}
  .btn-print:hover {{ background: var(--primary-dark); }}

  /* Cards */
  .card {{
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--border);
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    margin-bottom: 24px;
    overflow: hidden;
  }}
  .card-header {{
    padding: 24px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(to right, #ffffff, #f0f9ff);
  }}
  .company-title {{
    font-size: 22px;
    font-weight: 700;
    color: #0f172a;
    margin-bottom: 8px;
  }}
  .meta-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
    font-size: 13.5px;
    color: var(--text-muted);
  }}
  .meta-grid strong {{ color: var(--text-main); }}

  /* KPI highlights */
  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    padding: 20px 24px;
    background: #ffffff;
    border-bottom: 1px solid var(--border);
  }}
  .kpi-card {{
    background: #f8fafc;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 18px;
  }}
  .kpi-label {{
    font-size: 12px;
    color: var(--text-muted);
    font-weight: 500;
    margin-bottom: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .kpi-val {{
    font-size: 18px;
    font-weight: 700;
    color: #0284c7;
  }}
  .kpi-val.positive {{ color: #16a34a; }}

  /* Tabs */
  .tabs-nav {{
    display: flex;
    gap: 4px;
    background: #e2e8f0;
    padding: 6px;
    border-radius: 10px;
    margin-bottom: 20px;
    overflow-x: auto;
  }}
  .tab-btn {{
    flex: 1;
    min-width: 180px;
    background: transparent;
    border: none;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-muted);
    border-radius: 8px;
    cursor: pointer;
    text-align: center;
    white-space: nowrap;
    transition: all 0.2s;
  }}
  .tab-btn.active {{
    background: #ffffff;
    color: var(--primary);
    box-shadow: 0 2px 4px rgba(0,0,0,0.06);
  }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

  /* Tables */
  .table-wrap {{
    overflow-x: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13.5px;
  }}
  th, td {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }}
  th {{
    background: #f1f5f9;
    font-weight: 600;
    color: #334155;
    text-align: left;
    position: sticky;
    top: 0;
  }}
  th.num, td.num {{ text-align: right; }}
  th.code, td.code {{ text-align: center; width: 80px; }}
  tr.header-row td {{
    background: #f8fafc;
    font-weight: 700;
    color: #1e293b;
  }}
  tr.sub-row td.item-name {{
    padding-left: 32px;
    color: #475569;
  }}
  tr:hover td {{
    background-color: #f8fafc;
  }}

  /* Sign footer */
  .signature-box {{
    padding: 24px;
    background: #f8fafc;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 20px;
  }}
  .cert-info {{
    font-size: 12.5px;
    color: var(--text-muted);
    line-height: 1.6;
  }}
  .stamp-box {{
    border: 2px dashed #16a34a;
    border-radius: 8px;
    padding: 12px 18px;
    background: #f0fdf4;
    color: #15803d;
    text-align: center;
    font-size: 13px;
    font-weight: 600;
  }}

  @media print {{
    body {{ background: #fff; padding: 0; }}
    .top-bar, .tabs-nav, .btn-print {{ display: none !important; }}
    .tab-content {{ display: block !important; page-break-after: always; }}
    .card {{ border: none; box-shadow: none; }}
  }}
</style>
</head>
<body>

<div class="container">
  <div class="top-bar">
    <div style="display: flex; align-items: center; gap: 12px;">
      <span class="badge-signed">✓ Hồ sơ thuế có Chữ ký số điện tử hợp lệ</span>
      <span style="font-size: 13px; color: #64748b;">Mã tờ khai: 683 | TT133/2016/TT-BTC</span>
    </div>
    <button class="btn-print" onclick="window.print()">🖨️ In Báo Cáo / Xuất PDF</button>
  </div>

  <div class="card">
    <div class="card-header">
      <div style="font-size: 13px; color: var(--primary); font-weight: 600; text-transform: uppercase; margin-bottom: 4px;">
        {html.escape(report_name)} (Kỳ {fiscal_year})
      </div>
      <h1 class="company-title">{html.escape(company_name)}</h1>
      <div class="meta-grid">
        <div><strong>Mã số thuế:</strong> {html.escape(tax_code)}</div>
        <div><strong>Cơ quan thuế:</strong> {html.escape(tax_dept)}</div>
        <div><strong>Kỳ kế toán:</strong> {from_date} đến {to_date}</div>
        <div><strong>Ngày ký nộp:</strong> {filing_date or sig_time[:10]}</div>
        <div><strong>Địa chỉ:</strong> {html.escape(address)}</div>
        <div><strong>Người đại diện:</strong> {html.escape(legal_rep)}</div>
      </div>
    </div>

    <!-- Quick Financial Metrics -->
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Doanh thu thuần (2025)</div>
        <div class="kpi-val">{fmt_vnd(rev)} <span style="font-size: 13px; font-weight: normal; color: #64748b;">VND</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Giá vốn hàng bán</div>
        <div class="kpi-val">{fmt_vnd(cogs)} <span style="font-size: 13px; font-weight: normal; color: #64748b;">VND</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Lợi nhuận sau thuế</div>
        <div class="kpi-val positive">{fmt_vnd(net_profit)} <span style="font-size: 13px; font-weight: normal; color: #64748b;">VND</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Tổng cộng Tài sản</div>
        <div class="kpi-val">{fmt_vnd(total_assets)} <span style="font-size: 13px; font-weight: normal; color: #64748b;">VND</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Vốn chủ sở hữu</div>
        <div class="kpi-val">{fmt_vnd(equity)} <span style="font-size: 13px; font-weight: normal; color: #64748b;">VND</span></div>
      </div>
    </div>
  </div>

  <!-- Tabs Navigation -->
  <div class="tabs-nav">
    <button class="tab-btn active" onclick="showTab(0)">B02 - Kết quả kinh doanh</button>
    <button class="tab-btn" onclick="showTab(1)">B01a - Cân đối kế toán</button>
    <button class="tab-btn" onclick="showTab(2)">B03a - Lưu chuyển tiền tệ</button>
    <button class="tab-btn" onclick="showTab(3)">Bảng Cân đối tài khoản ({len(trial_rows)})</button>
  </div>

  <!-- TAB 0: B02 - KQKD -->
  <div class="tab-content active" id="tab-0">
    <div class="card">
      <div style="padding: 16px 24px; border-bottom: 1px solid var(--border);">
        <h2 style="font-size: 17px; font-weight: 700;">Báo Cáo Kết Quả Hoạt Động Kinh Doanh (Mẫu B02 - DNN)</h2>
        <p style="font-size: 13px; color: var(--text-muted);">Đơn vị tính: Đồng Việt Nam (VND)</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="code">Mã số</th>
              <th>Chỉ tiêu</th>
              <th class="num" style="width: 220px;">Năm nay (2025)</th>
              <th class="num" style="width: 220px;">Năm trước (2024)</th>
            </tr>
          </thead>
          <tbody>
"""

    for code, name, is_header, level in B02_ITEMS:
        val_nn = b02_nn.get(f"ct{code}")
        val_nt = b02_nt.get(f"ct{code}")
        row_cls = "header-row" if is_header else ("sub-row" if level > 0 else "")
        html_page += f"""            <tr class="{row_cls}">
              <td class="code"><strong>{code}</strong></td>
              <td class="item-name">{'&nbsp;&nbsp;&nbsp;&nbsp;' * level}{html.escape(name)}</td>
              <td class="num"><strong>{fmt_vnd(val_nn)}</strong></td>
              <td class="num">{fmt_vnd(val_nt)}</td>
            </tr>\n"""

    html_page += """          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 1: B01a - Cân Đối Kế Toán -->
  <div class="tab-content" id="tab-1">
    <div class="card">
      <div style="padding: 16px 24px; border-bottom: 1px solid var(--border);">
        <h2 style="font-size: 17px; font-weight: 700;">Báo Cáo Tình Hình Tài Chính / Bảng Cân Đối Kế Toán (Mẫu B01a - DNN)</h2>
        <p style="font-size: 13px; color: var(--text-muted);">Đơn vị tính: Đồng Việt Nam (VND)</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="code">Mã số</th>
              <th>Chỉ tiêu</th>
              <th class="num" style="width: 220px;">Số cuối năm (31/12/2025)</th>
              <th class="num" style="width: 220px;">Số đầu năm (01/01/2025)</th>
            </tr>
          </thead>
          <tbody>
"""

    for code, name, is_header, level in B01A_ITEMS:
        if code in ("TÀI SẢN", "NGUỒN VỐN"):
            html_page += f"""            <tr class="header-row" style="background: #e2e8f0;">
              <td class="code"></td>
              <td colspan="3" style="font-size: 14px; font-weight: 800; color: #0f172a; text-transform: uppercase;">{html.escape(name)}</td>
            </tr>\n"""
            continue
        val_cn = b01_cn.get(f"ct{code}")
        val_dn = b01_dn.get(f"ct{code}")
        row_cls = "header-row" if is_header else ("sub-row" if level > 0 else "")
        html_page += f"""            <tr class="{row_cls}">
              <td class="code"><strong>{code}</strong></td>
              <td class="item-name">{'&nbsp;&nbsp;&nbsp;&nbsp;' * level}{html.escape(name)}</td>
              <td class="num"><strong>{fmt_vnd(val_cn)}</strong></td>
              <td class="num">{fmt_vnd(val_dn)}</td>
            </tr>\n"""

    html_page += """          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 2: B03a - Lưu Chuyển Tiền Tệ -->
  <div class="tab-content" id="tab-2">
    <div class="card">
      <div style="padding: 16px 24px; border-bottom: 1px solid var(--border);">
        <h2 style="font-size: 17px; font-weight: 700;">Báo Cáo Lưu Chuyển Tiền Tệ (Phương pháp trực tiếp - Mẫu B03a - DNN)</h2>
        <p style="font-size: 13px; color: var(--text-muted);">Đơn vị tính: Đồng Việt Nam (VND)</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="code">Mã số</th>
              <th>Chỉ tiêu</th>
              <th class="num" style="width: 220px;">Năm nay (2025)</th>
              <th class="num" style="width: 220px;">Năm trước (2024)</th>
            </tr>
          </thead>
          <tbody>
"""

    for code, name, is_header, level in B03A_ITEMS:
        if code in ("I", "II", "III"):
            html_page += f"""            <tr class="header-row" style="background: #f1f5f9;">
              <td class="code"><strong>{code}</strong></td>
              <td colspan="3" style="font-weight: 700;">{html.escape(name)}</td>
            </tr>\n"""
            continue
        val_nn = b03_nn.get(f"ct{code}")
        val_nt = b03_nt.get(f"ct{code}")
        row_cls = "header-row" if is_header else ("sub-row" if level > 0 else "")
        html_page += f"""            <tr class="{row_cls}">
              <td class="code"><strong>{code}</strong></td>
              <td class="item-name">{'&nbsp;&nbsp;&nbsp;&nbsp;' * level}{html.escape(name)}</td>
              <td class="num"><strong>{fmt_vnd(val_nn)}</strong></td>
              <td class="num">{fmt_vnd(val_nt)}</td>
            </tr>\n"""

    html_page += """          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 3: Bảng Cân Đối Tài Khoản -->
  <div class="tab-content" id="tab-3">
    <div class="card">
      <div style="padding: 16px 24px; border-bottom: 1px solid var(--border);">
        <h2 style="font-size: 17px; font-weight: 700;">Bảng Cân Đối Tài Khoản (Trial Balance)</h2>
        <p style="font-size: 13px; color: var(--text-muted);">Chi tiết các tài khoản phát sinh trong năm 2025</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th rowspan="2" class="code" style="vertical-align: middle;">Số hiệu TK</th>
              <th rowspan="2" style="vertical-align: middle;">Tên tài khoản</th>
              <th colspan="2" class="num" style="text-align: center; border-left: 1px solid #cbd5e1;">Số dư đầu kỳ</th>
              <th colspan="2" class="num" style="text-align: center; border-left: 1px solid #cbd5e1;">Số phát sinh trong kỳ</th>
              <th colspan="2" class="num" style="text-align: center; border-left: 1px solid #cbd5e1;">Số dư cuối kỳ</th>
            </tr>
            <tr>
              <th class="num" style="border-left: 1px solid #cbd5e1; width: 130px;">Nợ</th>
              <th class="num" style="width: 130px;">Có</th>
              <th class="num" style="border-left: 1px solid #cbd5e1; width: 130px;">Nợ</th>
              <th class="num" style="width: 130px;">Có</th>
              <th class="num" style="border-left: 1px solid #cbd5e1; width: 130px;">Nợ</th>
              <th class="num" style="width: 130px;">Có</th>
            </tr>
          </thead>
          <tbody>
"""

    tot_dk_no = sum(r["dk_no"] for r in trial_rows if not r["is_sub"])
    tot_dk_co = sum(r["dk_co"] for r in trial_rows if not r["is_sub"])
    tot_ps_no = sum(r["ps_no"] for r in trial_rows if not r["is_sub"])
    tot_ps_co = sum(r["ps_co"] for r in trial_rows if not r["is_sub"])
    tot_ck_no = sum(r["ck_no"] for r in trial_rows if not r["is_sub"])
    tot_ck_co = sum(r["ck_co"] for r in trial_rows if not r["is_sub"])

    for r in trial_rows:
        row_cls = "sub-row" if r["is_sub"] else "header-row"
        html_page += f"""            <tr class="{row_cls}">
              <td class="code"><strong>{r['acc']}</strong></td>
              <td class="item-name">{'&nbsp;&nbsp;&nbsp;&nbsp;' if r['is_sub'] else ''}{html.escape(r['name'])}</td>
              <td class="num" style="border-left: 1px solid #f1f5f9;">{fmt_vnd(r['dk_no'])}</td>
              <td class="num">{fmt_vnd(r['dk_co'])}</td>
              <td class="num" style="border-left: 1px solid #f1f5f9;">{fmt_vnd(r['ps_no'])}</td>
              <td class="num">{fmt_vnd(r['ps_co'])}</td>
              <td class="num" style="border-left: 1px solid #f1f5f9;">{fmt_vnd(r['ck_no'])}</td>
              <td class="num">{fmt_vnd(r['ck_co'])}</td>
            </tr>\n"""

    html_page += f"""            <tr class="header-row" style="background: #e2e8f0; font-weight: 800;">
              <td class="code"></td>
              <td>TỔNG CỘNG</td>
              <td class="num" style="border-left: 1px solid #cbd5e1;">{fmt_vnd(tot_dk_no)}</td>
              <td class="num">{fmt_vnd(tot_dk_co)}</td>
              <td class="num" style="border-left: 1px solid #cbd5e1;">{fmt_vnd(tot_ps_no)}</td>
              <td class="num">{fmt_vnd(tot_ps_co)}</td>
              <td class="num" style="border-left: 1px solid #cbd5e1;">{fmt_vnd(tot_ck_no)}</td>
              <td class="num">{fmt_vnd(tot_ck_co)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Sign / Footer Card -->
  <div class="card">
    <div class="signature-box">
      <div class="cert-info">
        <div><strong>Người lập biểu:</strong> {html.escape(text_of("nguoiLapBieu") or "Trần Huỳnh")}</div>
        <div><strong>Kế toán trưởng:</strong> {html.escape(text_of("keToanTruong") or signer)}</div>
        <div><strong>Người đại diện theo pháp luật:</strong> {html.escape(legal_rep)}</div>
        <div style="margin-top: 8px; font-size: 11.5px; color: #94a3b8;">
          Chứng thư số: {html.escape(sig_subj)}
        </div>
      </div>
      <div class="stamp-box">
        <div>ĐÃ KÝ ĐIỆN TỬ BỞI</div>
        <div style="font-size: 14px; font-weight: 700; margin: 4px 0;">{html.escape(company_name)}</div>
        <div style="font-size: 11.5px; opacity: 0.9;">Thời gian ký: {sig_time or filing_date}</div>
      </div>
    </div>
  </div>
</div>

<script>
function showTab(index) {{
  const tabs = document.querySelectorAll('.tab-content');
  const btns = document.querySelectorAll('.tab-btn');
  tabs.forEach((t, i) => {{
    t.classList.toggle('active', i === index);
  }});
  btns.forEach((b, i) => {{
    b.classList.toggle('active', i === index);
  }});
}}
</script>
</body>
</html>
"""

    if output_path is None:
        base, _ = os.path.splitext(xml_path)
        output_path = f"{base}_rendered.html"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_page)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Render Vietnamese Tax BCTC XML to HTML")
    parser.add_argument("xml_file", help="Path to the XML file")
    parser.add_argument("-o", "--output", help="Output HTML file path", default=None)
    args = parser.parse_args()

    out = render_xml_to_html(args.xml_file, args.output)
    print(f"Rendered successfully to: {out}")


if __name__ == "__main__":
    main()
