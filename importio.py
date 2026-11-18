import io
import json
import base64
import re
import argparse
import os
import sys
from pypdf import PdfReader
from pypdf.generic import ContentStream

# Garante logs legíveis no console do Windows (cp850/cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Palavras-chave (siglas) para identificar matérias técnicas
TECNICAS = [
    "ILPR", "MAIN", "ININ", "LDPR", "RDCO", "LPWE", "SOPE", "INSO", "IPRE",
    "BDDA", "PSCO", "PRIN", "CNVI", "SDRE", "ASRE", "RSFI", "GCLI", "ELET",
    "DCAD", "CAUT", "PROG", "PCOE", "EDIG", "PRI1", "ELIN", "CISUT", "INTI",
    "MAPI", "CNCM", "CLPR", "REPI", "HIEP", "MIMP", "PRI2"
]

# ---------------------------------------------------------------------------
# Padrão de uma linha de disciplina do boletim (texto já com espaços normalizados):
#
# [<diário>]INT.xxxxx (o diário pode vir colado, sem espaço, no texto do pypdf)
# INT.xxxxx (SIGLA) - NOME  CH  aulas  T.aulas  T.faltas  %freq
#   <situação>  N1 F1  N2 F2  N3 F3  N4 F4  MD  NAF(N) NAF(F)  MFD
#
# Nota = "10,00" ou "-" (sem nota). Falta = inteiro.
# ---------------------------------------------------------------------------
# Os PDFs podem usar vírgula (60,00) ou ponto (60.00) como separador decimal.
NOTA = r"\d{1,2}[.,]\d{2}|-"
FALTA = r"\d+"

PADRAO_DISCIPLINA = re.compile(
    r"(?P<nome>INT\.\d{5}\s*\((?P<sigla>[A-Z0-9]+)\)\s*-\s*.+?)\s+"
    r"(?P<ch>\d+[.,]\d{2})\s+(?P<aulas>\d+)\s+(?P<t_aulas>\d+)\s+"
    r"(?P<t_faltas>\d+)\s+(?P<freq>\d+(?:[.,]\d+)?)%\s+"
    r"(?P<situacao>[^\d\-]+?)\s*"
    rf"(?P<n1>{NOTA})\s+(?P<f1>{FALTA})\s+"
    rf"(?P<n2>{NOTA})\s+(?P<f2>{FALTA})\s+"
    rf"(?P<n3>{NOTA})\s+(?P<f3>{FALTA})\s+"
    rf"(?P<n4>{NOTA})\s+(?P<f4>{FALTA})"
)


# Algumas versões do pypdf "colam" as palavras na quebra de linha do nome da
# disciplina (ex.: "CONTEÚDOPARA"). Esta tabela devolve o espaço perdido.
CORRECOES_NOME = {
    "CONTEÚDOPARA": "CONTEÚDO PARA",
    "DESISTEMAS": "DE SISTEMAS",
    "DESERVIÇOS": "DE SERVIÇOS",
    "EVIRTUALIZAÇÃO": "E VIRTUALIZAÇÃO",
    "INTEGRADOR(TURMA": "INTEGRADOR (TURMA",
}


def limpar_nome(nome):
    for errado, certo in CORRECOES_NOME.items():
        nome = nome.replace(errado, certo)
    return nome.strip()


def tratar_nota(v):
    """Converte '10,00' -> 10.0 e '-' (ou vazio) -> None."""
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "-"):
        return None
    try:
        # Aceita "10,00" e "10.00"
        return float(v.replace(",", "."))
    except ValueError:
        return None


