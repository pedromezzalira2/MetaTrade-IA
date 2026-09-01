import os
import re
import json
import time
import threading
from pathlib import Path
from datetime import datetime, time as horario, timedelta

import requests
from dotenv import load_dotenv

from config_eas import CONFIGURACAO_EAS


# ============================================================
# CONFIGURAÇÃO BÁSICA
# ============================================================

PASTA_BASE = Path(__file__).resolve().parent

load_dotenv(PASTA_BASE / ".env")

BUCKET_HISTORICO = "historico"
MODELO_GEMINI = "gemini-3.5-flash-lite"

INTERVALO_IA_SEGUNDOS = 300

HORA_INICIO_IA = horario(9, 0)
HORA_FIM_IA = horario(18, 0)

CAMINHO_RESULTADO_IA = PASTA_BASE / "resultIA.py"

# Este arquivo impede que o histórico de 6 meses seja reenviado
# depois de restart/reload do servidor.
CAMINHO_ESTADO_IA = PASTA_BASE / ".ia_estado.json"


# ============================================================
# LOCKS
# ============================================================

_LOCK_CONFIGURACAO = threading.Lock()
_LOCK_RESULTADO_IA = threading.Lock()
_LOCK_ESTADO_IA = threading.Lock()
_LOCK_INICIALIZACAO_IA = threading.Lock()

_THREAD_IA = None
_ROTINA_IA_INICIADA = False


# ============================================================
# FUNÇÕES ORIGINAIS DE CONFIGURAÇÃO DAS EAs
# ============================================================

def _localizar_bloco_ea(conteudo: str, nome_ea: str):
    """
    Localiza o bloco de uma EA dentro do CONFIGURACAO_EAS
    no arquivo config_eas.py.
    """

    marcador = f'"{nome_ea}"'

    inicio_nome = conteudo.find(marcador)

    if inicio_nome == -1:
        return None

    inicio_bloco = conteudo.find("{", inicio_nome)

    if inicio_bloco == -1:
        return None

    nivel = 0

    for indice in range(inicio_bloco, len(conteudo)):
        caractere = conteudo[indice]

        if caractere == "{":
            nivel += 1

        elif caractere == "}":
            nivel -= 1

            if nivel == 0:
                return inicio_bloco, indice + 1

    return None


def _gravar_atomicamente(caminho: Path, conteudo: str):
    """
    Grava primeiro em arquivo temporário e depois substitui
    o arquivo final.
    """

    temporario = caminho.with_name(
        caminho.name + f".tmp.{os.getpid()}.{threading.get_ident()}"
    )

    try:
        with open(temporario, "w", encoding="utf-8") as arquivo:
            arquivo.write(conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())

        os.replace(temporario, caminho)

    finally:
        if temporario.exists():
            try:
                temporario.unlink()
            except Exception:
                pass


def atualizar_autorizacao_ea(nome_ea: str, autorizada: bool) -> bool:
    """
    Atualiza o campo 'autorizada' da EA no config_eas.py
    e também atualiza CONFIGURACAO_EAS em memória.
    """

    if nome_ea not in CONFIGURACAO_EAS:
        return False

    caminho_config = PASTA_BASE / "config_eas.py"

    with _LOCK_CONFIGURACAO:

        conteudo = caminho_config.read_text(encoding="utf-8")

        localizacao = _localizar_bloco_ea(conteudo, nome_ea)

        if localizacao is None:
            raise RuntimeError(
                f"Não foi possível localizar a EA {nome_ea} no config_eas.py"
            )

        inicio, fim = localizacao

        bloco = conteudo[inicio:fim]

        novo_valor = "True" if autorizada else "False"

        novo_bloco, quantidade = re.subn(
            r'("autorizada"\s*:\s*)(True|False)',
            rf"\g<1>{novo_valor}",
            bloco,
            count=1,
        )

        if quantidade != 1:
            raise RuntimeError(
                f"Campo 'autorizada' não encontrado para {nome_ea}"
            )

        novo_conteudo = conteudo[:inicio] + novo_bloco + conteudo[fim:]

        _gravar_atomicamente(caminho_config, novo_conteudo)

        CONFIGURACAO_EAS[nome_ea]["autorizada"] = autorizada

    return True


