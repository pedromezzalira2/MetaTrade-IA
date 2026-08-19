from typing import Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config_eas import CONFIGURACAO_EAS


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
    description="API de autorização de novas entradas das EAs V8",
    version="1.0.0",
)


# ============================================================
# DADOS RECEBIDOS DAS EAs V8
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
    simbolo: str
    timeframe: str
    direcao: Literal["BUY", "SELL"]

    preco_sinal: float
    stop_loss: float
    take_profit: float
    volume_solicitado: float = Field(gt=0)

    stops_dia: int = Field(ge=0)
    entradas_dia: int = Field(ge=0)
    lucro_dia: float

    posicao_aberta: bool
    magic_posicao: int
    volume_posicao: float = Field(ge=0)

    timestamp: int


# ============================================================
# RESPOSTA DEVOLVIDA PARA AS EAs V8
# ============================================================

class RespostaAutorizacao(BaseModel):
    autorizado: bool
    volume: float
    motivo: str
    request_id: str


# ============================================================
# AUTENTICAÇÃO
# ============================================================

def validar_token(authorization: str | None) -> None:
    token_esperado = f"Bearer {settings.token}"

    if authorization != token_esperado:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
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
        "eas_cadastradas": len(CONFIGURACAO_EAS),
    }


# ============================================================
# LISTAGEM DAS EAs CADASTRADAS
# ============================================================

@app.get("/v1/eas")
def listar_eas(
    authorization: str | None = Header(default=None),
):
    validar_token(authorization)

    resultado = []

    for nome_ea, configuracao in CONFIGURACAO_EAS.items():
        resultado.append(
            {
                "ea": nome_ea,
                "magic": configuracao["magic"],
                "autorizada": configuracao["autorizada"],
                "contratos": configuracao["contratos"],
            }
        )

    return {
        "quantidade": len(resultado),
        "eas": resultado,
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
    authorization: str | None = Header(default=None),
):
    validar_token(authorization)

    # Procura pelo nome exato enviado pela EA V8.
    configuracao = CONFIGURACAO_EAS.get(pedido.ea)

    # Bloqueia EAs que não estejam cadastradas.
    if configuracao is None:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo="EA não cadastrada na API",
            request_id=pedido.request_id,
        )

    # Confirma que nome e MagicNumber pertencem à mesma EA.
    if pedido.magic != configuracao["magic"]:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo="MagicNumber não corresponde à EA",
            request_id=pedido.request_id,
        )

    # Consulta a variável booleana da EA.
    if not configuracao["autorizada"]:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo="EA desativada pela configuração da API",
            request_id=pedido.request_id,
        )

    # Não permite nova entrada se já existe posição aberta.
    if pedido.posicao_aberta:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo="Já existe posição aberta no símbolo",
            request_id=pedido.request_id,
        )

    contratos = configuracao["contratos"]

    # Bloqueia quantidade inválida.
    if contratos <= 0:
        return RespostaAutorizacao(
            autorizado=False,
            volume=0.0,
            motivo="Quantidade de contratos inválida",
            request_id=pedido.request_id,
        )

    # Autoriza usando a quantidade definida em config_eas.py.
    return RespostaAutorizacao(
        autorizado=True,
        volume=float(contratos),
        motivo="EA autorizada pela configuração da API",
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