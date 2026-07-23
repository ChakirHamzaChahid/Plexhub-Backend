#!/usr/bin/env bash
#
# prewarm-dav-cache.sh — préchauffe le cache VFS de rclone (header + tail de
# chaque fichier exposé par /dav) AVANT un scan Plex.
#
# POURQUOI (retour device 2026-07) : pendant un scan, Plex analyse chaque
# fichier (ffprobe-like) en lisant l'en-tête + la fin (moov MP4 / Cues+Tracks
# MKV), et il TIENT une transaction d'écriture SQLite pendant toute cette
# lecture. Sur un flux IPTV relayé (haute latence, cap de connexions serré),
# chaque lecture prend plusieurs secondes → transaction tenue 8-10 s → cascade
# `database is locked` dans Plex (scan bloqué, 0 item indexé). Voir
# docs/30-ops-plex-webdav.md § « Blocage connu ».
#
# LE FIX : lire l'en-tête + la fin de chaque fichier À TRAVERS LE MONTAGE, EN
# SÉRIE (donc ≤ cap de connexions du compte), pendant que Plex est INACTIF.
# rclone (--vfs-cache-mode full) persiste ces octets sur disque local ; ensuite
# les lectures d'analyse de Plex tapent le cache local (rapide) → la transaction
# SQLite n'est plus tenue → plus de cascade de verrous. La lente I/O amont est
# ainsi DÉCOUPLÉE de la transaction Plex.
#
# PRÉREQUIS :
#   - le montage rclone est actif (plexhub-dav.service) ET son unité utilise
#     `--vfs-cache-max-age 720h` (sinon les octets préchauffés sont évincés au
#     bout de 1 h — défaut rclone — avant même le scan) ;
#   - `--vfs-cache-max-size` ≥ (nb d'items exposés) × (HEADER_MB + TAIL_MB) Mo,
#     sinon rclone évince en cours de route (voir la doc, § dimensionnement) ;
#   - Plex N'EST PAS en train de scanner (sinon les deux se disputent le cap de
#     connexions et le découplage est perdu).
#
# USAGE :
#   bash scripts/prewarm-dav-cache.sh [sous-dossier]
#     [sous-dossier]  optionnel, restreint au préchauffage d'un sous-arbre
#                     (ex. "Films" ou "Series/Show (2023)").
#
# RÉGLAGES (variables d'environnement) :
#   DAV_MOUNT               point de montage rclone           (déf. /mnt/plexhub-dav)
#   DAV_PREWARM_HEADER_MB   Mo lus en tête de fichier          (déf. 16)
#   DAV_PREWARM_TAIL_MB     Mo lus en fin de fichier           (déf. 32 — un moov
#                           MP4 sans faststart, en toute fin, peut faire plusieurs Mo)
#   DAV_PREWARM_CONCURRENCY fichiers en parallèle              (déf. 1 = série ;
#                           NE JAMAIS dépasser le max_connections du compte exposé)
#   DAV_PREWARM_LIMIT       stoppe après N fichiers (0 = tous) (déf. 0)
#
# NOTE : le script ne consomme AUCUN octet upstream « en trop » — il ne lit que
# les fenêtres header/tail, comme le fait Plex à l'analyse. La lecture de lecture
# (playback) réelle continue d'aller en direct vers l'amont, non cachée ici.

set -uo pipefail

MOUNT="${DAV_MOUNT:-/mnt/plexhub-dav}"
HEADER_MB="${DAV_PREWARM_HEADER_MB:-16}"
TAIL_MB="${DAV_PREWARM_TAIL_MB:-32}"
CONCURRENCY="${DAV_PREWARM_CONCURRENCY:-1}"
LIMIT="${DAV_PREWARM_LIMIT:-0}"
SUBDIR="${1:-}"

TAIL_BYTES=$((TAIL_MB * 1024 * 1024))
TARGET="$MOUNT${SUBDIR:+/$SUBDIR}"

# --- Préflight ---------------------------------------------------------------
if ! command -v rclone >/dev/null 2>&1; then
  echo "⚠️  rclone introuvable dans le PATH." >&2
fi
if ! mountpoint -q "$MOUNT" 2>/dev/null; then
  echo "❌ '$MOUNT' n'est pas un point de montage actif. Démarre plexhub-dav.service d'abord." >&2
  exit 1
fi
if [ ! -d "$TARGET" ]; then
  echo "❌ '$TARGET' n'existe pas dans le montage. Vérifie le sous-dossier / le rebuild d'arbre." >&2
  exit 1
fi

echo "▶ Préchauffage DAV : $TARGET"
echo "  header=${HEADER_MB}Mo  tail=${TAIL_MB}Mo  concurrency=${CONCURRENCY}  limit=${LIMIT:-0}"
echo "  (Plex doit être INACTIF pendant ce préchauffage.)"
echo

# --- Réchauffe un fichier (header + tail) ------------------------------------
warm_one() {
  local f="$1"
  local size
  size=$(stat -c %s "$f" 2>/dev/null) || { echo "  SKIP (stat KO) : ${f##*/}"; return 1; }

  # En-tête : les N premiers Mo (moov MP4 faststart, EBML/Tracks/SeekHead MKV).
  if ! dd if="$f" of=/dev/null bs=1M count="$HEADER_MB" iflag=fullblock status=none 2>/dev/null; then
    echo "  WARN header : ${f##*/}"; return 1
  fi

  # Fin : les N derniers Mo (moov MP4 non-faststart, Cues MKV) — seulement si le
  # fichier est plus gros que la fenêtre tail (sinon l'en-tête l'a déjà couvert).
  if [ "$size" -gt "$TAIL_BYTES" ]; then
    if ! dd if="$f" of=/dev/null bs=1M iflag=skip_bytes skip=$((size - TAIL_BYTES)) status=none 2>/dev/null; then
      echo "  WARN tail : ${f##*/}"; return 1
    fi
  fi
  return 0
}

# --- Boucle ------------------------------------------------------------------
processed=0
failed=0
running=0
start_ts=$(date +%s)

while IFS= read -r -d '' f; do
  if [ "$CONCURRENCY" -le 1 ]; then
    warm_one "$f" || failed=$((failed + 1))
  else
    warm_one "$f" || true &
    running=$((running + 1))
    if [ "$running" -ge "$CONCURRENCY" ]; then
      wait -n 2>/dev/null || true
      running=$((running - 1))
    fi
  fi

  processed=$((processed + 1))
  if [ $((processed % 10)) -eq 0 ]; then
    printf '\r  … %d fichiers préchauffés' "$processed"
  fi
  if [ "$LIMIT" -gt 0 ] && [ "$processed" -ge "$LIMIT" ]; then
    break
  fi
done < <(find "$TARGET" -type f -print0 2>/dev/null)

wait
elapsed=$(( $(date +%s) - start_ts ))

printf '\r%*s\r' 40 ''   # efface la ligne de progression
echo "✔ Terminé : $processed fichier(s) traité(s), $failed échec(s), en ${elapsed}s."
if [ "$failed" -gt 0 ]; then
  echo "  (Les échecs = flux amont indisponible/503 au moment du préchauffage — relançable, idempotent.)"
fi
echo "  → Lance maintenant le scan Plex (les analyses taperont le cache local)."
