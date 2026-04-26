import os
import re
import tempfile
import datetime
import pytesseract

pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import pytesseract
from PIL import Image, ImageEnhance
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tiff'}

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

NOISE_WORDS = {
    'clinic', 'hospital', 'pharmacy', 'medical', 'centre', 'center', 'care',
    'health', 'pvt', 'ltd', 'consulting', 'nursing', 'home', 'street', 'road',
    'nagar', 'colony', 'district', 'state', 'pin', 'mob', 'tel', 'fax', 'email',
    'timing', 'timings', 'open', 'closed', 'sunday', 'monday', 'tuesday',
    'wednesday', 'thursday', 'friday', 'saturday', 'am', 'pm', 'regd', 'reg',
    'registration', 'license', 'licence', 'degree', 'mbbs', 'md', 'ms', 'dnb',
    'frcs', 'mrcp', 'speciality', 'specialist', 'consultant', 'visit', 'fee',
    'charges', 'followup', 'follow', 'next', 'prescription', 'rx', 'date',
    'patient', 'address', 'city', 'signature', 'stamp', 'seal', 'print',
    'original', 'duplicate', 'copy', 'valid', 'days', 'issued',
}

# Ordered longest-first so multi-word keys are matched before single-word ones
FREQ_MAP = [
    ('once daily',         'OD (Once daily)'),
    ('twice daily',        'BD (Twice daily)'),
    ('twice a day',        'BD (Twice daily)'),
    ('thrice daily',       'TDS (Thrice daily)'),
    ('three times daily',  'TDS (Thrice daily)'),
    ('three times',        'TDS (Thrice daily)'),
    ('four times daily',   'QID (Four times daily)'),
    ('four times',         'QID (Four times daily)'),
    ('as needed',          'SOS (As needed)'),
    ('at bedtime',         'HS (Bedtime)'),
    ('before meals',       'AC (Before meals)'),
    ('after meals',        'PC (After meals)'),
    ('od hs',              'OD HS (Once daily at bedtime)'),
    ('od ac',              'OD AC (Once daily before meals)'),
    ('bd ac',              'BD AC (Twice daily before meals)'),
    ('od pc',              'OD PC (Once daily after meals)'),
    ('od',                 'OD (Once daily)'),
    ('bd',                 'BD (Twice daily)'),
    ('bid',                'BD (Twice daily)'),
    ('tds',                'TDS (Thrice daily)'),
    ('tid',                'TDS (Thrice daily)'),
    ('qid',                'QID (Four times daily)'),
    ('sos',                'SOS (As needed)'),
    ('prn',                'SOS (As needed)'),
    ('stat',               'STAT (Immediately)'),
    ('hs',                 'HS (Bedtime)'),
    ('ac',                 'AC (Before meals)'),
    ('pc',                 'PC (After meals)'),
]

# Ordered longest-first
ROUTE_MAP = [
    ('inhalation',    'Inhalation'),
    ('inhaler',       'Inhalation'),
    ('nebulize',      'Inhalation'),
    ('intravenous',   'IV'),
    ('intramuscular', 'IM'),
    ('subcutaneous',  'SC'),
    ('sublingual',    'Sublingual'),
    ('ophthalmic',    'Ophthalmic'),
    ('by mouth',      'Oral'),
    ('oral',          'Oral'),
    ('topical',       'Topical'),
    ('ointment',      'Topical'),
    ('lotion',        'Topical'),
    ('rectal',        'Rectal'),
    ('nasal',         'Nasal'),
    ('tablet',        'Oral'),
    ('capsule',       'Oral'),
    ('syrup',         'Oral'),
    ('suspension',    'Oral'),
    ('tab',           'Oral'),
    ('cap',           'Oral'),
    ('syp',           'Oral'),
    ('cream',         'Topical'),
    ('gel',           'Topical'),
    ('drops',         'Ophthalmic/Otic'),
    ('eye',           'Ophthalmic'),
    ('ear',           'Otic'),
    ('iv',            'IV'),
    ('im',            'IM'),
    ('sc',            'SC'),
    ('sl',            'Sublingual'),
    ('po',            'Oral'),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def preprocess_image(path):
    img = Image.open(path).convert('L')
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(1.5)
    if max(img.size) < 1500:
        scale = 1500 / max(img.size)
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )
    return img