def obter_autorizacao_ea(nome_ea: str):
    configuracao = CONFIGURACAO_EAS.get(nome_ea)

    if configuracao is None:
        return None

    return bool(configuracao.get("autorizada", False))


def obter_autorizacoes_eas():
    return {
        nome_ea: bool(configuracao.get("autorizada", False))
        for nome_ea, configuracao in CONFIGURACAO_EAS.items()
    }


# ============================================================
# VARIÁVEIS DE AMBIENTE
# ============================================================

def _obter_variavel(nome: str) -> str:
    valor = os.getenv(nome, "").strip()

    if not valor:
        raise RuntimeError(
            f"Variável {nome} não configurada no .env"
        )

    return valor


def _configuracao_supabase():
    return {
        "url": _obter_variavel("SUPABASE_URL").rstrip("/"),
        "key": _obter_variavel("SUPABASE_ANON_KEY"),
        "prefix": _obter_variavel("SUPABASE_HISTORICO_PREFIX").strip("/"),
    }


def _gemini_api_key():
    return _obter_variavel("GEMINI_API_KEY")


# ============================================================
# ESTADO PERSISTENTE DA IA
# ============================================================

def _estado_padrao():
    return {
        "historico_processado": {}
    }


def _carregar_estado_ia():
    with _LOCK_ESTADO_IA:

        if not CAMINHO_ESTADO_IA.exists():
            return _estado_padrao()

        try:
            conteudo = CAMINHO_ESTADO_IA.read_text(
                encoding="utf-8"
            ).strip()

            if not conteudo:
                return _estado_padrao()

            estado = json.loads(conteudo)

            if not isinstance(estado, dict):
                return _estado_padrao()

            if "historico_processado" not in estado:
                estado["historico_processado"] = {}

            return estado

        except Exception as erro:
            print(
                f"IA ESTADO AVISO | "
                f"não foi possível ler .ia_estado.json | "
                f"{type(erro).__name__}: {erro}",
                flush=True,
            )

            return _estado_padrao()


def _salvar_estado_ia(estado):
    with _LOCK_ESTADO_IA:
        conteudo = json.dumps(
            estado,
            ensure_ascii=False,
            indent=2,
        )

        _gravar_atomicamente(
            CAMINHO_ESTADO_IA,
            conteudo + "\n",
        )


def _historico_ja_processado_hoje(ativo: str) -> bool:
    estado = _carregar_estado_ia()

    data_salva = (
        estado
        .get("historico_processado", {})
        .get(ativo)
    )

    return data_salva == datetime.now().date().isoformat()


def _marcar_historico_processado_hoje(ativo: str):
    estado = _carregar_estado_ia()

    estado.setdefault(
        "historico_processado",
        {}
    )

    estado["historico_processado"][ativo] = (
        datetime.now().date().isoformat()
    )

    _salvar_estado_ia(estado)


# ============================================================
# SUPABASE STORAGE
# ============================================================

def _headers_supabase():
    config = _configuracao_supabase()

    return {
        "apikey": config["key"],
        "Authorization": f"Bearer {config['key']}",
        "Content-Type": "application/json",
    }


def _listar_arquivos_historico():
    config = _configuracao_supabase()

    url = (
        f"{config['url']}/storage/v1/object/list/"
        f"{BUCKET_HISTORICO}"
    )

    payload = {
        "prefix": config["prefix"],
        "limit": 1000,
        "offset": 0,
        "sortBy": {
            "column": "name",
            "order": "asc",
        },
    }

    resposta = requests.post(
        url,
        headers=_headers_supabase(),
        json=payload,
        timeout=30,
    )

    resposta.raise_for_status()

    dados = resposta.json()

    if not isinstance(dados, list):
        raise RuntimeError(
            "Resposta inesperada ao listar arquivos do Supabase."
        )

    return dados


def _baixar_arquivo_storage(nome_arquivo: str) -> str:
    config = _configuracao_supabase()

    caminho_objeto = (
        f"{config['prefix']}/{nome_arquivo}"
        if config["prefix"]
        else nome_arquivo
    )

    url = (
        f"{config['url']}/storage/v1/object/"
        f"{BUCKET_HISTORICO}/{caminho_objeto}"
    )

    resposta = requests.get(
        url,
        headers={
            "apikey": config["key"],
            "Authorization": f"Bearer {config['key']}",
        },
        timeout=60,
    )

    resposta.raise_for_status()

    return resposta.text


