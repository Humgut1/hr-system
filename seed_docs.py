import os, sqlite3, uuid

UPLOAD_FOLDER = 'C:/Users/lg/hr-system/static/uploads/applicant_docs'
DB_PATH = 'C:/Users/lg/hr-system/hr_system.db'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def make_pdf(lines):
    parts = ['BT /F1 11 Tf 50 750 Td 16 TL']
    for ln in lines:
        safe = ln.replace('(', '[').replace(')', ']')
        parts.append(f'({safe}) Tj T*')
    parts.append('ET')
    stream = '\n'.join(parts).encode('latin-1', errors='ignore')
    obj4 = ('<<  /Length %d >>\nstream\n' % len(stream)).encode() + stream + b'\nendstream'
    objs = {
        1: b'<< /Type /Catalog /Pages 2 0 R >>',
        2: b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        3: b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
        4: obj4,
        5: b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    }
    pdf = b'%PDF-1.4\n'
    offsets = {}
    for i in range(1, 6):
        offsets[i] = len(pdf)
        pdf += ('%d 0 obj\n' % i).encode() + objs[i] + b'\nendobj\n'
    xref_pos = len(pdf)
    pdf += b'xref\n0 6\n0000000000 65535 f \n'
    for i in range(1, 6):
        pdf += ('%010d 00000 n \n' % offsets[i]).encode()
    pdf += ('trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % xref_pos).encode()
    return pdf

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute('''CREATE TABLE IF NOT EXISTS applicant_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_id INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL DEFAULT 'resume',
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')

applicants = conn.execute('SELECT id, name FROM applicants ORDER BY id LIMIT 5').fetchall()

docs = [
    (applicants[0]['id'], 'resume', 'resume_applicant1.pdf',
     ['Backend Engineer - Resume', '---', 'Career: Kakao 3yr / Naver 2yr', 'Skills: Python Flask Django PostgreSQL Redis', 'Education: Seoul National Univ. CS 2019']),
    (applicants[0]['id'], 'cover_letter', 'cover_letter_applicant1.pdf',
     ['Cover Letter', '---', 'Hello. I am applying for the Backend Engineer position.', '5 years experience designing high-traffic systems.', 'Looking forward to contributing to your team.']),
    (applicants[1]['id'], 'resume', 'resume_applicant2.pdf',
     ['Data Scientist - Resume', '---', 'Career: Coupang 2yr / Line 1yr', 'Skills: Python R TensorFlow PyTorch SQL', 'Education: KAIST CS 2021']),
    (applicants[2]['id'], 'resume', 'resume_applicant3.pdf',
     ['Product Manager - Resume', '---', 'Career: Toss 3yr / Banksalad 2yr', 'Fintech / OKR / Data-driven decision making', 'Education: Yonsei Univ. Business 2018']),
    (applicants[2]['id'], 'portfolio', 'portfolio_applicant3.pdf',
     ['Portfolio', '---', 'Project 1: Real-time Recommendation Engine [MAU 5M]', 'Project 2: A/B Test Platform', 'Accuracy: 87%']),
]

conn.execute('DELETE FROM applicant_documents WHERE uploaded_by IS NULL')
inserted = 0
for app_id, doc_type, fname, lines in docs:
    pdf_bytes = make_pdf(lines)
    stored = uuid.uuid4().hex + '.pdf'
    with open(UPLOAD_FOLDER + '/' + stored, 'wb') as f:
        f.write(pdf_bytes)
    conn.execute(
        'INSERT INTO applicant_documents (applicant_id, doc_type, original_name, stored_name, file_size) VALUES (?,?,?,?,?)',
        (app_id, doc_type, fname, stored, len(pdf_bytes))
    )
    inserted += 1
    print('inserted:', fname)

conn.commit()
conn.close()
print('Done:', inserted, 'docs')
