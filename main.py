from typing import Literal
from pathlib import Path

import uvicorn

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)

from pydantic import BaseModel, Field

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)

from config_eas import CONFIGURACAO_EAS

from helper import (
    atualizar_autorizacao_ea,
    solicitar_reload_iara,
    obter_estado_reload_iara,
    definir_estado_reload_iara,
)


# ============================================================
# CAMINHO DO RESULTADO DA IA
# ============================================================

PASTA_BASE = Path(__file__).resolve().parent

CAMINHO_RESULTADO_IA = (
    PASTA_BASE
    / "resultIA.py"
)


# ============================================================
# CONFIGURAÇÕES DO ARQUIVO .env
# ============================================================

class Settings(BaseSettings):

    token: str

    port: int = 8000

    auto_reload: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()


# ============================================================
# APLICAÇÃO FASTAPI
# ============================================================

app = FastAPI(
    title="API Orquestradora MT5",
    description=(
        "API de autorização das EAs V8 "
        "e integração com I.ARA"
    ),
    version="1.1.0",
)


# ============================================================
# DADOS RECEBIDOS DAS EAs
# ============================================================

class PedidoEntrada(BaseModel):

    request_id: str = Field(
        min_length=1,
        max_length=200,
    )

    ea: str = Field(
        min_length=1,
        max_length=100,
    )

    versao: str

    magic: int

    usuario: int

    simbolo: str

    timeframe: str

    direcao: Literal[
        "BUY",
        "SELL",
    ]

    preco_sinal: float

    stop_loss: float

    take_profit: float

    volume_solicitado: float = Field(
        gt=0
    )

    stops_dia: int = Field(
        ge=0
    )

    entradas_dia: int = Field(
        ge=0
    )

    lucro_dia: float

    posicao_aberta: bool

    magic_posicao: int

    volume_posicao: float = Field(
        ge=0
    )

    timestamp: int


# ============================================================
# RESPOSTA DEVOLVIDA PARA AS EAs
# ============================================================

class RespostaAutorizacao(BaseModel):

    autorizado: bool

    volume: float

    motivo: str

    request_id: str


# ============================================================
# ALTERAÇÃO ENVIADA PELO BUBBLE / I.ARA
# ============================================================

class PedidoAlteracaoAutorizacao(BaseModel):

    ea: str = Field(
        min_length=1,
        max_length=100,
    )

    autorizada: bool


# ============================================================
# PEDIDO DE RELOAD DA I.ARA
# ============================================================

class PedidoReloadIARA(BaseModel):

    usuario: int | None = None

    origem: str = Field(
        default="IARA",
        min_length=1,
        max_length=100,
    )


class PedidoEstadoReloadIARA(BaseModel):

    reload: bool

    usuario: int | None = None

    origem: str = Field(
        default="EXPORTADORA",
        min_length=1,
        max_length=100,
    )


# ============================================================
# AUTENTICAÇÃO
# ============================================================

def validar_token(
    authorization: str | None,
) -> None:

    token_esperado = (
        f"Bearer {settings.token}"
    )

    if authorization != token_esperado:

        raise HTTPException(
            status_code=(
                status.HTTP_401_UNAUTHORIZED
            ),
            detail="Token inválido ou ausente",
        )


# ============================================================
# ENDPOINT DE SAÚDE
# ============================================================

@app.get("/health")
def health():

    try:

        estado_reload = (
            obter_estado_reload_iara()
        )

    except Exception:

        estado_reload = {
            "reload": False,
            "erro": (
                "não foi possível "
                "consultar estado"
            ),
        }

    return {
        "status": "online",
        "servico": "api-mt5",
        "versao": "1.1.0",
        "porta": settings.port,
        "eas_cadastradas": len(
            CONFIGURACAO_EAS
        ),
        "iara": {
            "reload": estado_reload,
        },
    }

# ============================================================
# RECEBIMENTO DOS HISTÓRICOS DO METATRADER
# ============================================================

PASTA_HISTORICO = PASTA_BASE / "historico"


