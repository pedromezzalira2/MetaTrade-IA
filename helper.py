import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any


_LOCK_CONFIGURACAO = threading.Lock()


def _localizar_bloco_ea(
    conteudo: str,
    nome_ea: str,
) -> tuple[int, int]:

    marcador_duplo = f'"{nome_ea}"'
    marcador_simples = f"'{nome_ea}'"

    inicio = conteudo.find(marcador_duplo)

    if inicio < 0:
        inicio = conteudo.find(marcador_simples)

    if inicio < 0:
        raise ValueError(
            "EA não encontrada no arquivo config_eas.py"
        )

    abertura = conteudo.find("{", inicio)

    if abertura < 0:
        raise ValueError(
            "Bloco da EA inválido no arquivo config_eas.py"
        )

    nivel = 0

    for posicao in range(abertura, len(conteudo)):
        caractere = conteudo[posicao]

        if caractere == "{":
            nivel += 1

        elif caractere == "}":
            nivel -= 1

            if nivel == 0:
                return inicio, posicao + 1

    raise ValueError(
        "Bloco da EA não foi fechado no arquivo config_eas.py"
    )


def _gravar_atomicamente(
    caminho: Path,
    conteudo: str,
) -> None:

    caminho = caminho.resolve()
    arquivo_temporario: str | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=caminho.parent,
            prefix=f".{caminho.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporario:

            arquivo_temporario = temporario.name

            temporario.write(conteudo)
            temporario.flush()

            os.fsync(temporario.fileno())

        os.chmod(
            arquivo_temporario,
            caminho.stat().st_mode,
        )

        os.replace(
            arquivo_temporario,
            caminho,
        )

        arquivo_temporario = None

    finally:
        if arquivo_temporario is not None:
            try:
                os.unlink(arquivo_temporario)
            except FileNotFoundError:
                pass


def atualizar_autorizacao_ea(
    nome_ea: str,
    autorizada: bool,
    configuracao_eas: dict[str, dict[str, Any]],
) -> dict[str, Any]:

    if nome_ea not in configuracao_eas:
        raise KeyError(nome_ea)

    if type(autorizada) is not bool:
        raise TypeError(
            "O campo autorizada precisa ser booleano"
        )

    caminho_config = (
        Path(__file__)
        .resolve()
        .with_name("config_eas.py")
    )

    with _LOCK_CONFIGURACAO:

        conteudo = caminho_config.read_text(
            encoding="utf-8"
        )

        inicio, fim = _localizar_bloco_ea(
            conteudo,
            nome_ea,
        )

        bloco = conteudo[inicio:fim]

        padrao = re.compile(
            r'(["\']autorizada["\']\s*:\s*)(True|False)',
        )

        valor_python = (
            "True" if autorizada else "False"
        )

        bloco_atualizado, quantidade = padrao.subn(
            rf"\g<1>{valor_python}",
            bloco,
            count=1,
        )

        if quantidade != 1:
            raise ValueError(
                "Campo autorizada não encontrado "
                "no bloco da EA em config_eas.py"
            )

        conteudo_atualizado = (
            conteudo[:inicio]
            + bloco_atualizado
            + conteudo[fim:]
        )

        _gravar_atomicamente(
            caminho_config,
            conteudo_atualizado,
        )

        # Atualiza imediatamente a configuração
        # que já está carregada no main.py.
        configuracao_eas[nome_ea][
            "autorizada"
        ] = autorizada

        return {
            "ea": nome_ea,
            "magic": configuracao_eas[
                nome_ea
            ]["magic"],
            "autorizada": configuracao_eas[
                nome_ea
            ]["autorizada"],
            "contratos": configuracao_eas[
                nome_ea
            ]["contratos"],
        }


# ==========================================================
# CONSULTA / GET
# ==========================================================

def obter_autorizacao_ea(
    nome_ea: str,
    configuracao_eas: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    Retorna o estado atual de uma única EA.
    """

    if nome_ea not in configuracao_eas:
        raise KeyError(nome_ea)

    configuracao = configuracao_eas[nome_ea]

    return {
        "ea": nome_ea,
        "magic": configuracao["magic"],
        "autorizada": configuracao["autorizada"],
        "contratos": configuracao["contratos"],
    }


def obter_autorizacoes_eas(
    configuracao_eas: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Retorna o estado atual de todas as EAs.
    """

    resultado = []

    for nome_ea, configuracao in configuracao_eas.items():

        resultado.append(
            {
                "ea": nome_ea,
                "magic": configuracao["magic"],
                "autorizada": configuracao["autorizada"],
                "contratos": configuracao["contratos"],
            }
        )

    return resultado