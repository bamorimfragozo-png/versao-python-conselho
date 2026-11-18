import json
import os
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from importio import processar_dados_completos

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
INTEGRACAO_HTML = BASE_DIR / "integracao.html"
DASHBOARDS_HTML = BASE_DIR / "dashboards.html"
MANIFEST_JSON = BASE_DIR / "manifest.webmanifest"
ICON_SVG = BASE_DIR / "icon.svg"
DADOS_JSON = BASE_DIR / "dados_alunos.json"
UPLOAD_DIR = BASE_DIR / "uploads_tmp"
DASHBOARD_PAGE = os.environ.get("DASHBOARD_PAGE", "index.html")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024

DADOS_SEMENTE = [
    BASE_DIR / "saida_boletins.json",
    BASE_DIR / "saida_boletins2.json",
    BASE_DIR / "temp_boletins_alunos.json",
    BASE_DIR / "temp_boletins_duas.json",
]


def carregar_json(caminho):
    with open(caminho, "r", encoding="utf-8") as f:
        dados = json.load(f)
    if isinstance(dados, dict):
        dados = dados.get("alunos", [])
    return dados if isinstance(dados, list) else []


def garantir_dados():
    if DADOS_JSON.exists():
        return
    alunos = []
    for origem in DADOS_SEMENTE:
        if origem.exists():
            alunos = mesclar_alunos(alunos, carregar_json(origem))
    salvar_json(alunos)


def salvar_json(alunos):
    DADOS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(DADOS_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(alunos, f, ensure_ascii=False, indent=2)


def mesclar_alunos(base, novos):
    por_prontuario = {str(a.get("prontuario") or "").upper(): a for a in base if isinstance(a, dict)}
    for aluno in novos:
        if not isinstance(aluno, dict):
            continue
        chave = str(aluno.get("prontuario") or aluno.get("nome") or "").upper()
        if not chave:
            continue
        if chave in por_prontuario:
            antigo = por_prontuario[chave]
            disciplinas = antigo.get("disciplinas") or []
            nomes = {str(d.get("nome")) for d in disciplinas if isinstance(d, dict)}
            for d in aluno.get("disciplinas") or []:
                if isinstance(d, dict) and d.get("nome") not in nomes:
                    disciplinas.append(d)
                    nomes.add(d.get("nome"))
            antigo["disciplinas"] = disciplinas
            for campo in ("nome", "curso", "frequencia", "num_chamada"):
                if aluno.get(campo) not in (None, ""):
                    antigo[campo] = aluno[campo]
            if aluno.get("turma") and aluno.get("turma") != "Sem turma":
                antigo["turma"] = aluno["turma"]
            antigo["deliberacao"] = antigo.get("deliberacao") or aluno.get("deliberacao") or ""
            antigo["acoesNapne"] = antigo.get("acoesNapne") or aluno.get("acoesNapne") or []
            if aluno.get("napne"):
                antigo["napne"] = True
                antigo["pneInfo"] = aluno.get("pneInfo") or antigo.get("pneInfo")
            if not antigo.get("foto") and aluno.get("foto"):
                antigo["foto"] = aluno.get("foto")
        else:
            por_prontuario[chave] = aluno
    return sorted(por_prontuario.values(), key=lambda a: (a.get("num_chamada") or 0, str(a.get("nome") or "")))


def salvar_upload(file_storage, prefixo):
    nome = secure_filename(file_storage.filename or f"{prefixo}.pdf")
    if not nome.lower().endswith(".pdf"):
        raise ValueError("Apenas arquivos PDF são aceitos.")
    UPLOAD_DIR.mkdir(exist_ok=True)
    destino = UPLOAD_DIR / f"{prefixo}_{nome}"
    file_storage.save(destino)
    return destino


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, DASHBOARD_PAGE)


@app.get("/integracao")
def integracao():
    return send_from_directory(BASE_DIR, "integracao.html")


@app.get("/dashboards")
def dashboards():
    return send_from_directory(BASE_DIR, "dashboards.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(BASE_DIR, "manifest.webmanifest")


@app.get("/icon.svg")
def icon():
    return send_from_directory(BASE_DIR, "icon.svg")


@app.get("/api/dados")
def api_dados_get():
    garantir_dados()
    try:
        alunos = carregar_json(DADOS_JSON)
    except Exception as e:
        return jsonify({"erro": f"JSON inválido: {e}"}), 500
    return jsonify({"arquivo": DADOS_JSON.name, "alunos": alunos})


@app.post("/api/dados")
def api_dados_post():
    payload = request.get_json(silent=True) or {}
    alunos = payload.get("alunos", payload)
    if not isinstance(alunos, list):
        return jsonify({"erro": "Payload deve conter uma lista 'alunos'."}), 400
    try:
        salvar_json(alunos)
    except Exception as e:
        return jsonify({"erro": f"Falha ao salvar JSON: {e}"}), 500
    return jsonify({"ok": True, "arquivo": DADOS_JSON.name, "alunos": alunos})


@app.post("/api/processar-pdfs")
def api_processar_pdfs():
    turma = (request.form.get("turma") or "").strip()
    boletins = request.files.getlist("pdfs")
    napne = request.files.get("napne")
    substituir = request.form.get("substituir") == "1"

    boletins = [f for f in boletins if f and f.filename]
    if not boletins:
        return jsonify({"erro": "Envie ao menos um PDF de boletim."}), 400
    if napne and not napne.filename:
        napne = None

    caminho_napne = None
    temporarios = []
    try:
        if napne:
            caminho_napne = salvar_upload(napne, "napne")
            temporarios.append(caminho_napne)

        extraidos = []
        for i, boletim in enumerate(boletins, start=1):
            caminho_boletim = salvar_upload(boletim, f"boletim{i}")
            temporarios.append(caminho_boletim)
            saida_temp = UPLOAD_DIR / f"saida_{i}.json"
            processar_dados_completos(
                caminho_boletim=str(caminho_boletim),
                nome_turma=turma,
                nome_arquivo_saida=str(saida_temp),
                caminho_napne=str(caminho_napne) if caminho_napne else None,
                extrair_fotos=True,
            )
            extraidos.extend(carregar_json(saida_temp))

        atuais = [] if substituir else (carregar_json(DADOS_JSON) if DADOS_JSON.exists() else [])
        alunos = mesclar_alunos(atuais, extraidos)
        salvar_json(alunos)
        return jsonify({"ok": True, "arquivo": DADOS_JSON.name, "alunos": alunos})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    finally:
        for caminho in temporarios:
            try:
                os.remove(caminho)
            except OSError:
                pass
        for caminho in UPLOAD_DIR.glob("saida_*.json"):
            try:
                caminho.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    garantir_dados()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