def _mapear_arquivos_por_ativo():
    arquivos = _listar_arquivos_historico()

    mapa = {}

    sufixo_historico = "_M5_6MESES.csv"
    sufixo_hoje = "_M5_HOJE.csv"

    for item in arquivos:

        nome = item.get("name", "")

        if not nome:
            continue

        if nome.endswith(sufixo_historico):

            ativo = nome[:-len(sufixo_historico)]

            mapa.setdefault(ativo, {})
            mapa[ativo]["historico"] = nome

        elif nome.endswith(sufixo_hoje):

            ativo = nome[:-len(sufixo_hoje)]

            mapa.setdefault(ativo, {})
            mapa[ativo]["hoje"] = nome

    return mapa


# ============================================================
# GEMINI
# ============================================================

def _chamar_gemini(prompt: str, timeout=120) -> str:
    api_key = _gemini_api_key()

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODELO_GEMINI}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
        },
    }

    resposta = requests.post(
        url,
        params={"key": api_key},
        headers={
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=timeout,
    )

    if resposta.status_code == 429:

        try:
            detalhe = resposta.json()
            detalhe = json.dumps(
                detalhe,
                ensure_ascii=False,
            )
        except Exception:
            detalhe = resposta.text

        raise RuntimeError(
            f"GEMINI_429 | {detalhe[:4000]}"
        )

    if not resposta.ok:

        try:
            detalhe = resposta.text[:4000]
        except Exception:
            detalhe = "sem corpo de resposta"

        raise RuntimeError(
            f"GEMINI_HTTP_{resposta.status_code} | {detalhe}"
        )

    dados = resposta.json()

    try:
        partes = (
            dados["candidates"][0]
            ["content"]["parts"]
        )

        textos = []

        for parte in partes:
            texto = parte.get("text")

            if texto:
                textos.append(texto)

        resposta_texto = "\n".join(textos).strip()

    except Exception as erro:
        raise RuntimeError(
            f"Resposta inesperada do Gemini: {dados}"
        ) from erro

    if not resposta_texto:
        raise RuntimeError(
            "Gemini retornou resposta vazia."
        )

    return resposta_texto


# ============================================================
# CONTEXTO HISTÓRICO
# ============================================================

_contexto_historico = {}


def _prompt_historico(ativo: str, csv_historico: str):
    return f"""
Você é um módulo de análise quantitativa de mercado.

ATIVO:
{ativo}

Abaixo está o histórico de candles M5 de aproximadamente
6 meses.

Sua tarefa NÃO é autorizar operações e NÃO é escolher
estratégias.

Analise exclusivamente características estruturais úteis
para contextualizar o comportamento intraday atual.

Resuma de forma COMPACTA informações como:

- comportamento típico de tendência;
- comportamento em lateralização;
- volatilidade;
- expansão e contração;
- reversões;
- continuidade;
- características recorrentes do ativo;
- comportamento de preço relevante para análise M5.

O resultado será reutilizado durante todo o dia junto com
os candles do dia atual.

Não devolva JSON.
Não dê ordem de compra ou venda.
Não recomende setup.

Produza apenas um contexto histórico compacto.

CSV HISTÓRICO:

{csv_historico}
""".strip()


def _gerar_contexto_historico(
    ativo: str,
    nome_arquivo: str,
):
    csv_historico = _baixar_arquivo_storage(
        nome_arquivo
    )

    prompt = _prompt_historico(
        ativo,
        csv_historico,
    )

    contexto = _chamar_gemini(
        prompt,
        timeout=180,
    )

    _contexto_historico[ativo] = contexto

    # IMPORTANTE:
    # Só marca como processado DEPOIS que o Gemini
    # respondeu com sucesso.
    _marcar_historico_processado_hoje(ativo)

    print(
        f"IA HISTORICO OK | {ativo} | "
        f"{datetime.now().isoformat(timespec='seconds')}",
        flush=True,
    )

    return contexto


# ============================================================
# RECUPERAÇÃO DO CONTEXTO APÓS RESTART
# ============================================================

def _caminho_contexto_historico(ativo: str):
    nome_seguro = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        ativo,
    )

    return PASTA_BASE / f".ia_contexto_{nome_seguro}.txt"