def _extrair_info_napne_bloco(bloco):
    """Resume os campos de necessidade especial encontrados em um bloco do NAPNE/SUAP."""
    partes = []

    m_pne = re.search(
        r"Portador\(a\)\s+de\s+Necessidades\s+Especiais\s*:?\s*(Sim|Não)"
        r"(?:\s+Tipo de Necessidade Especial\s*:?\s*-?\s*([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\-/]{0,15}))?",
        bloco, re.IGNORECASE,
    )
    if m_pne:
        tipo = (m_pne.group(2) or "").strip(" -")
        if m_pne.group(1).strip().lower() == "sim":
            partes.append(f"Necessidades Especiais: Sim" + (f" (Tipo {tipo})" if tipo and tipo != "-" else ""))
        else:
            partes.append("Necessidades Especiais: Não")

    m_trans = re.search(
        r"Portador\(a\)\s+de\s+Transtorno\s*:?\s*(Sim|Não)"
        r"(?:\s+Tipo de Transtorno\s*:?\s*-?\s*([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\-/]{0,15}))?",
        bloco, re.IGNORECASE,
    )
    if m_trans:
        tipo = (m_trans.group(2) or "").strip(" -")
        if m_trans.group(1).strip().lower() == "sim":
            partes.append(f"Transtorno: Sim" + (f" (Tipo {tipo})" if tipo and tipo != "-" else ""))
        else:
            partes.append("Transtorno: Não")

    m_sup = re.search(
        r"Portador\(a\)\s+de\s+Superdota\S{0,3}o\s*:?\s*(Sim|Não)"
        r"(?:\s+Superdota\S{0,3}o\s*:?\s*-?\s*([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\-/]{0,15}))?",
        bloco, re.IGNORECASE,
    )
    if m_sup:
        tipo = (m_sup.group(2) or "").strip(" -")
        if m_sup.group(1).strip().lower() == "sim":
            partes.append(f"Superdotação: Sim" + (f" ({tipo})" if tipo and tipo != "-" else ""))
        else:
            partes.append("Superdotação: Não")

    m_obs = re.search(r"Observa..es Gerais\s*(.*?)(?=Dados Acad|Matr.cula|$)", bloco, re.IGNORECASE)
    if m_obs:
        obs = re.sub(r"\s+", " ", m_obs.group(1)).strip(" :-|")
        if obs and "nenhuma" not in obs.lower():
            partes.append(f"Observações: {obs[:240]}")

    if partes:
        return " | ".join(partes)

    limpo = re.sub(r"https?://\S+", " ", bloco)
    limpo = re.sub(r"\d{2}/\d{2}/\d{4},?\s*\d{1,2}:\d{2}.*?- SUAP.*?(?=\d/\d|$)", " ", limpo)
    tokens = [
        t for t in limpo.split(" ")
        if t and "=" not in t and not t.startswith("/")
        and not re.fullmatch(r"\d+/\d+", t)
    ]
    return " ".join(tokens).strip()[:400]


def carregar_dados_napne_pdf(caminho_napne_pdf):
    """
    Lê o PDF de apontamentos/dados do NAPNE e devolve {prontuario: descrição}.
    Funciona tanto para PDFs de apontamentos quanto para dumps do SUAP com os
    campos Portador(a) de Necessidades Especiais / Transtorno / Superdotação.
    """
    if not caminho_napne_pdf or not os.path.exists(caminho_napne_pdf):
        print("Aviso: Arquivo do NAPNE não encontrado. Prosseguindo apenas com dados do PDF de boletins.")
        return {}

    try:
        print(f"Lendo PDF do NAPNE: {caminho_napne_pdf}...")
        with open(caminho_napne_pdf, "rb") as f:
            memoria_pdf = io.BytesIO(f.read())
            leitor_pdf = PdfReader(memoria_pdf)

        texto_completo = ""
        for pagina in leitor_pdf.pages:
            texto_completo += (pagina.extract_text() or "") + "\n"

        texto_limpo = re.sub(r"\s+", " ", texto_completo)
        matches = list(re.finditer(r"BT\d{7}", texto_limpo))
        if not matches:
            return {}

        prontuarios = []
        for m in matches:
            p = m.group(0).upper()
            if p not in prontuarios:
                prontuarios.append(p)

        pos_primeira = {p: texto_limpo.find(p) for p in prontuarios}
        napne_dict = {}
        for i, prontuario in enumerate(prontuarios):
            ini = pos_primeira[prontuario]
            fim = pos_primeira[prontuarios[i + 1]] if i + 1 < len(prontuarios) else len(texto_limpo)
            bloco = texto_limpo[ini:fim]
            conteudo = _extrair_info_napne_bloco(bloco)
            napne_dict[prontuario] = conteudo if conteudo else "Aluno registrado no NAPNE"

        return napne_dict
    except Exception as e:
        print(f"Erro ao processar o PDF do NAPNE: {e}")
        return {}


