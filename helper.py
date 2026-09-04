import os
import re
import json
import time
import threading
from pprint import pformat

from pathlib import Path
from datetime import (
    datetime,
    time as horario,
    timedelta,
)

import requests

from dotenv import load_dotenv

from config_eas import CONFIGURACAO_EAS


# ============================================================
# CONFIGURAÇÃO BÁSICA
# ============================================================

PASTA_BASE = Path(__file__).resolve().parent

load_dotenv(
    PASTA_BASE / ".env"
)

BUCKET_HISTORICO = "historico"

MODELO_GEMINI = "gemini-3.5-flash-lite"

INTERVALO_IA_SEGUNDOS = 300

HORA_INICIO_IA = horario(
    9,
    0,
)

HORA_FIM_IA = horario(
    18,
    0,
)

CAMINHO_RESULTADO_IA = (
    PASTA_BASE
    / "resultIA.py"
)

CAMINHO_ESTADO_IA = (
    PASTA_BASE
    / ".ia_estado.json"
)

CAMINHO_RELOAD_IARA = (
    PASTA_BASE
    / ".iara_reload.json"
)


# ============================================================
# DIRETÓRIO LOCAL DOS CSVs
#
# Se PASTA_HISTORICO_LOCAL não estiver no .env:
#
# MetaTraderEA/
#     historico/
#         WIN..._M5_HOJE.csv
#         WIN..._M5_6MESES.csv
#
# ============================================================

_pasta_historico_env = (
    os.getenv(
        "PASTA_HISTORICO_LOCAL",
        "",
    )
    .strip()
)

if _pasta_historico_env:

    PASTA_HISTORICO_LOCAL = (
        Path(
            _pasta_historico_env
        )
        .expanduser()
        .resolve()
    )

else:

    PASTA_HISTORICO_LOCAL = (
        PASTA_BASE
        / "historico"
    )


# ============================================================
# LOCKS
# ============================================================

_LOCK_CONFIGURACAO = (
    threading.Lock()
)

_LOCK_RESULTADO_IA = (
    threading.Lock()
)

_LOCK_ESTADO_IA = (
    threading.Lock()
)

_LOCK_RELOAD_IARA = (
    threading.Lock()
)

_LOCK_INICIALIZACAO_IA = (
    threading.Lock()
)

_THREAD_IA = None

_ROTINA_IA_INICIADA = False


# ============================================================
# GRAVAÇÃO ATÔMICA
# ============================================================

def _gravar_atomicamente(
    caminho: Path,
    conteudo: str,
):

    temporario = (
        caminho.with_name(
            caminho.name
            + f".tmp.{os.getpid()}."
            + str(
                threading.get_ident()
            )
        )
    )

    try:

        with open(
            temporario,
            "w",
            encoding="utf-8",
        ) as arquivo:

            arquivo.write(
                conteudo
            )

            arquivo.flush()

            os.fsync(
                arquivo.fileno()
            )

        os.replace(
            temporario,
            caminho,
        )

    finally:

        if temporario.exists():

            try:

                temporario.unlink()

            except Exception:

                pass


# ============================================================
# CONFIGURAÇÃO DAS EAs
# ============================================================

def _localizar_bloco_ea(
    conteudo: str,
    nome_ea: str,
):

    marcador = (
        f'"{nome_ea}"'
    )

    inicio_nome = (
        conteudo.find(
            marcador
        )
    )

    if inicio_nome == -1:
        return None

    inicio_bloco = (
        conteudo.find(
            "{",
            inicio_nome,
        )
    )

    if inicio_bloco == -1:
        return None

    nivel = 0

    for indice in range(
        inicio_bloco,
        len(conteudo),
    ):

        caractere = (
            conteudo[indice]
        )

        if caractere == "{":

            nivel += 1

        elif caractere == "}":

            nivel -= 1

            if nivel == 0:

                return (
                    inicio_bloco,
                    indice + 1,
                )

    return None