def _salvar_contexto_historico_disco(
    ativo: str,
    contexto: str,
):
    caminho = _caminho_contexto_historico(ativo)

    _gravar_atomicamente(
        caminho,
        contexto,
    )


def _carregar_contexto_historico_disco(
    ativo: str,
):
    caminho = _caminho_contexto_historico(ativo)

    if not caminho.exists():
        return None

    try:
        contexto = caminho.read_text(
            encoding="utf-8"
        ).strip()

        return contexto or None

    except Exception:
        return None


def _obter_contexto_historico(
    ativo: str,
    nome_arquivo_historico: str,
):
    # Primeiro tenta RAM.
    contexto = _contexto_historico.get(ativo)

    if contexto:
        return contexto

    # Se já processamos hoje, tenta recuperar o contexto
    # salvo em disco. Assim restart NÃO reenvia os 6 meses.
    if _historico_ja_processado_hoje(ativo):

        contexto = _carregar_contexto_historico_disco(
            ativo
        )

        if contexto:
            _contexto_historico[ativo] = contexto

            print(
                f"IA HISTORICO CACHE | {ativo} | "
                f"reutilizando contexto de hoje",
                flush=True,
            )

            return contexto

        # Situação excepcional:
        # estado diz processado, mas o contexto sumiu.
        # Nesse caso não vamos gastar 6 meses automaticamente.
        raise RuntimeError(
            f"Histórico de {ativo} já foi processado hoje, "
            f"mas o arquivo de contexto não foi encontrado."
        )

    # Ainda não processado hoje: UMA chamada do histórico.
    contexto = _gerar_contexto_historico(
        ativo,
        nome_arquivo_historico,
    )

    _salvar_contexto_historico_disco(
        ativo,
        contexto,
    )

    return contexto


# ============================================================
# ANÁLISE DO DIA ATUAL
# ============================================================

def _prompt_analise_atual(
    ativo: str,
    contexto_historico: str,
    csv_hoje: str,
):
    agora = datetime.now()

    valido_ate = (
        agora + timedelta(minutes=5)
    ).replace(microsecond=0)

    return f"""
Você é um módulo de leitura quantitativa do mercado.

Você NÃO autoriza operações.
Você NÃO escolhe setups.
Você NÃO decide se uma EA pode operar.

Sua única função é estimar o estado provável do mercado
para os próximos aproximadamente 5 minutos.

ATIVO:
{ativo}

HORÁRIO ATUAL:
{agora.isoformat(timespec="seconds")}

CONTEXTO HISTÓRICO DO ATIVO:
{contexto_historico}

CANDLES M5 DO DIA ATUAL:
{csv_hoje}

Analise o comportamento recente dando maior importância
aos candles mais novos.

Retorne SOMENTE JSON válido, sem markdown, sem explicação
antes ou depois.

Formato obrigatório:

{{
  "ativo": "{ativo}",
  "direcao": "COMPRA|VENDA|NEUTRA",
  "forca": 0,
  "tendencia": "ALTA|BAIXA|LATERAL",
  "volatilidade": "BAIXA|MEDIA|ALTA",
  "confianca": 0,
  "risco": "BAIXO|MEDIO|ALTO",
  "estadoMercado": "FORCA_COMPRADORA_AUMENTANDO|FORCA_COMPRADORA_DIMINUINDO|FORCA_VENDEDORA_AUMENTANDO|FORCA_VENDEDORA_DIMINUINDO|LATERAL|INDEFINIDO",
  "validoAte": "{valido_ate.isoformat()}"
}}

Regras:

- forca deve ser inteiro entre 0 e 100;
- confianca deve ser inteiro entre 0 e 100;
- use exatamente os enums fornecidos;
- validoAte deve representar aproximadamente os próximos
  5 minutos;
- não inclua campos adicionais.
""".strip()


