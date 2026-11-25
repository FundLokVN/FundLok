"""Build a synthetic CIC credit-report PDF with a Vietnamese text layer: python mkcic.py OUT.pdf "COMPANY NAME" TAXCODE (needs fpdf2)."""
import sys
from fpdf import FPDF
name = sys.argv[2]
lines = ["TRUNG TÂM THÔNG TIN TÍN DỤNG QUỐC GIA VIỆT NAM (CIC)", "BÁO CÁO THÔNG TIN TÍN DỤNG DOANH NGHIỆP",
         "Số: 2026-E2E-0001", "Ngày hỏi tin: 10/11/2026", f"Tên doanh nghiệp: {name}", "Mã số thuế: " + sys.argv[3],
         "II. ĐIỂM TÍN DỤNG", "Điểm tín dụng: 560", "Hạng: B", "Ngày chấm điểm: 01/11/2026",
         "Dư nợ hiện tại: 0", "Nợ xấu: Không"]
pdf = FPDF(); pdf.add_page(); pdf.add_font("Arial", "", r"C:\Windows\Fonts\arial.ttf"); pdf.set_font("Arial", size=11)
for l in lines: pdf.cell(0, 8, l, new_x="LMARGIN", new_y="NEXT")
pdf.output(sys.argv[1])
