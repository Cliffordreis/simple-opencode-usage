#!/usr/bin/env bash
# Verificação anti-segredo: falha se houver algo parecido com uma chave de API
# no conteúdo que seria publicado no GitHub.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0

scan_files() {
    local -a files=()
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        mapfile -d '' files < <(git ls-files -z)
    else
        mapfile -d '' files < <(find . -type f \
            -not -path './.git/*' -not -path './dist/*' -not -name '*.deb' -print0)
    fi
    if (( ${#files[@]} == 0 )); then
        echo "OK: nenhum arquivo para verificar"
        return 0
    fi
    if grep -InE 'oc_sk_[A-Za-z0-9_]{8,}' "${files[@]}"; then
        echo "ERRO: possível chave de API encontrada nos arquivos acima." >&2
        fail=1
    fi
}

scan_files

# Se a chave real estiver no ambiente, garante que ela não aparece em lugar nenhum.
if [[ -n "${OPENCODE_GO_API_KEY:-}" ]]; then
    if grep -rIlF --exclude-dir=.git --exclude-dir=dist -- "$OPENCODE_GO_API_KEY" .; then
        echo "ERRO: a chave presente em OPENCODE_GO_API_KEY foi encontrada nos arquivos acima." >&2
        fail=1
    fi
fi

# Histórico do git (se existir).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git log -p --all 2>/dev/null | grep -nE 'oc_sk_[A-Za-z0-9_]{8,}'; then
        echo "ERRO: chave encontrada no histórico do git." >&2
        fail=1
    fi
fi

if (( fail )); then
    exit 1
fi
echo "OK: nenhuma chave de API detectada."