def _extrair_json(texto: str):
    texto = texto.strip()

    if texto.startswith("```"):
        texto = re.sub(
            r"^```(?:json)?\s*",
            "",
            texto,
            flags=re.IGNORECASE,
        )

        texto = re.sub(
            r"\s*```$",
            "",
            texto,
        )

    inicio = texto.find("{")
    fim = texto.rfind("}")

    if inicio == -1 or fim == -1 or fim <= inicio:
        raise RuntimeError(
            f"Gemini não retornou JSON válido: {texto[:500]}"
        )

    return json.loads(
        texto[inicio:fim + 1]
    )


def _normalizar_json_gemini(
    ativo: str,
    texto: str,
):
    dados = _extrair_json(texto)

    direcoes = {
        "COMPRA",
        "VENDA",
        "NEUTRA",
    }

    tendencias = {
        "ALTA",
        "BAIXA",
        "LATERAL",
    }

    volatilidades = {
        "BAIXA",
        "MEDIA",
        "ALTA",
    }

    riscos = {
        "BAIXO",
        "MEDIO",
        "ALTO",
    }

    estados = {
        "FORCA_COMPRADORA_AUMENTANDO",
        "FORCA_COMPRADORA_DIMINUINDO",
        "FORCA_VENDEDORA_AUMENTANDO",
        "FORCA_VENDEDORA_DIMINUINDO",
        "LATERAL",
        "INDEFINIDO",
    }

    direcao = str(
        dados.get("direcao", "NEUTRA")
    ).upper()

    tendencia = str(
        dados.get("tendencia", "LATERAL")
    ).upper()

    volatilidade = str(
        dados.get("volatilidade", "MEDIA")
    ).upper()

    risco = str(
        dados.get("risco", "MEDIO")
    ).upper()

    estado_mercado = str(
        dados.get(
            "estadoMercado",
            "INDEFINIDO",
        )
    ).upper()

    if direcao not in direcoes:
        direcao = "NEUTRA"

    if tendencia not in tendencias:
        tendencia = "LATERAL"

    if volatilidade not in volatilidades:
        volatilidade = "MEDIA"

    if risco not in riscos:
        risco = "MEDIO"

    if estado_mercado not in estados:
        estado_mercado = "INDEFINIDO"

    try:
        forca = int(dados.get("forca", 0))
    except Exception:
        forca = 0

    try:
        confianca = int(
            dados.get("confianca", 0)
        )
    except Exception:
        confianca = 0

    forca = max(0, min(100, forca))
    confianca = max(
        0,
        min(100, confianca),
    )

    # Não confiamos no horário produzido pelo modelo.
    # O servidor determina a validade real.
    valido_ate = (
        datetime.now()
        + timedelta(minutes=5)
    ).replace(microsecond=0).isoformat()

    return {
        "ativo": ativo,
        "direcao": direcao,
        "forca": forca,
        "tendencia": tendencia,
        "volatilidade": volatilidade,
        "confianca": confianca,
        "risco": risco,
        "estadoMercado": estado_mercado,
        "validoAte": valido_ate,
    }


# ============================================================
# RESULTIA.PY
# ============================================================

def _carregar_resultados_atuais():
    if not CAMINHO_RESULTADO_IA.exists():
        return {}

    try:
        namespace = {}

        exec(
            CAMINHO_RESULTADO_IA.read_text(
                encoding="utf-8"
            ),
            {},
            namespace,
        )

        resultados = namespace.get(
            "RESULTADOS_IA",
            {},
        )

        if isinstance(resultados, dict):
            return resultados

    except Exception as erro:
        print(
            f"IA RESULTADO AVISO | "
            f"{type(erro).__name__}: {erro}",
            flush=True,
        )

    return {}


def _salvar_resultado_ia(
    ativo: str,
    resultado: dict,
):
    with _LOCK_RESULTADO_IA:

        resultados = _carregar_resultados_atuais()

        resultados[ativo] = resultado

        conteudo = (
            "# Arquivo atualizado automaticamente pelo helper.py\n"
            "# Não editar manualmente durante a execução do servidor.\n\n"
            "RESULTADOS_IA = "
            + json.dumps(
                resultados,
                ensure_ascii=False,
                indent=4,
            )
            + "\n"
        )

        _gravar_atomicamente(
            CAMINHO_RESULTADO_IA,
            conteudo,
        )