def match_freq(text):
    t = text.lower()
    for key, val in FREQ_MAP:
        if re.search(r'\b' + re.escape(key) + r'\b', t):
            return val
    return ''


def match_route(text):
    t = text.lower()
    for key, val in ROUTE_MAP:
        if re.search(r'\b' + re.escape(key) + r'\b', t):
            return val
    return ''


# ---------------------------------------------------------------------------
# Medication parsing
# ---------------------------------------------------------------------------

DOSE_RE = re.compile(
    r'\d+\.?\d*\s*(?:mg/5ml|mg/ml|mcg/dose|mg|mcg|g\b|ml|iu|mmol|%)',
    re.I,
)


def parse_med_line(line):
    line = line.strip()
    if len(line) < 3:
        return None

    line = re.sub(r'\s*\+\s*', ' + ', line)  # normalise combination drug spacing

    med = {
        'drug': '', 'dose': '', 'route': '', 'frequency': '',
        'duration': '', 'indication': '', 'batch': '', 'expiry': '',
        'start': '', 'stop': '',
    }

    dose_m = DOSE_RE.search(line)
    if dose_m:
        med['dose'] = dose_m.group(0).strip()

    med['frequency'] = match_freq(line)
    med['route']     = match_route(line)

    dur_m = re.search(r'(?:x\s*|for\s*)(\d+)\s*(?:days?|weeks?|months?)', line, re.I)
    if dur_m:
        med['duration'] = dur_m.group(0).strip()

    # Build drug name from what remains after stripping known fields
    clean = line
    if dose_m:
        clean = clean.replace(dose_m.group(0), ' ')
    if dur_m:
        clean = clean.replace(dur_m.group(0), ' ')
    for key, _ in FREQ_MAP + ROUTE_MAP:
        clean = re.sub(r'\b' + re.escape(key) + r'\b', ' ', clean, flags=re.I)
    clean = re.sub(r'^\s*\d+[\.\)\-]?\s*', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip().strip('.,-()')

    tokens = [
        t for t in clean.split()
        if len(t) > 1 and t.lower() not in NOISE_WORDS
    ]

    drug_tokens = []
    for tok in tokens[:6]:
        if re.match(r'^\d+$', tok):
            break
        drug_tokens.append(tok)

    med['drug'] = ' '.join(drug_tokens).strip('.,')
    if not med['drug']:
        return None

    return med


def enrich_med(med, extra):
    if not med['frequency']:
        med['frequency'] = match_freq(extra)
    if not med['route']:
        med['route'] = match_route(extra)
    if not med['dose']:
        dm = DOSE_RE.search(extra)
        if dm:
            med['dose'] = dm.group(0).strip()
    if not med['indication']:
        ind_m = re.search(r'(?:indication|for|c/o)\s*[:\-]?\s*(.+)', extra, re.I)
        if ind_m:
            med['indication'] = ind_m.group(1).strip()[:60]
    return med


def extract_medications(lines):
    meds = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Pattern 1: numbered list  "1. Tab. Azithromycin 500mg OD x 5 days"
        num_m = re.match(r'^(\d+)[\.\)\-\s]+(.+)', line)
        if num_m:
            med = parse_med_line(num_m.group(2).strip())
            if med:
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    is_new_num = bool(re.match(r'^\d+[\.\)\-\s]+', nxt))
                    is_form    = bool(re.match(r'^(tab|cap|inj|syp)\b', nxt, re.I))
                    if not is_new_num and not is_form and len(nxt) < 80:
                        med = enrich_med(med, nxt)
                        i += 1
                meds.append(med)
            i += 1
            continue

        # Pattern 2: starts with dosage form  "Tab. Metformin 500mg BD"
        form_m = re.match(r'^(tab|cap|inj|syp|syr|oint|gel|cream)[\.\s]+(.+)', line, re.I)
        if form_m:
            med = parse_med_line(form_m.group(1) + ' ' + form_m.group(2))
            if med:
                meds.append(med)
            i += 1
            continue

        i += 1

    return meds[:4]


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def extract_patient_info(lines):
    info = {'name': '', 'initials': '', 'age': '', 'gender': '', 'weight': ''}

    for line in lines:
        ll = line.lower()

        if not info['name']:
            m = re.search(
                r'(?:patient|name|pt\.?)\s*[:\-]?\s*([A-Za-z][A-Za-z\s\.]{2,30})',
                line, re.I,
            )
            if m:
                name = m.group(1).strip().rstrip('.,')
                info['name'] = name
                words = [w for w in name.split() if w and w[0].isalpha()]
                info['initials'] = ''.join(w[0].upper() for w in words[:3])

        if not info['age']:
            m = re.search(r'(?:age|aged?)\s*[:\-]?\s*(\d{1,3})\s*(?:yrs?|years?|y)?', line, re.I)
            if m:
                info['age'] = m.group(1) + ' Y'
            else:
                m2 = re.search(r'\b(\d{1,3})\s*(?:yrs?|years?|y)\b', line, re.I)
                if m2:
                    info['age'] = m2.group(1) + ' Y'

        if not info['gender']:
            if re.search(r'\bfemale\b', ll):
                info['gender'] = 'Female'
            elif re.search(r'\bmale\b', ll):
                info['gender'] = 'Male'

        if not info['weight']:
            m = re.search(r'(?:wt|weight)\s*[:\-]?\s*(\d{2,3})\s*(?:kg)?', line, re.I)
            if m:
                info['weight'] = m.group(1) + ' kg'
            else:
                m2 = re.search(r'\b(\d{2,3})\s*kg\b', line, re.I)
                if m2:
                    info['weight'] = m2.group(1) + ' kg'

    return info


def extract_doctor_info(lines):
    info = {'name': '', 'qualification': '', 'reg_no': '', 'phone': ''}

    for line in lines:
        if not info['name']:
            m = re.search(r'\bdr\.?\s+([A-Za-z][A-Za-z\s\.]{2,30})', line, re.I)
            if m:
                info['name'] = 'Dr. ' + m.group(1).strip().rstrip('.,')

        if not info['qualification']:
            m = re.search(
                r'\b(mbbs|md|ms|dnb|bds|mds|dgo|dch|frcs|mrcp|fccp)[\s,\.]*([A-Za-z\s,\.]*)',
                line, re.I,
            )
            if m:
                info['qualification'] = m.group(0).strip()[:60]

        if not info['reg_no']:
            m = re.search(
                r'(?:reg\.?\s*no\.?|rmc|mci)\s*[:\-#\.]?\s*([A-Z]{1,5}[\-\d\/]{3,})',
                line, re.I,
            )
            if m:
                info['reg_no'] = m.group(1).strip()

        if not info['phone']:
            m = re.search(
                r'(?:mob|mobile|phone|tel|contact|ph)\s*[:\.\-\s]*'
                r'(\+?[\d][\d\s\-\(\)]{8,14})',
                line, re.I,
            )
            if m:
                info['phone'] = re.sub(r'[\s\-\(\)]', '', m.group(1))
            else:
                m2 = re.search(r'\b(\+?91\s*)?([6-9]\d{9})\b', line)
                if m2:
                    prefix = (m2.group(1) or '').replace(' ', '')
                    info['phone'] = prefix + m2.group(2)

    return info


def extract_clinic_info(lines):
    info = {'name': '', 'address': ''}
    candidates = [l.strip() for l in lines[:10] if l.strip() and len(l.strip()) > 4]
    if candidates:
        info['name'] = candidates[0]
    if len(candidates) > 1:
        info['address'] = candidates[1]
    return info


def extract_diagnosis(lines):
    for line in lines:
        m = re.search(
            r'(?:diagnosis|dx|c/o|chief\s+complaint|complaint|impression|assessment)'
            r'\s*[:\-]?\s*(.+)',
            line, re.I,
        )
        if m:
            return m.group(1).strip()[:150]
    return ''


def extract_date(lines):
    for line in lines:
        m = re.search(r'\b(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})\b', line)
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            if len(y) == 2:
                y = '20' + y
            return f"{d.zfill(2)}/{mo.zfill(2)}/{y}"
    return datetime.date.today().strftime('%d/%m/%Y')


# ---------------------------------------------------------------------------
# Main parse entry point
# ---------------------------------------------------------------------------

def parse_prescription(image_path):
    img  = preprocess_image(image_path)
    raw  = pytesseract.image_to_string(img, config='--oem 3 --psm 6')
    lines = [l for l in raw.splitlines() if l.strip()]

    patient = extract_patient_info(lines)
    doctor  = extract_doctor_info(lines)
    clinic  = extract_clinic_info(lines)
    meds    = extract_medications(lines)
    diag    = extract_diagnosis(lines)
    rx_date = extract_date(lines)

    return {
        'initials':             patient['initials'],
        'patient_name':         patient['name'],
        'age':                  patient['age'],
        'gender':               patient['gender'],
        'weight':               patient['weight'],
        'diagnosis':            diag,
        'medications':          meds,
        'doctor_name':          doctor['name'],
        'doctor_qualification': doctor['qualification'],
        'doctor_reg':           doctor['reg_no'],
        'doctor_phone':         doctor['phone'],
        'clinic_name':          clinic['name'],
        'clinic_address':       clinic['address'],
        'prescription_date':    rx_date,
        'report_date':          datetime.date.today().strftime('%d/%m/%Y'),
    }


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

def allowed_file(filename):
    return (
        '.' in filename
        and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    )


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/extract', methods=['POST'])
def extract():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename or not allowed_file(f.filename):
        return jsonify({'error': 'Invalid file type'}), 400

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(f.filename))
    f.save(path)

    try:
        data = parse_prescription(path)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(path):
            os.remove(path)