@app.post("/v1/historico")
async def receber_historico(
    request: Request,
    usuario: int,
    ativo: str,
    arquivo: str,
    authorization: str | None = Header(default=None),
):
    validar_token(authorization)

    nome_arquivo = Path(arquivo).name

    if nome_arquivo != arquivo or not nome_arquivo.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="Nome de arquivo inválido",
        )

    conteudo = await request.body()

    if not conteudo:
        raise HTTPException(
            status_code=400,
            detail="Arquivo vazio",
        )

    PASTA_HISTORICO.mkdir(
        parents=True,
        exist_ok=True,
    )

    destino = PASTA_HISTORICO / nome_arquivo

    destino.write_bytes(conteudo)

    print(
        f"HISTORICO RECEBIDO | "
        f"usuario={usuario} | "
        f"ativo={ativo} | "
        f"arquivo={nome_arquivo} | "
        f"bytes={len(conteudo)}",
        flush=True,
    )

    return {
        "sucesso": True,
        "arquivo": nome_arquivo,
        "bytes": len(conteudo),
    }
# ============================================================
# RELOAD DA I.ARA
#
# POST:
# I.ARA pede uma atualização imediata.
#
# GET:
# I.ARA / Exportadora podem consultar o estado.
#
# O helper.py é quem efetivamente processa os arquivos.
# ============================================================

@app.post("/v1/iara/reload")
def solicitar_reload(
    pedido: PedidoReloadIARA,
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    try:

        estado_reload = (
            solicitar_reload_iara(
                usuario=pedido.usuario,
                origem=pedido.origem,
            )
        )

    except Exception as erro:

        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=(
                "Não foi possível solicitar "
                f"reload da I.ARA: {erro}"
            ),
        )

    return {
        "sucesso": True,
        "mensagem": (
            "Reload da I.ARA solicitado"
        ),
        "estado": estado_reload,
    }


@app.get("/v1/iara/reload")
def consultar_reload(
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    try:

        estado_reload = (
            obter_estado_reload_iara()
        )

    except Exception as erro:

        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=(
                "Não foi possível consultar "
                f"reload da I.ARA: {erro}"
            ),
        )

    return estado_reload


@app.patch("/v1/iara/reload")
@app.put("/v1/iara/reload")
def atualizar_estado_reload(
    pedido: PedidoEstadoReloadIARA,
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    try:

        estado_reload = (
            definir_estado_reload_iara(
                reload=pedido.reload,
                usuario=pedido.usuario,
                origem=pedido.origem,
            )
        )

    except Exception as erro:

        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=(
                "Não foi possível atualizar "
                f"o estado do reload da I.ARA: {erro}"
            ),
        )

    return {
        "sucesso": True,
        "estado": estado_reload,
    }


# ============================================================
# CONSULTA DO RESULTADO ATUAL DA IA
# ============================================================

@app.get("/v1/resultia")
def consultar_resultado_ia(
    ativo: str,
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    ativo = (
        ativo
        .strip()
        .upper()
    )

    if not CAMINHO_RESULTADO_IA.exists():

        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "resultIA.py ainda não existe"
            ),
        )

    try:

        namespace = {}

        codigo = (
            CAMINHO_RESULTADO_IA
            .read_text(
                encoding="utf-8"
            )
        )

        exec(
            codigo,
            {
                "__builtins__": {}
            },
            namespace,
        )

        resultados = (
            namespace.get(
                "RESULTADOS_IA",
                {},
            )
        )

        if not isinstance(
            resultados,
            dict,
        ):

            raise RuntimeError(
                "RESULTADOS_IA inválido"
            )

    except Exception as erro:

        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=(
                "Não foi possível ler "
                f"resultIA.py: {erro}"
            ),
        )

    resultado = (
        resultados.get(
            ativo
        )
    )

    if resultado is None:

        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
            ),
            detail=(
                "Resultado da IA não encontrado "
                f"para o ativo {ativo}"
            ),
        )

    return resultado


# ============================================================
# LISTAGEM DAS EAs CADASTRADAS
# FILTRADA PELO USUÁRIO
#
# IMPORTANTE:
# A API é a fonte da verdade.
# A Exportadora não precisa conhecer IDs de contas.
# ============================================================

