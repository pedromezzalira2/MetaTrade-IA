from typing import Literal

import uvicorn
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    status,
)
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)

from config_eas import CONFIGURACAO_EAS
from helper import atualizar_autorizacao_ea


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
        "API de autorização de novas entradas das EAs V8"
    ),
    version="1.0.0",
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
    direcao: Literal["BUY", "SELL"]

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
# ALTERAÇÃO ENVIADA PELO BUBBLE
# ============================================================

class PedidoAlteracaoAutorizacao(BaseModel):
    ea: str = Field(
        min_length=1,
        max_length=100,
    )

    autorizada: bool


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
    return {
        "status": "online",
        "servico": "api-mt5",
        "versao": "1.0.0",
        "porta": settings.port,
        "eas_cadastradas": len(
            CONFIGURACAO_EAS
        ),
    }


# ============================================================
# LISTAGEM DAS EAs CADASTRADAS
# FILTRADA PELO USUÁRIO
# ============================================================

@app.get("/v1/eas")
def listar_eas(
    usuario: int,
    authorization: str | None = Header(
        default=None
    ),
):
    validar_token(authorization)

    resultado = []

    for nome_ea, configuracao in (
        CONFIGURACAO_EAS.items()
    ):
        # Retorna somente as EAs às quais
        # este usuário possui acesso.
        if usuario not in configuracao["usuario"]:
            continue

        resultado.append(
            {
                "ea": nome_ea,
                "magic": configuracao[
                    "magic"
                ],
                "autorizada": configuracao[
                    "autorizada"
                ],
                "contratos": configuracao[
                    "contratos"
                ],
            }
        )

    return {
        "quantidade": len(resultado),
        "eas": resultado,
    }


# ============================================================
# ALTERAÇÃO DE AUTORIZAÇÃO PELO BUBBLE
# ============================================================

@app.post("/v1/alterar-autorizacao")
def alterar_autorizacao(
    pedido: PedidoAlteracaoAutorizacao,
    authorization: str | None = Header(
        default=None
    ),
):
    validar_token(authorization)

    try:
        configuracao = (
            atualizar_autorizacao_ea(
                nome_ea=pedido.ea,
                autorizada=pedido.autorizada,
                configuracao_eas=(
                    CONFIGURACAO_EAS
                ),
            )
        )

    except KeyError:
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
            ),
            detail="EA não cadastrada na API",
        )

    except (
        OSError,
        TypeError,
        ValueError,
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
    validar_token(authorization)

    configuracao = CONFIGURACAO_EAS.get(
        pedido.ea
    )

    if configuracao is None:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "EA não cadastrada na API"
            ),
            request_id=pedido.request_id,
        )

    if pedido.magic != configuracao["magic"]:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "MagicNumber não corresponde à EA"
            ),
            request_id=pedido.request_id,
        )

    if pedido.usuario not in configuracao["usuario"]:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "Usuário não autorizado para esta EA"
            ),
            request_id=pedido.request_id,
        )

    if not configuracao["autorizada"]:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "EA desativada pela "
                "configuração da API"
            ),
            request_id=pedido.request_id,
        )

    if pedido.posicao_aberta:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "Já existe posição aberta "
                "no símbolo"
            ),
            request_id=pedido.request_id,
        )

    contratos = configuracao["contratos"]

    if contratos <= 0:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo=(
                "Quantidade de contratos inválida"
            ),
            request_id=pedido.request_id,
        )

    return RespostaAutorizacao(
        autorizado=True,
        volume=float(contratos),
        motivo=(
            "EA autorizada pela "
            "configuração da API"
        ),
        request_id=pedido.request_id,
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