def atualizar_autorizacao_ea(
    nome_ea: str,
    autorizada: bool,
) -> bool:

    if (
        nome_ea
        not in CONFIGURACAO_EAS
    ):

        return False

    caminho_config = (
        PASTA_BASE
        / "config_eas.py"
    )

    with _LOCK_CONFIGURACAO:

        conteudo = (
            caminho_config.read_text(
                encoding="utf-8"
            )
        )

        localizacao = (
            _localizar_bloco_ea(
                conteudo,
                nome_ea,
            )
        )

        if localizacao is None:

            raise RuntimeError(
                "Não foi possível localizar "
                f"a EA {nome_ea} "
                "no config_eas.py"
            )

        inicio, fim = (
            localizacao
        )

        bloco = (
            conteudo[
                inicio:fim
            ]
        )

        novo_valor = (
            "True"
            if autorizada
            else "False"
        )

        novo_bloco, quantidade = (
            re.subn(
                r'("autorizada"\s*:\s*)'
                r'(True|False)',
                rf"\g<1>{novo_valor}",
                bloco,
                count=1,
            )
        )

        if quantidade != 1:

            raise RuntimeError(
                "Campo 'autorizada' "
                "não encontrado para "
                f"{nome_ea}"
            )

        novo_conteudo = (
            conteudo[:inicio]
            + novo_bloco
            + conteudo[fim:]
        )

        _gravar_atomicamente(
            caminho_config,
            novo_conteudo,
        )

        CONFIGURACAO_EAS[
            nome_ea
        ][
            "autorizada"
        ] = autorizada

    return True


def obter_autorizacao_ea(
    nome_ea: str,
):

    configuracao = (
        CONFIGURACAO_EAS.get(
            nome_ea
        )
    )

    if configuracao is None:

        return None

    return bool(
        configuracao.get(
            "autorizada",
            False,
        )
    )


def obter_autorizacoes_eas():

    return {

        nome_ea: bool(
            configuracao.get(
                "autorizada",
                False,
            )
        )

        for nome_ea, configuracao
        in CONFIGURACAO_EAS.items()
    }


# ============================================================
# RELOAD DA I.ARA
# ============================================================

def _estado_reload_padrao():

    return {
        "reload": False,
        "usuario": None,
        "origem": None,
        "solicitadoEm": None,
        "processadoEm": None,
        "ultimoErro": None,
    }


def _carregar_reload_iara_sem_lock():

    if not CAMINHO_RELOAD_IARA.exists():

        return (
            _estado_reload_padrao()
        )

    try:

        conteudo = (
            CAMINHO_RELOAD_IARA
            .read_text(
                encoding="utf-8"
            )
            .strip()
        )

        if not conteudo:

            return (
                _estado_reload_padrao()
            )

        dados = json.loads(
            conteudo
        )

        if not isinstance(
            dados,
            dict,
        ):

            return (
                _estado_reload_padrao()
            )

        estado = (
            _estado_reload_padrao()
        )

        estado.update(
            dados
        )

        return estado

    except Exception as erro:

        print(
            "IARA RELOAD AVISO | "
            f"{type(erro).__name__}: "
            f"{erro}",
            flush=True,
        )

        return (
            _estado_reload_padrao()
        )