def extrair_disciplinas(bloco):
    """
    Extrai as disciplinas de um bloco de boletim lendo cada linha
    de forma estruturada (posicional), sem depender de números soltos.
    """
    texto = re.sub(r"\s+", " ", bloco)
    disciplinas = []

    for m in PADRAO_DISCIPLINA.finditer(texto):
        nome_materia = limpar_nome(m.group("nome"))
        sigla = m.group("sigla").upper()

        is_tecnico = any(kw in sigla for kw in TECNICAS)

        disciplinas.append({
            "nome": nome_materia,
            "b1": tratar_nota(m.group("n1")),
            "b2": tratar_nota(m.group("n2")),
            "b3": tratar_nota(m.group("n3")),
            "b4": tratar_nota(m.group("n4")),
            "nucleo": "Técnico" if is_tecnico else "Comum",
            "faltas": int(m.group("t_faltas")),   # T. Faltas (total da disciplina)
            "freq": float(m.group("freq").replace(",", "."))  # % de frequência da disciplina
        })

    return disciplinas


def imagens_usadas_na_pagina(pagina):
    """
    Retorna {nome_xobject: (bytes, mime, largura*altura)} apenas das imagens que a
    página realmente desenha (o pypdf lista todas as imagens compartilhadas do
    documento em todas as páginas, então é preciso olhar o content stream).
    """
    usadas = {}
    try:
        recursos = pagina.get("/Resources")
        xobjs = recursos.get_object().get("/XObject") if recursos else None
        if not xobjs:
            return usadas
        xobjs = xobjs.get_object()

        nomes = set()
        cs = ContentStream(pagina.get_contents(), pagina.pdf)
        for operandos, operador in cs.operations:
            if operador == b"Do" and operandos and operandos[0] in xobjs:
                if xobjs[operandos[0]].get_object().get("/Subtype") == "/Image":
                    nomes.add(str(operandos[0]).lstrip("/"))

        for img in pagina.images:
            base, _, ext = img.name.rpartition(".")
            if base in nomes:
                ext = ext.lower()
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                try:
                    w, h = img.image.size
                except Exception:
                    w, h = 0, 0
                usadas[base] = (img.data, mime, w * h)
    except Exception as e:
        print(f"Aviso: não foi possível ler imagens da página: {e}")
    return usadas


def agrupar_paginas_por_aluno(leitor_pdf, extrair_fotos=True):
    """
    Agrupa as páginas por boletim (cada boletim começa na página que contém o
    título) e identifica a foto do aluno, descartando o logo do IFSP, que se
    repete em todas as páginas.
    Retorna lista de dicts {"texto": str, "foto": "data:image/...;base64,..." | None}.
    """
    grupos = []
    for pagina in leitor_pdf.pages:
        texto = pagina.extract_text() or ""
        e_inicio = "BOLETIM DE NOTAS INDIVIDUAL" in texto
        imagens = imagens_usadas_na_pagina(pagina) if extrair_fotos else {}

        if e_inicio or not grupos:
            grupos.append({"texto": texto + "\n", "imgs_inicio": imagens, "imgs_outras": set()})
        else:
            grupos[-1]["texto"] += texto + "\n"
            grupos[-1]["imgs_outras"].update(imagens.keys())

    for g in grupos:
        # logo = imagem que também aparece nas páginas de continuação
        candidatas = {n: v for n, v in g["imgs_inicio"].items() if n not in g["imgs_outras"]}
        g["foto"] = None
        if candidatas:
            dados, mime, _ = max(candidatas.values(), key=lambda v: v[2])
            g["foto"] = f"data:{mime};base64,{base64.b64encode(dados).decode('ascii')}"
    return grupos