@app.route('/generate-pdf', methods=['POST'])
def generate_pdf():
    data = request.json
    pdf_path = build_adr_pdf(data)
    return send_file(
        pdf_path,
        as_attachment=True,
        download_name='ADR_Report.pdf',
        mimetype='application/pdf',
    )


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------

def build_adr_pdf(d):
    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    tmp.close()

    doc = SimpleDocTemplate(
        tmp.name, pagesize=A4,
        topMargin=8*mm, bottomMargin=8*mm,
        leftMargin=10*mm, rightMargin=10*mm,
    )

    RED      = colors.HexColor('#b71c1c')
    RED_DARK = colors.HexColor('#7f0000')
    LGRAY    = colors.HexColor('#f5f5f5')
    MGRAY    = colors.HexColor('#d0d0d0')
    WHITE    = colors.white

    def ps(size=8, bold=False, color=colors.black, align=TA_LEFT):
        return ParagraphStyle(
            'x', fontSize=size,
            fontName='Helvetica-Bold' if bold else 'Helvetica',
            textColor=color, alignment=align,
            leading=size * 1.3, spaceAfter=0,
        )

    def p(text, size=8, bold=False, color=colors.black, align=TA_LEFT):
        return Paragraph(str(text or ''), ps(size, bold, color, align))

    def chk(val):
        return '☑' if val else '☐'

    def section_hdr(title):
        t = Table([[p(title, 8, True, WHITE)]], colWidths=[190*mm])
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), RED),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ]))
        return t

    def info_tbl(rows, widths):
        t = Table(rows, colWidths=widths)
        t.setStyle(TableStyle([
            ('BOX',           (0, 0), (-1, -1), 0.4, MGRAY),
            ('INNERGRID',     (0, 0), (-1, -1), 0.3, MGRAY),
            ('BACKGROUND',    (0, 0), (-1, -1), LGRAY),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ]))
        return t

    elements = []

    # Title block
    title_rows = [
        [p('SUSPECTED ADVERSE DRUG REACTION REPORTING FORM', 10, True, WHITE, TA_CENTER)],
        [p('For VOLUNTARY reporting of ADRs by Healthcare Professionals', 7.5, False, WHITE, TA_CENTER)],
        [p('INDIAN PHARMACOPOEIA COMMISSION — National Coordination Centre-Pharmacovigilance Programme of India', 7, False, WHITE, TA_CENTER)],
        [p('Ministry of Health & Family Welfare, Govt. of India  ·  PvPI Helpline: 1800-180-3024 (Mon-Fri, 9 AM-5:30 PM)', 7, False, WHITE, TA_CENTER)],
    ]
    t = Table(title_rows, colWidths=[190*mm])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), RED),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 2*mm))

    # Section A — Patient
    elements.append(section_hdr('A.  PATIENT INFORMATION'))
    gender = d.get('gender', '')
    g_str = (
        chk(gender == 'Male')   + ' Male   ' +
        chk(gender == 'Female') + ' Female   ' +
        chk(gender not in ('Male', 'Female', '')) + ' Other'
    )
    elements.append(info_tbl(
        [
            [p('1. Patient Initials',    7.5, True), p(d.get('initials', ''), 8),
             p('2. Age / Date of Birth', 7.5, True), p(d.get('age', ''),      8)],
            [p('3. Gender',              7.5, True), p(g_str,                 8),
             p('4. Weight (kg)',         7.5, True), p(d.get('weight', ''),   8)],
        ],
        [38*mm, 57*mm, 38*mm, 57*mm],
    ))
    elements.append(Spacer(1, 2*mm))

    # Section B — Reaction
    elements.append(section_hdr('B.  SUSPECTED ADVERSE REACTION'))
    rx      = d.get('reaction', {})
    ser     = d.get('seriousness', {})
    outcome = d.get('outcome', '')

    ser_str = (
        chk(ser.get('death'))              + ' Death   ' +
        chk(ser.get('life_threatening'))   + ' Life threatening   ' +
        chk(ser.get('hospitalization'))    + ' Hospitalization   ' +
        chk(ser.get('disability'))         + ' Disability   ' +
        chk(ser.get('congenital_anomaly')) + ' Congenital anomaly   ' +
        chk(ser.get('other'))              + ' Other medically important'
    )
    out_str = (
        chk(outcome == 'Recovered')              + ' Recovered   ' +
        chk(outcome == 'Recovering')             + ' Recovering   ' +
        chk(outcome == 'Not recovered')          + ' Not recovered   ' +
        chk(outcome == 'Recovered with sequelae')+ ' Recovered with sequelae   ' +
        chk(outcome == 'Fatal')                  + ' Fatal   ' +
        chk(outcome == 'Unknown')                + ' Unknown'
    )
    rx_desc = rx.get('description') or d.get('diagnosis', '')

    t = Table(
        [
            [p('5. Reaction start date', 7.5, True), p(rx.get('start', ''), 8),
             p('6. Reaction stop date',  7.5, True), p(rx.get('stop',  ''), 8)],
            [p('7. Event description',   7.5, True), p(rx_desc, 8), '', ''],
            [p('14. Seriousness',        7.5, True), p(ser_str, 7.5), '', ''],
            [p('15. Outcome',            7.5, True), p(out_str, 7.5), '', ''],
        ],
        colWidths=[38*mm, 57*mm, 38*mm, 57*mm],
    )
    t.setStyle(TableStyle([
        ('BOX',           (0, 0), (-1, -1), 0.4, MGRAY),
        ('INNERGRID',     (0, 0), (-1, -1), 0.3, MGRAY),
        ('BACKGROUND',    (0, 0), (-1, -1), LGRAY),
        ('SPAN',          (1, 1), (3, 1)),
        ('SPAN',          (1, 2), (3, 2)),
        ('SPAN',          (1, 3), (3, 3)),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 2*mm))

    # Section C — Medications
    elements.append(section_hdr('C.  SUSPECTED MEDICATION(S)'))
    med_hdr = [p(h, 7, True) for h in (
        '#', 'Drug Name (Brand/Generic)', 'Batch No.', 'Expiry',
        'Dose', 'Route', 'Frequency', 'Therapy Start', 'Therapy Stop', 'Indication',
    )]
    med_rows = [med_hdr]
    meds = d.get('medications', [])
    for idx, lbl in enumerate(['i', 'ii', 'iii', 'iv']):
        m = meds[idx] if idx < len(meds) else {}
        med_rows.append([
            p(lbl,                    7),
            p(m.get('drug',      ''), 7.5),
            p(m.get('batch',     ''), 7),
            p(m.get('expiry',    ''), 7),
            p(m.get('dose',      ''), 7),
            p(m.get('route',     ''), 7),
            p(m.get('frequency', ''), 7),
            p(m.get('start',     ''), 7),
            p(m.get('stop',      ''), 7),
            p(m.get('indication',''), 7),
        ])
    t = Table(
        med_rows,
        colWidths=[7*mm, 36*mm, 17*mm, 13*mm, 15*mm, 14*mm, 28*mm, 16*mm, 16*mm, 28*mm],
    )
    t.setStyle(TableStyle([
        ('BOX',           (0, 0), (-1, -1), 0.4, MGRAY),
        ('INNERGRID',     (0, 0), (-1, -1), 0.3, MGRAY),
        ('BACKGROUND',    (0, 0), (-1,  0), MGRAY),
        ('BACKGROUND',    (0, 1), (-1, -1), LGRAY),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 3),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 3),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 2*mm))

    # Section D — Reporter
    elements.append(section_hdr('D.  REPORTER DETAILS'))
    reporter  = d.get('reporter', {})
    rep_name  = reporter.get('name')    or d.get('doctor_name', '')
    rep_qual  = d.get('doctor_qualification', '')
    clinic_n  = d.get('clinic_name', '')
    clinic_a  = d.get('clinic_address', '')
    rep_addr  = reporter.get('address') or (clinic_n + ('  ' + clinic_a if clinic_a else '')).strip()
    rep_phone = reporter.get('contact') or d.get('doctor_phone', '')
    rep_email = reporter.get('email',      '')
    rep_occ   = reporter.get('occupation', 'Medical Practitioner')
    rep_reg   = d.get('doctor_reg', '')
    rep_date  = d.get('report_date', datetime.date.today().strftime('%d/%m/%Y'))
    name_cell = rep_name + ('  |  ' + rep_qual if rep_qual else '')

    t = Table(
        [
            [p('16. Name & Address', 7.5, True), p(name_cell,  8),
             p('Reg. No.',           7.5, True), p(rep_reg,    8)],
            [p('Clinic / Hospital',  7.5, True), p(rep_addr,   8), '', ''],
            [p('Contact No.',        7.5, True), p(rep_phone,  8),
             p('Email',              7.5, True), p(rep_email,  8)],
            [p('Occupation',         7.5, True), p(rep_occ,    8),
             p('17. Date of report', 7.5, True), p(rep_date,   8)],
        ],
        colWidths=[38*mm, 62*mm, 38*mm, 52*mm],
    )
    t.setStyle(TableStyle([
        ('BOX',           (0, 0), (-1, -1), 0.4, MGRAY),
        ('INNERGRID',     (0, 0), (-1, -1), 0.3, MGRAY),
        ('BACKGROUND',    (0, 0), (-1, -1), LGRAY),
        ('SPAN',          (1, 1), (3, 1)),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 3*mm))

    # Confidentiality footer
    t = Table(
        [[p(
            "CONFIDENTIALITY: The patient's identity is held in strict confidence. "
            "Submission of a report does not constitute an admission that medical personnel "
            "or manufacturer caused the reaction. This report has no legal implication on the reporter.",
            7, False, WHITE,
        )]],
        colWidths=[190*mm],
    )
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), RED_DARK),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
    ]))
    elements.append(t)

    doc.build(elements)
    return tmp.name


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.makedirs('uploads', exist_ok=True)
    app.run(debug=True, port=5000)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
