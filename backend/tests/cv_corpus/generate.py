"""Reproducible synthetic physician CVs; expectations are authored, never parsed.
Run with reportlab installed. No real patient or physician data.
"""
import json
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).parent
NAMES = ['Avery Morgan', 'José Alvarez', 'Zoë Laurent', 'Amara Okafor', 'Priya Shah',
         'Jean-Luc Martin', 'Mei Chen', 'Sofia Rossi', 'Omar Hassan', 'Rachel O’Neill']
FIELDS = ['Nephrology', 'Cardiology', 'Internal Medicine', 'Pediatrics', 'Surgery',
          'Neurology', 'Radiology', 'Family Medicine', 'Dermatology', 'Psychiatry']
BOARDS = ['ABIM', 'ABIM', 'ABIM', 'ABP', 'ABS', 'ABPN', 'ABR', 'ABFM', 'ABD', 'ABPN']

def generate():
    # Portable reportlab-bundled Unicode font; fonts embedded in every PDF.
    import reportlab
    font = Path(reportlab.__file__).parent / 'fonts' / 'Vera.ttf'
    pdfmetrics.registerFont(TTFont('CV', str(font)))
    manifest = []
    for i in range(100):
        profile, layout = i % 10, i // 10
        name, field, board = NAMES[profile], FIELDS[profile], BOARDS[profile]
        degree = ['MD', 'DO', 'MBBS', 'MBChB', 'MD'][profile % 5]
        hospital = f'Example {chr(65 + profile)} Medical Center'
        training = f'Example {chr(65 + profile)} University Hospital'
        year = str(2010 + profile)
        number = f'A{900000 + i}'
        negative = layout == 8
        name_line = ('Dr. ' if layout in (2, 6) else '') + name + ', ' + degree
        lines = [name_line, f'Primary specialty: {field}', 'SYNTHETIC CV - TEST DATA ONLY',
                 f'LinkedIn: https://linkedin.com/in/synthetic-doctor-{i:03d}',
                 f'{profile + 3} years of clinical practice', 'Professional Experience',
                 f'{hospital}, 2020 - ' + ('present' if layout % 2 else 'Present'),
                 f'Attending Physician, {field}', 'Postgraduate Training']
        if layout in (1, 3, 5, 7):
            lines += [f'{training}, 2005 - {year}', 'Resident, Internal Medicine',
                      f'{hospital}, {year} - 2023', f'Fellow, {field}']
        else:
            lines += [f'Residency, Internal Medicine — {training}, 2005 - {year}',
                      f'Fellowship, {field} — {hospital}, {year} - 2023']
        lines += ['Certifications']
        if negative:
            lines += [f'Not board certified in {field}; {board} exam pending']
        elif layout == 7:
            lines += [f'Board Certified in {field}', f'Issuing board: {board}']
        else:
            lines += [f'Board Certified in {field} ({board})']
        lines += ['Licensure', f'California Medical License ' + (': ' if layout % 2 else '#') + number + (' - Not active' if negative else ' - Active')]
        if layout == 6:
            lines += ['References', 'Alex Example, MD, PhD', 'Reference author only; not applicant credentials.']
        lines += ['Selected Publications', 'Synthetic study. Example Journal. 2022. doi:10.0000/example']
        expected = {'fullLegalName': name, 'degree': degree, 'qualification': degree,
                    'primarySpecialty': field, 'healthSystem': hospital,
                    'yearsInActivePractice': str(profile + 3),
                    'linkedinUrl': f'https://linkedin.com/in/synthetic-doctor-{i:03d}',
                    'licenseNumber': number, 'licenseState': 'CA',
                    'residency': [{'institution': training, 'year': year}],
                    'residencyCompletionYear': year,
                    'fellowship': [{'institution': hospital, 'specialty': field, 'year': '2023'}],
                    'boardCertifications': ([{'board':'','specialty':'','subspecialty':'','active':None}] if negative else
                       [{'board':board,'specialty':field,'subspecialty':'','active':None}])}
        path = ROOT / 'pdfs' / f'cv-{i:03d}.pdf'
        path.parent.mkdir(exist_ok=True)
        c = canvas.Canvas(str(path), pagesize=(612, 792), invariant=1)
        c.setTitle(f'Synthetic physician CV {i:03d}')
        def draw(block, x=40, y=740, size=10):
            c.setFont('CV', size)
            for line in block:
                c.drawString(x, y, line)
                y -= 23
        if layout == 3:  # Complete sections in columns, not interleaved words.
            draw(lines[:8], 25, size=7)
            draw(lines[8:], 310, size=6)
        elif layout == 4:
            draw(lines[:8]); c.showPage(); draw(lines[8:])
        elif layout == 5:
            draw(lines, 30, 740, 9)
        elif layout == 9:
            # Header pushes identity beyond the old first-eight-line heuristic.
            draw(['Curriculum Vitae'] + ['Department contact information'] * 8 + lines[:5], size=9)
            c.showPage(); draw(lines[5:])
        else:
            draw(lines, size=9 if layout == 6 else 10)
        c.save()
        manifest.append({'id':f'cv-{i:03d}', 'layout':layout, 'expected':expected})
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'Created {len(manifest)} synthetic PDFs')

if __name__ == '__main__': generate()