def _salvar_reload_iara_sem_lock(
    estado: dict,
):

    conteudo = (
        json.dumps(
            estado,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )

    _gravar_atomicamente(
        CAMINHO_RELOAD_IARA,
        conteudo,
    )


def solicitar_reload_iara(
    usuario=None,
    origem="IARA",
):

    with _LOCK_RELOAD_IARA:

        estado = (
            _carregar_reload_iara_sem_lock()
        )

        estado[
            "reload"
        ] = True

        estado[
            "usuario"
        ] = usuario

        estado[
            "origem"
        ] = origem

        estado[
            "solicitadoEm"
        ] = (
            datetime.now()
            .replace(
                microsecond=0
            )
            .isoformat()
        )

        estado[
            "ultimoErro"
        ] = None

        _salvar_reload_iara_sem_lock(
            estado
        )

        print(
            "IARA RELOAD SOLICITADO | "
            f"usuario={usuario} | "
            f"origem={origem}",
            flush=True,
        )

        return dict(
            estado
        )


def obter_estado_reload_iara():

    with _LOCK_RELOAD_IARA:

        return dict(
            _carregar_reload_iara_sem_lock()
        )


def definir_estado_reload_iara(
    reload: bool,
    usuario=None,
    origem="EXPORTADORA",
):

    with _LOCK_RELOAD_IARA:

        estado = (
            _carregar_reload_iara_sem_lock()
        )

        estado["reload"] = bool(reload)

        if usuario is not None:
            estado["usuario"] = usuario

        if origem:
            estado["origem"] = origem

        if reload:
            estado["solicitadoEm"] = (
                datetime.now()
                .replace(microsecond=0)
                .isoformat()
            )
            estado["ultimoErro"] = None
        else:
            estado["processadoEm"] = (
                datetime.now()
                .replace(microsecond=0)
                .isoformat()
            )
            estado["ultimoErro"] = None

        _salvar_reload_iara_sem_lock(
            estado
        )

        return dict(estado)


def _reload_iara_pendente():

    estado = (
        obter_estado_reload_iara()
    )

    return bool(
        estado.get(
            "reload",
            False,
        )
    )


def _concluir_reload_iara():

    with _LOCK_RELOAD_IARA:

        estado = (
            _carregar_reload_iara_sem_lock()
        )

        estado[
            "reload"
        ] = False

        estado[
            "processadoEm"
        ] = (
            datetime.now()
            .replace(
                microsecond=0
            )
            .isoformat()
        )

        estado[
            "ultimoErro"
        ] = None

        _salvar_reload_iara_sem_lock(
            estado
        )

    print(
        "IARA RELOAD CONCLUÍDO",
        flush=True,
    )


def _registrar_erro_reload_iara(
    erro,
):

    with _LOCK_RELOAD_IARA:

        estado = (
            _carregar_reload_iara_sem_lock()
        )

        estado[
            "ultimoErro"
        ] = (
            f"{type(erro).__name__}: "
            f"{erro}"
        )

        # IMPORTANTE:
        # reload permanece TRUE.
        #
        # Portanto uma falha não é tratada
        # como atualização concluída.

        _salvar_reload_iara_sem_lock(
            estado
        )


# ============================================================
# VARIÁVEIS DE AMBIENTE
# ============================================================

def _obter_variavel(
    nome: str,
) -> str:

    valor = (
        os.getenv(
            nome,
            "",
        )
        .strip()
    )

    if not valor:

        raise RuntimeError(
            f"Variável {nome} "
            "não configurada no .env"
        )

    return valor


def _configuracao_supabase():

    return {
        "url": (
            _obter_variavel(
                "SUPABASE_URL"
            )
            .rstrip("/")
        ),

        "key": (
            _obter_variavel(
                "SUPABASE_ANON_KEY"
            )
        ),

        "prefix": (
            os.getenv(
                "SUPABASE_HISTORICO_PREFIX",
                "",
            )
            .strip("/")
        ),
    }


def _gemini_api_key():

    return _obter_variavel(
        "GEMINI_API_KEY"
    )


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

            return (
                _estado_padrao()
            )

        try:

            conteudo = (
                CAMINHO_ESTADO_IA
                .read_text(
                    encoding="utf-8"
                )
                .strip()
            )

            if not conteudo:

                return (
                    _estado_padrao()
                )

            estado = (
                json.loads(
                    conteudo
                )
            )

            if not isinstance(
                estado,
                dict,
            ):

                return (
                    _estado_padrao()
                )

            if (
                "historico_processado"
                not in estado
            ):

                estado[
                    "historico_processado"
                ] = {}

            return estado

        except Exception as erro:

            print(
                "IA ESTADO AVISO | "
                "não foi possível ler "
                ".ia_estado.json | "
                f"{type(erro).__name__}: "
                f"{erro}",
                flush=True,
            )

            return (
                _estado_padrao()
            )


def _salvar_estado_ia(
    estado,
):

    with _LOCK_ESTADO_IA:

        conteudo = (
            json.dumps(
                estado,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

        _gravar_atomicamente(
            CAMINHO_ESTADO_IA,
            conteudo,
        )


def _historico_ja_processado_hoje(
    ativo: str,
) -> bool:

    estado = (
        _carregar_estado_ia()
    )

    data_salva = (
        estado
        .get(
            "historico_processado",
            {},
        )
        .get(
            ativo
        )
    )

    return (
        data_salva
        == datetime.now()
        .date()
        .isoformat()
    )


def _marcar_historico_processado_hoje(
    ativo: str,
):

    estado = (
        _carregar_estado_ia()
    )

    estado.setdefault(
        "historico_processado",
        {},
    )

    estado[
        "historico_processado"
    ][
        ativo
    ] = (
        datetime.now()
        .date()
        .isoformat()
    )

    _salvar_estado_ia(
        estado
    )


# ============================================================
# SUPABASE STORAGE
# ============================================================

def _headers_supabase():

    config = (
        _configuracao_supabase()
    )

    return {
        "apikey": (
            config["key"]
        ),

        "Authorization": (
            f"Bearer {config['key']}"
        ),

        "Content-Type": (
            "application/json"
        ),
    }


def _listar_arquivos_historico_storage():

    config = (
        _configuracao_supabase()
    )

    url = (
        f"{config['url']}"
        "/storage/v1/object/list/"
        f"{BUCKET_HISTORICO}"
    )

    payload = {
        "prefix": (
            config["prefix"]
        ),

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

    if not isinstance(
        dados,
        list,
    ):

        raise RuntimeError(
            "Resposta inesperada ao "
            "listar arquivos do Supabase."
        )

    return dados


def _baixar_arquivo_storage(
    nome_arquivo: str,
) -> str:

    config = (
        _configuracao_supabase()
    )

    caminho_objeto = (
        f"{config['prefix']}/"
        f"{nome_arquivo}"
        if config["prefix"]
        else nome_arquivo
    )

    url = (
        f"{config['url']}"
        "/storage/v1/object/"
        f"{BUCKET_HISTORICO}/"
        f"{caminho_objeto}"
    )

    resposta = requests.get(
        url,
        headers={
            "apikey": (
                config["key"]
            ),

            "Authorization": (
                f"Bearer {config['key']}"
            ),
        },
        timeout=60,
    )

    resposta.raise_for_status()

    return resposta.text


# ============================================================
# DIRETÓRIO LOCAL DA EXPORTADORA
# ============================================================

def _mapear_nomes_arquivos(
    nomes,
):

    mapa = {}

    sufixo_historico = (
        "_M5_6MESES.csv"
    )

    sufixo_hoje = (
        "_M5_HOJE.csv"
    )

    for referencia in nomes:

        nome = (
            Path(
                str(referencia)
            )
            .name
        )

        if nome.endswith(
            sufixo_historico
        ):

            ativo = (
                nome[
                    :-len(
                        sufixo_historico
                    )
                ]
            )

            mapa.setdefault(
                ativo,
                {},
            )

            mapa[
                ativo
            ][
                "historico"
            ] = referencia

        elif nome.endswith(
            sufixo_hoje
        ):

            ativo = (
                nome[
                    :-len(
                        sufixo_hoje
                    )
                ]
            )

            mapa.setdefault(
                ativo,
                {},
            )

            mapa[
                ativo
            ][
                "hoje"
            ] = referencia

    return mapa


def _mapear_arquivos_locais():

    if (
        not PASTA_HISTORICO_LOCAL.exists()
    ):

        return {}

    referencias = []

    for caminho in (
        PASTA_HISTORICO_LOCAL
        .rglob("*.csv")
    ):

        if not caminho.is_file():
            continue

        referencias.append(
            str(
                caminho.resolve()
            )
        )

    return (
        _mapear_nomes_arquivos(
            referencias
        )
    )


def _mapear_arquivos_storage():

    arquivos = (
        _listar_arquivos_historico_storage()
    )

    nomes = []

    for item in arquivos:

        nome = (
            item.get(
                "name",
                "",
            )
        )

        if nome:

            nomes.append(
                nome
            )

    return (
        _mapear_nomes_arquivos(
            nomes
        )
    )


def _mapear_arquivos_por_ativo():

    # --------------------------------------------------------
    # 1. PRIORIDADE ABSOLUTA:
    #    arquivos já presentes no servidor.
    # --------------------------------------------------------

    mapa_local = (
        _mapear_arquivos_locais()
    )

    completos_locais = {}

    for ativo, arquivos in (
        mapa_local.items()
    ):

        if (
            arquivos.get(
                "historico"
            )
            and arquivos.get(
                "hoje"
            )
        ):

            arquivos[
                "_origem"
            ] = "LOCAL"

            completos_locais[
                ativo
            ] = arquivos

    if completos_locais:

        print(
            "IA ARQUIVOS | "
            "utilizando diretório local | "
            f"{PASTA_HISTORICO_LOCAL}",
            flush=True,
        )

        return (
            completos_locais
        )

    # --------------------------------------------------------
    # 2. FALLBACK:
    #    Supabase Storage.
    # --------------------------------------------------------

    print(
        "IA ARQUIVOS | "
        "arquivos locais não disponíveis | "
        "consultando Supabase Storage",
        flush=True,
    )

    mapa_storage = (
        _mapear_arquivos_storage()
    )

    for arquivos in (
        mapa_storage.values()
    ):

        arquivos[
            "_origem"
        ] = "STORAGE"

    return mapa_storage


def _ler_csv(
    referencia,
) -> str:

    texto_referencia = str(
        referencia
    )

    caminho = Path(
        texto_referencia
    )

    # Arquivo absoluto existente =
    # veio diretamente do diretório.

    if (
        caminho.is_absolute()
        and caminho.exists()
        and caminho.is_file()
    ):

        return (
            caminho.read_text(
                encoding="utf-8",
                errors="replace",
            )
        )

    # Caso contrário é nome do objeto
    # no Supabase Storage.

    return (
        _baixar_arquivo_storage(
            texto_referencia
        )
    )


# ============================================================
# GEMINI
# ============================================================

def _chamar_gemini(
    prompt: str,
    timeout=120,
) -> str:

    api_key = _gemini_api_key()

    modelos = [
        MODELO_GEMINI,             # gemini-3.5-flash-lite
        "gemini-3.1-flash-lite",   # fallback
    ]

    ultimo_erro = None

    for indice, modelo in enumerate(modelos):

        url = (
            "https://generativelanguage."
            "googleapis.com/"
            "v1beta/models/"
            f"{modelo}:"
            "generateContent"
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
        }

        print(
            "GEMINI REQUEST | "
            f"modelo={modelo} | "
            f"chars={len(prompt)} | "
            f"bytes={len(prompt.encode('utf-8'))}",
            flush=True,
        )

        try:

            resposta = requests.post(
                url,
                params={"key": api_key},
                headers={
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=timeout,
            )

        except requests.RequestException as erro:

            raise RuntimeError(
                "GEMINI_REQUEST_ERRO | "
                f"{type(erro).__name__}: "
                f"{erro}"
            ) from erro

        # ====================================================
        # SUCESSO
        # ====================================================

        if resposta.ok:

            try:

                dados = resposta.json()

                texto = (
                    dados["candidates"][0]
                    ["content"]
                    ["parts"][0]
                    ["text"]
                )

            except Exception as erro:

                raise RuntimeError(
                    "GEMINI_RESPOSTA_INVALIDA | "
                    f"{resposta.text}"
                ) from erro

            print(
                "GEMINI OK | "
                f"modelo={modelo} | "
                f"http={resposta.status_code}",
                flush=True,
            )

            return texto

        # ====================================================
        # ERRO
        # ====================================================

        try:

            detalhe = resposta.text

        except Exception:

            detalhe = (
                "Não foi possível ler "
                "o corpo da resposta."
            )

        ultimo_erro = (
            "GEMINI_HTTP_"
            f"{resposta.status_code}"
            f" | {detalhe}"
        )

        # ====================================================
        # FALLBACK SOMENTE PARA 503
        # ====================================================

        if (
            resposta.status_code == 503
            and indice < len(modelos) - 1
        ):

            proximo_modelo = modelos[indice + 1]

            print(
                "GEMINI 503 | "
                f"modelo={modelo} indisponível | "
                f"fallback={proximo_modelo}",
                flush=True,
            )

            continue

        # Qualquer erro diferente de 503
        # mantém o comportamento de falha.
        raise RuntimeError(
            ultimo_erro
        )

    raise RuntimeError(
        ultimo_erro
        or "GEMINI_ERRO_DESCONHECIDO"
    )

# ============================================================
# CONTEXTO HISTÓRICO
# ============================================================

_contexto_historico = {}


def _prompt_historico(
    ativo: str,
    csv_historico: str,
):

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
    referencia_historico,
):

    csv_historico = (
        _ler_csv(
            referencia_historico
        )
    )

    prompt = (
        _prompt_historico(
            ativo,
            csv_historico,
        )
    )

    contexto = (
        _chamar_gemini(
            prompt,
            timeout=180,
        )
    )

    _contexto_historico[
        ativo
    ] = contexto

    # Só marca como processado
    # DEPOIS que Gemini respondeu.

    _marcar_historico_processado_hoje(
        ativo
    )

    print(
        "IA HISTORICO OK | "
        f"{ativo} | "
        f"{datetime.now().isoformat(timespec='seconds')}",
        flush=True,
    )

    return contexto


# ============================================================
# CACHE DO CONTEXTO HISTÓRICO
# ============================================================

def _caminho_contexto_historico(
    ativo: str,
):

    nome_seguro = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        ativo,
    )

    return (
        PASTA_BASE
        / f".ia_contexto_{nome_seguro}.txt"
    )


def _salvar_contexto_historico_disco(
    ativo: str,
    contexto: str,
):

    caminho = (
        _caminho_contexto_historico(
            ativo
        )
    )

    _gravar_atomicamente(
        caminho,
        contexto,
    )


def _carregar_contexto_historico_disco(
    ativo: str,
):

    caminho = (
        _caminho_contexto_historico(
            ativo
        )
    )

    if not caminho.exists():

        return None

    try:

        contexto = (
            caminho.read_text(
                encoding="utf-8"
            )
            .strip()
        )

        return (
            contexto
            or None
        )

    except Exception:

        return None


def _obter_contexto_historico(
    ativo: str,
    referencia_historico,
):

    contexto = (
        _contexto_historico.get(
            ativo
        )
    )

    if contexto:

        return contexto

    if (
        _historico_ja_processado_hoje(
            ativo
        )
    ):

        contexto = (
            _carregar_contexto_historico_disco(
                ativo
            )
        )

        if contexto:

            _contexto_historico[
                ativo
            ] = contexto

            print(
                "IA HISTORICO CACHE | "
                f"{ativo} | "
                "reutilizando contexto "
                "de hoje",
                flush=True,
            )

            return contexto

        raise RuntimeError(
            f"Histórico de {ativo} "
            "já foi processado hoje, "
            "mas o arquivo de contexto "
            "não foi encontrado."
        )

    contexto = (
        _gerar_contexto_historico(
            ativo,
            referencia_historico,
        )
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
        agora
        + timedelta(
            minutes=5
        )
    ).replace(
        microsecond=0
    )

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


# ============================================================
# EXTRAÇÃO DO JSON DO GEMINI
# ============================================================

def _extrair_json(
    texto: str,
):

    texto = (
        texto.strip()
    )

    if texto.startswith(
        "```"
    ):

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

    inicio = (
        texto.find(
            "{"
        )
    )

    fim = (
        texto.rfind(
            "}"
        )
    )

    if (
        inicio == -1
        or fim == -1
        or fim <= inicio
    ):

        raise RuntimeError(
            "Gemini não retornou "
            "JSON válido: "
            f"{texto[:500]}"
        )

    return json.loads(
        texto[
            inicio:fim + 1
        ]
    )


# ============================================================
# NORMALIZAÇÃO DO RESULTADO DA IA
# ============================================================

def _normalizar_json_gemini(
    ativo: str,
    texto: str,
):

    dados = (
        _extrair_json(
            texto
        )
    )

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
        dados.get(
            "direcao",
            "NEUTRA",
        )
    ).upper()

    tendencia = str(
        dados.get(
            "tendencia",
            "LATERAL",
        )
    ).upper()

    volatilidade = str(
        dados.get(
            "volatilidade",
            "MEDIA",
        )
    ).upper()

    risco = str(
        dados.get(
            "risco",
            "MEDIO",
        )
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

    if (
        volatilidade
        not in volatilidades
    ):

        volatilidade = "MEDIA"

    if risco not in riscos:

        risco = "MEDIO"

    if (
        estado_mercado
        not in estados
    ):

        estado_mercado = (
            "INDEFINIDO"
        )

    try:

        forca = int(
            dados.get(
                "forca",
                0,
            )
        )

    except Exception:

        forca = 0

    try:

        confianca = int(
            dados.get(
                "confianca",
                0,
            )
        )

    except Exception:

        confianca = 0

    forca = max(
        0,
        min(
            100,
            forca,
        ),
    )

    confianca = max(
        0,
        min(
            100,
            confianca,
        ),
    )

    # --------------------------------------------------------
    # NÃO confiamos no horario produzido pelo modelo.
    #
    # O próprio servidor define a validade real.
    # --------------------------------------------------------

    valido_ate = (
        datetime.now()
        + timedelta(
            minutes=5
        )
    ).replace(
        microsecond=0
    ).isoformat()

    return {
        "ativo": ativo,
        "direcao": direcao,
        "forca": forca,
        "tendencia": tendencia,
        "volatilidade": volatilidade,
        "confianca": confianca,
        "risco": risco,
        "estadoMercado": (
            estado_mercado
        ),
        "validoAte": (
            valido_ate
        ),
    }


# ============================================================
# RESULTIA.PY
# ============================================================

def _carregar_resultados_atuais():

    if (
        not CAMINHO_RESULTADO_IA.exists()
    ):

        return {}

    try:

        namespace = {}

        exec(
            CAMINHO_RESULTADO_IA
            .read_text(
                encoding="utf-8"
            ),
            {},
            namespace,
        )

        resultados = (
            namespace.get(
                "RESULTADOS_IA",
                {},
            )
        )

        if isinstance(
            resultados,
            dict,
        ):

            return resultados

    except Exception as erro:

        print(
            "IA RESULTADO AVISO | "
            f"{type(erro).__name__}: "
            f"{erro}",
            flush=True,
        )

    return {}


def _salvar_resultado_ia(
    ativo: str,
    resultado: dict,
):

    with _LOCK_RESULTADO_IA:

        resultados = (
            _carregar_resultados_atuais()
        )

        resultados[
            ativo
        ] = resultado

        conteudo = (
            "# Arquivo atualizado "
            "automaticamente pelo helper.py\n"
            "# Não editar manualmente "
            "durante a execução do servidor.\n\n"
            "RESULTADOS_IA = "
            + pformat(
                resultados,
                width=120,
                sort_dicts=False,
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

    referencia_historico = (
        arquivos.get(
            "historico"
        )
    )

    referencia_hoje = (
        arquivos.get(
            "hoje"
        )
    )

    origem = (
        arquivos.get(
            "_origem",
            "DESCONHECIDA",
        )
    )

    if not referencia_historico:

        raise RuntimeError(
            f"{ativo}: arquivo "
            "6MESES não encontrado"
        )

    if not referencia_hoje:

        raise RuntimeError(
            f"{ativo}: arquivo "
            "HOJE não encontrado"
        )

    contexto = (
        _obter_contexto_historico(
            ativo,
            referencia_historico,
        )
    )

    # --------------------------------------------------------
    # Aqui está a mudança importante:
    #
    # se referencia_hoje for caminho absoluto,
    # lê DIRETAMENTE do diretório.
    #
    # se for nome de objeto,
    # baixa do Supabase Storage.
    # --------------------------------------------------------

    csv_hoje = (
        _ler_csv(
            referencia_hoje
        )
    )

    prompt = (
        _prompt_analise_atual(
            ativo,
            contexto,
            csv_hoje,
        )
    )

    resposta = (
        _chamar_gemini(
            prompt,
            timeout=120,
        )
    )

    resultado = (
        _normalizar_json_gemini(
            ativo,
            resposta,
        )
    )

    _salvar_resultado_ia(
        ativo,
        resultado,
    )

    print(
        "IA OK | "
        f"{ativo} | "
        f"origem={origem} | "
        f"{resultado['direcao']} | "
        f"forca={resultado['forca']} | "
        f"confianca={resultado['confianca']} | "
        f"validoAte={resultado['validoAte']}",
        flush=True,
    )


# ============================================================
# HORÁRIO DA IA
# ============================================================

def _dentro_horario_ia(
    agora: datetime,
):

    hora = (
        agora.time()
    )

    return (
        HORA_INICIO_IA
        <= hora
        <= HORA_FIM_IA
    )


def _slot_cinco_minutos(
    agora: datetime,
):

    return (
        agora.year,
        agora.month,
        agora.day,
        agora.hour,
        agora.minute // 5,
    )


# ============================================================
# EXECUTA UM CICLO COMPLETO
# ============================================================

def _executar_ciclo_ia(
    motivo="CICLO",
):

    mapa = (
        _mapear_arquivos_por_ativo()
    )

    if not mapa:

        raise RuntimeError(
            "Nenhum ativo encontrado "
            "no diretório local nem "
            "no bucket historico"
        )

    quantidade_ok = 0

    erros = []

    for ativo, arquivos in (
        mapa.items()
    ):

        try:

            _processar_ativo(
                ativo,
                arquivos,
            )

            quantidade_ok += 1

        except Exception as erro:

            mensagem = (
                f"{type(erro).__name__}: "
                f"{erro}"
            )

            erros.append(
                (
                    ativo,
                    mensagem,
                )
            )

            if (
                "GEMINI_429"
                in mensagem
            ):

                print(
                    "IA 429 | "
                    f"{ativo} | "
                    f"{mensagem}",
                    flush=True,
                )

            else:

                print(
                    "IA ERRO | "
                    f"{ativo} | "
                    f"{mensagem}",
                    flush=True,
                )

    if erros:

        resumo = "; ".join(
            f"{ativo}: {mensagem}"
            for ativo, mensagem
            in erros
        )

        raise RuntimeError(
            f"Ciclo {motivo} incompleto | "
            f"OK={quantidade_ok} | "
            f"ERROS={len(erros)} | "
            f"{resumo}"
        )

    print(
        "IA CICLO OK | "
        f"motivo={motivo} | "
        f"ativos={quantidade_ok}",
        flush=True,
    )

    return quantidade_ok


# ============================================================
# ROTINA BACKGROUND
# ============================================================

def _rotina_background_ia():

    print(
        "ROTINA IA INICIADA | "
        "09:00-18:00 | "
        "intervalo=5 minutos | "
        "reload I.ARA habilitado",
        flush=True,
    )

    ultimo_slot = None

    while True:

        try:

            agora = (
                datetime.now()
            )

            # ------------------------------------------------
            # Mantemos o horário original da IA.
            # ------------------------------------------------

            if not _dentro_horario_ia(
                agora
            ):

                time.sleep(
                    5
                )

                continue

            # ------------------------------------------------
            # RELOAD MANUAL DA I.ARA
            #
            # Tem prioridade sobre o ciclo normal.
            # Não precisa esperar minuto 00/05/10...
            # ------------------------------------------------

            if _reload_iara_pendente():

                print(
                    "IARA RELOAD | "
                    "iniciando processamento",
                    flush=True,
                )

                try:

                    _executar_ciclo_ia(
                        motivo="RELOAD_IARA"
                    )

                    # ----------------------------------------
                    # SÓ limpa reload se TUDO terminou OK.
                    # ----------------------------------------

                    _concluir_reload_iara()

                    # Consideramos esse slot processado,
                    # evitando chamada duplicada imediata.

                    ultimo_slot = (
                        _slot_cinco_minutos(
                            agora
                        )
                    )

                except Exception as erro:

                    _registrar_erro_reload_iara(
                        erro
                    )

                    print(
                        "IARA RELOAD ERRO | "
                        f"{type(erro).__name__}: "
                        f"{erro}",
                        flush=True,
                    )

                    # ----------------------------------------
                    # IMPORTANTE:
                    #
                    # Não fica martelando Gemini/API a cada
                    # 5 segundos em caso de erro.
                    #
                    # Espera 60 segundos para nova tentativa.
                    # ----------------------------------------

                    time.sleep(
                        60
                    )

                continue

            # ------------------------------------------------
            # CICLO NORMAL
            #
            # Executa em:
            # 00,05,10,15,20...
            # ------------------------------------------------

            if (
                agora.minute
                % 5
                != 0
            ):

                time.sleep(
                    5
                )

                continue

            slot = (
                _slot_cinco_minutos(
                    agora
                )
            )

            if (
                slot
                == ultimo_slot
            ):

                time.sleep(
                    5
                )

                continue

            # Marcamos o slot ANTES do processamento.
            #
            # Isso evita tempestade de requisições
            # caso alguma chamada falhe.
            #
            # Mesma filosofia da correção aplicada
            # na Exportadora.

            ultimo_slot = slot

            try:

                _executar_ciclo_ia(
                    motivo="CICLO_5_MIN"
                )

            except Exception as erro:

                print(
                    "IA CICLO ERRO | "
                    f"{type(erro).__name__}: "
                    f"{erro}",
                    flush=True,
                )

        except Exception as erro:

            print(
                "ROTINA IA ERRO | "
                f"{type(erro).__name__}: "
                f"{erro}",
                flush=True,
            )

        time.sleep(
            5
        )


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

        _THREAD_IA = (
            threading.Thread(
                target=(
                    _rotina_background_ia
                ),
                name="RotinaIA",
                daemon=True,
            )
        )

        _THREAD_IA.start()


# ============================================================
# INÍCIO AUTOMÁTICO
#
# main.py importa funções deste helper.
# Quando o helper é importado, a thread da IA inicia.
# ============================================================

iniciar_rotina_ia()