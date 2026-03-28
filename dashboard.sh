#!/bin/bash
# Live investigation dashboard — V1 columns with dynamic nodes
STATUS_FILE="$1"; JOB_ID="$2"; INV_DIR="$3"
[ -z "$STATUS_FILE" ] && exit 1
printf '\e]2;🔍 Investigation %s\a' "$JOB_ID"
printf '\e[?25l'; trap 'printf "\e[?25h"' EXIT
TICK=0; PULSE=('◦' '○' '◎' '●' '◎' '○')
NAMES=(c1-internet c2-kb c3-context c4-docs c5-internal)
ICONS=("🌐" "📚" "📁" "📖" "🏢")
TAGS=("Web" "KB" "Local" "Docs" "Internal")

get_cols() { stty size < /dev/tty 2>/dev/null | awk '{print $2}'; }
get_rows() { stty size < /dev/tty 2>/dev/null | awk '{print $1}'; }

elapsed_str() {
  local ts="$1"
  local h=${ts%%:*}; local rest=${ts#*:}; local m=${rest%%:*}; local s=${rest#*:}
  local node_sec=$(( 10#$h * 3600 + 10#$m * 60 + 10#$s ))
  local now_sec=$(( 10#$(date +%H) * 3600 + 10#$(date +%M) * 60 + 10#$(date +%S) ))
  local diff=$(( now_sec - node_sec ))
  [ $diff -lt 0 ] && diff=0
  if [ $diff -lt 60 ]; then echo "${diff}s"
  else echo "$((diff/60))m$((diff%60))s"; fi
}

while true; do
  printf '\e[H'; TICK=$((TICK + 1))
  COLS=$(get_cols); ROWS=$(get_rows)
  [ -z "$COLS" ] && COLS=120; [ -z "$ROWS" ] && ROWS=40
  CW=$((COLS / 5))
  LW=$((CW - 4))  # label width per column (after " ● ")
  [ ! -f "$STATUS_FILE" ] && { printf '\033[2K ⏳ Waiting...\n'; sleep 2; continue; }
  PHASE=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('phase','unknown'))" 2>/dev/null)
  ORCH=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('orchestrator','none'))" 2>/dev/null)
  P=${PULSE[$((TICK % ${#PULSE[@]}))]}

  # Header
  printf '\033[2K\033[48;5;236m\033[1;97m 🔍 %-*s %s \033[0m\n' $((COLS - 13)) "Investigation: $JOB_ID" "$(date +%H:%M:%S)"
  case "$PHASE" in
    investigating) printf '\033[2K \033[43;30m INVESTIGATING \033[0m\n' ;;
    validating)    printf '\033[2K \033[44;97m  VALIDATING  \033[0m\n' ;;
    orchestrating) printf '\033[2K \033[45;97m ORCHESTRATING \033[0m\n' ;;
    visualizing)   printf '\033[2K \033[46;97m  VISUALIZING  \033[0m\n' ;;
    complete)      printf '\033[2K \033[42;97m   COMPLETE   \033[0m\n' ;;
    stopped)       printf '\033[2K \033[41;97m   STOPPED    \033[0m\n' ;;
  esac

  # Column headers
  printf '\033[2K'
  for i in 0 1 2 3 4; do
    printf '\033[1m%s %-*s\033[0m' "${ICONS[$i]}" $((CW - 3)) "${TAGS[$i]}"
  done
  echo ""
  printf '\033[2K'
  for i in 0 1 2 3 4; do
    printf '\033[2m'; printf '─%.0s' $(seq 1 $((CW - 1))); printf ' \033[0m'
  done
  echo ""

  # Read node counts
  MAX_NODES=0
  for i in 0 1 2 3 4; do
    NF="$INV_DIR/${NAMES[$i]}/nodes"
    [ -f "$NF" ] && N=$(wc -l < "$NF" | tr -d ' ') || N=0
    eval "NC_$i=$N"
    [ $N -gt $MAX_NODES ] && MAX_NODES=$N
  done

  # Reserve: header(2)+phase(1)+colhdr(2)+merge(2)+val_hdr(2)+val_nodes(8)+merge2(1)+orch(8)+sep(1)+summaries(12)+footer(2)=41
  MAX_NODE_ROWS=$(( ROWS - 41 ))
  [ $MAX_NODE_ROWS -lt 4 ] && MAX_NODE_ROWS=4

  SKIP=0
  if [ $MAX_NODES -gt $MAX_NODE_ROWS ]; then
    SKIP=$((MAX_NODES - MAX_NODE_ROWS))
    printf '\033[2K'
    for i in 0 1 2 3 4; do
      eval "nc=\$NC_$i"
      s=$((nc - MAX_NODE_ROWS))
      if [ $s -gt 0 ]; then
        printf '\033[2m ↑ %d more%-*s\033[0m' "$s" $((CW - 10)) ""
      else
        printf '%-*s' "$CW" ""
      fi
    done
    echo ""
  fi

  # Draw nodes (no connector lines — saves vertical space)
  for row in $(seq $((SKIP + 1)) $MAX_NODES); do
    printf '\033[2K'
    for i in 0 1 2 3 4; do
      NF="$INV_DIR/${NAMES[$i]}/nodes"
      eval "nc=\$NC_$i"
      if [ -f "$NF" ] && [ $row -le $nc ]; then
        LINE=$(sed -n "${row}p" "$NF")
        LABEL=$(echo "$LINE" | cut -d'|' -f2 | cut -c1-$LW)
        inv_s=$(python3 -c "
import json;s=json.load(open('$STATUS_FILE'));c=s.get('children',{}).get('${NAMES[$i]}',{})
print(c.get('inv_status','pending'))" 2>/dev/null)
        if [ $row -eq $nc ] && [ "$inv_s" = "running" ]; then
          TS=$(echo "$LINE" | cut -d'|' -f1)
          ET=$(elapsed_str "$TS")
          SLABEL=$(echo "$LINE" | cut -d'|' -f2 | cut -c1-$((LW - 9)))
          printf '\033[33m %s %-*s\033[0m' "$P" $((CW - 4)) "$SLABEL ($ET)"
        else
          printf '\033[32m ● %-*s\033[0m' $((CW - 4)) "$LABEL"
        fi
      else
        printf '%-*s' "$CW" ""
      fi
    done
    echo ""
  done

  # Merge line
  printf '\033[2K\033[2m └'
  printf '─%.0s' $(seq 1 $((COLS - 4)))
  printf '┘\033[0m\n'

  # ── Validator columns ──
  printf '\033[2K'
  for i in 0 1 2 3 4; do
    printf '\033[1m✓ %-*s\033[0m' $((CW - 3)) "${TAGS[$i]}"
  done
  echo ""
  printf '\033[2K'
  for i in 0 1 2 3 4; do
    printf '\033[2m'; printf '─%.0s' $(seq 1 $((CW - 1))); printf ' \033[0m'
  done
  echo ""

  # Validator node rows
  MAX_VNODES=0
  for i in 0 1 2 3 4; do
    VNF="$INV_DIR/${NAMES[$i]}/val_nodes"
    [ -f "$VNF" ] && N=$(wc -l < "$VNF" | tr -d ' ') || N=0
    eval "VNC_$i=$N"
    [ $N -gt $MAX_VNODES ] && MAX_VNODES=$N
  done
  MAX_VROWS=6
  VSKIP=0
  [ $MAX_VNODES -gt $MAX_VROWS ] && VSKIP=$((MAX_VNODES - MAX_VROWS))

  for row in $(seq $((VSKIP + 1)) $MAX_VNODES); do
    printf '\033[2K'
    for i in 0 1 2 3 4; do
      VNF="$INV_DIR/${NAMES[$i]}/val_nodes"
      eval "vnc=\$VNC_$i"
      if [ -f "$VNF" ] && [ $row -le $vnc ]; then
        LINE=$(sed -n "${row}p" "$VNF")
        LABEL=$(echo "$LINE" | cut -d'|' -f2 | cut -c1-$LW)
        val_s=$(python3 -c "
import json;s=json.load(open('$STATUS_FILE'));c=s.get('children',{}).get('${NAMES[$i]}',{})
print(c.get('val_status','pending'))" 2>/dev/null)
        if [ $row -eq $vnc ] && [ "$val_s" = "running" ]; then
          TS=$(echo "$LINE" | cut -d'|' -f1)
          ET=$(elapsed_str "$TS")
          SLABEL=$(echo "$LINE" | cut -d'|' -f2 | cut -c1-$((LW - 9)))
          printf '\033[36m %s %-*s\033[0m' "$P" $((CW - 4)) "$SLABEL ($ET)"
        else
          printf '\033[36m ● %-*s\033[0m' $((CW - 4)) "$LABEL"
        fi
      else
        printf '%-*s' "$CW" ""
      fi
    done
    echo ""
  done
  [ $MAX_VNODES -eq 0 ] && printf '\033[2K\033[2m%*s(waiting for findings)\033[0m\n' $((COLS/2 - 12)) ""

  # Merge line 2
  printf '\033[2K\033[2m └'
  printf '─%.0s' $(seq 1 $((COLS - 4)))
  printf '┘\033[0m\n'

  # ── Bottom pipeline: synthesize → report → visual report ──
  PAD=$(( COLS/2 - 15 ))
  ONF="$INV_DIR/orchestrator_nodes"

  # Synthesize stage header
  if [ "$PHASE" = "complete" ] || [ "$PHASE" = "visualizing" ]; then
    printf '\033[2K%*s\033[32m● synthesize\033[0m\n' "$PAD" ""
  elif [ "$PHASE" = "orchestrating" ]; then
    if [ -f "$ONF" ]; then
      OTS=$(tail -1 "$ONF" | cut -d'|' -f1)
      OET=$(elapsed_str "$OTS")
      printf '\033[2K%*s\033[33m%s synthesize (%s)\033[0m\n' "$PAD" "" "$P" "$OET"
    else
      printf '\033[2K%*s\033[33m%s synthesize\033[0m\n' "$PAD" "" "$P"
    fi
  else
    printf '\033[2K%*s\033[2m○ synthesize\033[0m\n' "$PAD" ""
  fi

  # Orchestrator sub-nodes (indented under synthesize)
  if [ -f "$ONF" ]; then
    ON=$(wc -l < "$ONF" | tr -d ' ')
    OSTART=$((ON - 3)); [ $OSTART -lt 1 ] && OSTART=1
    for orow in $(seq $OSTART $ON); do
      OLINE=$(sed -n "${orow}p" "$ONF")
      OLABEL=$(echo "$OLINE" | cut -d'|' -f2 | cut -c1-45)
      if [ $orow -eq $ON ] && [ "$PHASE" = "orchestrating" ]; then
        OTS=$(echo "$OLINE" | cut -d'|' -f1)
        OET=$(elapsed_str "$OTS")
        printf '\033[2K%*s  \033[35m%s %s (%s)\033[0m\n' "$PAD" "" "$P" "$OLABEL" "$OET"
      else
        printf '\033[2K%*s  \033[35m● %s\033[0m\n' "$PAD" "" "$OLABEL"
      fi
    done
  fi

  # Report stage header
  if [ -f "$INV_DIR/final_report.md" ]; then
    RLINES=$(wc -l < "$INV_DIR/final_report.md" | tr -d ' ')
    printf '\033[2K%*s\033[32m● report (%sL)\033[0m\n' "$PAD" "" "$RLINES"
  elif [ "$PHASE" = "orchestrating" ]; then
    printf '\033[2K%*s\033[33m%s report\033[0m\n' "$PAD" "" "$P"
  else
    printf '\033[2K%*s\033[2m○ report\033[0m\n' "$PAD" ""
  fi

  # Visual Report stage header
  VNF="$INV_DIR/visual_nodes"
  VISUAL=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('visual','none'))" 2>/dev/null)
  HAS_PDF=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('has_pdf',False))" 2>/dev/null)
  if [ "$HAS_PDF" = "True" ]; then
    printf '\033[2K%*s\033[32m● 📊 visual report (PDF ready)\033[0m\n' "$PAD" ""
  elif [ "$VISUAL" = "running" ]; then
    if [ -f "$VNF" ]; then
      VTS=$(tail -1 "$VNF" | cut -d'|' -f1)
      VET=$(elapsed_str "$VTS")
      printf '\033[2K%*s\033[36m%s 📊 visual report (%s)\033[0m\n' "$PAD" "" "$P" "$VET"
    else
      printf '\033[2K%*s\033[36m%s 📊 visual report\033[0m\n' "$PAD" "" "$P"
    fi
  elif [ "$VISUAL" = "done" ]; then
    printf '\033[2K%*s\033[33m● 📊 generating PDF…\033[0m\n' "$PAD" ""
  else
    printf '\033[2K%*s\033[2m○ 📊 visual report\033[0m\n' "$PAD" ""
  fi

  # Visual report sub-nodes (indented under visual report)
  if [ -f "$VNF" ]; then
    VN=$(wc -l < "$VNF" | tr -d ' ')
    VSTART=$((VN - 2)); [ $VSTART -lt 1 ] && VSTART=1
    for vrow in $(seq $VSTART $VN); do
      VLINE=$(sed -n "${vrow}p" "$VNF")
      VLABEL=$(echo "$VLINE" | cut -d'|' -f2 | cut -c1-45)
      if [ $vrow -eq $VN ] && [ "$VISUAL" = "running" ]; then
        VTS=$(echo "$VLINE" | cut -d'|' -f1)
        VET=$(elapsed_str "$VTS")
        printf '\033[2K%*s  \033[36m%s %s (%s)\033[0m\n' "$PAD" "" "$P" "$VLABEL" "$VET"
      else
        printf '\033[2K%*s  \033[36m● %s\033[0m\n' "$PAD" "" "$VLABEL"
      fi
    done
  fi

  # Separator
  printf '\033[2K\033[2m'
  printf '─%.0s' $(seq 1 $COLS)
  printf '\033[0m\n'

  # Summaries — full width, one agent per block
  for i in 0 1 2 3 4; do
    FPATH="$INV_DIR/${NAMES[$i]}/findings.md"; VPATH="$INV_DIR/${NAMES[$i]}/validated.md"
    NF="$INV_DIR/${NAMES[$i]}/nodes"
    SHOW=""; TAG=""
    [ -f "$VPATH" ] && SHOW="$VPATH" && TAG="validated"
    [ -z "$SHOW" ] && [ -f "$FPATH" ] && SHOW="$FPATH"
    if [ -n "$SHOW" ] && [ -z "$TAG" ]; then
      inv_s=$(python3 -c "
import json;s=json.load(open('$STATUS_FILE'));c=s.get('children',{}).get('${NAMES[$i]}',{})
print(c.get('inv_status','pending'))" 2>/dev/null)
      [ "$inv_s" = "running" ] && TAG="in progress" || TAG="findings"
    fi
    if [ -n "$SHOW" ]; then
      L=$(wc -l < "$SHOW" | tr -d ' ')
      printf '\033[2K%s \033[1m%s\033[0m \033[2m(%s, %sL)\033[0m\n' "${ICONS[$i]}" "${TAGS[$i]}" "$TAG" "$L"
      sed '/^$/d; /^#/d; /^---/d' "$SHOW" | head -3 | while IFS= read -r line; do
        printf '\033[2K  \033[2m%.*s\033[0m\n' $((COLS - 4)) "$line"
      done
    else
      printf '\033[2K%s \033[2m%s …\033[0m\n' "${ICONS[$i]}" "${TAGS[$i]}"
    fi
  done

  # Final report preview
  if [ -f "$INV_DIR/final_report.md" ]; then
    RLINES=$(wc -l < "$INV_DIR/final_report.md" | tr -d ' ')
    printf '\033[2K\n\033[42;97m 📋 REPORT \033[0m \033[2m%s lines\033[0m\n' "$RLINES"
    head -5 "$INV_DIR/final_report.md" | sed '/^$/d' | head -3 | while IFS= read -r line; do
      printf '\033[2K  \033[2m%.*s\033[0m\n' $((COLS - 4)) "$line"
    done
  fi

  printf '\e[J'
  [ "$PHASE" = "complete" ] && { printf '\033[2K\n\033[1;32m ✅ Done. Close tab when finished.\033[0m\n'; printf '\e[?25h'; cat; }
  [ "$PHASE" = "stopped" ] && { printf '\033[2K\n\033[1;31m ⛔ Stopped.\033[0m\n'; printf '\e[?25h'; cat; }
  sleep 2
done
