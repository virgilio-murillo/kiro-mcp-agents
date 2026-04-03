#!/bin/bash
# Live investigation dashboard — with head agent trail
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
  local diff=$(( now_sec - node_sec )); [ $diff -lt 0 ] && diff=0
  [ $diff -lt 60 ] && echo "${diff}s" || echo "$((diff/60))m$((diff%60))s"
}

while true; do
  printf '\e[H'; TICK=$((TICK + 1))
  COLS=$(get_cols); ROWS=$(get_rows)
  [ -z "$COLS" ] && COLS=120; [ -z "$ROWS" ] && ROWS=40
  CW=$((COLS / 5)); LW=$((CW - 4))
  [ ! -f "$STATUS_FILE" ] && { printf '\033[2K ⏳ Waiting...\n'; sleep 2; continue; }
  PHASE=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('phase','unknown'))" 2>/dev/null)
  HEAD_S=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('head_agent','none'))" 2>/dev/null)
  P=${PULSE[$((TICK % ${#PULSE[@]}))]}

  # ── Header (1 line) ──
  HAS_ERR=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('has_error',False))" 2>/dev/null)
  FCOUNT=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('findings_count',0))" 2>/dev/null)
  printf '\033[2K\033[48;5;236m\033[1;97m 🔍 %s' "$JOB_ID"
  case "$PHASE" in
    investigating) printf ' \033[43;30m INV \033[48;5;236m' ;;
    orchestrating) printf ' \033[45;97m ORCH \033[48;5;236m' ;;
    visualizing)   printf ' \033[46;97m VIS \033[48;5;236m' ;;
    complete)      printf ' \033[42;97m DONE \033[48;5;236m' ;;
    stopped)       printf ' \033[41;97m STOP \033[48;5;236m' ;;
  esac
  [ "$HEAD_S" = "running" ] && printf ' \033[33m%s🧠\033[97m' "$P"
  [ "$HEAD_S" = "done" ] && printf ' \033[32m✓🧠\033[97m'
  [ "$HEAD_S" = "stale" ] && printf ' \033[31m⚠🧠stale\033[97m'
  [ "$HAS_ERR" = "True" ] && printf ' \033[31m⚠ERR\033[97m'
  [ "$FCOUNT" != "0" ] && printf ' \033[2m📡%s\033[97m' "$FCOUNT"
  printf '%*s\033[0m\n' $((COLS - 40)) "$(date +%H:%M:%S)"

  # ── Always show 5 columns with nodes ──
  printf '\033[2K'
  for i in 0 1 2 3 4; do printf '\033[1m%s%-*s\033[0m' "${ICONS[$i]}" $((CW - 2)) "${TAGS[$i]}"; done
  echo ""

  MAX_NODES=0
  for i in 0 1 2 3 4; do
    NF="$INV_DIR/${NAMES[$i]}/nodes"; [ -f "$NF" ] && N=$(wc -l < "$NF" | tr -d ' ') || N=0
    eval "NC_$i=$N"; [ $N -gt $MAX_NODES ] && MAX_NODES=$N
  done

  MAX_NR=$((ROWS / 3)); [ $MAX_NR -lt 3 ] && MAX_NR=3
  SKIP=0; [ $MAX_NODES -gt $MAX_NR ] && SKIP=$((MAX_NODES - MAX_NR))

  [ $SKIP -gt 0 ] && {
    printf '\033[2K'
    for i in 0 1 2 3 4; do
      eval "nc=\$NC_$i"; s=$((nc - MAX_NR))
      [ $s -gt 0 ] && printf '\033[2m↑%d%-*s\033[0m' "$s" $((CW - 3)) "" || printf '%-*s' "$CW" ""
    done; echo ""
  }

  for row in $(seq $((SKIP + 1)) $MAX_NODES); do
    printf '\033[2K'
    for i in 0 1 2 3 4; do
      NF="$INV_DIR/${NAMES[$i]}/nodes"; eval "nc=\$NC_$i"
      if [ -f "$NF" ] && [ $row -le $nc ]; then
        LINE=$(sed -n "${row}p" "$NF"); LABEL=$(echo "$LINE" | cut -d'|' -f2 | cut -c1-$LW)
        inv_s=$(python3 -c "import json;s=json.load(open('$STATUS_FILE'));c=s['children']['${NAMES[$i]}'];print(c['inv_status'],c.get('exit_code',''))" 2>/dev/null)
        inv_status=$(echo "$inv_s" | awk '{print $1}')
        exit_code=$(echo "$inv_s" | awk '{print $2}')
        if [ $row -eq $nc ] && [ "$inv_status" = "running" ]; then
          TS=$(echo "$LINE" | cut -d'|' -f1); ET=$(elapsed_str "$TS")
          SL=$(echo "$LINE" | cut -d'|' -f2 | cut -c1-$((LW - 8)))
          printf '\033[33m%s%-*s\033[0m' "$P" $((CW - 1)) "$SL($ET)"
        elif [ $row -eq $nc ] && [ -n "$exit_code" ] && [ "$exit_code" != "0" ] && [ "$exit_code" != "None" ]; then
          printf '\033[31m✗\033[0m%-*s' $((CW - 1)) "$LABEL(exit:$exit_code)"
        else
          printf '\033[32m●\033[0m%-*s' $((CW - 1)) "$LABEL"
        fi
      else
        printf '%-*s' "$CW" ""
      fi
    done; echo ""
  done

  # ── Validator nodes (compact, below child columns) ──
  HAS_VALS=false
  for i in 0 1 2 3 4; do
    [ -f "$INV_DIR/${NAMES[$i]}/val_nodes" ] && HAS_VALS=true
  done
  if [ "$HAS_VALS" = true ]; then
    printf '\033[2K'
    for i in 0 1 2 3 4; do printf '\033[2m'; printf '·%.0s' $(seq 1 $((CW - 1))); printf ' \033[0m'; done
    echo ""
    printf '\033[2K'
    for i in 0 1 2 3 4; do printf '\033[36m✓ %-*s\033[0m' $((CW - 3)) "${TAGS[$i]}"; done
    echo ""
    # Show last 3 validator nodes per child
    MAX_VROWS=3
    for vrow in 1 2 3; do
      printf '\033[2K'
      for i in 0 1 2 3 4; do
        VNF="$INV_DIR/${NAMES[$i]}/val_nodes"
        if [ -f "$VNF" ]; then
          VNC=$(wc -l < "$VNF" | tr -d ' ')
          VSTART=$((VNC - MAX_VROWS)); [ $VSTART -lt 0 ] && VSTART=0
          ACTUAL_ROW=$((VSTART + vrow))
          if [ $ACTUAL_ROW -le $VNC ]; then
            VLINE=$(sed -n "${ACTUAL_ROW}p" "$VNF")
            VLABEL=$(echo "$VLINE" | cut -d'|' -f2 | cut -c1-$LW)
            val_s=$(python3 -c "import json;s=json.load(open('$STATUS_FILE'));print(s['children']['${NAMES[$i]}']['val_status'])" 2>/dev/null)
            if [ $ACTUAL_ROW -eq $VNC ] && [ "$val_s" = "running" ]; then
              VTS=$(echo "$VLINE" | cut -d'|' -f1); VET=$(elapsed_str "$VTS")
              VSL=$(echo "$VLINE" | cut -d'|' -f2 | cut -c1-$((LW - 8)))
              printf '\033[36m%s%-*s\033[0m' "$P" $((CW - 1)) "$VSL($VET)"
            else
              printf '\033[36m●\033[0m%-*s' $((CW - 1)) "$VLABEL"
            fi
          else
            printf '%-*s' "$CW" ""
          fi
        else
          printf '%-*s' "$CW" ""
        fi
      done; echo ""
    done
  fi

  # ── Merge + orchestrator (compact) ──
  printf '\033[2K\033[2m└'; printf '─%.0s' $(seq 1 $((COLS - 3))); printf '┘\033[0m\n'
  ORCH=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('orchestrator','none'))" 2>/dev/null)
  VISUAL=$(python3 -c "import json;print(json.load(open('$STATUS_FILE')).get('visual','none'))" 2>/dev/null)
  printf '\033[2K'
  [ "$ORCH" = "running" ] && printf ' \033[33m%s🧪 Synthesizing\033[0m' "$P"
  [ "$ORCH" = "done" ] && printf ' \033[32m●📋 Report(%sL)\033[0m' "$(wc -l < "$INV_DIR/final_report.md" 2>/dev/null | tr -d ' ')"
  [ "$VISUAL" = "running" ] && printf '  \033[36m%s📊 Visual\033[0m' "$P"
  [ -f "$INV_DIR/visual_report.pdf" ] && printf '  \033[32m●📊 PDF\033[0m'
  echo ""

  # ── HEAD AGENT TRAIL (last 20 updates) ──
  printf '\033[2K\033[48;5;236m\033[1;97m 🧠 HEAD AGENT'
  [ "$HEAD_S" = "running" ] && printf ' %s' "$P"
  [ "$HEAD_S" = "done" ] && printf ' ✓'
  printf '%*s\033[0m\n' $((COLS - 17)) ""

  # Merge: control actions + negotiations + activity into one feed (last 20)
  {
    # Control actions (pure bash parsing)
    CBF="$INV_DIR/control_bus.jsonl"
    [ -f "$CBF" ] && while IFS= read -r cline; do
      act=$(echo "$cline" | sed -n 's/.*"action":"\([^"]*\)".*/\1/p')
      tgt=$(echo "$cline" | sed -n 's/.*"target":"\([^"]*\)".*/\1/p')
      rsn=$(echo "$cline" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p' | cut -c1-100)
      case "$act" in
        kill)            printf '\033[2K \033[31m⊘ KILL\033[0m %s: \033[2m%s\033[0m\n' "$tgt" "$rsn" ;;
        redirect)        printf '\033[2K \033[33m↻ REDIR\033[0m %s: \033[2m%s\033[0m\n' "$tgt" "$rsn" ;;
        skip_validation) printf '\033[2K \033[36m⏭ SKIP\033[0m %s: \033[2m%s\033[0m\n' "$tgt" "$rsn" ;;
        finalize)        printf '\033[2K \033[1;32m✓ FINAL\033[0m \033[2m%s\033[0m\n' "$rsn" ;;
        withdraw)        printf '\033[2K \033[2m↩ WDRAW\033[0m %s: %s\033[0m\n' "$tgt" "$rsn" ;;
      esac
    done < "$CBF"

    # Negotiations
    SF="$INV_DIR/shared_findings.jsonl"
    [ -f "$SF" ] && grep '"type":"argue"' "$SF" 2>/dev/null | while IFS= read -r aline; do
      afrom=$(echo "$aline" | sed -n 's/.*"from":"\([^"]*\)".*/\1/p')
      aagainst=$(echo "$aline" | sed -n 's/.*"against":"\([^"]*\)".*/\1/p')
      arsn=$(echo "$aline" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p' | cut -c1-100)
      printf '\033[2K \033[33m⚡ %s vs %s:\033[0m %s\n' "$afrom" "$aagainst" "$arsn"
    done

    # Activity nodes (filter malformed lines — must contain | separator)
    HNF="$INV_DIR/head_nodes"
    [ -f "$HNF" ] && grep '|' "$HNF" | tail -20 | while IFS= read -r hline; do
      TS=$(echo "$hline" | cut -d'|' -f1)
      MSG=$(echo "$hline" | cut -d'|' -f2-)
      printf '\033[2K   \033[36m%s\033[0m %s\n' "$TS" "$MSG"
    done
  }

  printf '\e[J'
  [ "$PHASE" = "complete" ] && { printf '\033[2K\n\033[1;32m ✅ Done.\033[0m\n'; printf '\e[?25h'; cat; }
  [ "$PHASE" = "stopped" ] && { printf '\033[2K\n\033[1;31m ⛔ Stopped.\033[0m\n'; printf '\e[?25h'; cat; }
  sleep 2
done
