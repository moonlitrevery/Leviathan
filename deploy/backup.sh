#!/usr/bin/env bash
# Backup do banco do Leviathan Bot.
#
# Por que não é um "cp": o banco roda em WAL, então o arquivo .db quente está
# incompleto — parte das escritas confirmadas vive no -wal. Copiar só o .db
# rende um backup silenciosamente desatualizado, e copiar os três arquivos em
# sequência rende uma combinação que nunca existiu. O ".backup" do sqlite3 usa
# a API de backup online: ele lê um retrato consistente com o bot escrevendo.
#
# Uso:
#     deploy/backup.sh                       # usa os padrões abaixo
#     BANCO=/caminho/leviathan.db deploy/backup.sh
#     DESTINO=/mnt/bkp RETENCAO=14 deploy/backup.sh
#
# Com Docker, o banco vive num volume nomeado; a forma de chegar nele está no
# README (seção de backup).

set -euo pipefail

BANCO="${BANCO:-/opt/leviathan/data/leviathan.db}"
DESTINO="${DESTINO:-/opt/leviathan/backups}"
RETENCAO="${RETENCAO:-7}"   # dias de backup mantidos

log() { printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
erro() { log "ERRO: $*" >&2; exit 1; }

command -v sqlite3 >/dev/null 2>&1 || erro "sqlite3 não está instalado (apt install sqlite3)"
[ -f "$BANCO" ] || erro "banco não encontrado em $BANCO"

mkdir -p "$DESTINO"

CARIMBO="$(date '+%Y%m%d-%H%M%S')"
ALVO="$DESTINO/leviathan-$CARIMBO.db"
PARCIAL="$ALVO.parcial"

# .backup é seguro com o bot rodando. O timeout evita desistir na hora se
# houver uma escrita em andamento.
if ! sqlite3 "$BANCO" ".timeout 10000" ".backup '$PARCIAL'"; then
    rm -f "$PARCIAL"
    erro "o .backup do sqlite3 falhou"
fi

# Um backup que não passa no integrity_check não serve de backup.
RESULTADO="$(sqlite3 "$PARCIAL" 'PRAGMA integrity_check;' || true)"
if [ "$RESULTADO" != "ok" ]; then
    rm -f "$PARCIAL"
    erro "o backup saiu corrompido (integrity_check: ${RESULTADO:-sem resposta})"
fi

# Só vira backup de verdade depois de verificado: assim uma execução
# interrompida no meio nunca deixa um arquivo .db pela metade parecendo bom.
mv "$PARCIAL" "$ALVO"
gzip -f "$ALVO"
log "backup criado: $ALVO.gz ($(du -h "$ALVO.gz" | cut -f1))"

# Retenção: apaga o que passou de RETENCAO dias.
APAGADOS="$(find "$DESTINO" -maxdepth 1 -name 'leviathan-*.db.gz' -type f -mtime "+$RETENCAO" -print -delete | wc -l)"
log "retenção de $RETENCAO dia(s): $APAGADOS arquivo(s) apagado(s)"

RESTANTES="$(find "$DESTINO" -maxdepth 1 -name 'leviathan-*.db.gz' -type f | wc -l)"
log "backups guardados: $RESTANTES"