def processar_dados_completos(caminho_boletim, nome_turma, nome_arquivo_saida, caminho_napne=None, extrair_fotos=True):
    """
    Lê o PDF de boletins, cruza com o PDF de apontamentos do NAPNE e exporta o JSON final.
    """
    dados_napne_extra = carregar_dados_napne_pdf(caminho_napne)

    alunos_mapeados = {}
    numero_chamada = 1

    print(f"Lendo PDF de boletins: {caminho_boletim}...")
    try:
        with open(caminho_boletim, "rb") as f:
            memoria_pdf = io.BytesIO(f.read())
            leitor_pdf = PdfReader(memoria_pdf)
    except Exception as e:
        print(f"Erro crítico ao ler o PDF de boletins: {e}")
        return

    # Cada boletim começa na página com o título; páginas seguintes sem título
    # (continuação das disciplinas) ficam no mesmo grupo do aluno.
    grupos = agrupar_paginas_por_aluno(leitor_pdf, extrair_fotos)

    for grupo in grupos:
        bloco = grupo["texto"]
        if "Disciplina" not in bloco and "TÉCNICO" not in bloco:
            continue

        bloco_limpo = re.sub(r'\s+', ' ', bloco)
        nome_aluno = ""
        matricula_aluno = ""
        serie_aluno = ""
        freq_global = 100.0

        # Nome do Aluno
        m_nome = re.search(r"Aluno\(a\):\s*([^\n|]+)", bloco, re.IGNORECASE)
        if m_nome:
            nome_aluno = m_nome.group(1).strip()
            nome_aluno = re.sub(r"Matrícula:.*", "", nome_aluno, flags=re.IGNORECASE).strip()

        if not nome_aluno:
            m_nome2 = re.search(r"BOLETIM DE NOTAS INDIVIDUAL\s*([^\n]+)", bloco, re.IGNORECASE)
            if m_nome2:
                nome_aluno = m_nome2.group(1).strip()

        # Matrícula e Turma (a sala/turma é lida do próprio boletim)
        m_mat = re.search(r"BT\d{7}", bloco)
        if m_mat:
            matricula_aluno = m_mat.group(0).strip()

        m_turma = re.search(r"Turma\s*:\s*([A-Za-z0-9._-]+)", bloco_limpo, re.IGNORECASE)
        turma_aluno = m_turma.group(1).strip() if m_turma else ""
        if not turma_aluno:
            m_turma2 = re.search(r"202\d+\.\d+\.[A-Z0-9.]+", bloco_limpo)
            if m_turma2:
                turma_aluno = m_turma2.group(0).strip()
        serie_aluno = turma_aluno

        # Curso (linha "Curso: ..." do cabeçalho do boletim)
        curso_aluno = ""
        m_curso = re.search(r"Curso\s*:\s*([^\n]+)", bloco, re.IGNORECASE)
        if m_curso:
            curso_aluno = m_curso.group(1).strip()

        # Frequência global
        m_freq = re.search(r"Frequência\s*:\s*\|?\s*(\d+[\.,]?\d*)\s*%", bloco_limpo, re.IGNORECASE)
        if m_freq:
            freq_global = float(m_freq.group(1).replace(",", "."))

        if not nome_aluno or nome_aluno == "Não Identificado" or nome_aluno.startswith("BT30"):
            continue

        prontuario_chave = matricula_aluno if matricula_aluno else f"SEM_MATRICULA_{numero_chamada}"

        if prontuario_chave not in alunos_mapeados:
            pne_texto = []
            m_pne = re.search(r"Portador\(a\)\s+de\s+Necessidades\s+Especiais\s*:?\s*(Sim|Não)", bloco_limpo, re.IGNORECASE)
            if m_pne and m_pne.group(1).capitalize() == "Sim":
                m_tip_pne = re.search(r"Tipo\s+de\s+Necessidade\s+Especial\s*:?\s*(.*?)(?=Portador|\bTranstorno\b|\bSuperdotação\b|Disciplina|\Z)", bloco_limpo, re.IGNORECASE)
                pne_texto.append(f"PNE: {m_tip_pne.group(1).strip() if m_tip_pne else '-'}")

            m_trans = re.search(r"Portador\(a\)\s+de\s+Transtorno\s*:?\s*(Sim|Não)", bloco_limpo, re.IGNORECASE)
            if m_trans and m_trans.group(1).capitalize() == "Sim":
                m_tip_trans = re.search(r"Tipo\s+de\s+Transtorno\s*:?\s*(.*?)(?=Portador|\bSuperdotação\b|Disciplina|\Z)", bloco_limpo, re.IGNORECASE)
                pne_texto.append(f"Transtorno: {m_tip_trans.group(1).strip() if m_tip_trans else '-'}")

            chave_busca_napne = prontuario_chave.upper()
            possui_napne_externo = chave_busca_napne in dados_napne_extra

            if possui_napne_externo:
                pne_texto.append(f"Laudo NAPNE: {dados_napne_extra[chave_busca_napne]}")

            info_napne = " | ".join(pne_texto) if pne_texto else "Nenhum registro de PNE/Transtorno/Superdotação."

            alunos_mapeados[prontuario_chave] = {
                "num_chamada": numero_chamada,
                "prontuario": prontuario_chave,
                "nome": nome_aluno,
                "curso": curso_aluno or (serie_aluno if serie_aluno else "Técnico Integrado"),
                "turma": turma_aluno or nome_turma or "Sem turma",
                "frequencia": freq_global,
                "napne": (len(pne_texto) > 0 or possui_napne_externo),
                "pneInfo": info_napne,
                "deliberacao": "",
                "foto": grupo["foto"],
                "disciplinas": []
            }
            numero_chamada += 1

        # Disciplinas (notas e faltas lidas da linha estruturada)
        existentes = {d["nome"] for d in alunos_mapeados[prontuario_chave]["disciplinas"]}
        for disc in extrair_disciplinas(bloco):
            if disc["nome"] not in existentes:
                alunos_mapeados[prontuario_chave]["disciplinas"].append(disc)
                existentes.add(disc["nome"])

    lista_final_alunos = list(alunos_mapeados.values())

    diretorio_saida = os.path.dirname(os.path.abspath(nome_arquivo_saida))
    os.makedirs(diretorio_saida, exist_ok=True)

    with open(nome_arquivo_saida, "w", encoding="utf-8", newline="\n") as f:
        json.dump(lista_final_alunos, f, ensure_ascii=False, indent=4)

    print(f"\n[SUCESSO] Processamento concluído: {len(lista_final_alunos)} alunos mapeados.")
    print(f"Estrutura gerada com sucesso em: '{nome_arquivo_saida}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CLI para Processamento de Boletins e Relatórios em PDF do NAPNE - IFSP Boituva."
    )

    parser.add_argument("-b", "--boletim", required=True, help="Caminho do arquivo PDF contendo os boletins.")
    parser.add_argument("-n", "--napne", default=None, help="Caminho do arquivo PDF contendo os apontamentos do NAPNE (opcional).")
    parser.add_argument("-t", "--turma", default="Redes 1", help="Nome da turma correspondente (Padrão: Redes 1).")
    parser.add_argument("-o", "--output", default="dados_alunos.json", help="Nome do arquivo JSON gerado (Padrão: dados_alunos.json).")

    parser.add_argument("--sem-fotos", action="store_true", help="Não extrai as fotos dos alunos (JSON menor).")

    args = parser.parse_args()

    if not os.path.exists(args.boletim):
        print(f"Erro Crítico: O PDF de boletins '{args.boletim}' não existe.")
        exit(1)

    if args.napne and not os.path.exists(args.napne):
        print(f"Erro Crítico: O PDF do NAPNE '{args.napne}' não existe.")
        exit(1)

    processar_dados_completos(
        caminho_boletim=args.boletim,
        nome_turma=args.turma,
        nome_arquivo_saida=args.output,
        caminho_napne=args.napne,
        extrair_fotos=not args.sem_fotos
    )