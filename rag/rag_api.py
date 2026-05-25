from flask import Flask, request, jsonify
import chromadb
import requests
import pymysql
from pypdf import PdfReader
from docx import Document
import os

app = Flask(__name__)

# ── Config ──────────────────────────────
LLAMA_URL = "https://inurbane-admittedly-mayra.ngrok-free.dev"

DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "root",
    "password": "Rootpass123!@#",
    "database": "orangehrm2",
    "charset": "utf8mb4"
}

# ── ChromaDB setup ───────────────────────
chroma = chromadb.PersistentClient(path="/var/www/html/leavesystem/rag/vectordb")
collection = chroma.get_or_create_collection("hr_knowledge")

# ── Add text document ────────────────────
@app.route("/add-text", methods=["POST"])
def add_text():
    data = request.json
    text = data.get("text")
    doc_id = data.get("id")
    collection.add(documents=[text], ids=[doc_id])
    return jsonify({"status": "added", "id": doc_id})

# ── Add PDF ──────────────────────────────
@app.route("/add-pdf", methods=["POST"])
def add_pdf():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file"}), 400
    reader = PdfReader(file)
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text.strip():
            collection.add(
                documents=[text],
                ids=[f"{file.filename}-page-{i}"]
            )
    return jsonify({"status": "added", "pages": len(reader.pages)})

# ── Add Word doc ─────────────────────────
@app.route("/add-docx", methods=["POST"])
def add_docx():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file"}), 400
    doc = Document(file)
    chunks = []
    chunk = ""
    for para in doc.paragraphs:
        chunk += para.text + "\n"
        if len(chunk) > 500:
            chunks.append(chunk)
            chunk = ""
    if chunk:
        chunks.append(chunk)
    for i, c in enumerate(chunks):
        collection.add(documents=[c], ids=[f"{file.filename}-chunk-{i}"])
    return jsonify({"status": "added", "chunks": len(chunks)})

# ── Get live DB data ─────────────────────
def get_db_context(message):
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        context = ""
        keywords = message.lower()

        if any(w in keywords for w in ["leave", "balance", "entitlement"]):
            cursor.execute("""
                SELECT e.emp_firstname, e.emp_lastname, 
                       l.name as leave_type, el.no_of_entitlement
                FROM hs_hr_employee e
                JOIN ohrm_leave_entitlement el ON e.emp_number = el.emp_number
                JOIN ohrm_leave_type l ON el.leave_type_id = l.id
                LIMIT 10
            """)
            rows = cursor.fetchall()
            context += f"Leave entitlements: {rows}\n"

        cursor.close()
        conn.close()
        return context
    except Exception as e:
        return ""

# ── Main RAG chat ────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    message = data.get("message")

    # Search vector DB
    try:
        results = collection.query(query_texts=[message], n_results=3)
        docs = results["documents"][0] if results["documents"] else []
        context = "\n".join(docs)
    except:
        context = ""

    # Get live DB data
    db_context = get_db_context(message)

    # Build system prompt
    system_prompt = "You are Cohere AI, a helpful HR assistant for Cohere company.\n"
    if context:
        system_prompt += f"\nRelevant company knowledge:\n{context}\n"
    if db_context:
        system_prompt += f"\nLive HR data:\n{db_context}\n"
    system_prompt += "\nAnswer helpfully based on the above. If unsure, say so."

    # Call llama.cpp
    try:
        response = requests.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "model": "gemma-3-1b-it",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                "max_tokens": 500
            },
            timeout=120,
            headers={"ngrok-skip-browser-warning": "true"}
        )
        reply = response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        reply = f"Error: {str(e)}"

    return jsonify({"reply": reply})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