@app.get("/v1/eas")
def listar_eas(
    usuario: int,
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    resultado = []

    for nome_ea, configuracao in (
        CONFIGURACAO_EAS.items()
    ):

        usuarios = (
            configuracao.get(
                "usuario",
                [],
            )
        )

        if usuario not in usuarios:
            continue

        resultado.append(
            {
                "ea": nome_ea,

                "magic": (
                    configuracao[
                        "magic"
                    ]
                ),

                "autorizada": (
                    configuracao[
                        "autorizada"
                    ]
                ),

                "contratos": (
                    configuracao[
                        "contratos"
                    ]
                ),
            }
        )

    return {
        "quantidade": len(
            resultado
        ),
        "eas": resultado,
    }

@app.post("/v1/iara/reload/consume")
def consumir_reload_iara(
    usuario: int,
    authorization: str | None = Header(default=None),
):

    validar_token(authorization)

    try:
        estado_reload = definir_estado_reload_iara(
            reload=False,
            usuario=usuario,
            origem="EXPORTADORA",
        )

    except Exception as erro:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Não foi possível consumir "
                f"o reload da I.ARA: {erro}"
            ),
        )

    return {
        "sucesso": True,
        "reload": False,
        "estado": estado_reload,
    }
# ============================================================
# ALTERAÇÃO DE AUTORIZAÇÃO PELO BUBBLE / I.ARA
# ============================================================

@app.post(
    "/v1/alterar-autorizacao"
)
def alterar_autorizacao(
    pedido: PedidoAlteracaoAutorizacao,
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    try:

        sucesso = (
            atualizar_autorizacao_ea(
                nome_ea=pedido.ea,
                autorizada=pedido.autorizada,
            )
        )

        if not sucesso:

            raise KeyError(
                pedido.ea
            )

        configuracao = {
            "ea": pedido.ea,
            **CONFIGURACAO_EAS[
                pedido.ea
            ],
        }

    except KeyError:

        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
            ),
            detail=(
                "EA não cadastrada na API"
            ),
        )

    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as erro:

        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=(
                "Não foi possível atualizar "
                f"a configuração: {erro}"
            ),
        )

    return {
        "sucesso": True,
        "mensagem": (
            "Autorização atualizada"
        ),
        **configuracao,
    }


# ============================================================
# AUTORIZAÇÃO DE NOVAS ENTRADAS
# ============================================================

@app.post(
    "/v1/autorizar-entrada",
    response_model=RespostaAutorizacao,
)
def autorizar_entrada(
    pedido: PedidoEntrada,
    authorization: str | None = Header(
        default=None
    ),
):

    validar_token(
        authorization
    )

    configuracao = (
        CONFIGURACAO_EAS.get(
            pedido.ea
        )
    )

    # --------------------------------------------------------
    # EA precisa existir
    # --------------------------------------------------------

    if configuracao is None:

        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "EA não cadastrada na API"
            ),
            request_id=(
                pedido.request_id
            ),
        )

    # --------------------------------------------------------
    # MAGIC precisa corresponder
    # --------------------------------------------------------

    if (
        pedido.magic
        != configuracao["magic"]
    ):

        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "MagicNumber não corresponde "
                "à EA"
            ),
            request_id=(
                pedido.request_id
            ),
        )

    # --------------------------------------------------------
    # USUÁRIO precisa estar autorizado para essa EA
    # --------------------------------------------------------

    usuarios_permitidos = (
        configuracao.get(
            "usuario",
            [],
        )
    )

    if (
        pedido.usuario
        not in usuarios_permitidos
    ):

        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "Usuário não autorizado "
                "para esta EA"
            ),
            request_id=(
                pedido.request_id
            ),
        )

    # --------------------------------------------------------
    # EA precisa estar ligada
    # --------------------------------------------------------

    if not configuracao.get(
        "autorizada",
        False,
    ):

        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "EA desativada pela "
                "configuração da API"
            ),
            request_id=(
                pedido.request_id
            ),
        )

    # --------------------------------------------------------
    # Não permite nova entrada se já houver posição
    # --------------------------------------------------------

    if pedido.posicao_aberta:

        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "Já existe posição aberta "
                "no símbolo"
            ),
            request_id=(
                pedido.request_id
            ),
        )

    # --------------------------------------------------------
    # CONTRATOS definidos pela API
    # --------------------------------------------------------

    contratos = (
        configuracao.get(
            "contratos",
            0,
        )
    )

    if contratos <= 0:

        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "Quantidade de contratos "
                "inválida"
            ),
            request_id=(
                pedido.request_id
            ),
        )

    # --------------------------------------------------------
    # AUTORIZADO
    # --------------------------------------------------------

    return RespostaAutorizacao(
        autorizado=True,
        volume=float(
            contratos
        ),
        motivo=(
            "EA autorizada pela "
            "configuração da API"
        ),
        request_id=(
            pedido.request_id
        ),
    )


# ============================================================
# INICIALIZAÇÃO DO UVICORN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
        reload=settings.auto_reload,
    )