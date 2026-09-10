"""Generate image-only and mixed-page regression fixtures from synthetic cv-000.
Generation only: requires pymupdf; runtime and tests use the existing PDF stack.
"""
import io
from pathlib import Path
import pymupdf
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PyPDF2 import PdfReader, PdfWriter
root=Path(__file__).parent
source=root/'pdfs/cv-000.pdf'
doc=pymupdf.open(source)
png=doc[0].get_pixmap(matrix=pymupdf.Matrix(2,2)).tobytes('png')
scan=io.BytesIO(); c=canvas.Canvas(scan,pagesize=(612,792),invariant=1)
c.drawImage(ImageReader(io.BytesIO(png)),0,0,width=612,height=792);c.save()
(root/'scan.pdf').write_bytes(scan.getvalue())
cover=io.BytesIO();c=canvas.Canvas(cover,pagesize=(612,792),invariant=1)
c.drawString(40,740,'Synthetic document cover. Physician curriculum vitae on the next page.');c.save()
w=PdfWriter();w.add_page(PdfReader(cover).pages[0]);w.add_page(PdfReader(scan).pages[0])
with (root/'mixed-scan.pdf').open('wb') as f:w.write(f)