# ============================================================
# PROCESSAMENTO POR ATIVO
# ============================================================

def _processar_ativo(
    ativo: str,
    arquivos: dict,
):
    nome_historico = arquivos.get(
        "historico"
    )

    nome_hoje = arquivos.get(
        "hoje"
    )

    if not nome_historico:
        print(
            f"IA AVISO | {ativo} | "
            f"arquivo 6MESES não encontrado",
            flush=True,
        )

        return

    if not nome_hoje:
        print(
            f"IA AVISO | {ativo} | "
            f"arquivo HOJE não encontrado",
            flush=True,
        )

        return

    contexto = _obter_contexto_historico(
        ativo,
        nome_historico,
    )

    csv_hoje = _baixar_arquivo_storage(
        nome_hoje
    )

    prompt = _prompt_analise_atual(
        ativo,
        contexto,
        csv_hoje,
    )

    resposta = _chamar_gemini(
        prompt,
        timeout=120,
    )

    resultado = _normalizar_json_gemini(
        ativo,
        resposta,
    )

    _salvar_resultado_ia(
        ativo,
        resultado,
    )

    print(
        f"IA OK | {ativo} | "
        f"{resultado['direcao']} | "
        f"forca={resultado['forca']} | "
        f"confianca={resultado['confianca']} | "
        f"validoAte={resultado['validoAte']}",
        flush=True,
    )


# ============================================================
# CICLO DA IA
# ============================================================

def _dentro_horario_ia(agora: datetime):
    hora = agora.time()

    return (
        HORA_INICIO_IA
        <= hora
        <= HORA_FIM_IA
    )


def _slot_cinco_minutos(agora: datetime):
    return (
        agora.year,
        agora.month,
        agora.day,
        agora.hour,
        agora.minute // 5,
    )


def _rotina_background_ia():
    print(
        "ROTINA IA INICIADA | "
        "09:00-18:00 | intervalo=5 minutos",
        flush=True,
    )

    ultimo_slot = None

    while True:

        try:
            agora = datetime.now()

            if not _dentro_horario_ia(agora):
                time.sleep(10)
                continue

            # Executa somente nos minutos:
            # 00, 05, 10, 15, ...
            if agora.minute % 5 != 0:
                time.sleep(5)
                continue

            slot = _slot_cinco_minutos(
                agora
            )

            if slot == ultimo_slot:
                time.sleep(5)
                continue

            ultimo_slot = slot

            mapa = _mapear_arquivos_por_ativo()

            if not mapa:
                print(
                    "IA AVISO | nenhum ativo encontrado "
                    "no bucket historico",
                    flush=True,
                )

                time.sleep(5)
                continue

            for ativo, arquivos in mapa.items():

                try:
                    _processar_ativo(
                        ativo,
                        arquivos,
                    )

                except Exception as erro:

                    mensagem = (
                        f"{type(erro).__name__}: {erro}"
                    )

                    if "GEMINI_429" in mensagem:
                        print(
                            f"IA 429 | {ativo} | "
                            f"{mensagem}",
                            flush=True,
                        )

                    else:
                        print(
                            f"IA ERRO | {ativo} | "
                            f"{mensagem}",
                            flush=True,
                        )

        except Exception as erro:

            print(
                f"ROTINA IA ERRO | "
                f"{type(erro).__name__}: {erro}",
                flush=True,
            )

        time.sleep(5)


# ============================================================
# INICIALIZAÇÃO
# ============================================================

def iniciar_rotina_ia():
    global _THREAD_IA
    global _ROTINA_IA_INICIADA

    with _LOCK_INICIALIZACAO_IA:

        if _ROTINA_IA_INICIADA:
            return

        if (
            _THREAD_IA is not None
            and _THREAD_IA.is_alive()
        ):
            return

        _ROTINA_IA_INICIADA = True

        _THREAD_IA = threading.Thread(
            target=_rotina_background_ia,
            name="RotinaIA",
            daemon=True,
        )

        _THREAD_IA.start()


# O main.py importa atualizar_autorizacao_ea deste módulo.
# Portanto, a rotina inicia automaticamente quando helper.py
# é importado.
iniciar_rotina_ia()